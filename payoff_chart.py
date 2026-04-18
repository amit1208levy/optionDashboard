"""Payoff-at-expiration chart, rendered via matplotlib embedded in Qt."""
import matplotlib
matplotlib.use("QtAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

import theme as T
from models import payoff_range


class PayoffChart(FigureCanvasQTAgg):
    def __init__(self, strategy, parent=None, height=2.0):
        fig = Figure(figsize=(5, height), tight_layout=True,
                     facecolor=T.CARD)
        super().__init__(fig)
        self.setParent(parent)
        self.setStyleSheet(f"background: {T.CARD};")
        self.plot(strategy)

    def plot(self, strategy):
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(T.CARD)

        xs, ys = payoff_range(strategy)
        if not xs:
            ax.text(0.5, 0.5, "No option legs to chart",
                    color=T.MUTED, ha="center", va="center",
                    transform=ax.transAxes, fontsize=10)
            self._style_empty(ax)
            self.draw()
            return

        # Profit / loss fills
        ax.fill_between(xs, ys, 0, where=[y >= 0 for y in ys],
                        interpolate=True, color=T.GREEN, alpha=0.18)
        ax.fill_between(xs, ys, 0, where=[y < 0 for y in ys],
                        interpolate=True, color=T.RED,   alpha=0.18)

        # Payoff curve
        ax.plot(xs, ys, color=T.ACCENT, linewidth=1.8)

        # Zero line
        ax.axhline(0, color=T.MUTED, linewidth=0.8, linestyle="--", alpha=0.7)

        # Strike markers
        for leg in strategy.legs:
            if leg.strike:
                ax.axvline(leg.strike, color=T.BORDER_H, linewidth=0.7,
                           linestyle=":", alpha=0.8)

        self._style(ax)
        self.draw()

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
