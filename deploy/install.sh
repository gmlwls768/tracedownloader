#!/bin/bash
# Web server version setup script (Linux). Run from the project root:
#   bash deploy/install.sh
#
# Sets up a venv, downloads standalone yt-dlp/gallery-dl/deno binaries into
# ./bin (no system-wide install, nothing outside this folder), and creates
# the ./data folder for the database. Does NOT set up a systemd service -
# see deploy/app.service.example if you want this to run as one.
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

$SUDO apt-get update
$SUDO apt-get install -y --no-install-recommends python3-venv ffmpeg curl ca-certificates

mkdir -p bin data

# The standalone yt-dlp/gallery-dl builds are PyInstaller binaries built for
# specific CPU architectures - on anything other than x86_64 (an ARM SBC, an
# Apple Silicon VM, ...) they either don't exist or won't run (wrong
# architecture, or a newer glibc than an older distro ships). Rather than
# fail outright, each one is verified with --version before being trusted,
# falling back to pip install into ./venv otherwise - the same fallback the
# app's own auto-updater uses at runtime (see engine/updater.py).
ARCH="$(uname -m)"

python3 -m venv venv
venv/bin/pip install -q -r requirements.txt

pip_install_tool() {  # pip_install_tool <bin name> <pip package>
  echo "Installing $1 via pip instead."
  rm -f "bin/$1"
  venv/bin/pip install -q "$2"
  ln -sf "$ROOT/venv/bin/$1" "bin/$1"
}

# yt-dlp
case "$ARCH" in
  x86_64|amd64)  YTDLP_ASSET=yt-dlp_linux ;;
  aarch64|arm64) YTDLP_ASSET=yt-dlp_linux_aarch64 ;;
  *)             YTDLP_ASSET="" ;;
esac
if [ -n "$YTDLP_ASSET" ] \
   && curl -fL "https://github.com/yt-dlp/yt-dlp/releases/latest/download/$YTDLP_ASSET" -o bin/yt-dlp \
   && chmod +x bin/yt-dlp && bin/yt-dlp --version >/dev/null 2>&1; then
  :
else
  pip_install_tool yt-dlp "yt-dlp[default]"
fi
bin/yt-dlp --version

# gallery-dl — standalone Linux binary, x86_64 only (gallery-dl doesn't
# publish binaries on its own GitHub releases; its README points to this
# sibling repo's daily builds instead, and that repo only builds x86_64).
if [ "$ARCH" = "x86_64" ] || [ "$ARCH" = "amd64" ]; then
  curl -fL https://github.com/gdl-org/builds/releases/latest/download/gallery-dl_linux -o bin/gallery-dl \
    && chmod +x bin/gallery-dl && bin/gallery-dl --version >/dev/null 2>&1
else
  false
fi || pip_install_tool gallery-dl gallery-dl
bin/gallery-dl --version

# deno — JS runtime yt-dlp uses to decode YouTube's signature scheme.
# Without it, YouTube videos (including embeds) fail with "Requested
# format is not available" on most current Linux distros.
case "$ARCH" in
  x86_64|amd64)  DENO_ASSET=deno-x86_64-unknown-linux-gnu.zip ;;
  aarch64|arm64) DENO_ASSET=deno-aarch64-unknown-linux-gnu.zip ;;
  *)             DENO_ASSET="" ;;
esac
if [ -n "$DENO_ASSET" ] \
   && curl -sL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/latest/download/$DENO_ASSET"; then
  python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('bin')"
  chmod +x bin/deno
  rm /tmp/deno.zip
  bin/deno --version | head -1
else
  echo "No deno build for architecture '$ARCH' - skipping (some YouTube downloads may fail without it)."
fi

cat <<EOF

Done. Start it with:
  APP_HOME=$ROOT/data APP_BIN_DIR=$ROOT venv/bin/uvicorn server:app --host 127.0.0.1 --port 8686

Then open http://127.0.0.1:8686 (or the host's LAN IP if you set --host 0.0.0.0).

To run it as a systemd service instead, see deploy/app.service.example.
EOF
