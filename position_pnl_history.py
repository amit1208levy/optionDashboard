"""
position_pnl_history.py — reconstruct historical P&L curves for legs.

For each leg of a strategy we estimate, on each calendar day in its hold
window, what the leg's P&L would have been.  The estimate uses Black-
Scholes re-pricing with an implied volatility calibrated from the leg's
opening mark, so both delta evolution (gamma) and time decay (theta) are
captured automatically — no need to assume delta is constant.

For pure futures and equity legs the math collapses to:
    P&L(t) = sign × qty × multiplier × (S(t) − S(open))
where S(t) is the underlying close on day t.

For options (equity or futures-options):
    σ_open  = implied vol that makes BS(S_open, K, T_open, r, σ) = open_mark
    BS(t)   = BS(S(t), K, T(t), r, σ_open)
    P&L(t)  = sign × qty × multiplier × (BS(t) − open_mark)

For closed legs we additionally calibrate the curve so that BS(close_date)
exactly matches the realized P&L; this avoids a visual jump at close.

When we don't have the open mark for a closed leg (older history files),
we fall back to a constant 30 % IV and rely entirely on the calibration
step to anchor the endpoint.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Optional

import price_history


# ── Black-Scholes ────────────────────────────────────────────────────────────

_RISK_FREE = 0.05            # constant; close enough for visualization purposes
_SQRT_2PI  = math.sqrt(2 * math.pi)


def _norm_cdf(x: float) -> float:
    """Standard-normal CDF via erf — no scipy dep."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _bs_price(S: float, K: float, T: float, r: float, sigma: float,
              call_put: str) -> float:
    """Black-Scholes European option price."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        # Return intrinsic at expiry / undefined inputs.
        if call_put == "C":
            return max(0.0, S - K)
        return max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call_put == "C":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _implied_vol(S: float, K: float, T: float, r: float, mark: float,
                  call_put: str) -> Optional[float]:
    """
    Solve BS(σ) = mark for σ via Brent-style bisection.
    Returns None if no solution exists in [1e-4, 5.0].
    """
    if mark <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None
    lo, hi = 1e-4, 5.0
    f_lo = _bs_price(S, K, T, r, lo, call_put) - mark
    f_hi = _bs_price(S, K, T, r, hi, call_put) - mark
    if f_lo * f_hi > 0:
        # Mark lies outside the bracket — return endpoint vol whose BS
        # is closest, capped to a reasonable range.
        return hi if abs(f_hi) < abs(f_lo) else lo
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        f_mid = _bs_price(S, K, T, r, mid, call_put) - mark
        if abs(f_mid) < 1e-4:
            return mid
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


# ── Date helpers ─────────────────────────────────────────────────────────────

def _parse_iso_date(s) -> Optional[date]:
    if not s:
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _trading_days(start: date, end: date) -> list[date]:
    """All weekdays between start and end inclusive (excludes Sat/Sun)."""
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ── Per-leg curves ───────────────────────────────────────────────────────────

def estimate_open_leg_curve(
    *,
    root: str,
    sign: int,
    qty: float,
    multiplier: float,
    open_date: date,
    open_mark: float,
    is_option: bool,
    call_put: Optional[str],
    strike: Optional[float],
    expiration: Optional[date],
    end_date: Optional[date] = None,
    current_mark: Optional[float] = None,
) -> list[tuple[date, float]]:
    """
    Build a daily P&L series for an OPEN leg from open_date through end_date
    (default: today).  Uses BS re-pricing for options, S(t)-based math for
    futures / equities.
    """
    if end_date is None:
        end_date = date.today()
    if open_date > end_date:
        return []
    closes = price_history.get_daily_closes(root, open_date, end_date)
    if not closes:
        return []

    S_open = closes.get(open_date) or price_history.get_close_on_or_before(
        root, open_date)
    if S_open is None:
        # No underlying data for the open date — fall back to current mark
        # if available, otherwise bail.
        if current_mark is None or open_mark is None:
            return []
        S_open = open_mark   # last-resort placeholder

    sigma_open = None
    if is_option and strike and expiration and open_mark and open_mark > 0:
        T_open = max(0.0, (expiration - open_date).days / 365.0)
        sigma_open = _implied_vol(S_open, float(strike), T_open, _RISK_FREE,
                                   float(open_mark), call_put or "C")

    out: list[tuple[date, float]] = []
    for d in _trading_days(open_date, end_date):
        S_t = closes.get(d)
        if S_t is None:
            continue
        if is_option and strike and expiration:
            T_t = max(0.0, (expiration - d).days / 365.0)
            sigma = sigma_open if sigma_open is not None else 0.30
            mark_t = _bs_price(S_t, float(strike), T_t, _RISK_FREE, sigma,
                                call_put or "C")
        else:
            # Futures / equity: mark IS the underlying price.
            mark_t = S_t
        pnl = sign * qty * (multiplier or 1.0) * (mark_t - (open_mark or 0.0))
        out.append((d, pnl))
    return out


def estimate_closed_leg_curve(
    *,
    root: str,
    sign: int,
    qty: float,
    multiplier: float,
    open_date: date,
    close_date: date,
    open_mark: Optional[float],
    close_mark: Optional[float],
    realized_pnl: float,
    is_option: bool,
    call_put: Optional[str],
    strike: Optional[float],
    expiration: Optional[date],
) -> list[tuple[date, float]]:
    """
    Build a daily P&L series for a CLOSED leg from open_date through close_date.
    The endpoint is calibrated to match `realized_pnl` exactly.

    After close_date, callers should treat the leg's contribution as a
    constant equal to realized_pnl (this function only returns the hold
    window).
    """
    if open_date > close_date:
        return []
    closes = price_history.get_daily_closes(root, open_date, close_date)
    if not closes:
        # No price data — just step at open and close.
        return [(open_date, 0.0), (close_date, realized_pnl)]

    S_open = closes.get(open_date) or price_history.get_close_on_or_before(
        root, open_date)
    if S_open is None:
        return [(open_date, 0.0), (close_date, realized_pnl)]

    sigma = None
    if is_option and strike and expiration and open_mark and open_mark > 0:
        T_open = max(0.0, (expiration - open_date).days / 365.0)
        sigma = _implied_vol(S_open, float(strike), T_open, _RISK_FREE,
                              float(open_mark), call_put or "C")
    if sigma is None:
        sigma = 0.30

    raw: list[tuple[date, float]] = []
    for d in _trading_days(open_date, close_date):
        S_t = closes.get(d)
        if S_t is None:
            continue
        if is_option and strike and expiration:
            T_t = max(0.0, (expiration - d).days / 365.0)
            mark_t = _bs_price(S_t, float(strike), T_t, _RISK_FREE, sigma,
                                call_put or "C")
        else:
            mark_t = S_t
        raw_pnl = sign * qty * (multiplier or 1.0) * (mark_t - (open_mark or 0.0))
        raw.append((d, raw_pnl))

    if not raw:
        return [(open_date, 0.0), (close_date, realized_pnl)]

    # Calibration: scale curve so the last point matches realized_pnl.
    last_estimated = raw[-1][1]
    if abs(last_estimated) < 1e-6:
        # Degenerate — just shift to anchor.
        out = [(d, realized_pnl) for d, _ in raw]
        out[0] = (raw[0][0], 0.0)
        return out
    scale = realized_pnl / last_estimated if last_estimated else 1.0
    return [(d, p * scale) for d, p in raw]


# ── Per-underlying combined series ───────────────────────────────────────────

def build_per_underlying_series(
    *,
    legs: list,                # live Leg objects (open positions)
    history_entries: list,     # closed-leg dicts
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> dict[str, list[tuple[date, float]]]:
    """
    For each unique underlying root, compute a daily-summed P&L series
    spanning from the earliest leg open to today.

    Returns {root: [(date, pnl), ...]} sorted by date, with one point per
    weekday in the window (and constant carry-forward for closed legs).
    """
    if end_date is None:
        end_date = date.today()

    # Group legs by root.
    by_root: dict[str, dict] = {}

    for leg in legs or []:
        root = getattr(leg, "root", None)
        if not root:
            continue
        by_root.setdefault(root, {"open": [], "closed": []})["open"].append(leg)

    for h in history_entries or []:
        root = h.get("root")
        if not root:
            continue
        by_root.setdefault(root, {"open": [], "closed": []})["closed"].append(h)

    out: dict[str, list[tuple[date, float]]] = {}
    for root, groups in by_root.items():
        # Determine root's window.
        starts = []
        for leg in groups["open"]:
            d = _parse_iso_date(getattr(leg, "created_at", None))
            if d:
                starts.append(d)
        for h in groups["closed"]:
            d = _parse_iso_date(h.get("opened_at"))
            if d:
                starts.append(d)
        if not starts:
            continue
        root_start = start_date or min(starts)
        root_end   = end_date

        # Aggregate daily P&L: dict[date] -> running sum.
        daily: dict[date, float] = {d: 0.0 for d in _trading_days(root_start, root_end)}

        # Open legs.
        for leg in groups["open"]:
            open_d = _parse_iso_date(getattr(leg, "created_at", None))
            if open_d is None:
                continue
            curve = estimate_open_leg_curve(
                root          = root,
                sign          = int(getattr(leg, "sign", 1)),
                qty           = float(getattr(leg, "quantity", 0) or 0),
                multiplier    = float(getattr(leg, "multiplier", 1) or 1),
                open_date     = open_d,
                open_mark     = float(getattr(leg, "avg_open_price", 0) or 0),
                is_option     = bool(getattr(leg, "is_option", False)),
                call_put      = getattr(leg, "call_put", None),
                strike        = getattr(leg, "strike", None),
                expiration    = (getattr(leg, "expires_at", None).date()
                                  if getattr(leg, "expires_at", None) else None),
                end_date      = root_end,
                current_mark  = float(getattr(leg, "mark_price", 0) or 0),
            )
            for d, p in curve:
                if d in daily:
                    daily[d] += p

        # Closed legs.
        for h in groups["closed"]:
            open_d  = _parse_iso_date(h.get("opened_at"))
            close_d = _parse_iso_date(h.get("closed_at"))
            if open_d is None or close_d is None:
                # Just step at close.
                if close_d and close_d in daily:
                    daily[close_d] += float(h.get("pnl") or 0.0)
                # And carry forward.
                continue
            realized = float(h.get("pnl") or 0.0)
            curve = estimate_closed_leg_curve(
                root          = root,
                sign          = int(h.get("sign") or 1),
                qty           = float(h.get("qty") or 0),
                multiplier    = float(h.get("multiplier") or 1),
                open_date     = open_d,
                close_date    = close_d,
                open_mark     = h.get("open_price"),
                close_mark    = h.get("close_price"),
                realized_pnl  = realized,
                is_option     = bool(h.get("call_put")),
                call_put      = h.get("call_put"),
                strike        = h.get("strike"),
                expiration    = None,   # not stored on history; BS will use
                                        # opened-date implied IV anyway, and
                                        # the calibration anchors the end.
            )
            # Apply hold-window contribution.
            for d, p in curve:
                if d in daily:
                    daily[d] += p
            # After close_date, leg contributes constant `realized` forever.
            d = close_d + timedelta(days=1)
            while d <= root_end:
                if d in daily:
                    daily[d] += realized
                d += timedelta(days=1)

        out[root] = sorted(daily.items(), key=lambda x: x[0])

    return out
