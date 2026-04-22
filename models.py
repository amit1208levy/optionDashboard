"""Domain logic: parse positions, group into strategies, compute payoff."""
import hashlib
import math
import re
from collections import defaultdict
from datetime import datetime, timezone


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _days_between(a, b):
    if not a or not b:
        return None
    return (a.date() - b.date()).days


# ── Symbol parsing ───────────────────────────────────────────────────────────

_FUT_MONTH = "FGHJKMNQUVXZ"

def normalize_root(underlying_symbol):
    """
    Strip futures contract month/year suffix to return the product root.
    /MESU6 -> MES, /ZCK6 -> ZC, /ZBU6 -> ZB. AAPL -> AAPL.
    """
    if not underlying_symbol:
        return underlying_symbol
    s = underlying_symbol.strip()
    if s.startswith("/"):
        s = s[1:]
        m = re.match(rf"^([A-Z0-9]+?)([{_FUT_MONTH}])(\d{{1,2}})$", s)
        if m:
            return m.group(1)
        return s
    return s


def parse_option_symbol(symbol):
    """Return (call_put, strike) or (None, None)."""
    if not symbol:
        return None, None
    m = re.search(r"(\d{6})([CP])(\d{8})\s*$", symbol)
    if m:
        return m.group(2), int(m.group(3)) / 1000.0
    m = re.search(r"(\d{6})([CP])(\d+(?:\.\d+)?)\s*$", symbol)
    if m:
        return m.group(2), float(m.group(3))
    return None, None


# ── Position ─────────────────────────────────────────────────────────────────

class Position:
    """
    Convention (per user feedback):
      * cost_basis   = premium amount at open (ALWAYS POSITIVE)
      * market_value = premium value at current mark (ALWAYS POSITIVE)
      * pnl          = sign × (market_value − cost_basis)
      * credit_debit = net cash flow at open: positive = credit received (short),
                       negative = debit paid (long)
    """

    def __init__(self, raw):
        self.raw             = raw
        self.symbol          = raw.get("symbol", "")
        self.underlying      = raw.get("underlying-symbol", "")
        self.root            = normalize_root(self.underlying) or self.underlying
        self.instrument_type = raw.get("instrument-type", "")

        qty_raw = _to_float(raw.get("quantity"))
        direction = (raw.get("quantity-direction") or "").lower()
        self.is_long   = direction == "long"
        self.sign      = 1 if self.is_long else -1
        self.quantity  = qty_raw

        self.avg_open_price = _to_float(raw.get("average-open-price"))
        self.mark_price     = _to_float(raw.get("mark-price"))
        self.close_price    = _to_float(raw.get("close-price"))
        self.multiplier     = _to_float(raw.get("multiplier"), 1.0)

        self.expires_at = _parse_iso(raw.get("expires-at"))
        self.created_at = _parse_iso(raw.get("created-at"))

        self.call_put, self.strike = parse_option_symbol(self.symbol)
        self.is_option = self.call_put in ("C", "P")
        # is_future: any futures-derived position (Future, Future Option, etc.)
        # Futures differ from equities: notional ≠ cash, delta = 1 per contract
        # times multiplier, no shares concept.
        self.is_future = "future" in (self.instrument_type or "").lower()

        # Monetary (cost basis / market value always positive)
        notional_open = self.quantity * self.multiplier * self.avg_open_price
        notional_mark = self.quantity * self.multiplier * self.mark_price
        self.cost_basis   = notional_open
        self.market_value = notional_mark
        self.pnl          = self.sign * (notional_mark - notional_open)
        self.credit_debit = -self.sign * notional_open  # +credit, -debit
        self.pnl_pct      = (self.pnl / notional_open * 100.0) if notional_open else 0.0

        # Filled in later from market-data endpoint (optional)
        self.delta           = None
        self.gamma           = None
        self.theta           = None
        self.vega            = None
        self.iv              = None
        self.underlying_price = None
        self.probability_otm = None   # from TastyTrade market-data API

    def _recompute(self):
        notional_open = self.quantity * self.multiplier * self.avg_open_price
        notional_mark = self.quantity * self.multiplier * self.mark_price
        self.cost_basis   = notional_open
        self.market_value = notional_mark
        self.pnl          = self.sign * (notional_mark - notional_open)
        self.credit_debit = -self.sign * notional_open
        self.pnl_pct      = (self.pnl / notional_open * 100.0) if notional_open else 0.0

    def attach_quote(self, quote):
        """Attach market-data response for this symbol: updates mark + Greeks."""
        if not quote:
            return
        def f(k):
            v = quote.get(k)
            try: return float(v)
            except (TypeError, ValueError): return None

        self.delta            = f("delta")
        self.gamma            = f("gamma")
        self.theta            = f("theta")
        self.vega             = f("vega")
        self.iv               = f("implied-volatility")
        self.underlying_price = f("underlying-price")

        # TastyTrade returns probability-otm (0–1 scale) per option quote.
        # This is the most accurate source — uses their own model with skew.
        raw_potm = f("probability-otm")
        if raw_potm is not None:
            # Normalise: API usually returns 0–1 but guard against 0–100
            self.probability_otm = raw_potm if raw_potm <= 1.0 else raw_potm / 100.0

        # Live mark — positions endpoint often returns 0 for futures options;
        # stocks return last/bid/ask rather than mark
        mark = f("mark")
        if mark is None or mark == 0:
            bid, ask = f("bid"), f("ask")
            if bid is not None and ask is not None:
                mark = (bid + ask) / 2.0
            elif f("last") is not None:
                mark = f("last")
        if mark is not None and mark > 0:
            self.mark_price = mark
            self._recompute()

    @property
    def dte(self):
        if not self.expires_at: return None
        return _days_between(self.expires_at, datetime.now(timezone.utc))

    @property
    def dit(self):
        if not self.created_at: return None
        return _days_between(datetime.now(timezone.utc), self.created_at)

    @property
    def direction_label(self):
        return "Long" if self.is_long else "Short"

    @property
    def type_label(self):
        if self.call_put == "C": return "Call"
        if self.call_put == "P": return "Put"
        return self.instrument_type or "Stock"

    @property
    def expiry_label(self):
        return self.expires_at.strftime("%b %d, %Y") if self.expires_at else "—"


# ── Strategy ─────────────────────────────────────────────────────────────────

class Strategy:
    def __init__(self, key, legs, custom_name=None, is_custom=False):
        self.key         = key
        self.legs        = sorted(
            legs,
            key=lambda l: (l.expires_at is None, l.expires_at or datetime.max.replace(tzinfo=timezone.utc),
                           l.strike or 0, l.call_put or "")
        )
        self.custom_name = custom_name
        self.is_custom   = is_custom   # True if assembled from user assignments

        # Choose a representative root/expiry
        self.root       = legs[0].root if legs else ""
        self.expires_at = min((l.expires_at for l in legs if l.expires_at), default=None)
        self.auto_name  = self._detect_name()

    @property
    def name(self):
        return self.custom_name or self.auto_name

    @property
    def cost_basis(self):   return sum(l.cost_basis for l in self.legs)
    @property
    def market_value(self): return sum(l.market_value for l in self.legs)
    @property
    def credit_debit(self): return sum(l.credit_debit for l in self.legs)
    @property
    def pnl(self):          return sum(l.pnl for l in self.legs)
    @property
    def pnl_pct(self):
        denom = self.cost_basis
        return (self.pnl / denom * 100.0) if denom else 0.0

    @property
    def dte(self):
        if not self.expires_at: return None
        return _days_between(self.expires_at, datetime.now(timezone.utc))

    @property
    def dit(self):
        dates = [l.created_at for l in self.legs if l.created_at]
        if not dates: return None
        return _days_between(datetime.now(timezone.utc), min(dates))

    # Aggregate Greeks (position-weighted; None if any leg missing)
    @property
    def net_delta(self): return self._agg("delta")
    @property
    def net_gamma(self): return self._agg("gamma")
    @property
    def net_theta(self): return self._agg("theta")
    @property
    def net_vega(self):  return self._agg("vega")

    def _agg(self, key):
        total = 0.0
        any_set = False
        for l in self.legs:
            v = getattr(l, key)
            if v is None:
                continue
            any_set = True
            total += l.sign * l.quantity * 100 * v
        return total if any_set else None

    def _detect_name(self):
        legs = self.legs
        opts = [l for l in legs if l.is_option]
        n = len(legs)
        if n == 1:
            l = legs[0]
            if l.is_option:
                return f"{l.direction_label} {l.type_label}"
            # Differentiate futures contracts from stock shares
            if l.is_future:
                return f"{l.direction_label} {l.root} Future"
            return f"{l.direction_label} Shares"
        if n == 2 and len(opts) == 2:
            a, b = sorted(opts, key=lambda l: (l.strike or 0))
            if a.call_put == b.call_put:
                if a.is_long != b.is_long:
                    if a.call_put == "C":
                        return "Bull Call Spread" if a.is_long else "Bear Call Spread"
                    return "Bull Put Spread" if b.is_long else "Bear Put Spread"
            else:
                if a.strike == b.strike:
                    return ("Long" if a.is_long and b.is_long else "Short") + " Straddle"
                return ("Long" if a.is_long and b.is_long else "Short") + " Strangle"
        if n == 4 and len(opts) == 4:
            calls = [l for l in opts if l.call_put == "C"]
            puts  = [l for l in opts if l.call_put == "P"]
            if len(calls) == 2 and len(puts) == 2:
                short_call = next((l for l in calls if not l.is_long), None)
                short_put  = next((l for l in puts  if not l.is_long), None)
                if short_call and short_put:
                    return "Iron Butterfly" if short_call.strike == short_put.strike else "Iron Condor"
        return f"{n}-Leg Custom"


# ── Grouping ─────────────────────────────────────────────────────────────────

def auto_group_key(position):
    """Default auto-group key: root ticker only."""
    return f"auto:{position.root}"


def group_positions(positions, assignments, names):
    """
    assignments: {symbol: group_id}  — user overrides
    names:       {group_id: custom_name}
    Returns a list of Strategy, sorted by DTE ascending.
    """
    buckets = defaultdict(list)
    for p in positions:
        gid = assignments.get(p.symbol)
        if not gid:
            gid = auto_group_key(p)
        buckets[gid].append(p)

    strategies = []
    for gid, legs in buckets.items():
        is_custom = not gid.startswith("auto:")
        strategies.append(Strategy(gid, legs, names.get(gid), is_custom=is_custom))

    strategies.sort(key=lambda s: (s.dte is None, s.dte if s.dte is not None else 10**9))
    return strategies


# ── Payoff calculation ───────────────────────────────────────────────────────

def payoff_at(strategy, underlying_price):
    total = 0.0
    for leg in strategy.legs:
        if leg.call_put == "C":
            intrinsic = max(underlying_price - (leg.strike or 0), 0.0)
        elif leg.call_put == "P":
            intrinsic = max((leg.strike or 0) - underlying_price, 0.0)
        else:
            intrinsic = underlying_price  # shares
        total += leg.sign * leg.quantity * leg.multiplier * (intrinsic - leg.avg_open_price)
    return total


def payoff_range(strategy, steps=220, pad_pct=0.35):
    strikes = [l.strike for l in strategy.legs if l.strike]
    if not strikes:
        return [], []
    low  = max(0.01, min(strikes) * (1 - pad_pct))
    high = max(strikes) * (1 + pad_pct)
    step = (high - low) / steps
    xs = [low + i * step for i in range(steps + 1)]
    ys = [payoff_at(strategy, x) for x in xs]
    return xs, ys


def strategy_extremes(strategy):
    """
    Returns (max_profit, max_loss, breakevens)
    max_profit / max_loss may be float('inf')/float('-inf') for undefined-risk.
    """
    xs, ys = payoff_range(strategy)
    if not xs:
        return None, None, []

    max_profit = max(ys)
    max_loss   = min(ys)

    # Detect unbounded: slope at far ends
    # If the curve is still rising at high-end → profit unbounded
    # If still falling at high-end → loss unbounded
    # Same for low-end (but bounded by S >= 0)
    n = len(ys)
    right_slope = ys[-1] - ys[n-6]
    left_slope  = ys[5] - ys[0]

    if right_slope > 0.01 and ys[-1] >= max_profit - 1e-6:
        max_profit = float("inf")
    if right_slope < -0.01 and ys[-1] <= max_loss + 1e-6:
        max_loss = float("-inf")
    if left_slope < -0.01 and ys[0] >= max_profit - 1e-6:
        max_profit = float("inf")
    if left_slope > 0.01 and ys[0] <= max_loss + 1e-6:
        max_loss = float("-inf")

    # Breakevens (zero-crossings)
    breakevens = []
    for i in range(1, len(xs)):
        if (ys[i-1] <= 0 <= ys[i]) or (ys[i-1] >= 0 >= ys[i]):
            if ys[i] - ys[i-1] != 0:
                t = -ys[i-1] / (ys[i] - ys[i-1])
                breakevens.append(xs[i-1] + t * (xs[i] - xs[i-1]))

    return max_profit, max_loss, breakevens


# ── Probability of profit (log-normal model) ─────────────────────────────────

def _norm_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _iv_from_delta(delta, s0, strike, dte, is_call):
    """
    Approximate IV from Black-Scholes delta via bisection (r=0 model).
    Returns IV or None if solution not found.
    """
    if not all([delta, s0, strike, dte]) or dte <= 0:
        return None
    T = dte / 365.0
    abs_delta = abs(delta)
    if abs_delta <= 0 or abs_delta >= 1:
        return None
    import math as _math
    from_iv, to_iv = 0.001, 5.0
    for _ in range(60):
        mid = (from_iv + to_iv) / 2.0
        sigma_T = mid * _math.sqrt(T)
        if sigma_T == 0:
            break
        d1 = (_math.log(s0 / strike) + 0.5 * mid * mid * T) / sigma_T
        model_delta = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0
        if abs(abs(model_delta) - abs_delta) < 1e-5:
            return mid
        if abs(model_delta) > abs_delta:
            to_iv = mid
        else:
            from_iv = mid
    return (from_iv + to_iv) / 2.0


def probability_of_profit(strategy, iv_fallback=0.20):
    """
    Return P(strategy profitable at expiration) as a %, or None.

    Priority order:
      1. TastyTrade 'probability-otm' from the market-data API — most accurate,
         uses their own model with vol skew and term structure.
      2. Delta-based approximation: P(OTM) ≈ 1 − |delta|.
      3. Full log-normal model using IV (self-contained, least accurate).

    For two-sided short strategies (strangles, condors, straddles):
        POP = P(inner_put OTM) + P(inner_call OTM) − 100
    This is mathematically exact for any continuous distribution:
        P(K_put < S < K_call) = P(S < K_call) − P(S ≤ K_put)
                               = P(call OTM) − (1 − P(put OTM))
                               = P(put OTM) + P(call OTM) − 1
    For one-sided shorts:  POP = P(short leg OTM)
    For long options:      POP = P(ITM) = 1 − P(OTM) ≈ |delta|
    """
    legs = [l for l in strategy.legs if l.is_option]
    if not legs:
        return None

    # ── Identify key legs ─────────────────────────────────────────────────
    short_puts  = [l for l in legs if l.call_put == "P" and not l.is_long]
    short_calls = [l for l in legs if l.call_put == "C" and not l.is_long]
    long_opts   = [l for l in legs if l.is_long]

    # Use the INNER short strikes (closest to ATM) to define the profitable zone.
    inner_put  = max(short_puts,  key=lambda l: l.strike or 0)           if short_puts  else None
    inner_call = min(short_calls, key=lambda l: l.strike or float("inf")) if short_calls else None

    # ── Helper: best available P(OTM) for a leg ───────────────────────────
    def _potm_pct(leg):
        """Return P(option expires OTM) as 0–100, or None."""
        if leg.probability_otm is not None:
            return leg.probability_otm * 100.0          # stored as 0–1
        if leg.delta is not None:
            return (1.0 - abs(leg.delta)) * 100.0       # delta ≈ P(ITM)
        return None

    # ── Attempt API/delta-based calculation ───────────────────────────────
    if inner_put and inner_call:
        # Two-sided: strangle / straddle / condor
        p_put  = _potm_pct(inner_put)
        p_call = _potm_pct(inner_call)
        if p_put is not None and p_call is not None:
            return max(0.0, min(100.0, p_put + p_call - 100.0))

    elif inner_put:
        p = _potm_pct(inner_put)
        if p is not None:
            return max(0.0, min(100.0, p))

    elif inner_call:
        p = _potm_pct(inner_call)
        if p is not None:
            return max(0.0, min(100.0, p))

    elif long_opts:
        # Long option: POP = P(ITM at expiry) = 1 − P(OTM)
        l = long_opts[0]
        if l.probability_otm is not None:
            return max(0.0, min(100.0, (1.0 - l.probability_otm) * 100.0))
        if l.delta is not None:
            return max(0.0, min(100.0, abs(l.delta) * 100.0))

    # ── Fallback: full log-normal model ───────────────────────────────────
    return _pop_lognormal(strategy, iv_fallback)


def _pop_lognormal(strategy, iv_fallback=0.20):
    """
    Self-contained log-normal POP estimate.  Used only when neither
    probability-otm nor delta is available from the API.
    """
    legs = [l for l in strategy.legs if l.is_option]
    if not legs:
        return None

    s0 = next((l.underlying_price for l in legs if l.underlying_price), None)
    if s0 is None:
        best = None
        for l in legs:
            if l.strike and l.delta is not None:
                score = abs(abs(l.delta) - 0.5)
                if best is None or score < best[0]:
                    best = (score, l.strike)
        if best:
            s0 = best[1]
    if s0 is None:
        strikes = [l.strike for l in legs if l.strike]
        if strikes:
            s0 = (min(strikes) + max(strikes)) / 2.0

    dte = strategy.dte
    ivs = [l.iv for l in legs if l.iv and l.iv > 0]
    if ivs:
        iv = sum(ivs) / len(ivs)
    elif s0 and dte:
        inferred = []
        for l in legs:
            if l.delta is not None and l.strike and s0:
                iv_est = _iv_from_delta(l.delta, s0, l.strike, dte,
                                        is_call=(l.call_put == "C"))
                if iv_est:
                    inferred.append(iv_est)
        iv = sum(inferred) / len(inferred) if inferred else iv_fallback
    else:
        iv = iv_fallback

    if s0 is None or dte is None or dte < 0 or iv <= 0:
        return None
    if dte == 0:
        return 100.0 if payoff_at(strategy, s0) > 0 else 0.0

    T = dte / 365.0
    sigma_T = iv * math.sqrt(T)
    mu = math.log(s0) - 0.5 * iv * iv * T

    xs, ys = payoff_range(strategy, steps=800, pad_pct=1.5)
    if not xs:
        return None

    intervals = []
    start = None
    for i, y in enumerate(ys):
        if y > 0 and start is None:
            if i == 0:
                start = xs[0]
            else:
                x0, x1, y0, y1 = xs[i-1], xs[i], ys[i-1], ys[i]
                start = x0 + (-y0) / (y1 - y0) * (x1 - x0)
        elif y <= 0 and start is not None:
            x0, x1, y0, y1 = xs[i-1], xs[i], ys[i-1], ys[i]
            end = x0 + (-y0) / (y1 - y0) * (x1 - x0)
            intervals.append((start, end))
            start = None
    if start is not None:
        intervals.append((start, xs[-1]))

    def lncdf(x):
        return _norm_cdf((math.log(max(x, 1e-9)) - mu) / sigma_T)

    prob = 0.0
    for lo, hi in intervals:
        prob += lncdf(hi) - lncdf(lo)
    if ys[-1] > 0:
        prob += 1.0 - lncdf(xs[-1])
    if ys[0] > 0:
        prob += lncdf(xs[0])

    return max(0.0, min(100.0, prob * 100.0))


# ── Strategy instances (template-based, user-defined) ────────────────────────

import strategies as _strategies_mod


class StrategyInstance(Strategy):
    """A user-created strategy assigned to a template with specific legs."""

    def __init__(self, data, positions):
        by_sym = {p.symbol: p for p in positions}
        self.leg_symbols = list(data.get("legs", []))
        legs = [by_sym[s] for s in self.leg_symbols if s in by_sym]
        super().__init__(
            data["id"], legs,
            custom_name=(data.get("name") or None),
            is_custom=True,
        )
        self._raw           = data   # live reference — mutations persist until save
        self.id             = data["id"]
        self.template_key   = data.get("template") or ""
        self.template       = _strategies_mod.get_template(self.template_key)
        self.notes          = data.get("notes", "")
        self.instance_created_at = _parse_iso(data.get("created_at"))
        self.missing_legs   = [s for s in self.leg_symbols if s not in by_sym]

    @property
    def exit_plan(self):
        return self._raw.get("exit_plan") or {}

    @property
    def name(self):
        if self.custom_name:
            return self.custom_name
        return self.template.name if self.template else self.auto_name

    def expected_leg_count(self):
        return len(self.template.legs) if self.template else len(self.leg_symbols)

    def validation_state(self):
        if self.missing_legs:
            return ("error", f"{len(self.missing_legs)} leg(s) missing from portfolio")
        if self.template and self.template.key == "custom":
            return ("ok", f"{len(self.legs)} leg(s)")
        expected = self.expected_leg_count()
        if len(self.legs) < expected:
            return ("warn", f"{expected - len(self.legs)} leg(s) not assigned yet")
        if len(self.legs) > expected:
            return ("warn", f"{len(self.legs) - expected} extra leg(s) for this template")
        return ("ok", "Complete")


def load_strategy_instances(raw_list, positions):
    return [StrategyInstance(d, positions) for d in (raw_list or [])]


def unassigned_positions(positions, strategies_raw):
    claimed = set()
    for s in (strategies_raw or []):
        for sym in s.get("legs", []):
            claimed.add(sym)
    return [p for p in positions if p.symbol not in claimed]


def group_unassigned(positions):
    """Auto-group unassigned legs by ticker for display."""
    buckets = defaultdict(list)
    for p in positions:
        buckets[f"auto:{p.root}"].append(p)
    out = []
    for gid, legs in buckets.items():
        out.append(Strategy(gid, legs, custom_name=None, is_custom=False))
    out.sort(key=lambda s: (s.dte is None, s.dte if s.dte is not None else 10**9))
    return out


# ── Snapshot / close detection ──────────────────────────────────────────────

def build_snapshot(positions):
    """Return {symbol: snapshot_data} for current positions."""
    snap = {}
    for p in positions:
        snap[p.symbol] = {
            "qty":         p.quantity,
            "sign":        p.sign,
            "open_price":  p.avg_open_price,
            "mark":        p.mark_price,
            "multiplier":  p.multiplier,
            "pnl":         p.pnl,       # live P&L stored so detect_closures can use it
            "instrument":  p.instrument_type,
            "call_put":    p.call_put,
            "strike":      p.strike,
            "expires_at":  p.expires_at.isoformat() if p.expires_at else None,
            "opened_at":   p.created_at.isoformat() if p.created_at else None,
            "root":        p.root,
        }
    return snap


def _tx_dollar_value(t):
    """
    Return the signed dollar value of a transaction.
    Credit = money in (positive), Debit = money out (negative).

    TastyTrade's transactions API does NOT include the contract multiplier for
    most instruments — the `value` and `multiplier` fields are unreliable.
    We always compute: sign × qty × price × _contract_multiplier(root, instr).
    """
    qty    = abs(float(t.get("quantity") or 0))
    price  = float(t.get("price") or 0)
    instr  = t.get("instrument-type") or ""
    effect = (t.get("value-effect") or "").lower()
    sign   = 1 if "credit" in effect else -1

    root_sym = t.get("underlying-symbol") or t.get("symbol") or ""
    root = normalize_root(root_sym) or root_sym.lstrip("/")

    mult = _contract_multiplier(root, instr)
    return sign * qty * price * mult


def transactions_to_closed_lots(transactions):
    """FIFO-pair transactions into closed lots and return history-shape entries."""
    ts = sorted(
        (t for t in transactions
         if t.get("transaction-type") in ("Trade", "Receive Deliver")
         and t.get("symbol")),
        key=lambda t: t.get("executed-at") or ""
    )
    queues = defaultdict(list)   # symbol -> list of open lot dicts
    lots = []
    for t in ts:
        sym    = t.get("symbol") or ""
        action = (t.get("action") or "").replace("_", " ").strip()
        al     = action.lower()
        qty    = abs(float(t.get("quantity") or 0))
        price  = float(t.get("price") or 0)
        when     = t.get("executed-at")
        root_sym = t.get("underlying-symbol") or ""
        root     = normalize_root(root_sym) or root_sym
        cp, k    = parse_option_symbol(sym)
        instr    = t.get("instrument-type") or ""
        mult     = _contract_multiplier(root, instr)   # effective $/pt multiplier
        # Signed dollar value: positive = cash received, negative = cash paid
        dollar_val = _tx_dollar_value(t)

        is_open_action   = "to open" in al
        is_close_action  = (
            "to close" in al or "expir" in al or "assign" in al or "exercise" in al
        )

        if is_open_action:
            side_sign = 1 if al.startswith("buy") else -1
            queues[sym].append({
                "qty": qty, "price": price, "when": when,
                "sign": side_sign, "multiplier": mult,
                "root": root, "call_put": cp, "strike": k, "instrument": instr,
                # dollar_val is negative for buy (debit), positive for sell (credit)
                "dollar_val": dollar_val,
            })
        elif is_close_action:
            close_price = 0.0 if "expir" in al else price
            close_dollar = 0.0 if "expir" in al else dollar_val
            remaining = qty
            open_q = queues.get(sym, [])
            while remaining > 0 and open_q:
                lot = open_q[0]
                take = min(remaining, lot["qty"])
                frac = take / lot["qty"] if lot["qty"] else 1.0
                # P&L = what we got on open + what we got on close
                # Short: open_dollar > 0 (credit), close_dollar < 0 (debit to buy back)
                # Long:  open_dollar < 0 (debit),  close_dollar > 0 (credit on sale)
                pnl = lot["dollar_val"] * frac + close_dollar * (take / qty if qty else 1.0)
                lots.append({
                    "symbol":      sym,
                    "root":        lot["root"],
                    "strategy_id": None,
                    "qty":         take,
                    "sign":        lot["sign"],
                    "open_price":  lot["price"],
                    "close_price": close_price,
                    "multiplier":  lot["multiplier"],
                    "opened_at":   lot["when"],
                    "closed_at":   when,
                    "pnl":         pnl,
                    "source":      "import",
                    "call_put":    lot["call_put"],
                    "strike":      lot["strike"],
                    "instrument":  lot["instrument"],
                })
                lot["qty"] -= take
                lot["dollar_val"] *= (1 - frac)
                remaining -= take
                if lot["qty"] <= 1e-9:
                    open_q.pop(0)
    return lots


def check_exit_conditions(strategy, exit_plan):
    """
    Evaluate a strategy's exit plan against its current live values.

    Returns a list of condition dicts:
      { type, label, target, current, pct_done, severity, message }
      severity: "hit" | "near" | "ok"

    An empty exit_plan returns [].
    """
    if not exit_plan:
        return []

    results  = []
    pnl      = strategy.pnl
    credit   = strategy.credit_debit          # + = credit received, − = debit paid
    ref      = abs(credit) if credit else None # reference amount for % calcs
    dte      = strategy.dte
    underlying = next(
        (l.underlying_price for l in strategy.legs if l.underlying_price), None
    )

    # ── Profit target (% of credit / ref) ────────────────────────────────
    pp = exit_plan.get("profit_pct")
    if pp and ref:
        target  = ref * pp / 100.0
        done    = min(1.0, pnl / target) if target else 0.0
        hit     = pnl >= target
        near    = (not hit) and pnl >= target * 0.85
        results.append({
            "type":     "profit",
            "label":    "Profit Target",
            "target":   target,
            "current":  pnl,
            "pct_done": done,
            "severity": "hit" if hit else ("near" if near else "ok"),
            "message":  f"Profit target {pp:.0f}% of credit reached",
        })

    # ── Stop loss (% of credit) ───────────────────────────────────────────
    sp = exit_plan.get("stop_pct")
    if sp and ref:
        stop   = -(ref * sp / 100.0)
        hit    = pnl <= stop
        near   = (not hit) and pnl <= stop * 0.85
        done   = min(1.0, pnl / stop) if stop else 0.0
        results.append({
            "type":     "stop",
            "label":    "Stop Loss",
            "target":   stop,
            "current":  pnl,
            "pct_done": done,
            "severity": "hit" if hit else ("near" if near else "ok"),
            "message":  f"Stop loss {sp:.0f}% of credit triggered",
        })

    # ── DTE exit ──────────────────────────────────────────────────────────
    de = exit_plan.get("dte_exit")
    if de is not None and dte is not None:
        hit  = dte <= de
        near = (not hit) and dte <= de + 7
        results.append({
            "type":     "dte",
            "label":    "DTE Exit",
            "target":   de,
            "current":  dte,
            "pct_done": None,
            "severity": "hit" if hit else ("near" if near else "ok"),
            "message":  f"DTE target ≤{de}d reached (now {dte}d)",
        })

    # ── Underlying below ──────────────────────────────────────────────────
    ub = exit_plan.get("underlying_below")
    if ub and underlying is not None:
        hit  = underlying <= ub
        near = (not hit) and underlying <= ub * 1.02
        results.append({
            "type":     "below",
            "label":    "Stop Below",
            "target":   ub,
            "current":  underlying,
            "pct_done": None,
            "severity": "hit" if hit else ("near" if near else "ok"),
            "message":  f"Underlying broke below {ub:g} (now {underlying:.4f})",
        })

    # ── Underlying above ──────────────────────────────────────────────────
    ua = exit_plan.get("underlying_above")
    if ua and underlying is not None:
        hit  = underlying >= ua
        near = (not hit) and underlying >= ua * 0.98
        results.append({
            "type":     "above",
            "label":    "Stop Above",
            "target":   ua,
            "current":  underlying,
            "pct_done": None,
            "severity": "hit" if hit else ("near" if near else "ok"),
            "message":  f"Underlying broke above {ua:g} (now {underlying:.4f})",
        })

    return results


def repair_history_pnl(history_all):
    """
    One-time migration: fix imported futures-option history entries whose P&L
    was recorded without the contract multiplier (e.g. /6A × 100,000).

    Detects affected entries by comparing the stored pnl to the expected value
    sign × qty × mult × (close_price − open_price).  If the stored value is
    roughly 1/mult of what it should be, recomputes and flags the entry.

    Mutates history_all in-place.  Returns True if any entries were changed.
    """
    changed = False
    for entries in history_all.values():
        for h in entries:
            if h.get("source") != "import":
                continue
            instr = (h.get("instrument") or "").lower()
            if "future" not in instr:
                continue
            mult = float(h.get("multiplier") or 1)
            if mult <= 1:
                continue
            try:
                sign = int(h.get("sign") or 0)
                qty  = float(h.get("qty") or 0)
                op   = float(h.get("open_price") or 0)
                cp   = float(h.get("close_price") or 0)
                correct_pnl = sign * qty * mult * (cp - op)
                stored_pnl  = float(h.get("pnl") or 0)
                # If stored P&L is ≈ correct_pnl / mult, the multiplier was dropped
                if abs(correct_pnl) > 1 and abs(stored_pnl) < abs(correct_pnl) * 0.1:
                    h["pnl"] = correct_pnl
                    changed = True
            except (TypeError, ValueError):
                continue
    return changed


def repair_pnl_missing_multiplier(history_all):
    """
    Fix imported history entries whose P&L was stored without the contract
    multiplier.  This happens because TastyTrade's transactions API does not
    include the multiplier field — so previously every entry was computed as
    price × qty × 1 instead of price × qty × correct_multiplier.

    Detection: stored P&L ≈ sign × (cp − op) × qty × 1  (i.e. multiplier=1).
    Fix:        stored P&L → sign × (cp − op) × qty × correct_multiplier.

    Mutates history_all in-place.  Returns True if any entries were changed.
    """
    changed = False
    for entries in history_all.values():
        for h in entries:
            if h.get("source") != "import":
                continue
            instr = h.get("instrument") or ""
            root  = h.get("root") or ""
            correct_mult = _contract_multiplier(root, instr)
            if correct_mult <= 1:
                continue   # stocks/unknown — nothing to fix

            stored_mult = float(h.get("multiplier") or 1)
            if stored_mult >= correct_mult * 0.9:
                continue   # already correct

            try:
                sign = int(h.get("sign") or 0)
                qty  = float(h.get("qty") or 0)
                op   = float(h.get("open_price") or 0)
                cp   = float(h.get("close_price") or 0)
                # Verify the stored P&L matches the wrong (mult=1) calculation
                # before overwriting, to avoid double-correcting.
                wrong_pnl   = sign * (cp - op) * qty * 1
                correct_pnl = sign * (cp - op) * qty * correct_mult
                stored_pnl  = float(h.get("pnl") or 0)
                # Only fix if stored P&L is close to the wrong value
                if abs(wrong_pnl) < 1e-9:
                    continue
                ratio = stored_pnl / wrong_pnl
                if not (0.8 < ratio < 1.2):
                    continue   # doesn't match the "missing multiplier" pattern
                h["pnl"]        = correct_pnl
                h["multiplier"] = correct_mult
                changed = True
            except (TypeError, ValueError, ZeroDivisionError):
                continue
    return changed


def _history_key(entry):
    """Dedupe key — symbol + closed_at (day) + open_price + close_price."""
    closed = (entry.get("closed_at") or "")[:10]
    return (
        entry.get("symbol"),
        closed,
        round(float(entry.get("open_price") or 0), 4),
        round(float(entry.get("close_price") or 0), 4),
    )


def merge_history(existing, new_entries):
    """Add new entries that don't collide with existing by _history_key. Returns count added."""
    seen = {_history_key(e) for e in (existing or [])}
    added = 0
    for e in new_entries:
        k = _history_key(e)
        if k in seen:
            continue
        existing.append(e)
        seen.add(k)
        added += 1
    return added


def detect_closures(prev_snap, current_symbols, strategy_map, now_iso):
    """
    Compare previous snapshot to currently-held symbols.
    Returns list of history entries for legs that disappeared.
    """
    closures = []
    for sym, d in (prev_snap or {}).items():
        if sym in current_symbols:
            continue
        qty   = d.get("qty") or 0
        sign  = d.get("sign") or 0
        op    = d.get("open_price") or 0
        mark  = d.get("mark") or 0
        mult  = d.get("multiplier") or 1
        # Prefer the live P&L stored in the snapshot (handles futures multiplier
        # correctly via position.pnl); fall back to price-based calc for old snapshots.
        if "pnl" in d:
            pnl = d["pnl"]
        else:
            notional_open  = sign * qty * mult * op
            notional_close = sign * qty * mult * mark
            pnl = notional_close - notional_open
        closures.append({
            "symbol":       sym,
            "root":         d.get("root"),
            "strategy_id":  strategy_map.get(sym),
            "qty":          qty,
            "sign":         sign,
            "open_price":   op,
            "close_price":  mark,
            "multiplier":   mult,
            "opened_at":    d.get("opened_at"),
            "closed_at":    now_iso,
            "pnl":          pnl,
            "source":       "auto",
            "call_put":     d.get("call_put"),
            "strike":       d.get("strike"),
            "instrument":   d.get("instrument"),
        })
    return closures


# ── Performance aggregation from history ─────────────────────────────────────

def _parse_any_iso(s):
    return _parse_iso(s)


def strategy_performance(strategy_id, history, capital_req=None):
    """
    Aggregate closed-leg stats for a strategy.
    Returns dict with total_pnl, avg_weekly, avg_monthly, yearly, closed_legs,
    avg_dit, win_rate.
    """
    entries = [h for h in (history or []) if h.get("strategy_id") == strategy_id]
    if not entries:
        return None

    total_pnl = sum(h.get("pnl", 0.0) or 0.0 for h in entries)

    dits = []
    for h in entries:
        op = _parse_any_iso(h.get("opened_at"))
        cl = _parse_any_iso(h.get("closed_at"))
        if op and cl:
            dits.append(max(0, (cl.date() - op.date()).days))
    avg_dit = (sum(dits) / len(dits)) if dits else None

    wins = sum(1 for h in entries if (h.get("pnl") or 0) > 0)
    win_rate = (wins / len(entries) * 100.0)

    # Span from first close to now
    closes = [_parse_any_iso(h.get("closed_at")) for h in entries]
    closes = [c for c in closes if c]
    span_days = None
    if closes:
        span_days = max(1, (datetime.now(timezone.utc).date() - min(closes).date()).days)

    avg_weekly  = (total_pnl / span_days * 7)  if span_days else None
    avg_monthly = (total_pnl / span_days * 30) if span_days else None
    yearly      = (total_pnl / span_days * 365) if span_days else None

    capital_pct = None
    if capital_req and avg_weekly is not None:
        capital_pct = avg_weekly / capital_req * 100.0

    return {
        "total_pnl":   total_pnl,
        "avg_weekly":  avg_weekly,
        "avg_monthly": avg_monthly,
        "yearly":      yearly,
        "closed_legs": len(entries),
        "avg_dit":     avg_dit,
        "win_rate":    win_rate,
        "weekly_pct":  capital_pct,
    }


# ── Portfolio-level analytics ───────────────────────────────────────────────

def _metric_float(metrics_for_symbol, *keys):
    """Pull the first present float from several possible API field names."""
    if not metrics_for_symbol:
        return None
    for k in keys:
        v = metrics_for_symbol.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def symbol_ivr(metrics_for_symbol):
    """Implied-Volatility Rank as 0–100, or None."""
    v = _metric_float(
        metrics_for_symbol,
        "implied-volatility-index-rank",
        "iv-rank",
    )
    if v is None:
        return None
    return v * 100 if v <= 1 else v


def symbol_ivp(metrics_for_symbol):
    """Implied-Volatility Percentile as 0–100, or None."""
    v = _metric_float(
        metrics_for_symbol,
        "implied-volatility-percentile",
        "iv-percentile",
    )
    if v is None:
        return None
    return v * 100 if v <= 1 else v


def symbol_beta(metrics_for_symbol):
    """Beta vs. SPY (1.0 default), or None if unknown."""
    return _metric_float(metrics_for_symbol, "beta")


def symbol_hv30(metrics_for_symbol):
    v = _metric_float(
        metrics_for_symbol,
        "historical-volatility-30-day",
        "hv-30",
    )
    if v is None:
        return None
    return v * 100 if v <= 1 else v


def portfolio_greeks(positions, metrics_by_root=None):
    """
    Aggregate net Δ/Γ/Θ/V across positions.
    Returns dict with net_delta/gamma/theta/vega and, if metrics provided,
    beta_weighted_delta (SPY-equivalent delta).
    """
    net_delta = 0.0
    net_gamma = 0.0
    net_theta = 0.0
    net_vega  = 0.0
    bw_delta  = 0.0
    have_beta = False

    metrics_by_root = metrics_by_root or {}

    for p in positions:
        if p.is_option:
            sign = p.sign  # +1 long / -1 short
            # Equity options: greeks are per-share; 100 shares per contract.
            # Futures options: greeks are already per-contract; no extra multiplier.
            mult = 100 if not _is_future_option(p.instrument_type) else 1
            d = _to_float(p.delta) * p.quantity * mult * sign
            g = _to_float(p.gamma) * p.quantity * mult * sign
            t = _to_float(p.theta) * p.quantity * mult * sign
            v = _to_float(p.vega)  * p.quantity * mult * sign
        elif p.is_future:
            # Pure futures contract: linear payoff, delta = 1 per $1 move in
            # underlying × contract multiplier.  No gamma/theta/vega.
            d = p.sign * p.quantity * (p.multiplier or 1)
            g = t = v = 0.0
        elif p.instrument_type == "Equity":
            # Stock: delta = 1 per share.
            d = p.sign * p.quantity
            g = t = v = 0.0
        else:
            continue

        net_delta += d
        net_gamma += g
        net_theta += t
        net_vega  += v

        beta = symbol_beta(metrics_by_root.get(p.root))
        if beta is not None:
            have_beta = True
            bw_delta += d * beta

    return {
        "net_delta":  net_delta,
        "net_gamma":  net_gamma,
        "net_theta":  net_theta,
        "net_vega":   net_vega,
        "beta_weighted_delta": bw_delta if have_beta else None,
    }


# ── SPAN scan-range table ────────────────────────────────────────────────────
# Approximate per-contract initial margin (USD) for common futures products.
# SPAN margin is exchange-set and NOT proportional to notional — currency futures
# can have ~$800 margin on $79K notional (1 %), while equity-index futures have
# ~$12K on $260K notional (5 %).  Using these product-specific values gives far
# more accurate distribution than any notional-based formula.
# Values are approximate CME/ICE typical initial margins; update as needed.

_FUTURES_SPAN = {
    # Currency futures (CME)
    "6A": 900,   "6B": 1800,  "6C": 650,   "6E": 1300,
    "6J": 1100,  "6M": 1300,  "6N": 650,   "6S": 1600,
    "6Z": 1200,  "DX": 1200,
    # Equity index (CME/CBOT)
    "ES":  12000, "NQ": 15000, "RTY": 6000, "YM": 5000,
    "MES": 1200,  "MNQ": 1500, "M2K": 600,  "MYM": 500,
    "EMD": 5000,  "VX":  3500,
    # Interest rates (CBOT/CME)
    "ZB": 2000, "ZN": 1100, "ZF": 650, "ZT": 400,
    "GE": 400,  "SR1": 250, "SR3": 300, "ZQ": 150,
    "UB": 3000,
    # Energy (NYMEX)
    "CL": 4000, "NG": 2200, "RB": 2200, "HO": 2200,
    "QM": 2000, "MCL": 400,
    # Metals (COMEX/NYMEX)
    "GC": 6500, "SI": 3500, "HG": 3000, "PL": 1800, "PA": 5000,
    "MGC": 650, "SIL": 2000,
    # Agriculture (CBOT/CME/ICE)
    "ZC": 1000, "ZS": 1600, "ZW": 1100, "ZL": 600,  "ZM": 900,
    "KC": 2500, "CC": 900,  "CT": 1400, "SB": 500,
    "OJ": 1000,
    # Livestock (CME)
    "LE": 1500, "HE": 1500, "GF": 1000,
    # Crypto (CME)
    "BTC": 11000, "MBT": 1100, "ETH": 4500, "MET": 450,
}

_SPAN_FALLBACK_PCT = 0.015   # 1.5 % of notional for unknown products


def _is_future_option(instr_type):
    """
    Return True if instr_type represents a futures option.
    Handles any capitalisation / formatting TastyTrade may return
    ("Future Option", "future-option", "FutureOption", etc.).
    """
    il = (instr_type or "").lower().replace("-", " ").replace("_", " ")
    return "future" in il and "option" in il


# ── Contract dollar multipliers ──────────────────────────────────────────────
# Dollar value of ONE UNIT of quoted option price × one contract.
# TastyTrade's transactions API does NOT include this; we look it up by root.
#   Equity options: always 100 (hardcoded, not in this table).
#   Futures options: product-specific below.
# Equity-index (CME): price in index points
# Currency (CME): price in USD per foreign-currency unit × units per contract
# Energy (NYMEX): price in $/unit × units per contract
# Metals (COMEX): price in $/unit × units per contract
# Rates (CBOT): price in %/100 of face × face_per_contract → expressed as $/1%

_CONTRACT_MULT = {
    # Equity index (CME)
    "ES": 50,   "MES": 5,   "NQ": 20,   "MNQ": 2,
    "RTY": 50,  "M2K": 5,   "YM": 5,    "MYM": 0.5,
    "EMD": 100, "VX": 1000,
    # Currency (CME) — units per contract × USD/unit already in price
    "6A": 100000, "6B": 62500,  "6C": 100000, "6E": 125000,
    "6J": 12500000, "6M": 500000, "6N": 100000, "6S": 125000,
    "6Z": 500000,   "DX": 1000,
    # Energy (NYMEX)
    "CL": 1000, "MCL": 100, "NG": 10000, "RB": 42000, "HO": 42000, "QM": 500,
    # Metals (COMEX/NYMEX)
    "GC": 100, "MGC": 10,  "SI": 5000, "SIL": 1000,
    "HG": 25000, "PL": 50, "PA": 100,
    # Interest rates (CBOT/CME)  price = decimal points; 1 pt = $1,000
    "ZB": 1000, "UB": 1000, "ZN": 1000, "ZF": 1000, "ZT": 2000,
    "ZQ": 4167,
    # Agriculture (CBOT/CME/ICE)
    "ZC": 50,  "ZS": 50,  "ZW": 50,  "ZL": 600, "ZM": 100,
    "KC": 375, "CC": 10,  "CT": 500, "SB": 1120, "OJ": 150,
    # Livestock (CME)
    "LE": 400, "HE": 400, "GF": 500,
    # Crypto (CME)
    "BTC": 5, "MBT": 0.1, "ETH": 50, "MET": 0.1,
}


def _contract_multiplier(root, instr):
    """
    Return the dollar value of 1.0 of quoted price for one contract.
    Equity options → always 100.
    Futures / futures options → look up by root, default 1 if unknown.
    """
    il = (instr or "").lower()
    if "equity option" in il or il == "equity-option":
        return 100.0
    if "future" in il:
        return float(_CONTRACT_MULT.get(root or "", 1))
    return 1.0


def _span_per_contract(leg):
    """
    Return the approximate SPAN initial margin per contract for a futures-option leg.
    Uses the product table first; falls back to 1.5 % of underlying notional.
    """
    root = leg.root or ""
    val = _FUTURES_SPAN.get(root)
    if val:
        return float(val)
    # Fallback: 1.5 % of notional
    ref = leg.underlying_price or leg.strike or 0
    return ref * (leg.multiplier or 1) * _SPAN_FALLBACK_PCT


def _notional_capital(strategy):
    """
    Margin estimate for undefined-risk strategies.

    Futures options  → SPAN scan-range table (per product) × qty
    Equity options   → strike × 100 × 20% × qty  (Reg-T naked rule)
    Stock positions  → abs(market_value)

    For strangles / straddles we take the LARGEST short-leg margin block
    (not a sum) because brokers net the two sides.
    Long legs add their cost basis on top.
    """
    best = 0.0
    long_cost = 0.0

    for leg in strategy.legs:
        qty = leg.quantity

        if not leg.is_option:
            best += abs(leg.market_value)
            continue

        if leg.is_long:
            long_cost += leg.cost_basis
            continue

        # --- short option ---
        if _is_future_option(leg.instrument_type):
            cap = _span_per_contract(leg) * qty
        else:
            # Naked equity option: ~20 % of strike × 100 shares
            cap = (leg.strike or 0) * min(leg.multiplier or 100, 100) * 0.20 * qty

        best = max(best, cap)

    return best + long_cost


def _capital_for(strategy):
    _, ml, _ = strategy_extremes(strategy)
    if ml is not None and ml != float("-inf"):
        return abs(ml)
    return _notional_capital(strategy)


def capital_for_strategy(strategy):
    """Public wrapper around _capital_for."""
    return _capital_for(strategy)


def distribute_futures_margin(strategies, unassigned_groups, total_futures_margin):
    """
    Distribute the account's total futures-margin-requirement (from the balances API)
    proportionally across strategies that contain Future Option legs.

    Weighting uses the SPAN scan-range table (_FUTURES_SPAN) rather than raw
    notional so that low-notional / low-volatility currency futures (e.g. /6A)
    aren't drowned out by high-notional equity-index futures (e.g. /ES).

    For multi-leg strategies (strangles, condors) we take the LARGEST single
    short-leg weight — matching how brokers typically calculate SPAN margin for
    short option combinations.

    Returns {strategy.key: allocated_margin_float}.
    Returns an empty dict if there are no futures-option legs or the total is ≤ 0.
    """
    if not total_futures_margin or total_futures_margin <= 0:
        return {}

    weights = {}
    for s in list(strategies) + list(unassigned_groups):
        key = s.key
        # For each strategy use the LARGEST short-leg SPAN estimate
        # (plus any long-leg cost — longs reduce net margin at the portfolio level
        #  but we keep them out of the weight for simplicity)
        max_short = 0.0
        for leg in s.legs:
            if not _is_future_option(leg.instrument_type):
                continue
            if leg.is_long:
                continue
            w = _span_per_contract(leg) * leg.quantity
            if w > max_short:
                max_short = w
        if max_short > 0:
            weights[key] = max_short

    total_w = sum(weights.values())
    if total_w <= 0:
        return {}

    return {k: total_futures_margin * (w / total_w) for k, w in weights.items()}


def distributed_delta_dte_capital(strategy, all_strategies, all_unassigned,
                                   futures_margin_total=0.0):
    """
    Estimate this strategy's capital requirement by distributing the total
    per-ticker capital pool proportionally, weighted by each strategy's
    delta × DTE risk profile.

    Algorithm
    ---------
    1. Find every strategy (instances + unassigned) that shares this
       strategy's root ticker.
    2. Compute the total capital pool for that root:
         pool = Σ _notional_capital(s)  for every strategy on the same root
       For futures-option roots: if futures_margin_total > 0 (the actual
       number from the balances API), scale the pool so the whole account's
       futures margin is distributed correctly.
    3. Each strategy's weight = largest (|delta| × DTE × qty) across its
       short option legs.  Defaults when live Greeks aren't available:
         delta = 0.30  (representative 30-delta short option)
         DTE   = 45    (typical entry DTE)
    4. This strategy's share = pool × (my_weight / Σ all_weights).

    Single-strategy root:  my_weight / total_w = 1 → gets the full pool,
    which equals _notional_capital(strategy).  Identical to the old path.

    Returns float or None.
    """
    root      = strategy.root or ""
    all_strats = list(all_strategies) + list(all_unassigned)

    # ── 1. Strategies on the same root ──────────────────────────────────────
    same_root = [s for s in all_strats if (s.root or "") == root]
    if not same_root:
        return None

    # ── 2. Total capital pool for this root ─────────────────────────────────
    has_fut = any(
        _is_future_option(leg.instrument_type)
        for s in same_root for leg in s.legs
    )

    root_pool = sum(_notional_capital(s) for s in same_root)

    if has_fut and futures_margin_total > 0 and root_pool > 0:
        # Scale the pool so it reflects the broker's actual reported margin.
        # We don't know per-product breakdowns, so attribute futures margin
        # proportionally across roots by their SPAN weight.
        all_fut_strats = [
            s for s in all_strats
            if any(_is_future_option(leg.instrument_type) for leg in s.legs)
        ]
        account_span = sum(_notional_capital(s) for s in all_fut_strats)
        if account_span > 0:
            root_pool = futures_margin_total * (root_pool / account_span)

    if root_pool <= 0:
        return None

    # ── 3. Delta × DTE weight per strategy ──────────────────────────────────
    def _weight(s):
        best = 0.0
        for leg in s.legs:
            if not leg.is_option or leg.is_long:
                continue
            delta = abs(leg.delta) if leg.delta is not None else 0.30
            dte   = max(leg.dte or 45, 1)
            w = delta * dte * leg.quantity
            if w > best:
                best = w
        return best

    weights   = {s.key: _weight(s) for s in same_root}
    total_w   = sum(weights.values())
    my_weight = weights.get(strategy.key, 0.0)

    # ── 4. Proportional allocation ───────────────────────────────────────────
    if total_w <= 0:
        # No Greeks at all → equal split
        return root_pool / len(same_root)
    if my_weight <= 0:
        # This strategy has no short options (all-long, stock-only, etc.)
        return None

    return root_pool * (my_weight / total_w)


def strategy_allocation(instances, unassigned_groups, overrides_by_id=None):
    """
    Like capital_allocation but returns one row per strategy (not per ticker).
    Each row: {id, name, root, capital, pct, pnl}.
    """
    overrides_by_id = overrides_by_id or {}
    rows = []
    for inst in instances:
        cap = overrides_by_id.get(inst.id)
        if cap is None:
            cap = _capital_for(inst)
        rows.append({
            "id":      inst.id,
            "name":    inst.name,
            "root":    inst.root or "—",
            "capital": cap,
            "pct":     0.0,
            "pnl":     inst.pnl,
        })
    for g in unassigned_groups:
        rows.append({
            "id":      g.key,   # Strategy uses .key; only StrategyInstance has .id
            "name":    g.auto_name,
            "root":    g.root or "—",
            "capital": _capital_for(g),
            "pct":     0.0,
            "pnl":     g.pnl,
        })
    total = sum(r["capital"] for r in rows)
    for r in rows:
        r["pct"] = r["capital"] / total * 100 if total else 0.0
    rows.sort(key=lambda x: x["capital"], reverse=True)
    return rows, total


def capital_allocation(instances, unassigned_groups, overrides_by_id=None):
    """
    Return a list of {"root": str, "capital": float, "pct": float}
    sorted by capital desc. Uses max-loss when defined; otherwise a
    notional estimate (short-strike notional + long option cost).
    """
    overrides_by_id = overrides_by_id or {}
    buckets = defaultdict(float)

    for inst in instances:
        cap = overrides_by_id.get(inst.id)
        if cap is None:
            cap = _capital_for(inst)
        buckets[inst.root or "—"] += cap

    for g in unassigned_groups:
        buckets[g.root or "—"] += _capital_for(g)

    total = sum(buckets.values())
    rows = [
        {"root": r, "capital": c, "pct": (c / total * 100 if total else 0)}
        for r, c in buckets.items()
    ]
    rows.sort(key=lambda x: x["capital"], reverse=True)
    return rows, total


# ── What-if scenario ────────────────────────────────────────────────────────

def scenario_pnl(strategy, price_pct=0.0, iv_pct=0.0, days_forward=0):
    """
    Estimate strategy P&L under a hypothetical move using first-order Greeks.
      ΔS = current_price * price_pct/100
      P&L ≈ Σ legs of (Δ*ΔS + 0.5*Γ*ΔS² + V*ΔIV + Θ*days) * qty * mult * sign
    Returns dict with pnl and shocked greeks.
    """
    pnl = 0.0
    new_delta = 0.0
    new_theta = 0.0
    new_vega  = 0.0

    S = next((l.underlying_price for l in strategy.legs if l.underlying_price), None)
    if S is None:
        # No live underlying price — use first strike as crude anchor
        S = next((l.strike for l in strategy.legs if l.strike), 100.0)

    dS = S * (price_pct / 100.0)

    for leg in strategy.legs:
        if not leg.is_option:
            continue
        mult = leg.multiplier or 100
        sign = leg.sign
        qty  = leg.quantity
        d = _to_float(leg.delta)
        g = _to_float(leg.gamma)
        t = _to_float(leg.theta)
        v = _to_float(leg.vega)

        leg_pnl = (d * dS + 0.5 * g * dS * dS
                   + v * iv_pct           # vega is per 1 vol-point
                   + t * days_forward)    # theta is per day
        pnl += leg_pnl * qty * mult * sign

        # Shocked Greeks — use the same qty×100×sign convention as Strategy._agg
        # so the values are consistent with the main Greeks card display.
        # (P&L above uses the real multiplier because it's in dollars.)
        shocked_d = d + g * dS
        new_delta += shocked_d * qty * 100 * sign
        new_theta += t         * qty * 100 * sign
        new_vega  += v         * qty * 100 * sign

    # First-order extrapolation can overshoot the theoretical bounds of the
    # payoff diagram. Clamp so a short option can't "profit" beyond the credit
    # received, etc.
    mp, ml, _ = strategy_extremes(strategy)
    if mp is not None and mp != float("inf"):
        pnl = min(pnl, mp)
    if ml is not None and ml != float("-inf"):
        pnl = max(pnl, ml)

    return {
        "pnl":       pnl,
        "net_delta": new_delta,
        "net_theta": new_theta,
        "net_vega":  new_vega,
    }
