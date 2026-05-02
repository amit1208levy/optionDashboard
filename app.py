"""Options Dashboard — setup + portfolio + configure screens."""
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from PyQt6.QtWidgets import (
    QApplication, QWidget, QStackedWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFrame, QScrollArea, QComboBox,
    QDialog, QDialogButtonBox, QFormLayout, QMessageBox, QTextEdit,
    QCheckBox,
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal

import theme as T
import api
import updater
import streamer as _streamer_mod
import pnl as _pnl_mod
from quotes_tasty import TastyQuotesProvider
from quotes_hybrid import HybridQuotesProvider
from version import VERSION
from models import (
    Position, StrategyInstance, unassigned_positions, group_unassigned,
    build_snapshot, detect_closures, portfolio_greeks, repair_history_pnl,
    repair_pnl_missing_multiplier, check_exit_conditions, probability_of_profit,
)
from strategy_card import StrategyCard, pnl_color, money, fmt_num
from strategies_page import ConfigurePage
from strategy_detail import StrategyDetailPage
from watchlist_page import WatchlistPage
from risk_page import RiskPage


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

    # YTD transaction TTL: don't re-fetch within the same hour.
    _YTD_TTL_SECONDS = 3600

    def __init__(self, token, creds=None, ytd_cache=None, year_start_cache=None,
                 pnl_cache=None, quotes=None, ibkr_provider=None,
                 ibkr_data_source_only=True):
        super().__init__()
        self.token      = token
        self.creds      = creds
        # Quotes provider — when None we fall back to direct api.get_market_data
        # (so this class still works in legacy contexts without the provider).
        self.quotes     = quotes
        # IBKR account provider — when set AND data_source_only is False,
        # the worker appends an IBKR account entry to the results.
        self._ibkr_provider         = ibkr_provider
        self._ibkr_data_source_only = ibkr_data_source_only
        # ytd_cache is a mutable dict shared with PortfolioScreen so it
        # survives across worker instances (i.e. across live-mode refreshes).
        self._ytd_cache        = ytd_cache        if ytd_cache        is not None else {}
        # year_start_cache holds Jan-1 NetLiq per account — doesn't change
        # during a session so we fetch it once and reuse forever.
        self._year_start_cache = year_start_cache if year_start_cache is not None else {}
        # pnl_cache holds compute_ytd_pnl results with a 60s TTL — avoids
        # hammering /net-liq/history (which rate-limits at 429) every refresh.
        self._pnl_cache        = pnl_cache        if pnl_cache        is not None else {}

    # ── Per-account helpers ──────────────────────────────────────────────────

    def _ytd_txns(self, num):
        """Return YTD transactions from cache if fresh, else fetch and cache."""
        entry = self._ytd_cache.get(num, {})
        try:
            age = (datetime.now(timezone.utc) -
                   datetime.fromisoformat(entry["fetched_at"])).total_seconds()
            if age < self._YTD_TTL_SECONDS:
                return entry["transactions"]
        except (KeyError, ValueError):
            pass
        txns = api.get_transactions_ytd(self.token, num)
        self._ytd_cache[num] = {
            "fetched_at":   datetime.now(timezone.utc).isoformat(),
            "transactions": txns,
        }
        return txns

    def _year_start_nl(self, num):
        """Return Jan-1 NetLiq for this account, cached indefinitely."""
        if num in self._year_start_cache:
            return self._year_start_cache[num]
        val = api.get_year_start_net_liq(self.token, num)
        self._year_start_cache[num] = val
        return val

    _PNL_TTL_SECONDS = 60   # cache YTD result for 1 minute

    def _pnl(self, num):
        """Return cached compute_ytd_pnl result if fresh; refetch otherwise."""
        entry = self._pnl_cache.get(num)
        if entry:
            ts, result = entry
            try:
                age = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(ts)).total_seconds()
                if age < self._PNL_TTL_SECONDS:
                    return result
            except ValueError:
                pass
        result = _pnl_mod.compute_ytd_pnl(self.token, num)
        if result is not None:
            self._pnl_cache[num] = (
                datetime.now(timezone.utc).isoformat(), result
            )
        return result

    def _fetch_one(self, acct):
        """
        Fetch all data for one account using two parallel phases:
          Phase 1 (independent): balances + positions + YTD transactions
          Phase 2 (needs positions): market quotes + market metrics
        Returns a data dict, or None if the account has no number.
        """
        num = acct.get("account-number", "")
        if not num:
            return None
        try:
            # ── Phase 1: five independent calls in parallel ──────────────
            with ThreadPoolExecutor(max_workers=5) as ex:
                f_bal = ex.submit(api.get_balances,   self.token, num)
                f_pos = ex.submit(api.get_positions,  self.token, num)
                f_ytd = ex.submit(self._ytd_txns,     num)
                f_ny  = ex.submit(self._year_start_nl, num)
                f_pnl = ex.submit(self._pnl, num)
                balances      = f_bal.result()
                positions_raw = f_pos.result()
                ytd_txns      = f_ytd.result()
                year_start_nl = f_ny.result()
                ytd_pnl       = f_pnl.result()
            # Debug: log SDK path success/failure to stderr
            print(f"[pnl] account {num}: SDK returned {'OK' if ytd_pnl else 'NONE (using fallback)'}",
                  file=sys.stderr, flush=True)

            positions = [Position(p) for p in positions_raw]

            # ── Phase 2: quotes + metrics in parallel (both need positions) ──
            eq_opts = [p.symbol for p in positions
                       if p.is_option and p.instrument_type == "Equity Option"]
            fu_opts = [p.symbol for p in positions
                       if p.is_option and p.instrument_type == "Future Option"]
            equities = [p.symbol for p in positions
                        if not p.is_option and not p.is_future
                        and p.instrument_type == "Equity"]
            # Pure futures (not options) need quotes via the futures= param,
            # not equities= — the API distinguishes by symbol type.
            futures = [p.symbol for p in positions
                       if not p.is_option and p.is_future]

            # Futures need "/" prefix on /market-metrics queries; equities
            # don't.  Determine which roots are futures by checking whether
            # the original underlying-symbol starts with "/".
            fut_roots = {p.root for p in positions
                         if p.root and (p.underlying or "").startswith("/")}
            eq_roots  = {p.root for p in positions
                         if p.root and not (p.underlying or "").startswith("/")}
            metric_syms = list(eq_roots) + [f"/{r}" for r in fut_roots]

            # Live quotes go through the QuotesProvider abstraction so we can
            # swap vendors (Schwab, etc.) without touching the worker.  Falls
            # back to a direct TastyTrade fetch if no provider was passed in
            # (legacy / testing path).
            def _quotes_call():
                if self.quotes is not None:
                    return self.quotes.get_quotes(
                        equity_options=eq_opts,
                        future_options=fu_opts,
                        equities=equities,
                        futures=futures,
                    )
                return api.get_market_data(
                    self.token,
                    equity_options=eq_opts,
                    future_options=fu_opts,
                    equities=equities,
                    futures=futures,
                )

            with ThreadPoolExecutor(max_workers=2) as ex:
                f_quotes  = ex.submit(_quotes_call)
                f_metrics = ex.submit(api.get_market_metrics, self.token, metric_syms)
                quotes  = f_quotes.result()
                metrics = f_metrics.result()

            for p in positions:
                p.attach_quote(quotes.get(p.symbol))

            return {
                "number":             num,
                "nickname":           acct.get("nickname") or num,
                "balances":           balances,
                "positions":          positions,
                "metrics":            metrics,
                "ytd_txns":           ytd_txns,
                "year_start_net_liq": year_start_nl,
                "ytd_pnl_sdk":        ytd_pnl,   # may be None if SDK call failed
            }
        except Exception:
            return None   # account-level error: skip this account gracefully

    # ── Main run ─────────────────────────────────────────────────────────────

    def run(self):
        new_token = None
        for attempt in range(2):   # attempt 0 = normal; attempt 1 = after token refresh
            try:
                accounts_raw = [a for a in api.list_accounts(self.token)
                                if a.get("account-number")]

                # Fetch TastyTrade accounts + (optionally) IBKR account in parallel.
                workers = max(len(accounts_raw), 1)
                if self._ibkr_provider and not self._ibkr_data_source_only:
                    workers += 1

                with ThreadPoolExecutor(max_workers=workers) as ex:
                    tt_futures = [ex.submit(self._fetch_one, a) for a in accounts_raw]
                    ibkr_fut = None
                    if self._ibkr_provider and not self._ibkr_data_source_only:
                        from ibkr_account import fetch_ibkr_account
                        ibkr_fut = ex.submit(fetch_ibkr_account, self._ibkr_provider)
                    results = [f.result() for f in tt_futures]

                accounts = [r for r in results if r is not None]

                # Append IBKR account at the end of the list if available.
                if ibkr_fut is not None:
                    try:
                        ibkr_acct = ibkr_fut.result()
                        if ibkr_acct:
                            # Refresh Greeks via the live quotes provider so the
                            # IBKR account's position cards show full Greek rows.
                            positions = ibkr_acct.get("positions", [])
                            if positions and self.quotes is not None:
                                eq_opts = [p.symbol for p in positions
                                           if p.is_option and not p.is_future]
                                fu_opts = [p.symbol for p in positions
                                           if p.is_option and p.is_future]
                                equities = [p.symbol for p in positions
                                            if not p.is_option and not p.is_future]
                                futures  = [p.symbol for p in positions
                                            if not p.is_option and p.is_future]
                                try:
                                    quotes = self.quotes.get_quotes(
                                        equity_options=eq_opts,
                                        future_options=fu_opts,
                                        equities=equities,
                                        futures=futures,
                                    )
                                    for p in positions:
                                        q = quotes.get(p.symbol)
                                        if q:
                                            p.attach_quote(q)
                                except Exception as qe:
                                    print(f"[ibkr_account] quote refresh: {qe}", flush=True)
                            accounts.append(ibkr_acct)
                    except Exception as e:
                        print(f"[ibkr_account] fetch failed: {e}", flush=True)

                self.done.emit({"accounts": accounts, "error": "", "new_token": new_token})
                return

            except Exception as e:
                err = str(e)
                # 401 = access token expired → refresh once and retry
                if ("401" in err or "Unauthorized" in err) and self.creds and attempt == 0:
                    tok, refresh_err = api.get_access_token(
                        self.creds["refresh_token"], self.creds["secret_token"]
                    )
                    if tok:
                        self.token = tok
                        new_token  = tok
                        continue
                    self.done.emit({
                        "accounts":  [],
                        "error":     f"Session expired — re-auth failed: {refresh_err}",
                        "new_token": None,
                    })
                    return
                self.done.emit({"accounts": [], "error": err, "new_token": None})


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
    # All optional Greek columns that can appear in the legs table
    LEG_GREEK_OPTIONS = [
        ("delta", "Δ  Delta"),
        ("theta", "Θ  Theta"),
        ("gamma", "Γ  Gamma"),
        ("vega",  "V  Vega"),
    ]

    def __init__(self, accounts, overrides, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setStyleSheet(T.BASE_STYLE)
        self.setMinimumWidth(500)
        self._fields = {}
        self._greek_checks = {}
        self._ibkr_widgets: dict = {}
        # Strip any legacy "iv" entries that may have been saved by older versions
        enabled_greeks = [k for k in settings.get("leg_greeks", ["delta", "theta"])
                          if k != "iv"]
        ibkr_cfg = settings.get("ibkr") or {}

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(18)

        # ── Account names ────────────────────────────────────────────────────
        acct_title = QLabel("Rename accounts")
        acct_title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 15px; font-weight: bold; border: none;"
        )
        root.addWidget(acct_title)

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

        # ── Divider ──────────────────────────────────────────────────────────
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"color: {T.BORDER};")
        root.addWidget(div)

        # ── Leg column visibility ────────────────────────────────────────────
        col_title = QLabel("Leg column visibility")
        col_title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 15px; font-weight: bold; border: none;"
        )
        root.addWidget(col_title)

        col_hint = QLabel(
            "Choose which Greek columns appear in the Legs table on every strategy."
        )
        col_hint.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        col_hint.setWordWrap(True)
        root.addWidget(col_hint)

        checks_row = QHBoxLayout()
        checks_row.setSpacing(18)
        for key, label in self.LEG_GREEK_OPTIONS:
            cb = QCheckBox(label)
            cb.setChecked(key in enabled_greeks)
            cb.setStyleSheet(
                f"QCheckBox {{ color: {T.TEXT}; font-size: 13px; border: none; }}"
                f"QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; "
                f"border: 1px solid {T.BORDER}; background: {T.BG_ALT}; }}"
                f"QCheckBox::indicator:checked {{ background: {T.ACCENT}; border-color: {T.ACCENT}; }}"
            )
            self._greek_checks[key] = cb
            checks_row.addWidget(cb)
        checks_row.addStretch()
        root.addLayout(checks_row)

        # ── Divider ──────────────────────────────────────────────────────────
        div2 = QFrame()
        div2.setFrameShape(QFrame.Shape.HLine)
        div2.setStyleSheet(f"color: {T.BORDER};")
        root.addWidget(div2)

        # ── IBKR Gateway settings ────────────────────────────────────────────
        ibkr_title = QLabel("IBKR Gateway (live quotes)")
        ibkr_title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 15px; font-weight: bold; border: none;"
        )
        root.addWidget(ibkr_title)

        ibkr_hint = QLabel(
            "Gateway is auto-detected on startup — no configuration needed. "
            "If Gateway is running on this machine, it is used automatically "
            "for real-time quotes / Greeks. TastyTrade fills in any symbols "
            "Gateway doesn't cover and handles all non-quote data."
        )
        ibkr_hint.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        ibkr_hint.setWordWrap(True)
        root.addWidget(ibkr_hint)

        ibkr_enable_cb = QCheckBox("Use IBKR Gateway for live quotes (auto-detected)")
        # Checked by default; only unchecked when the user explicitly opts out.
        ibkr_enable_cb.setChecked(ibkr_cfg.get("enabled") is not False)
        _cb_style = (
            f"QCheckBox {{ color: {T.TEXT}; font-size: 13px; border: none; }}"
            f"QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; "
            f"border: 1px solid {T.BORDER}; background: {T.BG_ALT}; }}"
            f"QCheckBox::indicator:checked {{ background: {T.ACCENT}; "
            f"border-color: {T.ACCENT}; }}"
        )
        ibkr_enable_cb.setStyleSheet(_cb_style)
        self._ibkr_widgets["enabled"] = ibkr_enable_cb
        root.addWidget(ibkr_enable_cb)

        data_only_cb = QCheckBox("Data source only (don't show IBKR as an account)")
        data_only_cb.setChecked(bool(ibkr_cfg.get("data_source_only")))
        data_only_cb.setStyleSheet(_cb_style)
        data_only_cb.setToolTip(
            "When unchecked (default): IBKR Gateway appears as a second account\n"
            "in the account list — you can view its positions, Greeks, and P&L.\n\n"
            "When checked: Gateway is used only to supply live quotes / Greeks to\n"
            "your TastyTrade positions; no IBKR account row is shown."
        )
        self._ibkr_widgets["data_source_only"] = data_only_cb
        root.addWidget(data_only_cb)

        ibkr_form = QFormLayout()
        ibkr_form.setSpacing(8)

        host_edit = QLineEdit(str(ibkr_cfg.get("host") or "127.0.0.1"))
        host_edit.setStyleSheet(
            f"background: {T.BG_ALT}; border: 1px solid {T.BORDER}; "
            f"border-radius: 6px; padding: 6px 8px; color: {T.TEXT};"
        )
        self._ibkr_widgets["host"] = host_edit
        ibkr_form.addRow("Host:", host_edit)

        port_edit = QLineEdit(str(ibkr_cfg.get("port") or ""))
        port_edit.setPlaceholderText("auto (4001 → 4002 → 7496 → 7497)")
        port_edit.setStyleSheet(host_edit.styleSheet())
        port_edit.setToolTip("Leave blank to auto-detect. Or pin a specific port: "
                             "Gateway live = 4001, Gateway paper = 4002, "
                             "TWS live = 7496, TWS paper = 7497")
        self._ibkr_widgets["port"] = port_edit
        ibkr_form.addRow("Port:", port_edit)

        cid_edit = QLineEdit(str(ibkr_cfg.get("client_id") or 42))
        cid_edit.setStyleSheet(host_edit.styleSheet())
        cid_edit.setToolTip("Any unused integer; only matters when multiple "
                            "API clients connect to the same Gateway.")
        self._ibkr_widgets["client_id"] = cid_edit
        ibkr_form.addRow("Client ID:", cid_edit)

        root.addLayout(ibkr_form)

        # ── Buttons ──────────────────────────────────────────────────────────
        notice = QLabel("Changes to IBKR settings take effect on next app launch.")
        notice.setStyleSheet(f"color: {T.MUTED}; font-size: 11px; border: none;")
        notice.setWordWrap(True)
        root.addWidget(notice)

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

    def result_leg_greeks(self):
        """Return list of enabled Greek column keys in canonical order."""
        order = [key for key, _ in self.LEG_GREEK_OPTIONS]
        return [key for key in order if self._greek_checks[key].isChecked()]

    def result_ibkr_settings(self) -> dict:
        """Return the user's IBKR Gateway settings as a JSON-serializable dict."""
        def _int(field, default):
            try:
                return int(self._ibkr_widgets[field].text().strip())
            except (ValueError, AttributeError):
                return default
        raw_port = self._ibkr_widgets["port"].text().strip()
        return {
            "enabled":          self._ibkr_widgets["enabled"].isChecked(),
            "data_source_only": self._ibkr_widgets["data_source_only"].isChecked(),
            "host":             self._ibkr_widgets["host"].text().strip() or "127.0.0.1",
            # None → auto-detect across standard ports; explicit int → pin that port.
            "port":             int(raw_port) if raw_port else None,
            "client_id":        _int("client_id", 42),
        }


# ── IBKR late-connect probe ──────────────────────────────────────────────────

class _IBKRProbeWorker(QThread):
    """
    Background TCP probe that detects IBKR Gateway *after* app startup.

    The app may have launched before the user started Gateway.  After each
    data refresh we fire this off-thread so a 200 ms TCP timeout per port
    doesn't block the GUI.  On success it emits ``found(port)``; when all
    ports fail it emits ``done()`` so the caller can apply back-off.
    """
    found = pyqtSignal(int)   # first live port
    done  = pyqtSignal()      # all ports unreachable

    def __init__(self, host: str, ports: list):
        super().__init__()
        self.host  = host
        self.ports = ports

    def run(self):
        import socket
        for port in self.ports:
            try:
                with socket.create_connection((self.host, port), timeout=0.2):
                    self.found.emit(port)
                    return
            except (OSError, socket.timeout):
                continue
        self.done.emit()


# ── Portfolio screen ─────────────────────────────────────────────────────────

class PortfolioScreen(QWidget):
    logout_requested    = pyqtSignal()
    configure_requested = pyqtSignal()
    strategy_clicked    = pyqtSignal(object)
    watchlist_requested = pyqtSignal()
    risk_requested      = pyqtSignal()

    # Primary balance tile definitions (key → balance API field, label → tile header)
    # Tiles shown in primary row, in column order matching the reference screenshot.
    # Manually-computed tiles (Day P&L, YTD, etc.) are interleaved in _build_balance_row.
    MORE_CARDS = [
        ("cash-balance", "Cash"),
    ]

    def __init__(self, creds, token):
        super().__init__()
        self.creds      = creds
        self.token      = token
        # Quotes provider chain.
        #   - Tasty adapter is always built (it backs everything that's not
        #     live quotes — fallback for symbols IBKR can't resolve, and
        #     standalone if the user hasn't enabled IBKR).
        #   - IBKR provider is only built if the user has enabled it in
        #     settings AND Gateway is reachable on the configured port.
        #     When both, the Hybrid wrapper prefers IBKR for live quotes
        #     and falls back to Tasty for misses.
        # token_getter lambda lets the Tasty side always see the freshest
        # OAuth token after a rotation.
        tasty_provider = TastyQuotesProvider(token_getter=lambda: self.token)
        self.quotes    = self._build_quotes_provider(tasty_provider)
        self._worker    = None
        self._accounts  = []
        self._alerted   = {}   # {(strategy_id, condition_type): severity} — prevents repeat alerts
        self._ytd_cache = {}   # {account_number: {fetched_at, transactions}} — shared across workers
        self._streamer        = None   # Opaque StreamHandle from QuotesProvider (live mode only)
        self._strategy_cards  = []     # [StrategyCard] — current My Strategies cards
        self._ua_cards        = []     # [StrategyCard] — current Unassigned cards
        self._year_start_cache = {}    # {acct_num: jan1_net_liq} — fetched once per session
        self._pnl_cache        = {}    # {acct_num: (timestamp, result)} — 60s TTL

        # Late-connect IBKR probe state.
        # Every _on_data() call increments _probe_skip_cnt; a probe fires
        # once it reaches _probe_skips.  On each failed probe we double the
        # interval (1 → 2 → 4 → 8 refreshes, i.e. 15 s → 2 min max).
        self._probe_worker   = None
        self._probe_skips    = 1   # fire on the 1st refresh, then back off
        self._probe_skip_cnt = 0

        self.strategies_all = api.load_strategies()   # {acct_num: [entries]}
        self.history_all    = api.load_history()      # {acct_num: [entries]}
        # One-time fix: history imported before the multiplier fix had P&L stored
        # without the contract multiplier (price×qty×1 instead of price×qty×mult).
        _fixed = repair_history_pnl(self.history_all)
        _fixed |= repair_pnl_missing_multiplier(self.history_all)
        if _fixed:
            api.save_history(self.history_all)
        self.snapshots      = api.load_snapshots()
        self._account_names = api.load_account_names()
        self._settings      = api.load_settings()

        self.setStyleSheet(T.BASE_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header(creds))

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        body_w = QWidget()
        body_w.setStyleSheet(f"background: {T.BG};")
        self.body = QVBoxLayout(body_w)
        self.body.setContentsMargins(32, 24, 32, 32)
        self.body.setSpacing(14)
        self._scroll_area.setWidget(body_w)
        root.addWidget(self._scroll_area)

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

        # Sort + hidden state — persisted in user settings.
        self._my_sort_col       = self._settings.get("my_sort_col") or None
        self._my_sort_asc       = bool(self._settings.get("my_sort_asc", False))
        self._show_hidden_strats= bool(self._settings.get("show_hidden_strats", False))

        # Section header row: title on the left, "show hidden (N)" on the right
        my_hdr_row = QHBoxLayout()
        my_hdr_row.setContentsMargins(0, 0, 0, 0)
        my_hdr_row.setSpacing(8)
        self.my_header = self._section_header("My Strategies")
        my_hdr_row.addWidget(self.my_header)
        my_hdr_row.addStretch()
        self.hidden_toggle = QPushButton("")
        self.hidden_toggle.setProperty("ghost", True)
        self.hidden_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hidden_toggle.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: none; padding: 4px 8px; font-size: 11px; font-weight: 600; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; }}"
        )
        self.hidden_toggle.clicked.connect(self._on_toggle_hidden)
        self.hidden_toggle.setVisible(False)
        my_hdr_row.addWidget(self.hidden_toggle)
        self.body.addLayout(my_hdr_row)

        # Sortable column header bar (aligned with each card's right-side stats).
        self._my_sort_bar_widget = self._build_my_sort_bar()
        self.body.addWidget(self._my_sort_bar_widget)

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

        watchlist_btn = QPushButton("☆  Watchlist")
        watchlist_btn.setFixedHeight(32)
        watchlist_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        watchlist_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 0 12px; "
            f"font-size: 12px; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        watchlist_btn.clicked.connect(self.watchlist_requested.emit)
        hl.addWidget(watchlist_btn)

        risk_btn = QPushButton("⚠  Risk")
        risk_btn.setFixedHeight(32)
        risk_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        risk_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 0 12px; "
            f"font-size: 12px; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        risk_btn.clicked.connect(self.risk_requested.emit)
        hl.addWidget(risk_btn)

        self.refresh_btn = QPushButton("↻  Refresh")
        self.refresh_btn.setFixedHeight(32)
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._load_data)
        hl.addWidget(self.refresh_btn)

        # Spinner animation state for the refresh button
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(90)
        self._spin_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spin_idx = 0
        self._spin_timer.timeout.connect(self._on_spin_tick)

        self._done_timer = QTimer(self)
        self._done_timer.setInterval(1200)
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(self._reset_refresh_btn)

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

    def _style_live_btn(self, on, streaming=False, mode="connecting"):
        """mode: "connecting" | "rest" (REST fallback) | "streaming" (unused) """
        if on and streaming:
            self.live_btn.setText("● Streaming")
            self.live_btn.setStyleSheet(
                f"QPushButton {{ background: {T.GREEN_D}; color: white; border: none; "
                f"border-radius: 6px; padding: 0 10px; font-size: 11px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: {T.GREEN}; }}"
            )
        elif on and mode == "rest":
            self.live_btn.setText("●  Live")
            self.live_btn.setStyleSheet(
                f"QPushButton {{ background: {T.GREEN_D}; color: white; border: none; "
                f"border-radius: 6px; padding: 0 10px; font-size: 11px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: {T.GREEN}; }}"
            )
        elif on:
            self.live_btn.setText("⟳  Connecting")
            self.live_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T.YELLOW}; "
                f"border: 1px solid {T.YELLOW}; border-radius: 6px; padding: 0 10px; "
                f"font-size: 11px; }}"
                f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
            )
        else:
            # Check if IBKR is connected — if so show "● Gateway" instead of "○ Live".
            prov = self._ibkr_provider() if hasattr(self, "quotes") else None
            ibkr_on = prov is not None and prov.is_connected()
            if ibkr_on:
                port  = getattr(prov, "_port", None)
                label = {4001: "Gateway",    4002: "Gateway (paper)",
                         7496: "TWS",        7497: "TWS (paper)"}.get(port, "IBKR")
                self.live_btn.setText(f"●  {label}")
                self.live_btn.setStyleSheet(
                    f"QPushButton {{ background: transparent; color: {T.GREEN}; "
                    f"border: 1px solid {T.GREEN}; border-radius: 6px; padding: 0 10px; "
                    f"font-size: 11px; font-weight: bold; }}"
                    f"QPushButton:hover {{ background: {T.GREEN_D}; color: white; }}"
                )
            else:
                self.live_btn.setText("○  Live")
                self.live_btn.setStyleSheet(
                    f"QPushButton {{ background: transparent; color: {T.MUTED}; "
                    f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 0 10px; "
                    f"font-size: 11px; }}"
                    f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
                )

    # ── Quotes provider construction ────────────────────────────────────────

    def _build_quotes_provider(self, tasty_provider):
        """
        Auto-detect IBKR Gateway / TWS and route live quotes through it.

        Behavior is zero-config:
          1. Settings explicitly opt out  (ibkr.enabled == False)
                → return Tasty only.
          2. Settings name a specific port (ibkr.port set)
                → probe that port first.
          3. Otherwise probe the four standard ports:
             4001 (Gateway live) → 4002 (Gateway paper)
             → 7496 (TWS live)   → 7497 (TWS paper)
          4. First port that answers within 200 ms wins; we connect
             through that port.
          5. Any failure (no Gateway running, ib_insync not installed,
             auth error, …) is silent — the dashboard keeps working
             on TastyTrade alone.

        The whole probe budget is bounded to ~1 s so launch isn't
        slowed for users without IBKR.
        """
        settings = api.load_settings() or {}
        ibkr_cfg = settings.get("ibkr") or {}

        # Explicit opt-out — only path that disables IBKR entirely.
        if ibkr_cfg.get("enabled") is False:
            return tasty_provider

        host       = ibkr_cfg.get("host") or "127.0.0.1"
        client_id  = int(ibkr_cfg.get("client_id") or 42)
        market_typ = int(ibkr_cfg.get("market_data_type") or 1)

        # Build probe list — user-specified port (if any) first, then the
        # four IBKR defaults in order of how common they are.
        custom_port = ibkr_cfg.get("port")
        DEFAULT_PORTS = [4001, 4002, 7496, 7497]
        seen = set()
        candidate_ports: list[int] = []
        for p in [custom_port, *DEFAULT_PORTS]:
            try:
                p = int(p) if p else None
            except (TypeError, ValueError):
                p = None
            if p and p not in seen:
                candidate_ports.append(p)
                seen.add(p)

        # 200 ms TCP probe per port — fast no-op for users without IBKR.
        import socket
        detected_port: int | None = None
        for port in candidate_ports:
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    detected_port = port
                    break
            except (OSError, socket.timeout):
                continue

        if detected_port is None:
            # Silent — most users won't have Gateway running.  Logging at
            # DEBUG-equivalent so it isn't noise on stderr.
            return tasty_provider

        # Lazy import so users without ib_insync installed don't crash
        # at app startup — they just stay on TastyTrade.
        try:
            from quotes_ibkr import IBKRQuotesProvider
        except ImportError:
            print("[ibkr] Gateway is running but ib_insync isn't installed. "
                  "Install with: pip3 install ib_insync", flush=True)
            return tasty_provider

        try:
            ibkr_provider = IBKRQuotesProvider(
                host=host, port=detected_port,
                client_id=client_id, market_data_type=market_typ,
            )
        except Exception as e:
            print(f"[ibkr] auto-detect found Gateway on port {detected_port} "
                  f"but couldn't connect: {e} — using TastyTrade", flush=True)
            return tasty_provider

        # One concise success log so users can verify which feed is live.
        kind = {4001: "Gateway (live)", 4002: "Gateway (paper)",
                7496: "TWS (live)",     7497: "TWS (paper)"}.get(detected_port,
                                                                 f"port {detected_port}")
        print(f"[ibkr] auto-detected: {kind} — quotes will use IBKR push, "
              f"TastyTrade as fallback", flush=True)
        return HybridQuotesProvider(primary=ibkr_provider, fallback=tasty_provider)

    # ── IBKR account-data helpers ─────────────────────────────────────────────

    def _ibkr_provider(self):
        """Return the IBKRQuotesProvider if active, else None."""
        if isinstance(self.quotes, HybridQuotesProvider):
            return self.quotes._primary
        return None

    def _ibkr_data_source_only(self) -> bool:
        """True when the user wants IBKR as a data source only (no account row)."""
        return bool(
            (api.load_settings() or {}).get("ibkr", {}).get("data_source_only", False)
        )

    def _update_ibkr_pill(self):
        """
        When IBKR Gateway is connected, update the Live button to show it.
        The button still toggles TastyTrade streaming; IBKR push is always on.
        """
        prov = self._ibkr_provider()
        if prov is None or not prov.is_connected():
            # IBKR not connected — leave the Live button as-is.
            return
        port = getattr(prov, "_port", None)
        label = {4001: "Gateway",      4002: "Gateway (paper)",
                 7496: "TWS",          7497: "TWS (paper)"}.get(port, "IBKR")
        # Only update the label when the button is in its idle (off) state so we
        # don't clobber "● Streaming" / "⟳ Connecting" while streaming is active.
        if not self.live_btn.isChecked():
            self.live_btn.setText(f"●  {label}")
            self.live_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T.GREEN}; "
                f"border: 1px solid {T.GREEN}; border-radius: 6px; padding: 0 10px; "
                f"font-size: 11px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: {T.GREEN_D}; color: white; }}"
            )

    # ── IBKR late-connect probe ──────────────────────────────────────────────

    def _start_ibkr_probe(self):
        """
        Fire a background TCP probe for IBKR Gateway.  Called after each
        data refresh so the app detects a Gateway that came up *after* launch.

        Guards:
          • Already connected  → no-op.
          • Another probe in flight → no-op (never double-queue).
          • Back-off counter not reached yet → increment and return.
          • User explicitly disabled IBKR → no-op.
        """
        # Already wired up — nothing to do.
        prov = self._ibkr_provider()
        if prov is not None and prov.is_connected():
            return
        # Probe still running from the previous refresh.
        if self._probe_worker is not None and self._probe_worker.isRunning():
            return
        # Exponential back-off: only probe every _probe_skips refreshes.
        self._probe_skip_cnt += 1
        if self._probe_skip_cnt < self._probe_skips:
            return
        self._probe_skip_cnt = 0

        settings   = api.load_settings() or {}
        ibkr_cfg   = settings.get("ibkr") or {}
        if ibkr_cfg.get("enabled") is False:
            return   # user explicitly disabled IBKR

        host        = ibkr_cfg.get("host") or "127.0.0.1"
        custom_port = ibkr_cfg.get("port")
        DEFAULT_PORTS = [4001, 4002, 7496, 7497]
        seen: set = set()
        ports: list = []
        for p in [custom_port, *DEFAULT_PORTS]:
            try:
                p = int(p) if p else None
            except (TypeError, ValueError):
                p = None
            if p and p not in seen:
                ports.append(p)
                seen.add(p)

        self._probe_worker = _IBKRProbeWorker(host, ports)
        self._probe_worker.found.connect(self._on_ibkr_probe_found)
        self._probe_worker.done.connect(self._on_ibkr_probe_done)
        self._probe_worker.start()

    def _on_ibkr_probe_found(self, port: int):
        """
        Gateway just became reachable on ``port``.  Upgrade the quotes
        provider from TastyTrade-only to Hybrid (IBKR primary + Tasty
        fallback) without disturbing any live session in progress.
        """
        # Race guard: another path (e.g. settings save) may have wired IBKR already.
        if self._ibkr_provider() is not None:
            return

        try:
            from quotes_ibkr import IBKRQuotesProvider
        except ImportError:
            print("[ibkr-probe] Gateway found on port {port} but ib_insync "
                  "is not installed.  Install with: pip3 install ib_insync",
                  flush=True)
            return

        settings   = api.load_settings() or {}
        ibkr_cfg   = settings.get("ibkr") or {}
        host       = ibkr_cfg.get("host") or "127.0.0.1"
        client_id  = int(ibkr_cfg.get("client_id") or 42)
        mdt        = int(ibkr_cfg.get("market_data_type") or 1)

        try:
            ibkr_prov = IBKRQuotesProvider(
                host=host, port=port,
                client_id=client_id, market_data_type=mdt,
            )
        except Exception as exc:
            print(f"[ibkr-probe] Gateway on port {port} — connect failed: {exc}",
                  flush=True)
            return

        # Replace the Tasty-only provider with Hybrid.
        tasty       = self.quotes
        self.quotes = HybridQuotesProvider(primary=ibkr_prov, fallback=tasty)

        kind = {4001: "Gateway (live)", 4002: "Gateway (paper)",
                7496: "TWS (live)",     7497: "TWS (paper)"}.get(port, f"port {port}")
        print(f"[ibkr-probe] late-connected to {kind} — switching to IBKR quotes",
              flush=True)

        # Reset back-off so the probe fires promptly on the next cycle.
        self._probe_skips    = 1
        self._probe_skip_cnt = 0

        self._update_ibkr_pill()
        self._update_streamer_symbols()
        # Trigger an immediate refresh so the next fetch uses IBKR.
        self._load_data()

    def _on_ibkr_probe_done(self):
        """
        All ports failed — Gateway still not up.  Double the back-off
        interval (1 → 2 → 4 → 8 refreshes) so we slow down probing as
        time passes, capping at ~2 minutes on the 15 s live timer.
        """
        self._probe_skips = min(self._probe_skips * 2, 8)

    # ── Live toggle ──────────────────────────────────────────────────────────

    def _toggle_live(self, on):
        if on:
            # Show "Connecting…" (or "Live" if we've learned WS isn't available)
            mode = "rest" if getattr(self, "_ws_unavailable", False) else "connecting"
            self._style_live_btn(True, streaming=False, mode=mode)
            self._live_timer.start()
            self._start_streamer()
        else:
            # Clicking OFF — cancel any in-progress connect immediately and
            # go back to the "not live" state.
            self._live_timer.stop()
            self._stop_streamer()
            self._style_live_btn(False)

    def _start_streamer(self):
        """Open a streaming-quotes connection via the active QuotesProvider."""
        # Skip entirely if we've already determined streaming isn't available
        # for this OAuth app — avoids spamming 403 retries on every Live
        # toggle.  User stays on REST 15s polling.
        if getattr(self, "_ws_unavailable", False):
            self._style_live_btn(True, mode="rest")
            return
        self._stop_streamer()   # tear down any previous instance
        symbols = self._all_position_symbols()
        # The provider returns an opaque handle; we keep it under the same
        # `_streamer` attribute name so existing call sites (live mode UI
        # etc.) still work.
        self._streamer = self.quotes.start_stream(
            symbols,
            self._on_price_update,
            self._on_streamer_status,
        )

    def _stop_streamer(self):
        """Tear down the active stream (if any)."""
        if self._streamer is not None:
            try:
                self.quotes.stop_stream(self._streamer)
            except Exception:
                pass
            self._streamer = None

    def _all_position_symbols(self):
        """Flatten all open-position symbols across accounts."""
        out = []
        for acct in self._accounts:
            for p in acct.get("positions", []):
                out.append(p.symbol)
        return out

    def _update_streamer_symbols(self):
        """Push the latest position-symbol list to the streamer."""
        if self._streamer is None:
            return
        self.quotes.update_subscription(self._streamer, self._all_position_symbols())

    def _on_streamer_status(self, status: str):
        """Handle QuoteStreamer status changes on the GUI thread."""
        print(f"[streamer] status → {status!r}", file=sys.stderr, flush=True)

        # If Live mode was turned off, or we already marked WS as unavailable,
        # ignore any late in-flight status events (the worker might still be
        # emitting "connecting" from inside its retry loop while we shut down).
        if not self.live_btn.isChecked() or getattr(self, "_ws_unavailable", False):
            return

        if status == "connected":
            self._style_live_btn(True, streaming=True)
            self.status_lbl.setText("")
            self._stream_fail_count = 0
            return
        if status.startswith("error"):
            # HTTP 403 = OAuth app lacks quote-streaming permission.  This is
            # a permanent permission error, never retry — just fall back to
            # REST polling (which is already running) and mark WS unavailable
            # so we don't try again this session.
            if "403" in status:
                self._ws_unavailable = True
                self._stop_streamer()
                self._style_live_btn(True, mode="rest")
                return
            # Transient errors: give up after 2 retries
            self._stream_fail_count = getattr(self, "_stream_fail_count", 0) + 1
            if self._stream_fail_count >= 2:
                self._stop_streamer()
                self._style_live_btn(True, mode="rest")
                return
        elif status == "connecting":
            self._style_live_btn(True, streaming=False)
        elif status == "disconnected":
            if self.live_btn.isChecked():
                self._style_live_btn(True, streaming=False)
        elif status.startswith("error"):
            # Show briefly in status label; keep button in "connecting" state
            self.status_lbl.setStyleSheet(
                f"color: {T.YELLOW}; font-size: 12px; border: none; background: transparent;"
            )
            self.status_lbl.setText(f"Stream: {status}")

    # ── Live quote update ────────────────────────────────────────────────────

    def _on_price_update(self, quotes: dict):
        """
        Called on the GUI thread every time DXLink delivers new quotes.
        Updates mark prices on the current account's positions in-place
        and refreshes only the P&L labels — no full re-render.
        """
        acct = self.current_account()
        if not acct:
            return

        changed = False
        for p in acct.get("positions", []):
            q = quotes.get(p.symbol)
            if not q:
                continue
            mark = q.get("mark")
            if mark is not None and mark > 0 and abs(mark - p.mark_price) > 0.001:
                p.mark_price = mark
                changed = True
            # Update Greeks if present
            for attr in ("delta", "gamma", "theta", "vega"):
                v = q.get(attr)
                if v is not None:
                    setattr(p, attr, v)
            if mark is not None and mark > 0:
                p._recompute()

        if not changed:
            return

        # ── Refresh Open P&L tile ────────────────────────────────────────────
        total_pnl = (sum(c.strategy.pnl for c in self._strategy_cards)
                     + sum(c.strategy.pnl for c in self._ua_cards))
        self.pnl_total_lbl.setText(money(total_pnl, signed=True))
        self.pnl_total_lbl.setStyleSheet(
            f"color: {pnl_color(total_pnl)}; font-size: 22px; font-weight: bold; "
            f"border: none; background: transparent;"
        )

        # ── Refresh strategy card P&L labels ────────────────────────────────
        for card in self._strategy_cards + self._ua_cards:
            card.refresh_pnl()

        # ── Refresh Day P&L ──────────────────────────────────────────────────
        bal = acct.get("balances", {})

        def _sg(key):
            try:
                raw = float(bal.get(key) or 0)
                eff = (bal.get(f"{key}-effect") or "").lower()
                return -raw if "debit" in eff else raw
            except (TypeError, ValueError):
                return 0.0

        try:
            unrealized_day = sum(
                p.sign * p.quantity * p.multiplier * (p.mark_price - p.close_price)
                for p in acct.get("positions", [])
                if p.close_price and p.close_price > 0 and p.mark_price
            )
            day_pnl = unrealized_day + _sg("realized-day-gain")
            self.day_pnl_lbl.setText(money(day_pnl, signed=True))
            self.day_pnl_lbl.setStyleSheet(
                f"color: {pnl_color(day_pnl)}; font-size: 22px; font-weight: bold; "
                f"border: none; background: transparent;"
            )
        except (TypeError, ValueError):
            pass

        # ── Refresh P/L YTD and YTD W/Fees with the NetLiq-delta formula ────
        # NetLiq from the balance API only updates on the 15s polling cycle, so
        # we approximate the live NetLiq as:
        #   live_NetLiq ≈ NetLiq_at_balance_fetch + (open_pnl_now − open_pnl_at_fetch)
        # which equals "current cash + current marked-to-market positions".
        try:
            year_start_nl     = getattr(self, "_year_start_nl",     None)
            balance_net_liq   = getattr(self, "_balance_net_liq",   None)
            open_pnl_at_fetch = getattr(self, "_open_pnl_at_fetch", None)
            net_deposits      = getattr(self, "_ytd_net_deposits",  0.0)
            ytd_fees          = getattr(self, "_ytd_fees",          0.0)

            if (year_start_nl is not None
                    and balance_net_liq is not None
                    and open_pnl_at_fetch is not None):
                live_net_liq = balance_net_liq + (total_pnl - open_pnl_at_fetch)
                ytd_wf       = live_net_liq - year_start_nl - net_deposits
                ytd_total    = ytd_wf + ytd_fees

                self.ytd_gross_lbl.setText(money(ytd_total, signed=True))
                self.ytd_gross_lbl.setStyleSheet(
                    f"color: {pnl_color(ytd_total)}; font-size: 22px; font-weight: bold; "
                    f"border: none; background: transparent;"
                )
                self.ytd_pnl_lbl.setText(money(ytd_wf, signed=True))
                self.ytd_pnl_lbl.setStyleSheet(
                    f"color: {pnl_color(ytd_wf)}; font-size: 22px; font-weight: bold; "
                    f"border: none; background: transparent;"
                )
        except (TypeError, ValueError):
            pass

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

    # ── My Strategies: sortable header + hidden toggle ──────────────────────

    # (column label, sort key, fixed width, default ascending direction)
    # Widths/spacing match the per-card stats in StrategyCard so the headers
    # line up over the values they sort.
    _MY_SORT_COLS = [
        ("DTE",      "dte",   68,  True),
        ("POP",      "pop",   68,  False),
        ("Δ",        "delta", 82,  False),
        ("Θ",        "theta", 82,  False),
        ("DAY P&L",  "day",   100, False),
        ("OPEN P&L", "pnl",   110, False),
    ]

    def _build_my_sort_bar(self):
        bar = QFrame()
        bar.setStyleSheet("background: transparent; border: none;")
        h = QHBoxLayout(bar)
        # Match per-card horizontal padding (22, 16, 22, 16) and inner spacing (16).
        h.setContentsMargins(22, 0, 22, 4)
        h.setSpacing(16)
        h.addStretch()                 # consume the name+badges area on the left

        self._my_sort_lbls: dict = {}  # {key: (label, base_text, default_asc)}
        for label, key, width, asc_default in self._MY_SORT_COLS:
            lbl = QLabel(label)
            lbl.setFixedWidth(width)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            lbl.setToolTip(
                f"Sort by {label}  ·  click again to reverse  ·  third click clears"
            )
            lbl.mousePressEvent = (
                lambda _e, k=key, a=asc_default: self._on_my_sort_click(k, a)
            )
            self._my_sort_lbls[key] = (lbl, label, asc_default)
            h.addWidget(lbl)
        # Trailing space matching chevron(22) + hide button(20) + spacing.
        h.addSpacing(22 + 20 + 16)
        self._update_my_sort_headers()
        return bar

    def _update_my_sort_headers(self):
        for key, (lbl, base, _ad) in (self._my_sort_lbls or {}).items():
            if key == self._my_sort_col:
                arrow = " ▲" if self._my_sort_asc else " ▼"
                lbl.setText(base + arrow)
                lbl.setStyleSheet(
                    f"color: {T.ACCENT}; font-size: 10px; font-weight: bold; "
                    f"letter-spacing: 0.6px; border: none; "
                    f"background: {T.CARD_ALT}; border-radius: 3px; padding: 1px 3px;"
                )
            else:
                lbl.setText(base)
                lbl.setStyleSheet(
                    f"color: {T.MUTED}; font-size: 10px; font-weight: bold; "
                    f"letter-spacing: 0.6px; border: none;"
                )

    def _on_my_sort_click(self, col_key: str, default_asc: bool):
        if self._my_sort_col == col_key:
            if self._my_sort_asc == default_asc:
                self._my_sort_asc = not self._my_sort_asc
            else:
                self._my_sort_col = None
                self._my_sort_asc = False
        else:
            self._my_sort_col = col_key
            self._my_sort_asc = default_asc
        # Persist so sort survives restart
        self._settings["my_sort_col"] = self._my_sort_col
        self._settings["my_sort_asc"] = self._my_sort_asc
        api.save_settings(self._settings)
        self._update_my_sort_headers()
        acct = self.current_account()
        if acct:
            self._render(acct)

    def _on_toggle_hidden(self):
        self._show_hidden_strats = not self._show_hidden_strats
        self._settings["show_hidden_strats"] = self._show_hidden_strats
        api.save_settings(self._settings)
        acct = self.current_account()
        if acct:
            self._render(acct)

    def _on_strategy_hide(self, strategy):
        """Toggle the strategy's hidden flag and re-render the list."""
        raw = next(
            (r for r in self.strategies_raw if r.get("id") == getattr(strategy, "id", None)),
            None,
        )
        if raw is None:
            return
        raw["hidden"] = not bool(raw.get("hidden"))
        self.save_strategies()
        acct = self.current_account()
        if acct:
            self._render(acct)

    def _strategy_sort_value(self, inst, col):
        """Return the comparable scalar for `inst` under sort column `col`."""
        if col == "dte":
            return inst.dte
        if col == "pop":
            return probability_of_profit(inst)
        if col == "delta":
            return inst.net_delta
        if col == "theta":
            return inst.net_theta
        if col == "day":
            return sum(
                l.sign * l.quantity * l.multiplier * (l.mark_price - l.close_price)
                for l in inst.legs
                if l.close_price and l.close_price > 0 and l.mark_price
            )
        if col == "pnl":
            return inst.pnl
        return None

    def _bal_tile(self, label):
        w = QFrame()
        w.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border: 1px solid {T.BORDER}; border-radius: 12px; }}"
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
        return w, val

    def _build_balance_row(self):
        outer = QVBoxLayout()
        outer.setSpacing(8)
        self.bal_cards = {}

        # ── Primary row (essentials only) ──────────────────────────────────
        # Net Liq | Day P&L | P/L YTD | Open P&L | BP Used %
        primary = QHBoxLayout()
        primary.setSpacing(10)

        def _add_bal(key, label, parent_layout):
            w, val = self._bal_tile(label)
            self.bal_cards[key] = val
            parent_layout.addWidget(w)

        _add_bal("net-liquidating-value", "Net Liq", primary)

        w, self.day_pnl_lbl   = self._bal_tile("Day P&L")
        primary.addWidget(w)

        w, self.ytd_pnl_lbl   = self._bal_tile("YTD W/Fees")
        primary.addWidget(w)

        w, self.pnl_total_lbl = self._bal_tile("Open P&L")
        primary.addWidget(w)

        w, self.cap_used_lbl  = self._bal_tile("BP Used %")
        primary.addWidget(w)

        # More button
        self._more_expanded = False
        self._more_btn = QPushButton("More  ▼")
        self._more_btn.setFixedSize(80, 62)
        self._more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more_btn.setStyleSheet(
            f"QPushButton {{ background: {T.CARD}; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 12px; "
            f"font-size: 11px; font-weight: bold; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        self._more_btn.clicked.connect(self._toggle_more)
        primary.addWidget(self._more_btn)

        outer.addLayout(primary)

        # ── Secondary row (hidden by default) ──────────────────────────────
        # YTD W/Fees | Option BP | Stock BP | BP Used $ | Cash
        self._more_row_w = QWidget()
        self._more_row_w.setStyleSheet("background: transparent;")
        more_row = QHBoxLayout(self._more_row_w)
        more_row.setContentsMargins(0, 0, 0, 0)
        more_row.setSpacing(10)

        w, self.ytd_gross_lbl = self._bal_tile("P/L YTD")
        more_row.addWidget(w)

        _add_bal("derivative-buying-power", "Option BP", more_row)
        _add_bal("equity-buying-power",     "Stock BP",  more_row)
        _add_bal("maintenance-requirement", "BP Used",   more_row)

        for key, label in self.MORE_CARDS:
            w, val = self._bal_tile(label)
            self.bal_cards[key] = val
            more_row.addWidget(w)
        more_row.addStretch()

        self._more_row_w.setVisible(False)
        outer.addWidget(self._more_row_w)

        return outer

    def _toggle_more(self):
        self._more_expanded = not self._more_expanded
        self._more_row_w.setVisible(self._more_expanded)
        self._more_btn.setText("Less  ▲" if self._more_expanded else "More  ▼")

    # ── Data loading / close detection ──────────────────────────────────────

    def _load_data(self):
        # Don't start a second worker while one is already running — that would
        # drop the Python reference to the running worker, causing GC to call
        # QThread::~QThread() while the C++ thread is still alive → SIGABRT.
        if self._worker and self._worker.isRunning():
            return
        self.status_lbl.setStyleSheet(
            f"color: {T.MUTED}; font-size: 13px; border: none; background: transparent;"
        )
        self.status_lbl.setText("Loading portfolio…")

        # Start the spinner animation on the refresh button
        self._spin_idx = 0
        self._spin_timer.start()
        self._done_timer.stop()
        self.refresh_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.ACCENT}; "
            f"border: 1px solid {T.ACCENT}; border-radius: 6px; padding: 0 12px; "
            f"font-size: 11px; font-weight: bold; }}"
        )

        self._worker = PortfolioWorker(self.token, self.creds, self._ytd_cache,
                                       self._year_start_cache, self._pnl_cache,
                                       quotes=self.quotes,
                                       ibkr_provider=self._ibkr_provider(),
                                       ibkr_data_source_only=self._ibkr_data_source_only())
        self._worker.done.connect(self._on_data)
        self._worker.start()

    def _on_spin_tick(self):
        """Advance the refresh-button spinner frame."""
        self._spin_idx = (self._spin_idx + 1) % len(self._spin_frames)
        self.refresh_btn.setText(f"{self._spin_frames[self._spin_idx]}  Refreshing")

    def _reset_refresh_btn(self):
        """Return the refresh button to its idle state."""
        self.refresh_btn.setText("↻  Refresh")
        self.refresh_btn.setStyleSheet("")

    def _on_data(self, result):
        # Stop spinner + show "✓ Updated" briefly, then reset
        self._spin_timer.stop()
        self.refresh_btn.setText("✓  Updated")
        self.refresh_btn.setStyleSheet(
            f"QPushButton {{ background: {T.GREEN_D}; color: white; border: none; "
            f"border-radius: 6px; padding: 0 12px; font-size: 11px; font-weight: bold; }}"
        )
        self._done_timer.start()

        # If the worker refreshed an expired access token, adopt it so all
        # subsequent refreshes (live-mode timer, manual refresh) use the new one.
        if result.get("new_token"):
            self.token = result["new_token"]
            self.creds["access_token"] = self.token
            api.save_credentials(self.creds)

        if result.get("error"):
            self.status_lbl.setStyleSheet(
                f"color: {T.RED}; font-size: 13px; border: none; background: transparent;"
            )
            self.status_lbl.setText(result["error"])
            return
        self.status_lbl.setText("")
        self._accounts = result.get("accounts", [])

        # Refresh IBKR pill now that we know the connection state.
        self._update_ibkr_pill()

        # If Gateway isn't connected yet, probe for it in the background so the
        # app picks it up automatically when the user starts Gateway after launch.
        self._start_ibkr_probe()

        # Detect closures + update snapshots for every account
        self._process_snapshots()
        self._check_exit_alerts()

        # Push fresh symbol list to the streamer (positions may have changed)
        self._update_streamer_symbols()

        self._refresh_account_combo()
        if self._accounts:
            idx = max(0, self.account_combo.currentIndex())
            acct = self._accounts[idx]
            if self.isVisible():
                # Portfolio is the active screen — re-render in place while
                # preserving any expanded cards and the scroll position.
                self._render_keeping_state(acct)
            else:
                # User is on detail / watchlist / risk page — don't disrupt
                # their view.  Store the fresh data and render when they return.
                self._pending_acct = acct

    # ── State-preserving render ──────────────────────────────────────────────

    def _render_keeping_state(self, acct):
        """
        Re-render the portfolio card list while preserving:
          • which strategy cards were expanded (showing legs inline)
          • the vertical scroll position of the main scroll area

        Called on every live-mode refresh so the user's view isn't
        jarred back to the top with all cards collapsed every 15 seconds.
        """
        # Capture expanded strategy IDs before the wipe.
        expanded_my = {
            getattr(c.strategy, "id", None) or c.strategy.name
            for c in self._strategy_cards if c._expanded
        }
        expanded_ua = {
            getattr(c.strategy, "id", None) or c.strategy.name
            for c in self._ua_cards if c._expanded
        }

        # Capture scroll position.
        sb = self._scroll_area.verticalScrollBar()
        scroll_val = sb.value()

        self._render(acct)

        # Restore expanded state on newly-created cards.
        for card in self._strategy_cards:
            key = getattr(card.strategy, "id", None) or card.strategy.name
            if key in expanded_my:
                card._set_expanded(True)
        for card in self._ua_cards:
            key = getattr(card.strategy, "id", None) or card.strategy.name
            if key in expanded_ua:
                card._set_expanded(True)

        # Restore scroll — defer one event loop so Qt has laid out the new
        # widgets before we move the scrollbar.
        QTimer.singleShot(0, lambda: sb.setValue(scroll_val))

    def showEvent(self, event):
        """
        Flush any pending re-render that was deferred because the portfolio
        was off-screen (user was on detail / watchlist / risk page).
        """
        super().showEvent(event)
        acct = getattr(self, "_pending_acct", None)
        if acct is not None:
            self._pending_acct = None
            self._render(acct)   # coming back from another page — no need to
                                  # preserve state (there's nothing open to save)

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

    # ── Exit-plan alert checking ─────────────────────────────────────────────

    def _check_exit_alerts(self):
        """
        After each refresh, evaluate every strategy's exit plan and fire a
        macOS notification the first time a condition moves to 'hit' or 'near'.
        """
        for acct in self._accounts:
            positions = acct["positions"]
            for raw in self.strategies_all.get(acct["number"], []):
                ep = raw.get("exit_plan") or {}
                if not ep:
                    continue
                inst = StrategyInstance(raw, positions)
                conds = check_exit_conditions(inst, ep)
                for c in conds:
                    key = (raw["id"], c["type"])
                    prev_sev = self._alerted.get(key, "ok")
                    new_sev  = c["severity"]
                    # Only fire when escalating (ok→near, ok/near→hit)
                    if new_sev in ("hit", "near") and new_sev != prev_sev:
                        self._alerted[key] = new_sev
                        self._notify(inst.name, c["message"], new_sev)
                    elif new_sev == "ok":
                        # Reset so it can re-fire if condition re-triggers
                        self._alerted.pop(key, None)

    @staticmethod
    def _notify(strategy_name, message, severity):
        """Fire a macOS notification (silent failure on non-Mac or sandboxed)."""
        try:
            icon = "⚡" if severity == "hit" else "◐"
            title   = f"{icon} Exit Alert — {strategy_name}"
            script  = (
                f'display notification "{message}" '
                f'with title "{title}" '
                f'sound name "Basso"'
            )
            os.system(f"osascript -e '{script}'")
        except Exception:
            pass

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
        # Re-render immediately with whatever's cached so the user sees the
        # strategy structure update right away…
        acct = self.current_account()
        if acct:
            self._render(acct)
        # …and kick off a fresh fetch in the background so newly-assigned
        # legs (especially ones that weren't in the cached snapshot) get
        # populated with live quotes/Greeks. Without this, the home screen
        # shows the old structure until the user manually clicks Refresh.
        self._load_data()

    def _render(self, acct):
        is_ibkr = acct.get("source") == "ibkr"
        # Rename section headers to match the account type.
        self.ua_header.setText(("POSITIONS" if is_ibkr else "UNASSIGNED LEGS"))
        self.my_header.setVisible(not is_ibkr)
        self._my_sort_bar_widget.setVisible(not is_ibkr)
        self.greeks_header.setVisible(True)

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

        # TastyTrade gain fields are unsigned; direction is in *-effect (Credit/Debit/None)
        def _signed_gain(key):
            try:
                raw = float(bal.get(key) or 0)
                effect = (bal.get(f"{key}-effect") or "").lower()
                return -raw if "debit" in effect else raw
            except (TypeError, ValueError):
                return 0.0

        positions_now = acct.get("positions", [])
        ytd_txns      = acct.get("ytd_txns", [])

        # ── Day P&L ─────────────────────────────────────────────────────────
        # Computed from positions: mark vs prior-session close-price.
        # TastyTrade's balance "unrealized-day-gain" field is unreliable
        # (returns 0 when market is closed or for certain account types).
        try:
            unrealized_day = sum(
                p.sign * p.quantity * p.multiplier * (p.mark_price - p.close_price)
                for p in positions_now
                if p.close_price and p.close_price > 0 and p.mark_price
            )
            # TastyTrade's "realized-day-gain" covers trades already closed today
            day_pnl = unrealized_day + _signed_gain("realized-day-gain")
            self.day_pnl_lbl.setText(money(day_pnl, signed=True))
            self.day_pnl_lbl.setStyleSheet(
                f"color: {pnl_color(day_pnl)}; font-size: 22px; font-weight: bold; "
                f"border: none; background: transparent;"
            )
        except (ValueError, TypeError):
            self.day_pnl_lbl.setText("—")

        # ── YTD P&L (TastyTrade SDK preferred, fallback to manual math) ─────
        # Primary path uses the official tastytrade SDK for verified field
        # names and the get_net_liquidating_value_history() endpoint.  If the
        # SDK call failed (e.g. session token issue), fall back to summing
        # transactions ourselves.
        ytd_pnl = acct.get("ytd_pnl_sdk")

        if ytd_pnl is not None:
            ytd_total      = ytd_pnl["p_l_ytd"]
            ytd_wf         = ytd_pnl["p_l_ytd_w_fees"]
            ytd_fees       = ytd_pnl["ytd_fees"]
            net_deposits   = ytd_pnl["ytd_net_deposits"]
            year_start_nl  = ytd_pnl["year_start_net_liq"]
            current_nl     = ytd_pnl["current_net_liq"]
            # Surface unknown Money-Movement sub-types so we can extend the
            # filter without users having to debug.  Shown only when material.
            unk = ytd_pnl.get("unknown_subs") or {}
            material = {k: v for k, v in unk.items() if abs(v) >= 1.0}
            if material:
                self.status_lbl.setStyleSheet(
                    f"color: {T.YELLOW}; font-size: 11px; border: none; background: transparent;"
                )
                desc = ", ".join(f"{k}: ${v:+.0f}" for k, v in material.items())
                self.status_lbl.setText(
                    f"YTD: unrecognized money-movement sub-types — {desc}. "
                    f"Counted as P&L; report to dev if this is wrong."
                )
        else:
            # SDK call failed → numbers will be approximate.  Warn the user.
            self.status_lbl.setStyleSheet(
                f"color: {T.YELLOW}; font-size: 11px; border: none; background: transparent;"
            )
            self.status_lbl.setText(
                "YTD numbers are approximate (SDK fetch failed — using fallback math)."
            )

            # Fallback: same NetLiq-delta math but with our manual transaction
            # parsing.  Less robust because field names are guessed.
            ytd_fees = 0.0
            for t in ytd_txns:
                if (t.get("transaction-type") or "") not in ("Trade", "Receive Deliver"):
                    continue
                for k, v in t.items():
                    if v is None: continue
                    kl = str(k).lower()
                    if not ("fee" in kl or "commission" in kl): continue
                    if kl.endswith("-effect") or kl.endswith("-id") or "description" in kl:
                        continue
                    try: ytd_fees += abs(float(v))
                    except (TypeError, ValueError): pass

            net_deposits = 0.0
            for t in ytd_txns:
                if (t.get("transaction-type") or "").lower() != "money movement":
                    continue
                try:
                    val = float(t.get("value") or 0)
                    eff = (t.get("value-effect") or "").lower()
                    net_deposits += val if "credit" in eff else -val
                except (TypeError, ValueError): pass

            try:
                current_nl = float(bal.get("net-liquidating-value") or 0)
            except (TypeError, ValueError):
                current_nl = 0.0
            year_start_nl = acct.get("year_start_net_liq") or current_nl
            ytd_wf        = current_nl - year_start_nl - net_deposits
            ytd_total     = ytd_wf + ytd_fees

        self.ytd_gross_lbl.setText(money(ytd_total, signed=True))
        self.ytd_gross_lbl.setStyleSheet(
            f"color: {pnl_color(ytd_total)}; font-size: 22px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        self.ytd_pnl_lbl.setText(money(ytd_wf, signed=True))
        self.ytd_pnl_lbl.setStyleSheet(
            f"color: {pnl_color(ytd_wf)}; font-size: 22px; font-weight: bold; "
            f"border: none; background: transparent;"
        )

        # ── Cache values for live-mode incremental updates ──────────────────
        # Live path approximates NetLiq_now ≈ NetLiq_at_fetch + Δopen_pnl.
        self._ytd_fees           = ytd_fees
        self._ytd_net_deposits   = net_deposits
        self._year_start_nl      = year_start_nl
        self._balance_net_liq    = current_nl
        self._open_pnl_at_fetch  = sum(p.pnl for p in positions_now)

        self._clear_layout(self.my_container)
        self._clear_layout(self.ua_container)

        positions = acct["positions"]
        metrics   = acct.get("metrics") or {}
        instances = [StrategyInstance(d, positions) for d in self.strategies_raw]
        leftover  = unassigned_positions(positions, self.strategies_raw)
        unassigned = group_unassigned(leftover)

        self._render_greeks(positions, metrics)

        total_pnl = sum(i.pnl for i in instances) + sum(s.pnl for s in unassigned)
        self.pnl_total_lbl.setText(money(total_pnl, signed=True))
        self.pnl_total_lbl.setStyleSheet(
            f"color: {pnl_color(total_pnl)}; font-size: 22px; font-weight: bold; "
            f"border: none; background: transparent;"
        )

        self._strategy_cards = []

        # \u2500\u2500 Filter hidden + apply user's sort \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        hidden_count = sum(1 for i in instances if i._raw.get("hidden"))
        if self._show_hidden_strats:
            display_list = list(instances)   # everything, hidden cards dimmed
        else:
            display_list = [i for i in instances if not i._raw.get("hidden")]

        if self._my_sort_col:
            def _sort_key(inst):
                v = self._strategy_sort_value(inst, self._my_sort_col)
                # None values always sort to the bottom regardless of direction
                return (0, v) if v is not None else (1, 0)
            display_list.sort(key=_sort_key, reverse=not self._my_sort_asc)

        # Update the "Hidden (N) \u2014 show" toggle visibility/text.
        if hidden_count:
            self.hidden_toggle.setText(
                f"\u2298 Hidden ({hidden_count}) \u2014 "
                + ("hide" if self._show_hidden_strats else "show")
            )
            self.hidden_toggle.setVisible(True)
        else:
            self.hidden_toggle.setVisible(False)

        if not display_list:
            if acct.get("source") == "ibkr":
                empty_txt = "Strategies are not used for IBKR accounts \u2014 positions appear below."
            else:
                empty_txt = 'No strategies configured \u2014 click "Configure Account" in the header.'
            empty = QLabel(empty_txt)
            empty.setStyleSheet(
                f"color: {T.MUTED}; font-size: 13px; padding: 22px; border: 1px dashed "
                f"{T.BORDER}; border-radius: 10px; background: {T.CARD};"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.my_container.addWidget(empty)
        else:
            for inst in display_list:
                card = StrategyCard(
                    inst, metrics=metrics,
                    hidden=bool(inst._raw.get("hidden")),
                )
                card.clicked.connect(self.strategy_clicked.emit)
                card.hide_requested.connect(self._on_strategy_hide)
                self.my_container.addWidget(card)
                self._strategy_cards.append(card)

        self._ua_cards = []
        if not unassigned:
            is_ibkr = acct.get("source") == "ibkr"
            if is_ibkr:
                prov = self._ibkr_provider()
                if prov and prov.is_connected():
                    ua_empty_txt = "● Gateway connected — no open positions in this account."
                else:
                    ua_empty_txt = "○ Gateway not connected — start IBKR Gateway to see positions."
            else:
                ua_empty_txt = "All legs are assigned to strategies."
            empty = QLabel(ua_empty_txt)
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
                self._ua_cards.append(card)

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
            return

        self.update_btn.setText(f"v{VERSION}")
        err = result.get("error") or ""

        # SILENT (auto-check on startup): never show any popup, ever.
        # If the check fails, mark the button with a small hint and move on
        # — the user can retry by clicking it manually.
        if silent:
            if err:
                self.update_btn.setToolTip(f"Last update check failed: {err}")
                self.update_btn.setText(f"v{VERSION} ⚠")
            return

        # Manual "Check for updates" click:
        # transient network errors → silent (not actionable)
        el = err.lower()
        if any(t in el for t in ("timed out", "timeout", "network",
                                  "could not resolve", "no route", "name or service")):
            self.update_btn.setToolTip(f"Last update check failed: {err}")
            return

        # Real errors → warn; success → info + offer restart (so source users
        # who already pulled can load the new code without closing manually).
        if err:
            QMessageBox.warning(self, "Update check failed", err)
        else:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("Up to date")
            box.setText(f"You're on the latest version (v{VERSION}).")
            box.setInformativeText(
                "If you just pulled new code, restart to load it in memory."
            )
            restart_btn = box.addButton("Restart now",
                                         QMessageBox.ButtonRole.AcceptRole)
            box.addButton("OK", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is restart_btn:
                self._relaunch()

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

        # Bundled .app: download the latest .app.zip from GitHub Releases
        # and self-replace.  Falls back to opening the releases page in a
        # browser if the download/install failed.
        if result.get("bundle"):
            download.setText("⬇  Update now")
            def _go_bundle():
                download.setEnabled(False)
                download.setText("Downloading…")
                ok, msg = updater.self_install()
                if not ok:
                    import webbrowser
                    QMessageBox.warning(
                        dlg, "Update failed",
                        f"{msg}\n\nOpening GitHub Releases so you can "
                        f"download manually."
                    )
                    webbrowser.open(
                        "https://github.com/amit1208levy/optionDashboard/releases"
                    )
                    download.setEnabled(True)
                    download.setText("⬇  Update now")
                    return
                # Exit cleanly — the detached shell script will relaunch
                # the new bundle once we're gone.
                dlg.accept()
                import sys
                sys.exit(0)
            download.clicked.connect(_go_bundle)
        else:
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
        dlg = AccountSettingsDialog(
            self._accounts, self._account_names, self._settings, parent=self
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._account_names = dlg.result_names()
            api.save_account_names(self._account_names)
            self._settings["leg_greeks"] = dlg.result_leg_greeks()
            old_ibkr = self._settings.get("ibkr") or {}
            new_ibkr = dlg.result_ibkr_settings()
            self._settings["ibkr"] = new_ibkr
            api.save_settings(self._settings)
            self._refresh_account_combo()
            # IBKR provider is wired at PortfolioScreen.__init__ time, so a
            # change requires a relaunch to take effect.  Tell the user
            # rather than silently doing nothing on the next refresh.
            if old_ibkr != new_ibkr:
                QMessageBox.information(
                    self, "IBKR settings updated",
                    "The new IBKR Gateway settings will take effect the next "
                    "time the app launches.\n\n"
                    "Quit and relaunch to start using IBKR for live quotes.",
                )

    def stop_workers(self):
        """Gracefully stop all background threads before this widget is deleted.

        Must be called before deleteLater() / before Python drops the last
        reference, otherwise QThread::~QThread() aborts when the thread is
        still running (SIGABRT / EXC_CRASH).
        """
        # Stop the quote streamer first (it has its own asyncio loop)
        self._stop_streamer()
        # Stop the portfolio fetch worker and update-check worker
        for attr in ("_worker", "_update_worker"):
            w = getattr(self, attr, None)
            if w and w.isRunning():
                w.quit()
                w.wait(5000)   # 5 s safety timeout

    def _logout(self):
        self._stop_streamer()
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
        self.watchlist = None
        self.risk      = None
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
        self.portfolio.watchlist_requested.connect(self._show_watchlist)
        self.portfolio.risk_requested.connect(self._show_risk)
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

    def _show_watchlist(self):
        if self.portfolio is None:
            return
        acct = self.portfolio.current_account()
        nlv  = 0.0
        if acct:
            try:
                nlv = float(acct["balances"].get("net-liquidating-value") or 0)
            except (TypeError, ValueError):
                pass
        self.watchlist = WatchlistPage(self.portfolio.token, nlv,
                                       quotes=self.portfolio.quotes)
        self.watchlist.back_requested.connect(self._back_from_watchlist)
        self.addWidget(self.watchlist)
        self.setCurrentWidget(self.watchlist)

    def _back_from_watchlist(self):
        if self.portfolio:
            self.setCurrentWidget(self.portfolio)
        if self.watchlist:
            self.watchlist.stop_workers()   # join threads before deletion
            self.removeWidget(self.watchlist)
            self.watchlist.deleteLater()
            self.watchlist = None

    def _show_risk(self):
        if self.portfolio is None:
            return
        self.risk = RiskPage(self.portfolio)
        self.risk.back_requested.connect(self._back_from_risk)
        self.addWidget(self.risk)
        self.setCurrentWidget(self.risk)

    def _back_from_risk(self):
        if self.portfolio:
            self.setCurrentWidget(self.portfolio)
        if self.risk:
            self.removeWidget(self.risk)
            self.risk.deleteLater()
            self.risk = None

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
        # Stop background threads on every screen that has them before we
        # call deleteLater — prevents QThread::~QThread() crash (SIGABRT).
        if self.portfolio:
            self.portfolio.stop_workers()
        if self.watchlist:
            self.watchlist.stop_workers()
        while self.count():
            w = self.widget(0)
            self.removeWidget(w)
            w.deleteLater()
        self.portfolio = None
        self.configure = None
        self.detail    = None
        self.watchlist = None
        self.risk      = None


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Set the dock / window icon. Looks for AppIcon.png next to app.py
    # (works for source-clone installs); PyInstaller bundles use CFBundleIconFile
    # in the .app's Info.plist, so this is just a fallback / window-icon source.
    _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AppIcon.png")
    if os.path.exists(_icon_path):
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(_icon_path))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())
