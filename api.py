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


def get_market_data(token, equity_options=None, future_options=None):
    """
    Snapshot quotes + Greeks for options. Returns {symbol: quote dict}.
    Silently returns {} on error (Greeks are a nice-to-have).
    """
    params = {}
    if equity_options:
        params["equity-option"] = ",".join(equity_options)
    if future_options:
        params["future-option"] = ",".join(future_options)
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
