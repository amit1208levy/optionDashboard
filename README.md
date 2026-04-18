# Options Dashboard

Desktop dashboard for managing a TastyTrade options portfolio.

## Install (first time)

Requires Python 3.10+ and git.

```bash
git clone https://github.com/amit1208levy/optionDashboard.git
cd optionDashboard
./run.sh
```

The first run installs dependencies (PyQt6, requests, matplotlib) into your
user site-packages, then launches the app.

## Launch

```bash
./run.sh
```

## Updates

The app checks GitHub on launch. When new commits are on `main`, a
**⬇ Update** button appears in the header. Click it → **Update now** and
the app pulls the new code and relaunches itself. No manual download,
no Gatekeeper prompts.

## Releasing a new version (maintainer)

Just push to `main`. Every running copy will see the update on next launch.

```bash
git add -A
git commit -m "your change"
git push origin main
```

Bump `VERSION` in [`version.py`](version.py) for human-readable release notes,
but the update check itself is commit-SHA based — any push triggers it.
