"""Watchlist page — track tickers by IV rank and size potential trades."""
import re
import api
from models import symbol_ivr, symbol_ivp, symbol_beta, symbol_hv30
import theme as T

_FUT_MONTH = "FGHJKMNQUVXZ"

def _has_price(quote):
    """Return True if the quote dict contains a usable price."""
    if not quote:
        return False
    for k in ("mark", "last", "bid", "ask"):
        try:
            if float(quote.get(k) or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return False

def _normalize_ticker(text):
    """
    Collapse any futures contract symbol down to its root.
    /MESU6 -> MES,  /6AH6 -> 6A,  AAPL -> AAPL
    Also handles bare contract month: MESU6 -> MES
    """
    t = text.strip().upper().lstrip("/")
    # Strip trailing month-code + 1-2 digit year (e.g. U6, H26)
    m = re.match(rf"^([A-Z0-9]{{1,5}})[{_FUT_MONTH}]\d{{1,2}}$", t)
    if m:
        return m.group(1)
    return t

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
        # Try equity quotes first
        quotes = api.get_market_data(self.token, equities=self.tickers)
        # For any ticker that got no price, retry as a futures root (/MES, /6A …)
        missing = [t for t in self.tickers
                   if not _has_price(quotes.get(t))]
        if missing:
            fut_syms = ["/" + t for t in missing]
            fut_quotes = api.get_market_data(self.token, futures=fut_syms)
            for sym, q in fut_quotes.items():
                root = sym.lstrip("/")
                if root in missing:
                    quotes[root] = q
        self.done.emit(metrics, quotes)


# ── Position sizer dialog ─────────────────────────────────────────────────────

class PositionSizerDialog(QDialog):
    def __init__(self, ticker, price, nlv, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Position Sizer — {ticker}")
        self.setMinimumWidth(460)
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
        self.strategy_combo.currentIndexChanged.connect(self._recalc)
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

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"background: {T.BORDER}; max-height: 1px; border: none;")
        lay.addWidget(sep2)

        # ── Premium per contract ──────────────────────────────────────────────
        self._lbl(lay, "Premium per contract  (option price as shown on chain)")
        cp_row = QHBoxLayout()
        self.cp_spin = QDoubleSpinBox()
        self.cp_spin.setRange(0.01, 99_999)
        self.cp_spin.setDecimals(2)
        self.cp_spin.setSingleStep(0.10)
        # Default: ~2 % of underlying (rough ATM option value)
        default_cp = max(round(price * 0.02 * 4) / 4, 0.05)
        self.cp_spin.setValue(default_cp)
        self.cp_spin.setStyleSheet(
            f"QDoubleSpinBox {{ background: {T.CARD}; color: {T.TEXT}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 4px 8px; }}"
        )
        self.cp_spin.valueChanged.connect(self._recalc)
        cp_row.addWidget(self.cp_spin)

        mult_lbl = QLabel("×")
        mult_lbl.setStyleSheet(f"color: {T.MUTED}; border: none;")
        cp_row.addWidget(mult_lbl)

        self.mult_spin = QDoubleSpinBox()
        self.mult_spin.setRange(1, 100_000)
        self.mult_spin.setDecimals(0)
        self.mult_spin.setSingleStep(100)
        self.mult_spin.setValue(100)
        self.mult_spin.setFixedWidth(90)
        self.mult_spin.setToolTip("Contract multiplier (100 for equity options; varies for futures)")
        self.mult_spin.setStyleSheet(
            f"QDoubleSpinBox {{ background: {T.CARD}; color: {T.TEXT}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 4px 8px; }}"
        )
        self.mult_spin.valueChanged.connect(self._recalc)
        cp_row.addWidget(self.mult_spin)

        self.cp_total_lbl = QLabel()
        self.cp_total_lbl.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 12px; font-weight: bold; border: none; margin-left: 8px;"
        )
        cp_row.addWidget(self.cp_total_lbl)
        cp_row.addStretch()
        lay.addLayout(cp_row)

        # ── Delta ─────────────────────────────────────────────────────────────
        self._lbl(lay, "Delta  (of the option leg, e.g. 20 = 20Δ)")
        delta_row = QHBoxLayout()
        self.delta_slider = QSlider(Qt.Orientation.Horizontal)
        self.delta_slider.setRange(1, 50)
        self.delta_slider.setValue(20)
        self.delta_slider.valueChanged.connect(self._recalc)
        self.delta_val = QLabel("20Δ")
        self.delta_val.setFixedWidth(42)
        self.delta_val.setStyleSheet(
            f"color: {T.ACCENT}; font-weight: bold; border: none;"
        )
        delta_row.addWidget(self.delta_slider)
        delta_row.addWidget(self.delta_val)
        lay.addLayout(delta_row)

        # ── DTE ───────────────────────────────────────────────────────────────
        self._lbl(lay, "Estimated DTE  (days to expiration)")
        dte_row = QHBoxLayout()
        self.dte_slider = QSlider(Qt.Orientation.Horizontal)
        self.dte_slider.setRange(1, 180)
        self.dte_slider.setValue(45)
        self.dte_slider.valueChanged.connect(self._recalc)
        self.dte_val = QLabel("45d")
        self.dte_val.setFixedWidth(42)
        self.dte_val.setStyleSheet(
            f"color: {T.ACCENT}; font-weight: bold; border: none;"
        )
        dte_row.addWidget(self.dte_slider)
        dte_row.addWidget(self.dte_val)
        lay.addLayout(dte_row)

        # ── Results card ──────────────────────────────────────────────────────
        res = QFrame()
        res.setStyleSheet(
            f"background: {T.CARD}; border-radius: 8px; border: 1px solid {T.BORDER};"
        )
        res_lay = QVBoxLayout(res)
        res_lay.setContentsMargins(16, 14, 16, 16)
        res_lay.setSpacing(6)

        # Top: contracts + total premium side by side
        top_row = QHBoxLayout()
        top_row.setSpacing(24)

        self.contracts_lbl = QLabel()
        self.contracts_lbl.setStyleSheet(
            f"color: {T.TEXT}; font-size: 22px; font-weight: bold; border: none;"
        )
        top_row.addWidget(self.contracts_lbl)

        self.total_prem_lbl = QLabel()
        self.total_prem_lbl.setStyleSheet(
            f"color: {T.GREEN}; font-size: 22px; font-weight: bold; border: none;"
        )
        top_row.addWidget(self.total_prem_lbl)
        top_row.addStretch()
        res_lay.addLayout(top_row)

        # Detail lines
        self.capital_lbl = QLabel()
        self.capital_lbl.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        res_lay.addWidget(self.capital_lbl)

        self.bp_lbl = QLabel()
        self.bp_lbl.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        res_lay.addWidget(self.bp_lbl)

        self.theta_lbl = QLabel()
        self.theta_lbl.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        res_lay.addWidget(self.theta_lbl)

        self.pop_lbl = QLabel()
        self.pop_lbl.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        res_lay.addWidget(self.pop_lbl)

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
        self._recalc()

    def _lbl(self, parent_lay, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"border: none; margin-top: 4px;"
        )
        parent_lay.addWidget(lbl)

    def _cap_per_contract(self, cp_dollars, delta_frac, dte, strategy_idx):
        """
        Estimate capital (BP) required per contract.

        idx 0  Short Strangle/Straddle  → naked-style both sides:
                 20 % of underlying × mult  minus  premium collected
        idx 1  Iron Condor / Spread      → max-loss = width × mult
                 approximate width from delta: OTM width ≈ (0.5-delta) × price × 0.35
        idx 2  Naked Put or Call         → same as half of strangle
        idx 3  Long Option  (debit)      → just the premium paid
        idx 4  Stock / ETF               → full notional  (price × 1 share)
        """
        mult = float(self.mult_spin.value()) or 100
        S = self.price

        if strategy_idx == 4:          # equity / ETF
            return S * mult

        if strategy_idx == 3:          # long option — cost is the premium
            return cp_dollars

        # Naked-margin approximation (Reg-T style):
        #   20 % of underlying notional  minus  OTM discount  plus  premium
        # OTM discount ≈ (0.5 − delta) × price × mult × 0.06
        notional   = S * mult
        otm_disc   = (0.5 - delta_frac) * notional * 0.06
        naked_cap  = notional * 0.20 - otm_disc + cp_dollars

        if strategy_idx == 1:          # defined risk / spread
            # Estimate wing width from delta positioning:
            # width ≈ (0.5 − delta) × price × 0.20
            width = max((0.5 - delta_frac) * S * 0.20, S * 0.02)
            return width * mult

        if strategy_idx == 0:          # strangle / straddle — single margin block (not doubled)
            return max(naked_cap, cp_dollars * 1.5)

        # idx 2 — naked single leg
        return max(naked_cap, cp_dollars * 1.5)

    def _recalc(self):
        pct       = self.pct_slider.value()
        delta_raw = self.delta_slider.value()          # 1-50 integer
        dte       = self.dte_slider.value()
        cp        = float(self.cp_spin.value())
        mult      = float(self.mult_spin.value()) or 100
        idx       = self.strategy_combo.currentIndex()
        delta_frac = delta_raw / 100.0

        # Update slider labels
        self.pct_val.setText(f"{pct}%")
        self.delta_val.setText(f"{delta_raw}Δ")
        self.dte_val.setText(f"{dte}d")

        # Premium per contract in dollars (what the broker shows as credit/debit)
        prem_per_contract = cp * mult
        self.cp_total_lbl.setText(f"= ${prem_per_contract:,.2f} / contract")

        # For a strangle we collect two legs; show that in premium total
        sides = 2 if idx == 0 else 1   # strangle = 2 legs, everything else = 1
        cp_dollars = prem_per_contract * sides   # total premium per position

        # Capital per contract
        cap_per = max(self._cap_per_contract(cp_dollars, delta_frac, dte, idx), 1.0)

        max_cap   = self.nlv * pct / 100.0
        contracts = max(int(max_cap / cap_per), 0)
        total_cap = contracts * cap_per
        total_prem = contracts * cp_dollars
        pct_used   = total_cap / self.nlv * 100 if self.nlv else 0
        remaining  = self.nlv - total_cap

        # Theta estimate: total_premium / (DTE × ~1.7)  (not linear, rough approx)
        theta_est = total_prem / (dte * 1.7) if dte > 0 and contracts > 0 else 0

        # P(profit) for short strategies: Δ ≈ P(expiring ITM), so P(OTM) = 1 − Δ
        # For a strangle both sides: both must stay OTM
        if idx in (0, 2):       # undefined short
            pop = (1 - delta_frac) * 100
        elif idx == 3:          # long option — P(ITM) ≈ delta
            pop = delta_frac * 100
        else:
            pop = None

        # ── Update labels ──────────────────────────────────────────────────────
        c_label = "contract" if contracts == 1 else "contracts"
        self.contracts_lbl.setText(f"{contracts} {c_label}")

        if total_prem > 0:
            prefix = "Total credit:" if idx in (0, 2) else ("Total cost:" if idx == 3 else "Total prem:")
            self.total_prem_lbl.setText(f"${total_prem:,.0f}")
            self.total_prem_lbl.setStyleSheet(
                f"color: {T.GREEN if idx in (0, 2) else T.RED if idx == 3 else T.TEXT}; "
                f"font-size: 22px; font-weight: bold; border: none;"
            )
        else:
            self.total_prem_lbl.setText("—")

        self.capital_lbl.setText(
            f"Capital per contract: ${cap_per:,.0f}   ·   Total BP: ${total_cap:,.0f}"
        )
        self.bp_lbl.setText(
            f"{pct_used:.1f}% of NLV used   ·   Remaining BP: ${remaining:,.0f}"
        )
        theta_text = f"~${theta_est:,.2f}/day" if theta_est > 0 else "—"
        roc = (total_prem / total_cap * 100) if total_cap > 0 and total_prem > 0 else 0
        self.theta_lbl.setText(
            f"Theta est.: {theta_text}   ·   Return on capital: {roc:.1f}% (if held to exp.)"
        )
        if pop is not None:
            self.pop_lbl.setText(f"P(profit) ≈ {pop:.0f}%  (1 − delta, single side)")
        else:
            self.pop_lbl.setText("")


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
        lay.addSpacing(8)

        rm_btn = QPushButton("🗑")
        rm_btn.setFixedSize(32, 32)
        rm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rm_btn.setToolTip(f"Remove {ticker} from watchlist")
        rm_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.RED}; "
            f"border: 1px solid {T.RED}; border-radius: 6px; font-size: 14px; }}"
            f"QPushButton:hover {{ background: {T.RED}; color: white; }}"
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
        self.add_input.setPlaceholderText("Add ticker  (e.g. AAPL, MES, 6A)")
        self.add_input.setFixedWidth(220)
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
        ticker = _normalize_ticker(self.add_input.text())
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
