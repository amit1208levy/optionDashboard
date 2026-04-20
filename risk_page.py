"""Risk Management page — portfolio allocation by strategy + earnings calendar."""
import calendar as _cal
from datetime import date

import api
import theme as T
from models import (
    StrategyInstance, unassigned_positions, group_unassigned, strategy_allocation,
)
from strategy_card import money, pnl_color

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea,
)


# ── Background worker: fetch market metrics ───────────────────────────────────

class _MetricsWorker(QThread):
    done = pyqtSignal(dict)

    def __init__(self, token, symbols):
        super().__init__()
        self.token   = token
        self.symbols = symbols

    def run(self):
        try:
            self.done.emit(api.get_market_metrics(self.token, self.symbols))
        except Exception:
            self.done.emit({})


# ── Earnings calendar ─────────────────────────────────────────────────────────

class EarningsCalendar(QFrame):
    """
    3-month earnings calendar (current + next 2 months).

    Shows upcoming earnings for:
      • every ticker in ALL watchlists
      • every position currently in the portfolio

    Today is clearly highlighted.  Past earnings are dimmed.
    """

    NUM_MONTHS = 3

    def __init__(self, existing_metrics, token, parent=None):
        super().__init__(parent)
        self._metrics = dict(existing_metrics or {})
        self._token   = token
        self._worker  = None
        self._today   = date.today()

        # Collect every ticker across all watchlists
        settings   = api.load_settings()
        wl_lists   = settings.get("watchlists_v2", [])
        self._wl_tickers = sorted({
            t.upper().lstrip("/")
            for wl in wl_lists
            for t in wl.get("tickers", [])
        })

        self.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 12px; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 16, 22, 20)
        outer.setSpacing(10)

        # ── Header row ────────────────────────────────────────────────────────
        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(12)

        title = QLabel("EARNINGS CALENDAR")
        title.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"letter-spacing: 0.8px; border: none;"
        )
        hdr_row.addWidget(title)
        hdr_row.addStretch()

        # Legend
        for dot_color, label in [
            (T.YELLOW,  "upcoming"),
            (T.MUTED,   "reported"),
            (T.PURPLE,  "today"),
        ]:
            dot = QFrame()
            dot.setFixedSize(8, 8)
            dot.setStyleSheet(
                f"background: {dot_color}; border-radius: 4px; border: none;"
            )
            hdr_row.addWidget(dot)
            lb = QLabel(label)
            lb.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; border: none; margin-right: 8px;"
            )
            hdr_row.addWidget(lb)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"color: {T.MUTED}; font-size: 10px; border: none;")
        hdr_row.addWidget(self._status_lbl)
        outer.addLayout(hdr_row)

        # ── Month panels container ────────────────────────────────────────────
        self._panels_row = QHBoxLayout()
        self._panels_row.setSpacing(20)
        outer.addLayout(self._panels_row)

        # Build immediately with what we have (portfolio metrics)
        self._rebuild()

        # Kick off async fetch for watchlist tickers not yet in metrics
        missing = [t for t in self._wl_tickers if t not in self._metrics]
        if missing:
            self._status_lbl.setText(f"loading {len(missing)} watchlist symbols…")
            self._worker = _MetricsWorker(self._token, missing)
            self._worker.done.connect(self._on_metrics)
            self._worker.start()

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _on_metrics(self, new_metrics):
        self._metrics.update(new_metrics)
        self._status_lbl.setText("")
        self._rebuild()

    def _collect_earnings(self):
        """Return {date: [ticker, ...]} from all known metrics."""
        result = {}
        for ticker, m in self._metrics.items():
            earn = (m or {}).get("earnings") or {}
            raw  = earn.get("expected-report-date") or ""
            if not raw:
                continue
            try:
                d = date.fromisoformat(str(raw)[:10])
                result.setdefault(d, []).append(ticker)
            except ValueError:
                pass
        for d in result:
            result[d].sort()
        return result

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _clear_panels(self):
        while self._panels_row.count():
            item = self._panels_row.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _rebuild(self):
        self._clear_panels()
        earnings = self._collect_earnings()
        today    = self._today

        for i in range(self.NUM_MONTHS):
            raw_m  = today.month + i - 1
            year   = today.year + raw_m // 12
            month  = raw_m % 12 + 1
            first  = date(year, month, 1)
            panel  = self._build_month_panel(first, today, earnings)
            self._panels_row.addWidget(panel, 1)

    def _build_month_panel(self, first, today, earnings):
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background: {T.BG_ALT}; border: 1px solid {T.BORDER}; "
            f"border-radius: 8px; }}"
        )
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(10, 10, 10, 10)
        vl.setSpacing(6)

        # Month + year title
        title = QLabel(first.strftime("%B %Y"))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"color: {T.TEXT}; font-size: 13px; font-weight: bold; border: none;"
        )
        vl.addWidget(title)

        # Weekday column headers
        dow_row = QHBoxLayout()
        dow_row.setSpacing(2)
        for day_name in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]:
            lbl = QLabel(day_name)
            lbl.setFixedWidth(42)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            weekend = day_name in ("Sa", "Su")
            lbl.setStyleSheet(
                f"color: {'#2d3a50' if weekend else T.MUTED}; "
                f"font-size: 9px; font-weight: bold; border: none;"
            )
            dow_row.addWidget(lbl)
        vl.addLayout(dow_row)

        # Day grid
        _, num_days = _cal.monthrange(first.year, first.month)
        start_col   = first.weekday()   # 0 = Monday

        # Build as rows of 7
        rows = []
        current_row = []
        # Pad start
        for _ in range(start_col):
            current_row.append(None)
        for day_num in range(1, num_days + 1):
            current_row.append(date(first.year, first.month, day_num))
            if len(current_row) == 7:
                rows.append(current_row)
                current_row = []
        if current_row:
            while len(current_row) < 7:
                current_row.append(None)
            rows.append(current_row)

        for week_row in rows:
            row_lay = QHBoxLayout()
            row_lay.setSpacing(2)
            for d in week_row:
                if d is None:
                    spacer = QWidget()
                    spacer.setFixedWidth(42)
                    row_lay.addWidget(spacer)
                else:
                    cell = self._build_cell(d, today, earnings.get(d, []))
                    row_lay.addWidget(cell)
            vl.addLayout(row_lay)

        vl.addStretch()
        return panel

    def _build_cell(self, d, today, tickers):
        is_today   = (d == today)
        is_past    = (d < today)
        is_weekend = (d.weekday() >= 5)
        has_earn   = bool(tickers)

        cell = QFrame()
        cell.setFixedWidth(42)

        if is_today:
            bg     = T.PURPLE
            radius = "border-radius: 6px;"
            border = f"border: 2px solid {T.PURPLE2};"
        elif has_earn and not is_past:
            bg     = "#1a2540"
            radius = "border-radius: 5px;"
            border = f"border: 1px solid {T.BLUE};"
        elif has_earn and is_past:
            bg     = "#14171f"
            radius = "border-radius: 5px;"
            border = f"border: 1px solid {T.BORDER};"
        elif is_weekend:
            bg     = "transparent"
            radius = "border-radius: 0;"
            border = "border: none;"
        else:
            bg     = "transparent"
            radius = "border-radius: 0;"
            border = "border: none;"

        cell.setStyleSheet(
            f"QFrame {{ background: {bg}; {radius} {border} }}"
        )

        vl = QVBoxLayout(cell)
        vl.setContentsMargins(2, 3, 2, 3)
        vl.setSpacing(1)

        # Day number
        if is_today:
            num_color = "white"
            num_weight = "bold"
        elif is_past:
            num_color  = T.MUTED if not has_earn else "#475569"
            num_weight = "normal"
        elif is_weekend:
            num_color  = "#2d3a50"
            num_weight = "normal"
        else:
            num_color  = T.TEXT if has_earn else T.TEXT_DIM
            num_weight = "bold" if has_earn else "normal"

        num_lbl = QLabel(str(d.day))
        num_lbl.setFixedWidth(38)
        num_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        num_lbl.setStyleSheet(
            f"color: {num_color}; font-size: 11px; "
            f"font-weight: {num_weight}; border: none;"
        )
        vl.addWidget(num_lbl)

        # Ticker badges (max 3, then "+N")
        badge_bg    = "rgba(96,165,250,0.18)"  if (has_earn and not is_past) else "#1a1d2e"
        badge_color = T.BLUE                   if (has_earn and not is_past) else T.MUTED

        for tk in tickers[:3]:
            b = QLabel(tk[:5])
            b.setFixedWidth(38)
            b.setAlignment(Qt.AlignmentFlag.AlignCenter)
            b.setStyleSheet(
                f"color: {badge_color}; font-size: 7px; font-weight: bold; "
                f"background: {badge_bg}; border-radius: 3px; "
                f"padding: 0 2px; border: none;"
            )
            vl.addWidget(b)

        if len(tickers) > 3:
            more = QLabel(f"+{len(tickers) - 3} more")
            more.setFixedWidth(38)
            more.setAlignment(Qt.AlignmentFlag.AlignCenter)
            more.setStyleSheet(f"color: {T.MUTED}; font-size: 7px; border: none;")
            vl.addWidget(more)

        vl.addStretch()
        return cell


# ── Risk page ─────────────────────────────────────────────────────────────────

class RiskPage(QWidget):
    back_requested = pyqtSignal()

    def __init__(self, portfolio, parent=None):
        super().__init__(parent)
        self.portfolio = portfolio
        self.setStyleSheet(T.BASE_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body_w = QWidget()
        self.body = QVBoxLayout(body_w)
        self.body.setContentsMargins(28, 20, 28, 40)
        self.body.setSpacing(8)
        scroll.setWidget(body_w)
        root.addWidget(scroll)

        self._populate()

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self):
        hdr = QFrame()
        hdr.setFixedHeight(60)
        hdr.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border-bottom: 1px solid {T.BORDER}; border-radius: 0; }}"
        )
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(28, 0, 28, 0)
        hl.setSpacing(12)

        back_btn = QPushButton("← Back")
        back_btn.setFixedHeight(32)
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setProperty("flat", True)
        back_btn.clicked.connect(self.back_requested.emit)
        hl.addWidget(back_btn)

        title = QLabel("Risk Management")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 16px; font-weight: bold; border: none;"
        )
        hl.addWidget(title)
        hl.addStretch()
        return hdr

    # ── Section helpers ───────────────────────────────────────────────────────

    def _section_header(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"letter-spacing: 0.8px; border: none; "
            f"padding-top: 16px; padding-bottom: 4px;"
        )
        return lbl

    def _card(self):
        f = QFrame()
        f.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 12px; }}"
        )
        lay = QVBoxLayout(f)
        lay.setContentsMargins(20, 16, 20, 18)
        lay.setSpacing(8)
        return f, lay

    # ── Build content ─────────────────────────────────────────────────────────

    def _populate(self):
        try:
            acct = self.portfolio.current_account()
            if not acct:
                self.body.addWidget(QLabel("No account loaded."))
                self.body.addStretch()
                return

            positions  = acct["positions"]
            instances  = [StrategyInstance(d, positions)
                          for d in self.portfolio.strategies_raw]
            leftover   = unassigned_positions(positions, self.portfolio.strategies_raw)
            unassigned = group_unassigned(leftover)
            overrides  = {r["id"]: r["capital_override"]
                          for r in self.portfolio.strategies_raw
                          if r.get("capital_override") is not None}

            metrics = acct.get("metrics") or {}
            self._build_allocation(instances, unassigned, overrides)
            try:
                self._build_earnings_calendar(metrics)
            except Exception as _he:
                import traceback
                lbl = QLabel(f"Calendar unavailable: {_he}\n{traceback.format_exc()}")
                lbl.setStyleSheet(f"color: {T.MUTED}; font-size: 10px; border: none;")
                lbl.setWordWrap(True)
                self.body.addWidget(lbl)
            self.body.addStretch()
        except Exception as exc:
            import traceback
            err = QLabel(f"Error loading Risk page:\n{exc}\n\n{traceback.format_exc()}")
            err.setStyleSheet(f"color: {T.RED}; font-size: 11px; border: none;")
            err.setWordWrap(True)
            self.body.addWidget(err)
            self.body.addStretch()

    # ── Allocation by strategy ────────────────────────────────────────────────

    def _build_allocation(self, instances, unassigned, overrides):
        self.body.addWidget(self._section_header("PORTFOLIO ALLOCATION BY STRATEGY"))
        card, lay = self._card()

        rows, total = strategy_allocation(instances, unassigned, overrides)

        if not rows or total <= 0:
            lay.addWidget(QLabel("No capital data available."))
            self.body.addWidget(card)
            return

        # Header row
        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(0)
        for text, width, align in [
            ("Strategy",  240, Qt.AlignmentFlag.AlignLeft),
            ("Ticker",     70, Qt.AlignmentFlag.AlignLeft),
            ("",            0, Qt.AlignmentFlag.AlignLeft),
            ("Allocation", 70, Qt.AlignmentFlag.AlignRight),
            ("Capital",   115, Qt.AlignmentFlag.AlignRight),
            ("Open P&L",  100, Qt.AlignmentFlag.AlignRight),
        ]:
            if width == 0:
                hdr_row.addStretch(1)
                continue
            lbl = QLabel(text)
            lbl.setFixedWidth(width)
            lbl.setAlignment(align)
            lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; font-weight: bold; "
                f"letter-spacing: 0.4px; border: none;"
            )
            hdr_row.addWidget(lbl)
        lay.addLayout(hdr_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {T.BORDER}; max-height: 1px; border: none;")
        lay.addWidget(sep)

        for r in rows:
            row_w = QHBoxLayout()
            row_w.setSpacing(0)

            strat_name = r.get("name") or r["root"]
            name_lbl = QLabel(strat_name)
            name_lbl.setFixedWidth(240)
            name_lbl.setStyleSheet(
                f"color: {T.TEXT}; font-size: 12px; font-weight: bold; border: none;"
            )
            row_w.addWidget(name_lbl)

            root_lbl = QLabel(r["root"])
            root_lbl.setFixedWidth(70)
            root_lbl.setStyleSheet(
                f"color: {T.ACCENT}; font-size: 11px; border: none;"
            )
            row_w.addWidget(root_lbl)

            # Allocation bar
            bar_outer = QFrame()
            bar_outer.setFixedHeight(10)
            bar_outer.setStyleSheet(
                f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; "
                f"border-radius: 5px; }}"
            )
            bar_lay = QHBoxLayout(bar_outer)
            bar_lay.setContentsMargins(0, 0, 0, 0)
            bar_lay.setSpacing(0)
            fill = QFrame()
            fill_color = (T.RED if r["pct"] >= 40
                          else (T.YELLOW if r["pct"] >= 20 else T.PURPLE))
            fill.setStyleSheet(
                f"QFrame {{ background: {fill_color}; border: none; border-radius: 4px; }}"
            )
            sf = int(max(1, round(r["pct"] * 10)))
            sr = int(max(1, round((100 - r["pct"]) * 10)))
            bar_lay.addWidget(fill, sf)
            spacer = QWidget()
            spacer.setStyleSheet("background: transparent;")
            bar_lay.addWidget(spacer, sr)
            row_w.addWidget(bar_outer, 1)

            pct_lbl = QLabel(f"{r['pct']:.1f}%")
            pct_lbl.setFixedWidth(70)
            pct_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            pct_lbl.setStyleSheet(
                f"color: {T.TEXT_DIM}; font-size: 12px; border: none;"
            )
            row_w.addWidget(pct_lbl)

            cap_lbl = QLabel(money(r["capital"]))
            cap_lbl.setFixedWidth(115)
            cap_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            cap_lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 11px; border: none;"
            )
            row_w.addWidget(cap_lbl)

            pnl_val  = r.get("pnl") or 0.0
            pnl_text = money(pnl_val, signed=True)
            pnl_c    = pnl_color(pnl_val)
            pnl_lbl  = QLabel(pnl_text)
            pnl_lbl.setFixedWidth(100)
            pnl_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            pnl_lbl.setStyleSheet(
                f"color: {pnl_c}; font-size: 12px; font-weight: bold; border: none;"
            )
            row_w.addWidget(pnl_lbl)

            lay.addLayout(row_w)

        # Total footer
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"background: {T.BORDER}; max-height: 1px; border: none;")
        lay.addWidget(sep2)

        total_row = QHBoxLayout()
        total_lbl = QLabel("Total deployed")
        total_lbl.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; border: none;"
        )
        total_row.addWidget(total_lbl)
        total_row.addStretch()
        total_val = QLabel(money(total))
        total_val.setStyleSheet(
            f"color: {T.TEXT}; font-size: 12px; font-weight: bold; border: none;"
        )
        total_row.addWidget(total_val)
        lay.addLayout(total_row)

        self.body.addWidget(card)

    # ── Earnings calendar ─────────────────────────────────────────────────────

    def _build_earnings_calendar(self, metrics):
        self.body.addWidget(self._section_header("EARNINGS CALENDAR"))
        card = EarningsCalendar(metrics, self.portfolio.token)
        self.body.addWidget(card)
