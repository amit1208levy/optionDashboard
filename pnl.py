"""
YTD P&L calculation via raw TastyTrade API calls.

No SDK dependency — uses only `requests` (already bundled in every install).
This means the auto-updater can deliver this code to existing users without
needing them to download a new .app.

Algorithm
─────────
    P/L YTD w/Fees  =  NetLiq_today − NetLiq_Jan1 − Net Cash Flow YTD
    P/L YTD (gross) =  P/L YTD w/Fees + Fees YTD

NetLiq already reflects every fee paid (came out of cash), so the delta
is naturally net-of-fees.  Adding fees back gives the gross figure.

"Net Cash Flow" includes:
  • External deposits / withdrawals / wires / ACH / transfers / rollovers
  • Daily Mark-to-Market settlements on futures (broker tracks this
    separately from P&L on their UI)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import requests

BASE = "https://api.tastyworks.com"
UA   = "options-dashboard/1.0"

# TastyTrade datetime format expected by /net-liq/history
_TT_DATE_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Sub-types of "Money Movement" whose name alone tells us it's external
# cash flow (should be subtracted from NetLiq delta so it doesn't inflate P&L).
# Mark to Market is TastyTrade's daily futures cash settlement — also belongs
# outside of P&L per their UI convention.
_EXPLICIT_EXTERNAL_SUBTYPES = (
    "deposit", "withdrawal", "withdraw", "wire", "ach",
    "rollover", "mark to market",
)

# Ambiguous "Transfer" sub-types need the description to classify:
#   • Internal (between two TastyTrade accounts)  → leave in P&L
#   • External (to an outside bank)                → subtract from P&L
import re as _re
_TT_ACCT_RE = _re.compile(r"\b[0-9][A-Z]{2}\d{5}\b", _re.IGNORECASE)
_INTERNAL_HINTS = ("internal", "account transfer", "between accounts",
                   "journal", "sweep", "inter-account")
_EXTERNAL_HINTS = ("bank", "ach", "wire", "external", "check",
                   "to bank", "from bank")


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "User-Agent":    UA,
    }


def _is_external_money_movement(t: dict) -> bool:
    """
    Return True if this Money-Movement transaction is external cash flow
    (should be subtracted from the NetLiq-delta when computing P/L YTD).

    Explicit sub-types (Deposit, Withdrawal, Wire, ACH, MTM) are always
    external.  "Transfer" is ambiguous — a Transfer between two TastyTrade
    accounts is internal and should NOT be treated as cash flow, while a
    Transfer to an outside bank should be.  We look at the description
    field to decide:
       • mentions a TastyTrade account number or "internal" → internal
       • mentions bank / ACH / wire / external             → external
       • otherwise default to INTERNAL (safer — matches TT's own UI where
         ambiguous transfers stay inside P&L).
    """
    if (t.get("transaction-type") or "").lower() != "money movement":
        return False
    sub  = (t.get("transaction-sub-type") or "").lower()
    desc = (t.get("description") or "").lower()

    # Explicit external sub-types — always count
    if any(kw in sub for kw in _EXPLICIT_EXTERNAL_SUBTYPES):
        return True

    # Ambiguous "transfer" → inspect description first, then default external
    if "transfer" in sub:
        # Any TT account number (e.g. 5WZ12345) in the description means
        # it's a transfer BETWEEN two TT accounts — treat as internal.
        if _TT_ACCT_RE.search(desc):
            return False
        if any(kw in desc for kw in _INTERNAL_HINTS):
            return False
        # Default: treat as external cash flow (subtract from P&L).
        # Most users' transfers are bank deposits/withdrawals; the less-
        # common case of an internal transfer between two TT accounts gets
        # tagged with a TT account number or "internal" in the description
        # which the checks above already catch.
        return True

    return False


def _signed_value(t: dict) -> float:
    """
    Raw API returns `value` as an unsigned amount + a separate `value-effect`
    field ("Credit" / "Debit").  Convert to a signed dollar number where
    positive = money in (deposit / credit), negative = money out.
    """
    v = _to_float(t.get("value"))
    eff = (t.get("value-effect") or "").lower()
    return v if "credit" in eff else -v


def _tx_fees(t: dict) -> float:
    """Sum every fee field (always returns ≥ 0)."""
    return (abs(_to_float(t.get("commission")))
            + abs(_to_float(t.get("clearing-fees")))
            + abs(_to_float(t.get("regulatory-fees")))
            + abs(_to_float(t.get("proprietary-index-option-fees"))))


# ── API calls ─────────────────────────────────────────────────────────────────

def _get_balances(token: str, account_number: str) -> dict:
    r = requests.get(
        f"{BASE}/accounts/{account_number}/balances",
        headers=_headers(token), timeout=15,
    )
    r.raise_for_status()
    return r.json().get("data", {}) or {}


def _get_net_liq_history(token: str, account_number: str,
                         start_time: datetime) -> list:
    """
    GET /accounts/{num}/net-liq/history?start-time=YYYY-MM-DDTHH:MM:SSZ
    Returns list of OHLC snapshots with .close field for each day.
    Retries with backoff on 429 (rate-limit).
    """
    import time
    for attempt in range(4):
        r = requests.get(
            f"{BASE}/accounts/{account_number}/net-liq/history",
            headers=_headers(token),
            params={"start-time": start_time.strftime(_TT_DATE_FMT)},
            timeout=15,
        )
        if r.status_code == 429:
            time.sleep(0.5 * (attempt + 1))   # 0.5s, 1s, 1.5s, 2s
            continue
        r.raise_for_status()
        return r.json().get("data", {}).get("items", []) or []
    # All retries exhausted
    r.raise_for_status()
    return []


def _get_history_ytd(token: str, account_number: str) -> list:
    """
    Paginate through /accounts/{num}/transactions for the current year.
    Returns all transaction dicts.
    """
    items = []
    year_start = f"{date.today().year}-01-01"
    page = 0
    while page < 40:   # safety cap (40 × 250 = 10,000 txns)
        try:
            r = requests.get(
                f"{BASE}/accounts/{account_number}/transactions",
                headers=_headers(token),
                params={"per-page": 250, "page-offset": page,
                        "start-date": year_start},
                timeout=30,
            )
        except requests.exceptions.RequestException:
            break
        if r.status_code != 200:
            break
        batch = r.json().get("data", {}).get("items", []) or []
        items.extend(batch)
        if len(batch) < 250:
            break
        page += 1
    return items


# ── main entry point ──────────────────────────────────────────────────────────

def compute_ytd_pnl(access_token: str, account_number: str,
                    raise_on_error: bool = False) -> Optional[dict]:
    """
    Compute YTD P&L numbers using raw TastyTrade API calls (no SDK).

    Returns a dict on success, None on any failure (so caller can fall back).
    Set raise_on_error=True for debugging — propagates exceptions.

    Result dict shape:
        {
            "p_l_ytd":           float,   # gross of fees
            "p_l_ytd_w_fees":    float,   # net of fees
            "ytd_fees":          float,   # ≥ 0
            "ytd_net_deposits":  float,   # signed: + = deposit, − = withdrawal
            "year_start_net_liq":float,
            "current_net_liq":   float,
            "unknown_subs":      dict,    # diagnostic: unrecognized MM sub-types
        }
    """
    try:
        year       = date.today().year
        year_start = datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        # ── 1. Current NetLiq ────────────────────────────────────────────────
        bal        = _get_balances(access_token, account_number)
        current_nl = _to_float(bal.get("net-liquidating-value"))

        # ── 2. NetLiq at Jan 1 (first snapshot >= year start) ───────────────
        nl_history = _get_net_liq_history(access_token, account_number, year_start)
        if nl_history:
            year_start_nl = (
                _to_float(nl_history[0].get("close"))
                or _to_float(nl_history[0].get("open"))
                or current_nl
            )
        else:
            year_start_nl = current_nl

        # ── 3. YTD transactions: separate cash flow + sum fees ──────────────
        txns = _get_history_ytd(access_token, account_number)

        ytd_fees     = 0.0
        net_deposits = 0.0
        unknown_subs: dict = {}
        for t in txns:
            ttype = (t.get("transaction-type") or "").lower()
            if ttype in ("trade", "receive deliver"):
                ytd_fees += _tx_fees(t)
            elif ttype == "money movement":
                if _is_external_money_movement(t):
                    net_deposits += _signed_value(t)
                else:
                    sub = t.get("transaction-sub-type") or "(none)"
                    if sub not in ("Balance Adjustment", "Credit Interest",
                                    "Debit Interest", "Subscription Fee",
                                    "Dividend", "Mark to Market"):
                        unknown_subs[sub] = unknown_subs.get(sub, 0.0) \
                                          + _signed_value(t)

        # ── 4. Apply formula ────────────────────────────────────────────────
        p_l_w_fees = current_nl - year_start_nl - net_deposits
        p_l_gross  = p_l_w_fees + ytd_fees

        return {
            "p_l_ytd":             p_l_gross,
            "p_l_ytd_w_fees":      p_l_w_fees,
            "ytd_fees":            ytd_fees,
            "ytd_net_deposits":    net_deposits,
            "year_start_net_liq":  year_start_nl,
            "current_net_liq":     current_nl,
            "unknown_subs":        unknown_subs,
        }

    except Exception as e:
        if raise_on_error:
            raise
        # Log to stderr so we can see why it failed in the live app
        import sys, traceback
        print(f"[pnl] account {account_number} failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return None
