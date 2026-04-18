# Options Dashboard

Desktop dashboard for managing a TastyTrade options portfolio.

## Releasing a new version

1. Bump the version in [`version.py`](version.py) (e.g. `1.0.0` → `1.0.1`).
2. Commit and push.
3. Build the DMG locally:
   ```bash
   ./build.sh
   ```
4. Upload the DMG as a GitHub release:
   ```bash
   gh release create v1.0.1 dist/OptionsDashboard-1.0.1.dmg \
     --title "v1.0.1" \
     --notes "What changed in this release"
   ```

Every running copy of the app auto-checks GitHub releases on launch, and
users get a "⬇ Update available" button in the header when a newer version
is published.
