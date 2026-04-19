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

        # Monetary (cost basis / market value always positive)
        notional_open = self.quantity * self.multiplier * self.avg_open_price
        notional_mark = self.quantity * self.multiplier * self.mark_price
        self.cost_basis   = notional_open
        self.market_value = notional_mark
        self.pnl          = self.sign * (notional_mark - notional_open)
        self.credit_debit = -self.sign * notional_open  # +credit, -debit
        self.pnl_pct      = (self.pnl / notional_open * 100.0) if notional_open else 0.0

        # Filled in later from market-data endpoint (optional)
        self.delta = None
        self.gamma = None
        self.theta = None
        self.vega  = None
        self.iv    = None
        self.underlying_price = None

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

        self.delta = f("delta")
        self.gamma = f("gamma")
        self.theta = f("theta")
        self.vega  = f("vega")
        self.iv    = f("implied-volatility")
        self.underlying_price = f("underlying-price")

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
            return f"{l.direction_label} {l.type_label}" if l.is_option else f"{l.direction_label} Shares"
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


def probability_of_profit(strategy, iv_fallback=0.30):
    """
    Estimate P(strategy P&L > 0 at expiration) as a percentage, or None if
    we lack the inputs. Uses a log-normal model for the underlying at expiry.
    """
    legs = [l for l in strategy.legs if l.is_option]
    if not legs:
        return None

    # Underlying price from any leg's quote
    s0 = next((l.underlying_price for l in legs if l.underlying_price), None)
    # Fallback 1: infer from ATM leg (smallest |delta - 0.5| gives strike closest to spot)
    if s0 is None:
        best = None
        for l in legs:
            if l.strike and l.delta is not None:
                score = abs(abs(l.delta) - 0.5)
                if best is None or score < best[0]:
                    best = (score, l.strike)
        if best:
            s0 = best[1]
    # Fallback 2: midpoint of strikes
    if s0 is None:
        strikes = [l.strike for l in legs if l.strike]
        if strikes:
            s0 = (min(strikes) + max(strikes)) / 2.0

    # Volatility: average IV of legs that have one; else fallback
    ivs = [l.iv for l in legs if l.iv]
    iv = (sum(ivs) / len(ivs)) if ivs else iv_fallback

    dte = strategy.dte

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

    # Find profitable intervals with linear interpolation at boundaries
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

    # Unbounded profit tails (payoff still > 0 at extremes of our sampled range)
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
        self.id             = data["id"]
        self.template_key   = data.get("template") or ""
        self.template       = _strategies_mod.get_template(self.template_key)
        self.notes          = data.get("notes", "")
        self.instance_created_at = _parse_iso(data.get("created_at"))
        self.missing_legs   = [s for s in self.leg_symbols if s not in by_sym]

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
            "instrument":  p.instrument_type,
            "call_put":    p.call_put,
            "strike":      p.strike,
            "expires_at":  p.expires_at.isoformat() if p.expires_at else None,
            "opened_at":   p.created_at.isoformat() if p.created_at else None,
            "root":        p.root,
        }
    return snap


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
        mult   = float(t.get("multiplier") or 1) or 1
        when   = t.get("executed-at")
        root_sym = t.get("underlying-symbol") or ""
        root   = normalize_root(root_sym) or root_sym
        cp, k  = parse_option_symbol(sym)
        instr  = t.get("instrument-type") or ""

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
            })
        elif is_close_action:
            close_price = 0.0 if "expir" in al else price
            remaining = qty
            open_q = queues.get(sym, [])
            while remaining > 0 and open_q:
                lot = open_q[0]
                take = min(remaining, lot["qty"])
                pnl = lot["sign"] * take * lot["multiplier"] * (close_price - lot["price"])
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
                remaining -= take
                if lot["qty"] <= 1e-9:
                    open_q.pop(0)
    return lots


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
        if not p.is_option:
            continue
        sign = p.sign  # +1 long / -1 short
        d = _to_float(p.delta) * p.quantity * 100 * sign
        g = _to_float(p.gamma) * p.quantity * 100 * sign
        t = _to_float(p.theta) * p.quantity * 100 * sign
        v = _to_float(p.vega)  * p.quantity * 100 * sign
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


def _notional_capital(strategy):
    """Fallback capital estimate: short-option strike notional + long-option cost."""
    total = 0.0
    for leg in strategy.legs:
        mult = leg.multiplier or 1
        qty  = leg.quantity
        if leg.is_option:
            if leg.is_long:
                total += leg.cost_basis
            else:
                total += (leg.strike or 0) * qty * mult
        else:
            total += abs(leg.market_value)
    return total


def _capital_for(strategy):
    _, ml, _ = strategy_extremes(strategy)
    if ml is not None and ml != float("-inf"):
        return abs(ml)
    return _notional_capital(strategy)


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
            "id":      g.id,
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

        # Rough shocked greeks (delta approx: d + gamma*dS; vega & theta decay linearly)
        shocked_d = d + g * dS
        new_delta += shocked_d * qty * mult * sign
        new_theta += t * qty * mult * sign
        new_vega  += v * qty * mult * sign

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
