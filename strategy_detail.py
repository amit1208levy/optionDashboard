"""Full-page strategy detail: metrics, Greeks, legs, payoff chart, history."""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QScrollArea, QSizePolicy, QInputDialog, QDialog, QSlider,
    QMessageBox,
)

import api
import theme as T
from models import (
    StrategyInstance, strategy_extremes, probability_of_profit, capital_for_strategy,
    strategy_performance, symbol_ivr, symbol_ivp, symbol_beta, symbol_hv30,
    scenario_pnl, distribute_futures_margin, unassigned_positions, group_unassigned,
)
from payoff_chart import PayoffChart
from history_chart import HistoryChart
from strategy_card import money, pct, fmt_num, pnl_color, dte_color
from strategies_page import PastLegPickerDialog


# ── Leg row (read-only) ─────────────────────────────────────────────────────

LEG_COLUMNS = [
    ("Side",    60),
    ("Type",    58),
    ("Strike",  72),
    ("Exp",     92),
    ("Qty",     48),
    ("Open",    68),
    ("Mark",    68),
    ("Premium", 90),
    ("P&L",     96),
    ("Δ",       56),
    ("Θ",       56),
    ("IV",      62),
]


class LegHeader(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 4, 6, 6)
        h.setSpacing(8)
        for label, width in LEG_COLUMNS:
            l = QLabel(label.upper())
            l.setFixedWidth(width)
            l.setStyleSheet(
                f"color: {T.MUTED}; background: transparent; border: none; "
                f"font-size: 10px; font-weight: bold; letter-spacing: 0.6px;"
            )
            h.addWidget(l)
        h.addStretch()


class LegRow(QFrame):
    def __init__(self, leg, parent=None):
        super().__init__(parent)
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
            (leg.direction_label.upper(),       side_color, 700),
            (leg.type_label,                    type_color, 700),
            (f"${leg.strike:g}" if leg.strike else "—", T.TEXT, 600),
            (leg.expires_at.strftime("%b %d %y") if leg.expires_at else "—", T.TEXT_DIM, 400),
            (f"{leg.quantity:g}",               T.TEXT,     500),
            (money(leg.avg_open_price),         T.TEXT_DIM, 400),
            (money(leg.mark_price),             T.TEXT,     500),
            (money(leg.credit_debit, signed=True), prem_color, 600),
            (money(leg.pnl, signed=True),       pnl_color(leg.pnl), 700),
            (fmt_num(leg.delta, 2, signed=True), T.TEXT_DIM, 400),
            (fmt_num(leg.theta, 2, signed=True), T.TEXT_DIM, 400),
            (pct(leg.iv * 100 if leg.iv is not None else None, signed=False), T.TEXT_DIM, 400),
        ]
        for (text, color, weight), (_, width) in zip(cells, LEG_COLUMNS):
            l = QLabel(text)
            l.setFixedWidth(width)
            l.setStyleSheet(
                f"color: {color}; background: transparent; border: none; "
                f"font-size: 12px; font-weight: {weight};"
            )
            h.addWidget(l)
        h.addStretch()


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

        if isinstance(self.strategy, StrategyInstance):
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
            dte_color(s.dte),
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
        s = self.strategy
        max_profit, max_loss, breakevens = strategy_extremes(s)

        frame, lay = self._section_frame("Risk Metrics")

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

        cap_required, cap_source = self._capital_required_with_source()
        cap_box = self._capital_box(cap_required, cap_source)
        grid.addWidget(cap_box, 0, 2)

        be_text = "  /  ".join(f"${b:,.2f}" for b in breakevens[:2]) if breakevens else "—"
        cell(3, "Breakeven", be_text, T.TEXT_DIM)

        for i in range(4):
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
          'override'    – user-set manual override
          'max_loss'    – defined-risk strategy (max loss from payoff diagram)
          'distributed' – actual TastyTrade futures margin, distributed proportionally
          'estimate'    – SPAN / notional approximation (least accurate)
          None          – no value available
        """
        override = self._capital_override()
        if override is not None:
            return override, "override"

        _, max_loss, _ = strategy_extremes(self.strategy)
        if max_loss is not None and max_loss != float("-inf"):
            return abs(max_loss), "max_loss"

        # For futures-option strategies, use the actual margin from TastyTrade
        has_future_opts = any(
            l.instrument_type == "Future Option" for l in self.strategy.legs
        )
        if has_future_opts:
            dist = self._get_distributed_futures_margin()
            if dist is not None and dist > 0:
                return dist, "distributed"

        # Undefined-risk equity options: notional/SPAN approximation
        est = capital_for_strategy(self.strategy)
        return (est if est else None), "estimate"

    def _get_distributed_futures_margin(self):
        """
        Look up this strategy's pro-rata share of the account's
        futures-margin-requirement (from the balances API).
        Returns a float or None.
        """
        acct = self.portfolio.current_account() if self.portfolio else None
        if not acct:
            return None
        try:
            total_fm = float(acct["balances"].get("futures-margin-requirement") or 0)
        except (TypeError, ValueError):
            return None
        if total_fm <= 0:
            return None

        positions = acct["positions"]
        strat_raw = self.portfolio.strategies_raw
        instances = [StrategyInstance(d, positions) for d in strat_raw]
        leftover  = unassigned_positions(positions, strat_raw)
        unassigned = group_unassigned(leftover)

        dist = distribute_futures_margin(instances, unassigned, total_fm)
        return dist.get(self.strategy.key)

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
        l = QLabel("CAPITAL REQUIRED")
        l.setStyleSheet(
            f"color: {T.MUTED}; font-size: 9px; font-weight: bold; letter-spacing: 0.5px; "
            f"background: transparent; border: none;"
        )
        top.addWidget(l)
        top.addStretch()
        if isinstance(self.strategy, StrategyInstance):
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
        elif source == "distributed":
            badge_text, badge_color = "actual margin", T.TEAL
        elif source == "estimate":
            badge_text, badge_color = "estimate", T.MUTED
        else:
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

        items = [
            ("Δ Delta",  fmt_num(s.net_delta, 2, signed=True),
                pnl_color(s.net_delta) if s.net_delta else T.TEXT_DIM),
            ("Θ Theta",  fmt_num(s.net_theta, 2, signed=True),
                pnl_color(s.net_theta) if s.net_theta else T.TEXT_DIM),
            ("V Vega",   fmt_num(s.net_vega,  2, signed=True), T.TEXT_DIM),
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
        frame, lay = self._section_frame("Payoff at Expiration")
        chart = PayoffChart(self.strategy, height=3.6)
        chart.setMinimumHeight(320)
        lay.addWidget(chart)
        return frame

    # ── Legs table ──────────────────────────────────────────────────────────

    def _build_legs_card(self):
        frame, lay = self._section_frame(f"Legs ({len(self.strategy.legs)})")
        lay.addWidget(LegHeader())
        for leg in self.strategy.legs:
            lay.addWidget(LegRow(leg))
        return frame

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
            )
            hl = QHBoxLayout(row)
            hl.setContentsMargins(10, 6, 10, 6)
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
