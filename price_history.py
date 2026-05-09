"""
price_history.py — daily-close fetcher for underlying instruments.

Hits Yahoo Finance's public chart endpoint directly (no new dependency on
yfinance / pandas).  Results are cached on disk so subsequent runs don't
refetch the same date range.

API
---
get_daily_closes(root: str, start_date: date, end_date: date) -> dict[date, float]
    Returns a {date: close_price} mapping covering the requested window.
    Missing trading days (weekends, holidays) are simply absent from the dict.

Symbol mapping
--------------
TastyTrade-format roots map to Yahoo symbols:
    "SPY"   -> "SPY"           (equity, identity)
    "/ES"   -> "ES=F"          (futures, leading slash + "=F")
    "/MES"  -> "MES=F"
    "/ZB"   -> "ZB=F"
For any unmapped futures root, we strip the leading "/" and append "=F" as
a best-effort default — works for most CME / CBOT / NYMEX symbols.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Optional

import requests

import api as _api   # for _user_data_dir()

# ── Cache ────────────────────────────────────────────────────────────────────

def _cache_path() -> str:
    d = _api._user_data_dir() if hasattr(_api, "_user_data_dir") else "."
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, ".price_history_cache.json")


def _load_cache() -> dict:
    p = _cache_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        with open(_cache_path(), "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


# ── Symbol mapping ───────────────────────────────────────────────────────────

# Hand-curated for the most common futures roots. Falls back to "<root>=F".
_FUTURES_YAHOO = {
    "/ES":  "ES=F",   "/MES": "MES=F",
    "/NQ":  "NQ=F",   "/MNQ": "MNQ=F",
    "/RTY": "RTY=F",  "/M2K": "M2K=F",
    "/YM":  "YM=F",   "/MYM": "MYM=F",
    "/ZB":  "ZB=F",   "/ZN":  "ZN=F",   "/ZF": "ZF=F",   "/ZT": "ZT=F",
    "/CL":  "CL=F",   "/MCL": "MCL=F",  "/NG": "NG=F",
    "/GC":  "GC=F",   "/MGC": "MGC=F",  "/SI": "SI=F",   "/SIL": "SIL=F",
    "/HG":  "HG=F",
    "/6E":  "6E=F",   "/6J":  "6J=F",   "/6B":  "6B=F",
    "/ZC":  "ZC=F",   "/ZW":  "ZW=F",   "/ZS":  "ZS=F",
}


def _to_yahoo_symbol(root: str) -> Optional[str]:
    """Map a TT-style root ('SPY', '/ES', '/MES') to a Yahoo Finance symbol."""
    if not root:
        return None
    root = root.strip()
    if root.startswith("/"):
        return _FUTURES_YAHOO.get(root) or f"{root[1:]}=F"
    return root


# ── Fetch ────────────────────────────────────────────────────────────────────

_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"


def _fetch_yahoo(symbol: str, start: date, end: date) -> dict:
    """Hit Yahoo's chart endpoint, return {YYYY-MM-DD: close_float}."""
    # Convert to UTC midnight epoch seconds.  Add a day buffer on each end so
    # we never miss the requested boundary.
    p1 = int(datetime(start.year, start.month, start.day).timestamp()) - 86400
    p2 = int(datetime(end.year,   end.month,   end.day).timestamp()) + 86400
    params = {
        "period1":  p1,
        "period2":  p2,
        "interval": "1d",
        "events":   "history",
        "includeAdjustedClose": "true",
    }
    headers = {
        "User-Agent": "OptionsDashboard/1.0 (https://github.com)",
    }
    r = requests.get(_YAHOO_URL.format(sym=symbol),
                     params=params, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()

    out: dict[str, float] = {}
    try:
        result = data["chart"]["result"][0]
        ts = result.get("timestamp") or []
        closes = (result.get("indicators", {}).get("quote", [{}])[0]
                  .get("close") or [])
        for t, c in zip(ts, closes):
            if c is None:
                continue
            d = datetime.utcfromtimestamp(t).date().isoformat()
            out[d] = float(c)
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return out


# ── Public API ───────────────────────────────────────────────────────────────

def get_daily_closes(root: str,
                     start_date: date,
                     end_date: date) -> dict[date, float]:
    """
    Return {date: close_price} for `root` between start_date and end_date
    (inclusive).  Uses on-disk cache; missing windows are fetched lazily.
    """
    if not root or end_date < start_date:
        return {}
    yahoo = _to_yahoo_symbol(root)
    if not yahoo:
        return {}

    cache = _load_cache()
    sym_cache = cache.setdefault(yahoo, {})

    # Decide whether we need to refetch.  We fetch when:
    #   * any date in the requested window is missing from the cache, OR
    #   * the cache hasn't been refreshed today (so today's closing price
    #     becomes available after market close).
    today_iso = date.today().isoformat()
    last_fetch_iso = (cache.get("_meta", {}) or {}).get(yahoo, "")
    need_fetch = last_fetch_iso != today_iso

    # Always re-issue the fetch if even one weekday in the window is missing.
    if not need_fetch:
        d = start_date
        while d <= end_date:
            if d.weekday() < 5 and d.isoformat() not in sym_cache:
                need_fetch = True
                break
            d += timedelta(days=1)

    if need_fetch:
        try:
            fetched = _fetch_yahoo(yahoo, start_date, end_date)
            if fetched:
                sym_cache.update(fetched)
                cache.setdefault("_meta", {})[yahoo] = today_iso
                _save_cache(cache)
        except Exception:
            # Network error → fall back to whatever's in the cache.
            pass

    out: dict[date, float] = {}
    for k, v in sym_cache.items():
        try:
            d = datetime.strptime(k, "%Y-%m-%d").date()
        except Exception:
            continue
        if start_date <= d <= end_date:
            out[d] = float(v)
    return out


def get_close_on_or_before(root: str, target: date) -> Optional[float]:
    """
    Return the most recent close on or before `target`.  Useful for getting
    the "open mark date" underlying when the open date fell on a weekend.
    """
    closes = get_daily_closes(root, target - timedelta(days=10), target)
    if not closes:
        return None
    keys = [d for d in closes.keys() if d <= target]
    if not keys:
        return None
    return closes[max(keys)]
