"""TastyTrade API — auth, credentials, data fetch."""
import json
import os
import requests

BASE = "https://api.tastyworks.com"
UA   = "options-dashboard/1.0"
HERE = os.path.dirname(os.path.abspath(__file__))

CREDENTIALS_FILE    = os.path.join(HERE, ".credentials.json")
GROUPS_FILE         = os.path.join(HERE, ".groups.json")
ACCOUNT_NAMES_FILE  = os.path.join(HERE, ".account_names.json")
STRATEGIES_FILE     = os.path.join(HERE, ".strategies.json")
HISTORY_FILE        = os.path.join(HERE, ".history.json")
SNAPSHOTS_FILE      = os.path.join(HERE, ".snapshots.json")
SETTINGS_FILE       = os.path.join(HERE, ".settings.json")


# ── Credentials ─────────────────────────────────────────────────────────────

def load_credentials():
    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE) as f:
            return json.load(f)
    return None


def save_credentials(data):
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def clear_credentials():
    if os.path.exists(CREDENTIALS_FILE):
        os.remove(CREDENTIALS_FILE)


# ── Groups: assignments + names ─────────────────────────────────────────────
# File shape: {"assignments": {symbol: group_id}, "names": {group_id: name}}

def load_groups():
    if os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE) as f:
                data = json.load(f)
            # Migrate old flat {gid: name} format
            if data and all(not isinstance(v, dict) for v in data.values()):
                return {"assignments": {}, "names": data}
            return {
                "assignments": data.get("assignments", {}) or {},
                "names":       data.get("names", {}) or {},
            }
        except Exception:
            pass
    return {"assignments": {}, "names": {}}


def save_groups(assignments, names):
    with open(GROUPS_FILE, "w") as f:
        json.dump({"assignments": assignments, "names": names}, f, indent=2)


# ── Account name overrides ──────────────────────────────────────────────────

def load_account_names():
    if os.path.exists(ACCOUNT_NAMES_FILE):
        try:
            with open(ACCOUNT_NAMES_FILE) as f:
                return json.load(f) or {}
        except Exception:
            pass
    return {}


def save_account_names(names):
    with open(ACCOUNT_NAMES_FILE, "w") as f:
        json.dump(names, f, indent=2)


# ── Strategy instances (keyed per account) ──────────────────────────────────
# Shape: {account_number: [{"id","template","name","legs","created_at","notes"}]}

def load_strategies():
    if os.path.exists(STRATEGIES_FILE):
        try:
            with open(STRATEGIES_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_strategies(data):
    with open(STRATEGIES_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Closed-leg history (keyed per account) ──────────────────────────────────
# Shape: {account_number: [history_entry, ...]}

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_history(data):
    with open(HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Position snapshots (used to detect closures) ────────────────────────────
# Shape: {account_number: {symbol: {"qty": float, "sign": int,
#                                   "open_price": float, "mark": float,
#                                   "multiplier": float, "opened_at": iso}}}

def load_snapshots():
    if os.path.exists(SNAPSHOTS_FILE):
        try:
            with open(SNAPSHOTS_FILE) as f:
                return json.load(f) or {}
        except Exception:
            pass
    return {}


def save_snapshots(snapshots):
    with open(SNAPSHOTS_FILE, "w") as f:
        json.dump(snapshots, f, indent=2)


# ── Settings (small key/value blob) ─────────────────────────────────────────

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


# ── API ──────────────────────────────────────────────────────────────────────

def auth_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }


def get_access_token(refresh_token, client_secret):
    resp = requests.post(
        f"{BASE}/oauth/token",
        json={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token.strip(),
            "client_secret": client_secret.strip(),
        },
        headers={"Content-Type": "application/json", "User-Agent": UA},
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.json().get("access_token"), None
    try:
        err = resp.json()
        desc = err.get("error_description") or err.get("error_code") or resp.text[:200]
    except Exception:
        desc = resp.text[:200]
    return None, desc


def list_accounts(token):
    r = requests.get(f"{BASE}/customers/me/accounts", headers=auth_headers(token), timeout=10)
    r.raise_for_status()
    return [i.get("account", i) for i in r.json().get("data", {}).get("items", [])]


def get_balances(token, account_number):
    r = requests.get(
        f"{BASE}/accounts/{account_number}/balances",
        headers=auth_headers(token), timeout=10,
    )
    return r.json().get("data", {}) if r.status_code == 200 else {}


def get_positions(token, account_number):
    r = requests.get(
        f"{BASE}/accounts/{account_number}/positions",
        headers=auth_headers(token), timeout=10,
    )
    return r.json().get("data", {}).get("items", []) if r.status_code == 200 else []


def get_transactions(token, account_number, per_page=250, max_pages=40):
    """Paginate through /accounts/{num}/transactions. Returns all items."""
    items = []
    page = 0
    while page < max_pages:
        try:
            r = requests.get(
                f"{BASE}/accounts/{account_number}/transactions",
                headers=auth_headers(token),
                params={"per-page": per_page, "page-offset": page},
                timeout=30,
            )
        except requests.exceptions.RequestException:
            break
        if r.status_code != 200:
            break
        batch = r.json().get("data", {}).get("items", []) or []
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return items


def get_year_start_net_liq(token, account_number):
    """
    Return the net-liquidating-value at the start of the current calendar year,
    taken from the last trading-day EOD balance-snapshot on or before Jan 1.

    Walks back up to 10 days to handle weekends / holidays (New Year's Day
    is never a trading day).  Returns None if no snapshot found.
    """
    from datetime import date, timedelta
    year = date.today().year
    for days_back in range(0, 12):
        target = date(year, 1, 1) - timedelta(days=days_back)
        try:
            r = requests.get(
                f"{BASE}/accounts/{account_number}/balance-snapshots",
                headers=auth_headers(token),
                params={
                    "snapshot-date": target.isoformat(),
                    "time-of-day":   "EOD",
                },
                timeout=10,
            )
            if r.status_code != 200:
                continue
            items = r.json().get("data", {}).get("items", []) or []
            if not items:
                continue
            val = items[0].get("net-liquidating-value")
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        except requests.exceptions.RequestException:
            continue
    return None


def get_transactions_ytd(token, account_number):
    """
    Fetch all Trade / Receive-Deliver transactions for the current calendar year.
    Uses start-date filtering so only 1-5 API pages are needed instead of 40.
    Returns a list of raw transaction dicts.
    """
    from datetime import date
    start = f"{date.today().year}-01-01"
    items = []
    page  = 0
    while page < 20:          # 20 × 250 = 5 000 — more than enough for one year
        try:
            r = requests.get(
                f"{BASE}/accounts/{account_number}/transactions",
                headers=auth_headers(token),
                params={"per-page": 250, "page-offset": page,
                        "start-date": start},
                timeout=15,
            )
        except requests.exceptions.RequestException:
            break
        if r.status_code != 200:
            break
        batch = r.json().get("data", {}).get("items", []) or []
        items.extend(batch)
        if len(batch) < 250:
            break
        page += 1
    return items


def get_market_metrics(token, symbols):
    """
    Returns {symbol: metrics_dict} with fields like
      implied-volatility-index, implied-volatility-index-rank,
      implied-volatility-percentile, beta, historical-volatility-30-day,
      liquidity-rating, earnings, etc.
    """
    if not symbols:
        return {}
    # API accepts up to ~100 symbols per call
    out = {}
    unique = list({s for s in symbols if s})
    for i in range(0, len(unique), 100):
        chunk = unique[i:i+100]
        try:
            r = requests.get(
                f"{BASE}/market-metrics",
                headers=auth_headers(token),
                params={"symbols": ",".join(chunk)},
                timeout=15,
            )
        except requests.exceptions.RequestException:
            continue
        if r.status_code != 200:
            continue
        for it in r.json().get("data", {}).get("items", []) or []:
            sym = it.get("symbol")
            if sym:
                out[sym] = it
                # Also key by stripped-slash form (e.g. "/MES" → "MES") so
                # callers can look up by bare root without caring which style
                # the API echoed back.
                if sym.startswith("/"):
                    out[sym[1:]] = it
    return out


def search_instruments(token, query, per_page=10):
    """
    Type-ahead symbol search.  Returns a list of dicts:
      [{"symbol": str, "description": str, "type": "Equity"|"ETF"|"Index"}, ...]

    Uses TastyTrade /instruments/equities?symbol-starts-with=<q>.
    Returns [] silently on any error so the UI can degrade gracefully.
    """
    if not query:
        return []
    try:
        r = requests.get(
            f"{BASE}/instruments/equities",
            headers=auth_headers(token),
            params={"symbol-starts-with": query.upper(), "per-page": per_page},
            timeout=5,
        )
        if r.status_code != 200:
            return []
        out = []
        for item in r.json().get("data", {}).get("items", []) or []:
            sym  = item.get("symbol") or ""
            desc = (item.get("description") or
                    item.get("short-description") or "")
            kind = item.get("instrument-type") or "Equity"
            if sym:
                out.append({"symbol": sym, "description": desc, "type": kind})
        return out
    except Exception:
        return []


def get_futures_active_contracts(token, roots):
    """
    Resolve futures root codes to their active front-month contract symbols.
    E.g. ["ES", "MES"] → {"ES": "/ESM26", "MES": "/MESM26"}.
    Returns {} silently on any error.
    """
    if not roots:
        return {}
    try:
        from datetime import date
        today = date.today().isoformat()
        params = [("product-code[]", r) for r in roots]
        r = requests.get(
            f"{BASE}/instruments/futures",
            headers=auth_headers(token),
            params=params,
            timeout=10,
        )
        if r.status_code != 200:
            return {}
        items = r.json().get("data", {}).get("items", []) or []
        # Group contracts by product-code, picking the nearest active expiry
        by_root = {}
        for item in items:
            root = item.get("product-code") or ""
            sym  = item.get("symbol") or ""
            exp  = item.get("expiration-date") or ""
            is_active = bool(item.get("active-month") or item.get("is-front-month"))
            if not root or not sym:
                continue
            by_root.setdefault(root, []).append((is_active, exp, sym))
        out = {}
        for root, contracts in by_root.items():
            # Prefer active-month flag; among ties, pick soonest expiry
            contracts.sort(key=lambda x: (not x[0], x[1]))
            for _active, exp, sym in contracts:
                if exp >= today:
                    out[root] = sym
                    break
            if root not in out and contracts:
                out[root] = contracts[0][2]
        return out
    except Exception:
        return {}


def get_option_chain(token, symbol):
    """
    Fetch the nested option chain for an equity/ETF symbol.
    Returns a list of expirations, each with strikes + call/put OCC symbols.
    Silently returns [] on error.
    """
    if not symbol:
        return []
    try:
        r = requests.get(
            f"{BASE}/option-chains/{symbol}/nested",
            headers=auth_headers(token), timeout=15,
        )
        if r.status_code != 200:
            return []
        items = r.json().get("data", {}).get("items", []) or []
        if not items:
            return []
        return items[0].get("expirations", []) or []
    except requests.exceptions.RequestException:
        return []


def get_market_data(token, equity_options=None, future_options=None, equities=None, futures=None):
    """
    Snapshot quotes + Greeks for options and stocks/futures. Returns {symbol: quote dict}.
    Silently returns {} on error (Greeks are a nice-to-have).
    """
    params = {}
    if equity_options:
        params["equity-option"] = ",".join(equity_options)
    if future_options:
        params["future-option"] = ",".join(future_options)
    if equities:
        params["equity"] = ",".join(equities)
    if futures:
        params["future"] = ",".join(futures)
    if not params:
        return {}
    try:
        r = requests.get(
            f"{BASE}/market-data/by-type",
            headers=auth_headers(token),
            params=params,
            timeout=15,
        )
        if r.status_code != 200:
            return {}
        items = r.json().get("data", {}).get("items", [])
        return {it.get("symbol", ""): it for it in items}
    except requests.exceptions.RequestException:
        return {}
