"""Cumulative P&L chart for a strategy's closed-leg history."""
from datetime import datetime

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
import matplotlib.dates as mdates

import theme as T


def _parse_date(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(iso[:10], "%Y-%m-%d").date()
        except Exception:
            return None


class HistoryChart(FigureCanvasQTAgg):
    def __init__(self, entries, parent=None, height=3.0):
        fig = Figure(figsize=(6, height), facecolor=T.CARD)
        fig.subplots_adjust(left=0.13, right=0.97, top=0.95, bottom=0.15)
        super().__init__(fig)
        self.setParent(parent)
        self.setStyleSheet(f"background: {T.CARD};")
        self.entries = entries
        self._xs = []
        self._ys = []
        self._cursor = None
        self._annot  = None
        self.plot()
        self.mpl_connect("motion_notify_event", self._on_motion)

    def plot(self):
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(T.CARD)
        self._ax = ax

        pts = []
        for e in self.entries:
            d = _parse_date(e.get("closed_at"))
            if d is None:
                continue
            pts.append((d, float(e.get("pnl") or 0.0)))
        pts.sort(key=lambda p: p[0])

        if not pts:
            ax.text(0.5, 0.5, "No closed legs yet",
                    color=T.MUTED, ha="center", va="center",
                    transform=ax.transAxes, fontsize=10)
            for s in ax.spines.values():
                s.set_visible(False)
            ax.set_xticks([]); ax.set_yticks([])
            self.draw()
            return

        running = 0.0
        xs, ys = [], []
        for d, p in pts:
            running += p
            xs.append(d); ys.append(running)
        self._xs, self._ys = xs, ys

        ax.plot(xs, ys, color=T.ACCENT, linewidth=1.8)
        ax.fill_between(xs, ys, 0, where=[y >= 0 for y in ys],
                        interpolate=True, color=T.GREEN, alpha=0.18)
        ax.fill_between(xs, ys, 0, where=[y < 0 for y in ys],
                        interpolate=True, color=T.RED, alpha=0.18)
        ax.axhline(0, color=T.MUTED, linewidth=0.8, linestyle="--", alpha=0.7)

        # Per-trade markers
        colors = [T.GREEN if p >= 0 else T.RED for _, p in pts]
        ax.scatter(xs, ys, c=colors, s=18, zorder=3, edgecolors="none")

        ax.tick_params(colors=T.MUTED, labelsize=8, length=3)
        for s in ax.spines.values():
            s.set_color(T.BORDER); s.set_linewidth(0.8)
        ax.set_ylabel("Cumulative P&L ($)", color=T.MUTED, fontsize=9)
        ax.grid(color=T.BORDER, alpha=0.25, linewidth=0.6)
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:,.0f}")
        )
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))

        self._cursor = ax.axvline(xs[0], color=T.ACCENT, linewidth=0.8,
                                  alpha=0.5, visible=False)
        self._annot = ax.annotate(
            "", xy=(0, 0), xytext=(10, 12), textcoords="offset points",
            ha="left", va="bottom",
            bbox=dict(boxstyle="round,pad=0.4", fc=T.BG_ALT, ec=T.BORDER, alpha=0.95),
            color=T.TEXT, fontsize=9, visible=False,
        )
        self.draw()

    def wheelEvent(self, event):
        """Forward scroll events to the parent so the page scrolls normally."""
        event.ignore()

    def _on_motion(self, event):
        if event.inaxes != self._ax or not self._xs or event.xdata is None:
            if self._cursor is not None:
                self._cursor.set_visible(False)
                self._annot.set_visible(False)
                self.draw_idle()
            return
        # xdata is a matplotlib date number; compare to each point's num
        x_nums = [mdates.date2num(d) for d in self._xs]
        idx = min(range(len(x_nums)), key=lambda i: abs(x_nums[i] - event.xdata))
        sx, sy = self._xs[idx], self._ys[idx]
        self._cursor.set_xdata([sx, sx])
        self._cursor.set_visible(True)
        sign = "+" if sy >= 0 else "−"
        self._annot.xy = (sx, sy)
        self._annot.set_text(
            f"{sx.strftime('%b %d, %Y')}\nCumulative: {sign}${abs(sy):,.2f}"
        )
        # Flip annotation to stay inside axes near edges.
        xl, xr = self._ax.get_xlim()
        yl, yu = self._ax.get_ylim()
        x_num = mdates.date2num(sx)
        x_off = -10 if x_num > (xl + xr) / 2 else 10
        y_off = -12 if sy > (yl + yu) / 2 else 12
        self._annot.set_position((x_off, y_off))
        self._annot.set_ha("right" if x_off < 0 else "left")
        self._annot.set_va("top"   if y_off < 0 else "bottom")
        self._annot.set_visible(True)
        self.draw_idle()
