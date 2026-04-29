"""
TastyTrade session-token authentication.

Session tokens (POST /sessions with login+password) carry the streaming
entitlement that OAuth tokens lack — that's why the DXLink quote streamer
gets HTTP 403 on /api-quote-tokens when authenticated via OAuth.

This module wraps the /sessions endpoint:

    login(login, password, otp=None, remember_me=True)
        → {"session_token": str, "remember_token": str|None, "error": str|None}

    refresh_with_remember(login, remember_token)
        → {"session_token": str, "remember_token": str|None, "error": str|None}

    validate(session_token)
        → bool

2FA flow
--------
TastyTrade returns 401 with header ``X-Tastyworks-OTP: present`` (case-
insensitive) when 2FA is required.  Caller catches the resulting
``"otp_required"`` error string, asks the user for the 6-digit code, and
calls login() again with otp set.

Remember-me
-----------
With remember-me enabled, the response includes a long-lived
``remember-token`` that can be exchanged for fresh session tokens for ~30
days WITHOUT re-prompting for the password or 2FA.  We persist this
token alongside the user's login (never the password).
"""
from __future__ import annotations

import requests

BASE = "https://api.tastyworks.com"
UA   = "options-dashboard/1.0"


def _headers(otp: str | None = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "User-Agent":   UA,
    }
    if otp:
        h["X-Tastyworks-OTP"] = otp
    return h


def login(login: str, password: str, otp: str | None = None,
          remember_me: bool = True) -> dict:
    """
    Authenticate to TastyTrade with username + password.

    Returns a dict with one of these shapes:
        success:   {"session_token": str, "remember_token": str|None,
                    "error": None}
        2FA:       {"session_token": None, "remember_token": None,
                    "error": "otp_required"}
        bad creds: {"session_token": None, "remember_token": None,
                    "error": "invalid_credentials"}
        other:     {"session_token": None, "remember_token": None,
                    "error": "<message>"}

    Pass the returned dict back into login() with ``otp`` set when error
    is "otp_required".
    """
    if not login or not password:
        return {"session_token": None, "remember_token": None,
                "error": "missing credentials"}

    body = {
        "login":    login,
        "password": password,
        "remember-me": bool(remember_me),
    }

    try:
        r = requests.post(
            f"{BASE}/sessions",
            json=body,
            headers=_headers(otp=otp),
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        return {"session_token": None, "remember_token": None,
                "error": f"network: {e}"}

    # 2FA required → server returns 401 with X-Tastyworks-OTP: present
    if r.status_code == 401:
        otp_hdr = r.headers.get("X-Tastyworks-OTP", "").lower()
        if "present" in otp_hdr or "required" in otp_hdr:
            return {"session_token": None, "remember_token": None,
                    "error": "otp_required"}
        return {"session_token": None, "remember_token": None,
                "error": "invalid_credentials"}

    if r.status_code not in (200, 201):
        # Try to surface the API's error message
        try:
            msg = (r.json().get("error") or {}).get("message") \
                  or r.json().get("message") \
                  or r.text[:200]
        except ValueError:
            msg = r.text[:200] or f"HTTP {r.status_code}"
        return {"session_token": None, "remember_token": None,
                "error": str(msg)}

    try:
        data = r.json().get("data", {}) or {}
    except ValueError:
        return {"session_token": None, "remember_token": None,
                "error": "non-JSON response"}

    sess = data.get("session-token") or data.get("sessionToken") or ""
    remb = data.get("remember-token") or data.get("rememberToken")
    if not sess:
        return {"session_token": None, "remember_token": None,
                "error": "no session token in response"}

    return {"session_token": sess, "remember_token": remb, "error": None}


def refresh_with_remember(login: str, remember_token: str) -> dict:
    """
    Exchange a remember-token for a fresh session-token.  No 2FA prompt.
    Same return shape as login().
    """
    if not login or not remember_token:
        return {"session_token": None, "remember_token": None,
                "error": "missing credentials"}

    body = {
        "login": login,
        "remember-token": remember_token,
        "remember-me": True,
    }
    try:
        r = requests.post(
            f"{BASE}/sessions",
            json=body,
            headers=_headers(),
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        return {"session_token": None, "remember_token": None,
                "error": f"network: {e}"}

    if r.status_code not in (200, 201):
        return {"session_token": None, "remember_token": None,
                "error": f"HTTP {r.status_code}"}

    try:
        data = r.json().get("data", {}) or {}
    except ValueError:
        return {"session_token": None, "remember_token": None,
                "error": "non-JSON response"}

    sess = data.get("session-token") or data.get("sessionToken") or ""
    remb = data.get("remember-token") or data.get("rememberToken")
    if not sess:
        return {"session_token": None, "remember_token": None,
                "error": "no session token"}
    return {"session_token": sess, "remember_token": remb, "error": None}


def validate(session_token: str) -> bool:
    """Return True if the session token is still valid."""
    if not session_token:
        return False
    try:
        r = requests.post(
            f"{BASE}/sessions/validate",
            headers={
                "Authorization": session_token,
                "User-Agent":    UA,
            },
            timeout=10,
        )
        return r.status_code in (200, 201)
    except requests.exceptions.RequestException:
        return False


def logout(session_token: str) -> bool:
    """Invalidate the session token.  Best-effort."""
    if not session_token:
        return False
    try:
        r = requests.delete(
            f"{BASE}/sessions",
            headers={
                "Authorization": session_token,
                "User-Agent":    UA,
            },
            timeout=10,
        )
        return r.status_code in (200, 204)
    except requests.exceptions.RequestException:
        return False
