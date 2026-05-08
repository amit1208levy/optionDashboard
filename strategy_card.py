"""Compact strategy card — essentials only.
Click toggles an expanded legs view; a 'View details' button inside the
expanded section opens the full detail page."""
from PyQt6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QLabel,
                              QPushButton, QWidget)
from PyQt6.QtCore import Qt, pyqtSignal

import theme as T
from models import probability_of_profit, symbol_ivr, check_exit_conditions


def _fmt_greek(v):
    """Format a Greek value with sensible precision for the legs card."""
    if v is None:
        return "—"
    av = abs(v)
    if av < 0.0005:
        return "~0"
    if av < 0.01:
        return f"{v:+.4f}"
    if av < 0.1:
        return f"{v:+.3f}"
    if av < 100:
        return f"{v:+.2f}"
    return f"{v:+,.0f}"


def money(v, signed=False, na="—"):
    if v is None: return na
    if v == float("inf") or v == float("-inf"): return "Unlimited"
    if signed:    return f"{'+' if v >= 0 else '−'}${abs(v):,.2f}"
    return f"${v:,.2f}"


def pct(v, signed=True, na="—"):
    if v is None: return na
    sign = "+" if v >= 0 else "−"
    if not signed: return f"{abs(v):.1f}%"
    return f"{sign}{abs(v):.1f}%"


def fmt_num(v, digits=3, signed=False, na="—"):
    if v is None: return na
    if signed:
        sign = "+" if v >= 0 else "−"
        return f"{sign}{abs(v):.{digits}f}"
    return f"{v:.{digits}f}"


def dte_color(d):
    if d is None: return T.MUTED
    if d <= 3:    return T.RED
    if d <= 14:   return T.YELLOW
    return T.TEAL


def pnl_color(v):
    if v is None or v == 0: return T.MUTED
    return T.GREEN if v > 0 else T.RED


# ── Strategy card ───────────────────────────────────────────────────────────

class StrategyCard(QFrame):
    clicked        = pyqtSignal(object)   # strategy
    hide_requested = pyqtSignal(object)   # strategy — user clicked hide

    # Color cycle for leg-group markers — high contrast against dark theme.
    _GROUP_COLORS = (
        "#60a5fa",  # blue
        "#4ade80",  # green
        "#fbbf24",  # amber
        "#f472b6",  # pink
        "#a78bfa",  # violet
        "#2dd4bf",  # teal
        "#fb923c",  # orange
    )

    # Master column registry. Used for both the sort-header bar and the per-
    # card stats so they stay in sync. Tuple: (key, label, width, default_asc).
    # ALL_COLUMNS is the complete catalog; the user picks which to show and
    # in what order via the column-settings dialog.
    ALL_COLUMNS = (
        ("dte",     "DTE",       68,  True),
        ("pop",     "POP",       68,  False),
        ("delta",   "Δ",         82,  False),
        ("theta",   "Θ",         82,  False),
        ("day",     "DAY P&L",   100, False),
        ("pnl",     "OPEN P&L",  100, False),
        ("pnl_pct", "P&L %",     72,  False),
        ("ytd",     "P&L YTD",   100, False),
        ("ytd_pct", "YTD %",     72,  False),
    )
    DEFAULT_COLUMN_KEYS = tuple(c[0] for c in ALL_COLUMNS)

    # ── LEG-LEVEL columns (shown inside each leg row) ─────────────────────
    # Identity columns (qty, group marker, ticker, FUT pill, futures-contract
    # pill) always render — they're not configurable. Everything else IS.
    LEG_ALL_COLUMNS = (
        ("exp",     "Expiration"),
        ("dte",     "DTE"),
        ("strike",  "Strike"),
        ("cp",      "C/P"),
        ("pnl",     "P&L"),
        ("pnl_pct", "P&L %"),
        ("day",     "Day P&L"),
        ("theta_d", "Θ $"),
        ("delta",   "Δ"),
        ("gamma",   "Γ"),
        ("vega",    "V"),
        ("dit",     "DIT"),
    )
    DEFAULT_LEG_COLUMN_KEYS = (
        "exp", "dte", "strike", "cp", "pnl", "pnl_pct", "day", "theta_d", "dit",
    )

    def __init__(self, strategy, parent=None, metrics=None, hidden=False,
                 history=None, column_keys=None, leg_column_keys=None):
        super().__init__(parent)
        self.strategy = strategy
        self.metrics = metrics or {}
        self.is_hidden = bool(hidden)
        self.history = history or []
        self.leg_column_keys = (
            list(leg_column_keys) if leg_column_keys
            else list(self.DEFAULT_LEG_COLUMN_KEYS)
        )

        # Build a {leg_symbol: (color, group_name)} map from the strategy's
        # saved leg groups (set in the strategy detail page). Used by
        # _build_leg_card to draw a colored stripe + group pill on each leg.
        self._sym_to_group: dict = {}
        try:
            from models import StrategyInstance
            if isinstance(strategy, StrategyInstance):
                groups = (strategy._raw.get("leg_groups") or [])
                for i, g in enumerate(groups):
                    color = self._GROUP_COLORS[i % len(self._GROUP_COLORS)]
                    name  = g.get("name") or f"Group {i+1}"
                    for sym in (g.get("legs") or []):
                        self._sym_to_group[sym] = (color, name)
        except Exception:
            self._sym_to_group = {}
        self._pnl_val_lbl = None   # QLabel — set by _stat() when is_pnl=True
        self._pnl_pct_lbl = None   # QLabel for pct sub-label
        self._expanded  = False
        self._body      = None     # expandable legs container (built lazily)
        self._chevron   = None
        self.setObjectName("card")
        if self.is_hidden:
            # Dim look so users can tell "show hidden" mode is on at a glance.
            self.setStyleSheet(
                f"QFrame#card {{ background: {T.BG_ALT}; border: 1px dashed {T.BORDER}; "
                f"border-radius: 14px; }}"
                f"QFrame#card:hover {{ border-color: {T.PURPLE}; }}"
            )
        else:
            self.setStyleSheet(
                f"QFrame#card {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
                f"border-radius: 14px; }}"
                f"QFrame#card:hover {{ border-color: {T.PURPLE}; }}"
            )
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # Outer vertical layout: header row on top, expandable legs below
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header row (what the card has always shown) ──────────────────
        header = QFrame()
        header.setStyleSheet("background: transparent; border: none;")
        h = QHBoxLayout(header)
        h.setContentsMargins(22, 16, 22, 16)
        h.setSpacing(16)
        outer.addWidget(header)

        # ── Left: name + badges ────────────────────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(4)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_lbl = QLabel(strategy.name)
        name_lbl.setStyleSheet(
            f"color: {T.TEXT}; font-size: 16px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        name_row.addWidget(name_lbl)
        name_row.addStretch()
        left.addLayout(name_row)

        sub_row = QHBoxLayout()
        sub_row.setSpacing(8)
        sub_row.addWidget(self._badge(strategy.root or "—", T.ACCENT))
        sub_row.addWidget(self._badge(f"{len(strategy.legs)} legs", T.MUTED, outlined=True))

        ivr = symbol_ivr(self.metrics.get(strategy.root))
        if ivr is not None:
            ivr_c = T.GREEN if ivr >= 50 else (T.YELLOW if ivr >= 25 else T.RED)
            sub_row.addWidget(self._badge(f"IVR {ivr:.0f}", ivr_c, outlined=True))

        # Exit-plan alert badge
        from models import StrategyInstance as _SI
        if isinstance(strategy, _SI) and strategy.exit_plan:
            conds = check_exit_conditions(strategy, strategy.exit_plan)
            hit   = [c for c in conds if c["severity"] == "hit"]
            near  = [c for c in conds if c["severity"] == "near"]
            if hit:
                badge_txt   = f"⚡ {hit[0]['label']}"
                badge_style = (
                    f"color: {T.RED}; background: #2d1515; border: 1px solid {T.RED}; "
                    f"border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: bold;"
                )
                sub_row.addWidget(self._badge_raw(badge_txt, badge_style))
            elif near:
                badge_txt   = f"◐ {near[0]['label']}"
                badge_style = (
                    f"color: {T.YELLOW}; background: #2a2010; border: 1px solid {T.YELLOW}; "
                    f"border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: bold;"
                )
                sub_row.addWidget(self._badge_raw(badge_txt, badge_style))

        sub_row.addStretch()
        left.addLayout(sub_row)

        h.addLayout(left, 3)

        # ── Stats: render columns in the order the user configured ───────
        # Compute every value up-front; we'll iterate column_keys to add
        # only the ones the user wants, in the order they want.
        col_meta = {k: (label, w) for k, label, w, _ in self.ALL_COLUMNS}
        column_keys = list(column_keys or self.DEFAULT_COLUMN_KEYS)

        from models import strategy_pnl_summary
        sid     = getattr(strategy, "id", None)
        summary = strategy_pnl_summary(sid, self.history, strategy) if sid else None

        # POP
        pop = probability_of_profit(strategy)
        # Net Δ / Θ
        nd = strategy.net_delta
        nt = strategy.net_theta
        # Day P&L
        day_pnl = sum(
            l.sign * l.quantity * l.multiplier * (l.mark_price - l.close_price)
            for l in strategy.legs
            if l.close_price and l.close_price > 0 and l.mark_price
        )
        # YTD totals
        ytd_total = summary["total_ytd"]     if summary else None
        ytd_pct   = summary["total_ytd_pct"] if summary else None

        def _greek_text(v):
            if v is None:
                return "—", T.MUTED
            t = (f"{v:+,.0f}" if abs(v) >= 100 else f"{v:+.1f}").replace("-", "−")
            return t, pnl_color(v)

        for key in column_keys:
            if key not in col_meta:
                continue
            label, width = col_meta[key]
            if key == "dte":
                txt = str(strategy.dte) if strategy.dte is not None else "—"
                h.addWidget(self._stat(label, txt, T.TEXT, width=width))
            elif key == "pop":
                if pop is None:
                    txt, c = "—", T.MUTED
                else:
                    txt = f"{pop:.0f}%"
                    c = T.GREEN if pop >= 60 else (T.YELLOW if pop >= 40 else T.RED)
                h.addWidget(self._stat(label, txt, c, width=width))
            elif key == "delta":
                txt, c = _greek_text(nd)
                h.addWidget(self._stat(label, txt, c, width=width))
            elif key == "theta":
                txt, c = _greek_text(nt)
                h.addWidget(self._stat(label, txt, c, width=width))
            elif key == "day":
                if day_pnl == 0:
                    txt, c = "—", T.MUTED
                else:
                    txt, c = money(day_pnl, signed=True), pnl_color(day_pnl)
                h.addWidget(self._stat(label, txt, c, width=width))
            elif key == "pnl":
                h.addWidget(self._stat(
                    label, money(strategy.pnl, signed=True),
                    pnl_color(strategy.pnl), is_pnl=True, width=width,
                ))
            elif key == "pnl_pct":
                h.addWidget(self._stat(
                    label, pct(strategy.pnl_pct),
                    pnl_color(strategy.pnl_pct), width=width,
                ))
            elif key == "ytd":
                txt = (money(ytd_total, signed=True) if ytd_total is not None else "—")
                c   = pnl_color(ytd_total) if ytd_total is not None else T.MUTED
                h.addWidget(self._stat(label, txt, c, width=width))
            elif key == "ytd_pct":
                txt = (pct(ytd_pct) if ytd_pct is not None else "—")
                c   = pnl_color(ytd_pct) if ytd_pct is not None else T.MUTED
                h.addWidget(self._stat(label, txt, c, width=width))

        # Cache values for the parent's sort logic — exposes computed numbers
        # without re-deriving them in app.py.
        self._sort_values = {
            "dte":      float(strategy.dte) if strategy.dte is not None else None,
            "pop":      float(pop) if pop is not None else None,
            "delta":    float(nd) if nd is not None else None,
            "theta":    float(nt) if nt is not None else None,
            "day":      float(day_pnl) if day_pnl else 0.0,
            "pnl":      float(strategy.pnl) if strategy.pnl is not None else 0.0,
            "pnl_pct":  float(strategy.pnl_pct) if strategy.pnl_pct is not None else 0.0,
            "ytd":      float(summary["total_ytd"]) if summary else 0.0,
            "ytd_pct":  float(summary["total_ytd_pct"]) if summary and summary["total_ytd_pct"] is not None else 0.0,
            "all_time": float(summary["total_all"]) if summary else 0.0,
        }

        self._chevron = QLabel("›")
        self._chevron.setStyleSheet(
            f"color: {T.MUTED}; font-size: 22px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        h.addWidget(self._chevron)

        # ── Hide / unhide button ──────────────────────────────────────────
        # Tiny ✕ button at the far right; click toggles this strategy's
        # hidden state.  QPushButton consumes the click so card.clicked
        # doesn't also fire.
        hide_btn = QPushButton("↺" if self.is_hidden else "✕")
        hide_btn.setFixedSize(20, 20)
        hide_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        hide_btn.setToolTip(
            "Show this strategy again" if self.is_hidden
            else "Hide this strategy from the list"
        )
        hide_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid transparent; border-radius: 10px; "
            f"font-size: 12px; font-weight: bold; padding: 0; }}"
            f"QPushButton:hover {{ color: {T.ACCENT if self.is_hidden else T.RED}; "
            f"border-color: {T.BORDER}; background: {T.BG_ALT}; }}"
        )
        hide_btn.clicked.connect(lambda: self.hide_requested.emit(self.strategy))
        h.addWidget(hide_btn)

        # ── Expandable legs body (hidden until user clicks) ───────────────
        self._outer_lay = outer

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _badge_raw(self, text, stylesheet):
        l = QLabel(text)
        l.setStyleSheet(stylesheet)
        return l

    def _badge(self, text, color, outlined=False):
        l = QLabel(text)
        if outlined:
            l.setStyleSheet(
                f"color: {color}; background: transparent; border: 1px solid {T.BORDER}; "
                f"border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: 600;"
            )
        else:
            l.setStyleSheet(
                f"color: white; background: {color}; border: none; "
                f"border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: 700;"
            )
        return l

    def _stat(self, label, value, color, sub=None, is_pnl=False, width=110):
        w = QFrame()
        w.setStyleSheet("background: transparent; border: none;")
        w.setFixedWidth(width)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.setAlignment(Qt.AlignmentFlag.AlignRight)
        lbl = QLabel(label.upper())
        lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.7px; "
            f"background: transparent; border: none;"
        )
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        lay.addWidget(lbl)
        val = QLabel(value)
        val.setStyleSheet(
            f"color: {color}; font-size: 15px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        val.setAlignment(Qt.AlignmentFlag.AlignRight)
        lay.addWidget(val)
        sub_lbl = None
        if sub:
            sub_lbl = QLabel(sub)
            sub_lbl.setStyleSheet(
                f"color: {color}; font-size: 11px; background: transparent; border: none;"
            )
            sub_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            lay.addWidget(sub_lbl)
        if is_pnl:
            self._pnl_val_lbl = val
            self._pnl_pct_lbl = sub_lbl
        return w

    def refresh_pnl(self):
        """Update the Open P&L stat in-place after a live quote update.
        Called from the GUI thread; safe since all Qt label updates must be
        on the main thread (the Qt signal from QuoteStreamer delivers here)."""
        if self._pnl_val_lbl is None:
            return
        p   = self.strategy.pnl
        pp  = self.strategy.pnl_pct
        c   = pnl_color(p)
        self._pnl_val_lbl.setText(money(p, signed=True))
        self._pnl_val_lbl.setStyleSheet(
            f"color: {c}; font-size: 15px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        if self._pnl_pct_lbl is not None:
            self._pnl_pct_lbl.setText(pct(pp))
            self._pnl_pct_lbl.setStyleSheet(
                f"color: {c}; font-size: 11px; "
                f"background: transparent; border: none;"
            )

    # ── Expand / collapse logic ──────────────────────────────────────────────

    def _build_body(self):
        """Lazily construct the legs table shown when the card is expanded."""
        if self._body is not None:
            return

        body = QFrame()
        body.setStyleSheet(
            f"QFrame {{ background: #12151d; border-top: 1px solid {T.BORDER}; "
            f"border-bottom-left-radius: 13px; border-bottom-right-radius: 13px; }}"
        )
        lay = QVBoxLayout(body)
        lay.setContentsMargins(22, 18, 22, 20)
        lay.setSpacing(10)

        # ── Aggregate stats row (Day P&L + Greeks) ────────────────────────
        from models import _is_future_option
        s      = self.strategy
        # Day P&L aggregate across legs
        day_pnl = sum(
            l.sign * l.quantity * l.multiplier * (l.mark_price - l.close_price)
            for l in s.legs
            if l.close_price and l.close_price > 0 and l.mark_price
        )
        net_delta = s.net_delta
        net_theta = s.net_theta
        net_vega  = s.net_vega

        agg_row = QHBoxLayout()
        agg_row.setSpacing(10)
        agg_row.addWidget(self._chip("Day P&L",  money(day_pnl, signed=True),
                                      pnl_color(day_pnl)))
        agg_row.addWidget(self._chip("Net Δ",    _fmt_greek(net_delta), T.TEXT))
        agg_row.addWidget(self._chip("Net Θ",    _fmt_greek(net_theta),
                                      pnl_color(net_theta)))
        agg_row.addWidget(self._chip("Net V",    _fmt_greek(net_vega),  T.TEXT))
        agg_row.addStretch()
        lay.addLayout(agg_row)

        # ── Legs section ──────────────────────────────────────────────────
        section_title = QLabel(f"LEGS  ·  {len(s.legs)}")
        section_title.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"letter-spacing: 0.8px; border: none; background: transparent;"
        )
        lay.addWidget(section_title)

        # Each leg is its own card — two lines: headline + Greeks
        for leg in s.legs:
            lay.addWidget(self._build_leg_card(leg))

        # ── "View details" button ─────────────────────────────────────────
        lay.addSpacing(6)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn = QPushButton("View full details →")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedHeight(34)
        btn.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: white; border: none; "
            f"border-radius: 8px; padding: 0 18px; font-size: 12px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
        )
        btn.clicked.connect(lambda: self.clicked.emit(self.strategy))
        btn_row.addWidget(btn)
        lay.addLayout(btn_row)

        self._body = body
        self._body.setVisible(False)
        self._outer_lay.addWidget(body)

    def _build_leg_card(self, leg):
        """Compact single-row leg card: Qty|Exp|DTE|Strike|C/P|P&L|P&L%|Day|Θ$|DIT|DTE."""
        from models import _is_future_option, _CONTRACT_MULT

        is_fut          = bool(getattr(leg, "is_future", False))
        is_fut_opt      = is_fut and bool(getattr(leg, "is_option", False))
        is_fut_contract = is_fut and not getattr(leg, "is_option", False)

        card = QFrame()
        if is_fut:
            # Amber-800 (much darker than the bright #fbbf24) — still
            # signals "futures" without yelling. Pill + yellow ticker
            # provide the primary visual cue.
            card.setStyleSheet(
                f"QFrame {{ background: {T.CARD_ALT}; "
                f"border: 1px solid #78350f; "
                f"border-radius: 8px; }}"
            )
        else:
            card.setStyleSheet(
                f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
                f"border-radius: 8px; }}"
            )
        row = QHBoxLayout(card)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(14)

        # Group marker: colored vertical stripe on the left side of the card +
        # a small pill with the group name. Both inserted before everything
        # else so they're the leftmost visual on the row.
        group_info = self._sym_to_group.get(getattr(leg, "symbol", None))
        if group_info:
            g_color, g_name = group_info
            stripe = QFrame()
            stripe.setFixedWidth(4)
            stripe.setMinimumHeight(28)
            stripe.setStyleSheet(
                f"background: {g_color}; border: none; border-radius: 2px;"
            )
            row.addWidget(stripe)

            g_pill = QLabel(g_name.upper())
            g_pill.setStyleSheet(
                f"color: {g_color}; background: transparent; "
                f"border: 1px solid {g_color}; border-radius: 5px; "
                f"padding: 2px 8px; font-size: 10px; font-weight: 800; "
                f"letter-spacing: 0.5px;"
            )
            row.addWidget(g_pill)

        side_color = T.GREEN if leg.is_long else T.RED
        qty_signed = leg.sign * leg.quantity
        if abs(qty_signed - round(qty_signed)) < 1e-9:
            qty_text = f"{int(qty_signed):+d}".replace("-", "−")
        else:
            qty_text = f"{qty_signed:+g}".replace("-", "−")
        qty_lbl = QLabel(qty_text)
        qty_lbl.setStyleSheet(
            f"color: {side_color}; font-size: 18px; font-weight: 800; "
            f"background: transparent; border: none; min-width: 36px;"
        )
        row.addWidget(qty_lbl)

        def _cell(text, color, weight=500, size=12):
            l = QLabel(text)
            l.setStyleSheet(
                f"color: {color}; font-size: {size}px; font-weight: {weight}; "
                f"background: transparent; border: none;"
            )
            return l

        ticker_color = T.YELLOW if is_fut else side_color
        ticker_size  = 14 if is_fut else 13
        row.addWidget(_cell(leg.root or "—", ticker_color, 800, ticker_size))

        # ── Futures-contract shortcut layout ─────────────────────────────
        # Pure futures contracts have no strikes, no Greeks, no DTE in the
        # options sense. Skip the configurable column iteration entirely
        # and just show: FUTURES CONTRACT pill + P&L + Open price + Capital
        # requirement estimate. Three labeled values, that's it.
        if is_fut_contract:
            from models import _FUTURES_SPAN, _SPAN_FALLBACK_PCT

            pill = QLabel("FUTURES CONTRACT")
            pill.setStyleSheet(
                f"color: #1a1500; background: {T.YELLOW}; border: none; "
                f"border-radius: 5px; padding: 3px 10px; "
                f"font-size: 11px; font-weight: 900; letter-spacing: 0.6px;"
            )
            row.addWidget(pill)

            # Vertical rule before the value trio
            sep = QFrame()
            sep.setFixedWidth(1)
            sep.setFixedHeight(22)
            sep.setStyleSheet(f"background: {T.BORDER}; border: none; margin: 0 4px;")
            row.addWidget(sep)

            def _labeled(label_text, value_text, value_color, value_weight=800,
                          value_size=14):
                grp = QHBoxLayout()
                grp.setSpacing(6)
                lbl = QLabel(label_text)
                lbl.setStyleSheet(
                    f"color: {T.MUTED}; font-size: 10px; font-weight: 600; "
                    f"letter-spacing: 0.5px; background: transparent; border: none;"
                )
                val = QLabel(value_text)
                val.setStyleSheet(
                    f"color: {value_color}; font-size: {value_size}px; "
                    f"font-weight: {value_weight}; background: transparent; border: none;"
                )
                grp.addWidget(lbl)
                grp.addWidget(val)
                container = QFrame()
                container.setStyleSheet("background: transparent; border: none;")
                container.setLayout(grp)
                return container

            # P&L
            row.addWidget(_labeled("P&L",
                                    money(leg.pnl, signed=True),
                                    pnl_color(leg.pnl), 800, 14))
            # Open price (per-contract, what you paid/received)
            open_str = (f"{leg.avg_open_price:g}"
                        if getattr(leg, "avg_open_price", None) else "—")
            row.addWidget(_labeled("OPEN", open_str, T.TEXT, 700, 13))
            # Capital requirement = SPAN initial margin × qty
            margin_per = _FUTURES_SPAN.get(leg.root or "", 0)
            if not margin_per:
                ref = float(getattr(leg, "underlying_price", 0) or 0)
                margin_per = ref * float(leg.multiplier or 1) * _SPAN_FALLBACK_PCT
            cap_total = margin_per * (leg.quantity or 0)
            row.addWidget(_labeled("CAP", money(cap_total),
                                    T.YELLOW, 700, 13))
            row.addStretch()
            return card

        # Pre-compute everything once; stat cells are added based on the
        # configured leg-column order.
        exp_str = leg.expires_at.strftime("%b %d") if leg.expires_at else "—"
        dte_str = f"{leg.dte}d" if leg.dte is not None else "—"

        pnl_pct = leg.pnl_pct
        if pnl_pct is not None:
            pnl_pct_str = (f"+{pnl_pct:.1f}%" if pnl_pct >= 0
                           else f"−{abs(pnl_pct):.1f}%")
        else:
            pnl_pct_str = "—"

        if leg.close_price and leg.close_price > 0 and leg.mark_price:
            day_pnl = (leg.sign * leg.quantity * leg.multiplier
                       * (leg.mark_price - leg.close_price))
        else:
            day_pnl = None
        day_str = money(day_pnl, signed=True) if day_pnl is not None else "—"

        if _is_future_option(leg.instrument_type):
            theta_mult = float(_CONTRACT_MULT.get(leg.root or "", 1))
        else:
            theta_mult = 100.0
        theta_dollar = (leg.theta * leg.quantity * theta_mult * leg.sign
                        if leg.theta is not None else None)
        theta_str = money(theta_dollar, signed=True) if theta_dollar is not None else "—"

        dit_str = f"{leg.dit}d" if leg.dit is not None else "—"

        # Insert a vertical rule between the identity cells (exp/dte/strike/cp)
        # and the performance cells (pnl/pnl_pct/day/...) once we cross over.
        sep_inserted = [False]
        identity_keys = {"exp", "dte", "strike", "cp"}

        def _maybe_separator(key):
            if not sep_inserted[0] and key not in identity_keys:
                sep = QFrame()
                sep.setFixedWidth(1)
                sep.setFixedHeight(22)
                sep.setStyleSheet(f"background: {T.BORDER}; border: none; margin: 0 4px;")
                row.addWidget(sep)
                sep_inserted[0] = True

        # For futures contracts, the "FUTURES CONTRACT" pill replaces strike+cp.
        # We render it on the first 'strike' key we encounter and skip the 'cp'.
        future_pill_drawn = [False]

        for key in self.leg_column_keys:
            _maybe_separator(key)

            if key in ("strike", "cp") and is_fut_contract:
                if not future_pill_drawn[0]:
                    pill = QLabel("FUTURES CONTRACT")
                    pill.setStyleSheet(
                        f"color: #1a1500; background: {T.YELLOW}; border: none; "
                        f"border-radius: 5px; padding: 3px 10px; "
                        f"font-size: 11px; font-weight: 900; letter-spacing: 0.6px;"
                    )
                    row.addWidget(pill)
                    future_pill_drawn[0] = True
                continue

            if key == "exp":
                row.addWidget(_cell(exp_str, T.TEXT_DIM, 400, 12))
            elif key == "dte":
                row.addWidget(_cell(dte_str, dte_color(leg.dte), 700, 12))
            elif key == "strike":
                strike_str = f"{leg.strike:g}" if leg.strike else "—"
                row.addWidget(_cell(strike_str, T.TEXT, 800, 14))
            elif key == "cp":
                cp_str = leg.call_put or "—"
                row.addWidget(_cell(cp_str, side_color, 800, 14))
            elif key == "pnl":
                row.addWidget(_cell(money(leg.pnl, signed=True),
                                    pnl_color(leg.pnl), 800, 14))
            elif key == "pnl_pct":
                row.addWidget(_cell(pnl_pct_str, pnl_color(pnl_pct), 600, 12))
            elif key == "day":
                row.addWidget(_cell(
                    day_str,
                    pnl_color(day_pnl) if day_pnl is not None else T.MUTED,
                    700, 13,
                ))
            elif key == "theta_d":
                row.addWidget(_cell(
                    theta_str,
                    pnl_color(theta_dollar) if theta_dollar is not None else T.MUTED,
                    600, 12,
                ))
            elif key == "delta":
                row.addWidget(_cell(_fmt_greek(leg.delta), T.TEXT_DIM, 500, 11))
            elif key == "gamma":
                row.addWidget(_cell(_fmt_greek(leg.gamma), T.TEXT_DIM, 500, 11))
            elif key == "vega":
                row.addWidget(_cell(_fmt_greek(leg.vega),  T.TEXT_DIM, 500, 11))
            elif key == "dit":
                row.addWidget(_cell(dit_str, T.TEXT_DIM, 400, 11))

        row.addStretch()
        return card

    def _chip(self, label, value, color):
        """Little summary tile shown at the top of the expanded legs body."""
        w = QFrame()
        w.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 8px; }}"
        )
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 8, 14, 10)
        lay.setSpacing(2)
        l = QLabel(label.upper())
        l.setStyleSheet(
            f"color: {T.MUTED}; font-size: 9px; font-weight: bold; "
            f"letter-spacing: 0.6px; border: none; background: transparent;"
        )
        v = QLabel(value)
        v.setStyleSheet(
            f"color: {color}; font-size: 14px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        lay.addWidget(l); lay.addWidget(v)
        return w

    def _set_expanded(self, expanded: bool):
        self._expanded = expanded
        if expanded:
            self._build_body()
        if self._body:
            self._body.setVisible(expanded)
        if self._chevron:
            self._chevron.setText("⌄" if expanded else "›")

    def toggle_expanded(self):
        self._set_expanded(not self._expanded)

    # ── Events ──────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # If the click landed inside the expanded body, let its own
            # widgets handle it (e.g. the "View details" button).
            if (self._body is not None and self._body.isVisible()
                    and self._body.geometry().contains(event.pos())):
                super().mousePressEvent(event)
                return
            self.toggle_expanded()
            event.accept()
        else:
            super().mousePressEvent(event)
