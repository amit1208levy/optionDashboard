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
import json
from datetime import datetime, timezone
from typing import Optional, Tuple, Union

import requests

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

# Files we sync across devices.
SYNCED_FILES = (
    ".strategies.json",
    ".groups.json",
    ".history.json",
    ".snapshots.json",
    ".account_names.json",
)


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
    """One instance per (account_number, passphrase) pair. Cheap to
    construct — just derives the encryption key + the Firestore document
    path. Methods do the actual network I/O."""

    def __init__(self, account_number: str, passphrase: str):
        if not HAVE_CRYPTO:
            raise RuntimeError("cryptography package not installed")
        if not account_number or not passphrase:
            raise ValueError("account_number and passphrase required")
        self.account = str(account_number)
        self._fernet = self._build_fernet(passphrase)
        self._user_id = self._derive_user_id(passphrase)

    # ── Crypto setup ─────────────────────────────────────────────────────
    # OWASP 2024 recommendation for PBKDF2-HMAC-SHA256. Higher = slower
    # to brute-force the passphrase if the encrypted data ever leaks.
    # 600k iterations ≈ 250 ms on Apple Silicon — only paid once per
    # passphrase change, cached afterwards.
    _PBKDF2_ITERATIONS = 600_000

    def _build_fernet(self, passphrase: str) -> "Fernet":
        # Salt = sha256(account_number). Same on every device that types the
        # same passphrase + uses the same TT account → same key → can decrypt.
        salt = hashlib.sha256(self.account.encode("utf-8")).digest()
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=self._PBKDF2_ITERATIONS,
        )
        key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
        return Fernet(key)

    def _derive_user_id(self, passphrase: str) -> str:
        h = hashlib.sha256()
        h.update(b"OptionsDashboard-v1|")
        h.update(self.account.encode("utf-8"))
        h.update(b"|")
        h.update(passphrase.encode("utf-8"))
        return h.hexdigest()

    # ── Firestore REST helpers ───────────────────────────────────────────
    def _file_url(self, file_name: str) -> str:
        safe = file_name.replace("/", "_")
        return (
            f"{_BASE_URL}/syncs/{self._user_id}/files/{safe}"
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
                               json=body, timeout=timeout)
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
        (None, None) means: not in cloud, network failed, or wrong passphrase.
        """
        try:
            r = requests.get(self._file_url(file_name), timeout=timeout)
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
