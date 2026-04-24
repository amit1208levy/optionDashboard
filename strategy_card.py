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
    clicked = pyqtSignal(object)   # strategy

    def __init__(self, strategy, parent=None, metrics=None):
        super().__init__(parent)
        self.strategy = strategy
        self.metrics = metrics or {}
        self._pnl_val_lbl = None   # QLabel — set by _stat() when is_pnl=True
        self._pnl_pct_lbl = None   # QLabel for pct sub-label
        self._expanded  = False
        self._body      = None     # expandable legs container (built lazily)
        self._chevron   = None
        self.setObjectName("card")
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
        ))

        pop = probability_of_profit(strategy)
        if pop is None:
            pop_text, pop_c = "—", T.MUTED
        else:
            pop_text = f"{pop:.0f}%"
            pop_c = T.GREEN if pop >= 60 else (T.YELLOW if pop >= 40 else T.RED)
        h.addWidget(self._stat("POP", pop_text, pop_c))

        h.addWidget(self._stat(
            "Open P&L",
            money(strategy.pnl, signed=True),
            pnl_color(strategy.pnl),
            sub=pct(strategy.pnl_pct),
            is_pnl=True,
        ))

        self._chevron = QLabel("›")
        self._chevron.setStyleSheet(
            f"color: {T.MUTED}; font-size: 22px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        h.addWidget(self._chevron)

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

    def _stat(self, label, value, color, sub=None, is_pnl=False):
        w = QFrame()
        w.setStyleSheet("background: transparent; border: none;")
        w.setFixedWidth(110)
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

        # ── Legs table ────────────────────────────────────────────────────
        section_title = QLabel(f"Legs ({len(s.legs)})")
        section_title.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"letter-spacing: 0.5px; border: none; background: transparent;"
        )
        lay.addWidget(section_title)

        cols = [
            ("Leg",      0,   Qt.AlignmentFlag.AlignLeft),
            ("Qty",      55,  Qt.AlignmentFlag.AlignRight),
            ("Strike",   75,  Qt.AlignmentFlag.AlignRight),
            ("DTE",      50,  Qt.AlignmentFlag.AlignRight),
            ("Mark",     80,  Qt.AlignmentFlag.AlignRight),
            ("Day P&L",  85,  Qt.AlignmentFlag.AlignRight),
            ("Θ",        65,  Qt.AlignmentFlag.AlignRight),
            ("P&L",      90,  Qt.AlignmentFlag.AlignRight),
        ]
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        for text, width, align in cols:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; font-weight: bold; "
                f"letter-spacing: 0.6px; border: none; background: transparent;"
            )
            if width:
                lbl.setFixedWidth(width)
            lbl.setAlignment(align)
            hdr.addWidget(lbl, 0 if width else 1)
        lay.addLayout(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {T.BORDER}; max-height: 1px; border: none;")
        lay.addWidget(sep)

        # One row per leg
        for leg in s.legs:
            row = QHBoxLayout()
            row.setSpacing(10)

            # Leg name: direction pill + type
            leg_wrap = QHBoxLayout()
            leg_wrap.setSpacing(6)
            leg_wrap.setContentsMargins(0, 0, 0, 0)
            direction = leg.direction_label     # "Long" / "Short"
            dir_color = T.GREEN if leg.is_long else T.RED
            dir_badge = QLabel(direction[0])    # "L" / "S"
            dir_badge.setFixedSize(18, 18)
            dir_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dir_badge.setStyleSheet(
                f"color: white; background: {dir_color}; "
                f"border-radius: 4px; font-size: 10px; font-weight: bold;"
            )
            leg_wrap.addWidget(dir_badge)
            kind_lbl = QLabel(leg.type_label)
            kind_lbl.setStyleSheet(
                f"color: {T.TEXT}; font-size: 12px; border: none; background: transparent;"
            )
            leg_wrap.addWidget(kind_lbl)
            leg_wrap.addStretch()
            leg_widget = QWidget()
            leg_widget.setLayout(leg_wrap)
            leg_widget.setStyleSheet("background: transparent;")
            row.addWidget(leg_widget, 1)

            def add(text, width, color=T.TEXT_DIM, bold=False):
                l = QLabel(text)
                l.setFixedWidth(width)
                l.setAlignment(Qt.AlignmentFlag.AlignRight)
                l.setStyleSheet(
                    f"color: {color}; font-size: 12px; "
                    f"font-weight: {'bold' if bold else 'normal'}; "
                    f"border: none; background: transparent;"
                )
                row.addWidget(l)

            add(f"{leg.sign * leg.quantity:+g}",           55)
            add(f"{leg.strike:g}" if leg.strike else "—",  75)
            add(f"{leg.dte}d" if leg.dte is not None else "—", 50)
            add(f"${leg.mark_price:,.2f}",                 80)

            # Day P&L per leg
            if leg.close_price and leg.close_price > 0 and leg.mark_price:
                leg_day = leg.sign * leg.quantity * leg.multiplier \
                          * (leg.mark_price - leg.close_price)
                add(money(leg_day, signed=True), 85, pnl_color(leg_day), bold=True)
            else:
                add("—", 85)

            # Theta per leg in dollars-per-day (uses the contract $-multiplier
            # so futures options aren't understated by the per-point factor)
            if leg.theta is not None:
                mult = leg.multiplier or (1 if _is_future_option(leg.instrument_type) else 100)
                leg_theta = leg.theta * leg.quantity * mult * leg.sign
                add(_fmt_greek(leg_theta), 65, pnl_color(leg_theta))
            else:
                add("—", 65)

            add(money(leg.pnl, signed=True), 90, pnl_color(leg.pnl), bold=True)

            lay.addLayout(row)

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
