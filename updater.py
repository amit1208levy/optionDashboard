"""Git-based updater — check GitHub for new commits, pull, relaunch."""
import os
import subprocess
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))

# For .app bundles we can't run `git fetch` inside the sealed bundle, so
# instead we ask GitHub's HTTP API for the latest commit SHA on main.
# No auth needed for public repos.
_REPO_API = "https://api.github.com/repos/amit1208levy/optionDashboard"


def _is_frozen_bundle() -> bool:
    """Running inside a PyInstaller .app bundle (not a live git checkout)?"""
    return getattr(sys, "frozen", False) or "Contents/Resources" in HERE


def _bundled_sha_file():
    """Path to the baked-in SHA stamp created at build time."""
    return os.path.join(HERE, "_build_sha.txt")


def _local_sha_for_bundle():
    """Return the git SHA that was current at the time the .app was built."""
    path = _bundled_sha_file()
    if not os.path.exists(path):
        return ""
    try:
        with open(path) as f:
            return f.read().strip()[:7]
    except Exception:
        return ""


def _remote_sha_via_http():
    """HTTP-based remote HEAD SHA lookup for bundled apps."""
    try:
        r = requests.get(f"{_REPO_API}/commits/main",
                         timeout=10,
                         headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        data = r.json()
        return (data.get("sha") or "")[:7], ""
    except requests.exceptions.RequestException as e:
        return None, str(e)


def _recent_commits_via_http(since_sha):
    """Return 'bullet list' of recent commit subjects from GitHub."""
    try:
        r = requests.get(f"{_REPO_API}/commits",
                         params={"per_page": 10},
                         timeout=10,
                         headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return ""
        out = []
        for c in r.json():
            subj = (c.get("commit", {}).get("message") or "").splitlines()[0]
            sha  = (c.get("sha") or "")[:7]
            out.append(f"• {subj}")
            if since_sha and sha == since_sha:
                break
        return "\n".join(out) or "(no commit messages)"
    except Exception:
        return ""


def _git(*args, timeout=15):
    """Run a git command in the repo directory. Returns (ok, stdout, stderr)."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=HERE,
            capture_output=True, text=True,
            timeout=timeout,
        )
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, "", str(e)


def is_git_repo():
    ok, _, _ = _git("rev-parse", "--is-inside-work-tree")
    return ok


def check_latest():
    """
    Returns dict:
      { "available": bool,
        "latest": short-sha or "",
        "local":  short-sha or "",
        "notes":  recent commit subjects (joined),
        "error":  str }
    """
    # Bundled .app path: check GitHub HTTP API instead of git fetch.
    if _is_frozen_bundle() or not is_git_repo():
        local = _local_sha_for_bundle()
        remote, err = _remote_sha_via_http()
        if remote is None:
            # Can't reach GitHub — silent, don't nag the user.
            return {"available": False, "latest": "", "local": local,
                    "notes": "", "error": err or ""}
        if not local or local == remote:
            return {"available": False, "latest": remote, "local": local,
                    "notes": "", "error": ""}
        # Update available — but bundled users can't self-update.  Surface
        # that clearly so they know they need a replacement .app.
        return {
            "available": True,
            "latest":    remote,
            "local":     local,
            "notes":     _recent_commits_via_http(local) +
                         "\n\n⚠  Download the latest OptionsDashboard.app "
                         "and replace the one in /Applications.",
            "error":     "",
            "bundle":    True,
        }

    # Allow up to 45 s on slow connections; retry once before giving up.
    ok, _, err = _git("fetch", "--quiet", "origin", timeout=45)
    if not ok:
        ok, _, err = _git("fetch", "--quiet", "origin", timeout=45)
    if not ok:
        return {"available": False, "latest": "", "local": "", "notes": "",
                "error": f"Fetch failed: {err or 'network error'}"}

    ok, local, _   = _git("rev-parse", "--short", "HEAD")
    ok2, remote, _ = _git("rev-parse", "--short", "@{u}")
    if not ok or not ok2:
        return {"available": False, "latest": "", "local": local, "notes": "",
                "error": "Could not read git refs."}

    if local == remote:
        return {"available": False, "latest": remote, "local": local,
                "notes": "", "error": ""}

    # Collect commit subjects between local and remote
    _, log_out, _ = _git(
        "log", "--pretty=format:• %s", f"{local}..{remote}"
    )

    return {
        "available": True,
        "latest":    remote,
        "local":     local,
        "notes":     log_out or "(no commit messages)",
        "error":     "",
    }


def pull():
    """Pull the latest main. Returns (ok, message)."""
    ok, out, err = _git("pull", "--ff-only", "origin", "main", timeout=60)
    if ok:
        return True, out or "Up to date."
    return False, (err or out or "git pull failed.")
