#!/bin/bash
# Update an installed web server version to the latest code. Run from the
# project root (a git checkout):
#   bash deploy/update.sh
#
# Pulls the newest code, refreshes Python deps if requirements changed, and
# restarts the systemd service if one is running. Your data/, bin/,
# download/ and cookies are untouched (they're gitignored). The download
# tools (yt-dlp/gallery-dl) update themselves separately at runtime.
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -d .git ]; then
  echo "This isn't a git checkout, so there's nothing to pull."
  echo "Re-install with:  git clone https://github.com/gmlwls768/tracedownloader.git"
  exit 1
fi

echo "== pulling latest code"
before="$(git rev-parse HEAD)"
git pull --ff-only
after="$(git rev-parse HEAD)"

if [ "$before" = "$after" ]; then
  echo "Already up to date."
  exit 0
fi

if [ -x venv/bin/pip ] && git diff --name-only "$before" "$after" | grep -q '^requirements.txt$'; then
  echo "== requirements changed - updating venv"
  venv/bin/pip install -q -r requirements.txt
fi

# Restart the service if this checkout is the one it runs from.
UNIT="tracedownloader.service"
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$UNIT" 2>/dev/null; then
  echo "== restarting $UNIT"
  ${SUDO:-} systemctl restart "$UNIT"
  echo "Done. Updated $(git rev-parse --short "$before") -> $(git rev-parse --short "$after")."
else
  echo "Updated $(git rev-parse --short "$before") -> $(git rev-parse --short "$after")."
  echo "Restart the server for the change to take effect."
fi
