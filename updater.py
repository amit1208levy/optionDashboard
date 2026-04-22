"""Git-based updater — check GitHub for new commits, pull, relaunch."""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


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
    if not is_git_repo():
        return {"available": False, "latest": "", "local": "", "notes": "",
                "error": "Not a git checkout — reinstall from GitHub."}

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
