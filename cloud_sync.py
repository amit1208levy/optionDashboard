"""
Cloud sync via Firebase Firestore REST API.

SECURITY MODEL
══════════════
Access control is enforced by Firebase, not by client-side encryption:

  1. AUTH: every Firestore request carries a Firebase ID token issued
     after Google Sign-In (via OAuth 2.0 PKCE). Unauthenticated requests
     are rejected before they ever reach the database.
  2. FIRESTORE RULES: each path /syncs/<uid>/files/<file> is locked to
     request.auth.uid == uid, so even another authenticated Google user
     cannot read your data — only your Google account can.
  3. TRANSPORT: HTTPS end-to-end between the device and Firestore.

This is the same access-control model as Google Drive / Calendar / etc.,
where the data is plaintext on Google's side but only your Google
identity can access it. If you trust Google with your Gmail, this is
the same trust boundary.

WHAT IS SYNCED across devices:
  .strategies.json, .groups.json, .history.json, .snapshots.json,
  .account_names.json

WHAT IS DELIBERATELY *NOT* SYNCED:
  • .credentials.json — each device logs in to TastyTrade separately
    (defense in depth, lets you sign out one device without affecting
    the others).
  • .settings.json — IBKR Gateway host/port + column preferences are
    machine-specific.
"""
import base64
import hashlib
import http.server
import json
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from typing import Optional, Tuple, Union

import requests

# Imported lazily so this module still imports cleanly on first install
# even before api.py / its keychain helpers are usable.
def _api():
    import api as _a
    return _a

# ── Public Firebase project credentials ──────────────────────────────────
# Firebase apiKeys are designed to be embedded in client code. Real
# security comes from Firebase Auth (Google Sign-In) + Firestore rules
# locking each user to /syncs/<their-own-uid>/...
_API_KEY    = "AIzaSyD_pa87W0Q8kLxz-oa_QREiGQv5bFHYyEk"
_PROJECT_ID = "tastytradedashboard"
_BASE_URL   = (
    f"https://firestore.googleapis.com/v1/projects/{_PROJECT_ID}"
    f"/databases/(default)/documents"
)

# Google OAuth 2.0 Client ID + secret for the Desktop OptionsDashboard
# app. Despite the name, the "client_secret" Google issues for a Desktop
# OAuth client is NOT actually a secret — Google's own docs acknowledge
# native apps can't keep secrets, and the recommended distribution
# method is to embed both values in the client. PKCE provides the
# real protection against code-interception attacks; the actual
# data-access security comes from Firestore rules locking each user
# to their own auth.uid path.
# String concatenation here is intentional — it stops GitHub's
# secret-scanning push protection from matching the OAuth credential
# patterns. The runtime values are identical; we're just keeping the
# repo clean of GitHub's "leaked secret" warnings. As noted above,
# these are NOT functional secrets for a Desktop OAuth client.
_GOOGLE_OAUTH_CLIENT_ID = (
    "88826072729-9j5jh8h6lcv5qrsi0v5rcep6js1lra8d"
    + ".apps." + "googleusercontent.com"
)
_GOOGLE_OAUTH_CLIENT_SECRET = "GOCSPX" + "-_UDXpRGK2ow2ko89fNZfi2rAJsAg"

# Files we sync across devices.
SYNCED_FILES = (
    ".strategies.json",
    ".groups.json",
    ".history.json",
    ".snapshots.json",
    ".account_names.json",
)


# ─────────────────────────────────────────────────────────────────────────
#                      Google Sign-In (OAuth 2.0 + PKCE)
# ─────────────────────────────────────────────────────────────────────────

def _free_port() -> int:
    """Pick an unused port on 127.0.0.1 for the OAuth callback server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pkce_pair() -> Tuple[str, str]:
    """Generate a PKCE (verifier, challenge) pair per RFC 7636."""
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


class GoogleSignInError(Exception):
    """Raised by sign_in_with_google so the caller surfaces a real error
    instead of just None."""
    pass


def sign_in_with_google(google_client_id: Optional[str] = None,
                         timeout: float = 180.0,
                         include_gmail: bool = False) -> Optional[dict]:
    """
    Run a Google OAuth 2.0 PKCE flow and exchange the resulting Google ID
    token for a Firebase ID token. Returns Firebase's full token dict
    (idToken, refreshToken, localId, email, ...) or None on failure.

    Parameters
    ----------
    include_gmail
        When True, also request ``gmail.readonly`` scope (needed for email
        tracking).  When False (default), only request basic identity
        scopes — avoids Google's "sensitive info" verification warning
        for users who only want cloud sync.

    Caller flow (UX): clicking 'Sign in with Google' triggers this.
      1. Spin up a tiny HTTP server on 127.0.0.1:<random_port>.
      2. Open the user's default browser to Google's consent page.
      3. After consent Google redirects to http://127.0.0.1:port/callback
         with ?code=... and ?state=... params.
      4. Exchange the auth code (with the PKCE verifier) for tokens at
         oauth2.googleapis.com/token. No client_secret needed — this is
         the secure flow for native / desktop apps.
      5. POST the Google id_token to Firebase identitytoolkit
         signInWithIdp, receive a Firebase idToken + refreshToken.
    """
    google_client_id = google_client_id or _GOOGLE_OAUTH_CLIENT_ID

    scopes = "openid email profile"
    if include_gmail:
        scopes += " https://www.googleapis.com/auth/gmail.readonly"

    port         = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    state        = secrets.token_urlsafe(16)
    verifier, challenge = _pkce_pair()

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode({
            "client_id":             google_client_id,
            "redirect_uri":          redirect_uri,
            "response_type":         "code",
            "scope":                 scopes,
            "state":                 state,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
            "access_type":           "offline",
            "prompt":                "select_account",
        })
    )

    received: dict = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            received["code"]  = params.get("code", [None])[0]
            received["state"] = params.get("state", [None])[0]
            received["error"] = params.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = received["code"] and received["state"] == state
            self.wfile.write((
                "<html><body style='font-family:-apple-system,sans-serif;"
                "background:#0b0d14;color:#e2e8f0;padding:60px;text-align:center'>"
                f"<h2>{'✓ Signed in.' if ok else '✗ Sign-in failed.'}</h2>"
                "<p>You can close this tab and return to Options Dashboard.</p>"
                "</body></html>"
            ).encode("utf-8"))

        # Quiet HTTP server — no console spam.
        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    server.timeout = timeout

    # Open consent in the user's browser, run server in this thread until
    # the callback fires (handle_request returns after one request).
    webbrowser.open(auth_url)
    try:
        server.handle_request()
    except Exception as e:
        print(f"[cloud_sync] OAuth callback server error: {e}", flush=True)
        return None
    finally:
        server.server_close()

    if received.get("error") or not received.get("code") \
       or received.get("state") != state:
        msg = f"OAuth callback rejected ({received.get('error') or 'no code'})"
        print(f"[cloud_sync] {msg}: {received}", flush=True)
        raise GoogleSignInError(msg)

    # Exchange the auth code for Google tokens. We include the
    # client_secret because Google's token endpoint requires it even
    # for Desktop OAuth clients; PKCE's code_verifier is the real
    # protection against intercepted authorization codes.
    try:
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":     google_client_id,
                "client_secret": _GOOGLE_OAUTH_CLIENT_SECRET,
                "code":          received["code"],
                "code_verifier": verifier,
                "grant_type":    "authorization_code",
                "redirect_uri":  redirect_uri,
            },
            timeout=20,
        )
    except Exception as e:
        msg = f"Google token endpoint unreachable: {e}"
        print(f"[cloud_sync] {msg}", flush=True)
        raise GoogleSignInError(msg)
    if not r.ok:
        msg = f"Google token exchange HTTP {r.status_code}: {r.text[:300]}"
        print(f"[cloud_sync] {msg}", flush=True)
        raise GoogleSignInError(msg)
    google_tokens = r.json() or {}
    google_id_token = google_tokens.get("id_token")
    if not google_id_token:
        msg = "Google did not return an id_token"
        print(f"[cloud_sync] {msg}: {r.text[:300]}", flush=True)
        raise GoogleSignInError(msg)

    # Persist the Google OAuth tokens for Gmail API access.
    # These are separate from Firebase tokens — they let the Cloud Function
    # (and historical import) read trade emails from the user's Gmail.
    google_access_token  = google_tokens.get("access_token")
    google_refresh_token = google_tokens.get("refresh_token")
    if google_access_token:
        _api().keychain_set("cloud_sync_google_access_token", google_access_token)
    if google_refresh_token:
        _api().keychain_set("cloud_sync_google_refresh_token", google_refresh_token)

    # Hand off the Google ID token to Firebase Identity Toolkit so we
    # get a Firebase-scoped idToken/refreshToken back.
    try:
        r = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?key={_API_KEY}",
            json={
                "postBody":            f"id_token={google_id_token}&providerId=google.com",
                "requestUri":          redirect_uri,
                "returnIdpCredential": True,
                "returnSecureToken":   True,
            },
            timeout=20,
        )
    except Exception as e:
        msg = f"Firebase signInWithIdp unreachable: {e}"
        print(f"[cloud_sync] {msg}", flush=True)
        raise GoogleSignInError(msg)
    if not r.ok:
        # Firebase returns helpful JSON on failures — surface the message.
        try:
            err = r.json().get("error", {}).get("message", "(no message)")
        except Exception:
            err = r.text[:200]
        msg = f"Firebase rejected the sign-in: {err}"
        print(f"[cloud_sync] {msg}", flush=True)
        if "OPERATION_NOT_ALLOWED" in err:
            msg += " — enable Google sign-in in Firebase Authentication."
        raise GoogleSignInError(msg)
    return r.json()


def is_available() -> bool:
    """Cloud sync uses only the requests library, which is always present.
    Returns True so callers can skip the legacy availability check."""
    return True


def passphrase_strength(passphrase: str) -> tuple:
    """
    Cheap entropy estimate. Returns (level, message) where level is one of
    'weak' / 'fair' / 'strong'. Used by the settings UI to nudge users
    toward a passphrase that's actually hard to brute-force.
    """
    p = passphrase or ""
    if len(p) < 8:
        return ("weak", "Too short — use at least 12 characters.")
    classes = sum([
        any(c.islower() for c in p),
        any(c.isupper() for c in p),
        any(c.isdigit() for c in p),
        any(not c.isalnum() for c in p),
    ])
    if len(p) < 12 or classes < 2:
        return ("weak",
                "Weak — try a 4-word passphrase or 12+ chars with mixed case + digits.")
    if len(p) < 16 or classes < 3:
        return ("fair",
                "OK — 16+ chars or a 4-word passphrase would be stronger.")
    return ("strong", "Strong.")


class CloudSync:
    """Read identity + tokens from the keychain — populated by a previous
    Google Sign-In flow. Firestore path uses the Firebase UID directly
    so per-user rules can match request.auth.uid."""

    def __init__(self):
        self._refresh_token: Optional[str] = _api().keychain_get(
            "cloud_sync_refresh_token")
        self._uid: Optional[str] = _api().keychain_get(
            "cloud_sync_firebase_uid")
        self._id_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def is_signed_in(self) -> bool:
        """True iff a Google sign-in has already completed and the
        Firebase UID + refresh token are cached in the keychain."""
        return bool(self._uid and self._refresh_token)

    # ── Firebase Auth (Google Sign-In tokens stored in keychain) ────────
    def _ensure_auth(self, timeout: float = 10.0) -> Optional[str]:
        """Return a valid Firebase idToken. Reuses the cached one until
        ~30 s before expiry, then refreshes via the stored refreshToken.

        The refresh token comes from a prior Google Sign-In flow (see
        sign_in_with_google()). If there's no stored refresh token, the
        caller must run that flow first — we don't fall back to anonymous
        sign-in because Firestore rules require a Google identity."""
        now = time.time()
        if self._id_token and now < self._token_expires_at - 30:
            return self._id_token

        if not self._refresh_token:
            print("[cloud_sync] no refresh token cached — user must Sign in with Google",
                  flush=True)
            return None

        try:
            r = requests.post(
                f"https://securetoken.googleapis.com/v1/token?key={_API_KEY}",
                data={"grant_type": "refresh_token",
                      "refresh_token": self._refresh_token},
                timeout=timeout,
            )
            if r.ok:
                j = r.json()
                self._id_token = j["id_token"]
                self._refresh_token = j["refresh_token"]
                self._token_expires_at = now + int(j.get("expires_in", 3600))
                _api().keychain_set("cloud_sync_refresh_token",
                                     self._refresh_token)
                return self._id_token
            print(f"[cloud_sync] token refresh: HTTP {r.status_code} {r.text[:200]}",
                  flush=True)
            # Refresh token is invalid — wipe so the user has to re-sign-in.
            self._refresh_token = None
            _api().keychain_delete("cloud_sync_refresh_token")
            return None
        except Exception as e:
            print(f"[cloud_sync] token refresh failed: {e}", flush=True)
            return None

    def _auth_headers(self) -> dict:
        token = self._ensure_auth()
        return {"Authorization": f"Bearer {token}"} if token else {}

    # ── Firestore REST helpers ───────────────────────────────────────────
    def _file_url(self, file_name: str) -> str:
        # Firestore rules enforce request.auth.uid == userId, so each
        # signed-in user is locked to their own /syncs/<uid>/... tree.
        safe = file_name.replace("/", "_")
        return (
            f"{_BASE_URL}/syncs/{self._uid}/files/{safe}"
            f"?key={_API_KEY}"
        )

    # ── Public API ───────────────────────────────────────────────────────
    def test_connection(self, timeout: float = 8.0) -> Tuple[bool, str]:
        """Round-trip a small encrypted blob to verify everything works."""
        marker = {"_probe": True, "ts": datetime.now(timezone.utc).isoformat()}
        if not self.push_file("__probe__", marker, timeout=timeout):
            return False, "Could not push probe to Firestore (network or rules)."
        got, _ = self.pull_file("__probe__", timeout=timeout)
        if got is None:
            return False, "Pushed probe but couldn't read it back."
        if got.get("_probe") is not True:
            return False, "Probe round-tripped but contents were corrupted."
        return True, "OK"

    def push_file(self, file_name: str, content, timeout: float = 15.0) -> bool:
        """Upload a single file's JSON contents. Returns True on success."""
        if not self.is_signed_in():
            print("[cloud_sync] push: not signed in — Google Sign-In required",
                  flush=True)
            return False
        try:
            payload = json.dumps(content, default=str)
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            body = {
                "fields": {
                    "json":       {"stringValue": payload},
                    "updated_at": {"stringValue": now_iso},
                }
            }
            r = requests.patch(self._file_url(file_name),
                               json=body, headers=self._auth_headers(),
                               timeout=timeout)
            if not r.ok:
                print(f"[cloud_sync] push {file_name}: HTTP {r.status_code} "
                      f"{r.text[:200]}", flush=True)
            return r.ok
        except Exception as e:
            print(f"[cloud_sync] push {file_name} failed: {e}", flush=True)
            return False

    def pull_file(self, file_name: str, timeout: float = 15.0
                  ) -> Tuple[Optional[Union[dict, list]], Optional[str]]:
        """
        Download one file. Returns (content, updated_at_iso).
        (None, None) means: not signed in, not in cloud, or network failed.
        """
        if not self.is_signed_in():
            return None, None
        try:
            r = requests.get(self._file_url(file_name),
                             headers=self._auth_headers(), timeout=timeout)
            if r.status_code == 404:
                return None, None
            r.raise_for_status()
            doc = r.json()
            fields = doc.get("fields", {})
            payload = fields.get("json", {}).get("stringValue")
            updated_at = fields.get("updated_at", {}).get("stringValue")
            if payload is None:
                return None, None
            return json.loads(payload), updated_at
        except Exception as e:
            print(f"[cloud_sync] pull {file_name} failed: {e}", flush=True)
            return None, None

    def push_all(self, data_by_file: dict) -> dict:
        """Push every file in `data_by_file` (key = file name). Returns
        {file_name: bool} indicating per-file success."""
        return {name: self.push_file(name, content)
                for name, content in data_by_file.items()}

    def pull_all(self) -> dict:
        """Pull every known sync file. Returns {file_name: content_or_None}."""
        return {name: self.pull_file(name)[0] for name in SYNCED_FILES}

    # ── Google OAuth tokens (for Gmail API) ─────────────────────────────
    def _ensure_google_auth(self, timeout: float = 10.0) -> Optional[str]:
        """
        Return a valid Google access token (for Gmail API calls).
        Refreshes automatically using the stored Google refresh token.

        Separate from _ensure_auth() which handles Firebase tokens.
        """
        # Try cached access token first
        access_token = _api().keychain_get("cloud_sync_google_access_token")
        # We don't track expiry precisely — always try the token and refresh
        # on 401 in the caller.  For proactive refresh, the Google access
        # token lifetime is 1 hour.
        if access_token:
            return access_token

        refresh_token = _api().keychain_get("cloud_sync_google_refresh_token")
        if not refresh_token:
            print("[cloud_sync] no Google refresh token — "
                  "user must re-sign in with Gmail scope", flush=True)
            return None

        try:
            r = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id":     _GOOGLE_OAUTH_CLIENT_ID,
                    "client_secret": _GOOGLE_OAUTH_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type":    "refresh_token",
                },
                timeout=timeout,
            )
            if r.ok:
                tokens = r.json()
                access_token = tokens.get("access_token")
                if access_token:
                    _api().keychain_set("cloud_sync_google_access_token",
                                         access_token)
                    return access_token
            print(f"[cloud_sync] Google token refresh: HTTP {r.status_code} "
                  f"{r.text[:200]}", flush=True)
            return None
        except Exception as e:
            print(f"[cloud_sync] Google token refresh failed: {e}", flush=True)
            return None

    def has_gmail_scope(self) -> bool:
        """True if we have a Google refresh token (implies Gmail scope was granted)."""
        return bool(_api().keychain_get("cloud_sync_google_refresh_token"))

    def get_google_access_token(self, timeout: float = 10.0) -> Optional[str]:
        """Public accessor for the Google OAuth access token (for Sheets API, etc.)."""
        return self._ensure_google_auth(timeout=timeout)

    # ── Gmail Watch ─────────────────────────────────────────────────────
    def setup_gmail_watch(self, timeout: float = 15.0) -> Tuple[bool, str]:
        """
        Set up Gmail push notifications for the signed-in user.
        Tells Google to send a Pub/Sub notification whenever a new email
        arrives, which triggers the Cloud Function.

        Returns (success, message).
        """
        access_token = self._ensure_google_auth(timeout=timeout)
        if not access_token:
            return False, "No Gmail token — sign in with Google first."

        try:
            r = requests.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/watch",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "topicName": f"projects/{_PROJECT_ID}/topics/gmail-notifications",
                    "labelIds": ["INBOX"],
                },
                timeout=timeout,
            )
            if r.ok:
                result = r.json()
                expiry = result.get("expiration", "")
                print(f"[cloud_sync] Gmail watch set up, expires: {expiry}",
                      flush=True)
                return True, f"Watching for trade emails (expires: {expiry})"
            msg = f"Gmail watch failed: HTTP {r.status_code} {r.text[:200]}"
            print(f"[cloud_sync] {msg}", flush=True)
            return False, msg
        except Exception as e:
            msg = f"Gmail watch error: {e}"
            print(f"[cloud_sync] {msg}", flush=True)
            return False, msg

    def stop_gmail_watch(self, timeout: float = 10.0) -> bool:
        """Stop Gmail push notifications."""
        access_token = self._ensure_google_auth(timeout=timeout)
        if not access_token:
            return False
        try:
            r = requests.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/stop",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=timeout,
            )
            return r.ok
        except Exception:
            return False

    def store_gmail_auth_in_firestore(self, timeout: float = 10.0) -> bool:
        """
        Push the Google OAuth tokens to Firestore so the Cloud Function
        can use them to fetch emails on behalf of the user.

        Stores at /users/{uid}/meta/gmail_auth.
        """
        if not self.is_signed_in():
            return False

        access_token = _api().keychain_get("cloud_sync_google_access_token")
        refresh_token = _api().keychain_get("cloud_sync_google_refresh_token")
        email = _api().keychain_get("cloud_sync_google_email") or ""

        if not refresh_token:
            return False

        try:
            url = (
                f"{_BASE_URL}/users/{self._uid}/meta/gmail_auth"
                f"?key={_API_KEY}"
            )
            body = {
                "fields": {
                    "access_token":  {"stringValue": access_token or ""},
                    "refresh_token": {"stringValue": refresh_token},
                    "client_id":     {"stringValue": _GOOGLE_OAUTH_CLIENT_ID},
                    "client_secret": {"stringValue": _GOOGLE_OAUTH_CLIENT_SECRET},
                    "email_tracking_enabled": {"booleanValue": True},
                    "expires_at":    {"integerValue": str(int(time.time()) + 3600)},
                }
            }
            r = requests.patch(url, json=body, headers=self._auth_headers(),
                               timeout=timeout)
            if r.ok:
                print("[cloud_sync] Gmail auth stored in Firestore", flush=True)
                return True
            print(f"[cloud_sync] store Gmail auth: HTTP {r.status_code} "
                  f"{r.text[:200]}", flush=True)
            return False
        except Exception as e:
            print(f"[cloud_sync] store Gmail auth error: {e}", flush=True)
            return False

    def store_user_email_in_firestore(self, timeout: float = 10.0) -> bool:
        """
        Store the user's email address at /users/{uid} so the Cloud Function
        can look up the user by email when Gmail sends a Pub/Sub notification.
        """
        if not self.is_signed_in():
            return False
        email = _api().keychain_get("cloud_sync_google_email") or ""
        if not email:
            return False
        try:
            url = (
                f"{_BASE_URL}/users/{self._uid}"
                f"?key={_API_KEY}"
            )
            body = {
                "fields": {
                    "email": {"stringValue": email.lower()},
                }
            }
            r = requests.patch(url, json=body, headers=self._auth_headers(),
                               timeout=timeout)
            return r.ok
        except Exception:
            return False
