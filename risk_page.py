"""Risk Management page — portfolio allocation + SPY & VIX scenario charts."""
import numpy as np

import matplotlib
matplotlib.use("QtAgg")                     # must be set before other mpl imports
import matplotlib.pyplot as plt
import matplotlib.ticker
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSizePolicy,
)

import api
import theme as T
from models import (
    StrategyInstance, unassigned_positions, group_unassigned,
    strategy_allocation, portfolio_greeks, symbol_beta, _is_future_option,
)
from strategy_card import money, pnl_color


# ── helpers ──────────────────────────────────────────────────────────────────

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Color palette for strategy segments — cohesive cool/violet tonal palette
# anchored on the app's purple accent, with blue/teal/indigo siblings and a
# single warm pop (rose) for contrast.  Avoids the saturated orange/lime/cyan
# that clashed with the rest of the dashboard's slate-purple look.
_PALETTE = [
    "#6d28d9",   # violet-700  (deep purple — primary)
    "#0f766e",   # teal-700
    "#4338ca",   # indigo-700
    "#1d4ed8",   # blue-700
    "#b45309",   # amber-700
    "#0d9488",   # teal-600
    "#0369a1",   # sky-700
    "#7c3aed",   # violet-600
    "#15803d",   # green-700
    "#be185d",   # pink-700
]

def _style_ax(ax, xlabel: str = "", ylabel: str = ""):
    """Apply the same clean style used by PayoffChart across the app."""
    ax.set_facecolor(T.CARD)
    ax.tick_params(colors=T.MUTED, labelsize=8, length=3)
    for spine in ax.spines.values():
        spine.set_color(T.BORDER)
        spine.set_linewidth(0.8)
    if xlabel:
        ax.set_xlabel(xlabel, color=T.MUTED, fontsize=9)
    if ylabel:
        ax.set_ylabel(ylabel, color=T.MUTED, fontsize=9)
    ax.grid(color=T.BORDER, alpha=0.25, linewidth=0.6)


# ── background workers ────────────────────────────────────────────────────────

class _PriceWorker(QThread):
    """Fetch SPY and VIX current prices in the background."""
    done = pyqtSignal(float, float)   # spy_price, vix_price

    def __init__(self, token, positions, quotes=None):
        super().__init__()
        self._token     = token
        self._positions = list(positions)
        # Optional QuotesProvider — falls back to direct TastyTrade if None.
        self._quotes    = quotes

    def run(self):
        spy = vix = None

        # Fast path: SPY underlying_price is already in portfolio positions
        for p in self._positions:
            if p.root == "SPY" and p.underlying_price and p.underlying_price > 0:
                spy = p.underlying_price
                break

        try:
            if self._quotes is not None:
                quotes = self._quotes.get_quotes(equities=["SPY", "VIX"])
            else:
                quotes = api.get_market_data(self._token, equities=["SPY", "VIX"])

            if spy is None:
                q = quotes.get("SPY", {})
                b, a = _f(q.get("bid")), _f(q.get("ask"))
                if b and a and b > 0 and a > 0:
                    spy = (b + a) / 2.0
                elif _f(q.get("last")):
                    spy = _f(q.get("last"))
                elif _f(q.get("mark")):
                    spy = _f(q.get("mark"))

            q = quotes.get("VIX", {})
            b, a = _f(q.get("bid")), _f(q.get("ask"))
            if b and a and b > 0 and a > 0:
                vix = (b + a) / 2.0
            elif _f(q.get("last")):
                vix = _f(q.get("last"))
            elif _f(q.get("mark")):
                vix = _f(q.get("mark"))
        except Exception:
            pass

        # Fallbacks when market is closed or API doesn't support these symbols
        self.done.emit(spy or 500.0, vix or 18.0)


# ── allocation row widget ─────────────────────────────────────────────────────

class _AllocationRow(QFrame):
    """
    One strategy row in the allocation table.
    The full row height is 68 px; left edge has a colored accent bar.
    The % allocation is the most prominent number.
    """

    def __init__(self, row_data: dict, color: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(68)
        self.setStyleSheet(
            f"QFrame {{ background: {T.BG_ALT}; border: 1px solid {T.BORDER}; "
            f"border-radius: 10px; }}"
        )

        hl = QHBoxLayout(self)
        hl.setContentsMargins(0, 0, 20, 0)
        hl.setSpacing(0)

        # ── left color accent ────────────────────────────────────────────────
        accent = QFrame()
        accent.setFixedWidth(6)
        accent.setStyleSheet(
            f"QFrame {{ background: {color}; border: none; border-radius: 0; "
            f"border-top-left-radius: 9px; border-bottom-left-radius: 9px; }}"
        )
        hl.addWidget(accent)
        hl.addSpacing(16)

        # ── strategy name + ticker ───────────────────────────────────────────
        name_col = QVBoxLayout()
        name_col.setSpacing(4)
        name_col.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        name = row_data.get("name") or row_data["root"]
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(
            f"color: {T.TEXT}; font-size: 14px; font-weight: bold; border: none;"
        )
        name_col.addWidget(name_lbl)

        root_lbl = QLabel(row_data["root"])
        root_lbl.setStyleSheet(
            f"color: white; background: {color}; border: none; border-radius: 5px; "
            f"padding: 1px 8px; font-size: 10px; font-weight: bold;"
        )
        root_lbl.setMaximumWidth(80)
        name_col.addWidget(root_lbl)

        hl.addLayout(name_col, 1)

        # ── % allocation (biggest number on the row) ─────────────────────────
        pct_val = row_data["pct"]
        pct_color = (T.RED if pct_val >= 40
                     else (T.YELLOW if pct_val >= 20 else T.TEAL))
        pct_lbl = QLabel(f"{pct_val:.1f}%")
        pct_lbl.setFixedWidth(80)
        pct_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pct_lbl.setStyleSheet(
            f"color: {pct_color}; font-size: 22px; font-weight: bold; border: none;"
        )
        hl.addWidget(pct_lbl)
        hl.addSpacing(20)

        # ── capital ──────────────────────────────────────────────────────────
        cap_lbl = QLabel(money(row_data["capital"]))
        cap_lbl.setFixedWidth(110)
        cap_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        cap_lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none;"
        )
        hl.addWidget(cap_lbl)
        hl.addSpacing(16)

        # ── open P&L ─────────────────────────────────────────────────────────
        pnl_val  = row_data.get("pnl") or 0.0
        pnl_c    = pnl_color(pnl_val)
        pnl_lbl  = QLabel(money(pnl_val, signed=True))
        pnl_lbl.setFixedWidth(110)
        pnl_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pnl_lbl.setStyleSheet(
            f"color: {pnl_c}; font-size: 13px; font-weight: bold; border: none;"
        )
        hl.addWidget(pnl_lbl)


# ── chart helpers ─────────────────────────────────────────────────────────────

def _make_canvas(width_px=500, height_px=300) -> FigureCanvasQTAgg:
    """Return an MPL canvas sized to the given pixel dimensions."""
    dpi = 96
    fig = Figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi,
                 facecolor=T.CARD)
    fig.subplots_adjust(left=0.12, right=0.97, top=0.93, bottom=0.18)
    canvas = FigureCanvasQTAgg(fig)
    canvas.setStyleSheet(f"background: {T.CARD};")
    canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    canvas.setFixedHeight(height_px)
    return canvas


def _bw_gamma(positions, metrics_by_root) -> float:
    """Beta-weighted gamma: Σ gamma × beta² × qty × mult × sign."""
    total = 0.0
    for p in positions:
        if not p.is_option or p.gamma is None:
            continue
        mult = 100 if not _is_future_option(p.instrument_type) else 1
        beta = symbol_beta(metrics_by_root.get(p.root)) or 1.0
        total += p.gamma * p.quantity * mult * p.sign * (beta ** 2)
    return total


class _ScenarioChart(QFrame):
    """
    Scenario chart wrapper with hover tooltip + zoom buttons.
    Used for both SPY and VIX scenario charts on the Risk page.
    """

    def __init__(self, parent=None, height_px=300):
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 10px; }}"
        )
        self.setFixedHeight(height_px + 12)

        # Canvas
        self.canvas = _make_canvas(width_px=800, height_px=height_px)
        self.canvas.setParent(self)
        # Let mouse-wheel events propagate up to the scroll area instead of
        # being swallowed by the matplotlib canvas.
        self.canvas.wheelEvent = lambda ev: ev.ignore()

        # Zoom buttons overlaid top-right
        self.zoom_in_btn  = self._zoom_btn("＋", self)
        self.zoom_out_btn = self._zoom_btn("－", self)
        self.zoom_in_btn.clicked.connect(self._on_zoom_in)
        self.zoom_out_btn.clicked.connect(self._on_zoom_out)

        # Hover state
        self._xs: list = []
        self._ys: list = []
        self._cursor_line = None
        self._cursor_dot  = None
        self._annot       = None
        self._ax          = None
        self._zoom_factor = 1.0
        self._base_xmin   = 0.0
        self._base_xmax   = 0.0
        self._center      = 0.0      # current x (SPY or VIX)
        self._fmt_x       = lambda v: f"{v:,.2f}"
        self._fmt_y       = lambda v: f"{v:,.2f}"
        self._xlabel      = "X"
        self._ylabel      = "Y"

        self.canvas.mpl_connect("motion_notify_event", self._on_motion)

    def _zoom_btn(self, text, parent):
        btn = QPushButton(text, parent)
        btn.setFixedSize(26, 24)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ background: {T.BG_ALT}; color: {T.TEXT}; "
            f"border: 1px solid {T.BORDER}; border-radius: 4px; "
            f"font-size: 14px; font-weight: bold; padding: 0; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        return btn

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w, h = self.width(), self.height()
        self.canvas.setGeometry(0, 0, w, h - 12)
        # Zoom buttons in top-right corner
        self.zoom_out_btn.move(w - 64, 8)
        self.zoom_in_btn.move(w - 34, 8)
        self.zoom_out_btn.raise_()
        self.zoom_in_btn.raise_()

    # ── plotting (called by subclass-like closures) ──────────────────────────

    def draw(self, x: np.ndarray, y: np.ndarray, center: float,
             accent: str, xlabel: str, ylabel: str,
             fmt_x, fmt_y):
        self._xs = list(x)
        self._ys = list(y)
        self._center = center
        self._base_xmin = float(x.min())
        self._base_xmax = float(x.max())
        self._zoom_factor = 1.0
        self._fmt_x = fmt_x
        self._fmt_y = fmt_y
        self._xlabel = xlabel
        self._ylabel = ylabel

        fig = self.canvas.figure
        fig.clear()
        fig.patch.set_facecolor(T.CARD)
        ax  = fig.add_subplot(111)
        self._ax = ax

        y0 = y[(np.abs(x - center)).argmin()] if len(x) else 0
        ax.axhline(y0, color=T.MUTED, linewidth=0.8, linestyle="--", alpha=0.6)
        ax.axvline(center, color=T.BORDER_H, linewidth=0.6, linestyle=":", alpha=0.6)
        ax.fill_between(x, y0, y, where=(y >  y0), alpha=0.18,
                        color=T.GREEN, interpolate=True)
        ax.fill_between(x, y0, y, where=(y <= y0), alpha=0.18,
                        color=T.RED,   interpolate=True)
        ax.plot(x, y, color=accent, linewidth=1.8)
        ax.scatter([center], [y0], color=T.ACCENT, zorder=5, s=30)

        _style_ax(ax, xlabel=xlabel, ylabel=ylabel)
        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: fmt_x(v)))
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: fmt_y(v)))

        # Hover cursor
        self._cursor_line = ax.axvline(x[0], color=T.ACCENT, linewidth=0.8,
                                       alpha=0.5, visible=False)
        self._cursor_dot, = ax.plot([x[0]], [y[0]], "o", color=T.ACCENT,
                                    markersize=5, visible=False)
        self._annot = ax.annotate(
            "", xy=(0, 0), xytext=(10, 12), textcoords="offset points",
            ha="left", va="bottom",
            bbox=dict(boxstyle="round,pad=0.4", fc=T.BG_ALT, ec=T.BORDER, alpha=0.95),
            color=T.TEXT, fontsize=9, visible=False,
        )
        self.canvas.draw()

    def _apply_zoom(self):
        if not self._ax:
            return
        half = (self._base_xmax - self._base_xmin) / 2.0 * self._zoom_factor
        self._ax.set_xlim(self._center - half, self._center + half)
        self.canvas.draw_idle()

    def _on_zoom_in(self):
        self._zoom_factor = max(self._zoom_factor * 0.75, 0.1)
        self._apply_zoom()

    def _on_zoom_out(self):
        self._zoom_factor = min(self._zoom_factor / 0.75, 4.0)
        self._apply_zoom()

    def _on_motion(self, event):
        if event.inaxes != self._ax or not self._xs:
            self._hide_cursor()
            return
        x = event.xdata
        if x is None:
            self._hide_cursor()
            return
        # Clamp to visible x range so the cursor never "jumps" to the edge
        # sample when the mouse wanders past the plotted data.
        xl, xr = self._ax.get_xlim()
        if x < xl or x > xr:
            self._hide_cursor()
            return

        idx = min(range(len(self._xs)), key=lambda i: abs(self._xs[i] - x))
        sx, sy = self._xs[idx], self._ys[idx]
        self._cursor_line.set_xdata([sx, sx])
        self._cursor_line.set_visible(True)
        self._cursor_dot.set_data([sx], [sy])
        self._cursor_dot.set_visible(True)

        # Flip annotation to stay inside axes near edges.
        yl, yu = self._ax.get_ylim()
        x_off = -10 if sx > (xl + xr) / 2 else 10
        y_off = -12 if sy > (yl + yu) / 2 else 12
        self._annot.set_position((x_off, y_off))
        self._annot.set_ha("right" if x_off < 0 else "left")
        self._annot.set_va("top"   if y_off < 0 else "bottom")

        self._annot.xy = (sx, sy)
        # Delta vs. the current underlying price (the anchor dashed line)
        delta   = sx - self._center
        delta_s = "+" if delta >= 0 else "−"
        pct = (delta / self._center * 100.0) if self._center else 0.0
        pct_s   = "+" if pct >= 0 else "−"
        self._annot.set_text(
            f"{self._xlabel}: {self._fmt_x(sx)}\n"
            f"{self._ylabel}: {self._fmt_y(sy)}\n"
            f"vs now: {delta_s}{self._fmt_x(abs(delta))}   "
            f"({pct_s}{abs(pct):.1f}%)"
        )
        self._annot.set_visible(True)
        self.canvas.draw_idle()

    def _hide_cursor(self):
        if self._cursor_line is None:
            return
        self._cursor_line.set_visible(False)
        self._cursor_dot.set_visible(False)
        self._annot.set_visible(False)
        self.canvas.draw_idle()


def _draw_spy_chart(chart: "_ScenarioChart",
                    spy: float, net_liq: float,
                    bwd: float, bwg: float):
    """SPY scenario chart — account value / SPY across ±25% price range."""
    x   = np.linspace(spy * 0.75, spy * 1.25, 400)
    ds  = x - spy
    pnl = bwd * ds + 0.5 * bwg * ds ** 2
    y   = (net_liq + pnl) / x
    chart.draw(
        x, y, center=spy, accent=T.ACCENT,
        xlabel="SPY Price", ylabel="Account / SPY",
        fmt_x=lambda v: f"${v:,.0f}",
        fmt_y=lambda v: f"{v:,.2f}",
    )


def _draw_vix_chart(chart: "_ScenarioChart",
                    vix: float, net_liq: float, net_vega: float):
    """VIX scenario chart — account value / VIX across 0.4×–2.5× current VIX."""
    x_min = max(5.0,  vix * 0.4)
    x_max = min(80.0, vix * 2.5)
    x   = np.linspace(x_min, x_max, 400)
    dv  = x - vix
    pnl = net_vega * dv
    y   = (net_liq + pnl) / x
    chart.draw(
        x, y, center=vix, accent=T.ACCENT,
        xlabel="VIX", ylabel="Account / VIX",
        fmt_x=lambda v: f"{v:.1f}",
        fmt_y=lambda v: f"{v:,.2f}",
    )


# ── main page ─────────────────────────────────────────────────────────────────

class RiskPage(QWidget):
    back_requested = pyqtSignal()

    def __init__(self, portfolio, parent=None):
        super().__init__(parent)
        self.portfolio = portfolio
        self._worker   = None
        self._spy_chart = None
        self._vix_chart = None
        self.setStyleSheet(T.BASE_STYLE)

        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)
        root_lay.addWidget(self._build_header())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body_w = QWidget()
        self.body = QVBoxLayout(body_w)
        self.body.setContentsMargins(28, 20, 28, 40)
        self.body.setSpacing(8)
        scroll.setWidget(body_w)
        root_lay.addWidget(scroll)

        self._populate()

    # ── header ────────────────────────────────────────────────────────────────

    def _build_header(self):
        hdr = QFrame()
        hdr.setFixedHeight(60)
        hdr.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border-bottom: 1px solid {T.BORDER}; "
            f"border-radius: 0; }}"
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

    # ── layout helpers ────────────────────────────────────────────────────────

    def _section_header(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"letter-spacing: 0.8px; border: none; "
            f"padding-top: 16px; padding-bottom: 4px;"
        )
        return lbl

    def _card_frame(self):
        f = QFrame()
        f.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 12px; }}"
        )
        lay = QVBoxLayout(f)
        lay.setContentsMargins(20, 16, 20, 18)
        lay.setSpacing(8)
        return f, lay

    # ── populate ──────────────────────────────────────────────────────────────

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

            metrics  = acct.get("metrics") or {}
            bal      = acct.get("balances", {})
            net_liq  = _f(bal.get("net-liquidating-value")) or 0.0
            greeks   = portfolio_greeks(positions, metrics)
            bwd      = greeks.get("beta_weighted_delta") or 0.0
            bwg      = _bw_gamma(positions, metrics)
            net_vega = greeks.get("net_vega") or 0.0

            # Allocation table
            self._build_allocation(instances, unassigned, overrides)

            # Chart sections (placeholder while prices load)
            self._build_chart_section("SPY CHART", T.PURPLE, "spy")
            self._build_chart_section("VIX CHART", T.TEAL,   "vix")

            self.body.addStretch()

            # Kick off background price fetch; render charts when done
            self._worker = _PriceWorker(
                self.portfolio.token, positions,
                quotes=getattr(self.portfolio, "quotes", None),
            )
            self._worker.done.connect(
                lambda s, v: self._on_prices(s, v, net_liq, bwd, bwg, net_vega)
            )
            self._worker.start()

        except Exception as exc:
            import traceback
            err = QLabel(f"Error:\n{exc}\n\n{traceback.format_exc()}")
            err.setStyleSheet(f"color: {T.RED}; font-size: 11px; border: none;")
            err.setWordWrap(True)
            self.body.addWidget(err)
            self.body.addStretch()

    # ── pie chart helper ──────────────────────────────────────────────────────

    def _build_pie_chart(self, labels, values, title):
        """Return a card containing a matplotlib pie chart."""
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 10px; }}"
        )
        inner = QVBoxLayout(card)
        inner.setContentsMargins(14, 12, 14, 14)
        inner.setSpacing(4)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"letter-spacing: 0.5px; border: none; background: transparent;"
        )
        inner.addWidget(title_lbl)

        canvas = _make_canvas(width_px=460, height_px=280)
        canvas.setFixedHeight(280)
        # Let scroll-wheel events bubble up to the page scroll area
        canvas.wheelEvent = lambda ev: ev.ignore()
        inner.addWidget(canvas)

        total = sum(values) or 1.0
        colors = [_PALETTE[i % len(_PALETTE)] for i in range(len(values))]

        fig = canvas.figure
        fig.clear()
        fig.patch.set_facecolor(T.CARD)
        # Leave the right ~45% of the figure for the legend; pie sits on the left.
        fig.subplots_adjust(left=0.0, right=0.55, top=0.98, bottom=0.02)
        ax = fig.add_subplot(111)
        ax.set_facecolor(T.CARD)

        def fmt(pct):
            return f"{pct:.0f}%" if pct >= 4 else ""

        wedges, _, _ = ax.pie(
            values,
            labels=None,
            colors=colors,
            autopct=fmt,
            startangle=90,
            counterclock=False,
            wedgeprops={"edgecolor": T.CARD, "linewidth": 1.5},
            pctdistance=0.75,
            textprops={"color": "white", "fontsize": 9, "fontweight": "bold"},
        )
        ax.axis("equal")

        # Right-side legend — colored swatches + label + percentage.
        legend_labels = [
            f"{lbl}  ·  {v/total*100:.1f}%"
            for lbl, v in zip(labels, values)
        ]
        ax.legend(
            wedges, legend_labels,
            loc="center left", bbox_to_anchor=(1.05, 0.5),
            frameon=False, fontsize=9,
            labelcolor=T.TEXT,
            handlelength=1.4, handletextpad=0.7,
        )
        canvas.draw()

        return card

    # ── allocation section ────────────────────────────────────────────────────

    def _build_allocation(self, instances, unassigned, overrides):
        self.body.addWidget(self._section_header("PORTFOLIO ALLOCATION BY STRATEGY"))
        card, lay = self._card_frame()

        rows, total = strategy_allocation(instances, unassigned, overrides)

        if not rows or total <= 0:
            lay.addWidget(QLabel("No capital data available."))
            self.body.addWidget(card)
            return

        # ── two pie charts side by side: by strategy | by ticker ─────────────
        pie_row = QHBoxLayout()
        pie_row.setSpacing(16)
        pie_row.addWidget(self._build_pie_chart(
            labels=[(r.get("name") or r["root"]) for r in rows],
            values=[r["capital"] for r in rows],
            title="By Strategy",
        ))
        # Aggregate by ticker
        ticker_totals: dict = {}
        for r in rows:
            root = r["root"] or "—"
            ticker_totals[root] = ticker_totals.get(root, 0.0) + r["capital"]
        ticker_items = sorted(ticker_totals.items(), key=lambda x: x[1], reverse=True)
        pie_row.addWidget(self._build_pie_chart(
            labels=[t for t, _ in ticker_items],
            values=[v for _, v in ticker_items],
            title="By Ticker",
        ))
        lay.addLayout(pie_row)

        # ── column headers ───────────────────────────────────────────────────
        lay.addSpacing(10)
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(22, 0, 0, 0)
        hdr_row.setSpacing(0)
        for text, width, align in [
            ("Strategy",  0,   Qt.AlignmentFlag.AlignLeft),
            ("Alloc",     90,  Qt.AlignmentFlag.AlignRight),
            ("Capital",   120, Qt.AlignmentFlag.AlignRight),
            ("Open P&L",  120, Qt.AlignmentFlag.AlignRight),
        ]:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; font-weight: bold; "
                f"letter-spacing: 0.4px; border: none;"
            )
            if width:
                lbl.setFixedWidth(width)
            lbl.setAlignment(align)
            hdr_row.addWidget(lbl, 0 if width else 1)
        lay.addLayout(hdr_row)
        lay.addSpacing(4)

        # ── per-strategy rows ────────────────────────────────────────────────
        for i, r in enumerate(rows):
            row = _AllocationRow(r, _PALETTE[i % len(_PALETTE)])
            lay.addWidget(row)
            lay.setSpacing(6)

        # ── footer ───────────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {T.BORDER}; max-height: 1px; border: none;")
        lay.addWidget(sep)

        footer = QHBoxLayout()
        tl = QLabel("Total deployed capital")
        tl.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; border: none;"
        )
        footer.addWidget(tl)
        footer.addStretch()
        tv = QLabel(money(total))
        tv.setStyleSheet(
            f"color: {T.TEXT}; font-size: 13px; font-weight: bold; border: none;"
        )
        footer.addWidget(tv)
        lay.addLayout(footer)

        self.body.addWidget(card)

    # ── chart sections ────────────────────────────────────────────────────────

    def _build_chart_section(self, title: str, accent_color: str, key: str):
        """Create a card with a section header and an embedded scenario chart."""
        self.body.addWidget(self._section_header(title))
        card, lay = self._card_frame()

        loading = QLabel("Fetching market data…")
        loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none; padding: 40px 0;"
        )
        lay.addWidget(loading)

        chart = _ScenarioChart(height_px=300)
        chart.setVisible(False)
        lay.addWidget(chart)

        # ── Least-neutral strategies dropdown ────────────────────────────
        # Shows strategies sorted by magnitude of their contribution to the
        # axis this chart represents:
        #   SPY → |beta-weighted delta|
        #   VIX → |net vega|
        metric_key = "bwd" if key == "spy" else "vega"
        drop = self._build_least_neutral_dropdown(metric_key)
        lay.addWidget(drop)

        self.body.addWidget(card)

        if key == "spy":
            self._spy_chart   = chart
            self._spy_loading = loading
        else:
            self._vix_chart   = chart
            self._vix_loading = loading

    def _build_least_neutral_dropdown(self, metric_key: str):
        """
        Collapsible list of the strategies furthest from neutral for the
        given risk axis: 'bwd' for SPY chart, 'vega' for VIX chart.
        """
        wrap = QFrame()
        wrap.setStyleSheet(
            f"QFrame {{ background: {T.BG_ALT}; border: 1px solid {T.BORDER}; "
            f"border-radius: 8px; }}"
        )
        out = QVBoxLayout(wrap)
        out.setContentsMargins(0, 0, 0, 0)
        out.setSpacing(0)

        label = ("Least-neutral strategies by |β-Wtd Δ|"
                 if metric_key == "bwd"
                 else "Least-neutral strategies by |Net Vega|")

        # Toggle header
        header = QPushButton(f"▸  {label}")
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header.setStyleSheet(
            f"QPushButton {{ text-align: left; background: transparent; "
            f"color: {T.TEXT_DIM}; border: none; padding: 10px 14px; "
            f"font-size: 12px; font-weight: bold; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; }}"
        )
        out.addWidget(header)

        body = QFrame()
        body.setStyleSheet("background: transparent; border: none;")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(14, 0, 14, 14)
        body_lay.setSpacing(4)
        body.setVisible(False)
        out.addWidget(body)

        # Build the sorted list now
        rows = self._least_neutral_rows(metric_key)
        if not rows:
            empty = QLabel("No strategies to rank.")
            empty.setStyleSheet(
                f"color: {T.MUTED}; font-size: 11px; border: none; padding: 6px 0;"
            )
            body_lay.addWidget(empty)
        else:
            # Explainer for what the sign means
            if metric_key == "bwd":
                expl = ("Negative = bearish SPY exposure (gains if SPY drops). "
                        "Positive = bullish. Values are shares-of-SPY equivalent.")
            else:
                expl = ("Negative = short vol (loses if IV spikes). "
                        "Positive = long vol. Values are \\$ per 1 vol-point.")
            expl_lbl = QLabel(expl)
            expl_lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; border: none; "
                f"padding: 2px 0 6px 0;"
            )
            expl_lbl.setWordWrap(True)
            body_lay.addWidget(expl_lbl)

            # Column headers
            hdr = QHBoxLayout()
            hdr.setSpacing(10)
            for text, width, align in [
                ("STRATEGY",  0,   Qt.AlignmentFlag.AlignLeft),
                ("TICKER",    60,  Qt.AlignmentFlag.AlignCenter),
                ("EXPOSURE", 160,  Qt.AlignmentFlag.AlignRight),
                ("OPEN P&L", 100,  Qt.AlignmentFlag.AlignRight),
            ]:
                l = QLabel(text)
                l.setStyleSheet(
                    f"color: {T.MUTED}; font-size: 9px; font-weight: bold; "
                    f"letter-spacing: 0.6px; border: none;"
                )
                if width:
                    l.setFixedWidth(width)
                l.setAlignment(align)
                hdr.addWidget(l, 0 if width else 1)
            body_lay.addLayout(hdr)

            for r in rows:
                body_lay.addWidget(self._build_neutrality_row(r, metric_key))

        def _toggle():
            vis = not body.isVisible()
            body.setVisible(vis)
            header.setText(f"{'▾' if vis else '▸'}  {label}")
        header.clicked.connect(_toggle)

        return wrap

    def _least_neutral_rows(self, metric_key: str):
        """Return strategies sorted by |metric| descending, with ticker + P&L."""
        from models import symbol_beta, _is_future_option, _CONTRACT_MULT

        acct = self.portfolio.current_account() if self.portfolio else None
        if not acct:
            return []
        positions   = acct["positions"]
        metrics     = acct.get("metrics") or {}
        strat_raw   = self.portfolio.strategies_raw
        instances   = [StrategyInstance(d, positions) for d in strat_raw]
        leftover    = unassigned_positions(positions, strat_raw)
        unassigned  = group_unassigned(leftover)

        def _strategy_bwd(s):
            total = 0.0
            for l in s.legs:
                if not l.is_option or l.delta is None:
                    continue
                if _is_future_option(l.instrument_type):
                    mult = float(_CONTRACT_MULT.get(l.root or "", 1))
                else:
                    mult = 100.0
                beta = symbol_beta(metrics.get(l.root)) or 1.0
                total += l.delta * l.quantity * mult * l.sign * beta
            return total

        def _metric(s):
            if metric_key == "bwd":
                return _strategy_bwd(s)
            # Vega: dollar vega of the strategy
            return s.net_vega or 0.0

        rows = []
        for s in instances + unassigned:
            val = _metric(s)
            if val is None:
                continue
            rows.append({
                "name":  s.name,
                "root":  s.root or "—",
                "value": float(val),
                "pnl":   s.pnl,
            })
        rows.sort(key=lambda r: abs(r["value"]), reverse=True)
        return rows

    def _build_neutrality_row(self, r, metric_key):
        w = QFrame()
        w.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
            f"border-radius: 6px; }}"
        )
        hl = QHBoxLayout(w)
        hl.setContentsMargins(10, 8, 10, 8)
        hl.setSpacing(10)

        # Strategy name on the left (takes all remaining space)
        name_lbl = QLabel(r["name"] or "(unnamed)")
        name_lbl.setStyleSheet(
            f"color: {T.TEXT}; font-size: 12px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        hl.addWidget(name_lbl, 1)

        # Ticker badge — centered in its column
        root_lbl = QLabel(r["root"])
        root_lbl.setFixedWidth(60)
        root_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root_lbl.setStyleSheet(
            f"color: white; background: {T.ACCENT}; border: none; border-radius: 4px; "
            f"padding: 2px 0; font-size: 10px; font-weight: bold;"
        )
        hl.addWidget(root_lbl)

        # Exposure: bullish/bearish (BWD) or long/short vol (Vega) +  number
        val = r["value"]
        if metric_key == "bwd":
            direction = "Bullish" if val > 0 else ("Bearish" if val < 0 else "Neutral")
            val_color = T.GREEN if val > 0 else (T.RED if val < 0 else T.MUTED)
            exp_text  = f"{direction}  ·  {val:+,.0f}"
        else:
            direction = "Long vol" if val > 0 else ("Short vol" if val < 0 else "Neutral")
            val_color = T.GREEN if val > 0 else (T.RED if val < 0 else T.MUTED)
            exp_text  = f"{direction}  ·  {val:+,.0f}"

        exp_lbl = QLabel(exp_text)
        exp_lbl.setFixedWidth(160)
        exp_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        exp_lbl.setStyleSheet(
            f"color: {val_color}; font-size: 12px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        hl.addWidget(exp_lbl)

        pnl_lbl = QLabel(money(r["pnl"], signed=True))
        pnl_lbl.setFixedWidth(100)
        pnl_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        pnl_lbl.setStyleSheet(
            f"color: {pnl_color(r['pnl'])}; font-size: 12px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        hl.addWidget(pnl_lbl)
        return w

    def _on_prices(self, spy: float, vix: float,
                   net_liq: float, bwd: float, bwg: float, net_vega: float):
        """Called on GUI thread when the price worker completes."""
        if getattr(self, "_spy_chart", None):
            _draw_spy_chart(self._spy_chart, spy, net_liq, bwd, bwg)
            self._spy_loading.setVisible(False)
            self._spy_chart.setVisible(True)

        if getattr(self, "_vix_chart", None):
            _draw_vix_chart(self._vix_chart, vix, net_liq, net_vega)
            self._vix_loading.setVisible(False)
            self._vix_chart.setVisible(True)
