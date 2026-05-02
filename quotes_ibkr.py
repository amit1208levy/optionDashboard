"""
Interactive Brokers Gateway / TWS quotes provider.

Implements the QuotesProvider interface over ``ib_insync``.  All
ib_insync calls are serialised onto a single dedicated background
thread (which owns the asyncio loop and the IB connection) so callers
on any thread (PortfolioWorker, watchlist worker, etc.) can use the
provider concurrently without thread-safety issues.

REST snapshot
-------------
``get_quotes()`` qualifies the contracts (cached after the first call),
subscribes to streaming market data, waits briefly for ticks to populate,
and returns the current Ticker state — then leaves the subscriptions
open so subsequent calls return immediately with fresh data.  This trades
a bit of memory (one Ticker per symbol) for ~10× faster repeated calls
and is exactly how IBKR's own clients drive their dashboards.

Streaming
---------
``start_stream()`` reuses the same persistent subscriptions.  Whenever
ib_insync emits ``pendingTickersEvent`` we map each Ticker back to its
TastyTrade-format symbol via the reverse cache and forward the changed
fields to the user's ``on_update`` callback.

Greeks
------
For options, ib_insync exposes computed Greeks on
``Ticker.modelGreeks`` (delta / gamma / theta / vega / impliedVol /
undPrice).  We rename ``impliedVol``→``implied-volatility`` and
``undPrice``→``underlying-price`` to match the dashboard's TT-style
NORMALIZED_KEYS contract.

Limitations
-----------
* Futures-options symbols (``./ESM4 …``) are not yet supported by
  ``ibkr_symbols.tt_to_contract`` — those symbols silently fall through
  (they appear as a "miss" in the result dict so the hybrid wrapper can
  fall back to TastyTrade for them).
* Probability-OTM is not provided by IBKR; consumers retain the last
  TastyTrade value for that field.
"""
from __future__ import annotations

import asyncio
import math
import threading
from typing import Callable, Iterable, Optional

from ib_insync import IB, Ticker, Contract

from quotes_provider import QuotesProvider, StreamHandle
import ibkr_symbols


# ── helpers ──────────────────────────────────────────────────────────────────

def _f(v) -> Optional[float]:
    """Safe float that returns None for NaN / non-numeric values."""
    try:
        x = float(v)
        return x if x == x else None       # NaN guard (NaN != NaN)
    except (TypeError, ValueError):
        return None


def _ticker_to_normalized(t: Ticker) -> Optional[dict]:
    """Convert ib_insync Ticker → dashboard NORMALIZED_KEYS dict."""
    if t is None:
        return None
    out: dict = {}

    bid  = _f(t.bid)
    ask  = _f(t.ask)
    last = _f(t.last)
    if bid  is not None: out["bid"]  = bid
    if ask  is not None: out["ask"]  = ask
    if last is not None: out["last"] = last

    # Mark = midpoint when both sides are quoted, else last, else IBKR's
    # marketPrice() (a curated fallback).
    if bid and ask and bid > 0 and ask > 0:
        out["mark"] = (bid + ask) / 2.0
    elif last and last > 0:
        out["mark"] = last
    else:
        mp = _f(t.marketPrice())
        if mp is not None and mp > 0:
            out["mark"] = mp

    # Greeks (options only) — ib_insync populates modelGreeks on options.
    mg = getattr(t, "modelGreeks", None)
    if mg is not None:
        for src, dst in (("delta", "delta"),
                         ("gamma", "gamma"),
                         ("theta", "theta"),
                         ("vega",  "vega")):
            v = _f(getattr(mg, src, None))
            if v is not None:
                out[dst] = v
        iv = _f(getattr(mg, "impliedVol", None))
        if iv is not None:
            out["implied-volatility"] = iv
        up = _f(getattr(mg, "undPrice", None))
        if up is not None:
            out["underlying-price"] = up

    return out or None


# ── stream handle ────────────────────────────────────────────────────────────

class _IBKRStreamHandle(StreamHandle):
    """Holds the live subscription set + user callbacks for one stream."""

    def __init__(self, on_update, on_status, symbols):
        self.on_update = on_update
        self.on_status = on_status
        self.subscribed_tt: set[str] = set(symbols)
        self.disconnect_handler = None    # filled when handler is registered
        self.ticker_handler = None


# ── provider ─────────────────────────────────────────────────────────────────

class IBKRQuotesProvider(QuotesProvider):
    """
    QuotesProvider backed by IBKR Gateway / TWS via ib_insync.

    Parameters
    ----------
    host, port, client_id
        Where to find Gateway.  Default port 4001 = live Gateway.
        client_id can be any unused integer; multiple clients may
        connect to the same Gateway concurrently.
    market_data_type
        IBKR market-data tier to request.
            1 = real-time  (requires OPRA/CME subs)
            2 = frozen     (last-known close — no subscription needed)
            3 = delayed    (15-min delay — free)
            4 = delayed-frozen
        Default 1; falls back to 3 automatically per-symbol if 1 returns
        nothing within the wait window.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 4001,
                 client_id: int = 42, market_data_type: int = 1):
        self._host = host
        self._port = port
        self._client_id = client_id
        self._market_data_type = market_data_type

        self._ib: Optional[IB] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()
        self._loop_lock = threading.Lock()

        # Maps shared between submission threads and the IO loop.
        # Always read/written from the IO loop except where noted.
        self._contracts_by_tt: dict[str, Contract] = {}
        self._tickers_by_tt:   dict[str, Ticker]   = {}
        self._tt_by_conid:     dict[int, str]      = {}   # for reverse lookup
        self._streams:         list[_IBKRStreamHandle] = []

        # Background thread that owns the asyncio loop + IB instance.
        self._thread = threading.Thread(
            target=self._thread_main,
            name="IBKRQuotesProvider",
            daemon=True,
        )
        self._thread.start()
        self._loop_ready.wait(timeout=5)

    # ── thread / loop lifecycle ────────────────────────────────────────────

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def _submit(self, coro, timeout: float = 10.0):
        """Schedule a coroutine on the IO loop and block for the result."""
        if self._loop is None or self._loop.is_closed():
            return None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            # Surface the error at the call site instead of swallowing it.
            print(f"[ibkr] submit error: {e}", flush=True)
            return None

    # ── connection ─────────────────────────────────────────────────────────

    async def _ensure_connected(self) -> bool:
        if self._ib is not None and self._ib.isConnected():
            return True
        if self._ib is None:
            self._ib = IB()
            # Re-key streaming-tick events so they fan out to caller callbacks.
            self._ib.pendingTickersEvent += self._on_pending_tickers_loop
            self._ib.disconnectedEvent   += self._on_disconnected_loop
        try:
            await self._ib.connectAsync(
                self._host, self._port,
                clientId=self._client_id,
                readonly=True,
                timeout=5,
            )
            self._ib.reqMarketDataType(self._market_data_type)
            return True
        except Exception as e:
            print(f"[ibkr] connect failed: {e}", flush=True)
            self._notify_status_all(f"error:{e}")
            return False

    def is_connected(self) -> bool:
        return bool(self._ib and self._ib.isConnected())

    # ── Account data (portfolio + summary) ────────────────────────────────

    def get_portfolio(self) -> list:
        """Return list of ib_insync.PortfolioItem for the connected account."""
        return self._submit(self._async_portfolio(), timeout=10.0) or []

    async def _async_portfolio(self) -> list:
        if not await self._ensure_connected():
            return []
        try:
            import asyncio

            # ib_insync.connect() automatically subscribes to account updates,
            # so portfolio() will be populated shortly after the connection.
            # The `subscribe` keyword on reqAccountUpdatesAsync was removed in
            # newer ib_insync versions, so we don't call it explicitly — just
            # poll until data arrives or we time out (~6 s).
            items = list(self._ib.portfolio())
            if items:
                return items
            for _ in range(12):
                await asyncio.sleep(0.5)
                items = list(self._ib.portfolio())
                if items:
                    return items
            return items   # may be [] if account truly has no positions
        except Exception as e:
            print(f"[ibkr] portfolio(): {e}", flush=True)
            return []

    def get_account_summary(self) -> dict:
        """Return {tag: value_str} for the primary account from accountSummary."""
        return self._submit(self._async_account_summary(), timeout=10.0) or {}

    async def _async_account_summary(self) -> dict:
        if not await self._ensure_connected():
            return {}
        try:
            rows = await self._ib.accountSummaryAsync()
            # Keep USD values only (BASE is a synthetic multi-currency total;
            # USD is what we want for single-currency accounts).
            out = {}
            for r in rows:
                if r.currency in ("USD", "BASE", ""):
                    # Prefer USD over BASE if both exist.
                    if r.tag not in out or r.currency == "USD":
                        out[r.tag] = r.value
            return out
        except Exception as e:
            print(f"[ibkr] accountSummary(): {e}", flush=True)
            return {}

    def disconnect(self):
        async def _dc():
            try:
                if self._ib and self._ib.isConnected():
                    self._ib.disconnect()
            except Exception:
                pass
        self._submit(_dc(), timeout=2.0)

    # ── REST snapshot ──────────────────────────────────────────────────────

    def get_quotes(
        self,
        equity_options: Iterable[str] = (),
        future_options: Iterable[str] = (),
        equities:       Iterable[str] = (),
        futures:        Iterable[str] = (),
    ) -> dict[str, dict]:
        wanted = (list(equities) + list(equity_options)
                  + list(futures) + list(future_options))
        wanted = [s for s in wanted if s]
        if not wanted:
            return {}
        return self._submit(self._async_get_quotes(wanted), timeout=15.0) or {}

    async def _async_get_quotes(self, tt_symbols: list[str]) -> dict[str, dict]:
        if not await self._ensure_connected():
            return {}

        # 1. Build / cache contracts for every requested symbol.
        contracts_to_qualify: list[Contract] = []
        request_tt: list[str] = []
        for tt in tt_symbols:
            if tt in self._contracts_by_tt:
                request_tt.append(tt)
                continue
            try:
                c = ibkr_symbols.tt_to_contract(tt)
            except ibkr_symbols.UnsupportedSymbol:
                # Caller (hybrid wrapper) will handle the gap.
                continue
            self._contracts_by_tt[tt] = c
            contracts_to_qualify.append(c)
            request_tt.append(tt)

        if contracts_to_qualify:
            try:
                await self._ib.qualifyContractsAsync(*contracts_to_qualify)
            except Exception as e:
                print(f"[ibkr] qualify failed: {e}", flush=True)

        # 2. For each symbol with a valid conId, ensure it's subscribed.
        new_subs = []
        for tt in request_tt:
            c = self._contracts_by_tt.get(tt)
            if c is None or not c.conId:
                continue
            if tt not in self._tickers_by_tt:
                t = self._ib.reqMktData(c, "", snapshot=False, regulatorySnapshot=False)
                self._tickers_by_tt[tt] = t
                self._tt_by_conid[c.conId] = tt
                new_subs.append(tt)

        # 3. Wait briefly for fresh ticks if we just subscribed.
        if new_subs:
            try:
                await asyncio.sleep(1.5)
            except asyncio.CancelledError:
                raise

        # 4. Read current state for each requested symbol.
        out: dict[str, dict] = {}
        for tt in request_tt:
            t = self._tickers_by_tt.get(tt)
            q = _ticker_to_normalized(t)
            if q:
                q["symbol"] = tt
                out[tt] = q
        return out

    # ── Streaming ──────────────────────────────────────────────────────────

    def start_stream(
        self,
        symbols:   Iterable[str],
        on_update: Callable[[dict], None],
        on_status: Callable[[str], None],
    ) -> Optional[_IBKRStreamHandle]:
        symbols = [s for s in symbols if s]
        handle = _IBKRStreamHandle(on_update, on_status, symbols)
        self._streams.append(handle)

        def _fire():
            on_status("connecting")
        try:
            _fire()
        except Exception:
            pass

        self._submit(self._async_subscribe(symbols), timeout=15.0)
        # _on_pending_tickers_loop will start firing on_update; status
        # 'connected' fires from there once the first tick batch is seen.
        return handle

    def update_subscription(
        self,
        handle:  _IBKRStreamHandle,
        symbols: Iterable[str],
    ) -> None:
        if handle is None:
            return
        new_set = set(s for s in symbols if s)
        added   = new_set - handle.subscribed_tt
        # We never aggressively unsubscribe — keeping subs warm makes the
        # next get_quotes() instant.  Memory cost is small and a single
        # client has a generous IBKR market-data line budget.
        handle.subscribed_tt = new_set
        if added:
            self._submit(self._async_subscribe(list(added)), timeout=10.0)

    def stop_stream(self, handle: _IBKRStreamHandle) -> None:
        if handle in self._streams:
            self._streams.remove(handle)
        try:
            handle.on_status("disconnected")
        except Exception:
            pass

    async def _async_subscribe(self, tt_symbols: list[str]):
        """Idempotently subscribe ``tt_symbols`` to streaming market data."""
        if not await self._ensure_connected():
            return
        # Reuse get_quotes' qualify+subscribe path — it's already idempotent.
        await self._async_get_quotes(tt_symbols)

    # ── ib_insync event handlers (run on the IO loop) ──────────────────────

    def _on_pending_tickers_loop(self, tickers):
        """ib_insync emits this whenever any subscribed Ticker has new data."""
        if not self._streams:
            return
        batch: dict[str, dict] = {}
        for t in tickers:
            tt = self._tt_by_conid.get(t.contract.conId) if t.contract else None
            if not tt:
                continue
            q = _ticker_to_normalized(t)
            if q:
                q["symbol"] = tt
                batch[tt] = q
        if not batch:
            return
        for h in list(self._streams):
            try:
                h.on_update(batch)
            except Exception as e:
                print(f"[ibkr] on_update raised: {e}", flush=True)

    def _on_disconnected_loop(self):
        self._notify_status_all("disconnected")

    def _notify_status_all(self, status: str):
        for h in list(self._streams):
            try:
                h.on_status(status)
            except Exception:
                pass
