"""Risk Management page — portfolio allocation by strategy + P&L calendar."""
from datetime import date, timedelta

import numpy as np
import matplotlib
matplotlib.use("QtAgg")
import matplotlib.pyplot as plt
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


# ── Heatmap helper (also imported by app.py) ─────────────────────────────────

def build_heatmap_canvas(history):
    """
    Build and return a matplotlib FigureCanvas calendar heatmap of daily P&L.
    Returns None if there are no closed trades.
    """
    today = date.today()
    start = today - timedelta(weeks=52)
    daily = {}
    for lot in history:
        raw = lot.get("closed_at") or lot.get("close_date") or ""
        try:
            d = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if d >= start:
            daily[d] = daily.get(d, 0.0) + float(lot.get("pnl") or 0)

    if not daily:
        return None

    start_dow = start.weekday()
    num_weeks = 53
    grid = np.full((7, num_weeks), np.nan)
    for day_off in range((today - start).days + 1):
        d   = start + timedelta(days=day_off)
        col = (day_off + start_dow) // 7
        row = d.weekday()
        if col < num_weeks:
            grid[row, col] = daily.get(d, np.nan)

    fig, ax = plt.subplots(figsize=(14, 1.8))
    fig.patch.set_facecolor("#161928")
    ax.set_facecolor("#161928")

    vmax = max(abs(v) for v in daily.values())
    vmax = max(vmax, 1)

    norm    = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)
    masked  = np.ma.masked_invalid(grid)
    ax.imshow(masked, cmap="RdYlGn", norm=norm, aspect="auto",
              interpolation="nearest")

    nan_overlay = np.where(np.isnan(grid), 1.0, np.nan)
    ax.imshow(nan_overlay, cmap=ListedColormap(["#1a1d2e"]),
              aspect="auto", interpolation="nearest", vmin=0, vmax=1)

    # Month labels
    cur_month, month_labels, month_cols = None, [], []
    for day_off in range((today - start).days + 1):
        d   = start + timedelta(days=day_off)
        col = (day_off + start_dow) // 7
        if d.month != cur_month and col < num_weeks:
            month_labels.append(d.strftime("%b"))
            month_cols.append(col)
            cur_month = d.month
    ax.set_xticks(month_cols)
    ax.set_xticklabels(month_labels, color="#64748b", fontsize=8)
    ax.set_yticks(range(7))
    ax.set_yticklabels(["M", "T", "W", "T", "F", "S", "S"],
                       color="#64748b", fontsize=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(length=0)
    fig.tight_layout(pad=0.3)

    canvas = FigureCanvas(fig)
    canvas.setFixedHeight(130)
    canvas.setStyleSheet("background: transparent;")
    plt.close(fig)
    return canvas


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

        self._build_allocation(instances, unassigned, overrides)
        self._build_heatmap()
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

    def _build_heatmap(self):
        self.body.addWidget(self._section_header("P&L CALENDAR  (closed trades, last 52 weeks)"))
        card, lay = self._card()

        canvas = build_heatmap_canvas(self.portfolio.history)
        if canvas:
            lay.addWidget(canvas)

            # Legend
            legend_row = QHBoxLayout()
            legend_row.addStretch()
            for color, label in [("#f87171", "Loss"), ("#1a1d2e", "No trades"), ("#4ade80", "Profit")]:
                dot = QFrame()
                dot.setFixedSize(10, 10)
                dot.setStyleSheet(
                    f"background: {color}; border-radius: 2px; border: none;"
                )
                legend_row.addWidget(dot)
                lbl = QLabel(label)
                lbl.setStyleSheet(
                    f"color: {T.MUTED}; font-size: 10px; border: none; margin-right: 8px;"
                )
                legend_row.addWidget(lbl)
            lay.addLayout(legend_row)
        else:
            empty = QLabel("No closed trades yet — P&L calendar will appear here.")
            empty.setStyleSheet(
                f"color: {T.MUTED}; font-size: 12px; border: none;"
            )
            lay.addWidget(empty)

        self.body.addWidget(card)
