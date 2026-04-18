"""Watchlist page — track tickers by IV rank and size potential trades."""
import api
from models import symbol_ivr, symbol_ivp, symbol_beta, symbol_hv30
import theme as T

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QScrollArea, QDialog, QSlider,
    QComboBox, QDoubleSpinBox, QSizePolicy,
)


# ── Fetch worker ──────────────────────────────────────────────────────────────

class _FetchWorker(QThread):
    done = pyqtSignal(dict, dict)  # metrics, quotes

    def __init__(self, token, tickers, parent=None):
        super().__init__(parent)
        self.token   = token
        self.tickers = tickers

    def run(self):
        if not self.tickers:
            self.done.emit({}, {})
            return
        metrics = api.get_market_metrics(self.token, self.tickers)
        quotes  = api.get_market_data(self.token, equities=self.tickers)
        self.done.emit(metrics, quotes)


# ── Position sizer dialog ─────────────────────────────────────────────────────

class PositionSizerDialog(QDialog):
    def __init__(self, ticker, price, nlv, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Position Sizer — {ticker}")
        self.setMinimumWidth(400)
        self.setStyleSheet(f"background: {T.BG}; color: {T.TEXT};")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 22)
        lay.setSpacing(10)

        title = QLabel(f"Size a Trade — {ticker}")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 15px; font-weight: bold; border: none;"
        )
        lay.addWidget(title)

        sub = QLabel(
            f"Current price: ${price:,.2f}   ·   Account NLV: ${nlv:,.0f}"
        )
        sub.setStyleSheet(f"color: {T.MUTED}; font-size: 11px; border: none;")
        lay.addWidget(sub)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {T.BORDER}; max-height: 1px; border: none;")
        lay.addWidget(sep)

        # Strategy type
        self._lbl(lay, "Strategy type")
        self.strategy_combo = QComboBox()
        self.strategy_combo.addItems([
            "Short Strangle / Straddle  (undefined risk)",
            "Iron Condor / Spread  (defined risk)",
            "Naked Put or Call  (undefined risk)",
            "Long Option  (debit)",
            "Stock / ETF  (equity)",
        ])
        self.strategy_combo.currentIndexChanged.connect(self._update_hint)
        lay.addWidget(self.strategy_combo)

        # Max allocation slider
        self._lbl(lay, "Max capital allocation  (% of NLV)")
        pct_row = QHBoxLayout()
        self.pct_slider = QSlider(Qt.Orientation.Horizontal)
        self.pct_slider.setRange(1, 20)
        self.pct_slider.setValue(5)
        self.pct_slider.valueChanged.connect(self._recalc)
        self.pct_val = QLabel("5%")
        self.pct_val.setFixedWidth(36)
        self.pct_val.setStyleSheet(
            f"color: {T.ACCENT}; font-weight: bold; border: none;"
        )
        pct_row.addWidget(self.pct_slider)
        pct_row.addWidget(self.pct_val)
        lay.addLayout(pct_row)

        # Capital per contract
        self._lbl(lay, "Capital required per contract  ($BP used)")
        self.cap_spin = QDoubleSpinBox()
        self.cap_spin.setRange(1, 999_999)
        self.cap_spin.setDecimals(0)
        self.cap_spin.setSingleStep(50)
        default = max(round(price * 100 * 0.10 / 50) * 50, 200)
        self.cap_spin.setValue(default)
        self.cap_spin.setStyleSheet(
            f"QDoubleSpinBox {{ background: {T.CARD}; color: {T.TEXT}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 4px 8px; }}"
        )
        self.cap_spin.valueChanged.connect(self._recalc)
        lay.addWidget(self.cap_spin)

        self.hint_lbl = QLabel("")
        self.hint_lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; border: none;"
        )
        lay.addWidget(self.hint_lbl)

        # Results card
        res = QFrame()
        res.setStyleSheet(
            f"background: {T.CARD}; border-radius: 8px; border: 1px solid {T.BORDER};"
        )
        res_lay = QVBoxLayout(res)
        res_lay.setContentsMargins(16, 14, 16, 14)
        res_lay.setSpacing(4)

        self.contracts_lbl = QLabel()
        self.contracts_lbl.setStyleSheet(
            f"color: {T.TEXT}; font-size: 22px; font-weight: bold; border: none;"
        )
        res_lay.addWidget(self.contracts_lbl)

        self.capital_lbl = QLabel()
        self.capital_lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none;"
        )
        res_lay.addWidget(self.capital_lbl)

        self.bp_lbl = QLabel()
        self.bp_lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none;"
        )
        res_lay.addWidget(self.bp_lbl)
        lay.addWidget(res)

        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(34)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        close_btn.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: white; border: none; "
            f"border-radius: 6px; padding: 0 20px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
        )
        lay.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

        self.nlv   = nlv
        self.price = price
        self._update_hint()
        self._recalc()

    def _lbl(self, parent_lay, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"border: none; margin-top: 4px;"
        )
        parent_lay.addWidget(lbl)

    def _update_hint(self):
        idx = self.strategy_combo.currentIndex()
        hints = [
            "Tip: BP used ≈ short-strike × 100 × ~20% for strangles",
            "Tip: BP used = max-loss = width × 100",
            "Tip: BP used ≈ strike × 100 × ~20% for naked puts",
            "Tip: BP used = premium paid × 100",
            "Tip: BP used = share price × shares",
        ]
        self.hint_lbl.setText(hints[idx])
        self._recalc()

    def _recalc(self):
        pct = self.pct_slider.value()
        self.pct_val.setText(f"{pct}%")
        max_cap = self.nlv * pct / 100.0
        cap_per = max(float(self.cap_spin.value()), 1.0)
        contracts = int(max_cap / cap_per)
        total     = contracts * cap_per
        pct_used  = total / self.nlv * 100 if self.nlv else 0
        remaining = self.nlv - total

        label = "contract" if contracts == 1 else "contracts"
        self.contracts_lbl.setText(f"{contracts} {label}")
        self.capital_lbl.setText(
            f"Total BP: ${total:,.0f}   ·   Per contract: ${cap_per:,.0f}"
        )
        self.bp_lbl.setText(
            f"{pct_used:.1f}% of NLV used   ·   Remaining BP: ${remaining:,.0f}"
        )


# ── Ticker row ────────────────────────────────────────────────────────────────

class _TickerRow(QFrame):
    remove_clicked = pyqtSignal(str)
    size_clicked   = pyqtSignal(str, float, float)  # ticker, price, nlv

    def __init__(self, ticker, nlv, parent=None):
        super().__init__(parent)
        self.ticker = ticker
        self.nlv    = nlv
        self._price = 0.0

        self.setFixedHeight(54)
        self.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border-radius: 8px; "
            f"border: 1px solid {T.BORDER}; }}"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 0, 12, 0)
        lay.setSpacing(0)

        # Ticker symbol
        sym = QLabel(ticker)
        sym.setFixedWidth(80)
        sym.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 14px; font-weight: bold; border: none;"
        )
        lay.addWidget(sym)

        # Metric columns
        self.price_lbl = self._col(90)
        self.ivr_lbl   = self._col(70)
        self.ivp_lbl   = self._col(70)
        self.hv_lbl    = self._col(70)
        self.beta_lbl  = self._col(60)
        lay.addWidget(self.price_lbl)
        lay.addWidget(self.ivr_lbl)
        lay.addWidget(self.ivp_lbl)
        lay.addWidget(self.hv_lbl)
        lay.addWidget(self.beta_lbl)

        lay.addStretch()

        size_btn = QPushButton("Size trade")
        size_btn.setFixedHeight(28)
        size_btn.setFixedWidth(80)
        size_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        size_btn.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: white; border: none; "
            f"border-radius: 5px; font-size: 11px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
        )
        size_btn.clicked.connect(
            lambda: self.size_clicked.emit(self.ticker, self._price, self.nlv)
        )
        lay.addWidget(size_btn)

        rm_btn = QPushButton("×")
        rm_btn.setFixedSize(28, 28)
        rm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rm_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; border: none; "
            f"font-size: 16px; font-weight: bold; }}"
            f"QPushButton:hover {{ color: {T.RED}; }}"
        )
        rm_btn.clicked.connect(lambda: self.remove_clicked.emit(self.ticker))
        lay.addWidget(rm_btn)

    def _col(self, width):
        lbl = QLabel("—")
        lbl.setFixedWidth(width)
        lbl.setStyleSheet(f"color: {T.TEXT}; font-size: 12px; border: none;")
        return lbl

    def update_data(self, metrics, quote):
        # Price
        price = None
        for key in ("mark", "last"):
            v = quote.get(key) if quote else None
            try:
                price = float(v)
                break
            except (TypeError, ValueError):
                pass
        if price is None and quote:
            bid = quote.get("bid")
            ask = quote.get("ask")
            try:
                price = (float(bid) + float(ask)) / 2
            except (TypeError, ValueError):
                pass
        if price and price > 0:
            self._price = price
            self.price_lbl.setText(f"${price:,.2f}")
        else:
            self.price_lbl.setText("—")

        # IVR
        ivr = symbol_ivr(metrics)
        if ivr is not None:
            color = T.GREEN if ivr >= 50 else (T.YELLOW if ivr >= 25 else T.RED)
            self.ivr_lbl.setText(f"<span style='color:{color}'>{ivr:.0f}</span>")
        else:
            self.ivr_lbl.setText("—")

        # IVP
        ivp = symbol_ivp(metrics)
        self.ivp_lbl.setText(f"{ivp:.0f}" if ivp is not None else "—")

        # HV30
        hv = symbol_hv30(metrics)
        self.hv_lbl.setText(f"{hv:.0f}%" if hv is not None else "—")

        # Beta
        beta = symbol_beta(metrics)
        self.beta_lbl.setText(f"{beta:.2f}" if beta is not None else "—")


# ── Watchlist page ────────────────────────────────────────────────────────────

class WatchlistPage(QWidget):
    back_requested = pyqtSignal()

    def __init__(self, token, nlv, parent=None):
        super().__init__(parent)
        self.token   = token
        self.nlv     = nlv
        self._rows   = {}    # ticker -> _TickerRow
        self._worker = None

        settings = api.load_settings()
        self._tickers = list(settings.get("watchlist_tickers", []))

        self.setStyleSheet(T.BASE_STYLE)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        # Column header bar
        root.addWidget(self._build_col_header())

        # Scrollable list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body_w = QWidget()
        self.body = QVBoxLayout(body_w)
        self.body.setContentsMargins(28, 12, 28, 28)
        self.body.setSpacing(6)
        self.body.addStretch()
        scroll.setWidget(body_w)
        root.addWidget(scroll)

        self._rebuild_rows()
        if self._tickers:
            self._fetch()

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

        title = QLabel("Watchlist")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 16px; font-weight: bold; border: none;"
        )
        hl.addWidget(title)
        hl.addStretch()

        self.add_input = QLineEdit()
        self.add_input.setPlaceholderText("Add ticker  (e.g. AAPL)")
        self.add_input.setFixedWidth(200)
        self.add_input.setFixedHeight(32)
        self.add_input.returnPressed.connect(self._add_ticker)
        hl.addWidget(self.add_input)

        add_btn = QPushButton("+ Add")
        add_btn.setFixedHeight(32)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self._add_ticker)
        hl.addWidget(add_btn)

        self.refresh_btn = QPushButton("↻  Refresh")
        self.refresh_btn.setFixedHeight(32)
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._fetch)
        hl.addWidget(self.refresh_btn)

        return hdr

    def _build_col_header(self):
        bar = QFrame()
        bar.setFixedHeight(32)
        bar.setStyleSheet(
            f"background: {T.BG_ALT}; border-bottom: 1px solid {T.BORDER};"
        )
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(46, 0, 120, 0)
        hl.setSpacing(0)

        cols = [
            ("Ticker", 80), ("Price", 90), ("IVR", 70),
            ("IVP", 70), ("HV30", 70), ("Beta", 60),
        ]
        for name, w in cols:
            lbl = QLabel(name)
            lbl.setFixedWidth(w)
            lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; font-weight: bold; "
                f"letter-spacing: 0.5px; border: none;"
            )
            hl.addWidget(lbl)

        hl.addStretch()
        return bar

    # ── Data ─────────────────────────────────────────────────────────────────

    def _add_ticker(self):
        ticker = self.add_input.text().strip().upper()
        if not ticker or ticker in self._tickers:
            self.add_input.clear()
            return
        self._tickers.append(ticker)
        self._save()
        self.add_input.clear()
        self._rebuild_rows()
        self._fetch()

    def _remove_ticker(self, ticker):
        if ticker in self._tickers:
            self._tickers.remove(ticker)
        self._save()
        self._rebuild_rows()

    def _save(self):
        settings = api.load_settings()
        settings["watchlist_tickers"] = self._tickers
        api.save_settings(settings)

    def _fetch(self):
        if not self._tickers:
            return
        if self._worker and self._worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Loading…")
        self._worker = _FetchWorker(self.token, self._tickers, self)
        self._worker.done.connect(self._on_fetch_done)
        self._worker.start()

    def _on_fetch_done(self, metrics, quotes):
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("↻  Refresh")
        for ticker, row in self._rows.items():
            row.update_data(metrics.get(ticker, {}), quotes.get(ticker, {}))

    # ── UI rebuild ────────────────────────────────────────────────────────────

    def _rebuild_rows(self):
        # Remove old rows
        for row in self._rows.values():
            self.body.removeWidget(row)
            row.deleteLater()
        self._rows = {}

        if not self._tickers:
            # Show empty state (insert before the stretch)
            empty = QLabel("No tickers yet — add one above.")
            empty.setStyleSheet(
                f"color: {T.MUTED}; font-size: 13px; border: none;"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.body.insertWidget(0, empty)
            self._empty_lbl = empty
        else:
            if hasattr(self, "_empty_lbl"):
                self.body.removeWidget(self._empty_lbl)
                self._empty_lbl.deleteLater()
                del self._empty_lbl

            for i, ticker in enumerate(self._tickers):
                row = _TickerRow(ticker, self.nlv, self)
                row.remove_clicked.connect(self._remove_ticker)
                row.size_clicked.connect(self._open_sizer)
                self._rows[ticker] = row
                self.body.insertWidget(i, row)

    def _open_sizer(self, ticker, price, nlv):
        dlg = PositionSizerDialog(ticker, price, nlv, self)
        dlg.exec()
