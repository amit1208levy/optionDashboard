"""
YahooQuotesProvider — last-resort quote source when IBKR and TastyTrade
are both unavailable.

Yahoo Finance is free, requires no auth, and serves option chains via
two undocumented endpoints:

    https://query2.finance.yahoo.com/v7/finance/options/{root}?date={unix}
    https://query1.finance.yahoo.com/v8/finance/chart/{root}?interval=1m&range=1d

It comes with hard limits the caller must accept:

  • Equity options only. NO futures options — `./CL...` / `./ES...` symbols
    will silently get an empty result.
  • Quotes are typically ~15 minutes delayed. Each returned quote dict
    includes ``"source": "yahoo"`` and ``"delayed_minutes": 15`` so the
    UI can badge it.
  • Greeks are not returned by Yahoo. We compute delta / gamma / theta /
    vega locally via Black-Scholes (``bs_greeks.compute_greeks``) using
    Yahoo's IV. Accurate ±2% for liquid ATM contracts; less so for wings.
  • Yahoo changes its endpoints every few months. All endpoint knowledge
    is isolated in this file so the rest of the codebase is unaffected.

Streaming is not supported (Yahoo has no push interface). The streaming
methods are no-ops that satisfy the QuotesProvider contract.
"""
from __future__ import annotations

import re
import time
import threading
from typing import Callable, Iterable, Optional

import requests

import bs_greeks
from quotes_provider import QuotesProvider, StreamHandle


# ── Yahoo HTTP endpoints ──────────────────────────────────────────────────────

_OPTIONS_URL = "https://query2.finance.yahoo.com/v7/finance/options/{root}"
_CHART_URL   = "https://query1.finance.yahoo.com/v8/finance/chart/{root}"
_CRUMB_URL   = "https://query1.finance.yahoo.com/v1/test/getcrumb"
_COOKIE_URL  = "https://fc.yahoo.com/"
_UA          = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " \
               "AppleWebKit/605.1.15 OptionsDashboard/1.0"
# Don't send `Accept: application/json`. The crumb endpoint returns
# `text/plain` and explicitly rejects an Accept: application/json header
# with HTTP 406. The chart/options endpoints return JSON regardless of
# what we send.
_HEADERS     = {"User-Agent": _UA}

# Crumb refresh interval — Yahoo crumbs can survive hours but we re-fetch
# every 2 h to be safe. If a request fails with 401 we also force-refresh.
_CRUMB_TTL_SECONDS = 7200.0

# Cache TTL: a single (root, expiry) chain is fetched at most this often.
# 30 s covers two consecutive 15 s refresh ticks; Yahoo's data isn't
# realtime anyway.
_CACHE_TTL_SECONDS = 30.0

# Network timeouts: (connect, read). A stalled connection raises instead
# of freezing the dashboard.
_TIMEOUT = (5, 10)


# ── OCC option symbol parsing ─────────────────────────────────────────────────
#
# TastyTrade's wire format for equity options IS the OCC format with the
# underlying root left-padded to 6 characters with spaces, e.g.
#
#     "AAPL  240419P00150000"
#      ^^^^^^------|||--------
#     root (6)    exp  C/P   strike (8 digits, x1000)
#
# The regex below tolerates roots of 1–6 chars (we'll normalize on output).

_OCC_RE = re.compile(
    r"^(?P<root>[A-Z]{1,6})\s*"      # 1-6 letter root, optional padding
    r"(?P<yy>\d{2})"
    r"(?P<mm>\d{2})"
    r"(?P<dd>\d{2})"
    r"(?P<cp>[CP])"
    r"(?P<strike>\d{8})$"
)


def _parse_option_symbol(sym: str):
    """
    Parse a TT-format equity option into its components.

    Returns ``(root, expiry_yyyymmdd, is_call, strike_float)`` on success,
    or ``None`` for anything we can't parse (futures options, malformed
    strings, etc.).
    """
    if not sym:
        return None
    m = _OCC_RE.match(sym.strip())
    if not m:
        return None
    yyyy = 2000 + int(m["yy"])
    mm   = int(m["mm"])
    dd   = int(m["dd"])
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    strike = int(m["strike"]) / 1000.0
    is_call = (m["cp"] == "C")
    return m["root"], f"{yyyy:04d}-{mm:02d}-{dd:02d}", is_call, strike


def _pad_root(root: str) -> str:
    """Left-pad an underlying root to 6 characters for TT wire format."""
    return f"{root:<6}"


def _occ_symbol_tt(root: str, expiry_yyyymmdd: str, is_call: bool,
                   strike: float) -> str:
    """Rebuild the canonical TT-wire-format symbol from components."""
    yy = expiry_yyyymmdd[2:4]
    mm = expiry_yyyymmdd[5:7]
    dd = expiry_yyyymmdd[8:10]
    cp = "C" if is_call else "P"
    strike_i = int(round(strike * 1000))
    return f"{_pad_root(root)}{yy}{mm}{dd}{cp}{strike_i:08d}"


def _expiry_to_unix_midnight_utc(yyyy: int, mm: int, dd: int) -> int:
    """
    Yahoo's `?date=` parameter is the option-chain expiry as Unix seconds
    at **midnight UTC** of the expiry date. We previously passed noon UTC,
    which silently fell off the bucket and returned an unrelated chain.
    """
    import datetime as dt
    return int(dt.datetime(yyyy, mm, dd, 0, 0, 0,
                           tzinfo=dt.timezone.utc).timestamp())


# ── Provider ─────────────────────────────────────────────────────────────────

class YahooQuotesProvider(QuotesProvider):
    """
    Tertiary quote source. Use as the bottom of `HybridQuotesProvider`'s
    fallback chain — it activates only for symbols the higher-priority
    providers couldn't fill.

    Thread-safe-ish: the in-memory cache is a plain dict, fine for the
    dashboard's serialized refresh pattern. Don't share one instance
    across threads doing concurrent get_quotes() calls.
    """

    def __init__(self):
        # Cache: (root, expiry_yyyymmdd) → (timestamp, options_payload)
        self._chain_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        # Cache: root → (timestamp, spot_price)
        self._spot_cache:  dict[str, tuple[float, float]]            = {}
        # One requests.Session keeps cookies across calls. Crumb token
        # is fetched lazily and refreshed periodically.
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._crumb: Optional[str] = None
        self._crumb_ts: float = 0.0
        self._crumb_lock = threading.Lock()

    # ── Crumb / cookie handshake ──────────────────────────────────────────
    #
    # Since mid-2023 Yahoo's finance API requires a "crumb" anti-CSRF token
    # on most endpoints. The dance:
    #   1. Hit fc.yahoo.com to plant the consent / GUC cookies in our jar.
    #   2. GET /v1/test/getcrumb with those cookies — returns a short
    #      token string in the response body.
    #   3. Include ?crumb=<token> on every subsequent request, with the
    #      cookies along for the ride.
    # The crumb is tied to the session cookies, so if we drop the cookies
    # we have to refetch the crumb.

    def _refresh_crumb(self) -> Optional[str]:
        with self._crumb_lock:
            # Best-effort cookie warmup — fc.yahoo.com now returns 404 and
            # doesn't actually plant cookies, but the call may still set
            # consent cookies on some regions. Tolerate any outcome.
            try:
                self._session.get(_COOKIE_URL, timeout=_TIMEOUT,
                                  allow_redirects=True)
            except requests.RequestException:
                pass
            try:
                r = self._session.get(_CRUMB_URL, timeout=_TIMEOUT)
                if r.status_code == 200 and r.text and len(r.text) < 100:
                    self._crumb = r.text.strip()
                    self._crumb_ts = time.time()
                    print(f"[yahoo] new crumb acquired", flush=True)
                    return self._crumb
                print(f"[yahoo] crumb HTTP {r.status_code}: "
                      f"{r.text[:120]!r}", flush=True)
            except requests.RequestException as e:
                print(f"[yahoo] crumb fetch failed: {e}", flush=True)
            return None

    def _get_crumb(self, force: bool = False) -> Optional[str]:
        if (force
            or self._crumb is None
            or (time.time() - self._crumb_ts) > _CRUMB_TTL_SECONDS):
            return self._refresh_crumb()
        return self._crumb

    # ── REST snapshot ──────────────────────────────────────────────────────

    def get_quotes(
        self,
        equity_options: Iterable[str] = (),
        future_options: Iterable[str] = (),
        equities:       Iterable[str] = (),
        futures:        Iterable[str] = (),
    ) -> dict[str, dict]:
        out: dict[str, dict] = {}

        # Futures + futures options: Yahoo can't help.
        _ = list(future_options); _ = list(futures)

        eq_opts = [s for s in equity_options if s]
        eq      = [s for s in equities       if s]

        # ── Bucket option requests by (root, expiry) ─────────────────────
        # Multiple legs of the same strategy on the same chain → one HTTP call.
        chain_buckets: dict[tuple[str, str], list[tuple[str, bool, float]]] = {}
        unparsed: list[str] = []
        for sym in eq_opts:
            parsed = _parse_option_symbol(sym)
            if parsed is None:
                unparsed.append(sym)
                continue
            root, expiry, is_call, strike = parsed
            chain_buckets.setdefault((root, expiry), []).append(
                (sym, is_call, strike)
            )

        if unparsed:
            print(f"[yahoo] couldn't parse {len(unparsed)} option symbol(s); "
                  f"skipping (likely futures options): {unparsed[:3]}",
                  flush=True)

        # ── Fetch each chain (uses cache) ────────────────────────────────
        for (root, expiry), legs in chain_buckets.items():
            try:
                chain = self._fetch_chain(root, expiry)
            except Exception as e:
                print(f"[yahoo] chain {root} {expiry}: {e}", flush=True)
                continue
            if not chain:
                continue

            spot = self._fetch_spot(root)
            now_unix = time.time()

            for sym, is_call, strike in legs:
                row = self._find_row(chain, is_call, strike)
                if row is None:
                    continue
                quote = self._row_to_normalized(
                    sym, row, spot=spot, is_call=is_call,
                    strike=strike, expiry=expiry, now_unix=now_unix,
                )
                if quote is not None:
                    out[sym] = quote

        # ── Equities (underlying tickers): quick spot lookup ─────────────
        for sym in eq:
            # Equities arrive un-padded (e.g. "AAPL"); strip whitespace
            # just in case.
            root = sym.strip()
            try:
                spot = self._fetch_spot(root)
            except Exception as e:
                print(f"[yahoo] spot {root}: {e}", flush=True)
                continue
            if spot is None:
                continue
            out[sym] = {
                "symbol":         sym,
                "mark":           float(spot),
                "last":           float(spot),
                "source":         "yahoo",
                "delayed_minutes": 15,
            }

        if out:
            print(f"[yahoo] returned {len(out)} symbol(s)", flush=True)
        return out

    # ── Internals ──────────────────────────────────────────────────────────

    def _authed_get(self, url: str, params: dict) -> Optional[requests.Response]:
        """
        GET with crumb included. On 401 (stale crumb / expired cookies),
        re-fetch the crumb once and retry. Returns the Response object, or
        None on a hard failure.
        """
        crumb = self._get_crumb()
        if crumb is None:
            return None
        attempt_params = dict(params, crumb=crumb)
        try:
            r = self._session.get(url, params=attempt_params, timeout=_TIMEOUT)
        except requests.RequestException as e:
            print(f"[yahoo] GET {url}: {e}", flush=True)
            return None
        if r.status_code == 401:
            # Crumb likely invalidated — try once more with a fresh one.
            crumb = self._get_crumb(force=True)
            if crumb is None:
                return r
            attempt_params["crumb"] = crumb
            try:
                r = self._session.get(url, params=attempt_params, timeout=_TIMEOUT)
            except requests.RequestException as e:
                print(f"[yahoo] retry GET {url}: {e}", flush=True)
                return None
        return r

    def _fetch_chain(self, root: str, expiry_yyyymmdd: str) -> Optional[dict]:
        """
        Fetch (or read from cache) the option chain for `root` expiring on
        `expiry_yyyymmdd`. Returns the raw Yahoo `optionChain.result[0]`
        dict, or None on any error.
        """
        cache_key = (root, expiry_yyyymmdd)
        cached = self._chain_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

        yyyy, mm, dd = int(expiry_yyyymmdd[:4]), \
                       int(expiry_yyyymmdd[5:7]), \
                       int(expiry_yyyymmdd[8:10])
        unix_ts = _expiry_to_unix_midnight_utc(yyyy, mm, dd)

        r = self._authed_get(
            _OPTIONS_URL.format(root=root),
            params={"date": unix_ts},
        )
        if r is None or r.status_code != 200:
            print(f"[yahoo] chain HTTP "
                  f"{r.status_code if r is not None else 'no response'} "
                  f"for {root} {expiry_yyyymmdd}", flush=True)
            return None

        try:
            data = r.json()
        except Exception:
            return None

        result = (data.get("optionChain") or {}).get("result") or []
        if not result:
            return None
        chain_payload = result[0]
        self._chain_cache[cache_key] = (time.time(), chain_payload)
        return chain_payload

    def _fetch_spot(self, root: str) -> Optional[float]:
        """Underlying spot price (last close from a 1-minute bar)."""
        cached = self._spot_cache.get(root)
        if cached and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

        r = self._authed_get(
            _CHART_URL.format(root=root),
            params={"interval": "1m", "range": "1d"},
        )
        if r is None or r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None

        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        # Try several fields in order of recency: regularMarketPrice (live),
        # then previousClose as a fallback.
        for key in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
            v = meta.get(key)
            if v is not None:
                try:
                    spot = float(v)
                except (TypeError, ValueError):
                    continue
                self._spot_cache[root] = (time.time(), spot)
                return spot
        return None

    @staticmethod
    def _find_row(chain: dict, is_call: bool, strike: float) -> Optional[dict]:
        """Find the call/put row matching `strike` within a Yahoo chain payload."""
        options_arr = chain.get("options") or []
        if not options_arr:
            return None
        bucket = options_arr[0]
        rows = bucket.get("calls" if is_call else "puts") or []
        # Match on strike with a small tolerance for float-print quirks.
        for row in rows:
            row_strike = row.get("strike")
            if row_strike is None:
                continue
            try:
                if abs(float(row_strike) - strike) < 1e-4:
                    return row
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _row_to_normalized(
        sym: str, row: dict, *,
        spot: Optional[float],
        is_call: bool,
        strike: float,
        expiry: str,
        now_unix: float,
    ) -> Optional[dict]:
        """Convert one Yahoo option row into the normalized quote dict."""
        def _f(k):
            v = row.get(k)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        bid  = _f("bid")
        ask  = _f("ask")
        last = _f("lastPrice")
        iv   = _f("impliedVolatility")

        # Mark = mid(bid, ask) if both look real, else last.
        mark = None
        if bid and ask and bid > 0 and ask > 0 and ask >= bid:
            mark = (bid + ask) / 2.0
        elif last and last > 0:
            mark = last

        if mark is None and last is None:
            # Genuinely nothing usable.
            return None

        # Compute Greeks if we have enough inputs. Days-to-expiry from
        # today's date in the same UTC frame we used to build the URL.
        import datetime as dt
        try:
            exp_date = dt.date(int(expiry[:4]), int(expiry[5:7]), int(expiry[8:10]))
            today    = dt.date.today()
            dte_days = max(0, (exp_date - today).days)
            dte_years = dte_days / 365.0
        except Exception:
            dte_years = 0.0

        greeks = bs_greeks.compute_greeks(
            spot=spot or 0.0,
            strike=strike,
            dte_years=dte_years,
            iv=iv or 0.0,
            is_call=is_call,
        )

        out = {
            "symbol":            sym,
            "mark":              mark,
            "bid":               bid,
            "ask":               ask,
            "last":              last,
            "implied-volatility": iv,
            "underlying-price":  spot,
            "source":            "yahoo",
            "delayed_minutes":   15,
        }
        for k in ("delta", "gamma", "theta", "vega"):
            v = greeks.get(k)
            if v is not None:
                out[k] = v
        return out

    # ── Streaming: not supported ──────────────────────────────────────────

    def start_stream(
        self,
        symbols:   Iterable[str],
        on_update: Callable[[dict], None],
        on_status: Callable[[str], None],
    ) -> Optional[StreamHandle]:
        # Yahoo has no push interface. The hybrid provider only calls us
        # for REST snapshots; signal "no stream" by returning None.
        return None

    def update_subscription(self, handle, symbols):
        return

    def stop_stream(self, handle):
        return
