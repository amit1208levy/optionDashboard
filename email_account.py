"""
email_account.py — build a dashboard-compatible account dict from Firestore.

Reads email-tracked positions from Firestore (populated by the Cloud Function
that processes TastyTrade trade confirmation emails) and converts them into
the same dict shape that PortfolioWorker produces for TastyTrade / IBKR
accounts.

This follows the exact same pattern as ibkr_account.py: build a raw dict
that models.Position.__init__() accepts, so the entire downstream pipeline
(quotes, Greeks, P&L, strategy grouping, charts) works identically.
"""
from __future__ import annotations

import json
import time
from typing import Optional

import requests

import models

# ── Sentinel used as the "account number" for the email-tracked entry ───────
EMAIL_ACCOUNT_NUMBER = "__email__"


def _safe_float(v, default=0.0) -> float:
    try:
        x = float(v)
        return x if x == x else default
    except (TypeError, ValueError):
        return default


def _position_doc_to_raw(doc: dict) -> Optional[dict]:
    """
    Convert one Firestore position document → TT-style raw dict for
    models.Position().

    Returns None if essential fields are missing.
    """
    symbol = doc.get("symbol")
    if not symbol:
        return None

    qty = _safe_float(doc.get("qty"))
    if qty <= 0:
        return None

    sign = doc.get("sign", 1)
    direction = "Long" if sign == 1 else "Short"

    instrument_type = doc.get("instrument_type", "Equity Option")
    multiplier = _safe_float(doc.get("multiplier"), 1.0)
    avg_open = _safe_float(doc.get("avg_open_price"))

    # Underlying symbol: use root field, prepend "/" for futures
    root = doc.get("root", "")
    normalized_root = doc.get("normalized_root", root)
    underlying = root

    # Expiration: convert YYYY-MM-DD to ISO timestamp
    expires_at = doc.get("expires_at")
    if expires_at and "T" not in str(expires_at):
        expires_at = f"{expires_at}T00:00:00Z"

    # Created at
    opened_at = doc.get("opened_at")
    if opened_at and "T" not in str(opened_at):
        opened_at = f"{opened_at}T00:00:00Z"

    return {
        "symbol":              symbol,
        "underlying-symbol":   underlying,
        "instrument-type":     instrument_type,
        "quantity":            qty,
        "quantity-direction":  direction,
        "mark-price":          0.0,         # filled later by quotes pipeline
        "close-price":         None,
        "multiplier":          multiplier,
        "average-open-price":  avg_open,
        "expires-at":          expires_at,
        "created-at":          opened_at,
    }


def fetch_email_positions(firebase_id_token: str, uid: str,
                          project_id: str = "tastytradedashboard",
                          timeout: float = 10.0) -> list[dict]:
    """
    Fetch all position documents from Firestore for the given user.
    Returns a list of raw position dicts.

    Uses the Firestore REST API (same pattern as cloud_sync.py) so we
    don't need the firebase-admin SDK in the desktop app.
    """
    base_url = (
        f"https://firestore.googleapis.com/v1/projects/{project_id}"
        f"/databases/(default)/documents"
    )
    url = f"{base_url}/users/{uid}/positions"

    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {firebase_id_token}"},
            timeout=timeout,
        )
        if not r.ok:
            print(f"[email_account] fetch positions: HTTP {r.status_code} "
                  f"{r.text[:200]}", flush=True)
            return []

        data = r.json()
        docs = data.get("documents", [])
        positions = []
        for doc in docs:
            fields = doc.get("fields", {})
            # Convert Firestore field format to plain dict
            plain = {}
            for key, val in fields.items():
                if "stringValue" in val:
                    plain[key] = val["stringValue"]
                elif "integerValue" in val:
                    plain[key] = int(val["integerValue"])
                elif "doubleValue" in val:
                    plain[key] = float(val["doubleValue"])
                elif "booleanValue" in val:
                    plain[key] = val["booleanValue"]
                elif "nullValue" in val:
                    plain[key] = None
                else:
                    plain[key] = str(val)
            positions.append(plain)
        return positions

    except Exception as e:
        print(f"[email_account] fetch positions error: {e}", flush=True)
        return []


def fetch_email_history(firebase_id_token: str, uid: str,
                        project_id: str = "tastytradedashboard",
                        timeout: float = 10.0) -> list[dict]:
    """
    Fetch closed-position history from Firestore for the given user.
    Returns a list of history entry dicts (matching .history.json format).
    """
    base_url = (
        f"https://firestore.googleapis.com/v1/projects/{project_id}"
        f"/databases/(default)/documents"
    )
    url = f"{base_url}/users/{uid}/history"

    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {firebase_id_token}"},
            timeout=timeout,
        )
        if not r.ok:
            return []

        data = r.json()
        docs = data.get("documents", [])
        history = []
        for doc in docs:
            fields = doc.get("fields", {})
            plain = {}
            for key, val in fields.items():
                if "stringValue" in val:
                    plain[key] = val["stringValue"]
                elif "integerValue" in val:
                    plain[key] = int(val["integerValue"])
                elif "doubleValue" in val:
                    plain[key] = float(val["doubleValue"])
                elif "booleanValue" in val:
                    plain[key] = val["booleanValue"]
                elif "nullValue" in val:
                    plain[key] = None
                else:
                    plain[key] = str(val)
            history.append(plain)
        return history

    except Exception as e:
        print(f"[email_account] fetch history error: {e}", flush=True)
        return []


def build_email_account(firebase_id_token: str, uid: str) -> Optional[dict]:
    """
    Build a TT-compatible account dict from Firestore email-tracked positions.

    Returns the same dict shape as ibkr_account.fetch_ibkr_account() and
    PortfolioWorker._fetch_one(), so it slots right into the existing
    rendering pipeline.

    Returns None if no positions found or fetch fails.
    """
    position_docs = fetch_email_positions(firebase_id_token, uid)

    if not position_docs:
        return None

    positions: list[models.Position] = []
    skipped = 0
    for doc in position_docs:
        raw = _position_doc_to_raw(doc)
        if raw is None:
            skipped += 1
            continue
        try:
            positions.append(models.Position(raw))
        except Exception as e:
            skipped += 1
            print(f"[email_account] skipping position {doc.get('symbol', '?')}: {e}",
                  flush=True)

    if skipped:
        print(f"[email_account] {skipped} positions couldn't be converted", flush=True)
    print(f"[email_account] built {len(positions)} positions from Firestore", flush=True)

    if not positions:
        return None

    return {
        "number":             EMAIL_ACCOUNT_NUMBER,
        "nickname":           "Email Tracker",
        "source":             "email",
        "balances":           {},              # no balance data from email
        "positions":          positions,
        "metrics":            {},
        "ytd_txns":           [],
        "year_start_net_liq": None,
        "ytd_pnl_sdk":        None,
    }
