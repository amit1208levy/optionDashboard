"""
Hybrid quotes provider — IBKR Gateway primary, TastyTrade fallback.

The dashboard always asks for quotes through this single object.  It
asks IBKR first (fast, real-time push); for any symbols IBKR doesn't
return data for (futures options, occasional outages, symbols that
fail to qualify, etc.) it transparently falls back to TastyTrade's
REST endpoint.  Callers see a unified ``{tt_symbol: quote}`` dict and
never know which side answered.

Streaming wraps both providers — IBKR is the primary streamer; the
callback receives merged updates from both sources.  Status callbacks
report the IBKR side's connection state (the user-visible "● Live"
indicator follows IBKR, since that's the real-time source).

Circuit breaker
---------------
Three consecutive IBKR get_quotes() failures in a row bypass IBKR for
the next 5 minutes — avoids stacking 15 s timeouts onto every refresh
when Gateway is down.
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

    def __init__(self, primary: QuotesProvider, fallback: QuotesProvider):
        self._primary  = primary
        self._fallback = fallback
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
            else:
                # Empty result → counts toward the breaker (Gateway down or
                # qualify failed for everything).
                if wanted:
                    self._record_primary_fail()

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
