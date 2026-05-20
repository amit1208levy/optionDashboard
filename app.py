"""Options Dashboard — setup + portfolio + configure screens."""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from PyQt6.QtWidgets import (
    QApplication, QWidget, QStackedWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFrame, QScrollArea, QComboBox,
    QDialog, QDialogButtonBox, QFormLayout, QMessageBox, QTextEdit,
    QCheckBox, QListWidget, QListWidgetItem, QAbstractItemView,
    QTabWidget, QSystemTrayIcon, QMenu,
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
    stub_position_from_symbol,
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


class UpdateInstallWorker(QThread):
    """Downloads + extracts the new .app on a worker thread.

    The actual ~40 MB HTTPS download must NOT run on the GUI thread — a slow
    connection would freeze the event loop and macOS reports it as a hang
    (see v1.5.0 hang report 2BE6936C).
    """
    progress = pyqtSignal(int, int)   # (bytes_done, bytes_total)  total=0 if unknown
    done     = pyqtSignal(bool, str)  # (ok, message)

    def run(self):
        def cb(done_bytes, total_bytes):
            self.progress.emit(int(done_bytes), int(total_bytes))
        ok, msg = updater.self_install(progress_cb=cb)
        self.done.emit(ok, msg)


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
                 ibkr_data_source_only=True,
                 email_tracking_enabled=False,
                 email_firebase_token=None,
                 email_uid=None):
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
        # Email tracking: positions derived from Gmail trade confirmations,
        # stored in Firestore by the Cloud Function.
        self._email_tracking_enabled = email_tracking_enabled
        self._email_firebase_token   = email_firebase_token
        self._email_uid              = email_uid
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

            n_quoted = sum(1 for p in positions if quotes.get(p.symbol))
            print(f"[worker] acct {num}: {len(positions)} positions, "
                  f"{len(quotes)} quotes returned, {n_quoted} matched",
                  flush=True)

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
        print(f"[worker] run() started — token={'yes' if self.token else 'EMPTY'}, "
              f"ibkr_provider={type(self._ibkr_provider).__name__ if self._ibkr_provider else 'None'}, "
              f"data_source_only={self._ibkr_data_source_only}", flush=True)
        new_token = None
        for attempt in range(2):   # attempt 0 = normal; attempt 1 = after token refresh
            try:
                # When no TT token, skip TT API calls entirely (email-only mode).
                # Build offline account(s) from locally-stored strategies/history
                # so the user can still see their portfolio structure.
                ibkr_fut = None
                if not self.token:
                    accounts = []
                    stored_strategies = api.load_strategies()
                    stored_names = api.load_account_names()
                    stored_history = api.load_history()
                    today = datetime.now(timezone.utc).date()
                    strategies_changed = False
                    history_changed = False

                    for acct_num in stored_strategies:
                        if stored_strategies[acct_num] or acct_num in stored_names:
                            # Reconstruct positions from stored strategy legs,
                            # skipping expired legs (auto-retire them to history).
                            stubs = []
                            for strat in stored_strategies.get(acct_num, []):
                                expired_syms = []
                                for sym in list(strat.get("legs", [])):
                                    try:
                                        pos = stub_position_from_symbol(sym)
                                    except Exception:
                                        continue
                                    # If the leg has expired, retire it instead
                                    # of showing it with a negative DTE.
                                    if pos.expires_at and pos.expires_at.date() < today:
                                        expired_syms.append(sym)
                                        # Add a minimal history entry so the
                                        # closed leg shows in the strategy detail.
                                        stored_history.setdefault(acct_num, []).append({
                                            "symbol":      sym,
                                            "root":        pos.root,
                                            "strategy_id": strat["id"],
                                            "qty":         1,
                                            "sign":        pos.sign,
                                            "open_price":  0,
                                            "close_price": 0,
                                            "multiplier":  pos.multiplier,
                                            "closed_at":   pos.expires_at.isoformat(),
                                            "pnl":         0,
                                            "source":      "auto-expired",
                                            "call_put":    pos.call_put,
                                            "strike":      pos.strike,
                                            "instrument":  pos.instrument_type,
                                        })
                                        history_changed = True
                                        continue
                                    stubs.append(pos)

                                # Remove expired legs from the strategy.
                                if expired_syms:
                                    strat["legs"] = [s for s in strat["legs"]
                                                     if s not in expired_syms]
                                    if "leg_order" in strat:
                                        strat["leg_order"] = [s for s in strat["leg_order"]
                                                              if s not in expired_syms]
                                    strategies_changed = True

                            accounts.append({
                                "number":             acct_num,
                                "nickname":           stored_names.get(acct_num) or acct_num,
                                "balances":           {},
                                "positions":          stubs,
                                "metrics":            {},
                                "ytd_txns":           [],
                                "year_start_net_liq": None,
                                "ytd_pnl_sdk":        None,
                                "source":             "offline",
                            })

                    # Persist changes so expired legs stay retired on next launch.
                    if strategies_changed:
                        api.save_strategies(stored_strategies)
                        print(f"[worker] retired expired legs from strategies",
                              flush=True)
                    if history_changed:
                        api.save_history(stored_history)
                        print(f"[worker] added expired legs to history",
                              flush=True)
                    # Even without a TT token, fetch IBKR account if Gateway
                    # is connected — IBKR doesn't need TT credentials.
                    print(f"[worker] no-token branch: {len(accounts)} offline accts, "
                          f"ibkr_provider={bool(self._ibkr_provider)}, "
                          f"data_source_only={self._ibkr_data_source_only}", flush=True)
                    if self._ibkr_provider and not self._ibkr_data_source_only:
                        from ibkr_account import fetch_ibkr_account
                        print("[worker] submitting fetch_ibkr_account...", flush=True)
                        ibkr_fut = ThreadPoolExecutor(max_workers=1).submit(
                            fetch_ibkr_account, self._ibkr_provider
                        )
                else:
                    accounts_raw = [a for a in api.list_accounts(self.token)
                                    if a.get("account-number")]

                    # Fetch TastyTrade accounts + (optionally) IBKR account in parallel.
                    workers = max(len(accounts_raw), 1)
                    if self._ibkr_provider and not self._ibkr_data_source_only:
                        workers += 1

                    with ThreadPoolExecutor(max_workers=workers) as ex:
                        tt_futures = [ex.submit(self._fetch_one, a) for a in accounts_raw]
                        if self._ibkr_provider and not self._ibkr_data_source_only:
                            from ibkr_account import fetch_ibkr_account
                            ibkr_fut = ex.submit(fetch_ibkr_account, self._ibkr_provider)
                        results = [f.result() for f in tt_futures]

                    accounts = [r for r in results if r is not None]

                # Append IBKR account at the end of the list if available.
                print(f"[worker] ibkr_fut={'submitted' if ibkr_fut else 'None'}, "
                      f"accounts so far: {len(accounts)}", flush=True)
                if ibkr_fut is not None:
                    try:
                        ibkr_acct = ibkr_fut.result()
                        print(f"[worker] ibkr_acct result: "
                              f"{'has ' + str(len(ibkr_acct.get('positions', []))) + ' positions' if ibkr_acct else 'None'}", flush=True)
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

                            # When user is in offline mode (no TT token) but
                            # IBKR Gateway IS connected, the offline accounts
                            # have empty balance dicts → NET LIQ / BP USED %
                            # show "—". Copy IBKR's balances onto each
                            # offline account so the user sees their broker's
                            # NetLiquidation regardless of which account is
                            # selected. Safe because in offline mode there's
                            # no competing TT balance to clobber.
                            ibkr_bal = ibkr_acct.get("balances") or {}
                            if ibkr_bal:
                                for a in accounts:
                                    if (a.get("source") == "offline"
                                            and not a.get("balances")):
                                        a["balances"] = dict(ibkr_bal)
                                        # year_start_net_liq currently None
                                        # for offline accounts; copy from IBKR
                                        # if it has one (rare — Gateway
                                        # doesn't expose it natively).
                                        if (a.get("year_start_net_liq") is None
                                                and ibkr_acct.get("year_start_net_liq")):
                                            a["year_start_net_liq"] = (
                                                ibkr_acct["year_start_net_liq"]
                                            )
                    except Exception as e:
                        print(f"[ibkr_account] fetch failed: {e}", flush=True)

                # Append email-tracked account if enabled.
                if self._email_tracking_enabled:
                    try:
                        from email_account import build_email_account
                        email_acct = build_email_account(
                            self._email_firebase_token,
                            self._email_uid,
                        )
                        if email_acct:
                            positions = email_acct.get("positions", [])
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
                                    print(f"[email_account] quote refresh: {qe}",
                                          flush=True)
                            accounts.append(email_acct)
                    except Exception as e:
                        print(f"[email_account] fetch failed: {e}", flush=True)

                # Attach IBKR quotes to offline (stub) positions so they get
                # live mark prices, Greeks, and underlying price.
                if self.quotes is not None:
                    for acct in accounts:
                        if acct.get("source") != "offline":
                            continue
                        positions = acct.get("positions", [])
                        if not positions:
                            continue
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
                            attached = 0
                            for p in positions:
                                q = quotes.get(p.symbol)
                                if q:
                                    p.attach_quote(q)
                                    attached += 1
                            print(f"[worker] offline quotes: {attached}/{len(positions)} "
                                  f"symbols got IBKR data", flush=True)
                        except Exception as qe:
                            print(f"[worker] offline quote fetch: {qe}", flush=True)

                total_pos = sum(len(a.get("positions", [])) for a in accounts)
                print(f"[worker] DONE — emitting {len(accounts)} accounts, "
                      f"{total_pos} total positions", flush=True)
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
    skipped   = pyqtSignal()           # emitted when user clicks "Skip"

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

        # "Skip" link — enter app without TastyTrade credentials
        skip_btn = QPushButton("Skip — use Email Tracking only")
        skip_btn.setFlat(True)
        skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        skip_btn.setStyleSheet(
            f"QPushButton {{ color: {T.MUTED}; font-size: 12px; border: none; "
            f"background: transparent; text-decoration: underline; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; }}"
        )
        skip_btn.clicked.connect(lambda: self.skipped.emit())
        lay.addSpacing(8)
        lay.addWidget(skip_btn, alignment=Qt.AlignmentFlag.AlignCenter)

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
        self.setMinimumWidth(640)
        self.setMaximumHeight(720)
        self._fields = {}
        self._greek_checks = {}
        self._ibkr_widgets: dict = {}
        # Strip any legacy "iv" entries that may have been saved by older versions
        enabled_greeks = [k for k in settings.get("leg_greeks", ["delta", "theta"])
                          if k != "iv"]
        ibkr_cfg = settings.get("ibkr") or {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scrollable content area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            f"QScrollArea {{ border: none; background: transparent; }}"
            f"QScrollBar:vertical {{ background: {T.BG}; width: 8px; "
            f"border-radius: 4px; }}"
            f"QScrollBar::handle:vertical {{ background: {T.BORDER}; "
            f"border-radius: 4px; min-height: 30px; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical "
            f"{{ height: 0; }}"
        )
        _inner = QWidget()
        root = QVBoxLayout(_inner)
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

        # ── TastyTrade API Credentials ──────────────────────────────────────
        tt_title = QLabel("TastyTrade API")
        tt_title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 15px; font-weight: bold; border: none;"
        )
        root.addWidget(tt_title)

        _input_style = (
            f"background: {T.BG_ALT}; border: 1px solid {T.BORDER}; "
            f"border-radius: 6px; padding: 6px 8px; color: {T.TEXT};"
        )

        existing_creds = api.load_credentials() or {}
        self._tt_widgets: dict = {}

        if existing_creds:
            tt_hint = QLabel(
                f"Connected as: {existing_creds.get('name', '?')}"
            )
            tt_hint.setStyleSheet(
                f"color: {T.GREEN}; font-size: 12px; border: none;"
            )
        else:
            tt_hint = QLabel(
                "Connect your TastyTrade developer API credentials to pull "
                "live account data, positions, and P&&L."
            )
            tt_hint.setStyleSheet(
                f"color: {T.MUTED}; font-size: 12px; border: none;"
            )
        tt_hint.setWordWrap(True)
        root.addWidget(tt_hint)
        self._tt_status = tt_hint

        tt_form = QFormLayout()
        tt_form.setSpacing(8)

        for key, label, placeholder, is_secret in [
            ("name",          "Name:",          "Your name", False),
            ("client_id",     "Client ID:",     "TastyTrade OAuth Client ID", False),
            ("refresh_token", "Refresh Token:", "Long-lived refresh token", True),
            ("secret_token",  "Secret Token:",  "Client secret", True),
        ]:
            edit = QLineEdit(existing_creds.get(key, ""))
            edit.setPlaceholderText(placeholder)
            edit.setStyleSheet(_input_style)
            if is_secret:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._tt_widgets[key] = edit
            tt_form.addRow(label, edit)
        root.addLayout(tt_form)

        tt_btn_row = QHBoxLayout()
        tt_btn_row.setSpacing(10)

        self._tt_connect_btn = QPushButton(
            "Update Connection" if existing_creds else "Connect"
        )
        self._tt_connect_btn.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: #fff; "
            f"border: none; border-radius: 6px; padding: 8px 18px; "
            f"font-size: 13px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
            f"QPushButton:disabled {{ background: {T.BORDER}; color: {T.MUTED}; }}"
        )
        self._tt_connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tt_connect_btn.clicked.connect(self._on_tt_connect)
        tt_btn_row.addWidget(self._tt_connect_btn)

        if existing_creds:
            tt_disconnect_btn = QPushButton("Disconnect")
            tt_disconnect_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T.RED}; "
                f"border: 1px solid {T.RED}; border-radius: 6px; padding: 8px 14px; "
                f"font-size: 13px; }}"
                f"QPushButton:hover {{ background: {T.RED}; color: #fff; }}"
            )
            tt_disconnect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            tt_disconnect_btn.clicked.connect(self._on_tt_disconnect)
            tt_btn_row.addWidget(tt_disconnect_btn)

        tt_btn_row.addStretch()
        root.addLayout(tt_btn_row)

        self._tt_result_label = QLabel("")
        self._tt_result_label.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none;"
        )
        self._tt_result_label.setWordWrap(True)
        root.addWidget(self._tt_result_label)

        # ── Divider ──────────────────────────────────────────────────────────
        div_tt = QFrame()
        div_tt.setFrameShape(QFrame.Shape.HLine)
        div_tt.setStyleSheet(f"color: {T.BORDER};")
        root.addWidget(div_tt)

        # ── Cloud Sync (encrypted Firestore via Google Sign-In) ──────────────
        # Replaces the old per-Greek visibility checkboxes — leg columns are
        # now configured via the gear-icon dialog on the home page, which
        # makes the checkboxes here redundant.
        sync_title = QLabel("Cloud Sync")
        sync_title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 15px; font-weight: bold; border: none;"
        )
        root.addWidget(sync_title)

        self._sync_panel = _CloudSyncPanel(parent_dialog=self)
        root.addWidget(self._sync_panel)
        # The previous per-Greek setting is preserved as-is so other parts
        # of the app that still read settings["leg_greeks"] keep working.
        self._enabled_greeks = list(enabled_greeks)

        # ── Divider ──────────────────────────────────────────────────────────
        div2 = QFrame()
        div2.setFrameShape(QFrame.Shape.HLine)
        div2.setStyleSheet(f"color: {T.BORDER};")
        root.addWidget(div2)

        _cb_style = (
            f"QCheckBox {{ color: {T.TEXT}; font-size: 13px; border: none; }}"
            f"QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; "
            f"border: 1px solid {T.BORDER}; background: {T.BG_ALT}; }}"
            f"QCheckBox::indicator:checked {{ background: {T.ACCENT}; "
            f"border-color: {T.ACCENT}; }}"
        )

        # ── Email Position Tracking ─────────────────────────────────────────
        email_title = QLabel("Email Position Tracking")
        email_title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 15px; font-weight: bold; border: none;"
        )
        root.addWidget(email_title)

        email_hint = QLabel(
            "Automatically reads TastyTrade trade confirmation emails from "
            "your Gmail to track your portfolio — no TastyTrade API credentials "
            "needed. Requires Google Sign-In with Gmail read permission."
        )
        email_hint.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        email_hint.setWordWrap(True)
        root.addWidget(email_hint)

        et_cfg = settings.get("email_tracking", {})
        email_cb = QCheckBox("Enable email-based position tracking")
        email_cb.setChecked(bool(et_cfg.get("enabled")))
        email_cb.setStyleSheet(_cb_style)
        email_cb.setToolTip(
            "When enabled: the app reads TastyTrade trade confirmations from\n"
            "your Gmail inbox and maintains a live position ledger.\n"
            "A Cloud Function processes new emails automatically."
        )
        self._email_enabled_cb = email_cb
        root.addWidget(email_cb)

        # Status label
        self._email_status = QLabel("")
        self._email_status.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none;"
        )
        root.addWidget(self._email_status)

        # Check if Gmail scope is available
        try:
            cs = cloud_sync.CloudSync()
            if cs.is_signed_in() and cs.has_gmail_scope():
                self._email_status.setText("✓ Gmail access available")
                self._email_status.setStyleSheet(
                    f"color: {T.GREEN}; font-size: 12px; border: none;"
                )
            elif cs.is_signed_in():
                self._email_status.setText(
                    "⚠ Sign out and back in to grant Gmail read permission"
                )
                self._email_status.setStyleSheet(
                    f"color: {T.YELLOW}; font-size: 12px; border: none;"
                )
            else:
                self._email_status.setText(
                    "Sign in with Google (above) to enable email tracking"
                )
        except Exception:
            pass

        # ── Divider ──────────────────────────────────────────────────────────
        div3 = QFrame()
        div3.setFrameShape(QFrame.Shape.HLine)
        div3.setStyleSheet(f"color: {T.BORDER};")
        root.addWidget(div3)

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

        notice = QLabel("Changes to IBKR settings take effect on next app launch.")
        notice.setStyleSheet(f"color: {T.MUTED}; font-size: 11px; border: none;")
        notice.setWordWrap(True)
        root.addWidget(notice)

        # ── Divider ──────────────────────────────────────────────────────────
        div4 = QFrame()
        div4.setFrameShape(QFrame.Shape.HLine)
        div4.setStyleSheet(f"color: {T.BORDER};")
        root.addWidget(div4)

        # ── Google Sheets Export ─────────────────────────────────────────────
        sheets_title = QLabel("Google Sheets Export")
        sheets_title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 15px; font-weight: bold; border: none;"
        )
        root.addWidget(sheets_title)

        sheets_hint = QLabel(
            "Export a live snapshot of your portfolio to Google Sheets. "
            "Includes positions, strategies, Greeks, and P&&L."
        )
        sheets_hint.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        sheets_hint.setWordWrap(True)
        root.addWidget(sheets_hint)

        sheets_row = QHBoxLayout()
        sheets_row.setSpacing(10)

        self._sheets_export_btn = QPushButton("Export to Google Sheets")
        self._sheets_export_btn.setStyleSheet(
            f"QPushButton {{ background: {T.PURPLE}; color: #fff; "
            f"border: none; border-radius: 6px; padding: 8px 18px; "
            f"font-size: 13px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {T.PURPLE2}; }}"
            f"QPushButton:disabled {{ background: {T.BORDER}; color: {T.MUTED}; }}"
        )
        self._sheets_export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sheets_export_btn.clicked.connect(self._on_export_sheets)
        sheets_row.addWidget(self._sheets_export_btn)

        self._sheets_open_btn = QPushButton("Open Sheet ↗")
        self._sheets_open_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.ACCENT}; "
            f"border: 1px solid {T.ACCENT}; border-radius: 6px; padding: 8px 14px; "
            f"font-size: 13px; }}"
            f"QPushButton:hover {{ background: {T.ACCENT}; color: #fff; }}"
        )
        self._sheets_open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sheets_open_btn.clicked.connect(self._on_open_sheet)
        # Only show if we have a saved sheet URL
        saved_sheet_url = settings.get("sheets_export", {}).get("url")
        self._sheets_open_btn.setVisible(bool(saved_sheet_url))
        self._sheets_url = saved_sheet_url or ""
        sheets_row.addWidget(self._sheets_open_btn)

        sheets_row.addStretch()
        root.addLayout(sheets_row)

        self._sheets_status = QLabel("")
        self._sheets_status.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none;"
        )
        self._sheets_status.setWordWrap(True)
        root.addWidget(self._sheets_status)

        # Show last export time if available
        last_export = settings.get("sheets_export", {}).get("last_export")
        if last_export:
            self._sheets_status.setText(f"Last export: {last_export}")

        # Store accounts + strategies for export
        self._export_accounts = accounts
        self._export_settings = settings

        # ── End of scrollable content ────────────────────────────────────────
        root.addStretch()

        scroll.setWidget(_inner)
        outer.addWidget(scroll, 1)

        # ── Buttons (pinned at bottom, outside scroll) ──────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        btn_container = QWidget()
        btn_layout = QVBoxLayout(btn_container)
        btn_layout.setContentsMargins(24, 10, 24, 14)
        btn_layout.addWidget(buttons)
        outer.addWidget(btn_container)

    def result_names(self):
        out = {}
        for num, edit in self._fields.items():
            txt = edit.text().strip()
            if txt:
                out[num] = txt
        return out

    def result_leg_greeks(self):
        """Return list of enabled Greek column keys in canonical order.
        The per-Greek checkboxes were removed in favor of the home-page
        gear-icon → Leg Details column customizer; this just echoes back
        whatever was already saved in settings."""
        order = [key for key, _ in self.LEG_GREEK_OPTIONS]
        return [k for k in order if k in self._enabled_greeks]

    def accept(self):
        """Persist the Cloud Sync panel's state alongside the rest of the
        settings dialog's outputs."""
        try:
            if hasattr(self, "_sync_panel") and self._sync_panel is not None:
                self._sync_panel.commit()
        except Exception as e:
            print(f"[cloud_sync] commit failed: {e}", flush=True)
        super().accept()

    def result_email_tracking(self) -> dict:
        """Return email tracking settings as a JSON-serializable dict."""
        return {
            "enabled": self._email_enabled_cb.isChecked(),
        }

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

    def result_sheets_export(self) -> dict:
        """Return sheets export settings."""
        return getattr(self, "_sheets_export_result", {})

    def _on_export_sheets(self):
        """Handle the Export to Google Sheets button click."""
        self._sheets_export_btn.setEnabled(False)
        self._sheets_export_btn.setText("Exporting…")
        self._sheets_status.setText("Connecting to Google Sheets…")
        self._sheets_status.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none;"
        )
        # Force UI repaint before the blocking export call
        QApplication.processEvents()

        try:
            import cloud_sync
            cs = cloud_sync.CloudSync()
            if not cs.is_signed_in():
                self._sheets_status.setText(
                    "⚠ Sign in with Google (Cloud Sync section above) first."
                )
                self._sheets_status.setStyleSheet(
                    f"color: {T.YELLOW}; font-size: 12px; border: none;"
                )
                self._sheets_export_btn.setEnabled(True)
                self._sheets_export_btn.setText("Export to Google Sheets")
                return

            access_token = cs.get_google_access_token()
            if not access_token:
                self._sheets_status.setText(
                    "⚠ Could not get Google token — try signing out and back in."
                )
                self._sheets_status.setStyleSheet(
                    f"color: {T.YELLOW}; font-size: 12px; border: none;"
                )
                self._sheets_export_btn.setEnabled(True)
                self._sheets_export_btn.setText("Export to Google Sheets")
                return

            import sheets_export

            # Gather strategies from the parent portfolio screen
            all_strategies = []
            parent_screen = self.parent()
            if parent_screen and hasattr(parent_screen, "_strategy_cards"):
                for card in parent_screen._strategy_cards:
                    if hasattr(card, "strategy"):
                        all_strategies.append(card.strategy)
            if parent_screen and hasattr(parent_screen, "_ua_cards"):
                for card in parent_screen._ua_cards:
                    if hasattr(card, "strategy"):
                        all_strategies.append(card.strategy)

            existing_id = self._export_settings.get("sheets_export", {}).get(
                "spreadsheet_id"
            )
            sid, url, msg = sheets_export.export_portfolio(
                google_access_token=access_token,
                accounts=self._export_accounts,
                all_strategies=all_strategies,
                strategies_all=self._export_settings.get("strategies"),
                existing_spreadsheet_id=existing_id,
            )

            if sid and url:
                from datetime import datetime, timezone
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                self._sheets_export_result = {
                    "spreadsheet_id": sid,
                    "url": url,
                    "last_export": now_str,
                }
                self._sheets_url = url
                self._sheets_open_btn.setVisible(True)
                self._sheets_status.setText(f"✓ {msg}")
                self._sheets_status.setStyleSheet(
                    f"color: {T.GREEN}; font-size: 12px; border: none;"
                )
                # Persist immediately so the sheet ID survives even if
                # the user cancels the dialog.
                try:
                    settings = api.load_settings()
                    settings["sheets_export"] = self._sheets_export_result
                    api.save_settings(settings)
                except Exception:
                    pass
            else:
                self._sheets_status.setText(f"⚠ {msg}")
                self._sheets_status.setStyleSheet(
                    f"color: {T.YELLOW}; font-size: 12px; border: none;"
                )

        except Exception as e:
            print(f"[sheets] export error: {e}", flush=True)
            self._sheets_status.setText(f"⚠ Export failed: {e}")
            self._sheets_status.setStyleSheet(
                f"color: {T.YELLOW}; font-size: 12px; border: none;"
            )
        finally:
            self._sheets_export_btn.setEnabled(True)
            self._sheets_export_btn.setText("Export to Google Sheets")

    def _on_open_sheet(self):
        """Open the exported Google Sheet in the browser."""
        if self._sheets_url:
            import webbrowser
            webbrowser.open(self._sheets_url)

    # ── TastyTrade credential management ─────────────────────────────────

    def _on_tt_connect(self):
        """Validate and save TastyTrade API credentials."""
        data = {k: w.text().strip() for k, w in self._tt_widgets.items()}
        if not all(data.values()):
            self._tt_result_label.setText("All fields are required.")
            self._tt_result_label.setStyleSheet(
                f"color: {T.RED}; font-size: 12px; border: none;"
            )
            return

        self._tt_connect_btn.setEnabled(False)
        self._tt_connect_btn.setText("Connecting…")
        self._tt_result_label.setText("")
        QApplication.processEvents()

        try:
            token, err = api.get_access_token(
                data["refresh_token"], data["secret_token"]
            )
            if token:
                api.save_credentials(data)
                self._tt_result_label.setText(
                    "✓ Connected — restart the app to load TastyTrade data."
                )
                self._tt_result_label.setStyleSheet(
                    f"color: {T.GREEN}; font-size: 12px; border: none;"
                )
                self._tt_status.setText(f"Connected as: {data.get('name', '?')}")
                self._tt_status.setStyleSheet(
                    f"color: {T.GREEN}; font-size: 12px; border: none;"
                )
                self._tt_needs_restart = True
            else:
                msg = err or "Connection failed"
                if "invalid_grant" in (err or "") or "Invalid JWT" in (err or ""):
                    msg = ("Invalid credentials — Refresh Token and Secret Token "
                           "must be from the same OAuth app on developer.tastytrade.com")
                self._tt_result_label.setText(f"⚠ {msg}")
                self._tt_result_label.setStyleSheet(
                    f"color: {T.RED}; font-size: 12px; border: none;"
                )
        except Exception as e:
            self._tt_result_label.setText(f"⚠ {e}")
            self._tt_result_label.setStyleSheet(
                f"color: {T.RED}; font-size: 12px; border: none;"
            )
        finally:
            self._tt_connect_btn.setEnabled(True)
            self._tt_connect_btn.setText("Update Connection")

    def _on_tt_disconnect(self):
        """Remove saved TastyTrade credentials."""
        reply = QMessageBox.question(
            self, "Disconnect TastyTrade",
            "Remove your TastyTrade API credentials?\n\n"
            "You can re-add them later. IBKR and email tracking\n"
            "will continue to work.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            api.clear_credentials()
            for w in self._tt_widgets.values():
                w.setText("")
            self._tt_status.setText("Disconnected — add credentials to reconnect.")
            self._tt_status.setStyleSheet(
                f"color: {T.MUTED}; font-size: 12px; border: none;"
            )
            self._tt_result_label.setText(
                "✓ Credentials removed — restart the app to apply."
            )
            self._tt_result_label.setStyleSheet(
                f"color: {T.YELLOW}; font-size: 12px; border: none;"
            )
            self._tt_needs_restart = True


# ── Column-customization dialog ──────────────────────────────────────────────

class _ColumnPicker(QWidget):
    """
    Reusable two-pane (VISIBLE / HIDDEN) column picker. Drag items between
    lists to show/hide, drag within VISIBLE to reorder.
    """
    def __init__(self, all_cols, current_keys, default_keys, parent=None):
        super().__init__(parent)
        self._label_by_key = {c[0]: c[1] for c in all_cols}
        self._all_cols     = list(all_cols)
        self._defaults     = tuple(default_keys)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        cols = QHBoxLayout()
        cols.setSpacing(20)
        self.visible_list = self._make_list_widget()
        self.hidden_list  = self._make_list_widget()
        cols.addLayout(self._labelled_list("VISIBLE", T.ACCENT, self.visible_list), 1)
        cols.addLayout(self._labelled_list("HIDDEN",  T.MUTED,  self.hidden_list),  1)
        v.addLayout(cols, 1)

        ordered_visible = [k for k in (current_keys or []) if k in self._label_by_key]
        hidden = [c[0] for c in all_cols if c[0] not in ordered_visible]
        for k in ordered_visible:
            self.visible_list.addItem(self._make_item(k))
        for k in hidden:
            self.hidden_list.addItem(self._make_item(k))

        reset_btn = QPushButton("Reset to defaults")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid {T.BORDER}; border-radius: 6px; padding: 6px 14px; "
            f"font-size: 12px; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.ACCENT}; }}"
        )
        reset_btn.clicked.connect(self.reset_defaults)
        bot = QHBoxLayout()
        bot.addWidget(reset_btn)
        bot.addStretch()
        v.addLayout(bot)

    def _make_list_widget(self):
        w = QListWidget()
        w.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        w.setDefaultDropAction(Qt.DropAction.MoveAction)
        w.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        w.setAcceptDrops(True)
        w.setDragEnabled(True)
        w.setMovement(QListWidget.Movement.Snap)
        w.setStyleSheet(
            f"QListWidget {{ background: {T.BG_ALT}; color: {T.TEXT}; "
            f"border: 1px solid {T.BORDER}; border-radius: 10px; padding: 6px; "
            f"font-size: 14px; }}"
            f"QListWidget::item {{ padding: 10px 12px; border-radius: 6px; "
            f"margin: 2px 0; }}"
            f"QListWidget::item:hover {{ background: {T.CARD_ALT}; }}"
            f"QListWidget::item:selected {{ background: {T.CARD_ALT}; "
            f"color: {T.ACCENT}; }}"
        )
        return w

    def _labelled_list(self, text, color, list_widget):
        v = QVBoxLayout()
        v.setSpacing(6)
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: bold; "
            f"letter-spacing: 0.8px; border: none; background: transparent;"
        )
        v.addWidget(lbl)
        v.addWidget(list_widget, 1)
        return v

    def _make_item(self, key):
        it = QListWidgetItem(f"⠿   {self._label_by_key.get(key, key)}")
        it.setData(Qt.ItemDataRole.UserRole, key)
        it.setFlags(
            (it.flags() | Qt.ItemFlag.ItemIsDragEnabled)
            & ~Qt.ItemFlag.ItemIsDropEnabled
        )
        return it

    def reset_defaults(self):
        self.visible_list.clear()
        self.hidden_list.clear()
        for k in self._defaults:
            self.visible_list.addItem(self._make_item(k))

    def result_keys(self):
        out = []
        for i in range(self.visible_list.count()):
            out.append(self.visible_list.item(i).data(Qt.ItemDataRole.UserRole))
        return out


class _GoogleSignInWorker(QThread):
    """Run cloud_sync.sign_in_with_google() off the UI thread so the
    app stays responsive while the user completes the browser flow."""
    done = pyqtSignal(object, str)   # (tokens dict or None, error message)

    def __init__(self, include_gmail: bool = False):
        super().__init__()
        self._include_gmail = include_gmail

    def run(self):
        try:
            import cloud_sync
            tokens = cloud_sync.sign_in_with_google(
                include_gmail=self._include_gmail,
            )
            self.done.emit(tokens, "")
        except Exception as e:
            self.done.emit(None, str(e))


class _CloudPullWorker(QThread):
    """Pull every synced file from Firestore on app startup. Emits a
    {filename: content} dict for the UI to apply. Empty dict = nothing
    to do (not signed in / sync disabled / network failed)."""
    done = pyqtSignal(dict)

    def run(self):
        try:
            import cloud_sync
            sync = cloud_sync.CloudSync()
            if not sync.is_signed_in():
                self.done.emit({})
                return
            results = {}
            for name in cloud_sync.SYNCED_FILES:
                content, _ = sync.pull_file(name)
                if content is not None:
                    results[name] = content
            self.done.emit(results)
        except Exception as e:
            print(f"[cloud_sync] startup pull failed: {e}", flush=True)
            self.done.emit({})


class _CloudSyncPanel(QWidget):
    """
    Cloud Sync settings panel inside the Customize Columns dialog.
    Lets the user enable encrypted Firestore sync of strategies / history /
    groups across multiple Macs that share a passphrase + TT account.
    """

    def __init__(self, parent_dialog=None):
        super().__init__()
        self._parent_dialog = parent_dialog
        self._settings = api.load_settings() or {}
        # One-time cleanup: any older builds stored a plaintext or keychain
        # passphrase. We don't use a passphrase anymore — Google identity
        # is the secret. Wipe both copies.
        if "cloud_sync_passphrase" in self._settings:
            self._settings.pop("cloud_sync_passphrase", None)
            api.save_settings(self._settings)
        api.keychain_delete("cloud_sync_passphrase")

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        intro = QLabel(
            "Sign in with the <b>same Google account</b> on every Mac to "
            "share strategies, history, leg groups, and account names. "
            "Data is AES-encrypted on this device before upload."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setStyleSheet(
            f"color: {T.TEXT_DIM}; font-size: 12px; border: none; "
            f"background: transparent;"
        )
        intro.setMinimumHeight(46)
        v.addWidget(intro)

        # Enable toggle — persists immediately on click so the setting
        # survives regardless of how the dialog is closed (OK / Cancel / X).
        self._enable_chk = QCheckBox("Enable cloud sync")
        self._enable_chk.setChecked(bool(self._settings.get("cloud_sync_enabled")))
        self._enable_chk.setStyleSheet(
            f"QCheckBox {{ color: {T.TEXT}; font-size: 13px; font-weight: bold; "
            f"border: none; padding: 4px 0; }}"
            f"QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; "
            f"border: 1px solid {T.BORDER}; background: {T.BG_ALT}; }}"
            f"QCheckBox::indicator:checked {{ background: {T.ACCENT}; "
            f"border-color: {T.ACCENT}; }}"
        )
        self._enable_chk.toggled.connect(self._on_enable_toggled)
        v.addWidget(self._enable_chk)

        # Google OAuth Client ID is embedded in cloud_sync.py — no
        # paste-the-ID step. Just click Sign in with Google below.

        # Sign-in status line
        self._signin_status = QLabel("")
        self._signin_status.setStyleSheet(
            f"color: {T.MUTED}; font-size: 12px; border: none; "
            f"padding: 4px 0;"
        )
        self._refresh_signin_status()
        v.addWidget(self._signin_status)

        # ── Buttons ───────────────────────────────────────────────────────
        # Sync runs automatically — the app pulls on startup and pushes
        # after every change. No manual buttons except sign in / sign out.
        def _btn(label, slot, primary=False):
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumHeight(34)
            b.setStyleSheet(
                f"QPushButton {{ background: {T.PURPLE if primary else 'transparent'}; "
                f"color: {'white' if primary else T.ACCENT}; "
                f"border: 1px solid {T.PURPLE if primary else T.ACCENT}; "
                f"border-radius: 6px; padding: 0 18px; "
                f"font-size: 12px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: "
                f"{T.PURPLE2 if primary else T.CARD_ALT}; }}"
            )
            b.clicked.connect(slot)
            return b

        signin_row = QHBoxLayout()
        signin_row.setSpacing(10)
        signin_row.addWidget(_btn("Sign in with Google", self._on_google_signin, primary=True))
        signin_row.addWidget(_btn("Sign out", self._on_sign_out))
        signin_row.addStretch()
        v.addLayout(signin_row)

        auto_hint = QLabel(
            "Sync runs automatically: pulls on app launch, pushes after every "
            "change you save."
        )
        auto_hint.setWordWrap(True)
        auto_hint.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none; padding: 6px 0;"
        )
        v.addWidget(auto_hint)

        # Status line
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none; "
            f"background: transparent; padding: 4px 0;"
        )
        v.addWidget(self._status)
        v.addStretch()

    def _label(self, text, color):
        l = QLabel(text)
        l.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: bold; "
            f"letter-spacing: 0.5px; border: none;"
        )
        return l

    def _refresh_signin_status(self):
        email = api.keychain_get("cloud_sync_google_email")
        if email:
            self._signin_status.setText(f"✓ Signed in as <b>{email}</b>")
            self._signin_status.setTextFormat(Qt.TextFormat.RichText)
            self._signin_status.setStyleSheet(
                f"color: {T.GREEN}; font-size: 11px; border: none;"
            )
        else:
            self._signin_status.setText("Not signed in.")
            self._signin_status.setStyleSheet(
                f"color: {T.MUTED}; font-size: 11px; border: none;"
            )

    def _on_google_signin(self):
        # Don't kick off a second worker if one's already running.
        if getattr(self, "_signin_worker", None) and self._signin_worker.isRunning():
            return
        self._status.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none;"
        )
        self._status.setText(
            "Opening browser for Google sign-in… "
            "(this dialog stays responsive — finish the sign-in in the browser tab)"
        )
        QApplication.processEvents()

        # Run the blocking OAuth flow off the UI thread.
        # Only request Gmail scope if email tracking is enabled — avoids
        # Google's "sensitive info" verification screen for cloud-sync-only users.
        settings = api.load_settings() or {}
        email_on = bool((settings.get("email_tracking") or {}).get("enabled"))
        self._signin_worker = _GoogleSignInWorker(include_gmail=email_on)
        self._signin_worker.done.connect(self._on_signin_done)
        self._signin_worker.start()

    def _on_signin_done(self, tokens, error):
        if error:
            self._status.setStyleSheet(
                f"color: {T.RED}; font-size: 11px; border: none;"
            )
            self._status.setText(f"✗ {error}")
            return
        if not tokens or "refreshToken" not in tokens:
            self._status.setStyleSheet(
                f"color: {T.RED}; font-size: 11px; border: none;"
            )
            self._status.setText("✗ Sign-in did not return tokens.")
            return

        # Persist Firebase tokens + the Google email for status display.
        # The Firebase localId IS the uid that Firestore rules check.
        api.keychain_set("cloud_sync_refresh_token", tokens["refreshToken"])
        if tokens.get("localId"):
            api.keychain_set("cloud_sync_firebase_uid", tokens["localId"])
        if tokens.get("email"):
            api.keychain_set("cloud_sync_google_email", tokens["email"])

        self._status.setStyleSheet(
            f"color: {T.GREEN}; font-size: 11px; border: none;"
        )
        self._status.setText(f"✓ Signed in as {tokens.get('email', 'Google user')}.")
        self._refresh_signin_status()

        # If email tracking is enabled, auto-setup Gmail watch now.
        try:
            settings = api.load_settings()
            if settings.get("email_tracking", {}).get("enabled"):
                cs = cloud_sync.CloudSync()
                if cs.is_signed_in() and cs.has_gmail_scope():
                    cs.store_user_email_in_firestore()
                    cs.store_gmail_auth_in_firestore()
                    ok, msg = cs.setup_gmail_watch()
                    if ok:
                        self._status.setText(
                            f"✓ Signed in + Gmail watch active")
                        print(f"[email_tracking] auto-setup: {msg}",
                              flush=True)
                    else:
                        print(f"[email_tracking] auto-setup failed: {msg}",
                              flush=True)
        except Exception as e:
            print(f"[email_tracking] auto-setup error: {e}", flush=True)

        # Update the email status label if it exists in the parent dialog
        if self._parent_dialog and hasattr(self._parent_dialog, '_email_status'):
            self._parent_dialog._email_status.setText(
                "✓ Gmail access available")
            self._parent_dialog._email_status.setStyleSheet(
                f"color: {T.GREEN}; font-size: 12px; border: none;")

    def _on_sign_out(self):
        api.keychain_delete("cloud_sync_refresh_token")
        api.keychain_delete("cloud_sync_google_email")
        api.keychain_delete("cloud_sync_firebase_uid")
        self._refresh_signin_status()
        self._status.setStyleSheet(
            f"color: {T.MUTED}; font-size: 11px; border: none;"
        )
        self._status.setText("Signed out. Cached tokens cleared.")

    # ── Helpers ──────────────────────────────────────────────────────────
    def _make_sync(self):
        """Build a CloudSync object reading cached Google sign-in tokens
        from the keychain. Returns None with a status message if the user
        hasn't signed in yet."""
        try:
            import cloud_sync
        except ImportError:
            self._status.setText("✗ cloud_sync module unavailable.")
            return None
        if not cloud_sync.is_available():
            self._status.setStyleSheet(f"color: {T.RED}; font-size: 11px; border: none;")
            self._status.setText(
                "✗ The 'cryptography' Python package isn't installed. "
                "Re-run setup_app.sh to install it."
            )
            return None
        sync = cloud_sync.CloudSync()
        if not sync.is_signed_in():
            self._status.setStyleSheet(f"color: {T.RED}; font-size: 11px; border: none;")
            self._status.setText(
                "✗ Click 'Sign in with Google' first."
            )
            return None
        return sync

    def _on_enable_toggled(self, checked: bool):
        """Save the toggle to disk immediately so it persists even if the
        user closes the dialog with X or Cancel instead of OK."""
        s = api.load_settings() or {}
        s["cloud_sync_enabled"] = checked
        api.save_settings(s)

    def commit(self):
        """Persist the enable toggle. All other state lives in the
        keychain (refresh token, uid, email) and is written eagerly by
        the Sign-in / Sign-out handlers.
        NOTE: The toggle is also saved eagerly via _on_enable_toggled(),
        so this is effectively a no-op for the checkbox — kept for
        backward compat with the accept() call chain."""
        s = api.load_settings() or {}
        s["cloud_sync_enabled"] = bool(self._enable_chk.isChecked())
        s.pop("cloud_sync_passphrase", None)
        api.save_settings(s)


class _ColumnSettingsDialog(QDialog):
    """
    Tabbed dialog for customizing both strategy-level columns (home-screen
    sort bar + per-strategy stats) and leg-level columns (cells inside each
    leg row in the home dropdown AND the strategy detail page).
    """

    def __init__(self, current_keys, current_leg_keys, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Customize Columns")
        self.setMinimumSize(720, 600)
        self.resize(780, 640)
        self.setStyleSheet(T.BASE_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(10)

        title = QLabel("Customize Columns")
        title.setStyleSheet(
            f"color: {T.ACCENT}; font-size: 18px; font-weight: bold; "
            f"border: none; background: transparent;"
        )
        root.addWidget(title)

        hint = QLabel(
            "Drag rows between VISIBLE and HIDDEN to show/hide. "
            "Drag within VISIBLE to reorder."
        )
        hint.setStyleSheet(f"color: {T.MUTED}; font-size: 12px; border: none;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # Two-tab interface — Strategies and Leg Details. Same change to
        # leg columns affects every leg display (home drop-down + detail page).
        tabs = QTabWidget()
        tabs.setStyleSheet(
            f"QTabWidget::pane {{ border: 1px solid {T.BORDER}; border-radius: 8px; "
            f"background: transparent; top: -1px; }}"
            f"QTabBar::tab {{ background: transparent; color: {T.MUTED}; "
            f"padding: 8px 18px; margin-right: 4px; border: 1px solid {T.BORDER}; "
            f"border-bottom: none; border-top-left-radius: 8px; "
            f"border-top-right-radius: 8px; font-weight: bold; font-size: 12px; }}"
            f"QTabBar::tab:selected {{ background: {T.CARD}; color: {T.ACCENT}; }}"
            f"QTabBar::tab:hover:!selected {{ color: {T.TEXT_DIM}; }}"
        )

        self._strategy_picker = _ColumnPicker(
            StrategyCard.ALL_COLUMNS,
            current_keys,
            StrategyCard.DEFAULT_COLUMN_KEYS,
        )
        self._leg_picker = _ColumnPicker(
            StrategyCard.LEG_ALL_COLUMNS,
            current_leg_keys,
            StrategyCard.DEFAULT_LEG_COLUMN_KEYS,
        )

        s_w = QWidget(); s_l = QVBoxLayout(s_w); s_l.setContentsMargins(14, 14, 14, 14)
        s_l.addWidget(self._strategy_picker)
        l_w = QWidget(); l_l = QVBoxLayout(l_w); l_l.setContentsMargins(14, 14, 14, 14)
        l_l.addWidget(self._leg_picker)
        # Cloud Sync now lives in the main Settings dialog (Account Settings),
        # not here. Column tabs only.
        tabs.addTab(s_w, "Strategies")
        tabs.addTab(l_w, "Leg Details")
        root.addWidget(tabs, 1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def result_keys(self):
        return self._strategy_picker.result_keys()

    def result_leg_keys(self):
        return self._leg_picker.result_keys()


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
        if self.token:
            tasty_provider = TastyQuotesProvider(token_getter=lambda: self.token)
            self.quotes    = self._build_quotes_provider(tasty_provider)
        else:
            # No TT token — still try to detect IBKR Gateway for quotes.
            self.quotes    = self._build_quotes_provider(None)
        # Log which quotes provider is active for debugging.
        if self.quotes is None:
            print("[startup] quotes provider: NONE (no TT token, no IBKR)", flush=True)
        elif isinstance(self.quotes, HybridQuotesProvider):
            print("[startup] quotes provider: Hybrid (IBKR primary + TT fallback)", flush=True)
        else:
            print(f"[startup] quotes provider: {type(self.quotes).__name__}", flush=True)
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

        # Cloud-sync: pull latest from Firestore in the background. When it
        # finishes, _on_cloud_pull_done refreshes our in-memory dicts and
        # re-renders the UI so the user sees synced data without lifting a
        # finger.
        self._cloud_pull_worker = None
        self._maybe_start_cloud_pull()

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

        # Column visibility / order — list of column keys (subset of
        # StrategyCard.ALL_COLUMNS). Falls back to the full default order.
        all_keys = list(StrategyCard.DEFAULT_COLUMN_KEYS)
        saved = self._settings.get("my_columns")
        if isinstance(saved, list) and all(k in all_keys for k in saved) and saved:
            self._my_columns = saved
        else:
            self._my_columns = all_keys

        # Leg-row columns (per-leg cells inside the expanded strategy card
        # AND in the strategy detail page). Same shape as my_columns.
        all_leg_keys = list(StrategyCard.DEFAULT_LEG_COLUMN_KEYS)
        saved_leg = self._settings.get("my_leg_columns")
        if isinstance(saved_leg, list) and all(k in all_leg_keys for k in saved_leg) and saved_leg:
            self._my_leg_columns = saved_leg
        else:
            self._my_leg_columns = all_leg_keys

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

        # Auto-enable live mode when IBKR Gateway was detected at startup.
        # This fires the 15-second refresh timer and shows the "● Gateway"
        # pill without the user having to click the Live button.
        if self._ibkr_provider() is not None:
            self.live_btn.blockSignals(True)
            self.live_btn.setChecked(True)
            self.live_btn.blockSignals(False)
            self._live_timer.start()
            self._ws_unavailable = not self.token   # no TT streaming without token
            self._update_ibkr_pill_checked()

    # ── Header ──────────────────────────────────────────────────────────────

    def _build_header(self, creds):
        header = QFrame()
        header.setFixedHeight(60)
        header.setStyleSheet(
            f"QFrame {{ background: {T.CARD}; border-bottom: 1px solid {T.BORDER}; border-radius: 0; }}"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 28, 0)
        hl.setSpacing(12)

        # App logo — show AppIcon.png if it's bundled next to app.py.
        # Falls back to the previous hex-glyph if the file is missing.
        from PyQt6.QtGui import QPixmap
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AppIcon.png")
        if os.path.exists(icon_path):
            logo = QLabel()
            pix = QPixmap(icon_path).scaled(
                36, 36,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            logo.setPixmap(pix)
            logo.setStyleSheet("background: transparent; border: none;")
            hl.addWidget(logo)
            title_text = "Options Dashboard"
        else:
            title_text = "⬢  Options Dashboard"

        title = QLabel(title_text)
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
            # If IBKR is the active provider and there's no TT token, show
            # Gateway instead of "Connecting" to a dead TT stream.
            prov = self._ibkr_provider() if hasattr(self, "quotes") else None
            if prov is not None and not self.token:
                self._update_ibkr_pill_checked()
                return
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
            return tasty_provider  # may be None when no TT token

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
            if tasty_provider is None:
                # No IBKR and no TT — hand back a Yahoo-only provider so
                # equity-option positions still get marks + computed Greeks.
                # Yahoo can't help futures options; those stay blank.
                from quotes_yahoo import YahooQuotesProvider
                print("[quotes] no IBKR / no TT — falling back to Yahoo "
                      "for equity-option quotes (delayed ~15 min, no "
                      "futures options).", flush=True)
                return YahooQuotesProvider()
            # TT only — keep existing behavior. (Yahoo would only help when
            # TT can't cover a symbol, which is rare for accounts that have
            # a valid TT token.)
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

        # Yahoo is the always-on tertiary safety net. Activates only when
        # both IBKR and TT (when present) fail to fill a symbol.
        from quotes_yahoo import YahooQuotesProvider
        yahoo = YahooQuotesProvider()

        if tasty_provider is not None:
            print(f"[ibkr] auto-detected: {kind} — quotes will use IBKR push, "
                  f"TastyTrade as fallback, Yahoo as last-resort", flush=True)
            return HybridQuotesProvider(
                primary=ibkr_provider, fallback=tasty_provider, tertiary=yahoo,
            )
        else:
            # No TT — use Yahoo as the (single) fallback behind IBKR.
            print(f"[ibkr] auto-detected: {kind} — quotes will use IBKR push, "
                  f"Yahoo as fallback (no TastyTrade credentials)", flush=True)
            return HybridQuotesProvider(
                primary=ibkr_provider, fallback=yahoo,
            )

    # ── IBKR account-data helpers ─────────────────────────────────────────────

    def _ibkr_provider(self):
        """Return the IBKRQuotesProvider if active, else None."""
        if isinstance(self.quotes, HybridQuotesProvider):
            return self.quotes._primary
        # When running without TT credentials, quotes IS the IBKR provider.
        try:
            from quotes_ibkr import IBKRQuotesProvider
            if isinstance(self.quotes, IBKRQuotesProvider):
                return self.quotes
        except ImportError:
            pass
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
        # Update the label when the button is idle (off), OR when the button is
        # on but we have no TT token (IBKR is the only provider, so "Gateway"
        # is more informative than "Connecting" to a dead TT stream).
        if not self.live_btn.isChecked() or not self.token:
            self.live_btn.setText(f"●  {label}")
            self.live_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T.GREEN}; "
                f"border: 1px solid {T.GREEN}; border-radius: 6px; padding: 0 10px; "
                f"font-size: 11px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: {T.GREEN_D}; color: white; }}"
            )

    def _update_ibkr_pill_checked(self):
        """Show '● Gateway' even when the live button is checked (ON).
        Used when IBKR is the sole quotes provider (no TT token).
        Does NOT require is_connected() since the IB connection is lazy."""
        prov = self._ibkr_provider() if hasattr(self, "quotes") else None
        if prov is None:
            return
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

        # Replace the current provider. After late IBKR connect:
        #   • If we previously had a Yahoo-only provider (no IBKR + no TT
        #     state), promote IBKR to primary with Yahoo as fallback.
        #   • If we had TT (or a Hybrid wrapping TT), put IBKR in front
        #     and keep TT (or whatever previous) as fallback, with Yahoo
        #     as a tertiary safety net.
        from quotes_yahoo import YahooQuotesProvider
        prev = self.quotes
        if prev is None or isinstance(prev, YahooQuotesProvider):
            self.quotes = HybridQuotesProvider(
                primary=ibkr_prov, fallback=YahooQuotesProvider(),
            )
        else:
            self.quotes = HybridQuotesProvider(
                primary=ibkr_prov, fallback=prev, tertiary=YahooQuotesProvider(),
            )

        kind = {4001: "Gateway (live)", 4002: "Gateway (paper)",
                7496: "TWS (live)",     7497: "TWS (paper)"}.get(port, f"port {port}")
        print(f"[ibkr-probe] late-connected to {kind} — switching to IBKR quotes",
              flush=True)

        # Reset back-off so the probe fires promptly on the next cycle.
        self._probe_skips    = 1
        self._probe_skip_cnt = 0

        self._update_ibkr_pill()
        self._update_streamer_symbols()

        # Auto-enable live mode when Gateway comes up mid-session.
        if not self.live_btn.isChecked():
            self.live_btn.blockSignals(True)
            self.live_btn.setChecked(True)
            self.live_btn.blockSignals(False)
            self._live_timer.start()
            self._ws_unavailable = not self.token
            self._update_ibkr_pill_checked()

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
            self._live_timer.start()

            # When we have no TT token, TT streaming can't work.  If IBKR is
            # the active quotes provider show "● Gateway" straight away instead
            # of spinning on "Connecting" forever.  Don't check is_connected()
            # because the IB connection is lazy — the provider existing means
            # Gateway was reachable at startup.
            ibkr_prov = self._ibkr_provider() if hasattr(self, "quotes") else None
            if not self.token and ibkr_prov is not None:
                self._ws_unavailable = True          # no TT streamer needed
                self._update_ibkr_pill_checked()      # show "● Gateway" even while checked
                return

            # Show "Connecting…" (or "Live" if we've learned WS isn't available)
            mode = "rest" if getattr(self, "_ws_unavailable", False) else "connecting"
            self._style_live_btn(True, streaming=False, mode=mode)
            self._start_streamer()
        else:
            # Clicking OFF — cancel any in-progress connect immediately and
            # go back to the "not live" state.
            self._live_timer.stop()
            self._stop_streamer()
            self._style_live_btn(False)

    def _start_streamer(self):
        """Open a streaming-quotes connection via the active QuotesProvider."""
        if self.quotes is None:
            return
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
    # Master column registry pulled from StrategyCard so the sort-header bar
    # and per-card stats stay in sync.  The user picks visibility/order via
    # the column-settings dialog (gear icon at the right of the sort bar).
    @property
    def _my_sort_cols(self):
        # Return only the columns the user has chosen, in their order, with
        # (UPPER_LABEL, key, width, default_asc) tuples for the sort bar.
        meta = {k: (label, w, asc) for k, label, w, asc in StrategyCard.ALL_COLUMNS}
        out = []
        for k in self._my_columns:
            if k in meta:
                label, w, asc = meta[k]
                out.append((label.upper(), k, w, asc))
        return out

    def _build_my_sort_bar(self):
        bar = QFrame()
        bar.setStyleSheet("background: transparent; border: none;")
        h = QHBoxLayout(bar)
        # Match per-card horizontal padding (22, 16, 22, 16) and inner spacing (16).
        h.setContentsMargins(22, 0, 22, 4)
        h.setSpacing(16)
        h.addStretch()                 # consume the name+badges area on the left

        self._my_sort_lbls: dict = {}  # {key: (label, base_text, default_asc)}
        for label, key, width, asc_default in self._my_sort_cols:
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
        # Trailing chevron(22) + hide button(20) + spacing — same offset the
        # rows use, so the right edge of the bar lines up with the row's
        # right edge.
        h.addSpacing(22 + 20 + 16)
        # Gear button to open the column-customization dialog.
        gear = QPushButton("⚙")
        gear.setFixedSize(22, 22)
        gear.setCursor(Qt.CursorShape.PointingHandCursor)
        gear.setToolTip("Customize columns — choose which to show and in what order")
        gear.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {T.MUTED}; "
            f"border: 1px solid transparent; border-radius: 11px; "
            f"font-size: 13px; padding: 0; }}"
            f"QPushButton:hover {{ color: {T.ACCENT}; border-color: {T.BORDER}; "
            f"background: {T.BG_ALT}; }}"
        )
        gear.clicked.connect(self._open_columns_dialog)
        h.addWidget(gear)
        self._update_my_sort_headers()
        return bar

    def _update_my_sort_headers(self):
        # Reserve a fixed-width slot at the end of the text for the arrow so
        # the label doesn't shift left/right when an arrow is added/removed.
        # Use the same padding on both states so the vertical box doesn't grow.
        for key, (lbl, base, _ad) in (self._my_sort_lbls or {}).items():
            if key == self._my_sort_col:
                arrow = " ▲" if self._my_sort_asc else " ▼"
                lbl.setText(base + arrow)
                lbl.setStyleSheet(
                    f"color: {T.ACCENT}; font-size: 10px; font-weight: bold; "
                    f"letter-spacing: 0.6px; border: none; "
                    f"background: {T.CARD_ALT}; border-radius: 3px; "
                    f"padding: 1px 3px;"
                )
            else:
                # Trailing two spaces stand in for the arrow's slot — keeps
                # the right-aligned text from shifting when the arrow appears.
                lbl.setText(f"{base}  ")
                lbl.setStyleSheet(
                    f"color: {T.MUTED}; font-size: 10px; font-weight: bold; "
                    f"letter-spacing: 0.6px; border: none; "
                    f"background: transparent; border-radius: 3px; "
                    f"padding: 1px 3px;"
                )

    def _open_columns_dialog(self):
        """Open the customize-columns dialog. Saves the new orders on accept."""
        dlg = _ColumnSettingsDialog(
            self._my_columns, self._my_leg_columns, parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_order     = dlg.result_keys()     or list(StrategyCard.DEFAULT_COLUMN_KEYS)
            new_leg_order = dlg.result_leg_keys() or list(StrategyCard.DEFAULT_LEG_COLUMN_KEYS)
            changed = (new_order != self._my_columns
                       or new_leg_order != self._my_leg_columns)
            if not changed:
                return
            self._my_columns     = new_order
            self._my_leg_columns = new_leg_order
            self._settings["my_columns"]     = list(self._my_columns)
            self._settings["my_leg_columns"] = list(self._my_leg_columns)
            api.save_settings(self._settings)
            self._rebuild_my_sort_bar()
            acct = self.current_account()
            if acct:
                self._render(acct)

    def _rebuild_my_sort_bar(self):
        """Replace the sort bar with a freshly built one (after column changes)."""
        old = getattr(self, "_my_sort_bar_widget", None)
        new = self._build_my_sort_bar()
        if old is not None:
            idx = self.body.indexOf(old)
            if idx >= 0:
                self.body.insertWidget(idx, new)
                old.setParent(None)
                old.deleteLater()
        self._my_sort_bar_widget = new

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
        if col == "pnl_pct":
            return inst.pnl_pct
        if col in ("ytd", "ytd_pct"):
            from models import strategy_pnl_summary
            s = strategy_pnl_summary(inst.id, self.history, inst)
            return s["total_ytd"] if col == "ytd" else s["total_ytd_pct"]
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

    def _show_tt_warning(self, err):
        """Show a warning banner when TT token refresh failed."""
        msg = (
            "⚠ TastyTrade connection failed — positions won't load.\n"
            "Update your credentials in Settings → TastyTrade API."
        )
        if "invalid_grant" in (err or ""):
            msg = (
                "⚠ TastyTrade token expired — positions won't load.\n"
                "Update your credentials in Settings → TastyTrade API."
            )
        self.status_lbl.setText(msg)
        self.status_lbl.setStyleSheet(
            f"color: {T.YELLOW}; font-size: 12px; border: none; "
            f"background: transparent;"
        )

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

        # Check if email tracking is enabled and get Firebase token.
        email_enabled = False
        email_token = None
        email_uid = None
        try:
            settings = api.load_settings()
            et = settings.get("email_tracking", {})
            if et.get("enabled"):
                cs = cloud_sync.CloudSync()
                if cs.is_signed_in() and cs.has_gmail_scope():
                    email_token = cs._ensure_auth()
                    email_uid = cs._uid
                    email_enabled = bool(email_token and email_uid)
        except Exception:
            pass

        self._worker = PortfolioWorker(self.token, self.creds, self._ytd_cache,
                                       self._year_start_cache, self._pnl_cache,
                                       quotes=self.quotes,
                                       ibkr_provider=self._ibkr_provider(),
                                       ibkr_data_source_only=self._ibkr_data_source_only(),
                                       email_tracking_enabled=email_enabled,
                                       email_firebase_token=email_token,
                                       email_uid=email_uid)
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

    def _maybe_start_cloud_pull(self):
        """Kick off a background pull at startup if cloud sync is enabled
        and the user is signed in. Silent on failure."""
        if not (self._settings or {}).get("cloud_sync_enabled"):
            return
        if self._cloud_pull_worker and self._cloud_pull_worker.isRunning():
            return
        self._cloud_pull_worker = _CloudPullWorker()
        self._cloud_pull_worker.done.connect(self._on_cloud_pull_done)
        self._cloud_pull_worker.start()

    def _on_cloud_pull_done(self, results):
        """Apply pulled cloud data: write to local files, refresh in-memory
        dicts, re-render the home screen."""
        if not results:
            return
        data_dir = api._user_data_dir()
        # Write each pulled file to disk. Skip the auto-push hook so we
        # don't immediately push back what we just pulled.
        for fname, content in results.items():
            try:
                with open(os.path.join(data_dir, fname), "w") as f:
                    json.dump(content, f, indent=2, default=str)
            except Exception as e:
                print(f"[cloud_sync] write {fname} failed: {e}", flush=True)

        # Reload in-memory state from disk.
        self.strategies_all = api.load_strategies()
        self.history_all    = api.load_history()
        self.snapshots      = api.load_snapshots()
        self._account_names = api.load_account_names()
        # leg_groups live inside strategies_all so they're already refreshed.

        acct = self.current_account()
        if acct:
            self._render(acct)
        print(f"[cloud_sync] startup pull applied {len(results)} file(s)",
              flush=True)

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
        is_ibkr_acct = acct.get("source") == "ibkr"

        if is_ibkr_acct:
            # IBKR Gateway doesn't expose YTD natively — its summary fields
            # are session-only (since the gateway started). The TastyTrade-
            # specific SDK + transactions path doesn't apply here. Showing
            # "SDK fetch failed" was misleading because nothing actually
            # failed. Show "—" with a clarifying note instead.
            self.status_lbl.setStyleSheet(
                f"color: {T.MUTED}; font-size: 11px; border: none; background: transparent;"
            )
            self.status_lbl.setText(
                "YTD via IBKR Gateway not supported — track YTD on the TastyTrade account."
            )
            try:
                current_nl = float(bal.get("net-liquidating-value") or 0)
            except (TypeError, ValueError):
                current_nl = 0.0
            ytd_total     = None
            ytd_wf        = None
            ytd_fees      = 0.0
            net_deposits  = 0.0
            year_start_nl = current_nl
        elif ytd_pnl is not None:
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
            # Offline mode (no TT token) is a different scenario from a
            # generic SDK failure; tailor the message accordingly.
            self.status_lbl.setStyleSheet(
                f"color: {T.YELLOW}; font-size: 11px; border: none; background: transparent;"
            )
            if acct.get("source") == "offline":
                self.status_lbl.setText(
                    "Running without TastyTrade credentials — NET LIQ comes from "
                    "IBKR Gateway, YTD totals are aggregated from local history, "
                    "and OPEN P&L on individual strategies will show \"—\" until "
                    "you sign in to TastyTrade (Settings → TastyTrade API)."
                )
            else:
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

            # Offline mode (no TT credentials, no SDK history): the NetLiq-
            # delta formula collapses to 0 because year_start_nl falls back
            # to current_nl. But the user has real per-strategy YTD values
            # in .history.json (those drive the per-row "P&L YTD" column).
            # Sum them here so the portfolio-level tile matches reality.
            if (acct.get("source") == "offline"
                    and (ytd_wf is None or abs(ytd_wf) < 1e-6)):
                from datetime import date
                this_year = date.today().year
                acct_history = self.history_all.get(acct["number"], []) or []
                realized_ytd_sum = 0.0
                for h in acct_history:
                    closed_at = h.get("closed_at") or ""
                    if closed_at[:4] == str(this_year):
                        try:
                            realized_ytd_sum += float(h.get("pnl") or 0.0)
                        except (TypeError, ValueError):
                            pass
                # Add open P&L from non-stub positions (in offline mode,
                # stubs contribute 0, so this is usually just 0 unless
                # the user has a mix).
                open_pnl_sum = sum(
                    p.pnl for p in positions_now
                    if not getattr(p, "pnl_unknown", False)
                )
                ytd_wf    = realized_ytd_sum + open_pnl_sum
                ytd_total = ytd_wf  # no fees data in offline mode

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
                    history=self.history,
                    column_keys=self._my_columns,
                    leg_column_keys=self._my_leg_columns,
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
                card = StrategyCard(strat, metrics=metrics,
                                    column_keys=self._my_columns,
                                    leg_column_keys=self._my_leg_columns)
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
                later.setEnabled(False)
                download.setText("Downloading…")

                # Run the download + extract on a worker thread so the GUI
                # event loop keeps pumping (otherwise a slow connection
                # freezes the UI and macOS flags it as a hang).
                worker = UpdateInstallWorker(self)

                def _on_progress(done_bytes, total_bytes):
                    if total_bytes > 0:
                        pct = int(done_bytes * 100 / total_bytes)
                        mb_done = done_bytes / (1024 * 1024)
                        mb_total = total_bytes / (1024 * 1024)
                        download.setText(
                            f"Downloading… {pct}% ({mb_done:.1f}/{mb_total:.1f} MB)"
                        )
                    else:
                        mb_done = done_bytes / (1024 * 1024)
                        download.setText(f"Downloading… {mb_done:.1f} MB")

                def _on_done(ok, msg):
                    worker.deleteLater()
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
                        later.setEnabled(True)
                        download.setText("⬇  Update now")
                        return
                    # Exit cleanly — the detached shell script will relaunch
                    # the new bundle once we're gone.
                    dlg.accept()
                    import sys
                    sys.exit(0)

                worker.progress.connect(_on_progress)
                worker.done.connect(_on_done)
                worker.start()
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
        accepted = dlg.exec() == QDialog.DialogCode.Accepted

        # The cloud-sync enable checkbox saves eagerly on toggle (to disk),
        # so sync the on-disk value into self._settings no matter how the
        # dialog was closed (OK / Cancel / X).  This prevents any later
        # save_settings(self._settings) from clobbering the user's choice.
        disk = api.load_settings() or {}
        self._settings["cloud_sync_enabled"] = disk.get(
            "cloud_sync_enabled", False
        )

        if accepted:
            self._account_names = dlg.result_names()
            api.save_account_names(self._account_names)
            self._settings["leg_greeks"] = dlg.result_leg_greeks()
            old_ibkr = self._settings.get("ibkr") or {}
            new_ibkr = dlg.result_ibkr_settings()
            self._settings["ibkr"] = new_ibkr

            # Email tracking settings
            new_email = dlg.result_email_tracking()
            old_email = self._settings.get("email_tracking", {})
            self._settings["email_tracking"] = new_email

            # If email tracking was just enabled, try to set up Gmail watch.
            # If not signed in with Google yet, just save the preference —
            # Gmail watch will be set up when they sign in via Cloud Sync.
            if new_email.get("enabled") and not old_email.get("enabled"):
                try:
                    cs = cloud_sync.CloudSync()
                    if cs.is_signed_in() and cs.has_gmail_scope():
                        self._finish_email_tracking_setup(cs)
                    else:
                        QMessageBox.information(
                            self, "Email Tracking Enabled",
                            "Email tracking is saved.\n\n"
                            "To activate it, sign in with Google in the\n"
                            "Cloud Sync section of Settings. Gmail watch\n"
                            "will start automatically once signed in.",
                        )
                except Exception as e:
                    print(f"[email_tracking] setup error: {e}", flush=True)

            # Sheets export settings (saved even on Cancel via the export button,
            # but we also persist any result here in case user presses OK after).
            sheets_result = dlg.result_sheets_export()
            if sheets_result:
                self._settings["sheets_export"] = sheets_result

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

    def _finish_email_tracking_setup(self, cs):
        """Store Gmail tokens in Firestore and activate Gmail watch."""
        try:
            cs.store_user_email_in_firestore()
            cs.store_gmail_auth_in_firestore()
            ok, msg = cs.setup_gmail_watch()
            if ok:
                print(f"[email_tracking] Gmail watch: {msg}", flush=True)
                QMessageBox.information(
                    self, "Email Tracking Active",
                    "Gmail watch is set up!\n"
                    "Trade emails will now be processed automatically.",
                )
            else:
                print(f"[email_tracking] Gmail watch failed: {msg}",
                      flush=True)
                QMessageBox.warning(self, "Gmail Watch Failed", msg)
        except Exception as e:
            print(f"[email_tracking] setup error: {e}", flush=True)

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
        self._really_quit = False
        self._tray         = None
        self._build_tray()
        self._show_initial()

    # ── Background / tray behavior ────────────────────────────────────────
    def _build_tray(self):
        """Create a system-tray icon so the app stays reachable when its
        window is hidden. Click → reopen window. Right-click → menu."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AppIcon.png")
        from PyQt6.QtGui import QIcon
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QApplication.windowIcon()

        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Options Dashboard")

        menu = QMenu()
        show_act = menu.addAction("Show Options Dashboard")
        show_act.triggered.connect(self._reopen_from_tray)
        menu.addSeparator()
        quit_act = menu.addAction("Quit")
        quit_act.triggered.connect(self._quit_from_tray)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason):
        # Single-click / double-click on the tray icon → reopen window.
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._reopen_from_tray()

    def _reopen_from_tray(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self):
        self._really_quit = True
        QApplication.quit()

    def closeEvent(self, event):
        # Pressing the red X (or Cmd+W) hides the window instead of quitting,
        # so background workers (live quote streamer, IBKR Gateway watcher,
        # update poller, future notification jobs) keep running. Cmd+Q or the
        # tray menu's Quit actually terminates the app.
        if self._really_quit:
            event.accept()
            return
        event.ignore()
        self.hide()
        if self._tray is not None and self._tray.isVisible():
            self._tray.showMessage(
                "Options Dashboard",
                "Still running in the background. Click the tray icon to reopen.",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    def _show_initial(self):
        settings = api.load_settings()

        # First launch ever → show the setup screen so the user can
        # either connect TastyTrade or skip.  After that first choice,
        # the app always goes straight to the portfolio; credentials
        # can be added/changed later in Settings.
        # Also honor the legacy "skip_tt_login" flag from older versions.
        has_launched = (settings.get("first_launch_done")
                        or settings.get("skip_tt_login"))
        creds = api.load_credentials()

        if not has_launched and not creds:
            self._show_setup()
            return

        # Migrate: mark first_launch_done so the check is one flag going forward.
        if not settings.get("first_launch_done"):
            settings["first_launch_done"] = True
            api.save_settings(settings)

        # Returning user → try to use saved TT credentials silently.
        if creds:
            token, err = api.get_access_token(
                creds["refresh_token"], creds["secret_token"]
            )
            if token:
                self._show_portfolio(creds, token)
            else:
                # Token refresh failed — enter portfolio anyway.
                # Show a warning so the user knows to fix credentials
                # in Settings. IBKR / email tracking still work.
                print(f"[startup] TT token refresh failed: {err}", flush=True)
                self._show_portfolio(creds, "", tt_error=err)
        else:
            self._show_portfolio({}, "")

    def _show_setup(self, prefill=None):
        self._clear_all()
        screen = SetupScreen()
        if prefill:
            screen.prefill(prefill)
        screen.connected.connect(self._on_tt_connected)
        screen.skipped.connect(self._on_skip_login)
        self.addWidget(screen)
        self.setCurrentWidget(screen)

    def _on_tt_connected(self, creds, token):
        """User connected TT on setup screen — mark first launch done."""
        settings = api.load_settings()
        settings["first_launch_done"] = True
        api.save_settings(settings)
        self._show_portfolio(creds, token)

    def _on_skip_login(self):
        """User clicked Skip — mark first launch done and go to portfolio."""
        settings = api.load_settings()
        settings["first_launch_done"] = True
        api.save_settings(settings)
        self._show_portfolio({}, "")

    def _show_portfolio(self, creds, token, tt_error=None):
        self._clear_all()
        self.portfolio = PortfolioScreen(creds, token)
        self.portfolio.configure_requested.connect(self._show_configure)
        self.portfolio.strategy_clicked.connect(self._show_detail)
        self.portfolio.watchlist_requested.connect(self._show_watchlist)
        self.portfolio.risk_requested.connect(self._show_risk)
        self.addWidget(self.portfolio)
        self.setCurrentWidget(self.portfolio)
        # Show a warning banner if TT token failed so the user knows
        # to update credentials in Settings.
        if tt_error and creds:
            QTimer.singleShot(500, lambda: self.portfolio._show_tt_warning(tt_error))

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
            # Detail page may have edited the strategy (added legs, renamed,
            # closed legs, set exit plan, etc). Re-render so the home cards
            # reflect those edits — otherwise the user has to click Refresh.
            self.portfolio.reload_after_config_change()
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
    # Don't quit when the last visible window closes — the tray icon and
    # background workers (quote streamer, IBKR probe, update poller,
    # notification jobs) need to keep the process alive even after the
    # user clicks the red X on the main window.
    app.setQuitOnLastWindowClosed(False)

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
