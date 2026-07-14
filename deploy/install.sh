#!/bin/bash
# Linux setup script. Run from the project root:
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

# yt-dlp — standalone Linux binary, no Python dependency on the host.
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux -o bin/yt-dlp
chmod +x bin/yt-dlp
bin/yt-dlp --version

# gallery-dl — standalone Linux binary. If the release asset is ever
# unavailable, fall back to installing it into the venv and linking it into
# bin/ so it's found the same way either way.
if curl -fL https://github.com/mikf/gallery-dl/releases/latest/download/gallery-dl.bin -o bin/gallery-dl; then
  chmod +x bin/gallery-dl
else
  echo "Standalone gallery-dl download failed - installing via pip instead."
  rm -f bin/gallery-dl
  python3 -m venv venv
  venv/bin/pip install -q gallery-dl
  ln -sf "$ROOT/venv/bin/gallery-dl" bin/gallery-dl
fi
bin/gallery-dl --version

# deno — JS runtime yt-dlp uses to decode YouTube's signature scheme.
# Without it, YouTube videos (including embeds) fail with "Requested
# format is not available" on most current Linux distros.
curl -sL -o /tmp/deno.zip \
     https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('bin')"
chmod +x bin/deno
rm /tmp/deno.zip
bin/deno --version | head -1

python3 -m venv venv
venv/bin/pip install -q -r requirements.txt

cat <<EOF

Done. Start it with:
  APP_HOME=$ROOT/data venv/bin/uvicorn server:app --host 127.0.0.1 --port 8686

Then open http://127.0.0.1:8686 (or the host's LAN IP if you set --host 0.0.0.0).

To run it as a systemd service instead, see deploy/app.service.example.
EOF
