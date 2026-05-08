"""
Cloud sync via Firebase Firestore REST API + client-side AES encryption.

Each device pushes ENCRYPTED JSON blobs to Firestore — Firebase only ever
sees opaque ciphertext. Decryption requires the passphrase the user set in
the Cloud Sync settings; without it (and even WITH the public Firebase
apiKey), nobody can read the data.

Two layers of access control:
  1. The document path is sha256(account_number + passphrase) — a 64-char
     unguessable string. Without knowing the passphrase you can't even
     find the right document.
  2. Even if you find it, the contents are AES-encrypted with a key
     derived from the same passphrase via PBKDF2-200k.

Files synced: strategies, leg groups, history, snapshots, account-name
overrides. Files NOT synced: credentials (each device logs in separately
for safety) and per-machine settings (IBKR Gateway host/port, column
preferences) since they're device-specific.
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
    def _build_fernet(self, passphrase: str) -> "Fernet":
        # Salt = sha256(account_number). Same on every device that types the
        # same passphrase + uses the same TT account → same key → can decrypt.
        salt = hashlib.sha256(self.account.encode("utf-8")).digest()
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=200_000,
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
