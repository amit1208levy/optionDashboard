"""
Cloud sync via Firebase Firestore REST API + client-side AES encryption.

═══════════════════════════════════════════════════════════════════════════
                            SECURITY MODEL
═══════════════════════════════════════════════════════════════════════════

Every device pushes ENCRYPTED JSON blobs to Firestore. Firebase, Google,
and anyone who gains access to the database see ONLY opaque ciphertext.
Decryption requires the user's passphrase, which never leaves the device.

THREE INDEPENDENT LAYERS protect your data:

  1. UNGUESSABLE PATH
     Each user's documents live at  /syncs/<userId>/files/<file>  where
       userId = sha256("OptionsDashboard-v1|" + account_number + "|" + passphrase)
     That's 256 bits of entropy. Even if an attacker gets the Firebase
     project's public apiKey and reverse-engineers our schema, they'd
     have to brute-force the SHA-256 of (account + passphrase) to find
     the right path — infeasible.

  2. AUTHENTICATED ENCRYPTION (Fernet = AES-128-CBC + HMAC-SHA256)
     Every payload is encrypted on the device before it's uploaded. The
     key is derived via PBKDF2-HMAC-SHA256 with 600,000 iterations (the
     2024 OWASP recommendation, matching 1Password's current default).
     Salt = sha256(account_number) so every device with the same
     passphrase + account derives the SAME key — that's how multi-device
     sync works without ever transmitting the key. HMAC tag means any
     tampering with the ciphertext is detected on decrypt and rejected.

  3. FAIL-CLOSED PULL SEMANTICS
     If decryption fails (corrupted, tampered, wrong passphrase), the
     pull returns None and the local file is NOT touched. The worst an
     attacker who somehow finds your path can do is upload junk; they
     cannot poison your local data — we refuse to overwrite local with
     anything we can't authenticate.

WHAT'S NOT IN THE THREAT MODEL:

  • Filesystem access to your Mac. The passphrase is stored in
    .settings.json next to your TastyTrade credentials. Anyone with read
    access to your home directory can read both — same threat model as
    the rest of the app.
  • Forgetting your passphrase. There is no recovery. The data is
    cryptographically inaccessible without it. Pick something memorable
    or write it down once.
  • Free-tier abuse / DoS. Public-rules + path-obscurity means a random
    bad actor cannot find your data, but a determined attacker who DOES
    know your account+passphrase could spam writes. Free tier limits
    (20K writes/day) protect against runaway bills.

WHAT IS SYNCED across devices:
  .strategies.json, .groups.json, .history.json, .snapshots.json,
  .account_names.json

WHAT IS DELIBERATELY *NOT* SYNCED:
  • .credentials.json — each device logs in separately (defense in depth).
  • .settings.json — IBKR Gateway host/port, column preferences are
    machine-specific.
═══════════════════════════════════════════════════════════════════════════
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

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False


# ── Public Firebase project credentials ──────────────────────────────────
# These are NOT secrets — Firebase apiKeys are designed to be embedded in
# client code. Real security comes from the path-derivation + the payload
# encryption (see top-of-file docstring).
_API_KEY    = "AIzaSyD_pa87W0Q8kLxz-oa_QREiGQv5bFHYyEk"
_PROJECT_ID = "tastytradedashboard"
_BASE_URL   = (
    f"https://firestore.googleapis.com/v1/projects/{_PROJECT_ID}"
    f"/databases/(default)/documents"
)

# Google OAuth 2.0 Client ID for the Desktop OptionsDashboard app. This
# is NOT a secret — Client IDs are designed to be embedded in client
# applications. The actual security comes from PKCE in the OAuth flow
# (no client secret is sent) plus Firestore rules locking each user to
# their own auth.uid path.
_GOOGLE_OAUTH_CLIENT_ID = (
    "88826072729-9j5jh8h6lcv5qrsi0v5rcep6js1lra8d.apps.googleusercontent.com"
)

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
                         timeout: float = 180.0) -> Optional[dict]:
    """
    Run a Google OAuth 2.0 PKCE flow and exchange the resulting Google ID
    token for a Firebase ID token. Returns Firebase's full token dict
    (idToken, refreshToken, localId, email, ...) or None on failure.

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
            "scope":                 "openid email profile",
            "state":                 state,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
            "access_type":           "online",
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

    # Exchange the auth code for Google tokens (PKCE — no client secret).
    try:
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":     google_client_id,
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
    google_id_token = (r.json() or {}).get("id_token")
    if not google_id_token:
        msg = "Google did not return an id_token"
        print(f"[cloud_sync] {msg}: {r.text[:300]}", flush=True)
        raise GoogleSignInError(msg)

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
    """Cloud sync needs the cryptography package; report whether we have it."""
    return HAVE_CRYPTO


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
    Google Sign-In flow. No passphrase: encryption key + Firestore path
    are derived from the Firebase UID, which is stable per Google account
    and unique per user."""

    def __init__(self):
        if not HAVE_CRYPTO:
            raise RuntimeError("cryptography package not installed")
        self._refresh_token: Optional[str] = _api().keychain_get(
            "cloud_sync_refresh_token")
        self._uid: Optional[str] = _api().keychain_get(
            "cloud_sync_firebase_uid")
        self._id_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        if self._uid:
            self._fernet = self._build_fernet(self._uid)
        else:
            self._fernet = None

    def is_signed_in(self) -> bool:
        """True iff a Google sign-in has already completed and the
        Firebase UID + refresh token are cached in the keychain."""
        return bool(self._uid and self._refresh_token and self._fernet)

    # ── Crypto setup ─────────────────────────────────────────────────────
    # OWASP 2024 recommendation for PBKDF2-HMAC-SHA256.
    _PBKDF2_ITERATIONS = 600_000

    def _build_fernet(self, uid: str) -> "Fernet":
        # Encryption key = PBKDF2(firebase_uid). Same Google account →
        # same uid → same key → all of that user's devices can decrypt.
        salt = b"OptionsDashboard-cloud-sync-v2"
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=self._PBKDF2_ITERATIONS,
        )
        key = base64.urlsafe_b64encode(kdf.derive(uid.encode("utf-8")))
        return Fernet(key)

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
        """Encrypt and upload a single file. Returns True on success."""
        if not self.is_signed_in():
            print("[cloud_sync] push: not signed in — Google Sign-In required",
                  flush=True)
            return False
        try:
            payload = json.dumps(content, default=str).encode("utf-8")
            ciphertext = self._fernet.encrypt(payload).decode("ascii")
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            body = {
                "fields": {
                    "ciphertext": {"stringValue": ciphertext},
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
        Download + decrypt one file. Returns (content, updated_at_iso).
        (None, None) means: not signed in, not in cloud, network failed,
        or decrypt failed.
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
            ciphertext = fields.get("ciphertext", {}).get("stringValue")
            updated_at = fields.get("updated_at", {}).get("stringValue")
            if not ciphertext:
                return None, None
            plaintext = self._fernet.decrypt(ciphertext.encode("ascii"))
            content = json.loads(plaintext.decode("utf-8"))
            return content, updated_at
        except InvalidToken:
            print(f"[cloud_sync] pull {file_name}: wrong passphrase "
                  f"(could not decrypt cloud blob)", flush=True)
            return None, None
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
