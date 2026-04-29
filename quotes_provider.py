"""
Quotes-provider abstraction.

Decouples the rest of the app from the specific market-data vendor used
for live quotes / Greeks.  Callers always pass TastyTrade-format symbols
and receive a {tt_symbol: normalized_quote_dict} mapping back; the
provider implementation is responsible for any vendor-specific symbol
conversion or field renaming.

The normalized quote dict uses TastyTrade-style key names so that
`models.Position.attach_quote()` keeps working unchanged regardless of
the underlying provider:

    {
        "symbol":              "AAPL  240419P00150000",   # TT format
        "mark":                12.45,
        "bid":                 12.40,
        "ask":                 12.50,
        "last":                12.42,
        "delta":               -0.42,
        "gamma":                0.01,
        "theta":               -0.08,
        "vega":                 0.15,
        "implied-volatility":   0.27,
        "underlying-price":   178.32,
        "probability-otm":      0.62,   # 0..1, optional
    }

Missing keys are simply absent — Position.attach_quote tolerates that.

Streaming uses two callables instead of Qt signals so the abstraction
stays Qt-free.  The StreamHandle returned by start_stream is opaque;
treat it as a token to pass back into update_subscription / stop_stream.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Iterable


# Canonical normalized-quote keys.  Providers SHOULD populate as many as
# they can; consumers tolerate any subset.
NORMALIZED_KEYS = (
    "symbol",
    "mark", "bid", "ask", "last",
    "delta", "gamma", "theta", "vega",
    "implied-volatility",
    "underlying-price",
    "probability-otm",
)


class StreamHandle:
    """Opaque handle returned by start_stream.  Subclassed per provider."""
    pass


class QuotesProvider(ABC):
    """
    Abstract interface for live-quote providers.  Concrete implementations
    live in quotes_tasty.py, quotes_schwab.py, and the composite
    quotes_hybrid.py.
    """

    # ── REST snapshot ───────────────────────────────────────────────────────

    @abstractmethod
    def get_quotes(
        self,
        equity_options: Iterable[str] = (),
        future_options: Iterable[str] = (),
        equities:       Iterable[str] = (),
        futures:        Iterable[str] = (),
    ) -> dict[str, dict]:
        """
        Fetch a one-shot snapshot for the requested symbols.

        Parameters
        ----------
        equity_options, future_options, equities, futures
            Iterables of TastyTrade-format symbols.  Empty iterables are
            allowed (they just contribute nothing to the request).

        Returns
        -------
        dict
            ``{tt_symbol: normalized_quote_dict}`` for every symbol the
            provider managed to fetch.  An empty dict means total failure;
            partial dicts are allowed (caller can fall back for missing
            symbols).  This method MUST NOT raise — vendors fail silently
            so callers can degrade gracefully.
        """
        ...

    # ── Streaming ───────────────────────────────────────────────────────────

    @abstractmethod
    def start_stream(
        self,
        symbols:   Iterable[str],
        on_update: Callable[[dict], None],
        on_status: Callable[[str], None],
    ) -> StreamHandle:
        """
        Begin streaming quotes for the given symbols (TT-format).

        Parameters
        ----------
        symbols
            Initial subscription list.  Can be changed later via
            ``update_subscription``.
        on_update
            Called from a background thread whenever new quote data
            arrives.  Receives ``{tt_symbol: normalized_quote_dict}``
            with only the fields that changed (subset of NORMALIZED_KEYS).
        on_status
            Called when the connection state changes.  Argument is one of:
            ``"connecting"``, ``"connected"``, ``"disconnected"``, or
            ``"error:<message>"``.

        Returns
        -------
        StreamHandle
            Opaque token; pass back into ``update_subscription`` and
            ``stop_stream``.  May be ``None`` if streaming is not
            supported by this provider — caller should treat that as
            "REST polling only".
        """
        ...

    @abstractmethod
    def update_subscription(
        self,
        handle:  StreamHandle,
        symbols: Iterable[str],
    ) -> None:
        """Replace the active subscription list for an existing stream."""
        ...

    @abstractmethod
    def stop_stream(self, handle: StreamHandle) -> None:
        """Tear down the stream and release any resources."""
        ...
