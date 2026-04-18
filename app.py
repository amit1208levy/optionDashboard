"""Options Dashboard — setup + portfolio + configure screens."""
import sys
from datetime import datetime, timezone

from PyQt6.QtWidgets import (
    QApplication, QWidget, QStackedWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFrame, QScrollArea, QComboBox,
    QDialog, QDialogButtonBox, QFormLayout, QMessageBox, QTextEdit
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal

import theme as T
import api
import updater
from version import VERSION
from models import (
    Position, StrategyInstance, unassigned_positions, group_unassigned,
    build_snapshot, detect_closures, portfolio_greeks, capital_allocation
)
from strategy_card import StrategyCard, pnl_color, money, fmt_num
from strategies_page import ConfigurePage
from strategy_detail import StrategyDetailPage


# ── Workers ──────────────────────────────────────────────────────────────────

class UpdateCheckWorker(QThread):
    done = pyqtSignal(dict)

    def run(self):
        self.done.emit(updater.check_latest())


class ConnectWorker(QThread):
    done = pyqtSignal(str, str)

    def __init__(self, creds):
        super().__init__()
        self.creds = creds

    def run(self):
        token, err = api.get_access_token(
            self.creds["refresh_token"], self.creds["secret_token"]
        )
        self.done.emit(token or "", err or "")


class PortfolioWorker(QThread):
    done = pyqtSignal(dict)

    def __init__(self, token):
        super().__init__()
        self.token = token

    def run(self):
        try:
            accounts_raw = api.list_accounts(self.token)
            accounts = []
            for acct in accounts_raw:
                num = acct.get("account-number", "")
                if not num:
                    continue
                balances  = api.get_balances(self.token, num)
                positions = [Position(p) for p in api.get_positions(self.token, num)]

                equity_opts = [p.symbol for p in positions
                                if p.is_option and p.instrument_type == "Equity Option"]
                future_opts = [p.symbol for p in positions
                                if p.is_option and p.instrument_type == "Future Option"]
                equities    = [p.symbol for p in positions
                                if not p.is_option and p.instrument_type == "Equity"]
                quotes = api.get_market_data(
                    self.token, equity_options=equity_opts,
                    future_options=future_opts, equities=equities,
                )
                for p in positions:
                    p.attach_quote(quotes.get(p.symbol))

                # Per-underlying metrics: IVR/IVP/beta/HV30
                roots = list({p.root for p in positions if p.root})
                metrics = api.get_market_metrics(self.token, roots)

                accounts.append({
                    "number":    num,
                    "nickname":  acct.get("nickname") or num,
                    "balances":  balances,
                    "positions": positions,
                    "metrics":   metrics,
                })
            self.done.emit({"accounts": accounts, "error": ""})
        except Exception as e:
            self.done.emit({"accounts": [], "error": str(e)})


# ── Setup screen ─────────────────────────────────────────────────────────────

class SetupScreen(QWidget):
    connected = pyqtSignal(dict, str)

    def __init__(self):
        super().__init__()
        self.setStyleSheet(T.BASE_STYLE)
        self._worker = None

        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        box = QFrame()
        box.setFixedWidth(440)
        box.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; border-radius: 16px; }}"
        )
        outer.addWidget(box)

        lay = QVBoxLayout(box)
        lay.setContentsMargins(48, 40, 48, 40)
        lay.setSpacing(0)

        title = QLabel("Options Dashboard")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 22px; font-weight: bold; border: none; background: transparent;"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)

        sub = QLabel("Connect your TastyTrade account")
        sub.setStyleSheet(
            f"color: {T.MUTED}; font-size: 13px; border: none; background: transparent;"
        )
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(sub)
        lay.addSpacing(24)

        self.fields = {}
        specs = [
            ("name",          "Your Name",                False, "e.g. Amit"),
            ("client_id",     "TastyTrade Client ID",     False, "Your OAuth client ID"),
            ("refresh_token", "TastyTrade Refresh Token", True,  "Long-lived refresh token"),
            ("secret_token",  "Secret Token",             True,  "Client secret"),
        ]
        for key, lbl_text, secret, ph in specs:
            lbl = QLabel(lbl_text)
            lbl.setStyleSheet(
                f"color: {T.LABEL}; font-size: 12px; font-weight: bold; border: none;"
                f" background: transparent; margin-top: 14px; margin-bottom: 4px;"
            )
            lay.addWidget(lbl)
            entry = QLineEdit()
            entry.setPlaceholderText(ph)
            if secret:
                entry.setEchoMode(QLineEdit.EchoMode.Password)
            lay.addWidget(entry)
            self.fields[key] = entry

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addSpacing(12)
        lay.addWidget(self.status_lbl)

        self.btn = QPushButton("Connect")
        self.btn.setFixedHeight(42)
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: white; border: none; "
            f"border-radius: 8px; font-size: 15px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
            f"QPushButton:disabled {{ background: #374151; color: #6b7280; }}"
        )
        self.btn.clicked.connect(self._connect)
        lay.addSpacing(16)
        lay.addWidget(self.btn)

    def prefill(self, creds):
        for k, entry in self.fields.items():
            entry.setText(creds.get(k, ""))

    def _connect(self):
        data = {k: v.text().strip() for k, v in self.fields.items()}
        if not all(data.values()):
            self._status("All fields are required.", T.RED)
            return
        self.btn.setEnabled(False)
        self.btn.setText("Connecting…")
        self.status_lbl.setText("")
        self._worker = ConnectWorker(data)
        self._worker.done.connect(lambda tok, err: self._on_done(data, tok, err))
        self._worker.start()

    def _on_done(self, data, token, error):
        self.btn.setEnabled(True)
        self.btn.setText("Connect")
        if error:
            msg = error
            if "invalid_grant" in error or "Invalid JWT" in error:
                msg = ("Invalid credentials — Refresh Token and Secret Token\n"
                       "must be from the same OAuth app on developer.tastytrade.com")
            self._status(msg, T.RED)
        else:
            api.save_credentials(data)
            self.connected.emit(data, token)

    def _status(self, text, color):
        self.status_lbl.setStyleSheet(
            f"color: {color}; font-size: 12px; border: none; background: transparent;"
        )
        self.status_lbl.setText(text)


# ── Account Settings dialog ──────────────────────────────────────────────────

class AccountSettingsDialog(QDialog):
    def __init__(self, accounts, overrides, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Account Settings")
        self.setStyleSheet(T.BASE_STYLE)
        self.setMinimumWidth(460)
        self._fields = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(14)

        title = QLabel("Rename accounts")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 16px; font-weight: bold; border: none;"
        )
        root.addWidget(title)

        hint = QLabel("Leave blank to use the default nickname from TastyTrade.")
        hint.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)
        for a in accounts:
            num = a["number"]
            default = a.get("nickname") or num
            try:
                nl = float(a.get("balances", {}).get("net-liquidating-value"))
                net_liq = f"${nl:,.2f}"
            except (TypeError, ValueError):
                net_liq = "—"
            lbl = QLabel(f"{default}  ·  {num}  ·  Net Liq {net_liq}")
            lbl.setStyleSheet(f"color: {T.LABEL}; font-size: 12px; border: none;")
            edit = QLineEdit(overrides.get(num, ""))
            edit.setPlaceholderText(default)
            self._fields[num] = edit
            form.addRow(lbl, edit)
        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def result_names(self):
        out = {}
        for num, edit in self._fields.items():
            txt = edit.text().strip()
            if txt:
                out[num] = txt
        return out


# ── Portfolio screen ─────────────────────────────────────────────────────────

class PortfolioScreen(QWidget):
    logout_requested    = pyqtSignal()
    configure_requested = pyqtSignal()
    strategy_clicked    = pyqtSignal(object)

    BALANCE_CARDS = [
        ("net-liquidating-value",   "Net Liq"),
        ("cash-balance",            "Cash"),
        ("derivative-buying-power", "Option BP"),
        ("equity-buying-power",     "Equity BP"),
    ]

    def __init__(self, creds, token):
        super().__init__()
        self.creds = creds
        self.token = token
        self._worker = None
        self._accounts = []

        self.strategies_all = api.load_strategies()   # {acct_num: [entries]}
        self.history_all    = api.load_history()      # {acct_num: [entries]}
        self.snapshots      = api.load_snapshots()
        self._account_names = api.load_account_names()
        self._settings      = api.load_settings()

        self.setStyleSheet(T.BASE_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header(creds))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body_w = QWidget()
        body_w.setStyleSheet(f"background: {T.BG};")
        self.body = QVBoxLayout(body_w)
        self.body.setContentsMargins(32, 24, 32, 32)
        self.body.setSpacing(14)
        scroll.setWidget(body_w)
        root.addWidget(scroll)

        self.body.addLayout(self._build_balance_row())
        self.body.addSpacing(4)

        self.greeks_header = self._section_header("Portfolio Greeks")
        self.body.addWidget(self.greeks_header)
        self.greeks_row = QHBoxLayout()
        self.greeks_row.setSpacing(12)
        self.body.addLayout(self.greeks_row)
        self.greek_tiles = {}
        for key, label in [("net_delta","Net Δ"), ("beta_weighted_delta","β-Wtd Δ (SPY)"),
                           ("net_theta","Net Θ"), ("net_vega","Net Vega")]:
            tile = self._make_tile(label)
            self.greek_tiles[key] = tile
            self.greeks_row.addWidget(tile["frame"])

        self.alloc_header = self._section_header("Capital by Ticker")
        self.body.addWidget(self.alloc_header)
        self.alloc_card = QFrame()
        self.alloc_card.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; border-radius: 12px; }}"
        )
        self.alloc_lay = QVBoxLayout(self.alloc_card)
        self.alloc_lay.setContentsMargins(18, 14, 18, 16)
        self.alloc_lay.setSpacing(6)
        self.body.addWidget(self.alloc_card)

        self.my_header  = self._section_header("My Strategies")
        self.body.addWidget(self.my_header)
        self.my_container = QVBoxLayout()
        self.my_container.setSpacing(10)
        self.body.addLayout(self.my_container)

        self.ua_header  = self._section_header("Unassigned Legs")
        self.body.addWidget(self.ua_header)
        self.ua_container = QVBoxLayout()
        self.ua_container.setSpacing(10)
        self.body.addLayout(self.ua_container)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 13px; border: none; background: transparent;"
        )
        self.status_lbl.setWordWrap(True)
        self.body.addWidget(self.status_lbl)
        self.body.addStretch()

        self._load_data()
        self._auto_update_check()

    # ── Header ──────────────────────────────────────────────────────────────

    def _build_header(self, creds):
        header = QFrame()
        header.setFixedHeight(60)
        header.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border-bottom: 1px solid {T.BORDER}; border-radius: 0; }}"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(28, 0, 28, 0)
        hl.setSpacing(16)

        title = QLabel("⬢  Options Dashboard")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 17px; font-weight: bold; border: none; background: transparent;"
        )
        hl.addWidget(title)

        welcome = QLabel(f"·  {creds.get('name', '')}")
        welcome.setStyleSheet(
            f"color: {T.MUTED}; font-size: 13px; border: none; background: transparent;"
        )
        hl.addWidget(welcome)
        hl.addStretch()

        self.account_combo = QComboBox()
        self.account_combo.setFixedWidth(200)
        self.account_combo.currentIndexChanged.connect(self._on_account_change)
        hl.addWidget(self.account_combo)

        configure_btn = QPushButton("⚙  Configure Account")
        configure_btn.setFixedHeight(32)
        configure_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        configure_btn.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: white; border: none; "
            f"border-radius: 6px; padding: 0 14px; font-size: 12px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
        )
        configure_btn.clicked.connect(self.configure_requested.emit)
        hl.addWidget(configure_btn)

        refresh_btn = QPushButton("↻  Refresh")
        refresh_btn.setFixedHeight(32)
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.clicked.connect(self._load_data)
        hl.addWidget(refresh_btn)

        self.live_btn = QPushButton("○  Live")
        self.live_btn.setFixedHeight(32)
        self.live_btn.setCheckable(True)
        self.live_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.live_btn.toggled.connect(self._toggle_live)
        self._style_live_btn(False)
        hl.addWidget(self.live_btn)

        self._live_timer = QTimer(self)
        self._live_timer.setInterval(15000)
        self._live_timer.timeout.connect(self._load_data)

        self.update_btn = QPushButton(f"v{VERSION}")
        self.update_btn.setFixedHeight(32)
        self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_btn.clicked.connect(self._check_for_update)
        self.update_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 0 10px; "
            f"font-size: 11px; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        hl.addWidget(self.update_btn)

        settings_btn = QPushButton("Settings")
        settings_btn.setFixedHeight(32)
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.clicked.connect(self._open_settings)
        hl.addWidget(settings_btn)

        logout_btn = QPushButton("Log out")
        logout_btn.setFixedHeight(32)
        logout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        logout_btn.clicked.connect(self._logout)
        logout_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 0 12px; "
            f"font-size: 12px; font-weight: normal; }}"
            f"QPushButton:hover {{ color: {T.TEXT}; border-color: {T.BORDER_H}; }}"
        )
        hl.addWidget(logout_btn)
        return header

    def _style_live_btn(self, on):
        if on:
            self.live_btn.setText("●  Live")
            self.live_btn.setStyleSheet(
                f"QPushButton {{ background: {T.GREEN_D}; color: white; border: none; "
                f"border-radius: 6px; padding: 0 10px; font-size: 11px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: {T.GREEN}; }}"
            )
        else:
            self.live_btn.setText("○  Live")
            self.live_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T.MUTED}; "
                f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 0 10px; "
                f"font-size: 11px; }}"
                f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
            )

    def _toggle_live(self, on):
        self._style_live_btn(on)
        if on:
            self._live_timer.start()
        else:
            self._live_timer.stop()

    def _make_tile(self, label):
        f = QFrame()
        f.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; border-radius: 12px; }}"
        )
        lay = QVBoxLayout(f)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(2)
        lbl = QLabel(label.upper())
        lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.7px; "
            f"border: none; background: transparent;"
        )
        val = QLabel("—")
        val.setStyleSheet(
            f"color: {T.TEXT}; font-size: 18px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        lay.addWidget(lbl); lay.addWidget(val)
        return {"frame": f, "value": val}

    def _section_header(self, text):
        l = QLabel(text.upper())
        l.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; font-weight: bold; letter-spacing: 0.8px; "
            f"border: none; background: transparent; padding: 8px 2px 4px 2px;"
        )
        return l

    def _build_balance_row(self):
        row = QHBoxLayout()
        row.setSpacing(12)
        self.bal_cards = {}
        for key, label in self.BALANCE_CARDS:
            w = QFrame()
            w.setStyleSheet(
                f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; "
                f"border-radius: 12px; }}"
            )
            lay = QVBoxLayout(w)
            lay.setContentsMargins(20, 14, 20, 16)
            lay.setSpacing(4)
            lbl = QLabel(label.upper())
            lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.7px; "
                f"border: none; background: transparent;"
            )
            val = QLabel("—")
            val.setStyleSheet(
                f"color: {T.TEXT}; font-size: 22px; font-weight: bold; "
                f"border: none; background: transparent;"
            )
            lay.addWidget(lbl); lay.addWidget(val)
            self.bal_cards[key] = val
            row.addWidget(w)

        cap_card = QFrame()
        cap_card.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; border-radius: 12px; }}"
        )
        clay = QVBoxLayout(cap_card)
        clay.setContentsMargins(20, 14, 20, 16)
        clay.setSpacing(4)
        clbl = QLabel("CAPITAL USED")
        clbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.7px; "
            f"border: none; background: transparent;"
        )
        self.cap_used_lbl = QLabel("—")
        self.cap_used_lbl.setStyleSheet(
            f"color: {T.TEXT}; font-size: 22px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        clay.addWidget(clbl); clay.addWidget(self.cap_used_lbl)
        row.addWidget(cap_card)

        pnl_card = QFrame()
        pnl_card.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; border-radius: 12px; }}"
        )
        lay = QVBoxLayout(pnl_card)
        lay.setContentsMargins(20, 14, 20, 16)
        lay.setSpacing(4)
        lbl = QLabel("OPEN P&L")
        lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 10px; font-weight: bold; letter-spacing: 0.7px; "
            f"border: none; background: transparent;"
        )
        self.pnl_total_lbl = QLabel("—")
        self.pnl_total_lbl.setStyleSheet(
            f"color: {T.TEXT}; font-size: 22px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        lay.addWidget(lbl); lay.addWidget(self.pnl_total_lbl)
        row.addWidget(pnl_card)

        return row

    # ── Data loading / close detection ──────────────────────────────────────

    def _load_data(self):
        self.status_lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 13px; border: none; background: transparent;"
        )
        self.status_lbl.setText("Loading portfolio…")
        self._worker = PortfolioWorker(self.token)
        self._worker.done.connect(self._on_data)
        self._worker.start()

    def _on_data(self, result):
        if result.get("error"):
            self.status_lbl.setStyleSheet(
                f"color: {T.RED}; font-size: 13px; border: none; background: transparent;"
            )
            self.status_lbl.setText(result["error"])
            return
        self.status_lbl.setText("")
        self._accounts = result.get("accounts", [])

        # Detect closures + update snapshots for every account
        self._process_snapshots()

        self._refresh_account_combo()
        if self._accounts:
            idx = max(0, self.account_combo.currentIndex())
            self._render(self._accounts[idx])

    def _process_snapshots(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        history_changed = False
        changed = False
        for acct in self._accounts:
            num = acct["number"]
            strategy_map = {}
            for s in self.strategies_all.get(num, []):
                for sym in s.get("legs", []):
                    strategy_map[sym] = s["id"]
            prev = self.snapshots.get(num, {})
            curr_syms = {p.symbol for p in acct["positions"]}
            closures = detect_closures(prev, curr_syms, strategy_map, now_iso)
            if closures:
                self.history_all.setdefault(num, []).extend(closures)
                history_changed = True
            self.snapshots[num] = build_snapshot(acct["positions"])
            changed = True

        if history_changed:
            api.save_history(self.history_all)
        if changed:
            api.save_snapshots(self.snapshots)

    # ── Per-account strategies / history accessors ──────────────────────────

    @property
    def strategies_raw(self):
        acct = self.current_account()
        if not acct:
            return []
        return self.strategies_all.setdefault(acct["number"], [])

    @strategies_raw.setter
    def strategies_raw(self, value):
        acct = self.current_account()
        if acct:
            self.strategies_all[acct["number"]] = value

    @property
    def history(self):
        acct = self.current_account()
        if not acct:
            return []
        return self.history_all.setdefault(acct["number"], [])

    @history.setter
    def history(self, value):
        acct = self.current_account()
        if acct:
            self.history_all[acct["number"]] = value

    def save_strategies(self):
        api.save_strategies(self.strategies_all)

    def save_history(self):
        api.save_history(self.history_all)

    def _display_name(self, account):
        num = account["number"]
        return self._account_names.get(num) or account.get("nickname") or num

    def _refresh_account_combo(self):
        prev_num = self.account_combo.currentData()
        if prev_num is None:
            prev_num = self._settings.get("selected_account")
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        target_idx = 0
        for i, a in enumerate(self._accounts):
            self.account_combo.addItem(self._display_name(a), a["number"])
            if a["number"] == prev_num:
                target_idx = i
        if self.account_combo.count():
            self.account_combo.setCurrentIndex(target_idx)
        self.account_combo.blockSignals(False)

    def _on_account_change(self, idx):
        if 0 <= idx < len(self._accounts):
            self._settings["selected_account"] = self._accounts[idx]["number"]
            api.save_settings(self._settings)
            self._render(self._accounts[idx])

    def current_account(self):
        idx = self.account_combo.currentIndex()
        if 0 <= idx < len(self._accounts):
            return self._accounts[idx]
        return None

    def current_positions(self):
        acct = self.current_account()
        return acct["positions"] if acct else []

    def current_instances(self):
        positions = self.current_positions()
        return [StrategyInstance(d, positions) for d in self.strategies_raw]

    def reload_after_config_change(self):
        acct = self.current_account()
        if acct:
            self._render(acct)

    def _render(self, acct):
        bal = acct.get("balances", {})
        for key, widget in self.bal_cards.items():
            raw = bal.get(key)
            try:
                widget.setText(f"${float(raw):,.2f}")
            except (ValueError, TypeError):
                widget.setText("—")

        # Capital used = maintenance requirement / net liq
        try:
            maint   = float(bal.get("maintenance-requirement") or 0)
            net_liq = float(bal.get("net-liquidating-value") or 0)
            if net_liq > 0:
                pct = (maint / net_liq) * 100.0
                self.cap_used_lbl.setText(f"{pct:.1f}%")
                color = T.RED if pct >= 80 else (T.YELLOW if pct >= 50 else T.TEXT)
                self.cap_used_lbl.setStyleSheet(
                    f"color: {color}; font-size: 22px; font-weight: bold; "
                    f"border: none; background: transparent;"
                )
            else:
                self.cap_used_lbl.setText("—")
        except (ValueError, TypeError):
            self.cap_used_lbl.setText("—")

        self._clear_layout(self.my_container)
        self._clear_layout(self.ua_container)

        positions = acct["positions"]
        metrics   = acct.get("metrics") or {}
        instances = [StrategyInstance(d, positions) for d in self.strategies_raw]
        leftover  = unassigned_positions(positions, self.strategies_raw)
        unassigned = group_unassigned(leftover)

        self._render_greeks(positions, metrics)
        self._render_allocation(instances, unassigned)

        total_pnl = sum(i.pnl for i in instances) + sum(s.pnl for s in unassigned)
        self.pnl_total_lbl.setText(money(total_pnl, signed=True))
        self.pnl_total_lbl.setStyleSheet(
            f"color: {pnl_color(total_pnl)}; font-size: 22px; font-weight: bold; "
            f"border: none; background: transparent;"
        )

        if not instances:
            empty = QLabel("No strategies configured — click “Configure Account” in the header.")
            empty.setStyleSheet(
                f"color: {T.MUTED}; font-size: 13px; padding: 22px; border: 1px dashed "
                f"{T.BORDER}; border-radius: 10px; background: {T.CARD};"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.my_container.addWidget(empty)
        else:
            for inst in instances:
                card = StrategyCard(inst, metrics=metrics)
                card.clicked.connect(self.strategy_clicked.emit)
                self.my_container.addWidget(card)

        if not unassigned:
            empty = QLabel("All legs are assigned to strategies.")
            empty.setStyleSheet(
                f"color: {T.MUTED}; font-size: 12px; padding: 16px; border: 1px dashed "
                f"{T.BORDER}; border-radius: 10px; background: {T.CARD};"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.ua_container.addWidget(empty)
        else:
            for strat in unassigned:
                card = StrategyCard(strat, metrics=metrics)
                card.clicked.connect(self.strategy_clicked.emit)
                self.ua_container.addWidget(card)

    def _render_greeks(self, positions, metrics_by_root):
        g = portfolio_greeks(positions, metrics_by_root)
        fields = [
            ("net_delta",            g["net_delta"]),
            ("beta_weighted_delta",  g["beta_weighted_delta"]),
            ("net_theta",            g["net_theta"]),
            ("net_vega",             g["net_vega"]),
        ]
        for key, val in fields:
            tile = self.greek_tiles[key]
            if val is None:
                tile["value"].setText("—")
                tile["value"].setStyleSheet(
                    f"color: {T.MUTED}; font-size: 18px; font-weight: bold; "
                    f"border: none; background: transparent;"
                )
                continue
            color = pnl_color(val) if key in ("net_delta","beta_weighted_delta","net_theta") else T.TEXT
            sign = "+" if val >= 0 else "−"
            if abs(val) >= 100:
                text = f"{sign}{abs(val):,.0f}"
            else:
                text = f"{sign}{abs(val):.2f}"
            tile["value"].setText(text)
            tile["value"].setStyleSheet(
                f"color: {color}; font-size: 18px; font-weight: bold; "
                f"border: none; background: transparent;"
            )

    def _render_allocation(self, instances, unassigned):
        self._clear_layout(self.alloc_lay)
        overrides = {r["id"]: r["capital_override"]
                     for r in self.strategies_raw
                     if r.get("capital_override") is not None}
        rows, total = capital_allocation(instances, unassigned, overrides)

        if not rows or total <= 0:
            empty = QLabel("No capital allocated.")
            empty.setStyleSheet(
                f"color: {T.MUTED}; font-size: 12px; border: none; background: transparent;"
            )
            self.alloc_lay.addWidget(empty)
            return

        header = QLabel(f"Total deployed: {money(total)}")
        header.setStyleSheet(
            f"color: {T.TEXT_DIM}; font-size: 12px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        self.alloc_lay.addWidget(header)

        top_rows = rows[:8]
        for r in top_rows:
            row_w = QHBoxLayout()
            row_w.setSpacing(10)
            name = QLabel(r["root"])
            name.setFixedWidth(70)
            name.setStyleSheet(
                f"color: {T.TEXT}; font-size: 12px; font-weight: bold; "
                f"border: none; background: transparent;"
            )
            row_w.addWidget(name)

            bar_outer = QFrame()
            bar_outer.setFixedHeight(12)
            bar_outer.setStyleSheet(
                f"QFrame {{ background: #12151d; border: 1px solid {T.BORDER}; "
                f"border-radius: 6px; }}"
            )
            bar_lay = QHBoxLayout(bar_outer)
            bar_lay.setContentsMargins(0, 0, 0, 0)
            bar_lay.setSpacing(0)
            fill = QFrame()
            fill_color = T.RED if r["pct"] >= 40 else (T.YELLOW if r["pct"] >= 20 else T.PURPLE)
            fill.setStyleSheet(
                f"QFrame {{ background: {fill_color}; border: none; border-radius: 5px; }}"
            )
            stretch_fill = int(max(1, round(r["pct"] * 10)))
            stretch_rest = int(max(1, round((100 - r["pct"]) * 10)))
            bar_lay.addWidget(fill, stretch_fill)
            spacer = QWidget()
            spacer.setStyleSheet("background: transparent;")
            bar_lay.addWidget(spacer, stretch_rest)
            row_w.addWidget(bar_outer, 1)

            pct_lbl = QLabel(f"{r['pct']:.1f}%")
            pct_lbl.setFixedWidth(55)
            pct_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            pct_lbl.setStyleSheet(
                f"color: {T.TEXT_DIM}; font-size: 12px; "
                f"border: none; background: transparent;"
            )
            row_w.addWidget(pct_lbl)

            cap_lbl = QLabel(money(r["capital"]))
            cap_lbl.setFixedWidth(90)
            cap_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            cap_lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 11px; "
                f"border: none; background: transparent;"
            )
            row_w.addWidget(cap_lbl)

            self.alloc_lay.addLayout(row_w)

        if len(rows) > 8:
            more = QLabel(f"+ {len(rows) - 8} more tickers")
            more.setStyleSheet(
                f"color: {T.MUTED}; font-size: 11px; border: none; background: transparent;"
            )
            self.alloc_lay.addWidget(more)

    def _clear_layout(self, lay):
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    # ── Update checks ───────────────────────────────────────────────────────

    def _auto_update_check(self):
        self._update_worker = UpdateCheckWorker()
        self._update_worker.done.connect(
            lambda r: self._on_update_result(r, silent=True)
        )
        self._update_worker.start()

    def _check_for_update(self):
        self.update_btn.setText("Checking…")
        self.update_btn.setEnabled(False)
        self._update_worker = UpdateCheckWorker()
        self._update_worker.done.connect(
            lambda r: self._on_update_result(r, silent=False)
        )
        self._update_worker.start()

    def _on_update_result(self, result, silent):
        self.update_btn.setEnabled(True)
        if result.get("available"):
            self.update_btn.setText("⬇  Update")
            self.update_btn.setStyleSheet(
                f"QPushButton {{ background: {T.PURPLE}; color: white; "
                f"border: none; border-radius: 6px; padding: 0 10px; "
                f"font-size: 11px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
            )
            self._show_update_dialog(result)
        else:
            self.update_btn.setText(f"v{VERSION}")
            if silent:
                return
            if result.get("error"):
                QMessageBox.warning(self, "Update check failed", result["error"])
            else:
                QMessageBox.information(
                    self, "Up to date",
                    f"You're on the latest version (v{VERSION})."
                )

    def _show_update_dialog(self, result):
        dlg = QDialog(self)
        dlg.setWindowTitle("Update Available")
        dlg.setStyleSheet(T.BASE_STYLE)
        dlg.setMinimumSize(500, 380)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(10)

        hdr = QLabel("New update available")
        hdr.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 16px; font-weight: bold; border: none;"
        )
        lay.addWidget(hdr)

        sub = QLabel(
            f"You're on {result.get('local') or VERSION} — "
            f"latest is {result.get('latest') or '?'}."
        )
        sub.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        lay.addWidget(sub)

        notes_lbl = QLabel("Changes:")
        notes_lbl.setStyleSheet(
            f"color: {T.LABEL}; font-size: 11px; font-weight: bold; "
            f"border: none; margin-top: 6px;"
        )
        lay.addWidget(notes_lbl)

        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(result.get("notes") or "(no release notes)")
        notes.setStyleSheet(
            f"QTextEdit {{ background: #12151d; color: {T.TEXT_DIM}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 8px; "
            f"font-size: 12px; }}"
        )
        lay.addWidget(notes, 1)

        row = QHBoxLayout()
        later = QPushButton("Later")
        later.setCursor(Qt.CursorShape.PointingHandCursor)
        later.setFixedHeight(32)
        later.clicked.connect(dlg.reject)
        row.addWidget(later)
        row.addStretch()
        download = QPushButton("⬇  Download")
        download.setCursor(Qt.CursorShape.PointingHandCursor)
        download.setFixedHeight(32)
        download.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: white; border: none; "
            f"border-radius: 6px; padding: 0 16px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
        )
        download.setText("⬇  Update now")
        def _go():
            download.setEnabled(False)
            download.setText("Updating…")
            ok, msg = updater.pull()
            if not ok:
                QMessageBox.warning(dlg, "Update failed", msg)
                download.setEnabled(True)
                download.setText("⬇  Update now")
                return
            QMessageBox.information(
                dlg, "Update installed",
                "The app will now relaunch with the new version.",
            )
            dlg.accept()
            self._relaunch()
        download.clicked.connect(_go)
        row.addWidget(download)
        lay.addLayout(row)
        dlg.exec()

    def _relaunch(self):
        import os, sys
        here = os.path.dirname(os.path.abspath(__file__))
        python = sys.executable
        os.execv(python, [python, os.path.join(here, "app.py")])

    def _open_settings(self):
        if not self._accounts:
            return
        dlg = AccountSettingsDialog(self._accounts, self._account_names, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._account_names = dlg.result_names()
            api.save_account_names(self._account_names)
            self._refresh_account_combo()

    def _logout(self):
        api.clear_credentials()
        self.logout_requested.emit()


# ── Main window ──────────────────────────────────────────────────────────────

class MainWindow(QStackedWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Options Dashboard")
        self.resize(1420, 860)
        self.setMinimumSize(1000, 660)
        self.setStyleSheet(f"background: {T.BG};")
        self.portfolio = None
        self.configure = None
        self.detail    = None
        self._show_initial()

    def _show_initial(self):
        creds = api.load_credentials()
        if creds:
            token, err = api.get_access_token(
                creds["refresh_token"], creds["secret_token"]
            )
            if token:
                self._show_portfolio(creds, token)
            else:
                self._show_setup(creds)
        else:
            self._show_setup()

    def _show_setup(self, prefill=None):
        self._clear_all()
        screen = SetupScreen()
        if prefill:
            screen.prefill(prefill)
        screen.connected.connect(lambda creds, tok: self._show_portfolio(creds, tok))
        self.addWidget(screen)
        self.setCurrentWidget(screen)

    def _show_portfolio(self, creds, token):
        self._clear_all()
        self.portfolio = PortfolioScreen(creds, token)
        self.portfolio.logout_requested.connect(self._show_setup)
        self.portfolio.configure_requested.connect(self._show_configure)
        self.portfolio.strategy_clicked.connect(self._show_detail)
        self.addWidget(self.portfolio)
        self.setCurrentWidget(self.portfolio)

    def _show_configure(self):
        if self.portfolio is None:
            return
        self.configure = ConfigurePage(self.portfolio)
        self.configure.back_requested.connect(self._back_from_configure)
        self.configure.strategies_changed.connect(self.portfolio.reload_after_config_change)
        self.addWidget(self.configure)
        self.setCurrentWidget(self.configure)

    def _back_from_configure(self):
        if self.portfolio:
            self.setCurrentWidget(self.portfolio)
            self.portfolio.reload_after_config_change()
        if self.configure:
            self.removeWidget(self.configure)
            self.configure.deleteLater()
            self.configure = None

    def _show_detail(self, strategy):
        if self.portfolio is None:
            return
        self.detail = StrategyDetailPage(strategy, self.portfolio)
        self.detail.back_requested.connect(self._back_from_detail)
        self.detail.reopen_requested.connect(self._reopen_detail)
        self.addWidget(self.detail)
        self.setCurrentWidget(self.detail)

    def _reopen_detail(self, strategy):
        """Tear down current detail + re-open for same strategy (picks up edits)."""
        if self.detail:
            self.removeWidget(self.detail)
            self.detail.deleteLater()
            self.detail = None
        # Pull a fresh instance from the portfolio (reflects any saved edits)
        fresh = next(
            (i for i in self.portfolio.current_instances() if i.id == getattr(strategy, "id", None)),
            strategy,
        )
        self._show_detail(fresh)

    def _back_from_detail(self):
        if self.portfolio:
            self.setCurrentWidget(self.portfolio)
        if self.detail:
            self.removeWidget(self.detail)
            self.detail.deleteLater()
            self.detail = None

    def _clear_all(self):
        while self.count():
            w = self.widget(0)
            self.removeWidget(w)
            w.deleteLater()
        self.portfolio = None
        self.configure = None
        self.detail    = None


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
