"""Compact strategy card — essentials only. Click opens full detail page."""
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel
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
        self.setObjectName("card")
        self.setStyleSheet(
            f"QFrame#card {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 14px; }}"
            f"QFrame#card:hover {{ border-color: {T.PURPLE}; }}"
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        h = QHBoxLayout(self)
        h.setContentsMargins(22, 16, 22, 16)
        h.setSpacing(16)

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
            dte_color(strategy.dte)
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

        chevron = QLabel("›")
        chevron.setStyleSheet(
            f"color: {T.MUTED}; font-size: 22px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        h.addWidget(chevron)

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

    # ── Events ──────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.strategy)
            event.accept()
        else:
            super().mousePressEvent(event)
