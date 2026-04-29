"""
TastyTrade quotes adapter — implements QuotesProvider over the existing
api.get_market_data REST call and the streamer.QuoteStreamer DXLink
WebSocket class.

This is a pure adapter: no field renames, no symbol conversion, no logic
changes.  TastyTrade's own quote dicts already use the canonical
NORMALIZED_KEYS, so we pass them through unchanged.

The streamer is wrapped in a handle that the caller treats as opaque.
The handle internally owns the QThread and forwards Qt signals to the
plain Python callbacks the QuotesProvider interface defines.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

import api
import streamer as _streamer_mod
from quotes_provider import QuotesProvider, StreamHandle


class _TastyStreamHandle(StreamHandle):
    """Owns a single QuoteStreamer instance plus its signal connections."""

    def __init__(self, qt_streamer):
        self.streamer = qt_streamer


class TastyQuotesProvider(QuotesProvider):
    """
    Quotes provider backed by TastyTrade's own market-data endpoints.

    Parameters
    ----------
    token_getter
        Zero-arg callable returning the current TastyTrade access token.
        Using a getter (rather than a stored token) means token rotations
        from the auth layer are picked up automatically.
    """

    def __init__(self, token_getter: Callable[[], str]):
        self._get_token = token_getter

    # ── REST snapshot ───────────────────────────────────────────────────────

    def get_quotes(
        self,
        equity_options: Iterable[str] = (),
        future_options: Iterable[str] = (),
        equities:       Iterable[str] = (),
        futures:        Iterable[str] = (),
    ) -> dict[str, dict]:
        token = self._get_token() or ""
        if not token:
            return {}
        # api.get_market_data already returns {symbol: tt-keyed quote dict}
        # and silently returns {} on error, so we can pass through as-is.
        return api.get_market_data(
            token,
            equity_options=list(equity_options) or None,
            future_options=list(future_options) or None,
            equities=list(equities) or None,
            futures=list(futures) or None,
        ) or {}

    # ── Streaming ───────────────────────────────────────────────────────────

    def start_stream(
        self,
        symbols:   Iterable[str],
        on_update: Callable[[dict], None],
        on_status: Callable[[str], None],
    ) -> Optional[_TastyStreamHandle]:
        token = self._get_token() or ""
        if not token:
            on_status("error:no token")
            return None

        s = _streamer_mod.QuoteStreamer(token)
        # Qt's auto-connection type makes these queued when the receiver is
        # a QObject method bound to the GUI thread, and direct otherwise —
        # both cases work for our consumers.
        s.price_update.connect(on_update)
        s.status_changed.connect(on_status)
        s.start()
        if symbols:
            s.set_symbols(list(symbols))
        return _TastyStreamHandle(s)

    def update_subscription(
        self,
        handle:  _TastyStreamHandle,
        symbols: Iterable[str],
    ) -> None:
        if handle is None or handle.streamer is None:
            return
        handle.streamer.set_symbols(list(symbols))

    def stop_stream(self, handle: _TastyStreamHandle) -> None:
        if handle is None or handle.streamer is None:
            return
        s = handle.streamer
        try:
            s.status_changed.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            s.price_update.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            s.stop_streaming()
        except Exception:
            pass
        if s.isRunning():
            s.wait(3000)
        handle.streamer = None
