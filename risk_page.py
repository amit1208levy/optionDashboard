"""Risk Management page — portfolio allocation by strategy + P&L calendar."""
from datetime import date, timedelta

import numpy as np
import matplotlib
matplotlib.use("QtAgg")
from matplotlib.figure import Figure as MplFigure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.colors import TwoSlopeNorm, ListedColormap

import theme as T
from models import (
    StrategyInstance, unassigned_positions, group_unassigned, strategy_allocation,
)
from strategy_card import money, pnl_color

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea,
)


# ── Calendar card ─────────────────────────────────────────────────────────────

class CalendarCard(QFrame):
    """Collapsible P&L calendar with hover details, expiration + earnings markers."""

    NUM_WEEKS = 53

    def __init__(self, history, metrics=None, parent=None):
        super().__init__(parent)
        self._metrics  = metrics or {}
        self._expanded = False

        today = date.today()
        self._start    = today - timedelta(weeks=52)
        self._today    = today
        self._start_dow = self._start.weekday()

        # Precompute daily totals and per-day trade lists
        self._daily   = {}   # date -> float (total P&L)
        self._entries = {}   # date -> list[lot]
        for lot in history:
            raw = lot.get("closed_at") or lot.get("close_date") or ""
            try:
                d = date.fromisoformat(str(raw)[:10])
            except ValueError:
                continue
            if d >= self._start:
                self._daily[d]   = self._daily.get(d, 0.0) + float(lot.get("pnl") or 0)
                self._entries.setdefault(d, []).append(lot)

        self._expirations = self._calc_expirations()
        self._earnings    = self._calc_earnings()

        self.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 12px; }}"
        )
        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        main_lay.addWidget(self._build_header())

        # Collapsible content
        self._content_w = QWidget()
        self._content_w.setStyleSheet("background: transparent;")
        c_lay = QVBoxLayout(self._content_w)
        c_lay.setContentsMargins(18, 4, 18, 16)
        c_lay.setSpacing(6)

        if self._daily:
            canvas = self._build_canvas()
            if canvas:
                c_lay.addWidget(canvas)
        else:
            empty = QLabel("No closed trades yet — P&L calendar will appear once trades close.")
            empty.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
            c_lay.addWidget(empty)

        # Hover detail label
        self._hover_lbl = QLabel("Hover over a day to see details")
        self._hover_lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none;"
        )
        c_lay.addWidget(self._hover_lbl)

        # Legend
        c_lay.addLayout(self._build_legend())

        self._content_w.setVisible(False)
        main_lay.addWidget(self._content_w)

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self):
        hdr = QFrame()
        hdr.setStyleSheet("background: transparent; border: none; border-radius: 12px;")
        hdr.setCursor(Qt.CursorShape.PointingHandCursor)
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(18, 14, 18, 14)
        hl.setSpacing(8)

        self._chevron = QLabel("▶")
        self._chevron.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; border: none;"
        )
        hl.addWidget(self._chevron)

        title = QLabel("P&L CALENDAR  (closed trades · last 52 weeks)")
        title.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"letter-spacing: 0.8px; border: none;"
        )
        hl.addWidget(title)
        hl.addStretch()

        if self._daily:
            n_days   = len(self._daily)
            n_profit = sum(1 for v in self._daily.values() if v > 0)
            summary  = QLabel(f"{n_profit}/{n_days} profitable days")
            summary.setStyleSheet(
                f"color: {T.MUTED}; font-size: 11px; border: none;"
            )
            hl.addWidget(summary)

        hdr.mousePressEvent = lambda _e: self._toggle()
        return hdr

    def _toggle(self):
        self._expanded = not self._expanded
        self._content_w.setVisible(self._expanded)
        self._chevron.setText("▼" if self._expanded else "▶")

    # ── Canvas ────────────────────────────────────────────────────────────────

    def _build_canvas(self):
        try:
            return self._build_canvas_impl()
        except Exception:
            return None

    def _build_canvas_impl(self):
        start     = self._start
        today     = self._today
        start_dow = self._start_dow
        nw        = self.NUM_WEEKS

        # Build grid
        grid = np.full((7, nw), np.nan)
        for day_off in range((today - start).days + 1):
            d   = start + timedelta(days=day_off)
            col = (day_off + start_dow) // 7
            row = d.weekday()
            if col < nw:
                grid[row, col] = self._daily.get(d, np.nan)

        # Store grid metadata for hover lookup
        self._grid      = grid
        self._start_dow = start_dow

        # Use Figure() directly — avoids pyplot's figure manager so we don't
        # need to call plt.close() (which can corrupt the canvas before first paint)
        fig = MplFigure(figsize=(14, 2.0))
        fig.patch.set_facecolor("#161928")
        ax = fig.add_subplot(111)
        ax.set_facecolor("#161928")

        vmax = max((abs(v) for v in self._daily.values()), default=1)
        vmax = max(vmax, 1)
        norm = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)

        masked = np.ma.masked_invalid(grid)
        ax.imshow(masked, cmap="RdYlGn", norm=norm, aspect="auto",
                  interpolation="nearest")

        # Empty-cell overlay
        nan_ov = np.where(np.isnan(grid), 1.0, np.nan)
        ax.imshow(nan_ov, cmap=ListedColormap(["#1a1d2e"]),
                  aspect="auto", interpolation="nearest", vmin=0, vmax=1)

        # ── Expiration markers (white dot, top-right of cell) ─────────────
        for d in self._expirations:
            if start <= d <= today:
                day_off = (d - start).days
                col = (day_off + start_dow) // 7
                row = d.weekday()
                if 0 <= col < nw:
                    ax.plot(col + 0.35, row - 0.35, "o", color="white",
                            markersize=3, markeredgewidth=0, zorder=5)

        # ── Earnings markers (yellow dot, bottom-right of cell) ───────────
        earn_xs, earn_ys, earn_labels = [], [], []
        for d, tickers in self._earnings.items():
            if start <= d <= today + timedelta(days=60):
                day_off = (d - start).days
                col = (day_off + start_dow) // 7
                row = d.weekday()
                if 0 <= col < nw:
                    earn_xs.append(col + 0.35)
                    earn_ys.append(row + 0.35)
                    earn_labels.append(", ".join(tickers[:2]))
        if earn_xs:
            ax.scatter(earn_xs, earn_ys, s=18, color="#fbbf24",
                       zorder=6, linewidths=0)

        # Month labels
        cur_month = None
        month_cols, month_lbls = [], []
        for day_off in range((today - start).days + 1):
            d   = start + timedelta(days=day_off)
            col = (day_off + start_dow) // 7
            if d.month != cur_month and col < nw:
                month_cols.append(col)
                month_lbls.append(d.strftime("%b"))
                cur_month = d.month
        ax.set_xticks(month_cols)
        ax.set_xticklabels(month_lbls, color="#64748b", fontsize=8)
        ax.set_yticks(range(7))
        ax.set_yticklabels(["M", "T", "W", "T", "F", "S", "S"],
                           color="#64748b", fontsize=8)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(length=0)
        ax.set_xlim(-0.5, nw - 0.5)
        ax.set_ylim(6.5, -0.5)
        fig.tight_layout(pad=0.2)

        canvas = FigureCanvas(fig)
        canvas.setFixedHeight(140)
        canvas.setStyleSheet("background: transparent;")

        # Hover event
        def on_motion(event):
            if event.inaxes != ax or event.xdata is None:
                self._hover_lbl.setText("Hover over a day to see details")
                return
            col = int(round(event.xdata))
            row = int(round(event.ydata))
            if not (0 <= col < nw and 0 <= row < 7):
                self._hover_lbl.setText("Hover over a day to see details")
                return
            day_off = col * 7 + row - start_dow
            if day_off < 0:
                return
            d = start + timedelta(days=day_off)
            if d > today:
                self._hover_lbl.setText("")
                return

            parts = [f"<b>{d.strftime('%a %b %d, %Y')}</b>"]

            pnl = self._daily.get(d)
            if pnl is not None:
                sign = "+" if pnl >= 0 else ""
                parts.append(f"P&L: {sign}${pnl:,.2f}")
                trades = self._entries.get(d, [])
                if trades:
                    roots = sorted({t.get('root') or '' for t in trades if t.get('root')})
                    parts.append(f"{len(trades)} trade{'s' if len(trades)>1 else ''}: {', '.join(roots[:4])}")
            else:
                parts.append("No closed trades")

            if d in self._expirations:
                parts.append("● Options expiration")
            earn = self._earnings.get(d)
            if earn:
                parts.append(f"● Earnings: {', '.join(earn)}")

            self._hover_lbl.setText("  ·  ".join(parts))

        canvas.mpl_connect("motion_notify_event", on_motion)
        # Keep a strong reference so the figure isn't GC'd before first paint
        canvas._mpl_fig = fig
        return canvas

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _calc_expirations(self):
        """Third Friday of each month = standard monthly expiration."""
        exps = set()
        start  = self._start - timedelta(days=31)
        end    = self._today + timedelta(days=90)
        year, month = start.year, start.month
        while date(year, month, 1) <= end:
            first = date(year, month, 1)
            days_to_fri = (4 - first.weekday()) % 7
            third_fri   = first + timedelta(days=days_to_fri) + timedelta(weeks=2)
            exps.add(third_fri)
            month += 1
            if month > 12:
                month = 1
                year += 1
        return exps

    def _calc_earnings(self):
        """Return {date: [ticker]} from portfolio metrics earnings field."""
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
        return result

    def _build_legend(self):
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addStretch()
        for color, label in [
            ("#f87171", "Loss day"),
            ("#1a1d2e", "No trades"),
            ("#4ade80", "Profit day"),
            ("white",   "● Expiration"),
            ("#fbbf24", "● Earnings"),
        ]:
            dot = QFrame()
            dot.setFixedSize(10, 10)
            dot.setStyleSheet(
                f"background: {color}; border-radius: 2px; border: 1px solid #333;"
            )
            row.addWidget(dot)
            lbl = QLabel(label)
            lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; border: none; margin-right: 6px;"
            )
            row.addWidget(lbl)
        return row


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
                self._build_heatmap(metrics)
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
            ("",            0, Qt.AlignmentFlag.AlignLeft),   # bar spacer
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

            # Strategy / name
            strat_name = r.get("name") or r["root"]
            name_lbl = QLabel(strat_name)
            name_lbl.setFixedWidth(240)
            name_lbl.setStyleSheet(
                f"color: {T.TEXT}; font-size: 12px; font-weight: bold; border: none;"
            )
            row_w.addWidget(name_lbl)

            # Ticker
            root_lbl = QLabel(r["root"])
            root_lbl.setFixedWidth(70)
            root_lbl.setStyleSheet(
                f"color: {T.ACCENT}; font-size: 11px; border: none;"
            )
            row_w.addWidget(root_lbl)

            # Bar
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
            from PyQt6.QtWidgets import QWidget as _W
            spacer = _W(); spacer.setStyleSheet("background: transparent;")
            bar_lay.addWidget(spacer, sr)
            row_w.addWidget(bar_outer, 1)

            # Allocation %
            pct_lbl = QLabel(f"{r['pct']:.1f}%")
            pct_lbl.setFixedWidth(70)
            pct_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            pct_lbl.setStyleSheet(
                f"color: {T.TEXT_DIM}; font-size: 12px; border: none;"
            )
            row_w.addWidget(pct_lbl)

            # Capital
            cap_lbl = QLabel(money(r["capital"]))
            cap_lbl.setFixedWidth(115)
            cap_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            cap_lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 11px; border: none;"
            )
            row_w.addWidget(cap_lbl)

            # P&L
            pnl_val  = r.get("pnl") or 0.0
            pnl_text = money(pnl_val, signed=True)
            pnl_c    = pnl_color(pnl_val)
            pnl_lbl = QLabel(pnl_text)
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

    # ── P&L Heatmap ───────────────────────────────────────────────────────────

    def _build_heatmap(self, metrics=None):
        card = CalendarCard(self.portfolio.history, metrics=metrics)
        self.body.addWidget(card)
