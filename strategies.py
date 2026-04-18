"""Option-strategy template catalog (15 common strategies incl. PMCC)."""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class LegSpec:
    label: str                         # e.g. "Long Call @ lower strike"
    side: str                          # "long" | "short"
    option_type: str                   # "C" | "P" | "STOCK"
    strike_rank: Optional[int] = None  # 1 = lowest strike, ascending
    expiry_rank: Optional[int] = None  # 1 = nearest expiry, 2 = farther
    qty: int = 1


@dataclass
class Template:
    key: str
    name: str
    category: str         # "Directional" | "Income" | "Volatility" | "Spread"
    outlook: str          # "Bullish" | "Bearish" | "Neutral" | "Volatile"
    risk: str             # "Defined" | "Undefined"
    description: str
    setup: str            # one-line composition summary
    max_profit: str
    max_loss: str
    capital_note: str
    ideal_when: str
    legs: List[LegSpec]
    # which metrics matter for this template (subset of ALL_METRIC_KEYS)
    metrics: List[str] = field(default_factory=list)


ALL_METRIC_KEYS = [
    "max_profit", "max_loss", "break_even", "pop",
    "capital_req", "delta", "gamma", "theta", "vega",
    "iv", "dte", "dit",
]


TEMPLATES: List[Template] = [
    Template(
        key="custom",
        name="Custom Strategy",
        category="Custom",
        outlook="Any",
        risk="Custom",
        description="Freeform strategy with any combination of legs you choose. "
                    "Use this when your structure doesn't fit the preset templates.",
        setup="Any legs",
        max_profit="Depends on structure",
        max_loss="Depends on structure",
        capital_note="Depends on structure",
        ideal_when="Non-standard structures, calendars-of-calendars, ratios, etc.",
        legs=[],
        metrics=["max_profit", "max_loss", "break_even", "pop",
                 "capital_req", "delta", "theta", "vega", "dte"],
    ),
    Template(
        key="long_call",
        name="Long Call",
        category="Directional",
        outlook="Bullish",
        risk="Defined",
        description="Buy a call to profit from a rise in the underlying. Limited risk, unlimited upside.",
        setup="Buy 1 Call",
        max_profit="Unlimited",
        max_loss="Premium paid",
        capital_note="Debit paid = max loss. No margin required.",
        ideal_when="Strong bullish conviction with IV relatively low and enough time.",
        legs=[LegSpec("Long Call", "long", "C", strike_rank=1)],
        metrics=["max_loss", "break_even", "delta", "theta", "iv", "dte"],
    ),
    Template(
        key="long_put",
        name="Long Put",
        category="Directional",
        outlook="Bearish",
        risk="Defined",
        description="Buy a put to profit from a drop in the underlying. Limited risk.",
        setup="Buy 1 Put",
        max_profit="Strike − premium (underlying to 0)",
        max_loss="Premium paid",
        capital_note="Debit paid = max loss.",
        ideal_when="Bearish conviction with IV relatively low and enough time.",
        legs=[LegSpec("Long Put", "long", "P", strike_rank=1)],
        metrics=["max_profit", "max_loss", "break_even", "delta", "theta", "iv", "dte"],
    ),
    Template(
        key="covered_call",
        name="Covered Call",
        category="Income",
        outlook="Neutral-to-Bullish",
        risk="Defined (downside = stock)",
        description="Own 100 shares and sell a call against them to earn premium.",
        setup="Long 100 Stock + Short 1 Call (above spot)",
        max_profit="(Strike − cost basis) + premium",
        max_loss="Stock to 0 − premium",
        capital_note="Requires 100 shares (or ~full share cost).",
        ideal_when="Own the stock, neutral-to-mildly bullish, want to generate yield.",
        legs=[
            LegSpec("Long 100 Shares", "long", "STOCK", qty=100),
            LegSpec("Short Call", "short", "C", strike_rank=1),
        ],
        metrics=["max_profit", "break_even", "theta", "delta", "iv", "dte"],
    ),
    Template(
        key="csp",
        name="Cash-Secured Put",
        category="Income",
        outlook="Neutral-to-Bullish",
        risk="Defined (assigned stock)",
        description="Sell a put with cash set aside to buy the shares if assigned.",
        setup="Short 1 Put (cash-backed)",
        max_profit="Premium received",
        max_loss="(Strike − premium) if assigned and stock goes to 0",
        capital_note="Capital = strike × 100 − premium.",
        ideal_when="Willing to own the stock at the short strike for a discount.",
        legs=[LegSpec("Short Put", "short", "P", strike_rank=1)],
        metrics=["max_profit", "break_even", "pop", "theta", "delta", "iv", "dte"],
    ),
    Template(
        key="pmcc",
        name="Poor Man's Covered Call",
        category="Income",
        outlook="Neutral-to-Bullish",
        risk="Defined",
        description=(
            "Long-dated deep-ITM call acts as a stock substitute while a short-dated "
            "OTM call is sold against it for income. Lower capital than covered call."
        ),
        setup="Long LEAPS/long-dated ITM Call + Short near-dated OTM Call",
        max_profit="(Short strike − long strike) − net debit",
        max_loss="Net debit paid",
        capital_note="Net debit. Typically ≪ 100 shares of stock.",
        ideal_when="Moderately bullish, want covered-call-like income without tying up share capital.",
        legs=[
            LegSpec("Long ITM Call (back-month)", "long", "C",
                    strike_rank=1, expiry_rank=2),
            LegSpec("Short OTM Call (front-month)", "short", "C",
                    strike_rank=2, expiry_rank=1),
        ],
        metrics=["max_profit", "max_loss", "break_even",
                 "delta", "theta", "iv", "dte", "dit"],
    ),
    Template(
        key="bull_call_spread",
        name="Bull Call Spread",
        category="Spread",
        outlook="Bullish",
        risk="Defined",
        description="Debit vertical: buy a call, sell a higher-strike call (same expiry).",
        setup="Long Call (lower K) + Short Call (higher K)",
        max_profit="Width − net debit",
        max_loss="Net debit",
        capital_note="Net debit paid.",
        ideal_when="Bullish but want to cap cost and risk.",
        legs=[
            LegSpec("Long Call (low K)",  "long",  "C", strike_rank=1),
            LegSpec("Short Call (high K)","short", "C", strike_rank=2),
        ],
        metrics=["max_profit", "max_loss", "break_even", "pop", "delta", "theta", "dte"],
    ),
    Template(
        key="bear_put_spread",
        name="Bear Put Spread",
        category="Spread",
        outlook="Bearish",
        risk="Defined",
        description="Debit vertical: buy a put, sell a lower-strike put (same expiry).",
        setup="Long Put (higher K) + Short Put (lower K)",
        max_profit="Width − net debit",
        max_loss="Net debit",
        capital_note="Net debit paid.",
        ideal_when="Bearish but want to cap cost.",
        legs=[
            LegSpec("Short Put (low K)",  "short", "P", strike_rank=1),
            LegSpec("Long Put (high K)",  "long",  "P", strike_rank=2),
        ],
        metrics=["max_profit", "max_loss", "break_even", "pop", "delta", "theta", "dte"],
    ),
    Template(
        key="bull_put_spread",
        name="Bull Put Spread",
        category="Spread",
        outlook="Bullish",
        risk="Defined",
        description="Credit vertical: sell a put, buy a lower-strike put (same expiry).",
        setup="Short Put (higher K) + Long Put (lower K)",
        max_profit="Net credit",
        max_loss="Width − net credit",
        capital_note="Capital ≈ width − credit.",
        ideal_when="Neutral-to-bullish, high IV, defined risk premium seller.",
        legs=[
            LegSpec("Long Put (low K)",  "long",  "P", strike_rank=1),
            LegSpec("Short Put (high K)","short", "P", strike_rank=2),
        ],
        metrics=["max_profit", "max_loss", "break_even", "pop", "capital_req",
                 "delta", "theta", "iv", "dte"],
    ),
    Template(
        key="bear_call_spread",
        name="Bear Call Spread",
        category="Spread",
        outlook="Bearish",
        risk="Defined",
        description="Credit vertical: sell a call, buy a higher-strike call (same expiry).",
        setup="Short Call (lower K) + Long Call (higher K)",
        max_profit="Net credit",
        max_loss="Width − net credit",
        capital_note="Capital ≈ width − credit.",
        ideal_when="Neutral-to-bearish, high IV, defined risk premium seller.",
        legs=[
            LegSpec("Short Call (low K)", "short", "C", strike_rank=1),
            LegSpec("Long Call (high K)", "long",  "C", strike_rank=2),
        ],
        metrics=["max_profit", "max_loss", "break_even", "pop", "capital_req",
                 "delta", "theta", "iv", "dte"],
    ),
    Template(
        key="long_strangle",
        name="Long Strangle",
        category="Volatility",
        outlook="Volatile",
        risk="Defined",
        description="Buy OTM call and OTM put (same expiry) to profit from a big move either way.",
        setup="Long Put (low K) + Long Call (high K)",
        max_profit="Unlimited (large up move) / substantial (large down move)",
        max_loss="Net debit",
        capital_note="Net debit paid.",
        ideal_when="Expect a big move, IV low relative to expected realized vol.",
        legs=[
            LegSpec("Long Put",  "long", "P", strike_rank=1),
            LegSpec("Long Call", "long", "C", strike_rank=2),
        ],
        metrics=["max_loss", "break_even", "vega", "theta", "iv", "dte"],
    ),
    Template(
        key="short_strangle",
        name="Short Strangle",
        category="Volatility",
        outlook="Neutral",
        risk="Undefined",
        description="Sell OTM call and OTM put (same expiry). Profit if underlying stays between strikes.",
        setup="Short Put (low K) + Short Call (high K)",
        max_profit="Net credit",
        max_loss="Undefined",
        capital_note="Margin-intensive (undefined risk).",
        ideal_when="High IV, neutral outlook, expecting IV contraction.",
        legs=[
            LegSpec("Short Put",  "short", "P", strike_rank=1),
            LegSpec("Short Call", "short", "C", strike_rank=2),
        ],
        metrics=["max_profit", "break_even", "pop", "delta", "theta", "vega",
                 "iv", "capital_req", "dte"],
    ),
    Template(
        key="iron_condor",
        name="Iron Condor",
        category="Volatility",
        outlook="Neutral",
        risk="Defined",
        description="Sell an OTM put spread and an OTM call spread (same expiry).",
        setup="Long Put + Short Put + Short Call + Long Call (strikes ascending)",
        max_profit="Net credit",
        max_loss="Width − net credit (wider wing)",
        capital_note="Capital ≈ wider wing − credit.",
        ideal_when="High IV, neutral outlook, want defined risk premium.",
        legs=[
            LegSpec("Long Put (lowest K)",   "long",  "P", strike_rank=1),
            LegSpec("Short Put",             "short", "P", strike_rank=2),
            LegSpec("Short Call",            "short", "C", strike_rank=3),
            LegSpec("Long Call (highest K)", "long",  "C", strike_rank=4),
        ],
        metrics=["max_profit", "max_loss", "break_even", "pop", "capital_req",
                 "delta", "theta", "vega", "iv", "dte"],
    ),
    Template(
        key="iron_butterfly",
        name="Iron Butterfly",
        category="Volatility",
        outlook="Neutral",
        risk="Defined",
        description="Sell an ATM straddle and buy wings (same expiry).",
        setup="Long Put + Short Put/Call at ATM + Long Call",
        max_profit="Net credit (if expires at center strike)",
        max_loss="Wing width − net credit",
        capital_note="Capital ≈ wing width − credit.",
        ideal_when="Very neutral, expect price to pin near a specific strike; high IV.",
        legs=[
            LegSpec("Long Put (wing low)",  "long",  "P", strike_rank=1),
            LegSpec("Short Put (center)",   "short", "P", strike_rank=2),
            LegSpec("Short Call (center)",  "short", "C", strike_rank=2),
            LegSpec("Long Call (wing high)","long",  "C", strike_rank=3),
        ],
        metrics=["max_profit", "max_loss", "break_even", "pop", "capital_req",
                 "delta", "theta", "vega", "iv", "dte"],
    ),
    Template(
        key="calendar",
        name="Calendar Spread",
        category="Volatility",
        outlook="Neutral",
        risk="Defined",
        description="Sell a near-dated option, buy a far-dated option at the same strike.",
        setup="Short near-dated + Long far-dated (same K, same type)",
        max_profit="Variable (depends on IV changes and time)",
        max_loss="Net debit",
        capital_note="Net debit paid.",
        ideal_when="Expect underlying to pin near the strike; expect IV expansion in back-month.",
        legs=[
            LegSpec("Short near-dated", "short", "C", strike_rank=1, expiry_rank=1),
            LegSpec("Long far-dated",   "long",  "C", strike_rank=1, expiry_rank=2),
        ],
        metrics=["max_loss", "vega", "theta", "iv", "dte"],
    ),
    Template(
        key="diagonal",
        name="Diagonal Spread",
        category="Spread",
        outlook="Directional-Income",
        risk="Defined",
        description="Long far-dated option + short near-dated option at a different strike.",
        setup="Long back-month + Short front-month (different strikes)",
        max_profit="Variable (path-dependent)",
        max_loss="Net debit",
        capital_note="Net debit paid.",
        ideal_when="Directional bias with income via front-month theta.",
        legs=[
            LegSpec("Short front-month", "short", "C", strike_rank=2, expiry_rank=1),
            LegSpec("Long back-month",   "long",  "C", strike_rank=1, expiry_rank=2),
        ],
        metrics=["max_loss", "delta", "theta", "vega", "iv", "dte"],
    ),
]


def get_template(key):
    for t in TEMPLATES:
        if t.key == key:
            return t
    return None


def search_templates(query):
    q = (query or "").strip().lower()
    if not q:
        return list(TEMPLATES)
    return [t for t in TEMPLATES
            if q in t.name.lower()
            or q in t.category.lower()
            or q in t.outlook.lower()
            or q in t.description.lower()]
