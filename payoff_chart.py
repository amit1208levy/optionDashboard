"""Payoff-at-expiration chart — clamped Y axis + crosshair hover tooltip."""
import matplotlib
matplotlib.use("QtAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

import theme as T
from models import payoff_range, strategy_extremes


class PayoffChart(FigureCanvasQTAgg):
    def __init__(self, strategy, parent=None, height=2.0):
        fig = Figure(figsize=(5, height), facecolor=T.CARD)
        fig.subplots_adjust(left=0.13, right=0.97, top=0.95, bottom=0.22)
        super().__init__(fig)
        self.setParent(parent)
        self.setStyleSheet(f"background: {T.CARD};")
        self.strategy = strategy
        self._xs = []
        self._ys = []
        self._cursor_line = None
        self._cursor_dot  = None
        self._annot       = None
        self.plot(strategy)
        self.mpl_connect("motion_notify_event", self._on_motion)

    def plot(self, strategy):
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(T.CARD)
        self._ax = ax

        xs, ys = payoff_range(strategy)
        self._xs, self._ys = xs, ys
        if not xs:
            ax.text(0.5, 0.5, "No option legs to chart",
                    color=T.MUTED, ha="center", va="center",
                    transform=ax.transAxes, fontsize=10)
            self._style_empty(ax)
            self.draw()
            return

        # Clamp Y axis to a sane range (unbounded-loss strategies would
        # otherwise plot down to ~-$20k and crush the useful part of the curve).
        max_profit, max_loss, _ = strategy_extremes(strategy)
        y_max_raw = max(ys)
        y_min_raw = min(ys)
        headroom  = max(abs(y_max_raw), 1.0) * 0.15

        if max_loss == float("-inf") or y_min_raw < -abs(y_max_raw) * 4:
            y_floor = -abs(y_max_raw) * 3 if y_max_raw > 0 else y_min_raw * 0.4
        else:
            y_floor = y_min_raw - headroom
        y_ceil = y_max_raw + headroom

        # Profit / loss fills (clamp y-values so the fill respects the ylim)
        clamped = [max(y_floor, min(y_ceil, y)) for y in ys]
        ax.fill_between(xs, clamped, 0, where=[y >= 0 for y in clamped],
                        interpolate=True, color=T.GREEN, alpha=0.18)
        ax.fill_between(xs, clamped, 0, where=[y < 0 for y in clamped],
                        interpolate=True, color=T.RED, alpha=0.18)

        # Payoff curve
        ax.plot(xs, ys, color=T.ACCENT, linewidth=1.8)

        # Zero line
        ax.axhline(0, color=T.MUTED, linewidth=0.8, linestyle="--", alpha=0.7)

        # Strike markers
        for leg in strategy.legs:
            if leg.strike:
                ax.axvline(leg.strike, color=T.BORDER_H, linewidth=0.7,
                           linestyle=":", alpha=0.8)

        ax.set_ylim(y_floor, y_ceil)
        ax.set_xlim(xs[0], xs[-1])

        self._style(ax)

        # Hover cursor + annotation (hidden until mouse enters)
        self._cursor_line = ax.axvline(xs[0], color=T.ACCENT, linewidth=0.8,
                                       alpha=0.5, visible=False)
        self._cursor_dot, = ax.plot([xs[0]], [ys[0]], "o", color=T.ACCENT,
                                    markersize=5, visible=False)
        self._annot = ax.annotate(
            "", xy=(0, 0), xytext=(10, 12), textcoords="offset points",
            ha="left", va="bottom",
            bbox=dict(boxstyle="round,pad=0.4", fc=T.BG_ALT, ec=T.BORDER, alpha=0.95),
            color=T.TEXT, fontsize=9, visible=False,
        )
        self.draw()

    def _on_motion(self, event):
        if event.inaxes != self._ax or not self._xs:
            self._hide_cursor()
            return
        x = event.xdata
        if x is None:
            self._hide_cursor()
            return
        # Nearest sample
        idx = min(range(len(self._xs)), key=lambda i: abs(self._xs[i] - x))
        sx, sy = self._xs[idx], self._ys[idx]
        self._cursor_line.set_xdata([sx, sx])
        self._cursor_line.set_visible(True)
        self._cursor_dot.set_data([sx], [sy])
        self._cursor_dot.set_visible(True)
        sign = "+" if sy >= 0 else "−"
        self._annot.xy = (sx, sy)
        self._annot.set_text(f"Price: ${sx:,.2f}\nP&L: {sign}${abs(sy):,.2f}")
        # Flip annotation to stay inside axes near edges.
        xl, xr = self._ax.get_xlim()
        yl, yu = self._ax.get_ylim()
        x_off = -10 if sx > (xl + xr) / 2 else 10
        y_off = -12 if sy > (yl + yu) / 2 else 12
        ha    = "right" if x_off < 0 else "left"
        va    = "top"   if y_off < 0 else "bottom"
        self._annot.set_position((x_off, y_off))
        self._annot.set_ha(ha)
        self._annot.set_va(va)
        self._annot.set_visible(True)
        self.draw_idle()

    def wheelEvent(self, event):
        """Forward scroll events to the parent so the page scrolls normally."""
        event.ignore()

    def _hide_cursor(self):
        if self._cursor_line is None:
            return
        self._cursor_line.set_visible(False)
        self._cursor_dot.set_visible(False)
        self._annot.set_visible(False)
        self.draw_idle()

    def _style(self, ax):
        ax.tick_params(colors=T.MUTED, labelsize=8, length=3)
        for spine in ax.spines.values():
            spine.set_color(T.BORDER)
            spine.set_linewidth(0.8)
        ax.set_xlabel("Underlying Price at Expiration",
                      color=T.MUTED, fontsize=9)
        ax.set_ylabel("P&L ($)", color=T.MUTED, fontsize=9)
        ax.grid(color=T.BORDER, alpha=0.25, linewidth=0.6)
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:,.0f}")
        )
        ax.xaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:,.0f}")
        )

    def _style_empty(self, ax):
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_xticks([]); ax.set_yticks([])
