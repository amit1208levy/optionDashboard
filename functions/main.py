"""
Firebase Cloud Function — Gmail watch handler for TastyTrade trade emails.

Triggered by Google Pub/Sub when Gmail receives a new email.  Fetches the
email, checks if it's a TastyTrade trade confirmation, parses the fills,
determines open/close from the current position ledger in Firestore, and
updates positions + history.

Deployment:
    firebase deploy --only functions
"""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from typing import Optional

import firebase_admin
from firebase_admin import firestore
from firebase_functions import https_fn, pubsub_fn, scheduler_fn
from firebase_functions.params import StringParam
import requests

from parse_trade_email import parse_trade_email, ParsedFill

# ── Firebase init ───────────────────────────────────────────────────────────

firebase_admin.initialize_app()
_db = None


def _get_db():
    """Lazy-init Firestore client (avoids timeout during CLI code analysis)."""
    global _db
    if _db is None:
        _db = firestore.client()
    return _db

# TastyTrade sender addresses (may vary — check both)
_TT_SENDERS = {
    "tradedesk@tastytrade.com",
    "tastyworks@notify.tastytrade.com",
    "noreply@tastytrade.com",
    "notifications@tastytrade.com",
}


# ── Gmail helpers ───────────────────────────────────────────────────────────

def _get_user_gmail_token(uid: str) -> Optional[str]:
    """
    Retrieve the user's Google OAuth access token from Firestore.
    The desktop app stores it at /users/{uid}/meta/gmail_auth.
    If the access token is expired, refresh it.
    """
    doc = _get_db().collection("users").document(uid).collection("meta").document("gmail_auth").get()
    if not doc.exists:
        return None

    data = doc.to_dict()
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_at = data.get("expires_at", 0)

    # Check if token needs refresh
    if time.time() >= expires_at - 60:
        if not refresh_token:
            print(f"[processTradeEmail] No refresh token for user {uid}")
            return None
        # Refresh using Google's token endpoint
        client_id = data.get("client_id", "")
        client_secret = data.get("client_secret", "")
        try:
            r = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=10,
            )
            if r.ok:
                tokens = r.json()
                access_token = tokens["access_token"]
                new_expires = time.time() + int(tokens.get("expires_in", 3600))
                # Persist refreshed token
                _get_db().collection("users").document(uid).collection("meta") \
                    .document("gmail_auth").update({
                        "access_token": access_token,
                        "expires_at": new_expires,
                    })
            else:
                print(f"[processTradeEmail] Token refresh failed: {r.status_code} {r.text[:200]}")
                return None
        except Exception as e:
            print(f"[processTradeEmail] Token refresh error: {e}")
            return None

    return access_token


def _fetch_email(access_token: str, message_id: str) -> Optional[dict]:
    """
    Fetch a single Gmail message by ID.
    Returns the message dict or None on failure.
    """
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"
    try:
        r = requests.get(
            url,
            params={"format": "full"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if r.ok:
            return r.json()
        print(f"[processTradeEmail] Gmail fetch {message_id}: HTTP {r.status_code}")
        return None
    except Exception as e:
        print(f"[processTradeEmail] Gmail fetch error: {e}")
        return None


def _extract_plain_text(message: dict) -> str:
    """Extract plain text body from a Gmail message."""
    payload = message.get("payload", {})

    # Simple single-part message
    if payload.get("mimeType", "").startswith("text/plain"):
        body_data = payload.get("body", {}).get("data", "")
        if body_data:
            return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    # Multipart — find text/plain
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            body_data = part.get("body", {}).get("data", "")
            if body_data:
                return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    # Fall back to text/html and strip tags
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/html":
            body_data = part.get("body", {}).get("data", "")
            if body_data:
                html = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
                # Basic HTML tag stripping
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text)
                return text

    return ""


def _get_sender(message: dict) -> str:
    """Extract sender email from message headers."""
    headers = message.get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == "from":
            # "Name <email@example.com>" → extract email
            val = h.get("value", "")
            m = re.search(r"<([^>]+)>", val)
            return m.group(1).lower() if m else val.lower()
    return ""


def _is_trade_email(message: dict) -> bool:
    """Check if a Gmail message is a TastyTrade trade confirmation."""
    sender = _get_sender(message)
    # Check sender
    if not any(s in sender for s in _TT_SENDERS):
        return False
    # Check subject or body for trade keywords
    headers = message.get("payload", {}).get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == "subject":
            subj = h.get("value", "").lower()
            if "order" in subj or "fill" in subj or "trade" in subj or "confirmation" in subj:
                return True
    # If sender matches, assume it's a trade email even without subject match
    return True


def _search_trade_emails(access_token: str, after_date: Optional[str] = None,
                         max_results: int = 50) -> list[str]:
    """
    Search Gmail for TastyTrade trade confirmation emails.
    Returns list of message IDs.
    """
    query = "from:(tastytrade OR tastyworks) subject:(order OR fill OR confirmation)"
    if after_date:
        query += f" after:{after_date}"

    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    try:
        r = requests.get(
            url,
            params={"q": query, "maxResults": max_results},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if r.ok:
            return [m["id"] for m in r.json().get("messages", [])]
        print(f"[processTradeEmail] Gmail search: HTTP {r.status_code}")
        return []
    except Exception as e:
        print(f"[processTradeEmail] Gmail search error: {e}")
        return []


# ── Position ledger operations ──────────────────────────────────────────────

def _infer_open_close(fill: ParsedFill, positions_ref, uid: str) -> str:
    """
    Determine if a fill is opening or closing by checking existing positions.

    Returns "open" or "close".

    Logic (per exact TT symbol):
      Bought + existing short lots → Buy to Close
      Bought + no short lots      → Buy to Open
      Sold   + existing long lots → Sell to Close
      Sold   + no long lots       → Sell to Open
    """
    symbol = fill.tt_symbol

    # Check if position exists in Firestore
    docs = positions_ref.where("symbol", "==", symbol).get()
    if not docs:
        return "open"

    existing = docs[0].to_dict()
    existing_sign = existing.get("sign", 0)

    if fill.action == "Bought" and existing_sign == -1:
        return "close"
    if fill.action == "Sold" and existing_sign == 1:
        return "close"

    return "open"


def _apply_open(fill: ParsedFill, positions_ref, uid: str) -> None:
    """Add a new position or increase an existing one."""
    symbol = fill.tt_symbol
    sign = 1 if fill.action == "Bought" else -1

    # Check if position already exists with same direction
    docs = positions_ref.where("symbol", "==", symbol).get()
    if docs:
        existing = docs[0]
        data = existing.to_dict()
        if data.get("sign") == sign:
            # Same direction — average in
            old_qty = data.get("qty", 0)
            old_price = data.get("avg_open_price", 0)
            new_qty = old_qty + fill.quantity
            # Weighted average open price
            new_price = ((old_qty * old_price) + (fill.quantity * fill.price)) / new_qty
            existing.reference.update({
                "qty": new_qty,
                "avg_open_price": round(new_price, 6),
            })
            print(f"[processTradeEmail] Increased {symbol}: {old_qty} → {new_qty}")
            return

    # New position
    positions_ref.add({
        "symbol": symbol,
        "root": fill.root if not fill.root.startswith("/") else fill.root,
        "normalized_root": fill.normalized_root,
        "instrument_type": fill.instrument_type,
        "qty": fill.quantity,
        "sign": sign,
        "avg_open_price": fill.price,
        "multiplier": fill.multiplier,
        "call_put": fill.call_put[0].upper() if fill.call_put else None,
        "strike": fill.strike,
        "expires_at": fill.expiry_iso,
        "opened_at": fill.filled_at or datetime.now(timezone.utc).isoformat(),
        "sub_symbol": fill.sub_symbol,
    })
    print(f"[processTradeEmail] Opened {symbol}: {sign * fill.quantity}")


def _apply_close(fill: ParsedFill, positions_ref, history_ref, uid: str) -> None:
    """Close or reduce an existing position. Record P&L in history."""
    symbol = fill.tt_symbol

    docs = positions_ref.where("symbol", "==", symbol).get()
    if not docs:
        print(f"[processTradeEmail] WARN: closing {symbol} but no position found — treating as open")
        _apply_open(fill, positions_ref, uid)
        return

    existing = docs[0]
    data = existing.to_dict()
    old_qty = data.get("qty", 0)
    close_qty = min(fill.quantity, old_qty)
    remaining = old_qty - close_qty

    # Compute realized P&L for the closed quantity
    open_price = data.get("avg_open_price", 0)
    close_price = fill.price
    sign = data.get("sign", 1)
    multiplier = data.get("multiplier", 1)
    pnl = sign * close_qty * multiplier * (close_price - open_price)

    # Record in history
    cp_char = data.get("call_put")
    history_ref.add({
        "symbol": symbol,
        "root": data.get("normalized_root", data.get("root", "")),
        "qty": close_qty,
        "sign": sign,
        "open_price": open_price,
        "close_price": close_price,
        "multiplier": multiplier,
        "pnl": round(pnl, 2),
        "opened_at": data.get("opened_at", ""),
        "closed_at": fill.filled_at or datetime.now(timezone.utc).isoformat(),
        "call_put": cp_char,
        "strike": data.get("strike"),
        "instrument": data.get("instrument_type", ""),
    })
    print(f"[processTradeEmail] Closed {close_qty}x {symbol}: P&L ${pnl:.2f}")

    if remaining > 0:
        existing.reference.update({"qty": remaining})
        print(f"[processTradeEmail] Reduced {symbol}: {old_qty} → {remaining}")
    else:
        existing.reference.delete()
        print(f"[processTradeEmail] Fully closed {symbol}")

    # If we closed less than the fill qty, open the excess in opposite direction
    overflow = fill.quantity - close_qty
    if overflow > 0:
        print(f"[processTradeEmail] Overflow {overflow}x {symbol} — opening in opposite direction")
        fill_copy = ParsedFill(
            action=fill.action,
            quantity=overflow,
            root=fill.root,
            sub_symbol=fill.sub_symbol,
            expiry_str=fill.expiry_str,
            call_put=fill.call_put,
            strike=fill.strike,
            price=fill.price,
            filled_at=fill.filled_at,
            tt_symbol=fill.tt_symbol,
            instrument_type=fill.instrument_type,
            multiplier=fill.multiplier,
            normalized_root=fill.normalized_root,
            expiry_iso=fill.expiry_iso,
        )
        _apply_open(fill_copy, positions_ref, uid)


# ── Cloud Function entry point ──────────────────────────────────────────────

@pubsub_fn.on_message_published(topic="gmail-notifications")
def process_trade_email(event: pubsub_fn.CloudEvent[pubsub_fn.MessagePublishedData]):
    """
    Cloud Function triggered by Pub/Sub when Gmail receives a new email.

    The Pub/Sub message contains the user's email address and history ID.
    We use the stored Gmail access token to fetch the new message(s) and
    process any trade confirmations.
    """
    # Decode Pub/Sub message
    try:
        raw_data = event.data.message.data
        if raw_data:
            data = json.loads(base64.b64decode(raw_data).decode("utf-8"))
        else:
            data = event.data.message.json or {}
    except Exception:
        print("[processTradeEmail] Could not decode Pub/Sub message")
        return

    email_address = data.get("emailAddress", "")
    history_id = data.get("historyId")

    print(f"[processTradeEmail] Gmail notification for {email_address}, historyId={history_id}")

    # Find the user by email
    users_ref = _get_db().collection("users")
    user_docs = users_ref.where("email", "==", email_address.lower()).get()
    if not user_docs:
        print(f"[processTradeEmail] No user registered for {email_address}")
        return

    user_doc = user_docs[0]
    uid = user_doc.id

    # Get the user's Gmail access token
    access_token = _get_user_gmail_token(uid)
    if not access_token:
        print(f"[processTradeEmail] No Gmail token for user {uid}")
        return

    # Fetch recent messages using history API
    positions_ref = users_ref.document(uid).collection("positions")
    history_ref = users_ref.document(uid).collection("history")
    meta_ref = users_ref.document(uid).collection("meta")

    # Get the last processed history ID
    sync_doc = meta_ref.document("sync_state").get()
    last_history_id = None
    processed_emails = set()
    if sync_doc.exists:
        sync_data = sync_doc.to_dict()
        last_history_id = sync_data.get("last_history_id")
        processed_emails = set(sync_data.get("processed_emails", []))

    # Use Gmail history API to get new messages since last check
    new_message_ids = []
    if last_history_id:
        try:
            r = requests.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/history",
                params={
                    "startHistoryId": last_history_id,
                    "historyTypes": "messageAdded",
                },
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if r.ok:
                for h in r.json().get("history", []):
                    for msg in h.get("messagesAdded", []):
                        mid = msg.get("message", {}).get("id")
                        if mid:
                            new_message_ids.append(mid)
        except Exception as e:
            print(f"[processTradeEmail] History API error: {e}")

    if not new_message_ids:
        # Fallback: search for recent trade emails
        new_message_ids = _search_trade_emails(access_token, max_results=10)

    print(f"[processTradeEmail] Found {len(new_message_ids)} new messages to check")

    processed_count = 0
    for msg_id in new_message_ids:
        if msg_id in processed_emails:
            continue

        message = _fetch_email(access_token, msg_id)
        if not message:
            continue

        if not _is_trade_email(message):
            # Not a trade email — mark as processed and skip
            processed_emails.add(msg_id)
            continue

        # Extract and parse the email body
        body = _extract_plain_text(message)
        if not body:
            print(f"[processTradeEmail] Empty body for message {msg_id}")
            processed_emails.add(msg_id)
            continue

        parsed = parse_trade_email(body)
        if parsed.parse_errors:
            print(f"[processTradeEmail] Parse errors for {msg_id}: {parsed.parse_errors}")
            processed_emails.add(msg_id)
            continue

        if not parsed.fills:
            processed_emails.add(msg_id)
            continue

        # Process each fill
        for fill in parsed.fills:
            action = _infer_open_close(fill, positions_ref, uid)
            if action == "open":
                _apply_open(fill, positions_ref, uid)
            else:
                _apply_close(fill, positions_ref, history_ref, uid)

        processed_emails.add(msg_id)
        processed_count += 1
        print(f"[processTradeEmail] Processed order #{parsed.order_id}: "
              f"{len(parsed.fills)} fills")

    # Update sync state
    meta_ref.document("sync_state").set({
        "last_history_id": history_id,
        "processed_emails": list(processed_emails)[-500:],  # Keep last 500 for dedup
        "last_sync": datetime.now(timezone.utc).isoformat(),
        "total_processed": processed_count,
    }, merge=True)

    print(f"[processTradeEmail] Done. Processed {processed_count} trade emails.")


@scheduler_fn.on_schedule(schedule="0 0 */6 * *")
def renew_gmail_watch(event: scheduler_fn.ScheduledEvent):
    """
    Cloud Function triggered by Cloud Scheduler every 6 days to renew
    Gmail watch() subscriptions for all users with email tracking enabled.
    """
    users_ref = _get_db().collection("users")
    # Find all users with email tracking enabled
    docs = users_ref.stream()

    renewed = 0
    for doc in docs:
        uid = doc.id
        meta_doc = users_ref.document(uid).collection("meta").document("gmail_auth").get()
        if not meta_doc.exists:
            continue

        data = meta_doc.to_dict()
        if not data.get("email_tracking_enabled"):
            continue

        access_token = _get_user_gmail_token(uid)
        if not access_token:
            continue

        # Renew watch
        try:
            r = requests.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/watch",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "topicName": f"projects/{firebase_admin.get_app().project_id}/topics/gmail-notifications",
                    "labelIds": ["INBOX"],
                },
                timeout=10,
            )
            if r.ok:
                result = r.json()
                meta_doc.reference.update({
                    "watch_expiry": result.get("expiration"),
                })
                renewed += 1
                print(f"[renewGmailWatch] Renewed watch for user {uid}")
            else:
                print(f"[renewGmailWatch] Failed for {uid}: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"[renewGmailWatch] Error for {uid}: {e}")

    print(f"[renewGmailWatch] Renewed {renewed} watches")


@https_fn.on_request()
def historical_import(request: https_fn.Request) -> https_fn.Response:
    """
    HTTP Cloud Function to trigger historical email import for a user.
    Called by the desktop app when user clicks "Import History".

    Expects JSON body: {"uid": "...", "months_back": 6}
    """
    data = request.get_json(silent=True) or {}
    uid = data.get("uid")
    months_back = int(data.get("months_back", 6))

    if not uid:
        return https_fn.Response(json.dumps({"error": "uid required"}), status=400,
                                  content_type="application/json")

    access_token = _get_user_gmail_token(uid)
    if not access_token:
        return https_fn.Response(json.dumps({"error": "No Gmail token — sign in first"}),
                                  status=401, content_type="application/json")

    # Calculate date range
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=months_back * 30)
    after_date = cutoff.strftime("%Y/%m/%d")

    # Search for all trade emails in range
    message_ids = _search_trade_emails(access_token, after_date=after_date, max_results=500)
    print(f"[historicalImport] Found {len(message_ids)} emails to process for {uid}")

    positions_ref = _get_db().collection("users").document(uid).collection("positions")
    history_ref = _get_db().collection("users").document(uid).collection("history")
    meta_ref = _get_db().collection("users").document(uid).collection("meta")

    # Clear existing positions for fresh import
    for doc in positions_ref.stream():
        doc.reference.delete()
    for doc in history_ref.stream():
        doc.reference.delete()

    # Process in chronological order (oldest first)
    emails_with_time = []
    for msg_id in message_ids:
        message = _fetch_email(access_token, msg_id)
        if not message or not _is_trade_email(message):
            continue
        # Get internal date for sorting
        internal_date = int(message.get("internalDate", "0"))
        body = _extract_plain_text(message)
        if body:
            emails_with_time.append((internal_date, msg_id, body))

    # Sort chronologically (oldest first)
    emails_with_time.sort(key=lambda x: x[0])

    processed = 0
    for _, msg_id, body in emails_with_time:
        parsed = parse_trade_email(body)
        if not parsed.fills:
            continue

        for fill in parsed.fills:
            action = _infer_open_close(fill, positions_ref, uid)
            if action == "open":
                _apply_open(fill, positions_ref, uid)
            else:
                _apply_close(fill, positions_ref, history_ref, uid)
        processed += 1

    # Update sync state
    meta_ref.document("sync_state").set({
        "last_sync": datetime.now(timezone.utc).isoformat(),
        "import_complete": True,
        "import_emails_processed": processed,
    }, merge=True)

    return https_fn.Response(
        json.dumps({"success": True, "emails_processed": processed,
                     "total_found": len(message_ids)}),
        status=200, content_type="application/json",
    )
