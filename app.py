"""
Desktop launcher for the packaged Windows build (see deploy/build_windows.py).

Not used for the Linux deployment - that runs `uvicorn server:app` directly
(see deploy/install.sh). This adds the parts a double-clicked .exe needs:

- A data folder that lives next to the .exe, not PyInstaller's throwaway
  temp extraction folder, so the database survives between runs.
- Auto-downloading yt-dlp/gallery-dl/ffmpeg on first run, since a fresh
  install has nowhere else to get them from.
- Opening the browser once the server is actually ready to answer requests.
"""

import os
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile


def app_dir():
    """Folder the .exe (or this script) lives in."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE = app_dir()
DATA_DIR = os.path.join(BASE, "data")
OUTPUT_DIR = os.path.join(BASE, "download")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.environ.setdefault("APP_HOME", DATA_DIR)
os.environ.setdefault("APP_BIN_DIR", BASE)
os.environ.setdefault("APP_DEFAULT_OUTPUT", OUTPUT_DIR)
os.environ.setdefault("APP_HOST", "127.0.0.1")
os.environ.setdefault("APP_PORT", "8686")

BIN_DIR = os.path.join(BASE, "bin")
EXE_DOWNLOADS = {
    "yt-dlp.exe": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    # gallery-dl doesn't publish binaries on its own GitHub releases - the
    # project's README points to this sibling repo's daily builds instead.
    "gallery-dl.exe": "https://github.com/gdl-org/builds/releases/latest/download/gallery-dl_windows.exe",
}
# A long-standing, widely used source of static Windows ffmpeg builds
# (referenced by yt-dlp's own documentation). Swap this out, or drop
# ffmpeg.exe/ffprobe.exe into bin/ yourself, if you'd rather not fetch it
# from here.
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def _download(url, dest):
    print(f"Downloading {os.path.basename(dest)} ...")
    urllib.request.urlretrieve(url, dest)


def ensure_dependencies():
    os.makedirs(BIN_DIR, exist_ok=True)
    for filename, url in EXE_DOWNLOADS.items():
        dest = os.path.join(BIN_DIR, filename)
        if os.path.isfile(dest):
            continue
        try:
            _download(url, dest)
        except Exception as e:
            print(f"Couldn't download {filename}: {e}\nPlace it in {BIN_DIR} manually.")

    ffmpeg_dest  = os.path.join(BIN_DIR, "ffmpeg.exe")
    ffprobe_dest = os.path.join(BIN_DIR, "ffprobe.exe")
    if os.path.isfile(ffmpeg_dest) and os.path.isfile(ffprobe_dest):
        return
    tmp_zip = os.path.join(BASE, "_ffmpeg_download.zip")
    try:
        _download(FFMPEG_ZIP_URL, tmp_zip)
        with zipfile.ZipFile(tmp_zip) as z:
            for info in z.infolist():
                name = os.path.basename(info.filename)
                if name in ("ffmpeg.exe", "ffprobe.exe"):
                    with z.open(info) as src, open(os.path.join(BIN_DIR, name), "wb") as dst:
                        dst.write(src.read())
    except Exception as e:
        print(f"Couldn't download ffmpeg: {e}\nPlace ffmpeg.exe/ffprobe.exe in {BIN_DIR} manually.")
    finally:
        if os.path.isfile(tmp_zip):
            os.remove(tmp_zip)


def open_browser_when_ready(url):
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    webbrowser.open(url)


def main():
    ensure_dependencies()

    import uvicorn
    import server  # defines `server.app`, imported after APP_HOME etc. are set

    host = os.environ["APP_HOST"]
    port = int(os.environ["APP_PORT"])
    threading.Thread(target=open_browser_when_ready,
                     args=(f"http://{host}:{port}",), daemon=True).start()
    print(f"Starting on http://{host}:{port} - close this window to stop the server.")
    uvicorn.run(server.app, host=host, port=port)


if __name__ == "__main__":
    main()
