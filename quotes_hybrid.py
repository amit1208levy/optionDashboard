"""
Hybrid quotes provider — IBKR Gateway primary, TastyTrade fallback,
optional Yahoo tertiary.

The dashboard always asks for quotes through this single object.  It
asks IBKR first (fast, real-time push); for any symbols IBKR doesn't
return data for (futures options, occasional outages, symbols that
fail to qualify, etc.) it transparently falls back to TastyTrade's
REST endpoint.  If a tertiary provider is configured (typically
Yahoo Finance), it is consulted for any symbols still missing after
both IBKR and TT — useful when the user has neither IBKR Gateway
running nor a valid TT token.

Callers see a unified ``{tt_symbol: quote}`` dict and never know which
side answered. Quote dicts from the tertiary include
``"source": "yahoo"`` so the UI can badge them as delayed.

Streaming wraps the primary and fallback (Yahoo has no push API). The
callback receives merged updates from both sources.  Status callbacks
report the IBKR side's connection state (the user-visible "● Live"
indicator follows IBKR, since that's the real-time source).

Circuit breaker
---------------
Three consecutive IBKR get_quotes() failures in a row bypass IBKR for
the next 5 minutes — avoids stacking 15 s timeouts onto every refresh
when Gateway is down. The tertiary has no breaker (it's already the
last resort).
"""
from __future__ import annotations

import time
from typing import Callable, Iterable, Optional

from quotes_provider import QuotesProvider, StreamHandle


class _HybridStreamHandle(StreamHandle):
    """Holds whichever underlying handles the active streamers gave us."""

    def __init__(self, ibkr_handle, tasty_handle):
        self.ibkr_handle  = ibkr_handle
        self.tasty_handle = tasty_handle


class HybridQuotesProvider(QuotesProvider):
    """
    Composes a primary (IBKR) and fallback (TastyTrade) provider.

    The two arguments are themselves QuotesProvider instances — this
    class doesn't care which concrete classes they are.
    """

    _BREAKER_FAILS  = 3      # consecutive failures before tripping
    _BREAKER_COOLDOWN = 300  # seconds to wait before retrying primary

    def __init__(
        self,
        primary: QuotesProvider,
        fallback: QuotesProvider,
        tertiary: Optional[QuotesProvider] = None,
    ):
        self._primary  = primary
        self._fallback = fallback
        # Tertiary is consulted only for symbols neither primary nor fallback
        # could fill. Typically Yahoo Finance; may be None to disable.
        self._tertiary = tertiary
        self._fail_count       = 0
        self._breaker_until    = 0.0
        # Streaming uses one handle from each provider when both support it.

    # ── Circuit-breaker helpers ────────────────────────────────────────────

    def _primary_open(self) -> bool:
        return time.time() >= self._breaker_until

    def _record_primary_success(self):
        self._fail_count = 0

    def _record_primary_fail(self):
        self._fail_count += 1
        if self._fail_count >= self._BREAKER_FAILS:
            self._breaker_until = time.time() + self._BREAKER_COOLDOWN
            self._fail_count = 0

    # ── REST snapshot ──────────────────────────────────────────────────────

    def get_quotes(
        self,
        equity_options: Iterable[str] = (),
        future_options: Iterable[str] = (),
        equities:       Iterable[str] = (),
        futures:        Iterable[str] = (),
    ) -> dict[str, dict]:
        eq_opts = list(equity_options); fu_opts = list(future_options)
        eq      = list(equities);       fu      = list(futures)
        wanted  = set(eq_opts) | set(fu_opts) | set(eq) | set(fu)
        if not wanted:
            return {}

        merged: dict[str, dict] = {}

        # ── Primary (IBKR) ───────────────────────────────────────────────
        if self._primary_open():
            try:
                primary_res = self._primary.get_quotes(
                    equity_options=eq_opts,
                    future_options=fu_opts,
                    equities=eq,
                    futures=fu,
                )
            except Exception as e:
                print(f"[hybrid] primary raised: {e}", flush=True)
                primary_res = {}

            if primary_res:
                merged.update(primary_res)
                self._record_primary_success()
                print(f"[hybrid] IBKR returned {len(primary_res)}/{len(wanted)} "
                      f"symbols", flush=True)
            else:
                # Empty result → counts toward the breaker (Gateway down or
                # qualify failed for everything).
                if wanted:
                    self._record_primary_fail()
                    print(f"[hybrid] IBKR returned 0/{len(wanted)} — "
                          f"fail #{self._fail_count}", flush=True)
        else:
            print(f"[hybrid] IBKR breaker open — skipping primary "
                  f"(resumes at {self._breaker_until - time.time():.0f}s)",
                  flush=True)

        # ── Fallback (TastyTrade) for missing symbols only ───────────────
        missing = wanted - set(merged.keys())
        if missing:
            # Re-bucket missing symbols by the original asset class so the
            # TT endpoint gets the right query parameters.
            fb_eq_opts = [s for s in eq_opts if s in missing]
            fb_fu_opts = [s for s in fu_opts if s in missing]
            fb_eq      = [s for s in eq      if s in missing]
            fb_fu      = [s for s in fu      if s in missing]
            try:
                fb_res = self._fallback.get_quotes(
                    equity_options=fb_eq_opts,
                    future_options=fb_fu_opts,
                    equities=fb_eq,
                    futures=fb_fu,
                )
            except Exception as e:
                print(f"[hybrid] fallback raised: {e}", flush=True)
                fb_res = {}
            for k, v in (fb_res or {}).items():
                merged.setdefault(k, v)

        # ── Tertiary (Yahoo, etc.) for whatever's still missing ──────────
        # No circuit breaker — the tertiary is already the last resort.
        # It only ever runs when both IBKR and TT have failed to cover a
        # symbol, so the cost is naturally bounded.
        if self._tertiary is not None:
            still_missing = wanted - set(merged.keys())
            if still_missing:
                t_eq_opts = [s for s in eq_opts if s in still_missing]
                t_fu_opts = [s for s in fu_opts if s in still_missing]
                t_eq      = [s for s in eq      if s in still_missing]
                t_fu      = [s for s in fu      if s in still_missing]
                try:
                    t_res = self._tertiary.get_quotes(
                        equity_options=t_eq_opts,
                        future_options=t_fu_opts,
                        equities=t_eq,
                        futures=t_fu,
                    )
                except Exception as e:
                    print(f"[hybrid] tertiary raised: {e}", flush=True)
                    t_res = {}
                for k, v in (t_res or {}).items():
                    merged.setdefault(k, v)
                if t_res:
                    print(f"[hybrid] tertiary returned {len(t_res)}/"
                          f"{len(still_missing)} symbols", flush=True)

        return merged

    # ── Streaming ──────────────────────────────────────────────────────────

    def start_stream(
        self,
        symbols:   Iterable[str],
        on_update: Callable[[dict], None],
        on_status: Callable[[str], None],
    ) -> Optional[_HybridStreamHandle]:
        symbols = list(symbols)

        # Primary streamer is the visible/real-time one — its status drives
        # the UI's "● Live" indicator.  Fallback runs silently underneath
        # for any symbols the primary doesn't cover.
        ibkr_h: Optional[StreamHandle] = None
        if self._primary_open():
            try:
                ibkr_h = self._primary.start_stream(symbols, on_update, on_status)
            except Exception as e:
                print(f"[hybrid] primary start_stream: {e}", flush=True)
                ibkr_h = None
                self._record_primary_fail()

        # If primary streaming failed, surface a status so the UI can show
        # REST-fallback mode.  Otherwise stay quiet — primary's status will
        # propagate.
        tasty_h: Optional[StreamHandle] = None
        if ibkr_h is None:
            try:
                tasty_h = self._fallback.start_stream(symbols, on_update, on_status)
            except Exception as e:
                print(f"[hybrid] fallback start_stream: {e}", flush=True)
                tasty_h = None

        return _HybridStreamHandle(ibkr_h, tasty_h)

    def update_subscription(
        self,
        handle:  _HybridStreamHandle,
        symbols: Iterable[str],
    ) -> None:
        if handle is None:
            return
        symbols = list(symbols)
        if handle.ibkr_handle is not None:
            try:
                self._primary.update_subscription(handle.ibkr_handle, symbols)
            except Exception:
                pass
        if handle.tasty_handle is not None:
            try:
                self._fallback.update_subscription(handle.tasty_handle, symbols)
            except Exception:
                pass

    def stop_stream(self, handle: _HybridStreamHandle) -> None:
        if handle is None:
            return
        if handle.ibkr_handle is not None:
            try:
                self._primary.stop_stream(handle.ibkr_handle)
            except Exception:
                pass
        if handle.tasty_handle is not None:
            try:
                self._fallback.stop_stream(handle.tasty_handle)
            except Exception:
                pass
