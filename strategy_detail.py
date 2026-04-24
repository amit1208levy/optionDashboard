"""Full-page strategy detail: metrics, Greeks, legs, payoff chart, history."""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QScrollArea, QSizePolicy, QInputDialog, QDialog, QSlider,
    QMessageBox, QDoubleSpinBox, QDialogButtonBox, QLineEdit, QCheckBox,
)

import api
import theme as T

# ── Leg column definitions ───────────────────────────────────────────────────
# New layout: ⠿ | Ticker | Strike | Exp | P&L | Day | Qty | Open | Extrinsic | Greeks

_BASE_LEG_COLUMNS = [
    ("",          24),   # drag-handle placeholder (no header text)
    ("Ticker",    68),   # underlying root + C/P type
    ("Strike",    72),
    ("Exp",       82),
    ("P&L",       90),
    ("Day",       80),   # day P&L (mark − prior close)
    ("Qty",       52),
    ("Open",      84),   # net premium at open (credit/debit)
    ("Extrinsic", 76),   # current time-value of the option
]

# IV removed; only Δ Θ Γ V remain
_GREEK_COL_DEFS = {
    "delta": ("Δ", 52),
    "theta": ("Θ", 52),
    "gamma": ("Γ", 52),
    "vega":  ("V", 52),
}
_GREEK_ORDER = ["delta", "theta", "gamma", "vega"]


def _active_leg_columns():
    """Return (columns, enabled_greeks) respecting the user's settings."""
    settings = api.load_settings()
    enabled  = [k for k in settings.get("leg_greeks", ["delta", "theta"])
                if k in _GREEK_COL_DEFS]   # silently drop 'iv' from old saves
    cols = list(_BASE_LEG_COLUMNS)
    for key in _GREEK_ORDER:
        if key in enabled:
            cols.append(_GREEK_COL_DEFS[key])
    return cols, enabled


# ── Per-leg helpers ──────────────────────────────────────────────────────────

def _fmt_greek(v, signed=True):
    """
    Format a Greek value with auto-scaling precision.
    Avoids the "−0.00" display artifact for small values.
    """
    if v is None:
        return "—"
    abs_v = abs(v)
    if abs_v == 0:
        return "0"
    if abs_v < 0.0005:
        return "~0"
    if abs_v < 0.01:
        dp = 4
    elif abs_v < 0.1:
        dp = 3
    elif abs_v < 10:
        dp = 2
    else:
        dp = 1
    fmt = f"{v:+.{dp}f}" if signed else f"{v:.{dp}f}"
    return fmt


def _extrinsic_value(leg):
    """
    Time value (extrinsic) of the option at current mark.
    = mark − max(0, intrinsic).  Returns 0 for stock legs.
    """
    if not leg.is_option or leg.mark_price <= 0:
        return 0.0
    und = leg.underlying_price
    if und is None:
        return leg.mark_price   # no underlying → all extrinsic
    if leg.call_put == "C":
        intrinsic = max(0.0, und - (leg.strike or 0))
    else:
        intrinsic = max(0.0, (leg.strike or 0) - und)
    return max(0.0, leg.mark_price - intrinsic)


def _day_pnl_leg(leg):
    """Day P&L for one leg: sign × qty × mult × (mark − prior_close)."""
    if not leg.close_price:
        return None
    return leg.sign * leg.quantity * leg.multiplier * (leg.mark_price - leg.close_price)


from models import (
    StrategyInstance, strategy_extremes, probability_of_profit, capital_for_strategy,
    strategy_performance, symbol_ivr, symbol_ivp, symbol_beta, symbol_hv30,
    scenario_pnl, distributed_delta_dte_capital,
    unassigned_positions, group_unassigned, check_exit_conditions,
)
from payoff_chart import PayoffChart
from history_chart import HistoryChart
from strategy_card import money, pct, fmt_num, pnl_color, dte_color
from strategies_page import PastLegPickerDialog


# ── Drag handle ──────────────────────────────────────────────────────────────

class _DragHandle(QLabel):
    """⠿ icon; pressing it initiates a row-drag in _LegsBody."""
    pressed = pyqtSignal(int)   # global-Y at press time

    def __init__(self, parent=None):
        super().__init__("⠿", parent)
        self.setFixedWidth(24)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setStyleSheet(
            f"color: {T.MUTED}; border: none; background: transparent; "
            f"font-size: 13px; letter-spacing: 0;"
        )
        self.setToolTip("Drag to reorder this leg")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.pressed.emit(int(e.globalPosition().y()))
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(e)


# ── Leg header row ───────────────────────────────────────────────────────────

class LegHeader(QFrame):
    def __init__(self, columns, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 4, 6, 6)
        h.setSpacing(0)
        for label, width in columns:
            l = QLabel(label.upper() if label else "")
            l.setFixedWidth(width)
            l.setStyleSheet(
                f"color: {T.MUTED}; background: transparent; border: none; "
                f"font-size: 10px; font-weight: bold; letter-spacing: 0.6px;"
            )
            h.addWidget(l)
        h.addStretch()


# ── Single leg row ───────────────────────────────────────────────────────────

class LegRow(QFrame):
    drag_started = pyqtSignal(object, int)   # (self, global_y)

    _NORMAL = (
        "QFrame {{ background: {card}; border: 1px solid {border}; border-radius: 8px; }}"
        "QFrame:hover {{ border-color: {bh}; background: #1d2034; }}"
    )
    _DRAGGING = (
        "QFrame {{ background: #2a2e4a; border: 1px solid {purple}; "
        "border-radius: 8px; }}"
    )

    def __init__(self, leg, enabled_greeks, parent=None):
        super().__init__(parent)
        self.leg = leg
        self.setFixedHeight(44)
        self._set_style(False)

        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 6, 0)
        h.setSpacing(0)

        # ── Drag handle ───────────────────────────────────────────────────
        handle = _DragHandle()
        handle.pressed.connect(lambda y: self.drag_started.emit(self, y))
        h.addWidget(handle)

        # ── Data cells ────────────────────────────────────────────────────
        type_color = (T.GREEN if leg.call_put == "C"
                      else T.RED if leg.call_put == "P"
                      else T.MUTED)
        prem_color = (T.GREEN if leg.credit_debit > 0
                      else T.RED if leg.credit_debit < 0
                      else T.MUTED)
        side_tag   = " S" if not leg.is_long else " L"

        day  = _day_pnl_leg(leg)
        ext  = _extrinsic_value(leg)
        ticker_text = f"{leg.root or '—'}{side_tag}"

        # (text, color, weight, width)
        base_cells = [
            (ticker_text,
             type_color,                        700, 68),
            (f"${leg.strike:g}" if leg.strike else "—",
             T.TEXT,                            600, 72),
            (leg.expires_at.strftime("%b %d %y") if leg.expires_at else "—",
             T.TEXT_DIM,                        400, 82),
            (money(leg.pnl, signed=True),
             pnl_color(leg.pnl),                700, 90),
            (money(day, signed=True) if day is not None else "—",
             pnl_color(day) if day is not None else T.MUTED, 600, 80),
            (f"{leg.quantity:g}",
             T.TEXT,                            500, 52),
            (money(leg.credit_debit, signed=True),
             prem_color,                        600, 84),
            (f"${ext:.3f}" if ext > 0 else "—",
             T.TEXT_DIM,                        500, 76),
        ]
        for text, color, weight, width in base_cells:
            l = QLabel(text)
            l.setFixedWidth(width)
            l.setStyleSheet(
                f"color: {color}; background: transparent; border: none; "
                f"font-size: 12px; font-weight: {weight};"
            )
            h.addWidget(l)

        # ── Greek cells ───────────────────────────────────────────────────
        greek_vals = {
            "delta": _fmt_greek(leg.delta),
            "theta": _fmt_greek(leg.theta),
            "gamma": _fmt_greek(leg.gamma),
            "vega":  _fmt_greek(leg.vega),
        }
        for key in _GREEK_ORDER:
            if key in enabled_greeks:
                _, width = _GREEK_COL_DEFS[key]
                l = QLabel(greek_vals[key])
                l.setFixedWidth(width)
                l.setStyleSheet(
                    f"color: {T.TEXT_DIM}; background: transparent; border: none; "
                    f"font-size: 12px; font-weight: 400;"
                )
                h.addWidget(l)

        h.addStretch()

    def _set_style(self, dragging):
        if dragging:
            self.setStyleSheet(
                self._DRAGGING.format(purple=T.PURPLE)
            )
        else:
            self.setStyleSheet(
                self._NORMAL.format(
                    card=T.CARD, border=T.BORDER, bh=T.BORDER_H
                )
            )

    def set_dragging(self, on: bool):
        self._set_style(on)


# ── Reorderable legs body ────────────────────────────────────────────────────

class _LegsBody(QWidget):
    """
    VBox of LegRows with grab-mouse drag-to-reorder.

    The user grabs the ⠿ handle; _LegsBody captures all mouse events via
    grabMouse(), reorders rows live on mouse-move, and emits `reordered`
    (new symbol list) on mouse-release so the caller can persist the order.
    """
    reordered = pyqtSignal(list)   # [symbol, ...] in new order

    def __init__(self, legs, enabled_greeks, parent=None):
        super().__init__(parent)
        self._rows: list[LegRow] = []
        self._dragging: LegRow | None = None

        self._lay = QVBoxLayout(self)
        self._lay.setSpacing(4)
        self._lay.setContentsMargins(0, 0, 0, 0)

        for leg in legs:
            row = LegRow(leg, enabled_greeks)
            row.drag_started.connect(self._on_drag_started)
            self._rows.append(row)
            self._lay.addWidget(row)

    # ── Drag lifecycle ───────────────────────────────────────────────────────

    def _on_drag_started(self, row: LegRow, _global_y: int):
        if len(self._rows) < 2:
            return                     # nothing to reorder
        self._dragging = row
        row.set_dragging(True)
        self.grabMouse()               # all subsequent events come here

    def mouseMoveEvent(self, e):
        if self._dragging is None:
            return
        local_y = e.pos().y()
        # Find target index: insert before the row whose midpoint is below cursor
        target = len(self._rows) - 1
        for i, row in enumerate(self._rows):
            if local_y < row.pos().y() + row.height() // 2:
                target = i
                break

        curr = next((i for i, r in enumerate(self._rows) if r is self._dragging), -1)
        if curr < 0 or target == curr:
            return

        # Rebuild rows list with dragged row at target position
        others = [r for r in self._rows if r is not self._dragging]
        others.insert(min(target, len(others)), self._dragging)
        self._rows = others

        # Reflect new order in the layout (remove-all then re-add is simplest)
        for row in self._rows:
            self._lay.removeWidget(row)
        for row in self._rows:
            self._lay.addWidget(row)

        e.accept()

    def mouseReleaseEvent(self, e):
        if self._dragging is None:
            return
        self._dragging.set_dragging(False)
        self._dragging = None
        self.releaseMouse()
        self.reordered.emit([r.leg.symbol for r in self._rows])
        e.accept()


# ── Detail page ─────────────────────────────────────────────────────────────

class StrategyDetailPage(QWidget):
    back_requested   = pyqtSignal()
    reopen_requested = pyqtSignal(object)   # strategy — re-create the page

    def __init__(self, strategy, portfolio, parent=None):
        super().__init__(parent)
        self.strategy  = strategy
        self.portfolio = portfolio
        self.setStyleSheet(T.BASE_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body_w = QWidget()
        body_w.setStyleSheet(f"background: {T.BG};")
        body = QVBoxLayout(body_w)
        body.setContentsMargins(32, 24, 32, 40)
        body.setSpacing(18)

        body.addLayout(self._build_summary_row())
        body.addWidget(self._build_metrics_card())
        body.addWidget(self._build_greeks_card())
        mkt_card = self._build_market_card()
        if mkt_card:
            body.addWidget(mkt_card)
        body.addWidget(self._build_chart_card())
        body.addWidget(self._build_legs_card())

        # Leg groups — only for saved strategies (sub-groupings need persistence)
        if isinstance(self.strategy, StrategyInstance):
            body.addWidget(self._build_leg_groups_card())

        if isinstance(self.strategy, StrategyInstance):
            body.addWidget(self._build_exit_plan_card())
            body.addWidget(self._build_history_card())

        tmpl_card = self._build_template_card()
        if tmpl_card:
            body.addWidget(tmpl_card)                # About at bottom

        body.addStretch()
        scroll.setWidget(body_w)
        root.addWidget(scroll)

    # ── Header ──────────────────────────────────────────────────────────────

    def _build_header(self):
        header = QFrame()
        header.setFixedHeight(60)
        header.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border-bottom: 1px solid {T.BORDER}; border-radius: 0; }}"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(28, 0, 28, 0)
        hl.setSpacing(16)

        back = QPushButton("←  Back")
        back.setFixedHeight(32)
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 0 12px; font-size: 12px; }}"
            f"QPushButton:hover {{ color: {T.TEXT}; border-color: {T.ACCENT}; }}"
        )
        back.clicked.connect(self.back_requested.emit)
        hl.addWidget(back)

        title = QLabel(self.strategy.name)
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 17px; font-weight: bold; border: none; background: transparent;"
        )
        hl.addWidget(title)
        hl.addStretch()

        whatif = QPushButton("✦  What-if")
        whatif.setFixedHeight(32)
        whatif.setCursor(Qt.CursorShape.PointingHandCursor)
        whatif.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: white; border: none; "
            f"border-radius: 6px; padding: 0 14px; font-size: 12px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
        )
        whatif.clicked.connect(self._open_whatif)
        hl.addWidget(whatif)
        return header

    def _metrics_for_root(self):
        acct = self.portfolio.current_account() if self.portfolio else None
        if not acct:
            return None
        return (acct.get("metrics") or {}).get(self.strategy.root)

    def _build_market_card(self):
        m = self._metrics_for_root()
        if not m:
            return None

        ivr  = symbol_ivr(m)
        ivp  = symbol_ivp(m)
        hv30 = symbol_hv30(m)
        beta = symbol_beta(m)

        if all(v is None for v in (ivr, ivp, hv30, beta)):
            return None

        frame, lay = self._section_frame(f"Market Stats — {self.strategy.root}")
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)

        def ivr_color(v):
            if v is None: return T.MUTED
            return T.GREEN if v >= 50 else (T.YELLOW if v >= 25 else T.RED)

        items = [
            ("IV Rank",       f"{ivr:.0f}"  if ivr  is not None else "—", ivr_color(ivr)),
            ("IV Percentile", f"{ivp:.0f}"  if ivp  is not None else "—", ivr_color(ivp)),
            ("HV (30d)",      f"{hv30:.1f}%" if hv30 is not None else "—", T.TEXT),
            ("Beta",          f"{beta:.2f}" if beta is not None else "—", T.TEXT),
        ]
        for i, (k, v, c) in enumerate(items):
            grid.addWidget(self._metric_box(k, v, c), 0, i)
            grid.setColumnStretch(i, 1)
        lay.addLayout(grid)
        return frame

    def _open_whatif(self):
        dlg = WhatIfDialog(self.strategy, self)
        dlg.exec()

    # ── Summary tiles ───────────────────────────────────────────────────────

    def _build_summary_row(self):
        s = self.strategy
        row = QHBoxLayout()
        row.setSpacing(12)

        row.addWidget(self._big_tile(
            "OPEN P&L",
            money(s.pnl, signed=True),
            pnl_color(s.pnl),
            sub=pct(s.pnl_pct),
        ))

        pop = probability_of_profit(s)
        if pop is None:
            pop_text, pop_c = "—", T.MUTED
        else:
            pop_text = f"{pop:.1f}%"
            pop_c = T.GREEN if pop >= 60 else (T.YELLOW if pop >= 40 else T.RED)
        row.addWidget(self._big_tile("PROB. OF PROFIT", pop_text, pop_c))

        row.addWidget(self._big_tile(
            "DTE",
            str(s.dte) if s.dte is not None else "—",
            T.TEXT,
        ))

        dit_text = f"{s.dit}d" if s.dit is not None else "—"
        row.addWidget(self._big_tile("DAYS IN TRADE", dit_text, T.TEXT))

        cd = s.credit_debit
        cd_color = T.GREEN if cd > 0 else (T.RED if cd < 0 else T.MUTED)
        cd_label = "NET CREDIT" if cd >= 0 else "NET DEBIT"
        row.addWidget(self._big_tile(cd_label, money(abs(cd)), cd_color))

        return row

    def _big_tile(self, label, value, color, sub=None):
        w = QFrame()
        w.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 12px; }}"
        )
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 14, 20, 16)
        lay.setSpacing(4)
        l = QLabel(label.upper())
        l.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.7px; "
            f"border: none; background: transparent;"
        )
        lay.addWidget(l)
        v = QLabel(value)
        v.setStyleSheet(
            f"color: {color}; font-size: 22px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        lay.addWidget(v)
        if sub:
            sl = QLabel(sub)
            sl.setStyleSheet(
                f"color: {color}; font-size: 12px; border: none; background: transparent;"
            )
            lay.addWidget(sl)
        return w

    # ── Metrics card ────────────────────────────────────────────────────────

    def _build_metrics_card(self):
        from collections import OrderedDict
        from models import Strategy as _Strategy

        # Group legs by underlying so multi-ticker strategies can show
        # per-underlying risk metrics (max profit / loss / breakeven /
        # capital requirement are only meaningful against a single
        # underlying's price axis).
        groups: "OrderedDict[str, list]" = OrderedDict()
        for leg in self.strategy.legs:
            root = leg.root or leg.underlying or "—"
            groups.setdefault(root, []).append(leg)

        frame, lay = self._section_frame("Risk Metrics")

        # For single-ticker: show the normal Capital Required box at the top
        # (keeps the existing delta/DTE distribution, override support, etc.).
        # For multi-ticker: compute capital per ticker using the same algorithm
        # and show a TOTAL box at the top plus per-ticker values in each row.
        if len(groups) <= 1:
            cap_required, cap_source = self._capital_required_with_source()
            cap_row = QHBoxLayout()
            cap_row.addWidget(self._capital_box(cap_required, cap_source))
            cap_row.addStretch()
            lay.addLayout(cap_row)
            per_ticker_cap: dict = {}
        else:
            # Compute per-ticker capital (same algorithm as the single case)
            per_ticker_cap = {}
            for root, legs in groups.items():
                sub = _Strategy(f"{self.strategy.key}:{root}", legs,
                                custom_name=root, is_custom=True)
                per_ticker_cap[root] = capital_for_strategy(sub) or 0.0
            total_cap = sum(per_ticker_cap.values())

            cap_row = QHBoxLayout()
            cap_row.addWidget(self._capital_box(total_cap, "total"))
            cap_row.addStretch()
            lay.addLayout(cap_row)

        # Per-underlying: Max Profit | Max Loss | Breakeven | Capital (if multi)
        for root, legs in groups.items():
            if len(groups) > 1:
                sub_label = QLabel(root)
                sub_label.setStyleSheet(
                    f"color: {T.ACCENT}; font-size: 12px; font-weight: bold; "
                    f"border: none; background: transparent; padding-top: 8px;"
                )
                lay.addWidget(sub_label)

            sub_strategy = _Strategy(
                f"{self.strategy.key}:{root}",
                legs,
                custom_name=root,
                is_custom=True,
            )
            max_profit, max_loss, breakevens = strategy_extremes(sub_strategy)

            grid = QGridLayout()
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(6)

            def cell(col, label, value, color=T.TEXT):
                grid.addWidget(self._metric_box(label, value, color), 0, col)

            cell(0, "Max Profit",
                 "Unlimited" if max_profit == float("inf") else money(max_profit),
                 T.GREEN)
            cell(1, "Max Loss",
                 "Unlimited" if max_loss == float("-inf") else money(max_loss, signed=True),
                 T.RED if max_loss and max_loss != 0 else T.MUTED)
            be_text = "  /  ".join(f"${b:,.2f}" for b in breakevens[:2]) if breakevens else "—"
            cell(2, "Breakeven", be_text, T.TEXT_DIM)

            cols = 3
            if len(groups) > 1:
                cap = per_ticker_cap.get(root, 0.0)
                cell(3, "Capital Req.", money(cap) if cap else "—", T.TEXT_DIM)
                cols = 4

            for i in range(cols):
                grid.setColumnStretch(i, 1)
            lay.addLayout(grid)

        return frame

    # ── Capital required (auto + override) ──────────────────────────────────

    def _capital_required(self):
        """Return the active capital requirement value."""
        val, _ = self._capital_required_with_source()
        return val

    def _capital_required_with_source(self):
        """
        Returns (value, source) where source is one of:
          'override'   – user-set manual override
          'max_loss'   – defined-risk strategy (exact max loss from payoff diagram)
          'rough_est'  – delta/DTE-weighted share of total per-ticker capital pool
          None         – no value available
        """
        override = self._capital_override()
        if override is not None:
            return override, "override"

        _, max_loss, _ = strategy_extremes(self.strategy)
        if max_loss is not None and max_loss != float("-inf"):
            return abs(max_loss), "max_loss"

        # Undefined-risk: distribute per-ticker capital pool weighted by delta×DTE
        dist = self._get_delta_dte_capital()
        if dist is not None and dist > 0:
            return dist, "rough_est"

        # Last-resort fallback (all-long or no Greeks at all)
        est = capital_for_strategy(self.strategy)
        return (est if est else None), "rough_est"

    def _get_delta_dte_capital(self):
        """
        Distribute the total capital for this strategy's root ticker
        proportionally across all strategies on that root, weighted by
        each strategy's largest short-leg |delta| × DTE × qty.

        Works for both equity and futures options — no special-casing needed.
        Passes the broker's reported futures-margin-requirement so the pool
        for futures roots is anchored to the actual account margin.
        """
        acct = self.portfolio.current_account() if self.portfolio else None
        if not acct:
            return None

        positions  = acct["positions"]
        strat_raw  = self.portfolio.strategies_raw
        instances  = [StrategyInstance(d, positions) for d in strat_raw]
        leftover   = unassigned_positions(positions, strat_raw)
        unassigned = group_unassigned(leftover)

        try:
            total_fm = float(acct["balances"].get("futures-margin-requirement") or 0)
        except (TypeError, ValueError):
            total_fm = 0.0

        return distributed_delta_dte_capital(
            self.strategy, instances, unassigned,
            futures_margin_total=total_fm,
        )

    def _capital_override(self):
        if not isinstance(self.strategy, StrategyInstance):
            return None
        raw = next(
            (r for r in self.portfolio.strategies_raw if r["id"] == self.strategy.id),
            None,
        )
        if not raw:
            return None
        v = raw.get("capital_override")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _capital_box(self, cap_required, source="estimate"):
        is_override = (source == "override")
        border_color = T.PURPLE if is_override else T.BORDER
        w = QFrame()
        w.setStyleSheet(
            f"QFrame {{ background: #12151d; border: 1px solid "
            f"{border_color}; border-radius: 8px; }}"
        )
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(4)
        label_text = "TOTAL CAPITAL REQUIRED" if source == "total" else "CAPITAL REQUIRED"
        l = QLabel(label_text)
        l.setStyleSheet(
            f"color: {T.MUTED}; font-size: 9px; font-weight: bold; letter-spacing: 0.5px; "
            f"background: transparent; border: none;"
        )
        top.addWidget(l)
        top.addStretch()
        # Hide the manual-edit button when showing the multi-ticker total
        # (overrides are a single-ticker concept in the strategy config)
        if isinstance(self.strategy, StrategyInstance) and source != "total":
            edit = QPushButton("✎")
            edit.setFixedSize(18, 18)
            edit.setCursor(Qt.CursorShape.PointingHandCursor)
            edit.setToolTip("Set capital requirement manually")
            edit.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T.MUTED}; "
                f"border: none; font-size: 11px; }}"
                f"QPushButton:hover {{ color: {T.ACCENT}; }}"
            )
            edit.clicked.connect(self._edit_capital)
            top.addWidget(edit)
        lay.addLayout(top)

        if cap_required is None:
            text  = "Undefined"
            color = T.MUTED
        elif source == "rough_est":
            text  = "~" + money(cap_required)    # tilde = rough estimate
            color = T.TEXT_DIM
        else:
            text  = money(cap_required)
            color = T.PURPLE if is_override else T.TEXT_DIM
        v = QLabel(text)
        v.setStyleSheet(
            f"color: {color}; font-size: 15px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        lay.addWidget(v)

        # Source badge
        if source == "override":
            badge_text, badge_color = "manual", T.PURPLE
        elif source == "rough_est":
            badge_text, badge_color = "~roughly calculated", T.MUTED
        else:
            # max_loss → no badge (it's exact, self-explanatory)
            badge_text, badge_color = None, None

        if badge_text:
            badge = QLabel(badge_text)
            badge.setStyleSheet(
                f"color: {badge_color}; font-size: 9px; font-weight: bold; "
                f"background: transparent; border: none;"
            )
            lay.addWidget(badge)
        return w

    def _edit_capital(self):
        current = self._capital_required() or 0.0
        val, ok = QInputDialog.getDouble(
            self, "Capital Required",
            "Enter capital required for this strategy (0 to reset to auto):",
            value=current, min=0.0, max=1e9, decimals=2,
        )
        if not ok:
            return
        raw = next(
            (r for r in self.portfolio.strategies_raw if r["id"] == self.strategy.id),
            None,
        )
        if raw is None:
            return
        if val <= 0:
            raw.pop("capital_override", None)
        else:
            raw["capital_override"] = float(val)
        self.portfolio.save_strategies()
        # Rebuild metrics card in place
        self._rebuild()

    def _rebuild(self):
        self.reopen_requested.emit(self.strategy)

    # ── Greeks card ─────────────────────────────────────────────────────────

    def _build_greeks_card(self):
        s = self.strategy
        frame, lay = self._section_frame("Net Greeks")

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)

        # Beta-weighted delta
        m    = self._metrics_for_root()
        beta = symbol_beta(m) if m else None
        bwd  = (s.net_delta * beta
                if (s.net_delta is not None and beta is not None)
                else None)

        items = [
            ("Δ Delta",  _fmt_greek(s.net_delta),
                pnl_color(s.net_delta) if s.net_delta else T.TEXT_DIM),
            ("Θ Theta",  _fmt_greek(s.net_theta),
                pnl_color(s.net_theta) if s.net_theta else T.TEXT_DIM),
            ("V Vega",   _fmt_greek(s.net_vega),  T.TEXT_DIM),
            ("β×Δ BWD",  _fmt_greek(bwd) if bwd is not None else "—", T.TEXT_DIM),
        ]
        for i, (label, value, color) in enumerate(items):
            grid.addWidget(self._metric_box(label, value, color), 0, i)
            grid.setColumnStretch(i, 1)

        lay.addLayout(grid)
        return frame

    # ── Template info (only for StrategyInstance) ───────────────────────────

    def _build_template_card(self):
        if not isinstance(self.strategy, StrategyInstance):
            return None
        tmpl = self.strategy.template
        if not tmpl:
            return None

        frame, lay = self._section_frame(f"About — {tmpl.name}")

        sub = QLabel(f"{tmpl.category}  ·  {tmpl.outlook}  ·  {tmpl.risk} risk")
        sub.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none; background: transparent;"
        )
        lay.addWidget(sub)

        desc = QLabel(tmpl.description)
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {T.TEXT_DIM}; font-size: 13px; border: none; background: transparent; "
            f"margin-top: 6px;"
        )
        lay.addWidget(desc)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(4)
        fields = [
            ("Setup",      tmpl.setup),
            ("Max profit", tmpl.max_profit),
            ("Max loss",   tmpl.max_loss),
            ("Capital",    tmpl.capital_note),
            ("Ideal when", tmpl.ideal_when),
        ]
        for i, (k, v) in enumerate(fields):
            kl = QLabel(k.upper())
            kl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.5px; "
                f"border: none; background: transparent;"
            )
            vl = QLabel(v)
            vl.setWordWrap(True)
            vl.setStyleSheet(
                f"color: {T.TEXT_DIM}; font-size: 12px; border: none; background: transparent;"
            )
            grid.addWidget(kl, i, 0)
            grid.addWidget(vl, i, 1)
        grid.setColumnStretch(1, 1)
        lay.addSpacing(6)
        lay.addLayout(grid)
        return frame

    # ── Payoff chart ────────────────────────────────────────────────────────

    def _build_chart_card(self):
        # Group legs by underlying root — multi-ticker strategies get one
        # payoff chart per underlying, since each curve is only meaningful
        # against a single underlying's price axis.
        from collections import OrderedDict
        from models import Strategy as _Strategy
        groups: "OrderedDict[str, list]" = OrderedDict()
        for leg in self.strategy.legs:
            root = leg.root or leg.underlying or "—"
            groups.setdefault(root, []).append(leg)

        # Single ticker → original behavior
        if len(groups) <= 1:
            frame, lay = self._section_frame("Payoff at Expiration")
            chart = PayoffChart(self.strategy, height=3.6)
            chart.setMinimumHeight(320)
            lay.addWidget(chart)
            return frame

        # Multi-ticker → one chart per underlying, stacked vertically
        frame, lay = self._section_frame(
            f"Payoff at Expiration ({len(groups)} underlyings)"
        )
        for root, legs in groups.items():
            sub_label = QLabel(root)
            sub_label.setStyleSheet(
                f"color: {T.ACCENT}; font-size: 12px; font-weight: bold; "
                f"border: none; background: transparent; padding-top: 10px;"
            )
            lay.addWidget(sub_label)

            sub_strategy = _Strategy(
                f"{self.strategy.key}:{root}",
                legs,
                custom_name=root,
                is_custom=True,
            )
            chart = PayoffChart(sub_strategy, height=3.0)
            chart.setMinimumHeight(260)
            lay.addWidget(chart)
        return frame

    # ── Legs table ──────────────────────────────────────────────────────────

    def _build_legs_card(self):
        columns, enabled_greeks = _active_leg_columns()
        frame, lay = self._section_frame(f"Legs ({len(self.strategy.legs)})")

        # + Add Leg button (only for saved instances)
        if isinstance(self.strategy, StrategyInstance) and self.portfolio:
            btn_row = QHBoxLayout()
            btn_row.addStretch()
            add_btn = QPushButton("+ Add Leg")
            add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            add_btn.setFixedHeight(28)
            add_btn.setStyleSheet(
                f"QPushButton {{ background: {T.PURPLE}; color: white; border: none; "
                f"border-radius: 6px; padding: 0 14px; font-size: 11px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
            )
            add_btn.clicked.connect(self._on_add_leg)
            btn_row.addWidget(add_btn)
            lay.addLayout(btn_row)

        lay.addWidget(LegHeader(columns))

        # Apply user-defined display order if one has been saved
        legs = list(self.strategy.legs)
        if isinstance(self.strategy, StrategyInstance) and self.portfolio:
            raw = next(
                (r for r in self.portfolio.strategies_raw
                 if r["id"] == self.strategy.id), None
            )
            if raw and raw.get("leg_order"):
                order = {sym: i for i, sym in enumerate(raw["leg_order"])}
                legs.sort(key=lambda l: order.get(l.symbol, 999))

        body = _LegsBody(legs, enabled_greeks)
        if isinstance(self.strategy, StrategyInstance):
            body.reordered.connect(self._on_legs_reordered)
        lay.addWidget(body)
        return frame

    def _on_add_leg(self):
        """Pick an unassigned portfolio leg and attach it to this strategy."""
        if not isinstance(self.strategy, StrategyInstance) or not self.portfolio:
            return

        positions = self.portfolio.current_positions()
        strat_raw = self.portfolio.strategies_raw
        # Find legs not already in THIS strategy
        mine = set(self.strategy.leg_symbols)
        assigned_anywhere = set()
        owner = {}
        for raw in strat_raw:
            for sym in raw.get("legs", []) or []:
                assigned_anywhere.add(sym)
                owner[sym] = raw.get("name") or raw.get("id", "?")

        candidates = [p for p in positions if p.symbol not in mine]
        if not candidates:
            QMessageBox.information(
                self, "Add Leg",
                "No other positions available to add."
            )
            return

        dlg = _AddLegDialog(candidates, owner, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        symbols = dlg.result_symbols()
        if not symbols:
            return

        # Update persistence: append to this strategy, remove from any others
        raw = next(
            (r for r in self.portfolio.strategies_raw if r["id"] == self.strategy.id),
            None,
        )
        if raw is None:
            return
        for sym in symbols:
            # Remove from other strategies first
            for other in self.portfolio.strategies_raw:
                if other["id"] == raw["id"]:
                    continue
                if sym in (other.get("legs") or []):
                    other["legs"] = [s for s in other["legs"] if s != sym]
            # Append to this one
            if sym not in raw["legs"]:
                raw["legs"].append(sym)
        self.portfolio.save_strategies()
        self.reopen_requested.emit(self.strategy)

    def _on_legs_reordered(self, new_symbols: list):
        """Persist the user-defined leg display order to the strategies JSON."""
        if not isinstance(self.strategy, StrategyInstance) or not self.portfolio:
            return
        raw = next(
            (r for r in self.portfolio.strategies_raw
             if r["id"] == self.strategy.id), None
        )
        if raw is None:
            return
        raw["leg_order"] = new_symbols
        self.portfolio.save_strategies()

    # ── Exit Plan ────────────────────────────────────────────────────────────

    def _build_exit_plan_card(self):
        """Editable exit-plan card: profit target, stop loss, DTE, price bounds."""
        frame, lay = self._section_frame("Exit Plan")
        ep = self.strategy.exit_plan if isinstance(self.strategy, StrategyInstance) else {}
        conds = check_exit_conditions(self.strategy, ep)
        cond_map = {c["type"]: c for c in conds}

        credit = self.strategy.credit_debit
        ref    = abs(credit) if credit else None
        underlying = next(
            (l.underlying_price for l in self.strategy.legs if l.underlying_price), None
        )

        # ── Field builder helper ────────────────────────────────────────────
        def _row(label, ep_key, default_val, suffix, decimals, tooltip,
                 context_fn=None, cond_type=None):
            """Returns a QHBoxLayout row with a spinner and live status."""
            hl = QHBoxLayout()
            hl.setSpacing(10)

            lbl = QLabel(label.upper())
            lbl.setFixedWidth(130)
            lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; font-weight: bold; "
                f"letter-spacing: 0.5px; border: none;"
            )
            hl.addWidget(lbl)

            spin = QDoubleSpinBox()
            spin.setDecimals(decimals)
            spin.setMinimum(0.0)
            spin.setMaximum(999999.0)

            # Step size: adapt to the magnitude of the price (or default_val)
            ref_price = float(default_val or 0)
            if decimals == 0:
                step = 1.0
            elif ref_price >= 1000:
                step = 5.0
            elif ref_price >= 100:
                step = 0.5
            elif ref_price >= 10:
                step = 0.1
            elif ref_price >= 1:
                step = 0.01
            else:
                step = 0.0001
            spin.setSingleStep(step)

            # Use saved value if set; otherwise fall back to default_val
            # (for Stop Below/Above this is the current underlying price)
            saved = ep.get(ep_key)
            spin.setValue(float(saved) if saved else float(default_val or 0))
            spin.setToolTip(tooltip)
            spin.setFixedWidth(100)
            spin.setStyleSheet(
                f"QDoubleSpinBox {{ background: {T.BG_ALT}; color: {T.TEXT}; "
                f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 3px 6px; "
                f"font-size: 13px; }}"
                f"QDoubleSpinBox:focus {{ border-color: {T.ACCENT}; }}"
            )
            hl.addWidget(spin)

            suf_lbl = QLabel(suffix)
            suf_lbl.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
            hl.addWidget(suf_lbl)

            # Context text (target dollar amount, etc.)
            ctx_lbl = QLabel("")
            ctx_lbl.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 11px; border: none;")
            hl.addWidget(ctx_lbl)

            hl.addStretch()

            # Status dot
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {T.MUTED}; font-size: 14px; border: none;")
            hl.addWidget(dot)

            # Populate dot and context from current condition
            def _refresh_status():
                v = spin.value()
                # Update context
                if context_fn:
                    ctx_lbl.setText(context_fn(v))
                # Recompute condition live
                tmp_ep = dict(ep); tmp_ep[ep_key] = v if v > 0 else None
                tmp_conds = {c["type"]: c for c in check_exit_conditions(self.strategy, tmp_ep)}
                c = tmp_conds.get(cond_type) if cond_type else None
                if c is None or v == 0:
                    dot.setText("●")
                    dot.setStyleSheet(f"color: {T.MUTED}; font-size: 14px; border: none;")
                elif c["severity"] == "hit":
                    dot.setText("⚡")
                    dot.setStyleSheet(f"color: {T.RED}; font-size: 14px; border: none;")
                elif c["severity"] == "near":
                    dot.setText("●")
                    dot.setStyleSheet(f"color: {T.YELLOW}; font-size: 14px; border: none;")
                else:
                    dot.setText("●")
                    dot.setStyleSheet(f"color: {T.GREEN}; font-size: 14px; border: none;")

            _refresh_status()
            spin.valueChanged.connect(lambda _v: _refresh_status())
            spin.editingFinished.connect(lambda: self._save_exit_field(ep_key, spin.value()))

            return hl

        # Context helpers
        def profit_ctx(v):
            if not v or not ref: return ""
            t = ref * v / 100.0
            return f"→  target {money(t, signed=True)}"

        def stop_ctx(v):
            if not v or not ref: return ""
            t = -(ref * v / 100.0)
            return f"→  stop at {money(t, signed=True)}"

        def dte_ctx(v):
            if not v: return ""
            dte = self.strategy.dte
            return f"→  now {dte}d" if dte is not None else ""

        def below_ctx(v):
            if not v or not underlying: return ""
            return f"→  now {underlying:.4f}"

        def above_ctx(v):
            if not v or not underlying: return ""
            return f"→  now {underlying:.4f}"

        # ── Two-column grid ─────────────────────────────────────────────────
        left  = QVBoxLayout(); left.setSpacing(10)
        right = QVBoxLayout(); right.setSpacing(10)

        left.addLayout(_row(
            "Profit Target", "profit_pct", 50, "% of credit",
            0, "Close when P&L reaches this % of premium received",
            profit_ctx, "profit",
        ))
        left.addLayout(_row(
            "Stop Loss", "stop_pct", 200, "% of credit",
            0, "Stop out when loss exceeds this % of premium received",
            stop_ctx, "stop",
        ))
        left.addLayout(_row(
            "DTE Exit", "dte_exit", 21, "days",
            0, "Close when days to expiration drops to this level",
            dte_ctx, "dte",
        ))

        right.addLayout(_row(
            "Stop Below", "underlying_below", underlying or 0, "(underlying)",
            4, "Stop out if underlying price falls to or below this level",
            below_ctx, "below",
        ))
        right.addLayout(_row(
            "Stop Above", "underlying_above", underlying or 0, "(underlying)",
            4, "Stop out if underlying price rises to or above this level",
            above_ctx, "above",
        ))
        if underlying is not None:
            spot_lbl = QLabel(f"Current underlying:  {underlying:.4f}")
            spot_lbl.setStyleSheet(f"color: {T.MUTED}; font-size: 11px; border: none; margin-top: 4px;")
            right.addWidget(spot_lbl)
        right.addStretch()

        cols = QHBoxLayout()
        cols.setSpacing(32)
        cols.addLayout(left,  1)
        cols.addLayout(right, 1)
        lay.addLayout(cols)

        # ── Progress bar for profit target ─────────────────────────────────
        profit_c = cond_map.get("profit")
        if profit_c and profit_c["target"] > 0:
            done_pct = max(0.0, min(1.0, profit_c["pct_done"] or 0.0))
            bar_outer = QFrame()
            bar_outer.setFixedHeight(6)
            bar_outer.setStyleSheet(
                f"QFrame {{ background: {T.BG_ALT}; border: none; border-radius: 3px; }}"
            )
            bar_lay = QHBoxLayout(bar_outer)
            bar_lay.setContentsMargins(0, 0, 0, 0)
            bar_lay.setSpacing(0)
            fill = QFrame()
            fill_color = T.GREEN if done_pct >= 1.0 else (T.YELLOW if done_pct >= 0.7 else T.TEAL)
            fill.setStyleSheet(f"QFrame {{ background: {fill_color}; border: none; border-radius: 3px; }}")
            bar_lay.addWidget(fill, int(done_pct * 1000))
            bar_lay.addWidget(QFrame(), int((1.0 - done_pct) * 1000))
            pct_lbl = QLabel(f"{done_pct*100:.0f}% to target")
            pct_lbl.setStyleSheet(f"color: {T.MUTED}; font-size: 10px; border: none; margin-top: 2px;")
            lay.addWidget(bar_outer)
            lay.addWidget(pct_lbl)

        # ── Alert banners for triggered / near conditions ───────────────────
        hit_conds  = [c for c in conds if c["severity"] == "hit"]
        near_conds = [c for c in conds if c["severity"] == "near"]
        if hit_conds or near_conds:
            sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet(f"color: {T.BORDER}; margin-top: 4px;")
            lay.addWidget(sep)

        for c in hit_conds:
            banner = QFrame()
            banner.setStyleSheet(
                f"QFrame {{ background: #2d1515; border: 1px solid {T.RED}; border-radius: 8px; }}"
            )
            bl = QHBoxLayout(banner); bl.setContentsMargins(14, 8, 14, 8)
            icon = QLabel("⚡"); icon.setStyleSheet(f"color: {T.RED}; font-size: 16px; border: none;")
            txt  = QLabel(c["message"])
            txt.setStyleSheet(f"color: {T.RED}; font-size: 13px; font-weight: bold; border: none;")
            bl.addWidget(icon); bl.addWidget(txt); bl.addStretch()
            lay.addWidget(banner)

        for c in near_conds:
            banner = QFrame()
            banner.setStyleSheet(
                f"QFrame {{ background: #2a2010; border: 1px solid {T.YELLOW}; border-radius: 8px; }}"
            )
            bl = QHBoxLayout(banner); bl.setContentsMargins(14, 8, 14, 8)
            icon = QLabel("◐"); icon.setStyleSheet(f"color: {T.YELLOW}; font-size: 16px; border: none;")
            txt  = QLabel(f"Approaching: {c['message']}")
            txt.setStyleSheet(f"color: {T.YELLOW}; font-size: 12px; border: none;")
            bl.addWidget(icon); bl.addWidget(txt); bl.addStretch()
            lay.addWidget(banner)

        return frame

    def _save_exit_field(self, key, value):
        """Persist a single exit-plan field to the raw strategy dict."""
        if not isinstance(self.strategy, StrategyInstance):
            return
        raw = next(
            (r for r in self.portfolio.strategies_raw if r["id"] == self.strategy.id), None
        )
        if raw is None:
            return
        ep = raw.setdefault("exit_plan", {})
        if value > 0:
            ep[key] = value
        else:
            ep.pop(key, None)
        # Sync back to the live instance
        self.strategy._raw["exit_plan"] = ep
        self.portfolio.save_strategies()

    # ── History (always shown for StrategyInstance) ─────────────────────────

    def _build_history_card(self):
        if not isinstance(self.strategy, StrategyInstance):
            return QWidget()   # placeholder, never added to layout
        history = self.portfolio.history
        perf = strategy_performance(self.strategy.id, history,
                                    capital_req=self._capital_required())
        entries = [h for h in history if h.get("strategy_id") == self.strategy.id]

        # Build the card frame directly so we can put the Import button
        # inline with the section title (avoids fragile layout-item moves).
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 14px; }}"
        )
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(22, 18, 22, 20)
        lay.setSpacing(10)

        # ── Title row ──────────────────────────────────────────────────────
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)

        title_lbl = QLabel("Performance History")
        title_lbl.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 14px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        title_row.addWidget(title_lbl)
        title_row.addStretch()

        import_btn = QPushButton("⬇  Import History")
        import_btn.setFixedHeight(28)
        import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        import_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 0 12px; "
            f"font-size: 11px; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        import_btn.clicked.connect(self._import_history)
        title_row.addWidget(import_btn)

        lay.addLayout(title_row)

        if not entries:
            empty = QLabel(
                "No closed legs yet. When a position leaves the portfolio "
                "(expired, closed, or rolled), it'll be logged here automatically."
            )
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f"color: {T.MUTED}; font-size: 12px; border: none; background: transparent;"
            )
            lay.addWidget(empty)
            return frame

        # Performance stats grid
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(4)
        items = [
            ("Closed legs", str(perf["closed_legs"])),
            ("Total P&L",   money(perf["total_pnl"], signed=True)),
            ("Win rate",    f"{perf['win_rate']:.0f}%"),
            ("Avg DIT",     f"{perf['avg_dit']:.0f}d"
                             if perf["avg_dit"] is not None else "—"),
            ("Avg weekly",  money(perf["avg_weekly"], signed=True)
                             if perf["avg_weekly"] is not None else "—"),
            ("Avg monthly", money(perf["avg_monthly"], signed=True)
                             if perf["avg_monthly"] is not None else "—"),
            ("Yearly pace", money(perf["yearly"], signed=True)
                             if perf["yearly"] is not None else "—"),
            ("Weekly %",    f"{perf['weekly_pct']:+.2f}%"
                             if perf.get("weekly_pct") is not None else "—"),
        ]
        for i, (k, v) in enumerate(items):
            kl = QLabel(k.upper())
            kl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.5px; "
                f"border: none; background: transparent;"
            )
            vl = QLabel(v)
            vl.setStyleSheet(
                f"color: {T.TEXT}; font-size: 14px; font-weight: bold; "
                f"border: none; background: transparent;"
            )
            grid.addWidget(kl, (i//4)*2,     i%4)
            grid.addWidget(vl, (i//4)*2 + 1, i%4)
        lay.addLayout(grid)
        lay.addSpacing(6)

        # Cumulative P&L chart
        chart_hdr = QLabel("CUMULATIVE P&L")
        chart_hdr.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.5px; "
            f"border: none; background: transparent; margin-top: 6px;"
        )
        lay.addWidget(chart_hdr)
        chart = HistoryChart(entries, height=3.0)
        chart.setMinimumHeight(260)
        lay.addWidget(chart)

        # Closed-legs list
        hdr = QLabel("CLOSED LEGS")
        hdr.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.5px; "
            f"border: none; background: transparent; margin-top: 8px;"
        )
        lay.addWidget(hdr)
        for h in sorted(entries, key=lambda e: e.get("closed_at") or "", reverse=True):
            side = "Long" if (h.get("sign") or 0) > 0 else "Short"
            cp = {"C": "Call", "P": "Put"}.get(h.get("call_put"), "Stock")
            k  = f"{h.get('strike', 0):g}" if h.get("strike") else ""
            pnl = h.get("pnl") or 0.0
            row = QFrame()
            row.setStyleSheet(
                f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; "
                f"border-radius: 6px; }}"
                f"QFrame:hover {{ border-color: {T.BORDER_H}; }}"
            )
            hl = QHBoxLayout(row)
            hl.setContentsMargins(10, 6, 6, 6)
            hl.setSpacing(6)
            label = QLabel(
                f"{(h.get('closed_at') or '—')[:10]}  ·  {side} {int(h.get('qty') or 0)} "
                f"{h.get('root') or ''} {cp} {k}"
            )
            label.setStyleSheet(
                f"color: {T.TEXT_DIM}; font-size: 11px; border: none; background: transparent;"
            )
            hl.addWidget(label)
            hl.addStretch()
            pl = QLabel(money(pnl, signed=True))
            pl.setStyleSheet(
                f"color: {pnl_color(pnl)}; font-size: 12px; font-weight: bold; "
                f"border: none; background: transparent;"
            )
            hl.addWidget(pl)

            # Edit P&L button
            edit_btn = QPushButton("✎")
            edit_btn.setFixedSize(26, 26)
            edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            edit_btn.setToolTip("Edit P&L")
            edit_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T.MUTED}; "
                f"border: none; font-size: 13px; border-radius: 5px; }}"
                f"QPushButton:hover {{ background: #1e2438; color: {T.ACCENT}; }}"
            )
            edit_btn.clicked.connect(lambda _checked, entry=h, lbl=pl: self._edit_history_entry(entry, lbl))
            hl.addWidget(edit_btn)

            # Delete button
            del_btn = QPushButton("✕")
            del_btn.setFixedSize(26, 26)
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setToolTip("Remove entry")
            del_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T.MUTED}; "
                f"border: none; font-size: 13px; font-weight: bold; border-radius: 5px; }}"
                f"QPushButton:hover {{ background: #3d1a1a; color: {T.RED}; }}"
            )
            del_btn.clicked.connect(lambda _checked, entry=h, widget=row: self._delete_history_entry(entry, widget))
            hl.addWidget(del_btn)

            lay.addWidget(row)

        return frame

    def _import_history(self):
        if not isinstance(self.strategy, StrategyInstance):
            return
        dlg = PastLegPickerDialog(
            self.strategy.id, self.portfolio.history,
            self.portfolio.strategies_raw, parent=self
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        syms = set(dlg.selected_symbols())
        if not syms:
            return
        for h in self.portfolio.history:
            if h["symbol"] in syms:
                h["strategy_id"] = self.strategy.id
        self.portfolio.save_history()
        QMessageBox.information(
            self, "Saved", f"Assigned {len(syms)} closed leg(s) to this strategy."
        )
        self.reopen_requested.emit(self.strategy)

    def _edit_history_entry(self, entry, pnl_label):
        """Edit the P&L value of a closed-leg history entry inline."""
        current = float(entry.get("pnl") or 0.0)
        val, ok = QInputDialog.getDouble(
            self, "Edit P&L",
            "Enter corrected P&L for this leg:",
            value=current, min=-1e9, max=1e9, decimals=2,
        )
        if not ok:
            return
        entry["pnl"] = val
        self.portfolio.save_history()
        # Update the label live without a full rebuild
        from strategy_card import money, pnl_color
        pnl_label.setText(money(val, signed=True))
        pnl_label.setStyleSheet(
            f"color: {pnl_color(val)}; font-size: 12px; font-weight: bold; "
            f"border: none; background: transparent;"
        )

    def _delete_history_entry(self, entry, row_widget):
        """Remove a single closed-leg history entry after confirmation."""
        side = "Long" if (entry.get("sign") or 0) > 0 else "Short"
        cp   = {"C": "Call", "P": "Put"}.get(entry.get("call_put"), "Stock")
        k    = f" {entry.get('strike', 0):g}" if entry.get("strike") else ""
        date = (entry.get("closed_at") or "")[:10]
        desc = f"{date}  {side} {int(entry.get('qty') or 0)} {entry.get('root') or ''} {cp}{k}"
        reply = QMessageBox.question(
            self, "Remove entry",
            f"Remove this history entry?\n\n{desc}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        acct = self.portfolio.current_account()
        if acct:
            hist = self.portfolio.history_all.get(acct["number"], [])
            try:
                hist.remove(entry)
            except ValueError:
                pass
        self.portfolio.save_history()
        # Hide the row immediately — no full rebuild needed
        row_widget.setVisible(False)
        row_widget.setFixedHeight(0)

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _section_frame(self, title):
        f = QFrame()
        f.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 14px; }}"
        )
        lay = QVBoxLayout(f)
        lay.setContentsMargins(22, 18, 22, 20)
        lay.setSpacing(10)
        tl = QLabel(title)
        tl.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 14px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        lay.addWidget(tl)
        return f, lay

    # ── Leg groups ──────────────────────────────────────────────────────────

    def _leg_groups(self):
        """Return the raw list of leg groups stored on the strategy."""
        if not isinstance(self.strategy, StrategyInstance):
            return []
        return self.strategy._raw.setdefault("leg_groups", [])

    def _save_leg_groups(self, groups):
        self.strategy._raw["leg_groups"] = groups
        if self.portfolio:
            self.portfolio.save_strategies()

    def _build_leg_groups_card(self):
        frame, lay = self._section_frame("Leg Groups")

        # Hint + "+ New group" button row
        hint_row = QHBoxLayout()
        hint = QLabel(
            "Organize legs into named sub-strategies (e.g. 'Call Spread' + "
            "'Put Spread'). Each group gets its own payoff chart + Greeks."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none; background: transparent;"
        )
        hint_row.addWidget(hint, 1)

        add_btn = QPushButton("+ New group")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setFixedHeight(30)
        add_btn.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: white; border: none; "
            f"border-radius: 6px; padding: 0 14px; font-size: 11px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
        )
        add_btn.clicked.connect(self._on_add_leg_group)
        hint_row.addWidget(add_btn)
        lay.addLayout(hint_row)

        groups = self._leg_groups()
        if not groups:
            empty = QLabel("No groups yet — click + New group to create one.")
            empty.setStyleSheet(
                f"color: {T.MUTED}; font-size: 11px; padding: 16px; border: 1px dashed "
                f"{T.BORDER}; border-radius: 8px; background: #12151d;"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(empty)
        else:
            for grp in groups:
                lay.addWidget(self._build_single_group_card(grp))

        return frame

    def _build_single_group_card(self, grp):
        """Build one card per leg-group: name, payoff chart, Greeks, metrics."""
        from models import Strategy as _Strategy
        from payoff_chart import PayoffChart

        sym_set = set(grp.get("legs", []) or [])
        group_legs = [l for l in self.strategy.legs if l.symbol in sym_set]

        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; "
            f"border-radius: 10px; }}"
        )
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 14, 18, 16)
        cl.setSpacing(10)

        # Header: name + legs count + edit/delete
        top = QHBoxLayout()
        top.setSpacing(8)
        name_lbl = QLabel(grp.get("name") or "(unnamed)")
        name_lbl.setStyleSheet(
            f"color: {T.TEXT}; font-size: 15px; font-weight: bold; border: none; background: transparent;"
        )
        top.addWidget(name_lbl)
        count_lbl = QLabel(f"{len(group_legs)} legs")
        count_lbl.setStyleSheet(
            f"color: {T.MUTED}; background: {T.BG_ALT}; border: 1px solid {T.BORDER}; "
            f"border-radius: 5px; padding: 2px 8px; font-size: 10px; font-weight: bold;"
        )
        top.addWidget(count_lbl)
        top.addStretch()

        edit_btn = QPushButton("Edit")
        edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_btn.setFixedHeight(26)
        edit_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 5px; padding: 0 10px; font-size: 10px; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        edit_btn.clicked.connect(lambda: self._on_edit_leg_group(grp))
        top.addWidget(edit_btn)

        del_btn = QPushButton("Delete")
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setFixedHeight(26)
        del_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.RED}; "
            f"border: 1px solid {T.RED}; border-radius: 5px; padding: 0 10px; font-size: 10px; }}"
            f"QPushButton:hover {{ background: {T.RED}; color: white; }}"
        )
        del_btn.clicked.connect(lambda: self._on_delete_leg_group(grp))
        top.addWidget(del_btn)
        cl.addLayout(top)

        if not group_legs:
            msg = QLabel("No legs assigned to this group yet — click Edit.")
            msg.setStyleSheet(
                f"color: {T.MUTED}; font-size: 11px; border: none;"
            )
            cl.addWidget(msg)
            return card

        sub_strategy = _Strategy(
            f"{self.strategy.key}:{grp.get('id')}",
            group_legs,
            custom_name=grp.get("name"),
            is_custom=True,
        )

        # Metrics row: Δ, Θ, V, P&L, Max Profit, Max Loss
        max_profit, max_loss, _ = strategy_extremes(sub_strategy)
        metrics_row = QGridLayout()
        metrics_row.setHorizontalSpacing(10)
        metrics_row.setVerticalSpacing(6)

        def mb(col, label, value, color=T.TEXT):
            metrics_row.addWidget(self._metric_box(label, value, color), 0, col)

        mb(0, "P&L",
           money(sub_strategy.pnl, signed=True),
           pnl_color(sub_strategy.pnl))
        mb(1, "Max Profit",
           "Unlimited" if max_profit == float("inf") else money(max_profit),
           T.GREEN)
        mb(2, "Max Loss",
           "Unlimited" if max_loss == float("-inf") else money(max_loss, signed=True),
           T.RED if max_loss and max_loss != 0 else T.MUTED)
        mb(3, "Δ",  fmt_num(sub_strategy.net_delta, 2, signed=True), T.TEXT)
        mb(4, "Θ",  fmt_num(sub_strategy.net_theta, 2, signed=True),
           pnl_color(sub_strategy.net_theta))
        mb(5, "V",  fmt_num(sub_strategy.net_vega,  2, signed=True), T.TEXT)

        for i in range(6):
            metrics_row.setColumnStretch(i, 1)
        cl.addLayout(metrics_row)

        # Payoff chart
        chart = PayoffChart(sub_strategy, height=2.6)
        chart.setMinimumHeight(220)
        cl.addWidget(chart)

        return card

    def _on_add_leg_group(self):
        import uuid
        dlg = _LegGroupDialog(self.strategy.legs, name="", selected=set(), parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            groups = list(self._leg_groups())
            groups.append({
                "id":    uuid.uuid4().hex[:10],
                "name":  dlg.result_name(),
                "legs":  dlg.result_legs(),
            })
            self._save_leg_groups(groups)
            self._refresh_reopen()

    def _on_edit_leg_group(self, grp):
        dlg = _LegGroupDialog(
            self.strategy.legs,
            name=grp.get("name", ""),
            selected=set(grp.get("legs", [])),
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            grp["name"] = dlg.result_name()
            grp["legs"] = dlg.result_legs()
            self._save_leg_groups(self._leg_groups())
            self._refresh_reopen()

    def _on_delete_leg_group(self, grp):
        groups = [g for g in self._leg_groups() if g.get("id") != grp.get("id")]
        self._save_leg_groups(groups)
        self._refresh_reopen()

    def _refresh_reopen(self):
        """Close this page and reopen the detail view to pick up new groups."""
        self.reopen_requested.emit(self.strategy)

    def _metric_box(self, label, value, color):
        w = QFrame()
        w.setStyleSheet(
            f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; "
            f"border-radius: 8px; }}"
        )
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(2)
        l = QLabel(label.upper())
        l.setStyleSheet(
            f"color: {T.MUTED}; font-size: 9px; font-weight: bold; letter-spacing: 0.5px; "
            f"background: transparent; border: none;"
        )
        lay.addWidget(l)
        v = QLabel(value)
        v.setStyleSheet(
            f"color: {color}; font-size: 15px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        lay.addWidget(v)
        return w


# ── What-if scenario dialog ─────────────────────────────────────────────────

class WhatIfDialog(QDialog):
    def __init__(self, strategy, parent=None):
        super().__init__(parent)
        self.strategy = strategy
        self.setWindowTitle("What-if Scenario")
        self.setStyleSheet(T.BASE_STYLE)
        self.setMinimumSize(520, 460)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(14)

        hdr = QLabel(f"Stress test — {strategy.name}")
        hdr.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 16px; font-weight: bold; border: none;"
        )
        root.addWidget(hdr)

        hint = QLabel(
            "First-order estimate using current Greeks. "
            "Sliders apply simultaneously: underlying move, IV shift, time passage."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {T.MUTED}; font-size: 11px; border: none;")
        root.addWidget(hint)

        self.price_slider = self._add_slider(root, "Underlying move",
                                             -30, 30, 0, "%", scale=1)
        self.iv_slider    = self._add_slider(root, "IV shift",
                                             -30, 30, 0, " vol-pts", scale=1)
        self.time_slider  = self._add_slider(root, "Days forward",
                                             0, 60, 0, " days", scale=1)

        # Results
        self.results_frame = QFrame()
        self.results_frame.setStyleSheet(
            f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; border-radius: 10px; }}"
        )
        rl = QGridLayout(self.results_frame)
        rl.setContentsMargins(18, 14, 18, 14)
        rl.setHorizontalSpacing(18)
        rl.setVerticalSpacing(6)

        self._res_labels = {}
        for col, key, title in [
            (0, "pnl",       "Scenario P&L"),
            (1, "net_delta", "New Δ"),
            (2, "net_theta", "New Θ"),
            (3, "net_vega",  "New Vega"),
        ]:
            lbl = QLabel(title.upper())
            lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.5px; "
                f"border: none; background: transparent;"
            )
            val = QLabel("—")
            val.setStyleSheet(
                f"color: {T.TEXT}; font-size: 18px; font-weight: bold; "
                f"border: none; background: transparent;"
            )
            rl.addWidget(lbl, 0, col)
            rl.addWidget(val, 1, col)
            self._res_labels[key] = val

        root.addWidget(self.results_frame)

        close = QPushButton("Close")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close)
        root.addLayout(row)

        self._recompute()

    def _add_slider(self, parent_layout, label, minv, maxv, default, suffix, scale=1):
        wrap = QVBoxLayout()
        wrap.setSpacing(4)
        header = QHBoxLayout()
        name = QLabel(label.upper())
        name.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.6px; "
            f"border: none; background: transparent;"
        )
        header.addWidget(name)
        header.addStretch()
        val_lbl = QLabel(f"{default}{suffix}")
        val_lbl.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 13px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        header.addWidget(val_lbl)
        wrap.addLayout(header)

        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setMinimum(minv); sl.setMaximum(maxv); sl.setValue(default)
        sl.setStyleSheet(
            f"QSlider::groove:horizontal {{ background: {T.BORDER}; height: 4px; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {T.PURPLE}; width: 16px; "
            f"margin: -7px 0; border-radius: 8px; }}"
            f"QSlider::sub-page:horizontal {{ background: {T.PURPLE}; border-radius: 2px; }}"
        )
        wrap.addWidget(sl)
        parent_layout.addLayout(wrap)
        sl.valueChanged.connect(lambda v: (val_lbl.setText(f"{v}{suffix}"), self._recompute()))
        return sl

    def _recompute(self):
        res = scenario_pnl(
            self.strategy,
            price_pct=self.price_slider.value(),
            iv_pct=self.iv_slider.value(),
            days_forward=self.time_slider.value(),
        )
        pnl = res["pnl"]
        pnl_lbl = self._res_labels["pnl"]
        pnl_lbl.setText(money(pnl, signed=True))
        pnl_lbl.setStyleSheet(
            f"color: {pnl_color(pnl)}; font-size: 18px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        self._res_labels["net_delta"].setText(fmt_num(res["net_delta"], 2, signed=True))
        self._res_labels["net_theta"].setText(fmt_num(res["net_theta"], 2, signed=True))
        self._res_labels["net_vega"].setText(fmt_num(res["net_vega"], 2, signed=True))


# ── Leg-group editor dialog ──────────────────────────────────────────────────

class _LegGroupDialog(QDialog):
    """Edit a leg group's name + which legs belong to it."""

    def __init__(self, all_legs, name="", selected=None, parent=None):
        super().__init__(parent)
        selected = set(selected or [])
        self.setWindowTitle("Leg Group")
        self.setMinimumWidth(460)
        self.setStyleSheet(T.BASE_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)

        title = QLabel("Group legs into a sub-strategy")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 14px; font-weight: bold; border: none;"
        )
        root.addWidget(title)

        # Name input
        lbl = QLabel("Name")
        lbl.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; border: none;"
        )
        root.addWidget(lbl)
        self._name = QLineEdit(name)
        self._name.setPlaceholderText("e.g. Call Spread, Put Side, Bull Butterfly …")
        root.addWidget(self._name)

        # Legs checklist
        lbl = QLabel("Include these legs")
        lbl.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"border: none; margin-top: 8px;"
        )
        root.addWidget(lbl)

        self._boxes = []
        for leg in all_legs:
            root.addWidget(self._build_leg_row(leg, leg.symbol in selected))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_leg_row(self, leg, selected):
        """Checkbox row with full leg detail so the user can tell what they're picking."""
        from strategy_card import money, pnl_color
        row = QFrame()
        row.setStyleSheet(
            f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; "
            f"border-radius: 8px; }}"
        )
        hl = QHBoxLayout(row)
        hl.setContentsMargins(12, 8, 12, 8)
        hl.setSpacing(10)

        cb = QCheckBox()
        cb.setChecked(selected)
        cb.setStyleSheet(
            f"QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 4px; "
            f"border: 1px solid {T.BORDER}; background: {T.BG_ALT}; }}"
            f"QCheckBox::indicator:checked {{ background: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        cb.leg_symbol = leg.symbol
        self._boxes.append(cb)
        hl.addWidget(cb)

        # Direction badge
        dir_color = T.GREEN if leg.is_long else T.RED
        dir_badge = QLabel(leg.direction_label[0])    # L / S
        dir_badge.setFixedSize(22, 22)
        dir_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dir_badge.setStyleSheet(
            f"color: white; background: {dir_color}; "
            f"border-radius: 5px; font-size: 11px; font-weight: bold;"
        )
        hl.addWidget(dir_badge)

        # Info block: two lines
        info = QVBoxLayout()
        info.setSpacing(2)

        # Headline: Root · Type Strike · Qty
        head_parts = []
        if leg.root:
            head_parts.append(leg.root)
        if leg.is_option and leg.strike:
            head_parts.append(f"{leg.type_label} {leg.strike:g}")
        else:
            head_parts.append(leg.type_label)
        head_parts.append(f"×{leg.quantity:g}")
        head = QLabel("  ·  ".join(head_parts))
        head.setStyleSheet(
            f"color: {T.TEXT}; font-size: 13px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        info.addWidget(head)

        # Detail: expiry · DTE · mark · P&L
        bits = []
        if leg.expires_at:
            bits.append(leg.expires_at.strftime("%b %d %Y"))
        if leg.dte is not None:
            bits.append(f"{leg.dte}d")
        bits.append(f"Mark ${leg.mark_price:,.2f}")
        detail_left = QLabel("  ·  ".join(bits))
        detail_left.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none; background: transparent;"
        )
        info.addWidget(detail_left)

        hl.addLayout(info, 1)

        # P&L on the right
        pnl_lbl = QLabel(money(leg.pnl, signed=True))
        pnl_lbl.setStyleSheet(
            f"color: {pnl_color(leg.pnl)}; font-size: 13px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        pnl_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        hl.addWidget(pnl_lbl)

        # Clicking anywhere on the row toggles the checkbox
        def _toggle(ev):
            cb.setChecked(not cb.isChecked())
        row.mousePressEvent = _toggle

        return row

    def result_name(self):
        return self._name.text().strip()

    def result_legs(self):
        return [cb.leg_symbol for cb in self._boxes if cb.isChecked()]


# ── Add-leg picker ──────────────────────────────────────────────────────────

class _AddLegDialog(QDialog):
    """Pick one or more positions to attach to the current strategy."""

    def __init__(self, candidates, owner_by_sym, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Legs")
        # Size the dialog to the longest row so detail lines don't clip.
        longest = 0
        for p in candidates:
            bits = []
            if p.root: bits.append(p.root)
            if p.is_option and p.strike:
                bits.append(f"{p.type_label} {p.strike:g}")
            else:
                bits.append(p.type_label)
            bits.append(f"×{p.quantity:g}")
            head_len = len("  ·  ".join(bits))

            dbits = []
            if p.expires_at: dbits.append(p.expires_at.strftime("%b %d %Y"))
            if p.dte is not None: dbits.append(f"{p.dte}d")
            dbits.append(f"Mark ${p.mark_price:,.2f}")
            owner = owner_by_sym.get(p.symbol)
            if owner:
                dbits.append(f"(in '{owner}' — will be moved)")
            detail_len = len("  ·  ".join(dbits))
            longest = max(longest, head_len, detail_len)

        # ~7 px per char + fixed chrome (checkbox / badge / pnl / margins / scrollbar)
        self.resize(max(620, longest * 7 + 220), 560)
        self.setStyleSheet(T.BASE_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(10)

        title = QLabel("Add legs to this strategy")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 14px; font-weight: bold; border: none;"
        )
        root.addWidget(title)

        hint = QLabel(
            "Picking a leg currently assigned to another strategy will move it here."
        )
        hint.setStyleSheet(f"color: {T.MUTED}; font-size: 11px; border: none;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # Scrollable list of candidate legs
        from PyQt6.QtWidgets import QScrollArea as _SA
        scroll = _SA()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        list_w = QWidget()
        list_lay = QVBoxLayout(list_w)
        list_lay.setContentsMargins(0, 0, 0, 0)
        list_lay.setSpacing(6)

        self._boxes = []
        for p in candidates:
            list_lay.addWidget(self._build_row(p, owner_by_sym.get(p.symbol)))
        list_lay.addStretch()

        scroll.setWidget(list_w)
        scroll.setMinimumHeight(320)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_row(self, p, current_owner):
        from strategy_card import money, pnl_color
        row = QFrame()
        row.setStyleSheet(
            f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; "
            f"border-radius: 8px; }}"
        )
        hl = QHBoxLayout(row)
        hl.setContentsMargins(12, 8, 12, 8)
        hl.setSpacing(10)

        cb = QCheckBox()
        cb.setStyleSheet(
            f"QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 4px; "
            f"border: 1px solid {T.BORDER}; background: {T.BG_ALT}; }}"
            f"QCheckBox::indicator:checked {{ background: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        cb.leg_symbol = p.symbol
        self._boxes.append(cb)
        hl.addWidget(cb)

        dir_color = T.GREEN if p.is_long else T.RED
        dir_badge = QLabel(p.direction_label[0])
        dir_badge.setFixedSize(22, 22)
        dir_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dir_badge.setStyleSheet(
            f"color: white; background: {dir_color}; border-radius: 5px; "
            f"font-size: 11px; font-weight: bold;"
        )
        hl.addWidget(dir_badge)

        info = QVBoxLayout()
        info.setSpacing(2)
        head_parts = []
        if p.root:
            head_parts.append(p.root)
        if p.is_option and p.strike:
            head_parts.append(f"{p.type_label} {p.strike:g}")
        else:
            head_parts.append(p.type_label)
        head_parts.append(f"×{p.quantity:g}")
        head = QLabel("  ·  ".join(head_parts))
        head.setStyleSheet(
            f"color: {T.TEXT}; font-size: 13px; font-weight: bold; border: none;"
        )
        info.addWidget(head)

        bits = []
        if p.expires_at:
            bits.append(p.expires_at.strftime("%b %d %Y"))
        if p.dte is not None:
            bits.append(f"{p.dte}d")
        bits.append(f"Mark ${p.mark_price:,.2f}")
        if current_owner:
            bits.append(f"(in '{current_owner}' — will be moved)")
        detail = QLabel("  ·  ".join(bits))
        detail.setStyleSheet(f"color: {T.MUTED}; font-size: 11px; border: none;")
        info.addWidget(detail)
        hl.addLayout(info, 1)

        pnl_lbl = QLabel(money(p.pnl, signed=True))
        pnl_lbl.setStyleSheet(
            f"color: {pnl_color(p.pnl)}; font-size: 12px; font-weight: bold; border: none;"
        )
        hl.addWidget(pnl_lbl)

        row.mousePressEvent = lambda ev: cb.setChecked(not cb.isChecked())
        return row

    def result_symbols(self):
        return [cb.leg_symbol for cb in self._boxes if cb.isChecked()]
