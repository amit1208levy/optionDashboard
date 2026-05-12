"""
parse_trade_email.py — extract trade fills from TastyTrade confirmation emails.

Parses the plaintext (or stripped-HTML) body of a TastyTrade trade confirmation
email into structured fill objects.  Two fill-line formats are supported:

  Equity option:  Bought 2 NVDA 05/15/26 Call 225.00 @ 2.91
  Futures option: Sold 1 /CLM6 ML2K6 05/11/26 Call 101.00 @ 0.31

Each fill is returned as a dict with all the fields needed to reconstruct a
TastyTrade-format symbol and update the position ledger.

Pure equity and pure futures trades (no Call/Put) are handled as well:
  Equity:  Bought 100 GOOG @ 172.50
  Future:  Bought 1 /MESU6 @ 5678.25
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── Regex patterns ──────────────────────────────────────────────────────────

# Equity option: Bought 2 NVDA 05/15/26 Call 225.00 @ 2.91
_RE_EQ_OPT = re.compile(
    r"(Bought|Sold)\s+(\d+)\s+"
    r"([A-Z][A-Z0-9]*)\s+"             # root (e.g. NVDA, SPY, GOOG)
    r"(\d{2}/\d{2}/\d{2})\s+"          # expiry MM/DD/YY
    r"(Call|Put)\s+"
    r"([\d.]+)\s+@\s+([\d.]+)"         # strike @ price
)

# Futures option: Sold 1 /CLM6 ML2K6 05/11/26 Call 101.00 @ 0.31
_RE_FUT_OPT = re.compile(
    r"(Bought|Sold)\s+(\d+)\s+"
    r"(/[A-Z0-9]+)\s+"                 # futures root (e.g. /CLM6)
    r"([A-Z0-9]+)\s+"                  # sub-symbol (e.g. ML2K6)
    r"(\d{2}/\d{2}/\d{2})\s+"          # expiry MM/DD/YY
    r"(Call|Put)\s+"
    r"([\d.]+)\s+@\s+([\d.]+)"         # strike @ price
)

# Pure futures: Bought 1 /MESU6 @ 5678.25
_RE_FUT = re.compile(
    r"(Bought|Sold)\s+(\d+)\s+"
    r"(/[A-Z0-9]+)\s+"                 # futures symbol (e.g. /MESU6)
    r"@\s+([\d.]+)"                    # price
)

# Pure equity: Bought 100 GOOG @ 172.50
_RE_EQ = re.compile(
    r"(Bought|Sold)\s+(\d+)\s+"
    r"([A-Z][A-Z0-9]*)\s+"             # ticker
    r"@\s+([\d.]+)"                    # price
)

# Order header: Your order #465468389 received 2 fills
_RE_ORDER = re.compile(r"order\s+#(\d+)\s+received\s+(\d+)\s+fills?", re.I)

# Account: For account ending in 95
_RE_ACCOUNT = re.compile(r"account\s+ending\s+in\s+(\d+)", re.I)

# Fill timestamp: Filled at: May 11, 2026 11:13:07 AM EDT
_RE_FILLED_AT = re.compile(r"Filled\s+at:\s+(.+?)(?:\n|$)")


# ── Contract multiplier table (same as models._CONTRACT_MULT) ───────────────

_CONTRACT_MULT = {
    "ES": 50,   "MES": 5,   "NQ": 20,   "MNQ": 2,
    "RTY": 50,  "M2K": 5,   "YM": 5,    "MYM": 0.5,
    "EMD": 100, "VX": 1000,
    "6A": 100000, "6B": 62500,  "6C": 100000, "6E": 125000,
    "6J": 12500000, "6M": 500000, "6N": 100000, "6S": 125000,
    "CL": 1000, "MCL": 100, "NG": 10000, "RB": 42000, "HO": 42000,
    "GC": 100, "MGC": 10,  "SI": 5000, "SIL": 1000,
    "HG": 25000, "PL": 50, "PA": 100,
    "ZB": 1000, "UB": 1000, "ZN": 1000, "ZF": 1000, "ZT": 2000,
    "ZC": 50,  "ZS": 50,  "ZW": 50,  "ZL": 600, "ZM": 100,
    "KC": 375, "CC": 10,  "CT": 500, "SB": 1120, "OJ": 150,
    "LE": 400, "HE": 400, "GF": 500,
    "BTC": 5, "MBT": 0.1, "ETH": 50, "MET": 0.1,
}

_FUT_MONTH = "FGHJKMNQUVXZ"


def _normalize_futures_root(symbol: str) -> str:
    """
    Strip contract month/year from a futures symbol to get the product root.
    /MESU6 → MES, /CLM6 → CL, /ZBU6 → ZB.
    """
    s = symbol.lstrip("/")
    m = re.match(rf"^([A-Z0-9]+?)([{_FUT_MONTH}])(\d{{1,2}})$", s)
    if m:
        return m.group(1)
    return s


def _contract_multiplier(root: str, instrument_type: str) -> float:
    """Dollar value of 1.0 of quoted price for one contract."""
    il = instrument_type.lower()
    if "equity option" in il:
        return 100.0
    if "future" in il:
        return float(_CONTRACT_MULT.get(root, 1))
    return 1.0


# ── TT symbol reconstruction ───────────────────────────────────────────────

def _build_equity_option_symbol(root: str, expiry_str: str,
                                 call_put: str, strike: float) -> str:
    """
    Build a TastyTrade-format OCC equity option symbol.

    Input:  root="NVDA", expiry_str="05/15/26", call_put="Call", strike=225.0
    Output: "NVDA  260515C00225000"

    OCC format: 6-char left-padded root + YYMMDD + C/P + 8-digit strike×1000
    """
    # Parse MM/DD/YY → YYMMDD
    parts = expiry_str.split("/")
    mm, dd, yy = parts[0], parts[1], parts[2]
    yymmdd = f"{yy}{mm}{dd}"

    # C or P
    cp = "C" if call_put.lower().startswith("c") else "P"

    # 8-digit strike: multiply by 1000 and zero-pad
    strike_int = int(round(strike * 1000))
    strike_str = f"{strike_int:08d}"

    # 6-char padded root
    padded_root = f"{root:<6s}"

    return f"{padded_root}{yymmdd}{cp}{strike_str}"


def _build_futures_option_symbol(fut_root: str, sub_symbol: str,
                                  expiry_str: str, call_put: str,
                                  strike: float) -> str:
    """
    Build a TastyTrade-format futures option symbol.

    Input:  fut_root="/CLM6", sub_symbol="ML2K6",
            expiry_str="05/11/26", call_put="Call", strike=101.0
    Output: "./CLM6 ML2K6 260511C101"
    """
    parts = expiry_str.split("/")
    mm, dd, yy = parts[0], parts[1], parts[2]
    yymmdd = f"{yy}{mm}{dd}"

    cp = "C" if call_put.lower().startswith("c") else "P"

    # Strike: use integer if it's whole, else float
    if strike == int(strike):
        strike_str = str(int(strike))
    else:
        strike_str = f"{strike:g}"

    return f".{fut_root} {sub_symbol} {yymmdd}{cp}{strike_str}"


def _parse_expiry_to_iso(expiry_str: str) -> str:
    """Convert MM/DD/YY → ISO date string YYYY-MM-DD."""
    parts = expiry_str.split("/")
    mm, dd, yy = int(parts[0]), int(parts[1]), int(parts[2])
    year = 2000 + yy if yy < 80 else 1900 + yy
    return f"{year:04d}-{mm:02d}-{dd:02d}"


def _parse_fill_timestamp(ts_str: str) -> Optional[str]:
    """
    Parse a fill timestamp like 'May 11, 2026 11:13:07 AM EDT' to ISO format.
    Returns ISO string or None on failure.
    """
    # Strip timezone abbreviation (EDT, EST, CST, etc.) — Python's strptime
    # doesn't handle them natively, and the exact offset isn't critical.
    cleaned = re.sub(r"\s+[A-Z]{2,4}\s*$", "", ts_str.strip())
    for fmt in (
        "%B %d, %Y %I:%M:%S %p",   # May 11, 2026 11:13:07 AM
        "%b %d, %Y %I:%M:%S %p",   # May 11, 2026 11:13:07 AM (abbreviated)
        "%B %d, %Y %H:%M:%S",      # May 11, 2026 15:13:07
        "%b %d, %Y %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.isoformat() + "Z"
        except ValueError:
            continue
    return None


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class ParsedFill:
    """One fill line extracted from a trade confirmation email."""
    action: str             # "Bought" or "Sold"
    quantity: float
    root: str               # "NVDA" or "/CLM6"
    sub_symbol: Optional[str]  # futures option series (e.g. "ML2K6")
    expiry_str: Optional[str]  # "05/15/26" or None for pure futures/equity
    call_put: Optional[str]    # "Call", "Put", or None
    strike: Optional[float]
    price: float
    filled_at: Optional[str]   # ISO timestamp or None

    # Derived fields (set by parse_trade_email)
    tt_symbol: str = ""
    instrument_type: str = ""
    multiplier: float = 1.0
    normalized_root: str = ""  # e.g. "NVDA", "CL", "MES"
    expiry_iso: Optional[str] = None  # "2026-05-15"


@dataclass
class ParsedEmail:
    """All data extracted from one trade confirmation email."""
    order_id: Optional[str] = None
    account_suffix: Optional[str] = None
    fills: list[ParsedFill] = field(default_factory=list)
    raw_text: str = ""
    parse_errors: list[str] = field(default_factory=list)


# ── Main parser ─────────────────────────────────────────────────────────────

def parse_trade_email(body: str) -> ParsedEmail:
    """
    Parse a TastyTrade trade confirmation email body (plain text or
    HTML-stripped text) into structured fill objects.

    Returns a ParsedEmail with zero or more fills. If the email can't be
    parsed at all, fills will be empty and parse_errors populated.
    """
    result = ParsedEmail(raw_text=body)

    # Extract order header
    m = _RE_ORDER.search(body)
    if m:
        result.order_id = m.group(1)

    # Extract account suffix
    m = _RE_ACCOUNT.search(body)
    if m:
        result.account_suffix = m.group(1)

    # Extract fill timestamps (one per fill line, in order)
    fill_times = _RE_FILLED_AT.findall(body)
    parsed_times = [_parse_fill_timestamp(ts) for ts in fill_times]

    # Try to match fill lines — check most specific patterns first.
    # Split body into lines for ordered matching.
    lines = body.split("\n")
    fill_idx = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue

        fill: Optional[ParsedFill] = None
        ts = parsed_times[fill_idx] if fill_idx < len(parsed_times) else None

        # 1. Futures option
        m = _RE_FUT_OPT.search(line)
        if m:
            fut_root = m.group(3)
            sub_sym = m.group(4)
            expiry = m.group(5)
            cp = m.group(6)
            strike = float(m.group(7))
            price = float(m.group(8))
            norm_root = _normalize_futures_root(fut_root)
            tt_sym = _build_futures_option_symbol(fut_root, sub_sym, expiry, cp, strike)

            fill = ParsedFill(
                action=m.group(1),
                quantity=float(m.group(2)),
                root=fut_root,
                sub_symbol=sub_sym,
                expiry_str=expiry,
                call_put=cp,
                strike=strike,
                price=price,
                filled_at=ts,
                tt_symbol=tt_sym,
                instrument_type="Future Option",
                multiplier=_contract_multiplier(norm_root, "Future Option"),
                normalized_root=norm_root,
                expiry_iso=_parse_expiry_to_iso(expiry),
            )

        # 2. Equity option
        if fill is None:
            m = _RE_EQ_OPT.search(line)
            if m:
                root = m.group(3)
                expiry = m.group(4)
                cp = m.group(5)
                strike = float(m.group(6))
                price = float(m.group(7))
                tt_sym = _build_equity_option_symbol(root, expiry, cp, strike)

                fill = ParsedFill(
                    action=m.group(1),
                    quantity=float(m.group(2)),
                    root=root,
                    sub_symbol=None,
                    expiry_str=expiry,
                    call_put=cp,
                    strike=strike,
                    price=price,
                    filled_at=ts,
                    tt_symbol=tt_sym,
                    instrument_type="Equity Option",
                    multiplier=100.0,
                    normalized_root=root,
                    expiry_iso=_parse_expiry_to_iso(expiry),
                )

        # 3. Pure futures
        if fill is None:
            m = _RE_FUT.search(line)
            if m:
                fut_sym = m.group(3)
                norm_root = _normalize_futures_root(fut_sym)
                fill = ParsedFill(
                    action=m.group(1),
                    quantity=float(m.group(2)),
                    root=fut_sym,
                    sub_symbol=None,
                    expiry_str=None,
                    call_put=None,
                    strike=None,
                    price=float(m.group(4)),
                    filled_at=ts,
                    tt_symbol=fut_sym,
                    instrument_type="Future",
                    multiplier=_contract_multiplier(norm_root, "Future"),
                    normalized_root=norm_root,
                )

        # 4. Pure equity
        if fill is None:
            m = _RE_EQ.search(line)
            if m:
                ticker = m.group(3)
                fill = ParsedFill(
                    action=m.group(1),
                    quantity=float(m.group(2)),
                    root=ticker,
                    sub_symbol=None,
                    expiry_str=None,
                    call_put=None,
                    strike=None,
                    price=float(m.group(4)),
                    filled_at=ts,
                    tt_symbol=ticker,
                    instrument_type="Equity",
                    multiplier=1.0,
                    normalized_root=ticker,
                )

        if fill is not None:
            result.fills.append(fill)
            fill_idx += 1

    if not result.fills:
        result.parse_errors.append("No fill lines found in email body")

    return result
