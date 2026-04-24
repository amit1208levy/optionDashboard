"""Compact strategy card — essentials only.
Click toggles an expanded legs view; a 'View details' button inside the
expanded section opens the full detail page."""
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from PyQt6.QtCore import Qt, pyqtSignal

import theme as T
from models import probability_of_profit, symbol_ivr, check_exit_conditions


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
        lay.setContentsMargins(22, 12, 22, 14)
        lay.setSpacing(4)

        # Header row
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        for text, width, align in [
            ("Leg",      0,  Qt.AlignmentFlag.AlignLeft),
            ("Qty",     50,  Qt.AlignmentFlag.AlignRight),
            ("Strike",  70,  Qt.AlignmentFlag.AlignRight),
            ("DTE",     50,  Qt.AlignmentFlag.AlignRight),
            ("Mark",    70,  Qt.AlignmentFlag.AlignRight),
            ("P&L",     80,  Qt.AlignmentFlag.AlignRight),
        ]:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 9px; font-weight: bold; "
                f"letter-spacing: 0.5px; border: none;"
            )
            if width:
                lbl.setFixedWidth(width)
            lbl.setAlignment(align)
            hdr.addWidget(lbl, 0 if width else 1)
        lay.addLayout(hdr)

        # One row per leg
        for leg in self.strategy.legs:
            row = QHBoxLayout()
            row.setSpacing(8)

            direction = leg.direction_label[0]   # "L" / "S"
            kind      = leg.type_label            # Call / Put / Stock / Future
            name_lbl  = QLabel(f"{direction}  {kind}")
            name_lbl.setStyleSheet(
                f"color: {T.TEXT_DIM}; font-size: 11px; border: none;"
            )
            row.addWidget(name_lbl, 1)

            qty_lbl = QLabel(f"{leg.sign * leg.quantity:+g}")
            qty_lbl.setFixedWidth(50)
            qty_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            qty_lbl.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 11px; border: none;")
            row.addWidget(qty_lbl)

            strike_lbl = QLabel(f"{leg.strike:g}" if leg.strike else "—")
            strike_lbl.setFixedWidth(70)
            strike_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            strike_lbl.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 11px; border: none;")
            row.addWidget(strike_lbl)

            dte_lbl = QLabel(f"{leg.dte}d" if leg.dte is not None else "—")
            dte_lbl.setFixedWidth(50)
            dte_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            dte_lbl.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 11px; border: none;")
            row.addWidget(dte_lbl)

            mark_lbl = QLabel(f"${leg.mark_price:,.2f}")
            mark_lbl.setFixedWidth(70)
            mark_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            mark_lbl.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 11px; border: none;")
            row.addWidget(mark_lbl)

            pnl_lbl = QLabel(money(leg.pnl, signed=True))
            pnl_lbl.setFixedWidth(80)
            pnl_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            pnl_lbl.setStyleSheet(
                f"color: {pnl_color(leg.pnl)}; font-size: 11px; "
                f"font-weight: bold; border: none;"
            )
            row.addWidget(pnl_lbl)

            lay.addLayout(row)

        # "View details" button opens the full detail page
        lay.addSpacing(4)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn = QPushButton("View full details →")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedHeight(28)
        btn.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: white; border: none; "
            f"border-radius: 6px; padding: 0 14px; font-size: 11px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
        )
        btn.clicked.connect(lambda: self.clicked.emit(self.strategy))
        btn_row.addWidget(btn)
        lay.addLayout(btn_row)

        self._body = body
        self._body.setVisible(False)
        self._outer_lay.addWidget(body)

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
