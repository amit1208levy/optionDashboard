"""Watchlist page — multiple named lists, pin/star, IVR stars, liquidity stars."""
import json
import os
import re
import time

import api
from models import symbol_ivr, symbol_ivp, symbol_beta
import theme as T

_FUT_MONTH = "FGHJKMNQUVXZ"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _has_price(quote):
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
    t = text.strip().upper().lstrip("/")
    m = re.match(rf"^([A-Z0-9]{{1,5}})[{_FUT_MONTH}]\d{{1,2}}$", t)
    if m:
        return m.group(1)
    return t


def _stars_html(n, max_n=5, color=None):
    """Return HTML ★/☆ string.  n is already the star count (0–max_n)."""
    n = max(0, min(max_n, int(round(n or 0))))
    if color is None:
        if n == 0:
            color = T.MUTED
        elif n <= 1:
            color = T.RED
        elif n == 2:
            color = "#f97316"
        elif n == 3:
            color = T.YELLOW
        else:
            color = T.GREEN
    filled = f"<span style='color:{color}'>{'★' * n}</span>"
    empty  = f"<span style='color:{T.MUTED}'>{'☆' * (max_n - n)}</span>"
    return filled + empty


def _liq_stars(rating):
    """HTML stars for TastyTrade liquidity-rating (0-5 int)."""
    if rating is None:
        return f"<span style='color:{T.MUTED}'>—</span>"
    return _stars_html(rating)


def _ivr_stars(ivr):
    """IVR 0-100 → 1-5 star HTML (shown next to numeric IVR)."""
    if ivr is None:
        return ""
    n = min(5, max(1, round(ivr / 20)))
    color = T.GREEN if ivr >= 60 else (T.YELLOW if ivr >= 30 else T.RED)
    return _stars_html(n, color=color)


from PyQt6.QtCore import Qt, QThread, pyqtSignal, QStringListModel
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QScrollArea, QDialog, QSlider,
    QComboBox, QDoubleSpinBox, QSizePolicy, QInputDialog, QMessageBox,
    QCompleter, QListView,
)


# ── Known futures roots (offline, instant lookup) ────────────────────────────

_FUTURES_ROOTS = {
    # Equity index
    "ES":  "E-mini S&P 500",       "MES": "Micro E-mini S&P 500",
    "NQ":  "E-mini NASDAQ-100",    "MNQ": "Micro E-mini NASDAQ-100",
    "RTY": "E-mini Russell 2000",  "M2K": "Micro E-mini Russell 2000",
    "YM":  "E-mini Dow Jones",     "MYM": "Micro E-mini Dow Jones",
    "EMD": "E-mini S&P MidCap",    "VX":  "CBOE VIX Futures",
    # Currency
    "6E":  "Euro FX",              "6B":  "British Pound",
    "6J":  "Japanese Yen",         "6A":  "Australian Dollar",
    "6C":  "Canadian Dollar",      "6S":  "Swiss Franc",
    "6N":  "New Zealand Dollar",   "DX":  "US Dollar Index",
    "6M":  "Mexican Peso",         "6Z":  "South African Rand",
    # Energy
    "CL":  "Crude Oil WTI",        "MCL": "Micro Crude Oil",
    "NG":  "Natural Gas",          "RB":  "RBOB Gasoline",
    "HO":  "Heating Oil",          "QM":  "E-mini Crude Oil",
    # Metals
    "GC":  "Gold",                 "MGC": "Micro Gold",
    "SI":  "Silver",               "SIL": "E-mini Silver",
    "HG":  "Copper",               "PL":  "Platinum",
    "PA":  "Palladium",
    # Agriculture
    "ZC":  "Corn",                 "ZS":  "Soybeans",
    "ZW":  "Wheat",                "ZL":  "Soybean Oil",
    "ZM":  "Soybean Meal",         "KC":  "Coffee",
    "CC":  "Cocoa",                "CT":  "Cotton",
    "SB":  "Sugar No. 11",         "OJ":  "Orange Juice",
    # Interest rates
    "ZB":  "30-Year T-Bond",       "UB":  "Ultra T-Bond",
    "ZN":  "10-Year T-Note",       "ZF":  "5-Year T-Note",
    "ZT":  "2-Year T-Note",        "ZQ":  "30-Day Fed Funds",
    # Livestock
    "LE":  "Live Cattle",          "HE":  "Lean Hogs",
    "GF":  "Feeder Cattle",
    # Crypto
    "BTC": "Bitcoin",              "MBT": "Micro Bitcoin",
    "ETH": "Ether",                "MET": "Micro Ether",
}


# ── Known index / ETF symbols (offline, instant lookup) ──────────────────────

_INDEX_SYMBOLS = {
    # Cash indices (option underlyings)
    "SPX":  "S&P 500 Index",          "SPXW": "S&P 500 Weekly Index",
    "NDX":  "NASDAQ-100 Index",       "NQX":  "NASDAQ-100 Reduced Value Index",
    "RUT":  "Russell 2000 Index",     "RUA":  "Russell 3000 Index",
    "VIX":  "CBOE Volatility Index",  "VIX9D":"CBOE VIX 9-Day",
    "DJX":  "Dow Jones Index",        "XSP":  "Mini-SPX Index",
    "MXEA": "MSCI EAFE Index",        "MXEF": "MSCI EM Index",
    # Mega-cap & widely-traded equities
    "AAPL": "Apple",                  "MSFT": "Microsoft",
    "NVDA": "NVIDIA",                 "AMZN": "Amazon",
    "GOOGL":"Alphabet A",             "GOOG": "Alphabet C",
    "META": "Meta Platforms",         "TSLA": "Tesla",
    "BRK":  "Berkshire Hathaway",     "JPM":  "JPMorgan Chase",
    "V":    "Visa",                   "MA":   "Mastercard",
    "UNH":  "UnitedHealth",           "XOM":  "Exxon Mobil",
    "LLY":  "Eli Lilly",              "AVGO": "Broadcom",
    "AMD":  "Advanced Micro Devices", "INTC": "Intel",
    "NFLX": "Netflix",                "CRM":  "Salesforce",
    "ORCL": "Oracle",                 "ADBE": "Adobe",
    "SHOP": "Shopify",                "PLTR": "Palantir",
    "COIN": "Coinbase",               "HOOD": "Robinhood",
    "SOFI": "SoFi Technologies",      "UBER": "Uber",
    # Popular ETFs
    "SPY":  "SPDR S&P 500 ETF",       "IVV":  "iShares S&P 500 ETF",
    "VOO":  "Vanguard S&P 500 ETF",   "QQQ":  "Invesco NASDAQ-100 ETF",
    "IWM":  "iShares Russell 2000 ETF","DIA":  "SPDR Dow Jones ETF",
    "GLD":  "SPDR Gold ETF",          "IAU":  "iShares Gold ETF",
    "SLV":  "iShares Silver ETF",     "GDX":  "VanEck Gold Miners ETF",
    "USO":  "United States Oil ETF",  "XLE":  "Energy Select SPDR",
    "XLF":  "Financial Select SPDR",  "XLK":  "Technology Select SPDR",
    "XLV":  "Health Care Select SPDR","XLI":  "Industrial Select SPDR",
    "XLY":  "Consumer Discret. SPDR", "XLP":  "Consumer Staples SPDR",
    "XLB":  "Materials Select SPDR",  "XLRE": "Real Estate Select SPDR",
    "XLU":  "Utilities Select SPDR",  "XLC":  "Communication SPDR",
    "TLT":  "iShares 20+ T-Bond ETF", "IEF":  "iShares 7-10 T-Bond ETF",
    "HYG":  "iShares High Yield ETF", "LQD":  "iShares Corp Bond ETF",
    "EEM":  "iShares MSCI EM ETF",    "EFA":  "iShares MSCI EAFE ETF",
    "SMH":  "VanEck Semiconductor ETF","SOXX": "iShares Semiconductor ETF",
    "ARKK": "ARK Innovation ETF",     "ARKG": "ARK Genomic ETF",
    "UVXY": "ProShares Ultra VIX ETF", "SVXY": "ProShares Short VIX ETF",
    "TQQQ": "ProShares UltraPro QQQ", "SQQQ": "ProShares UltraPro Short QQQ",
    "UPRO": "ProShares UltraPro S&P", "SPXU": "ProShares UltraPro Short S&P",
    "SOXL": "Direxion Semi Bull 3X",  "SOXS": "Direxion Semi Bear 3X",
    "LABU": "Direxion Bio Bull 3X",   "LABD": "Direxion Bio Bear 3X",
    "FNGU": "MicroSectors FANG+ Bull","FNGD": "MicroSectors FANG+ Bear",
}


# ── Local ticker data (loaded once at import time) ────────────────────────────

def _load_ticker_items():
    """Return a list of display strings from tickers.json for the QCompleter."""
    path = os.path.join(os.path.dirname(__file__), "tickers.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = []
    items = []
    for t in data:
        sym  = (t.get("symbol") or "").strip()
        desc = (t.get("description") or "").strip()
        kind = (t.get("type") or "").strip()
        if not sym:
            continue
        if desc and kind:
            items.append(f"{sym}  ·  {desc}  [{kind}]")
        elif desc:
            items.append(f"{sym}  ·  {desc}")
        else:
            items.append(sym)
    return items


_COMPLETER_ITEMS = _load_ticker_items()


# ── Autocomplete completer ────────────────────────────────────────────────────
# Using QCompleter lets Qt handle popup positioning, z-order, and macOS window
# management — all the things that a hand-rolled popup struggles with.

class _SymCompleter(QCompleter):
    """
    Completer backed by the local tickers.json file.

    splitPath returns [""] so Qt shows everything already in the model;
    we populate the model ourselves in _on_input_changed with results
    sorted by match quality:
      1. exact symbol match
      2. symbol starts with query
      3. symbol contains query (substring)
      4. description contains query  (e.g. "gold" → GC, GLD)

    pathFromIndex strips the display label back to just the ticker symbol.
    """
    def splitPath(self, _path):        # let our manual filter handle it
        return [""]

    def pathFromIndex(self, index):    # insert only the symbol on selection
        text = index.data() or ""
        return text.split("  ·  ")[0].strip()


# ── Fetch worker ──────────────────────────────────────────────────────────────

class _FetchWorker(QThread):
    done = pyqtSignal(dict, dict)   # metrics, quotes

    def __init__(self, token, tickers, parent=None, quotes=None):
        super().__init__(parent)
        self.token   = token
        self.tickers = tickers
        # Optional QuotesProvider — used for live equity/futures quotes when
        # supplied; falls back to direct api.get_market_data otherwise.
        self.quotes  = quotes

    def run(self):
        if not self.tickers:
            self.done.emit({}, {})
            return

        # Known futures roots — these should always resolve as futures, not as
        # accidentally-matching stock tickers (e.g. "ES" the equity is
        # Eversource Energy at ~$66, NOT E-mini S&P at ~$5,800).
        from models import _CONTRACT_MULT
        from concurrent.futures import ThreadPoolExecutor
        known_futures_roots = set(_CONTRACT_MULT.keys())

        futures_tickers = [t for t in self.tickers
                           if t.startswith("/") or t in known_futures_roots]
        equity_tickers  = [t for t in self.tickers if t not in futures_tickers]
        roots = [t.lstrip("/") for t in futures_tickers]

        # Live equity/futures quotes go through the QuotesProvider (so we can
        # swap data vendors without changing this worker).  Market metrics
        # stay on TastyTrade — they're slow-changing daily values.
        def _fetch_eq_q():
            if self.quotes is not None:
                return self.quotes.get_quotes(equities=equity_tickers)
            return api.get_market_data(self.token, equities=equity_tickers)

        def _fetch_fut_q(syms):
            if self.quotes is not None:
                return self.quotes.get_quotes(futures=syms)
            return api.get_market_data(self.token, futures=syms)

        # ── Fire ALL independent API calls in parallel ──────────────────────
        # Old path ran these sequentially (~1.2s total).  In parallel the
        # whole fetch is bounded by the slowest single call (~250ms).
        with ThreadPoolExecutor(max_workers=5) as ex:
            f_eq_m  = ex.submit(api.get_market_metrics, self.token, equity_tickers) \
                      if equity_tickers else None
            f_eq_q  = ex.submit(_fetch_eq_q) if equity_tickers else None
            f_fut_m = ex.submit(api.get_market_metrics, self.token,
                                [f"/{r}" for r in roots]) if roots else None
            # Futures quotes need active-contract resolution first; submit that
            f_fut_r = ex.submit(api.get_futures_active_contracts, self.token, roots) \
                      if roots else None

            metrics = f_eq_m.result() if f_eq_m else {}
            quotes  = f_eq_q.result() if f_eq_q else {}

            if roots:
                # Merge futures metrics, keyed by bare root
                fut_metrics = f_fut_m.result() if f_fut_m else {}
                for r in roots:
                    for key in (f"/{r}", r):
                        if key in fut_metrics:
                            metrics[r] = fut_metrics[key]
                            break

                # Build futures quote fetch once active contracts are resolved
                root_map = f_fut_r.result() if f_fut_r else {}
                fut_syms    = []
                sym_to_root = {}
                for root in roots:
                    contract = root_map.get(root) or f"/{root}"
                    fut_syms.append(contract)
                    sym_to_root[contract] = root
                # This one runs in the main thread since we needed root_map
                fut_quotes = _fetch_fut_q(fut_syms)
                for sym, q in fut_quotes.items():
                    root = sym_to_root.get(sym) or sym_to_root.get("/" + sym.lstrip("/"))
                    if root:
                        quotes[root] = q

        # Fallback for equity tickers that returned no price (maybe they were
        # unknown futures roots)
        missing = [t for t in equity_tickers if not _has_price(quotes.get(t))]
        if missing:
            root_map = api.get_futures_active_contracts(self.token, missing)
            fut_syms    = []
            sym_to_root = {}
            for root in missing:
                contract = root_map.get(root) or f"/{root}"
                fut_syms.append(contract)
                sym_to_root[contract] = root
            if fut_syms:
                fut_quotes = _fetch_fut_q(fut_syms)
                for sym, q in fut_quotes.items():
                    root = sym_to_root.get(sym) or sym_to_root.get("/" + sym.lstrip("/"))
                    if root and root in missing:
                        quotes[root] = q

        self.done.emit(metrics, quotes)


# ── Position sizer dialog ─────────────────────────────────────────────────────

class _PremiumFetchWorker(QThread):
    """Fetch the option-chain closest to (target_dte, target_delta) and
    return the mark price + the actual delta/DTE that matched."""
    done = pyqtSignal(dict)   # {"premium": float, "delta": float, "dte": int, "strike": float, "error": str}

    def __init__(self, token, ticker, target_dte, target_delta_pct, direction="call",
                 quotes=None):
        super().__init__()
        self.token          = token
        self.ticker         = ticker
        self.target_dte     = target_dte
        self.target_delta   = target_delta_pct / 100.0   # slider is 20 → 0.20
        self.direction      = direction    # "call" or "put"
        # Optional QuotesProvider — uses TastyTrade directly if not supplied.
        self.quotes         = quotes

    def run(self):
        from datetime import date
        from collections import OrderedDict
        try:
            expirations = api.get_option_chain(self.token, self.ticker)
            if not expirations:
                self.done.emit({"error": "no option chain"})
                return

            # Pick the expiration closest to target DTE
            today = date.today()
            def dte_for(exp):
                raw = exp.get("expiration-date") or ""
                try:
                    return (date.fromisoformat(raw[:10]) - today).days
                except ValueError:
                    return 10**9
            best_exp = min(expirations, key=lambda e: abs(dte_for(e) - self.target_dte))
            actual_dte = dte_for(best_exp)

            # Collect option symbols for all strikes at that expiration
            strikes = best_exp.get("strikes", []) or []
            if not strikes:
                self.done.emit({"error": "no strikes"})
                return

            sym_key = "call" if self.direction == "call" else "put"
            sym_to_strike = {}
            syms = []
            for s in strikes:
                sym = s.get(sym_key)
                if sym:
                    syms.append(sym)
                    try:
                        sym_to_strike[sym] = float(s.get("strike-price") or 0)
                    except (TypeError, ValueError):
                        sym_to_strike[sym] = 0.0

            # Fetch market data (mark + delta) for all these option symbols
            if self.quotes is not None:
                quotes = self.quotes.get_quotes(equity_options=syms)
            else:
                quotes = api.get_market_data(self.token, equity_options=syms)

            # Find the strike whose |delta| is closest to our target
            best_sym, best_gap = None, 99.0
            for sym in syms:
                q = quotes.get(sym) or {}
                try:
                    d = abs(float(q.get("delta") or 0))
                except (TypeError, ValueError):
                    continue
                gap = abs(d - self.target_delta)
                if gap < best_gap:
                    best_gap, best_sym = gap, sym
            if not best_sym:
                self.done.emit({"error": "no deltas returned (market closed?)"})
                return

            q   = quotes[best_sym]
            try:
                bid = float(q.get("bid") or 0)
                ask = float(q.get("ask") or 0)
                mark = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else float(q.get("mark") or 0)
            except (TypeError, ValueError):
                mark = 0.0
            try:
                delta = abs(float(q.get("delta") or 0))
            except (TypeError, ValueError):
                delta = 0.0

            self.done.emit({
                "premium": mark,
                "delta":   delta,
                "dte":     actual_dte,
                "strike":  sym_to_strike.get(best_sym, 0.0),
                "error":   "",
            })
        except Exception as e:
            self.done.emit({"error": str(e)})


class PositionSizerDialog(QDialog):
    def __init__(self, ticker, price, nlv, parent=None, token=None, quotes=None):
        super().__init__(parent)
        self.ticker = ticker
        self.token  = token
        self.quotes = quotes   # forwarded to _PremiumFetchWorker
        self._fetch_worker = None
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

        sub = QLabel(f"Current price: ${price:,.2f}   ·   Account NLV: ${nlv:,.0f}")
        sub.setStyleSheet(f"color: {T.MUTED}; font-size: 11px; border: none;")
        lay.addWidget(sub)

        self._sep(lay)

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
        self.strategy_combo.currentIndexChanged.connect(self._auto_fetch_premium)
        lay.addWidget(self.strategy_combo)

        self._lbl(lay, "Max capital allocation  (% of NLV)")
        pct_row = QHBoxLayout()
        self.pct_slider = QSlider(Qt.Orientation.Horizontal)
        self.pct_slider.setRange(1, 100)
        self.pct_slider.setValue(5)
        self.pct_slider.valueChanged.connect(self._recalc)
        self.pct_val = QLabel("5%")
        self.pct_val.setFixedWidth(36)
        self.pct_val.setStyleSheet(f"color: {T.ACCENT}; font-weight: bold; border: none;")
        pct_row.addWidget(self.pct_slider)
        pct_row.addWidget(self.pct_val)
        lay.addLayout(pct_row)

        # Warning shown when allocation exceeds safe threshold
        self.pct_warn_lbl = QLabel("")
        self.pct_warn_lbl.setStyleSheet(
            f"color: {T.YELLOW}; font-size: 10px; border: none; "
            f"background: transparent; padding-top: 2px;"
        )
        self.pct_warn_lbl.setWordWrap(True)
        lay.addWidget(self.pct_warn_lbl)

        self._sep(lay)

        self._lbl(lay, "Premium per contract  (auto-fetched from option chain)")
        cp_row = QHBoxLayout()
        self.cp_spin = QDoubleSpinBox()
        self.cp_spin.setRange(0.01, 99_999)
        self.cp_spin.setDecimals(2)
        self.cp_spin.setSingleStep(0.10)
        self.cp_spin.setValue(max(round(price * 0.02 * 4) / 4, 0.05))
        self._match_lbl = QLabel("")   # shows matched DTE/delta after auto-fetch
        self.cp_spin.setStyleSheet(
            f"QDoubleSpinBox {{ background: {T.CARD}; color: {T.TEXT}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 4px 8px; }}"
        )
        self.cp_spin.valueChanged.connect(self._recalc)
        cp_row.addWidget(self.cp_spin)
        x = QLabel("×")
        x.setStyleSheet(f"color: {T.MUTED}; border: none;")
        cp_row.addWidget(x)
        self.mult_spin = QDoubleSpinBox()
        self.mult_spin.setRange(1, 100_000)
        self.mult_spin.setDecimals(0)
        self.mult_spin.setSingleStep(100)
        self.mult_spin.setValue(100)
        self.mult_spin.setFixedWidth(90)
        self.mult_spin.setStyleSheet(
            f"QDoubleSpinBox {{ background: {T.CARD}; color: {T.TEXT}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 4px 8px; }}"
        )
        self.mult_spin.valueChanged.connect(self._recalc)
        cp_row.addWidget(self.mult_spin)
        self.cp_total_lbl = QLabel()
        self.cp_total_lbl.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 12px; font-weight: bold; "
            f"border: none; margin-left: 8px;"
        )
        cp_row.addWidget(self.cp_total_lbl)
        cp_row.addStretch()
        lay.addLayout(cp_row)

        # Match-info line (populated by _auto_fetch_premium)
        self._match_lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; border: none; "
            f"background: transparent; padding-left: 2px;"
        )
        lay.addWidget(self._match_lbl)

        self._lbl(lay, "Delta  (of the option leg — e.g. 20 = 20Δ; 50+ = ITM)")
        delta_row = QHBoxLayout()
        self.delta_slider = QSlider(Qt.Orientation.Horizontal)
        self.delta_slider.setRange(1, 95)
        self.delta_slider.setValue(20)
        self.delta_slider.valueChanged.connect(self._recalc)
        self.delta_slider.sliderReleased.connect(self._auto_fetch_premium)
        self.delta_val = QLabel("20Δ")
        self.delta_val.setFixedWidth(42)
        self.delta_val.setStyleSheet(f"color: {T.ACCENT}; font-weight: bold; border: none;")
        delta_row.addWidget(self.delta_slider)
        delta_row.addWidget(self.delta_val)
        lay.addLayout(delta_row)

        self._lbl(lay, "Estimated DTE  (1d – 2y, for LEAPs)")
        dte_row = QHBoxLayout()
        self.dte_slider = QSlider(Qt.Orientation.Horizontal)
        self.dte_slider.setRange(1, 730)
        self.dte_slider.setValue(45)
        self.dte_slider.valueChanged.connect(self._recalc)
        self.dte_slider.sliderReleased.connect(self._auto_fetch_premium)
        self.dte_val = QLabel("45d")
        self.dte_val.setFixedWidth(42)
        self.dte_val.setStyleSheet(f"color: {T.ACCENT}; font-weight: bold; border: none;")
        dte_row.addWidget(self.dte_slider)
        dte_row.addWidget(self.dte_val)
        lay.addLayout(dte_row)

        res = QFrame()
        res.setStyleSheet(
            f"background: {T.CARD}; border-radius: 8px; border: 1px solid {T.BORDER};"
        )
        res_lay = QVBoxLayout(res)
        res_lay.setContentsMargins(16, 14, 16, 16)
        res_lay.setSpacing(6)
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

        # Kick off an initial auto-fetch so the premium starts accurate
        if self.token:
            self._auto_fetch_premium()

    def _auto_fetch_premium(self):
        """Fetch option-chain premium matching the current DTE + Delta sliders."""
        if not self.token:
            return
        # Only equity/ETF options; skip for "Stock / ETF" and "Long Option"
        idx = self.strategy_combo.currentIndex()
        if idx == 4:   # Stock/ETF has no option premium
            return
        # Avoid overlapping fetches
        if self._fetch_worker and self._fetch_worker.isRunning():
            return

        # Direction: short strategies usually price off puts; use put-side
        # for short-put / strangle / naked-put and calls for long/naked-call.
        direction = "put" if idx in (0, 2) else "call"

        self._match_lbl.setText("Fetching option chain…")
        self._fetch_worker = _PremiumFetchWorker(
            self.token, self.ticker,
            target_dte=self.dte_slider.value(),
            target_delta_pct=self.delta_slider.value(),
            direction=direction,
            quotes=self.quotes,
        )
        self._fetch_worker.done.connect(self._on_premium_fetched)
        self._fetch_worker.start()

    def _on_premium_fetched(self, result):
        if result.get("error"):
            self._match_lbl.setText(f"Auto-fetch failed: {result['error']} — enter manually")
            return
        self.cp_spin.blockSignals(True)
        self.cp_spin.setValue(result["premium"])
        self.cp_spin.blockSignals(False)
        self._match_lbl.setText(
            f"Matched: strike ${result['strike']:.2f}  ·  "
            f"{int(result['delta']*100)}Δ  ·  {result['dte']}d  ·  "
            f"mark ${result['premium']:.2f}"
        )
        self._recalc()

    def _sep(self, lay):
        s = QFrame()
        s.setFrameShape(QFrame.Shape.HLine)
        s.setStyleSheet(f"background: {T.BORDER}; max-height: 1px; border: none;")
        lay.addWidget(s)

    def _lbl(self, lay, text):
        l = QLabel(text)
        l.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"border: none; margin-top: 4px;"
        )
        lay.addWidget(l)

    # Strategy labels for each combo index (human-readable names for UI)
    _STRAT_LABELS = {
        0: ("strangle",     "strangles"),   # Short Strangle/Straddle
        1: ("spread",       "spreads"),     # Iron Condor/Spread
        2: ("contract",     "contracts"),   # Naked Put/Call
        3: ("contract",     "contracts"),   # Long Option
        4: ("share",        "shares"),      # Stock/ETF (uses 1 share unit)
    }

    def _cap_per_unit(self, cp_dollars, delta_frac, dte, idx):
        """Return the buying power required for ONE trade unit.
        Unit varies by strategy: one strangle / one spread / one contract /
        one long option / one share."""
        mult = float(self.mult_spin.value()) or 100
        S    = self.price
        # Stock: one share costs S.  Ignore mult (user often leaves it at 100).
        if idx == 4:
            return S
        # Long option debit: the premium paid IS the max risk.
        if idx == 3:
            return cp_dollars
        notional  = S * mult
        otm_disc  = (0.5 - delta_frac) * notional * 0.06
        # Reg-T naked: 20% of notional (rough) minus a small OTM discount
        # plus the premium received (adds to cash required).
        naked_cap = notional * 0.20 - otm_disc + cp_dollars
        if idx == 1:
            # Defined-risk spread: width × multiplier (max loss per spread)
            width = max((0.5 - delta_frac) * S * 0.20, S * 0.02)
            return width * mult
        return max(naked_cap, cp_dollars * 1.5)

    def _recalc(self):
        pct        = self.pct_slider.value()
        delta_raw  = self.delta_slider.value()
        dte        = self.dte_slider.value()
        cp         = float(self.cp_spin.value())
        mult       = float(self.mult_spin.value()) or 100
        idx        = self.strategy_combo.currentIndex()
        delta_frac = delta_raw / 100.0
        self.pct_val.setText(f"{pct}%")
        self.delta_val.setText(f"{delta_raw}Δ")
        self.dte_val.setText(f"{dte}d")
        # Allocation warning
        if pct >= 30:
            self.pct_warn_lbl.setText(
                f"⚠  {pct}% is aggressive. Conventional wisdom: keep any single "
                f"trade ≤ 30% of portfolio (5% is typical)."
            )
        else:
            self.pct_warn_lbl.setText("")
        prem_per = cp * mult
        self.cp_total_lbl.setText(f"= ${prem_per:,.2f} / contract")
        sides      = 2 if idx == 0 else 1
        cp_dollars = prem_per * sides
        cap_per    = max(self._cap_per_unit(cp_dollars, delta_frac, dte, idx), 1.0)
        max_cap    = self.nlv * pct / 100.0
        units      = max(int(max_cap / cap_per), 0)
        total_cap  = units * cap_per
        total_prem = units * cp_dollars
        pct_used   = total_cap / self.nlv * 100 if self.nlv else 0
        remaining  = self.nlv - total_cap
        theta_est  = total_prem / (dte * 1.7) if dte > 0 and units > 0 else 0
        pop = ((1 - delta_frac) * 100 if idx in (0, 2) else
               delta_frac * 100       if idx == 3 else None)

        unit_s, unit_p = self._STRAT_LABELS.get(idx, ("contract", "contracts"))
        unit_lbl       = unit_s if units == 1 else unit_p
        self.contracts_lbl.setText(f"{units} {unit_lbl}")
        if total_prem > 0:
            self.total_prem_lbl.setText(f"${total_prem:,.0f}")
            self.total_prem_lbl.setStyleSheet(
                f"color: {T.GREEN if idx in (0,2) else T.RED if idx==3 else T.TEXT}; "
                f"font-size: 22px; font-weight: bold; border: none;"
            )
        else:
            self.total_prem_lbl.setText("—")

        # "Buying power per X" — X changes with strategy, so the label is
        # clearer than the generic "per contract"
        self.capital_lbl.setText(
            f"Buying power per {unit_s}: ${cap_per:,.0f}   ·   "
            f"Total BP: ${total_cap:,.0f}"
        )
        self.bp_lbl.setText(
            f"{pct_used:.1f}% of NLV used   ·   Remaining BP: ${remaining:,.0f}"
        )
        roc = (total_prem / total_cap * 100) if total_cap > 0 and total_prem > 0 else 0
        theta_text = f"~${theta_est:,.2f}/day" if theta_est > 0 else "—"
        self.theta_lbl.setText(
            f"Theta est.: {theta_text}   ·   Return on capital: {roc:.1f}%"
        )
        self.pop_lbl.setText(
            f"P(profit) ≈ {pop:.0f}%  (1 − delta, single side)" if pop is not None else ""
        )


# ── Tab strip ─────────────────────────────────────────────────────────────────

class _TabStrip(QFrame):
    """Horizontal strip for switching / managing watchlists."""
    switched = pyqtSignal(str)       # watchlist id
    renamed  = pyqtSignal(str, str)  # id, new name
    deleted  = pyqtSignal(str)       # id
    created  = pyqtSignal()

    def __init__(self, watchlists, active_id, parent=None):
        super().__init__(parent)
        self.setFixedHeight(44)
        self.setStyleSheet(
            f"QFrame {{ background: {T.BG_ALT}; "
            f"border-bottom: 1px solid {T.BORDER}; }}"
        )
        outer = QHBoxLayout(self)
        outer.setContentsMargins(28, 5, 16, 0)
        outer.setSpacing(4)

        self._tabs_lay = QHBoxLayout()
        self._tabs_lay.setContentsMargins(0, 0, 0, 0)
        self._tabs_lay.setSpacing(2)
        outer.addLayout(self._tabs_lay)
        outer.addStretch()

        new_btn = QPushButton("＋ New list")
        new_btn.setFixedHeight(26)
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 5px; "
            f"font-size: 11px; padding: 0 10px; }}"
            f"QPushButton:hover {{ color: {T.GREEN}; border-color: {T.GREEN}; }}"
        )
        new_btn.clicked.connect(self.created.emit)
        outer.addWidget(new_btn)

        self.rebuild(watchlists, active_id)

    def rebuild(self, watchlists, active_id):
        while self._tabs_lay.count():
            item = self._tabs_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        multi = len(watchlists) > 1
        for wl in watchlists:
            self._tabs_lay.addWidget(
                self._make_tab(wl, wl["id"] == active_id, multi)
            )

    def _make_tab(self, wl, active, can_delete):
        f = QFrame()
        f.setCursor(Qt.CursorShape.PointingHandCursor)
        f.setFixedHeight(36)
        if active:
            f.setStyleSheet(
                f"QFrame {{ background: {T.CARD}; "
                f"border: 1px solid {T.BORDER}; border-bottom: 2px solid {T.PURPLE}; "
                f"border-radius: 6px 6px 0 0; }}"
            )
        else:
            f.setStyleSheet(
                f"QFrame {{ background: transparent; border: none; border-radius: 6px; }}"
                f"QFrame:hover {{ background: {T.CARD_ALT}; }}"
            )

        lay = QHBoxLayout(f)
        lay.setContentsMargins(12, 0, 7, 2)
        lay.setSpacing(6)

        ticker_count = len(wl.get("tickers", []))
        name_text = wl["name"]
        lbl = QLabel(
            f"{name_text}  "
            f"<span style='color:{T.MUTED};font-size:10px;'>{ticker_count}</span>"
        )
        lbl.setStyleSheet(
            f"color: {T.TEXT if active else T.MUTED}; "
            f"font-size: 12px; font-weight: {'600' if active else 'normal'}; "
            f"border: none; background: transparent;"
        )
        lay.addWidget(lbl)

        if can_delete:
            x = QPushButton("×")
            x.setFixedSize(15, 15)
            x.setCursor(Qt.CursorShape.PointingHandCursor)
            x.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T.MUTED}; "
                f"border: none; font-size: 12px; border-radius: 3px; padding: 0; }}"
                f"QPushButton:hover {{ color: {T.RED}; }}"
            )
            wl_id = wl["id"]
            x.clicked.connect(lambda _, i=wl_id: self.deleted.emit(i))
            lay.addWidget(x)

        wl_id   = wl["id"]
        wl_name = wl["name"]

        def _click(e, i=wl_id):
            if e.button() == Qt.MouseButton.LeftButton:
                self.switched.emit(i)
        f.mousePressEvent = _click
        lbl.mousePressEvent = _click

        def _dbl(e, i=wl_id, n=wl_name):
            new_name, ok = QInputDialog.getText(
                self, "Rename watchlist", "Name:", text=n
            )
            if ok and new_name.strip():
                self.renamed.emit(i, new_name.strip())
        f.mouseDoubleClickEvent = _dbl
        lbl.mouseDoubleClickEvent = _dbl

        return f


# ── Ticker row ────────────────────────────────────────────────────────────────

# Column definitions: (attr_name, header_label, width, sort_key)
# sort_key None = not sortable
_COLUMNS = [
    ("price_lbl", "Price",  90,  "price"),
    ("ivr_lbl",   "IVR ★", 100, "ivr"),
    ("ivp_lbl",   "IVP",    60,  "ivp"),
    ("beta_lbl",  "β-Wtd Δ", 70, "beta"),
    ("liq_lbl",   "Liq ★",  90,  "liq"),
]

# Mapping sort_key → (header_text, default_ascending)
# Ticker sorts A→Z by default; numeric cols sort high→low by default
_SORT_META = {
    "ticker": ("Ticker", True),
    "price":  ("Price",  False),
    "ivr":    ("IVR ★",  False),
    "ivp":    ("IVP",    False),
    "beta":   ("β-Wtd Δ", True),
    "liq":    ("Liq ★",  False),
}


class _TickerRow(QFrame):
    remove_clicked = pyqtSignal(str)
    pin_clicked    = pyqtSignal(str)
    size_clicked   = pyqtSignal(str, float, float)

    def __init__(self, ticker, nlv, pinned=False, parent=None):
        super().__init__(parent)
        self.ticker  = ticker
        self.nlv     = nlv
        self._price  = 0.0
        self._pinned = pinned

        self.setFixedHeight(54)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(0)

        # ── Star / pin ────────────────────────────────────────────────────────
        self._star_btn = QPushButton("★" if pinned else "☆")
        self._star_btn.setFixedSize(28, 28)
        self._star_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._star_btn.clicked.connect(lambda: self.pin_clicked.emit(self.ticker))
        lay.addWidget(self._star_btn)
        lay.addSpacing(6)

        # ── Ticker symbol ─────────────────────────────────────────────────────
        sym = QLabel(ticker)
        sym.setFixedWidth(80)
        sym.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 14px; font-weight: bold; border: none;"
        )
        lay.addWidget(sym)

        # ── Metric columns ────────────────────────────────────────────────────
        for attr, _hdr, width, _sk in _COLUMNS:
            lbl = QLabel("—")
            lbl.setFixedWidth(width)
            lbl.setStyleSheet(f"color: {T.TEXT}; font-size: 12px; border: none;")
            setattr(self, attr, lbl)
            lay.addWidget(lbl)

        lay.addStretch()

        # ── Size trade ────────────────────────────────────────────────────────
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

        # ── Remove ────────────────────────────────────────────────────────────
        rm_btn = QPushButton("✕")
        rm_btn.setFixedSize(28, 28)
        rm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rm_btn.setToolTip(f"Remove {ticker}")
        rm_btn.setStyleSheet(
            f"QPushButton {{ background: rgba(239,68,68,0.12); color: {T.RED}; "
            f"border: 1px solid rgba(239,68,68,0.25); font-size: 13px; "
            f"font-weight: bold; border-radius: 6px; }}"
            f"QPushButton:hover {{ background: rgba(239,68,68,0.28); "
            f"border-color: {T.RED}; }}"
        )
        rm_btn.clicked.connect(lambda: self.remove_clicked.emit(self.ticker))
        lay.addWidget(rm_btn)

        self._apply_pin_style()

    # ── Pin visual ────────────────────────────────────────────────────────────

    def _apply_pin_style(self):
        if self._pinned:
            self._star_btn.setText("★")
            self._star_btn.setToolTip("Unpin")
            self._star_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T.YELLOW}; "
                f"border: none; font-size: 16px; border-radius: 6px; }}"
                f"QPushButton:hover {{ background: rgba(251,191,36,0.12); }}"
            )
            self.setStyleSheet(
                f"QFrame {{ background: {T.CARD}; border-radius: 8px; "
                f"border: 1px solid {T.YELLOW}40; "
                f"border-left: 3px solid {T.YELLOW}; }}"
            )
        else:
            self._star_btn.setText("☆")
            self._star_btn.setToolTip("Pin to top")
            self._star_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T.MUTED}; "
                f"border: none; font-size: 16px; border-radius: 6px; }}"
                f"QPushButton:hover {{ color: {T.YELLOW}; "
                f"background: rgba(251,191,36,0.08); }}"
            )
            self.setStyleSheet(
                f"QFrame {{ background: {T.CARD}; border-radius: 8px; "
                f"border: 1px solid {T.BORDER}; }}"
            )

    def set_pinned(self, pinned):
        self._pinned = pinned
        self._apply_pin_style()

    # ── Data ──────────────────────────────────────────────────────────────────

    def update_data(self, metrics, quote):
        m = metrics or {}
        q = quote   or {}

        # Price
        price = None
        for key in ("mark", "last"):
            try:
                v = float(q.get(key) or 0)
                if v > 0:
                    price = v
                    break
            except (TypeError, ValueError):
                pass
        if price is None:
            try:
                price = (float(q.get("bid") or 0) + float(q.get("ask") or 0)) / 2 or None
            except (TypeError, ValueError):
                pass
        if price and price > 0:
            self._price = price
            self.price_lbl.setText(f"${price:,.2f}")
        else:
            self.price_lbl.setText("—")

        # IVR  (number + star rating)
        ivr = symbol_ivr(m)
        if ivr is not None:
            color = T.GREEN if ivr >= 60 else (T.YELLOW if ivr >= 30 else T.RED)
            stars = _ivr_stars(ivr)
            self.ivr_lbl.setText(
                f"<span style='color:{color};font-weight:bold'>{ivr:.0f}</span>"
                f"<span style='font-size:10px;'> {stars}</span>"
            )
        else:
            self.ivr_lbl.setText("—")

        # IVP
        ivp = symbol_ivp(m)
        self.ivp_lbl.setText(f"{ivp:.0f}" if ivp is not None else "—")

        # Beta
        beta = symbol_beta(m)
        self.beta_lbl.setText(f"{beta:.2f}" if beta is not None else "—")

        # Liquidity stars  (TastyTrade liquidity-rating: 0-5 int)
        liq_raw = m.get("liquidity-rating")
        self.liq_lbl.setText(_liq_stars(liq_raw))


# ── Watchlist page ────────────────────────────────────────────────────────────

class WatchlistPage(QWidget):
    back_requested = pyqtSignal()

    # Class-level cache survives widget recreations — re-opening the page
    # within _CACHE_TTL seconds skips the fetch and renders instantly.
    _CACHE_TTL  = 30.0                    # seconds
    _cache      = {"ts": 0.0,
                   "metrics": {}, "quotes": {},
                   "tickers": set()}      # shared across all instances

    # ── Init ──────────────────────────────────────────────────────────────────

    def __init__(self, token, nlv, parent=None, quotes=None):
        super().__init__(parent)
        self.token   = token
        self.nlv     = nlv
        # QuotesProvider — used for live equity/futures/option quotes.  When
        # None, falls back to direct api.get_market_data calls (legacy).
        self.quotes  = quotes
        self._rows   = {}    # ticker -> _TickerRow
        self._worker = None
        self._last_metrics: dict = {}
        self._last_quotes:  dict = {}
        self._sort_col: str | None = None   # active sort key
        self._sort_asc: bool       = True   # True = ascending

        self._watchlists, self._active_id = self._load_all()
        self._wl = self._find_wl(self._active_id)

        self.setStyleSheet(T.BASE_STYLE)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        self._tab_strip = _TabStrip(self._watchlists, self._active_id, self)
        self._tab_strip.switched.connect(self._switch_to)
        self._tab_strip.renamed.connect(self._rename_watchlist)
        self._tab_strip.deleted.connect(self._delete_watchlist)
        self._tab_strip.created.connect(self._create_watchlist)
        root.addWidget(self._tab_strip)

        root.addWidget(self._build_col_header())

        # ── Autocomplete via QCompleter backed by local tickers.json ─────────
        # Qt handles macOS popup z-order natively; no API calls during typing.
        # Real market data is fetched only when a ticker is actually added.
        self._completer = _SymCompleter(self.add_input)
        self._completer.setCompletionMode(
            QCompleter.CompletionMode.PopupCompletion
        )
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setMaxVisibleItems(12)
        self._completer.setModel(QStringListModel([], self._completer))

        _popup_view = QListView()
        _popup_view.setStyleSheet(
            f"QListView {{ background: {T.CARD}; color: {T.TEXT}; "
            f"border: 1px solid {T.PURPLE}; border-radius: 8px; "
            f"font-size: 13px; outline: none; }}"
            f"QListView::item {{ padding: 7px 12px; border-radius: 4px; }}"
            f"QListView::item:selected, QListView::item:hover "
            f"{{ background: {T.BG_ALT}; color: {T.ACCENT}; }}"
        )
        self._completer.setPopup(_popup_view)
        self.add_input.setCompleter(self._completer)
        self._completer.activated.connect(self._on_suggest_chosen)

        self.add_input.textChanged.connect(self._on_input_changed)
        self.add_input.installEventFilter(self)

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
        if self._wl["tickers"]:
            self._fetch()

    # ── Watchlist storage ─────────────────────────────────────────────────────

    @staticmethod
    def _load_all():
        settings = api.load_settings()
        if "watchlists_v2" in settings:
            wls    = settings["watchlists_v2"] or []
            active = settings.get("watchlist_active_id")
            if not wls:
                wls    = [{"id": "wl_1", "name": "Main", "tickers": [], "pinned": []}]
                active = "wl_1"
            if not active or not any(w["id"] == active for w in wls):
                active = wls[0]["id"]
            return wls, active
        # ── Migrate from old single-list format ──
        tickers = list(settings.get("watchlist_tickers") or [])
        pinned  = list(settings.get("watchlist_pinned")  or [])
        wl = {"id": "wl_1", "name": "Main", "tickers": tickers, "pinned": pinned}
        return [wl], "wl_1"

    def _find_wl(self, wl_id):
        return next((w for w in self._watchlists if w["id"] == wl_id),
                    self._watchlists[0])

    def _save_all(self):
        settings = api.load_settings()
        settings["watchlists_v2"]       = self._watchlists
        settings["watchlist_active_id"] = self._active_id
        # Remove legacy keys
        settings.pop("watchlist_tickers", None)
        settings.pop("watchlist_pinned",  None)
        api.save_settings(settings)

    def _save_current(self):
        """Flush current tickers/pinned back into the shared watchlists list."""
        self._wl["tickers"] = list(self._tickers)
        self._wl["pinned"]  = list(self._pinned)
        self._save_all()

    # ── Watchlist CRUD ────────────────────────────────────────────────────────

    def _switch_to(self, wl_id):
        if wl_id == self._active_id:
            return
        self._active_id    = wl_id
        self._wl           = self._find_wl(wl_id)
        self._last_metrics = {}
        self._last_quotes  = {}
        self._sort_col     = None   # reset sort for new list
        self._sort_asc     = True
        self._update_col_headers()
        self._completer.popup().hide()
        self._save_all()
        self._tab_strip.rebuild(self._watchlists, self._active_id)
        self._rebuild_rows()
        if self._wl["tickers"]:
            self._fetch()

    def _create_watchlist(self):
        name, ok = QInputDialog.getText(self, "New watchlist", "Name:")
        if not ok or not name.strip():
            return
        new_id = f"wl_{int(time.time() * 1000)}"
        self._watchlists.append(
            {"id": new_id, "name": name.strip(), "tickers": [], "pinned": []}
        )
        self._save_all()
        self._switch_to(new_id)

    def _rename_watchlist(self, wl_id, new_name):
        wl = self._find_wl(wl_id)
        wl["name"] = new_name
        self._save_all()
        self._tab_strip.rebuild(self._watchlists, self._active_id)

    def _delete_watchlist(self, wl_id):
        wl = self._find_wl(wl_id)
        n  = len(wl.get("tickers", []))
        msg = (
            f"Delete \"{wl['name']}\"?"
            + (f"\n\n{n} ticker{'s' if n != 1 else ''} will be removed." if n else "")
        )
        if QMessageBox.question(
            self, "Delete watchlist", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        ) != QMessageBox.StandardButton.Yes:
            return
        self._watchlists = [w for w in self._watchlists if w["id"] != wl_id]
        if not self._watchlists:
            self._watchlists = [
                {"id": "wl_1", "name": "Main", "tickers": [], "pinned": []}
            ]
        new_active = (self._active_id if any(w["id"] == self._active_id
                                              for w in self._watchlists)
                      else self._watchlists[0]["id"])
        self._save_all()
        self._switch_to(new_active)

    # ── Convenience props for active watchlist ────────────────────────────────

    @property
    def _tickers(self):
        return self._wl["tickers"]

    @_tickers.setter
    def _tickers(self, v):
        self._wl["tickers"] = v

    @property
    def _pinned(self):
        return set(self._wl.get("pinned") or [])

    @_pinned.setter
    def _pinned(self, v):
        self._wl["pinned"] = list(v)

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self):
        hdr = QFrame()
        hdr.setFixedHeight(60)
        hdr.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; "
            f"border-bottom: 1px solid {T.BORDER}; border-radius: 0; }}"
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
        self.add_input.setPlaceholderText("Add ticker  (e.g. AAPL, SPX, MES, 6A)")
        self.add_input.setMinimumWidth(280)
        self.add_input.setMaximumWidth(460)
        self.add_input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        # 40px: base-style has padding:8px top+bottom + font-size:14px ≈ 17px
        # total ≈ 35px; 40px gives comfortable room without clipping descenders.
        self.add_input.setFixedHeight(40)
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
        # Explicit refresh bypasses the cache
        self.refresh_btn.clicked.connect(lambda: self._fetch(force=True))
        hl.addWidget(self.refresh_btn)

        return hdr

    # ── Autocomplete ──────────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        # QCompleter handles FocusOut and Escape automatically.
        # We only intercept to hide the popup explicitly on Escape.
        if obj is self.add_input and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._completer.popup().hide()
                return True
        return super().eventFilter(obj, event)

    def _on_input_changed(self, text):
        q = text.strip()
        if not q:
            self._completer.popup().hide()
            return
        qu = q.upper()
        ql = q.lower()
        exact = []
        starts = []
        sym_has = []
        desc_has = []
        for item in _COMPLETER_ITEMS:
            sym = item.split("  ·  ")[0].strip().upper()
            if sym == qu:
                exact.append(item)
            elif sym.startswith(qu):
                starts.append(item)
            elif qu in sym:
                sym_has.append(item)
            elif ql in item.lower():
                desc_has.append(item)
        ordered = exact + starts + sym_has + desc_has
        self._completer.setModel(
            QStringListModel(ordered[:50], self._completer)
        )
        self._completer.complete()

    def _on_suggest_chosen(self, _text: str):
        # QCompleter already inserted pathFromIndex() (just the symbol) into
        # add_input via its activated signal; just trigger the add.
        self._add_ticker()

    def _build_col_header(self):
        bar = QFrame()
        bar.setFixedHeight(32)
        bar.setStyleSheet(
            f"background: {T.BG_ALT}; border-bottom: 1px solid {T.BORDER};"
        )
        hl = QHBoxLayout(bar)
        # left: body(28) + row-margin(12) + star-btn(28) + gap(6) = 74
        hl.setContentsMargins(74, 0, 120, 0)
        hl.setSpacing(0)

        self._col_hdr_lbls: dict = {}   # sort_key -> QLabel

        # Ticker column (sortable)
        ticker_lbl = self._make_sort_hdr("Ticker", "ticker", 80)
        hl.addWidget(ticker_lbl)

        for _attr, _hdr, width, sk in _COLUMNS:
            lbl = self._make_sort_hdr(_hdr, sk, width)
            hl.addWidget(lbl)

        hl.addStretch()
        self._update_col_headers()
        return bar

    def _make_sort_hdr(self, base_text, sort_key, width):
        lbl = QLabel()
        lbl.setFixedWidth(width)
        lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        lbl.setToolTip(f"Sort by {base_text}  (click again to reverse · third click clears)")
        self._col_hdr_lbls[sort_key] = lbl
        lbl.mousePressEvent = lambda _e, k=sort_key: self._on_sort_click(k)
        return lbl

    def _update_col_headers(self):
        for sk, lbl in self._col_hdr_lbls.items():
            base, _default_asc = _SORT_META[sk]
            active = (sk == self._sort_col)
            if active:
                arrow = " ▲" if self._sort_asc else " ▼"
                lbl.setText(base + arrow)
                lbl.setStyleSheet(
                    f"color: {T.ACCENT}; font-size: 10px; font-weight: bold; "
                    f"letter-spacing: 0.5px; border: none; "
                    f"background: {T.CARD_ALT}; border-radius: 3px; "
                    f"padding: 1px 3px;"
                )
            else:
                lbl.setText(base)
                lbl.setStyleSheet(
                    f"color: {T.MUTED}; font-size: 10px; font-weight: bold; "
                    f"letter-spacing: 0.5px; border: none;"
                )

    # ── Sort ──────────────────────────────────────────────────────────────────

    def _on_sort_click(self, col_key):
        if self._sort_col == col_key:
            _base, default_asc = _SORT_META[col_key]
            if self._sort_asc == default_asc:
                # Already at first direction → flip to reverse
                self._sort_asc = not self._sort_asc
            else:
                # At reverse → clear sort (back to insertion order)
                self._sort_col = None
                self._sort_asc = True
        else:
            self._sort_col = col_key
            self._sort_asc = _SORT_META[col_key][1]   # default direction

        self._update_col_headers()
        self._rebuild_rows()
        for t, row in self._rows.items():
            row.update_data(
                self._last_metrics.get(t, {}),
                self._last_quotes.get(t, {}),
            )

    def _get_price(self, quote):
        q = quote or {}
        for k in ("mark", "last"):
            try:
                v = float(q.get(k) or 0)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
        try:
            b = float(q.get("bid") or 0)
            a = float(q.get("ask") or 0)
            if b > 0 and a > 0:
                return (b + a) / 2.0
        except (TypeError, ValueError):
            pass
        return None

    def _raw_sort_value(self, ticker):
        """Return (is_none: int, value) for the active sort column."""
        m = self._last_metrics.get(ticker, {})
        q = self._last_quotes.get(ticker, {})
        col = self._sort_col

        if col == "ticker":
            return (0, ticker.lower())
        if col == "price":
            v = self._get_price(q)
        elif col == "ivr":
            v = symbol_ivr(m)
        elif col == "ivp":
            v = symbol_ivp(m)
        elif col == "beta":
            v = symbol_beta(m)
        elif col == "liq":
            raw = m.get("liquidity-rating")
            try:
                v = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                v = None
        else:
            v = None

        # None values always go to the end regardless of direction
        return (0 if v is not None else 1, v if v is not None else 0.0)

    def _apply_sort(self, tickers):
        """Return tickers sorted by active column; None values always last."""
        if not self._sort_col or not tickers:
            return list(tickers)
        reverse = not self._sort_asc
        return sorted(tickers, key=self._raw_sort_value, reverse=reverse)

    def stop_workers(self):
        """Gracefully stop background threads before this widget is deleted.

        Must be called before deleteLater() so that QThread::~QThread() is
        never reached while the C++ thread is still alive (SIGABRT / EXC_CRASH).
        """
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(5000)   # 5 s safety timeout

    # ── Ticker management ─────────────────────────────────────────────────────

    def _add_ticker(self):
        ticker = _normalize_ticker(self.add_input.text())
        if not ticker or ticker in self._tickers:
            self.add_input.clear()
            return
        self._tickers.append(ticker)
        self._save_current()
        self._tab_strip.rebuild(self._watchlists, self._active_id)
        self.add_input.clear()
        self._rebuild_rows()
        self._fetch()

    def _remove_ticker(self, ticker):
        tickers = self._tickers
        if ticker in tickers:
            tickers.remove(ticker)
        pinned = self._pinned
        pinned.discard(ticker)
        self._pinned = pinned
        self._last_metrics.pop(ticker, None)
        self._last_quotes.pop(ticker, None)
        self._save_current()
        self._tab_strip.rebuild(self._watchlists, self._active_id)
        self._rebuild_rows()

    def _toggle_pin(self, ticker):
        pinned = self._pinned
        if ticker in pinned:
            pinned.discard(ticker)
        else:
            pinned.add(ticker)
        self._pinned = pinned
        self._save_current()
        self._rebuild_rows()
        # Re-apply cached data after re-sort
        for t, row in self._rows.items():
            row.update_data(self._last_metrics.get(t, {}), self._last_quotes.get(t, {}))

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def _fetch(self, force: bool = False):
        if not self._tickers:
            return
        if self._worker and self._worker.isRunning():
            return

        # Serve from cache if fresh (≤ 30s) and ticker set unchanged
        import time
        cache = type(self)._cache
        age = time.time() - cache["ts"]
        if (not force
                and age < self._CACHE_TTL
                and cache["tickers"] == set(self._tickers)
                and cache["quotes"]):
            self._on_fetch_done(cache["metrics"], cache["quotes"])
            return

        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Loading…")
        self._worker = _FetchWorker(self.token, list(self._tickers), self,
                                    quotes=self.quotes)
        self._worker.done.connect(self._on_fetch_done_and_cache)
        self._worker.start()

    def _on_fetch_done_and_cache(self, metrics, quotes):
        # Store to class-level cache so re-opening the page is instant
        import time
        type(self)._cache = {
            "ts":      time.time(),
            "metrics": metrics,
            "quotes":  quotes,
            "tickers": set(self._tickers),
        }
        self._on_fetch_done(metrics, quotes)

    def _on_fetch_done(self, metrics, quotes):
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("↻  Refresh")
        self._last_metrics.update(metrics)
        self._last_quotes.update(quotes)
        if self._sort_col:
            # Re-sort rows now that real values are known
            self._rebuild_rows()
        for ticker, row in self._rows.items():
            row.update_data(
                self._last_metrics.get(ticker, {}),
                self._last_quotes.get(ticker, {}),
            )

    # ── UI rebuild ────────────────────────────────────────────────────────────

    def _ordered_tickers(self):
        """Return (pinned_list, unpinned_list) each sorted by active column."""
        pinned   = [t for t in self._tickers if t in self._pinned]
        unpinned = [t for t in self._tickers if t not in self._pinned]
        return self._apply_sort(pinned), self._apply_sort(unpinned)

    def _rebuild_rows(self):
        for row in self._rows.values():
            self.body.removeWidget(row)
            row.deleteLater()
        self._rows = {}

        # Remove divider if present
        if hasattr(self, "_divider"):
            self.body.removeWidget(self._divider)
            self._divider.deleteLater()
            del self._divider

        # Remove empty label if present
        if hasattr(self, "_empty_lbl"):
            self.body.removeWidget(self._empty_lbl)
            self._empty_lbl.deleteLater()
            del self._empty_lbl

        pinned_list, unpinned_list = self._ordered_tickers()
        ordered = pinned_list + unpinned_list

        if not ordered:
            empty = QLabel("No tickers yet — type a symbol above and press Add.")
            empty.setStyleSheet(
                f"color: {T.MUTED}; font-size: 13px; border: none;"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.body.insertWidget(0, empty)
            self._empty_lbl = empty
            return

        idx = 0
        for ticker in pinned_list:
            row = _TickerRow(ticker, self.nlv, pinned=True, parent=self)
            row.remove_clicked.connect(self._remove_ticker)
            row.pin_clicked.connect(self._toggle_pin)
            row.size_clicked.connect(self._open_sizer)
            self._rows[ticker] = row
            self.body.insertWidget(idx, row)
            idx += 1

        if pinned_list and unpinned_list:
            div = QFrame()
            div.setFixedHeight(1)
            div.setStyleSheet(f"background: {T.BORDER}; border: none;")
            self.body.insertWidget(idx, div)
            self._divider = div
            idx += 1

        for ticker in unpinned_list:
            row = _TickerRow(ticker, self.nlv, pinned=False, parent=self)
            row.remove_clicked.connect(self._remove_ticker)
            row.pin_clicked.connect(self._toggle_pin)
            row.size_clicked.connect(self._open_sizer)
            self._rows[ticker] = row
            self.body.insertWidget(idx, row)
            idx += 1

    def _open_sizer(self, ticker, price, nlv):
        dlg = PositionSizerDialog(ticker, price, nlv, self,
                                  token=self.token, quotes=self.quotes)
        dlg.exec()
