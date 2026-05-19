"""
Self-updating logic for OptionsDashboard.

For the bundled .app (what users run):
  • check_latest()   — asks GitHub Releases API for the newest version tag,
                       compares it to the VERSION baked into the bundle.
  • self_install()   — downloads the new .app.zip, swaps bundles, relaunches.

For developers running from source (git checkout):
  • check_latest()   — falls through to the old git-fetch path.
  • pull()           — git pull --ff-only.
"""
import os
import subprocess
import sys

import requests

from version import VERSION

HERE = os.path.dirname(os.path.abspath(__file__))

_REPO_API = "https://api.github.com/repos/amit1208levy/optionDashboard"

# The .zip uploaded to every GitHub Release as an asset.
# Matches what build.sh creates under dist/OptionsDashboard.zip.
_APP_ZIP_URL = (
    "https://github.com/amit1208levy/optionDashboard/releases/latest/"
    "download/OptionsDashboard.zip"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_frozen_bundle() -> bool:
    return getattr(sys, "frozen", False) or "Contents/Resources" in HERE


def _parse_version(v: str):
    """Parse 'v1.2.3' or '1.2.3' → (1, 2, 3).  Returns () on failure."""
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except (ValueError, AttributeError):
        return ()


def _latest_release():
    """
    Query GitHub Releases API.
    Returns (tag: str, notes: str, error: str).
    tag is empty string on error.
    """
    try:
        r = requests.get(
            f"{_REPO_API}/releases/latest",
            timeout=10,
            headers={"Accept": "application/vnd.github+json"},
        )
        if r.status_code == 404:
            return "", "", "No releases published yet."
        if r.status_code != 200:
            return "", "", f"GitHub API HTTP {r.status_code}"
        data = r.json()
        tag   = (data.get("tag_name") or "").lstrip("v")
        notes = (data.get("body") or "").strip()
        return tag, notes, ""
    except requests.exceptions.RequestException as exc:
        return "", "", str(exc)


# ── Git helpers (source-checkout only) ───────────────────────────────────────

def _git(*args, timeout=15):
    try:
        r = subprocess.run(
            ["git", *args], cwd=HERE,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, "", str(exc)


def is_git_repo():
    ok, _, _ = _git("rev-parse", "--is-inside-work-tree")
    return ok


# ── Public API ────────────────────────────────────────────────────────────────

def check_latest():
    """
    Returns:
      { "available": bool,
        "latest":    version string (e.g. "1.2.0"),
        "local":     version string (e.g. "1.1.0"),
        "notes":     release notes text,
        "error":     error string or "",
        "bundle":    True when running as a built .app (signals self_install path) }
    """
    # ── Bundled .app or non-git environment → version-based check ───────────
    if _is_frozen_bundle() or not is_git_repo():
        local = VERSION
        latest, notes, err = _latest_release()

        if err or not latest:
            return {"available": False, "latest": latest, "local": local,
                    "notes": "", "error": err, "bundle": True}

        local_t  = _parse_version(local)
        latest_t = _parse_version(latest)

        if not latest_t or not local_t or latest_t <= local_t:
            return {"available": False, "latest": latest, "local": local,
                    "notes": "", "error": "", "bundle": True}

        return {
            "available": True,
            "latest":    latest,
            "local":     local,
            "notes":     notes or "(no release notes)",
            "error":     "",
            "bundle":    True,
        }

    # ── Source checkout → git-based check ───────────────────────────────────
    ok, _, err = _git("fetch", "--quiet", "origin", timeout=45)
    if not ok:
        ok, _, err = _git("fetch", "--quiet", "origin", timeout=45)
    if not ok:
        return {"available": False, "latest": "", "local": "", "notes": "",
                "error": f"Fetch failed: {err or 'network error'}", "bundle": False}

    ok,  local, _  = _git("rev-parse", "--short", "HEAD")
    ok2, remote, _ = _git("rev-parse", "--short", "@{u}")
    if not ok or not ok2:
        return {"available": False, "latest": "", "local": local, "notes": "",
                "error": "Could not read git refs.", "bundle": False}

    if local == remote:
        return {"available": False, "latest": remote, "local": local,
                "notes": "", "error": "", "bundle": False}

    _, log_out, _ = _git("log", "--pretty=format:• %s", f"{local}..{remote}")
    return {
        "available": True,
        "latest":    remote,
        "local":     local,
        "notes":     log_out or "(no commit messages)",
        "error":     "",
        "bundle":    False,
    }


def pull():
    """Pull from git (source-checkout only). Returns (ok, message)."""
    ok, out, err = _git("pull", "--ff-only", "origin", "main", timeout=60)
    if ok:
        return True, out or "Up to date."
    return False, (err or out or "git pull failed.")


# ── .app self-update (bundled mode) ──────────────────────────────────────────

def _find_app_bundle_path():
    if not _is_frozen_bundle():
        return None
    exe = sys.executable
    app = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
    return app if app.endswith(".app") else None


def _download_zip(url: str, dest: str, progress_cb=None) -> None:
    """
    Stream-download `url` to `dest` with generous connect + per-chunk read
    timeouts.  Using requests in streaming mode (instead of
    urllib.urlretrieve) means a stalled GitHub CDN connection raises instead
    of hanging forever, while still tolerating slow but progressing
    downloads (40 MB over a 500 KB/s link takes ~80 s).

    Raises requests.RequestException on failure.
    """
    headers = {"User-Agent": "OptionsDashboard-Updater"}
    # connect=30 s, read=120 s between chunks — generous so a slow but
    # steady connection isn't killed mid-download.
    with requests.get(
        url, stream=True, timeout=(30, 120),
        allow_redirects=True, headers=headers,
    ) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done  = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress_cb is not None:
                    try:
                        progress_cb(done, total)
                    except Exception:
                        pass


def self_install(progress_cb=None):
    """
    Download the latest OptionsDashboard.zip from GitHub Releases,
    unpack it, replace the running .app bundle, and relaunch.

    The actual swap is done by a short detached bash script that waits
    for this process to exit before touching the bundle — so we never
    delete our own binary out from under ourselves.

    NOTE: this is a blocking network operation (~40 MB download).  Callers
    on a GUI thread must run it from a worker thread, or the event loop
    will freeze and macOS will report a hang.  Pass `progress_cb(done, total)`
    to receive byte-count updates from the worker thread.

    Returns (ok: bool, message: str).
    On success this process will exit shortly after returning True.
    """
    import tempfile, zipfile

    app_path = _find_app_bundle_path()
    if not app_path:
        return False, "Couldn't locate the running .app bundle."

    tmpdir   = tempfile.mkdtemp(prefix="optdash-update-")
    zip_path = os.path.join(tmpdir, "new.zip")

    # 1. Download
    try:
        _download_zip(_APP_ZIP_URL, zip_path, progress_cb=progress_cb)
    except Exception as exc:
        return False, f"Download failed: {exc}"

    # 2. Unpack
    extract_dir = os.path.join(tmpdir, "extracted")
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)
    except Exception as exc:
        return False, f"Could not unzip download: {exc}"

    # 3. Locate the .app inside the extracted folder
    new_app = os.path.join(extract_dir, "OptionsDashboard.app")
    if not os.path.isdir(new_app):
        for entry in os.listdir(extract_dir):
            cand = os.path.join(extract_dir, entry, "OptionsDashboard.app")
            if os.path.isdir(cand):
                new_app = cand
                break
    if not os.path.isdir(new_app):
        return False, "Downloaded zip didn't contain OptionsDashboard.app."

    # 4. Write a detached swap-and-relaunch script
    script_path = os.path.join(tmpdir, "replace.sh")
    our_pid     = os.getpid()
    with open(script_path, "w") as f:
        f.write(
f"""#!/bin/bash
# Wait for the old app to exit
while kill -0 {our_pid} 2>/dev/null; do sleep 0.3; done
sleep 0.5

# Swap bundles
rm -rf {app_path!r}
mv {new_app!r} {app_path!r} || cp -R {new_app!r} {app_path!r}

# Remove quarantine attribute so Gatekeeper doesn't block relaunch
xattr -dr com.apple.quarantine {app_path!r} 2>/dev/null

# Relaunch
open {app_path!r}

# Tidy up
rm -rf {tmpdir!r}
"""
        )
    os.chmod(script_path, 0o755)

    # 5. Launch the swap script detached — caller will then sys.exit(0)
    subprocess.Popen(
        ["/bin/bash", script_path],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True, "Downloading complete — app will relaunch in a moment."
