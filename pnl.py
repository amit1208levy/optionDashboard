"""
YTD P&L calculation using the official TastyTrade Python SDK.

The SDK gives us verified field names, typed responses, and methods like
get_net_liquidating_value_history() and get_history() that we'd otherwise
have to reverse-engineer.  This module isolates everything SDK-specific so
the rest of the app stays decoupled.

Authentication
──────────────
Our app uses OAuth (refresh-token + client-secret) but the SDK only ships
with email/password Session.__init__.  We construct a Session manually,
attaching our existing Bearer access token to its httpx client, so SDK
methods work exactly as if the user had logged in through the SDK.

Algorithm
─────────
    P/L YTD w/Fees  =  NetLiq_today − NetLiq_Jan1 − Net Deposits YTD
    P/L YTD (gross) =  P/L YTD w/Fees + Fees YTD

NetLiq already reflects every fee paid (came out of cash), so the delta
is naturally net-of-fees.  Adding fees back gives the gross figure.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from httpx import Client
from tastytrade import Account, Session
from tastytrade.account import Transaction


_API_URL = "https://api.tastyworks.com"
_UA      = "options-dashboard/1.0"


# ── Session construction (OAuth) ──────────────────────────────────────────────

def make_oauth_session(access_token: str) -> Session:
    """
    Build a Session object that uses our existing OAuth access token,
    bypassing the SDK's email+password login flow.
    """
    s = Session.__new__(Session)
    s.is_test       = False
    s.proxy         = None
    s.session_token = access_token
    s.user          = None
    s.remember_token = None
    s.sync_client = Client(
        base_url=_API_URL,
        headers={
            "Accept":        "application/json",
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent":    _UA,
        },
    )
    return s


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _tx_fees(t: Transaction) -> float:
    """
    Sum every explicit fee field as positive dollars.
    The SDK returns commission etc. signed (e.g. -0.75 for a $0.75 debit) so
    we take abs() of each field to get a positive "fees paid" total.
    """
    return (abs(_to_float(t.commission))
            + abs(_to_float(t.clearing_fees))
            + abs(_to_float(t.regulatory_fees))
            + abs(_to_float(t.proprietary_index_option_fees)))


# Sub-types of "Money Movement" that represent cash flow we want to subtract
# from the NetLiq delta (i.e. they shouldn't show up in P/L YTD):
#   • Real external cash:     Deposit, Withdrawal, Wire, ACH, Transfer, Rollover
#   • Daily futures cash settlement: Mark to Market — TastyTrade tracks
#     futures P&L outside of NetLiq delta, so we exclude these flows here too
# Anything not in this list (Balance Adjustment, Credit Interest, Subscription
# Fee, …) stays inside P&L.  Match case-insensitively against substrings.
_DEPOSIT_KEYWORDS = (
    "deposit", "withdrawal", "withdraw", "wire", "ach",
    "transfer", "rollover", "mark to market",
)


def _is_external_money_movement(t: Transaction) -> bool:
    """True if this transaction represents external cash entering/leaving the account."""
    if (t.transaction_type or "").lower() != "money movement":
        return False
    sub = (t.transaction_sub_type or "").lower()
    return any(kw in sub for kw in _DEPOSIT_KEYWORDS)


def _tx_signed_value(t: Transaction) -> float:
    """Signed dollar value (positive = money in, negative = money out)."""
    return _to_float(t.value)


# ── main entry point ──────────────────────────────────────────────────────────

def compute_ytd_pnl(access_token: str, account_number: str,
                    raise_on_error: bool = False) -> Optional[dict]:
    """
    Compute YTD P&L numbers for one account using verified TastyTrade SDK
    methods.  Returns a dict on success, or None on any failure (so caller
    can fall back to a different algorithm).

    Set raise_on_error=True for debugging — propagates exceptions so the
    failing line is visible.

    Result dict shape:
        {
            "p_l_ytd":           float,   # gross of fees
            "p_l_ytd_w_fees":    float,   # net of fees (NetLiq delta)
            "ytd_fees":          float,   # ≥ 0
            "ytd_net_deposits":  float,   # signed: + = deposit, − = withdrawal
            "year_start_net_liq":float,
            "current_net_liq":   float,
        }
    """
    try:
        session = make_oauth_session(access_token)
        # Pydantic v2: must use model_construct() to skip validation but still
        # set up internal model state.  __new__ alone leaves the object broken.
        account = Account.model_construct(account_number=account_number)

        year       = date.today().year
        year_start = datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        # ── 1. Current NetLiq ────────────────────────────────────────────────
        current_balance = account.get_balances(session)
        current_nl      = _to_float(current_balance.net_liquidating_value)

        # ── 2. NetLiq at Jan 1 (last EOD snapshot ≤ year start) ─────────────
        nl_history = account.get_net_liquidating_value_history(
            session, start_time=year_start
        )
        # The endpoint returns history starting AT or just AFTER start_time,
        # so the earliest entry is approximately year-open NetLiq.
        if nl_history:
            # Snapshot objects use either `.close` or `.net-liquidating-value`
            # depending on SDK version — try both.
            first = nl_history[0]
            year_start_nl = (
                _to_float(getattr(first, "close", None))
                or _to_float(getattr(first, "net_liquidating_value", None))
                or _to_float(getattr(first, "open", None))
            )
            if not year_start_nl:
                year_start_nl = current_nl
        else:
            year_start_nl = current_nl

        # ── 3. YTD transactions: separate deposits + sum fees ────────────────
        txns = account.get_history(session, start_date=date(year, 1, 1))

        ytd_fees     = 0.0
        net_deposits = 0.0
        for t in txns:
            ttype = (t.transaction_type or "").lower()
            if ttype in ("trade", "receive deliver"):
                ytd_fees += _tx_fees(t)
            if _is_external_money_movement(t):
                net_deposits += _tx_signed_value(t)

        # ── 4. Apply the formula ─────────────────────────────────────────────
        p_l_w_fees = current_nl - year_start_nl - net_deposits
        p_l_gross  = p_l_w_fees + ytd_fees

        return {
            "p_l_ytd":             p_l_gross,
            "p_l_ytd_w_fees":      p_l_w_fees,
            "ytd_fees":            ytd_fees,
            "ytd_net_deposits":    net_deposits,
            "year_start_net_liq":  year_start_nl,
            "current_net_liq":     current_nl,
        }

    except Exception:
        if raise_on_error:
            raise
        return None
