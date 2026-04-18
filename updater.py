"""GitHub Releases update checker — no auth needed for public repos."""
import re
import requests

from version import VERSION, GITHUB_OWNER, GITHUB_REPO

RELEASES_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"


def _parse(v):
    """'v1.2.3' or '1.2.3' → (1,2,3). Non-numeric chunks become 0."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def is_newer(remote, local=VERSION):
    return _parse(remote) > _parse(local)


def check_latest():
    """
    Returns dict:
      { "available": bool,
        "latest": "1.2.3" or "",
        "url": download-URL or "",
        "notes": release body,
        "error": str }
    Silent on network failure — returns available=False.
    """
    try:
        r = requests.get(RELEASES_URL, timeout=8,
                         headers={"Accept": "application/vnd.github+json"})
    except requests.exceptions.RequestException as e:
        return {"available": False, "latest": "", "url": "", "notes": "", "error": str(e)}

    if r.status_code == 404:
        return {"available": False, "latest": "", "url": "", "notes": "",
                "error": "No releases published yet."}
    if r.status_code != 200:
        return {"available": False, "latest": "", "url": "", "notes": "",
                "error": f"GitHub returned {r.status_code}"}

    data = r.json() or {}
    tag  = (data.get("tag_name") or "").lstrip("v")
    body = data.get("body") or ""

    # Prefer a .dmg asset; fall back to the release page URL
    dmg = ""
    for a in data.get("assets", []) or []:
        name = (a.get("name") or "").lower()
        if name.endswith(".dmg"):
            dmg = a.get("browser_download_url") or ""
            break
    url = dmg or data.get("html_url") or ""

    return {
        "available": is_newer(tag),
        "latest":    tag,
        "url":       url,
        "notes":     body,
        "error":     "",
    }
