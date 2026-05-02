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

    def __init__(self, strategy, parent=None, metrics=None, hidden=False, history=None):
        super().__init__(parent)
        self.strategy = strategy
        self.metrics = metrics or {}
        self.is_hidden = bool(hidden)
        self.history = history or []

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

        # ── Right: essential stats only ────────────────────────────────────
        h.addWidget(self._stat(
            "DTE",
            str(strategy.dte) if strategy.dte is not None else "—",
            T.TEXT,
            width=68,
        ))

        pop = probability_of_profit(strategy)
        if pop is None:
            pop_text, pop_c = "—", T.MUTED
        else:
            pop_text = f"{pop:.0f}%"
            pop_c = T.GREEN if pop >= 60 else (T.YELLOW if pop >= 40 else T.RED)
        h.addWidget(self._stat("POP", pop_text, pop_c, width=68))

        # Net Δ and Θ in dollar units (Strategy._agg multiplies by contract mult)
        nd = strategy.net_delta
        if nd is None:
            d_text, d_c = "—", T.MUTED
        else:
            d_text = f"{nd:+,.0f}" if abs(nd) >= 100 else f"{nd:+.1f}"
            d_text = d_text.replace("-", "−")
            d_c = pnl_color(nd)
        h.addWidget(self._stat("Δ", d_text, d_c, width=82))

        nt = strategy.net_theta
        if nt is None:
            t_text, t_c = "—", T.MUTED
        else:
            t_text = f"{nt:+,.0f}" if abs(nt) >= 100 else f"{nt:+.1f}"
            t_text = t_text.replace("-", "−")
            t_c = pnl_color(nt)
        h.addWidget(self._stat("Θ", t_text, t_c, width=82))

        # Day P&L: sum over legs of sign × qty × mult × (mark − close_price)
        day_pnl = sum(
            l.sign * l.quantity * l.multiplier * (l.mark_price - l.close_price)
            for l in strategy.legs
            if l.close_price and l.close_price > 0 and l.mark_price
        )
        if day_pnl == 0:
            day_text, day_c = "—", T.MUTED
        else:
            day_text = money(day_pnl, signed=True)
            day_c = pnl_color(day_pnl)
        h.addWidget(self._stat("Day P&L", day_text, day_c, width=100))

        h.addWidget(self._stat(
            "Open P&L",
            money(strategy.pnl, signed=True),
            pnl_color(strategy.pnl),
            is_pnl=True,
            width=100,
        ))
        h.addWidget(self._stat(
            "P&L %",
            pct(strategy.pnl_pct),
            pnl_color(strategy.pnl_pct),
            width=72,
        ))

        # ── YTD and All-Time P&L (open + realized history) ─────────────────
        from models import strategy_pnl_summary
        sid = getattr(strategy, "id", None)
        summary = strategy_pnl_summary(sid, self.history, strategy) if sid else None
        if summary is not None:
            ytd_total = summary["total_ytd"]
            all_total = summary["total_all"]
            ytd_pct   = summary["total_ytd_pct"]
            all_pct   = summary["total_all_pct"]

            h.addWidget(self._stat(
                "P&L YTD",
                money(ytd_total, signed=True),
                pnl_color(ytd_total),
                width=100,
            ))
            h.addWidget(self._stat(
                "YTD %",
                pct(ytd_pct) if ytd_pct is not None else "—",
                pnl_color(ytd_pct) if ytd_pct is not None else T.MUTED,
                width=72,
            ))
            # Hide All Time when it equals YTD (no closed legs from prior years).
            if abs(all_total - ytd_total) > 0.01:
                h.addWidget(self._stat(
                    "All Time",
                    money(all_total, signed=True),
                    pnl_color(all_total),
                    width=100,
                ))
                h.addWidget(self._stat(
                    "All %",
                    pct(all_pct) if all_pct is not None else "—",
                    pnl_color(all_pct) if all_pct is not None else T.MUTED,
                    width=72,
                ))

        # Cache values for the parent's sort logic — exposes computed numbers
        # without re-deriving them in app.py.
        self._sort_values = {
            "dte":      float(strategy.dte) if strategy.dte is not None else None,
            "pop":      float(pop) if pop is not None else None,
            "delta":    float(nd) if nd is not None else None,
            "theta":    float(nt) if nt is not None else None,
            "day":      float(day_pnl) if day_pnl else 0.0,
            "pnl":      float(strategy.pnl) if strategy.pnl is not None else 0.0,
            "ytd":      float(summary["total_ytd"]) if summary else 0.0,
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
            card.setStyleSheet(
                f"QFrame {{ background: {T.CARD_ALT}; border: 1.5px solid {T.YELLOW}; "
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

        exp_str = leg.expires_at.strftime("%b %d") if leg.expires_at else "—"
        row.addWidget(_cell(exp_str, T.TEXT_DIM, 400, 12))

        dte_str = f"{leg.dte}d" if leg.dte is not None else "—"
        row.addWidget(_cell(dte_str, dte_color(leg.dte), 700, 12))

        if is_fut_contract:
            pill = QLabel("FUTURES CONTRACT")
            pill.setStyleSheet(
                f"color: #1a1500; background: {T.YELLOW}; border: none; "
                f"border-radius: 5px; padding: 3px 10px; "
                f"font-size: 11px; font-weight: 900; letter-spacing: 0.6px;"
            )
            row.addWidget(pill)
        else:
            strike_str = f"{leg.strike:g}" if leg.strike else "—"
            row.addWidget(_cell(strike_str, T.TEXT, 800, 14))
            cp_str = leg.call_put or "—"
            row.addWidget(_cell(cp_str, side_color, 800, 14))

        # Thin vertical rule separating identity from performance
        sep = QFrame()
        sep.setFixedWidth(1)
        sep.setFixedHeight(22)
        sep.setStyleSheet(f"background: {T.BORDER}; border: none; margin: 0 4px;")
        row.addWidget(sep)

        # ── Performance: P&L | P&L% | Day | Θ$ | DIT | DTE ──────────────
        row.addWidget(_cell(money(leg.pnl, signed=True),
                            pnl_color(leg.pnl), 800, 14))

        pnl_pct = leg.pnl_pct
        if pnl_pct is not None:
            pnl_pct_str = (f"+{pnl_pct:.1f}%" if pnl_pct >= 0
                           else f"−{abs(pnl_pct):.1f}%")
        else:
            pnl_pct_str = "—"
        row.addWidget(_cell(pnl_pct_str, pnl_color(pnl_pct), 600, 12))

        if leg.close_price and leg.close_price > 0 and leg.mark_price:
            day_pnl = (leg.sign * leg.quantity * leg.multiplier
                       * (leg.mark_price - leg.close_price))
        else:
            day_pnl = None
        day_str = money(day_pnl, signed=True) if day_pnl is not None else "—"
        row.addWidget(_cell(day_str,
                            pnl_color(day_pnl) if day_pnl is not None else T.MUTED, 700, 13))

        if _is_future_option(leg.instrument_type):
            theta_mult = float(_CONTRACT_MULT.get(leg.root or "", 1))
        else:
            theta_mult = 100.0
        theta_dollar = (leg.theta * leg.quantity * theta_mult * leg.sign
                        if leg.theta is not None else None)
        theta_str = money(theta_dollar, signed=True) if theta_dollar is not None else "—"
        row.addWidget(_cell(theta_str,
                            pnl_color(theta_dollar) if theta_dollar is not None else T.MUTED,
                            600, 12))

        dit_str = f"{leg.dit}d" if leg.dit is not None else "—"
        row.addWidget(_cell(dit_str, T.TEXT_DIM, 400, 11))

        row.addWidget(_cell(dte_str, dte_color(leg.dte), 600, 11))

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
