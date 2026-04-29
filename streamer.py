"""
DXLink real-time quote streamer for TastyTrade.

Protocol flow
─────────────
  1. GET /api-quote-tokens  → streamer token + WebSocket URL
  2. WS connect
  3. Server sends  SETUP  →  client replies SETUP
  4. Client sends  CHANNEL_REQUEST (AUTH) + AUTH
  5. Server sends  AUTH_STATE: AUTHORIZED
  6. Client sends  CHANNEL_REQUEST (FEED)
  7. Server sends  CHANNEL_OPENED (feed)
  8. Client sends  FEED_SETUP  (compact format, requested fields)
  9. Client sends  FEED_SUBSCRIPTION  (symbols)
 10. Server sends  FEED_CONFIG  (actual schema) + FEED_DATA events
 11. Server sends  KEEPALIVE  every ~60 s  → client echoes back

Thread model
────────────
QuoteStreamer is a QThread that runs its own asyncio event loop.
All GUI interaction happens exclusively via Qt signals (price_update,
status_changed) which Qt automatically marshals to the main thread.
"""
import asyncio
import json
import re
import threading

import requests
from PyQt6.QtCore import QThread, pyqtSignal

BASE = "https://api.tastyworks.com"
UA   = "options-dashboard/1.0"

_CHANNEL_AUTH = 1
_CHANNEL_FEED = 3

# Fields we ask the server to include in COMPACT data rows.
_QUOTE_FIELDS  = ["eventSymbol", "bidPrice",  "askPrice"]
_GREEKS_FIELDS = ["eventSymbol", "delta",     "gamma",
                  "theta",       "rho",        "vega",
                  "volatility",  "price"]

# Detect option symbols: contains a 6-digit date + C/P + digits
_OPT_RE = re.compile(r"\d{6}[CP]\d+")


# ── helpers ──────────────────────────────────────────────────────────────────

def _f(v):
    """Safe float; returns None for NaN/non-numeric."""
    try:
        x = float(v)
        return x if x == x else None   # NaN guard
    except (TypeError, ValueError):
        return None


def get_streamer_token(access_token: str):
    """
    Fetch DXLink token + WS URL from /api-quote-tokens.
    Returns (token_str, url_str) on success, (None, err_msg) on failure.
    """
    try:
        r = requests.get(
            f"{BASE}/api-quote-tokens",
            headers={
                "Authorization":  f"Bearer {access_token}",
                "Content-Type":   "application/json",
                "User-Agent":     UA,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        data  = r.json().get("data", {})
        token = data.get("token") or ""
        url   = (data.get("dxlink-url")
                 or "wss://tasty-openapi-ws.dxfeed.com/realtime")
        if not token:
            return None, "Empty streamer token from API"
        return token, url
    except Exception as e:
        return None, str(e)


# ── streamer ─────────────────────────────────────────────────────────────────

class QuoteStreamer(QThread):
    """
    Real-time quote + Greeks streamer via TastyTrade DXLink.

    Signals
    -------
    price_update(dict)
        Fired on each FEED_DATA batch.
        Shape: {symbol: {"mark": float, "bid": float, "ask": float,
                          "delta": float, "gamma": float,
                          "theta": float, "vega": float}}
        Only keys that were present in the data are included.

    status_changed(str)
        "connecting" | "connected" | "error:<msg>" | "disconnected"
    """

    price_update   = pyqtSignal(dict)
    status_changed = pyqtSignal(str)

    def __init__(self, access_token: str, parent=None):
        super().__init__(parent)
        self._access_token = access_token
        self._symbols: set = set()
        self._lock         = threading.Lock()
        self._loop         = None   # asyncio loop (set inside run())
        self._stop_evt     = None   # asyncio.Event created inside the loop
        self._sub_pending  = False  # True when symbols changed and need resub
        self._schema: dict = {}     # {event_type: [field_names]}

    # ── public API (GUI-thread safe) ─────────────────────────────────────────

    def set_symbols(self, symbols):
        """Replace the subscription list.  Safe to call from any thread."""
        with self._lock:
            self._symbols = set(s for s in symbols if s)
        self._sub_pending = True
        # Wake the asyncio loop promptly (no-op if loop not yet started)
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(lambda: None)

    def stop_streaming(self):
        """Gracefully stop the streamer.  Safe to call from the GUI thread."""
        if self._loop and not self._loop.is_closed() and self._stop_evt:
            self._loop.call_soon_threadsafe(self._stop_evt.set)
        self.quit()

    # ── QThread entry point ──────────────────────────────────────────────────

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_loop())
        except Exception:
            pass
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    # ── async core ───────────────────────────────────────────────────────────

    async def _run_loop(self):
        """Outer retry loop — reconnects indefinitely until stopped."""
        self._stop_evt = asyncio.Event()

        while not self._stop_evt.is_set():
            try:
                await self._connect_once()
            except Exception as e:
                self.status_changed.emit(f"error:{e}")

            if self._stop_evt.is_set():
                break

            # 3 s backoff; bail early if stop is requested
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass

        self.status_changed.emit("disconnected")

    async def _connect_once(self):
        import websockets

        self.status_changed.emit("connecting")
        self._schema = {}

        # Fetch token via blocking HTTP (run in thread pool so we don't block the loop)
        token, url = await self._loop.run_in_executor(
            None, get_streamer_token, self._access_token
        )
        if not token:
            self.status_changed.emit(f"error:{url}")
            await asyncio.sleep(5)
            return

        try:
            async with websockets.connect(
                url,
                ping_interval=None,   # DXLink uses its own KEEPALIVE
                open_timeout=15,
            ) as ws:
                await self._handshake(ws, token)
                await self._message_loop(ws)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise RuntimeError(str(e)) from e

    async def _handshake(self, ws, token: str):
        """
        Perform the DXLink handshake up to (and including) the initial
        FEED_SUBSCRIPTION.  Raises RuntimeError on failure.
        """

        # ── server sends SETUP first; drain briefly to find it ───────────────
        for _ in range(8):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("type") == "SETUP":
                break
            if m.get("type") == "KEEPALIVE":
                await ws.send(raw)

        # Client SETUP reply
        await ws.send(json.dumps({
            "type": "SETUP", "channel": 0,
            "version": "0.1-js/1.0.0", "minVersion": "0.1",
            "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60,
        }))

        # Open AUTH channel + authenticate in one burst
        await ws.send(json.dumps({
            "type": "CHANNEL_REQUEST", "channel": _CHANNEL_AUTH,
            "service": "AUTH", "parameters": {},
        }))
        await ws.send(json.dumps({
            "type": "AUTH", "channel": _CHANNEL_AUTH, "token": token,
        }))

        # Wait for AUTHORIZED
        authorized = False
        for _ in range(30):
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            m   = json.loads(raw)
            if m.get("type") == "KEEPALIVE":
                await ws.send(raw)
                continue
            if (m.get("type") == "AUTH_STATE"
                    and m.get("state") == "AUTHORIZED"):
                authorized = True
                break
        if not authorized:
            raise RuntimeError("DXLink auth failed (no AUTHORIZED received)")

        # Open FEED channel
        await ws.send(json.dumps({
            "type": "CHANNEL_REQUEST", "channel": _CHANNEL_FEED,
            "service": "FEED", "parameters": {"contract": "AUTO"},
        }))

        # Wait for CHANNEL_OPENED on the feed channel
        feed_open = False
        for _ in range(30):
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            m   = json.loads(raw)
            if m.get("type") == "KEEPALIVE":
                await ws.send(raw)
                continue
            if (m.get("type") == "CHANNEL_OPENED"
                    and m.get("channel") == _CHANNEL_FEED):
                feed_open = True
                break
        if not feed_open:
            raise RuntimeError("Feed channel failed to open")

        # Configure feed (compact format + requested fields)
        await ws.send(json.dumps({
            "type": "FEED_SETUP", "channel": _CHANNEL_FEED,
            "acceptAggregationPeriod": 0.1,
            "acceptDataFormat":       "COMPACT",
            "acceptEventFields": {
                "Quote":  _QUOTE_FIELDS,
                "Greeks": _GREEKS_FIELDS,
            },
        }))

        # Initial subscription
        await self._send_sub(ws)
        self._sub_pending = False
        self.status_changed.emit("connected")

    async def _message_loop(self, ws):
        """Read messages until stop is requested."""
        while not self._stop_evt.is_set():
            # Re-subscribe if set_symbols() was called since last sub
            if self._sub_pending:
                self._sub_pending = False
                await self._send_sub(ws)

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue   # loop back to check stop_evt / sub_pending

            try:
                msg = json.loads(raw)
            except Exception:
                continue

            t  = msg.get("type", "")
            ch = msg.get("channel", 0)

            if t == "KEEPALIVE":
                await ws.send(raw)          # echo back on same channel

            elif t == "FEED_CONFIG" and ch == _CHANNEL_FEED:
                for ev, fields in (msg.get("eventFields") or {}).items():
                    self._schema[ev] = fields

            elif t == "FEED_DATA" and ch == _CHANNEL_FEED:
                self._parse_feed_data(msg.get("data", []))

            elif t == "AUTH_STATE":
                if msg.get("state") != "AUTHORIZED":
                    raise RuntimeError(f"Auth revoked: {msg.get('state')}")

    async def _send_sub(self, ws):
        """Send a FEED_SUBSCRIPTION for the current symbol set."""
        with self._lock:
            syms = list(self._symbols)
        if not syms:
            return

        options  = [s for s in syms if _OPT_RE.search(s)]
        equities = [s for s in syms if not _OPT_RE.search(s)]

        add: dict = {}
        if equities:
            add["Quote"] = equities
        if options:
            add["Quote"] = add.get("Quote", []) + options
            add["Greeks"] = options

        if add:
            await ws.send(json.dumps({
                "type": "FEED_SUBSCRIPTION",
                "channel": _CHANNEL_FEED,
                "add": add,
            }))

    # ── quote parsing ─────────────────────────────────────────────────────────

    def _parse_feed_data(self, data: list):
        """
        COMPACT format alternates schema headers and data rows:
          ["Quote",  ["eventSymbol","bidPrice","askPrice"]]  ← schema
          ["Quote",  "SPY", 450.5, 450.51]                  ← row
          ["Greeks", ["eventSymbol","delta","gamma",…]]      ← schema
          ["Greeks", "SPY …option…", 0.35, 0.02, …]         ← row
        """
        out: dict = {}

        for item in data:
            if not isinstance(item, list) or len(item) < 2:
                continue
            event_type = item[0]
            if not isinstance(event_type, str):
                continue
            second = item[1]

            if isinstance(second, list):
                # Schema header: [event_type, [field_names...]]
                self._schema[event_type] = second
                continue

            # Data row: [event_type, sym, v1, v2, ...]
            fields = self._schema.get(event_type)
            if not fields:
                continue
            event = dict(zip(fields, item[1:]))
            sym   = event.get("eventSymbol", "")
            if not sym:
                continue

            rec = out.setdefault(sym, {})

            if event_type == "Quote":
                bid = _f(event.get("bidPrice"))
                ask = _f(event.get("askPrice"))
                if bid is not None and ask is not None and bid >= 0 and ask >= 0:
                    rec["bid"]  = bid
                    rec["ask"]  = ask
                    rec["mark"] = (bid + ask) / 2.0

            elif event_type == "Greeks":
                for k in ("delta", "gamma", "theta", "vega", "volatility"):
                    v = _f(event.get(k))
                    if v is not None:
                        rec[k] = v
                # "price" in Greeks = fair-value mark for options
                price = _f(event.get("price"))
                if price is not None and price > 0:
                    rec["mark"] = price

        if out:
            self.price_update.emit(out)
