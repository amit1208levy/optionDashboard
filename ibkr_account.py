"""
ibkr_account.py — build a dashboard-compatible account dict from IBKR data.

ib_insync exposes the logged-in account's positions via ``ib.portfolio()``
and its balances via ``ib.accountSummary()``.  This module maps those to the
same dict shape that TastyTrade's PortfolioWorker produces, so the rest of the
app (``_render``, strategy grouping, Greeks display, …) works identically for
an IBKR account and a TastyTrade account.

Account sentinel number
-----------------------
IBKR accounts get the ``"__ibkr__"`` pseudo-number.  All dict keys that
expect account numbers will find it there and treat it like any other account.
The display name is ``"IBKR Gateway"`` by default (overridable in Settings).

Position mapping
----------------
Each ib_insync ``PortfolioItem`` is converted to a fake TastyTrade raw dict
that ``models.Position.__init__`` accepts.  Fields not provided by IBKR (e.g.
``close-price``) are left as ``None`` and are handled gracefully by Position.

averageCost semantics
---------------------
IBKR reports ``averageCost`` as the total cost per *contract* (includes
multiplier for options/futures).  TastyTrade's ``average-open-price`` is the
price per underlying share/point.  We divide by the multiplier to normalise.

For equities the multiplier is 1, so the division is a no-op.
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from ib_insync import Stock, Option, Future, FuturesOption
import ibkr_symbols
import models

if TYPE_CHECKING:
    from quotes_ibkr import IBKRQuotesProvider

# ── Sentinel used as the "account number" for the IBKR entry ─────────────────
IBKR_ACCOUNT_NUMBER = "__ibkr__"

# ── IBKR accountSummary tag → TastyTrade balance key ─────────────────────────
_SUMMARY_MAP = {
    "NetLiquidation":          "net-liquidating-value",
    "TotalCashValue":          "cash-balance",
    "MaintMarginReq":          "maintenance-requirement",
    "AvailableFunds":          "equity-buying-power",
    "OptionBuyingPower":       "derivative-buying-power",
    "UnrealizedPnL":           "unrealized-pnl",
    "RealizedPnL":             "realized-pnl",
    "GrossPositionValue":      "long-market-value",
}


def _safe_float(v) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x else None       # NaN guard
    except (TypeError, ValueError):
        return None


def _instrument_type(contract) -> str:
    """Map ib_insync contract type → TastyTrade instrument-type string."""
    if isinstance(contract, Option):
        return "Equity Option"
    if isinstance(contract, Future):
        return "Future"
    if isinstance(contract, FuturesOption):
        return "Future Option"
    return "Equity"                         # Stock or anything else


def _underlying_symbol(contract) -> str:
    """Best-effort TastyTrade-style underlying symbol."""
    if isinstance(contract, (Future, FuturesOption)):
        return f"/{contract.symbol}"
    return contract.symbol or ""


def _portfolio_item_to_raw(item) -> Optional[dict]:
    """
    Convert one ib_insync PortfolioItem → TT-style raw dict for Position().

    Returns None if we can't determine the TT symbol (unsupported contract).
    """
    c = item.contract
    if c is None:
        return None

    # TT-format symbol.
    try:
        tt_sym = ibkr_symbols.contract_to_tt(c)
    except Exception:
        tt_sym = None

    # Fall back to a simple representation so the position still shows.
    if not tt_sym:
        tt_sym = getattr(c, "localSymbol", "") or getattr(c, "symbol", "")
    if not tt_sym:
        return None

    qty        = _safe_float(item.position)
    mkt_price  = _safe_float(item.marketPrice)
    avg_cost   = _safe_float(item.averageCost)   # cost per contract (incl. mult)

    # Multiplier: IBKR stores it as a string; default 1 for equities.
    try:
        multiplier = float(c.multiplier) if c.multiplier else 1.0
    except (TypeError, ValueError):
        multiplier = 1.0
    if multiplier <= 0:
        multiplier = 1.0

    # Normalise averageCost → price-per-share/point (TT convention).
    avg_open = (avg_cost / multiplier) if avg_cost is not None else None

    instrument_type = _instrument_type(c)
    underlying      = _underlying_symbol(c)
    direction       = "Long" if (qty or 0) >= 0 else "Short"

    return {
        "symbol":                  tt_sym,
        "underlying-symbol":       underlying,
        "instrument-type":         instrument_type,
        "quantity":                abs(qty or 0),
        "quantity-direction":      direction,
        "mark-price":              mkt_price,
        "close-price":             None,
        "multiplier":              multiplier,
        "average-open-price":      avg_open,
        # IBKR's authoritative unrealized P&L for the position. Position
        # uses this directly instead of recomputing from open/mark prices,
        # which avoids futures-contract edge cases where averageCost
        # semantics drift between TWS / Gateway versions.
        "unrealized-pnl":          _safe_float(item.unrealizedPNL),
        "unrealized-day-gain":     _safe_float(item.unrealizedPNL),
    }


def _build_balances(summary: dict) -> dict:
    """Map IBKR accountSummary dict → TT-style balances dict."""
    out: dict = {}
    for ibkr_key, tt_key in _SUMMARY_MAP.items():
        v = _safe_float(summary.get(ibkr_key))
        if v is not None:
            out[tt_key] = v
    return out


def fetch_ibkr_account(ibkr_provider: "IBKRQuotesProvider") -> Optional[dict]:
    """
    Pull portfolio + account summary from the connected IBKR Gateway and
    return a dict in the same shape as PortfolioWorker._fetch_one().

    Returns None if Gateway is not connected or returns no data.
    """
    if not ibkr_provider.is_connected():
        return None

    portfolio_items = ibkr_provider.get_portfolio()
    summary         = ibkr_provider.get_account_summary()

    print(f"[ibkr_account] portfolio_items={len(portfolio_items)} "
          f"summary_tags={len(summary)}", flush=True)

    if not portfolio_items and not summary:
        return None

    # Build Position objects from portfolio items.
    positions: list[models.Position] = []
    skipped = 0
    for item in portfolio_items:
        raw = _portfolio_item_to_raw(item)
        if raw is None:
            skipped += 1
            continue
        try:
            positions.append(models.Position(raw))
        except Exception as e:
            skipped += 1
            print(f"[ibkr_account] skipping position {getattr(item, 'contract', '?')}: {e}",
                  flush=True)
    if skipped:
        print(f"[ibkr_account] {skipped} portfolio items couldn't be converted",
              flush=True)
    print(f"[ibkr_account] built {len(positions)} positions", flush=True)

    if not positions and not summary:
        return None

    balances = _build_balances(summary)

    return {
        "number":             IBKR_ACCOUNT_NUMBER,
        "nickname":           "IBKR Gateway",
        "source":             "ibkr",          # flag for conditional rendering
        "balances":           balances,
        "positions":          positions,
        "metrics":            {},              # IBKR Greeks come from Ticker, not metrics
        "ytd_txns":           [],
        "year_start_net_liq": None,
        "ytd_pnl_sdk":        None,
    }
