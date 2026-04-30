"""
TastyTrade ↔ Interactive Brokers symbol conversion.

The dashboard's internal "wire format" for symbols is the TastyTrade
representation:

    Equities          → "AAPL"
    Equity options    → "AAPL  240419P00150000"   (OCC, 6-char padded root)
    Futures           → "/ESM4"                    (root + month + 1-digit year)
    Future options    → "./ESM4 EW3M4 240920P5500" (multi-token, weeklies)

IBKR uses richer ``Contract`` objects (``Stock``, ``Option``, ``Future``,
``FuturesOption``) plus an exchange code per asset class.  This module
translates between the two — callers only ever pass TT-format symbols
through the QuotesProvider interface, and we keep a per-call reverse
map so the response dict can be re-keyed by TT symbol.

Supported in this revision
--------------------------
* Equities (every ticker → SMART exchange)
* Equity options (OCC → ``Option`` on SMART)
* Futures (basic root+month+1-digit-year → ``Future`` on the exchange
  appropriate for the root)

Not yet supported (raise ``UnsupportedSymbol`` so the caller can fall
back to TastyTrade for those specific symbols):

* Futures options — TT's multi-token format with weekly inserts is
  highly variable and needs real symbol pairs from a live account to
  codify.  Defer until we have data.
* Indexes / non-USD equities.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from ib_insync import Stock, Option, Future, Contract


class UnsupportedSymbol(ValueError):
    """Raised when we can't convert a TT symbol into an IBKR Contract."""


# ── Equity options: OCC format, 6-char left-padded root ──────────────────────
# AAPL  240419P00150000  →  groups = ('AAPL  ', '240419', 'P', '00150000')
_OPT_RE = re.compile(r"^(.{6})(\d{6})([CP])(\d{8})$")

# ── Futures: /ROOT + month code (1 letter) + 1-digit year ────────────────────
# /ESM4  →  groups = ('ES', 'M', '4')
_FUT_RE = re.compile(r"^/([A-Z0-9]{1,4})([FGHJKMNQUVXZ])(\d)$")

# Futures month codes (CME convention)
_MONTH_CODE = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K":  5, "M":  6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

# Map common future roots to the IBKR exchange the contract trades on.
# Anything not listed falls back to GLOBEX (CME's electronic platform), which
# IBKR routes correctly for most CME-listed products.
_FUT_EXCHANGE = {
    # CME equity-index futures
    "ES":  "CME",  "MES": "CME",  "NQ":  "CME",  "MNQ": "CME",
    "RTY": "CME",  "M2K": "CME",  "YM":  "CBOT", "MYM": "CBOT",
    # CBOT interest-rate futures
    "ZB":  "CBOT", "ZN":  "CBOT", "ZF":  "CBOT", "ZT":  "CBOT",
    "UB":  "CBOT", "TN":  "CBOT",
    # CBOT grains
    "ZC":  "CBOT", "ZS":  "CBOT", "ZW":  "CBOT", "ZL":  "CBOT",
    # NYMEX energy
    "CL":  "NYMEX", "MCL": "NYMEX", "NG":  "NYMEX", "QG":  "NYMEX",
    "RB":  "NYMEX", "HO":  "NYMEX",
    # COMEX metals
    "GC":  "COMEX", "MGC": "COMEX", "SI":  "COMEX", "SIL": "COMEX",
    "HG":  "COMEX", "MHG": "COMEX",
    # CME currencies
    "6E":  "CME",  "6B":  "CME",  "6J":  "CME",  "6A":  "CME",
    "6C":  "CME",  "6S":  "CME",  "6N":  "CME",
    # Bitcoin / crypto
    "BTC": "CME",  "MBT": "CME",  "ETH": "CME",  "MET": "CME",
}


# ── Public API ───────────────────────────────────────────────────────────────

def tt_to_contract(tt_symbol: str) -> Contract:
    """
    Convert one TastyTrade symbol to an ``ib_insync.Contract``.

    Returns an unqualified Contract — the caller (provider) is responsible
    for calling ``ib.qualifyContracts()`` to resolve conIds.

    Raises ``UnsupportedSymbol`` for futures options and any string that
    doesn't match a known shape, so the caller can fall back gracefully.
    """
    if not tt_symbol:
        raise UnsupportedSymbol("empty symbol")

    s = tt_symbol.strip()

    # Futures option — multi-token, defer
    if s.startswith("./"):
        raise UnsupportedSymbol(f"futures option (deferred): {s}")

    # Future root (single token starting with /)
    if s.startswith("/"):
        m = _FUT_RE.match(s)
        if not m:
            raise UnsupportedSymbol(f"unrecognized future symbol: {s}")
        root, month_code, year_digit = m.groups()
        return _build_future(root, month_code, year_digit)

    # Equity option — OCC format (exactly 21 characters when properly padded)
    m = _OPT_RE.match(s)
    if m:
        return _build_option(*m.groups())

    # Otherwise treat as a plain US equity / ETF
    return Stock(s, "SMART", "USD")


def is_supported(tt_symbol: str) -> bool:
    """True if tt_to_contract would succeed.  Doesn't actually build."""
    try:
        tt_to_contract(tt_symbol)
        return True
    except UnsupportedSymbol:
        return False


def contract_to_tt(c: Contract) -> Optional[str]:
    """
    Best-effort reverse conversion (IBKR Contract → TT-format string).
    Used to re-key streaming-tick payloads back to the caller's symbol
    space.  Returns None if we can't reconstruct the TT form.
    """
    if isinstance(c, Stock):
        return c.symbol
    if isinstance(c, Option):
        return _option_to_tt(c)
    if isinstance(c, Future):
        return _future_to_tt(c)
    return None


# ── Internals ────────────────────────────────────────────────────────────────

def _build_option(root_padded: str, ymd: str, cp: str, strike_8d: str) -> Option:
    root = root_padded.rstrip()                # "AAPL  " → "AAPL"
    expiry = "20" + ymd                         # "240419" → "20240419"
    strike = int(strike_8d) / 1000.0            # "00150000" → 150.0
    # Strikes that come out as whole numbers should be ints in the
    # repr — IBKR accepts both, but clean repr helps debugging.
    if abs(strike - round(strike)) < 1e-9:
        strike = float(round(strike))
    return Option(root, expiry, strike, cp, "SMART", currency="USD")


def _option_to_tt(c: Option) -> Optional[str]:
    """Reverse: Option('AAPL', '20240419', 150, 'P', ...) → 'AAPL  240419P00150000'."""
    if not c.symbol or not c.lastTradeDateOrContractMonth or not c.right or c.strike is None:
        return None
    root = c.symbol.upper().ljust(6)
    ymd = c.lastTradeDateOrContractMonth[2:8]   # 20240419 → 240419
    strike_int = int(round(float(c.strike) * 1000))
    return f"{root}{ymd}{c.right}{strike_int:08d}"


def _build_future(root: str, month_code: str, year_digit: str) -> Future:
    month = _MONTH_CODE[month_code]
    year  = _resolve_future_year(int(year_digit))
    expiry = f"{year}{month:02d}"               # YYYYMM, e.g. "202406"
    exch = _FUT_EXCHANGE.get(root, "GLOBEX")
    return Future(root, expiry, exch, currency="USD")


def _future_to_tt(c: Future) -> Optional[str]:
    if not c.symbol or not c.lastTradeDateOrContractMonth:
        return None
    s = c.lastTradeDateOrContractMonth
    if len(s) < 6:
        return None
    year  = int(s[0:4])
    month = int(s[4:6])
    inv = {v: k for k, v in _MONTH_CODE.items()}
    if month not in inv:
        return None
    return f"/{c.symbol.upper()}{inv[month]}{year % 10}"


def _resolve_future_year(last_digit: int) -> int:
    """
    Map a TastyTrade single-digit year back to a four-digit year.

    Heuristic: pick the year ending in ``last_digit`` that is closest to
    today.  Defaults to "this decade" but leaks one year into the
    previous decade so a contract that just expired still resolves
    correctly.
    """
    cur = date.today().year
    decade = (cur // 10) * 10
    candidates = [decade - 10 + last_digit, decade + last_digit, decade + 10 + last_digit]
    return min(candidates, key=lambda y: abs(y - cur))
