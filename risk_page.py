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


# Color palette for strategy segments (cycles if more strategies than entries)
_PALETTE = [
    T.PURPLE, T.TEAL, T.BLUE, T.GREEN, T.YELLOW,
    "#f97316", "#ec4899", "#06b6d4", "#84cc16", "#a78bfa",
]

# Matplotlib dark style matching the app
_MPL_STYLE = {
    "figure.facecolor":  T.BG,
    "axes.facecolor":    T.CARD,
    "axes.edgecolor":    T.BORDER,
    "text.color":        T.TEXT_DIM,
    "axes.labelcolor":   T.LABEL,
    "xtick.color":       T.MUTED,
    "ytick.color":       T.MUTED,
    "grid.color":        T.BORDER,
    "grid.alpha":        0.7,
    "axes.grid":         True,
    "axes.spines.top":   False,
    "axes.spines.right": False,
    "lines.linewidth":   2.2,
    "font.size":         10,
    "axes.titlesize":    11,
    "axes.labelsize":    10,
    "xtick.labelsize":   9,
    "ytick.labelsize":   9,
}


# ── background workers ────────────────────────────────────────────────────────

class _PriceWorker(QThread):
    """Fetch SPY and VIX current prices in the background."""
    done = pyqtSignal(float, float)   # spy_price, vix_price

    def __init__(self, token, positions):
        super().__init__()
        self._token     = token
        self._positions = list(positions)

    def run(self):
        spy = vix = None

        # Fast path: SPY underlying_price is already in portfolio positions
        for p in self._positions:
            if p.root == "SPY" and p.underlying_price and p.underlying_price > 0:
                spy = p.underlying_price
                break

        try:
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
    fig = Figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    canvas = FigureCanvasQTAgg(fig)
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


def _draw_spy_chart(canvas: FigureCanvasQTAgg,
                    spy: float, net_liq: float,
                    bwd: float, bwg: float):
    """
    SPY scenario chart.
    X: SPY price  (±25 % of current)
    Y: estimated account value / SPY price

    Shows how many "SPY shares" the account is worth as the market moves.
    The curve bends upward for net-long portfolios and downward for net-short.
    """
    with plt.rc_context(_MPL_STYLE):
        fig = canvas.figure
        fig.clear()
        ax  = fig.add_subplot(111)

        x   = np.linspace(spy * 0.75, spy * 1.25, 400)
        ds  = x - spy                           # ΔSPY
        pnl = bwd * ds + 0.5 * bwg * ds ** 2   # Greek-estimated P&L
        y   = (net_liq + pnl) / x              # account value normalised by SPY

        # Current value (horizontal baseline)
        y0  = net_liq / spy

        ax.axhline(y0, color=T.BORDER_H, linewidth=1, linestyle="--", alpha=0.7,
                   label="Current")
        ax.axvline(spy, color=T.MUTED, linewidth=1, linestyle=":", alpha=0.6)

        ax.plot(x, y, color=T.PURPLE, linewidth=2.5)
        ax.fill_between(x, y0, y, where=(y > y0),
                        alpha=0.15, color=T.GREEN, interpolate=True)
        ax.fill_between(x, y0, y, where=(y <= y0),
                        alpha=0.15, color=T.RED, interpolate=True)

        # Mark current point
        ax.scatter([spy], [y0], color=T.ACCENT, zorder=5, s=50)

        ax.set_xlabel("SPY Price  ($)")
        ax.set_ylabel("Account Value / SPY  (shares equivalent)")
        ax.set_title("SPY Scenario — Account Value Normalised to SPY")

        # Format Y tick labels with commas
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}")
        )
        ax.xaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:,.0f}")
        )
        fig.tight_layout(pad=1.2)
        canvas.draw()


def _draw_vix_chart(canvas: FigureCanvasQTAgg,
                    vix: float, net_liq: float, net_vega: float):
    """
    VIX scenario chart.
    X: VIX level  (0.4× – 2.5× current, clamped to [5, 80])
    Y: estimated account value / VIX level

    Vega is approximated as linear: P&L ≈ net_vega × ΔVIX.
    (VIX ≈ SPY 30-day IV in %; vega = $ per 1 vol-point.)
    """
    with plt.rc_context(_MPL_STYLE):
        fig = canvas.figure
        fig.clear()
        ax  = fig.add_subplot(111)

        x_min = max(5.0,  vix * 0.4)
        x_max = min(80.0, vix * 2.5)
        x   = np.linspace(x_min, x_max, 400)
        dv  = x - vix                   # ΔVIX
        pnl = net_vega * dv             # vega × ΔVIX
        y   = (net_liq + pnl) / x      # account value normalised by VIX

        y0  = net_liq / vix             # current baseline

        ax.axhline(y0, color=T.BORDER_H, linewidth=1, linestyle="--", alpha=0.7,
                   label="Current")
        ax.axvline(vix, color=T.MUTED, linewidth=1, linestyle=":", alpha=0.6)

        ax.plot(x, y, color=T.TEAL, linewidth=2.5)
        ax.fill_between(x, y0, y, where=(y > y0),
                        alpha=0.15, color=T.GREEN, interpolate=True)
        ax.fill_between(x, y0, y, where=(y <= y0),
                        alpha=0.15, color=T.RED, interpolate=True)

        ax.scatter([vix], [y0], color=T.ACCENT, zorder=5, s=50)

        ax.set_xlabel("VIX Level")
        ax.set_ylabel("Account Value / VIX  ($ per VIX unit)")
        ax.set_title("VIX Scenario — Account Value Normalised to VIX")

        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}")
        )
        fig.tight_layout(pad=1.2)
        canvas.draw()


# ── main page ─────────────────────────────────────────────────────────────────

class RiskPage(QWidget):
    back_requested = pyqtSignal()

    def __init__(self, portfolio, parent=None):
        super().__init__(parent)
        self.portfolio = portfolio
        self._worker   = None
        self._spy_canvas = None
        self._vix_canvas = None
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
            self._worker = _PriceWorker(self.portfolio.token, positions)
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

    def _on_prices(self, spy: float, vix: float,
                   net_liq: float, bwd: float, bwg: float, net_vega: float):
        if self._spy_canvas:
            _draw_spy_chart(self._spy_canvas, spy, net_liq, bwd, bwg)
        if self._vix_canvas:
            _draw_vix_chart(self._vix_canvas, vix, net_liq, net_vega)

    # ── allocation section ────────────────────────────────────────────────────

    def _build_allocation(self, instances, unassigned, overrides):
        self.body.addWidget(self._section_header("PORTFOLIO ALLOCATION BY STRATEGY"))
        card, lay = self._card_frame()

        rows, total = strategy_allocation(instances, unassigned, overrides)

        if not rows or total <= 0:
            lay.addWidget(QLabel("No capital data available."))
            self.body.addWidget(card)
            return

        # ── stacked overview bar ─────────────────────────────────────────────
        bar_outer = QFrame()
        bar_outer.setFixedHeight(28)
        bar_outer.setStyleSheet(
            f"background: {T.BG_ALT}; border-radius: 8px; border: none;"
        )
        bar_lay = QHBoxLayout(bar_outer)
        bar_lay.setContentsMargins(0, 0, 0, 0)
        bar_lay.setSpacing(1)

        for i, r in enumerate(rows):
            seg = QFrame()
            color = _PALETTE[i % len(_PALETTE)]
            # Round left corners on first segment, right on last
            radius = ""
            if i == 0:
                radius = "border-top-left-radius: 7px; border-bottom-left-radius: 7px;"
            if i == len(rows) - 1:
                radius += "border-top-right-radius: 7px; border-bottom-right-radius: 7px;"
            seg.setStyleSheet(
                f"QFrame {{ background: {color}; border: none; {radius} }}"
            )
            weight = max(1, int(r["pct"] * 10))
            bar_lay.addWidget(seg, weight)

        lay.addWidget(bar_outer)

        # ── column headers ───────────────────────────────────────────────────
        lay.addSpacing(6)
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
        """Create a card with a section header and an embedded matplotlib canvas."""
        self.body.addWidget(self._section_header(title))
        card, lay = self._card_frame()

        # Loading placeholder shown while worker fetches prices
        loading = QLabel("Fetching market data…")
        loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none; padding: 40px 0;"
        )
        lay.addWidget(loading)

        # Canvas (hidden until prices arrive)
        canvas = _make_canvas(width_px=800, height_px=300)
        canvas.setVisible(False)
        lay.addWidget(canvas)

        self.body.addWidget(card)

        # Store canvas + loading label so we can swap them in _on_prices
        if key == "spy":
            self._spy_canvas   = canvas
            self._spy_loading  = loading
        else:
            self._vix_canvas   = canvas
            self._vix_loading  = loading

    def _on_prices(self, spy: float, vix: float,
                   net_liq: float, bwd: float, bwg: float, net_vega: float):
        """Called on GUI thread when the price worker completes."""
        if self._spy_canvas:
            _draw_spy_chart(self._spy_canvas, spy, net_liq, bwd, bwg)
            self._spy_loading.setVisible(False)
            self._spy_canvas.setVisible(True)

        if self._vix_canvas:
            _draw_vix_chart(self._vix_canvas, vix, net_liq, net_vega)
            self._vix_loading.setVisible(False)
            self._vix_canvas.setVisible(True)
