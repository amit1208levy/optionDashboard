"""Expandable strategy card — TastyTrade-style custom ordering (drag legs + menus)."""
import uuid

from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QInputDialog, QSizePolicy, QMenu, QApplication
)
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData, QPoint
from PyQt6.QtGui import QDrag, QPixmap

import theme as T
from models import strategy_extremes, probability_of_profit
from payoff_chart import PayoffChart


MIME_LEG = "application/x-options-dashboard-leg"


# ── formatting ──────────────────────────────────────────────────────────────

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


def _menu_style():
    return f"""
    QMenu {{
        background: {T.CARD}; border: 1px solid {T.BORDER};
        color: {T.TEXT}; padding: 6px;
    }}
    QMenu::item {{
        padding: 6px 24px 6px 16px; border-radius: 4px; font-size: 13px;
    }}
    QMenu::item:selected {{ background: {T.BORDER_H}; }}
    QMenu::separator {{ height: 1px; background: {T.BORDER}; margin: 4px 6px; }}
    """


# ── Draggable leg row ───────────────────────────────────────────────────────

class LegRow(QFrame):
    move_requested = pyqtSignal(str, QPoint)   # leg_symbol, global_pos

    COLUMNS = [   # (label_header, width) — header only; data is rendered per row
        ("Side",    56),
        ("Type",    54),
        ("Strike",  70),
        ("Exp",     88),
        ("Qty",     44),
        ("Open",    64),
        ("Mark",    64),
        ("Premium", 86),
        ("P&L",     92),
        ("Δ",       52),
        ("Θ",       52),
        ("IV",      58),
    ]

    def __init__(self, leg, parent=None):
        super().__init__(parent)
        self.leg = leg
        self._press_pos = None

        self.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 8px; }}"
            f"QFrame:hover {{ border-color: {T.BORDER_H}; background: #1d2034; }}"
        )

        h = QHBoxLayout(self)
        h.setContentsMargins(10, 6, 6, 6)
        h.setSpacing(8)

        side_color = T.TEAL if leg.is_long else T.YELLOW
        type_color = T.GREEN if leg.call_put == "C" else (T.RED if leg.call_put == "P" else T.MUTED)
        prem_color = T.GREEN if leg.credit_debit > 0 else (T.RED if leg.credit_debit < 0 else T.MUTED)

        cells = [
            (leg.direction_label.upper(),       side_color, 700, 56),
            (leg.type_label,                    type_color, 700, 54),
            (f"${leg.strike:g}" if leg.strike else "—", T.TEXT, 600, 70),
            (leg.expires_at.strftime("%b %d %y") if leg.expires_at else "—", T.TEXT_DIM, 400, 88),
            (f"{leg.quantity:g}",               T.TEXT,     500, 44),
            (money(leg.avg_open_price),         T.TEXT_DIM, 400, 64),
            (money(leg.mark_price),             T.TEXT,     500, 64),
            (money(leg.credit_debit, signed=True), prem_color, 600, 86),
            (money(leg.pnl, signed=True),       pnl_color(leg.pnl), 700, 92),
            (fmt_num(leg.delta, 2, signed=True), T.TEXT_DIM, 400, 52),
            (fmt_num(leg.theta, 2, signed=True), T.TEXT_DIM, 400, 52),
            (pct(leg.iv * 100 if leg.iv is not None else None, signed=False), T.TEXT_DIM, 400, 58),
        ]
        for text, color, weight, width in cells:
            l = QLabel(text)
            l.setFixedWidth(width)
            l.setStyleSheet(
                f"color: {color}; background: transparent; border: none; "
                f"font-size: 12px; font-weight: {weight};"
            )
            h.addWidget(l)

        h.addStretch()



# ── Leg header row (column titles) ──────────────────────────────────────────

class LegHeader(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 2, 6, 4)
        h.setSpacing(8)
        for label, width in LegRow.COLUMNS:
            l = QLabel(label.upper())
            l.setFixedWidth(width)
            l.setStyleSheet(
                f"color: {T.MUTED}; background: transparent; border: none; "
                f"font-size: 10px; font-weight: bold; letter-spacing: 0.6px;"
            )
            h.addWidget(l)
        h.addStretch()


# ── Strategy card ───────────────────────────────────────────────────────────

class StrategyCard(QFrame):
    renamed           = pyqtSignal(str, str)            # gid, new_name
    leg_dropped       = pyqtSignal(str, str)            # leg_symbol, target_gid
    leg_menu_request  = pyqtSignal(str, QPoint)         # leg_symbol, global_pos
    card_menu_request = pyqtSignal(str, QPoint)         # gid, global_pos

    def __init__(self, strategy, parent=None):
        super().__init__(parent)
        self.strategy = strategy
        self.expanded = False
        self.setObjectName("card")
        self._set_normal_style()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())
        outer.addWidget(self._build_body())
        self.body.setVisible(False)

    def _set_normal_style(self):
        self.setStyleSheet(
            f"QFrame#card {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 14px; }}"
            f"QFrame#card:hover {{ border-color: {T.BORDER_H}; }}"
        )

    def _set_drop_highlight(self):
        self.setStyleSheet(
            f"QFrame#card {{ background: {T.CARD}; border: 2px solid {T.PURPLE}; "
            f"border-radius: 14px; }}"
        )

    # ── Drag-drop target ────────────────────────────────────────────────

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME_LEG):
            event.acceptProposedAction()
            self._set_drop_highlight()

    def dragLeaveEvent(self, event):
        self._set_normal_style()

    def dropEvent(self, event):
        if event.mimeData().hasFormat(MIME_LEG):
            symbol = bytes(event.mimeData().data(MIME_LEG)).decode("utf-8")
            self.leg_dropped.emit(symbol, self.strategy.key)
            event.acceptProposedAction()
        self._set_normal_style()

    # ── Header ──────────────────────────────────────────────────────────

    def _build_header(self):
        s = self.strategy
        header = QFrame()
        header.setStyleSheet("background: transparent; border: none;")
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header.mousePressEvent = self._toggle_event

        h = QHBoxLayout(header)
        h.setContentsMargins(22, 16, 22, 16)
        h.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(4)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self.name_lbl = QLabel(s.name)
        self.name_lbl.setStyleSheet(
            f"color: {T.TEXT}; font-size: 16px; font-weight: bold; background: transparent; border: none;"
        )
        name_row.addWidget(self.name_lbl)
        if s.is_custom:
            name_row.addWidget(self._badge("Custom", T.PURPLE))
        name_row.addStretch()
        left.addLayout(name_row)

        sub_row = QHBoxLayout()
        sub_row.setSpacing(8)
        sub_row.addWidget(self._badge(s.root or "—", T.ACCENT))
        if not s.is_custom:
            sub_row.addWidget(self._badge(s.auto_name, T.MUTED, outlined=True))
        sub_row.addWidget(self._badge(f"{len(s.legs)} legs", T.MUTED, outlined=True))
        sub_row.addStretch()
        left.addLayout(sub_row)
        h.addLayout(left, 3)

        h.addWidget(self._stat("DTE", str(s.dte) if s.dte is not None else "—", dte_color(s.dte)))
        h.addWidget(self._stat("DIT", str(s.dit) if s.dit is not None else "—", T.TEXT_DIM))

        cd = s.credit_debit
        cd_color = T.GREEN if cd > 0 else (T.RED if cd < 0 else T.MUTED)
        cd_label = "Credit" if cd >= 0 else "Debit"
        h.addWidget(self._stat(cd_label, money(abs(cd)), cd_color))

        h.addWidget(self._stat("Open P&L", money(s.pnl, signed=True),
                                pnl_color(s.pnl), pct(s.pnl_pct)))

        # Card ⋮
        card_menu_btn = QPushButton("⋮")
        card_menu_btn.setFixedSize(24, 24)
        card_menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        card_menu_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; border: none; "
            f"font-size: 18px; font-weight: bold; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; }}"
        )
        card_menu_btn.clicked.connect(self._open_card_menu)
        card_menu_btn.setVisible(False)
        h.addWidget(card_menu_btn)
        self._card_menu_btn = card_menu_btn

        self.arrow = QLabel("▾")
        self.arrow.setStyleSheet(
            f"color: {T.MUTED}; font-size: 14px; background: transparent; border: none;"
        )
        h.addWidget(self.arrow)
        return header

    # ── Body ────────────────────────────────────────────────────────────

    def _build_body(self):
        s = self.strategy
        body = QFrame()
        body.setStyleSheet(
            f"QFrame {{ background: {T.BG_ALT}; border: none; "
            f"border-top: 1px solid {T.BORDER}; "
            f"border-bottom-left-radius: 14px; border-bottom-right-radius: 14px; }}"
        )
        lay = QVBoxLayout(body)
        lay.setContentsMargins(22, 16, 22, 18)
        lay.setSpacing(14)

        lay.addLayout(self._build_metrics_row())

        bottom = QHBoxLayout()
        bottom.setSpacing(16)

        legs_block = QFrame()
        legs_block.setStyleSheet("background: transparent; border: none;")
        legs_lay = QVBoxLayout(legs_block)
        legs_lay.setContentsMargins(0, 0, 0, 0)
        legs_lay.setSpacing(4)

        hint = QLabel("Drag legs between strategies  ·  ⋮ for more options")
        hint.setStyleSheet(
            f"color: {T.MUTED}; background: transparent; border: none; font-size: 10px; "
            f"font-style: italic; padding-left: 10px;"
        )
        legs_lay.addWidget(hint)
        legs_lay.addWidget(LegHeader())
        for leg in s.legs:
            row = LegRow(leg)
            row.move_requested.connect(self.leg_menu_request)
            legs_lay.addWidget(row)
        legs_block.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        bottom.addWidget(legs_block, 3)

        chart_container = QFrame()
        chart_container.setStyleSheet(
            f"background: {T.CARD}; border: 1px solid {T.BORDER}; border-radius: 10px;"
        )
        cl = QVBoxLayout(chart_container)
        cl.setContentsMargins(8, 8, 8, 8)
        title = QLabel("Payoff at Expiration")
        title.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; font-weight: bold; "
            f"background: transparent; border: none; padding-left: 4px;"
        )
        cl.addWidget(title)
        cl.addWidget(PayoffChart(s, height=2.6))
        chart_container.setMinimumWidth(380)
        bottom.addWidget(chart_container, 2)

        lay.addLayout(bottom)
        self.body = body
        return body

    def _build_metrics_row(self):
        s = self.strategy
        max_profit, max_loss, breakevens = strategy_extremes(s)
        pop = probability_of_profit(s)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(0)

        def add_metric(col, label, value, color=T.TEXT):
            grid.addWidget(self._metric_box(label, value, color), 0, col)

        add_metric(0, "Max Profit",
                    ("Unlimited" if max_profit == float("inf") else money(max_profit)),
                    T.GREEN)
        add_metric(1, "Max Loss",
                    ("Unlimited" if max_loss == float("-inf") else money(max_loss, signed=True)),
                    T.RED if max_loss and max_loss != 0 else T.MUTED)

        if max_loss is not None and max_loss != float("-inf"):
            cap_text = money(abs(max_loss))
        else:
            cap_text = "Undefined"
        add_metric(2, "Capital Req", cap_text, T.TEXT_DIM)

        be_text = "  /  ".join(f"${b:,.2f}" for b in breakevens[:2]) if breakevens else "—"
        add_metric(3, "Breakeven", be_text, T.TEXT_DIM)

        # POP
        if pop is None:
            pop_text, pop_color = "—", T.MUTED
        else:
            pop_text = f"{pop:.1f}%"
            pop_color = T.GREEN if pop >= 60 else (T.YELLOW if pop >= 40 else T.RED)
        add_metric(4, "POP", pop_text, pop_color)

        add_metric(5, "Δ Delta", fmt_num(s.net_delta, 2, signed=True),
                    pnl_color(s.net_delta) if s.net_delta else T.TEXT_DIM)
        add_metric(6, "Γ Gamma", fmt_num(s.net_gamma, 4, signed=True), T.TEXT_DIM)
        add_metric(7, "Θ Theta", fmt_num(s.net_theta, 2, signed=True),
                    pnl_color(s.net_theta) if s.net_theta else T.TEXT_DIM)
        add_metric(8, "V Vega", fmt_num(s.net_vega, 2, signed=True), T.TEXT_DIM)

        for i in range(9):
            grid.setColumnStretch(i, 1)
        return grid

    def _metric_box(self, label, value, color):
        w = QFrame()
        w.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 8px; }}"
        )
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(2)
        l = QLabel(label.upper())
        l.setStyleSheet(
            f"color: {T.MUTED}; font-size: 9px; font-weight: bold; letter-spacing: 0.5px; "
            f"background: transparent; border: none;"
        )
        lay.addWidget(l)
        v = QLabel(value)
        v.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        lay.addWidget(v)
        return w

    # ── Small helpers ───────────────────────────────────────────────────

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

    def _stat(self, label, value, color, sub=None):
        w = QFrame()
        w.setStyleSheet("background: transparent; border: none;")
        w.setFixedWidth(108)
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

        if sub:
            s = QLabel(sub)
            s.setStyleSheet(
                f"color: {color}; font-size: 11px; background: transparent; border: none;"
            )
            s.setAlignment(Qt.AlignmentFlag.AlignRight)
            lay.addWidget(s)
        return w

    # ── Events ──────────────────────────────────────────────────────────

    def _toggle_event(self, _event):
        self.expanded = not self.expanded
        self.body.setVisible(self.expanded)
        self.arrow.setText("▴" if self.expanded else "▾")

    def _open_card_menu(self):
        pos = self._card_menu_btn.mapToGlobal(QPoint(0, self._card_menu_btn.height()))
        self.card_menu_request.emit(self.strategy.key, pos)

    def rename_inline(self, new_name):
        """Called by the parent after persisting a rename."""
        self.strategy.custom_name = new_name or None
        self.name_lbl.setText(self.strategy.name)

    def prompt_rename(self):
        new, ok = QInputDialog.getText(
            self, "Rename strategy", "New name:", text=self.strategy.name
        )
        if ok:
            name = new.strip()
            self.rename_inline(name or None)
            self.renamed.emit(self.strategy.key, name)
