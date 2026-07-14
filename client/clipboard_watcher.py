"""
Clipboard watcher + "open on this PC" helper (optional, Windows-only).

Useful when the web UI runs on a different machine than the one you
actually want files opened on (e.g. the server lives on a home NAS/LXC and
you browse to it from your desktop). Run this script on the PC you want
things opened on, in the background:

  1) Copy a URL to the clipboard -> it's posted to the server's /api/add.
  2) Right-click "Open video" / "Open folder" in the web UI -> that request
     is served locally by this script (127.0.0.1:8687) and opens the file
     with the OS default player / file explorer.

If you're running the packaged Windows app, everything already happens on
the same PC and you don't need this at all - it only matters when the
server and the browser are on two different machines.

No third-party dependencies (stdlib tkinter + urllib only).
Configuration lives in client_config.json next to this script; it's
created with defaults the first time you run it.
"""

import tkinter as tk
import urllib.request
import urllib.error
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_config.json")
DEFAULT_CONFIG = {
    "server": "http://127.0.0.1:8686",
    # Local path this PC sees the server's output folder under (for the
    # "open" helper below). Leave empty to disable that feature.
    "win_base": "",
    "open_port": 8687,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"Wrote default config to {CONFIG_PATH} - edit it if needed, then restart.")
    except (OSError, json.JSONDecodeError) as e:
        print(f"Couldn't read {CONFIG_PATH} ({e}) - using defaults.")
    return cfg


CFG        = load_config()
SERVER     = CFG["server"].rstrip("/")
WIN_BASE   = CFG["win_base"]
OPEN_PORT  = int(CFG["open_port"])
POLL_SEC   = 1.5

# Every URL, not just one site - the server itself decides how to route
# each one (persistent tracking, gallery, or a one-off video).
URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)


def clip_seq():
    """Windows clipboard sequence number - increments on every copy, even of
    identical text, so re-copying the same group URL can still retrigger a
    server-side re-check."""
    try:
        import ctypes
        return ctypes.windll.user32.GetClipboardSequenceNumber()
    except Exception:
        return None


def post_urls(text):
    """Send URL text to the server. Returns how many were newly added."""
    data = json.dumps({"urls": text}).encode("utf-8")
    req = urllib.request.Request(
        SERVER + "/api/add", data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r).get("added", 0)


def _explorer_select(full):
    """Open Explorer with the file pre-selected (Shell API).
    `explorer /select,path` is avoided - it breaks on filenames with commas."""
    try:
        import ctypes
        shell32 = ctypes.windll.shell32
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.ole32.CoInitialize(None)  # per-thread COM init, harmless if repeated
        pidl = shell32.ILCreateFromPathW(full)
        if not pidl:
            return False
        try:
            return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
        finally:
            shell32.ILFree(pidl)
    except Exception:
        return False


class OpenHandler(BaseHTTPRequestHandler):
    """Serves the web UI's "open" requests. Rejects anything outside WIN_BASE."""

    def log_message(self, *a):  # quiet - don't spam the console with request logs
        pass

    def _headers(self, code):
        self.send_response(code)
        # Chrome's Private Network Access policy requires these for a page
        # on the LAN to call into localhost.
        self.send_header("Access-Control-Allow-Origin", SERVER)
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_OPTIONS(self):
        self._headers(204)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/open":
            self._headers(404)
            return
        if not WIN_BASE:
            self._headers(404)
            return
        q = parse_qs(u.query)
        rel  = (q.get("rel")  or [""])[0]
        play = (q.get("play") or ["0"])[0] == "1"
        base = os.path.normpath(WIN_BASE)
        full = os.path.normpath(os.path.join(base, rel.replace("/", "\\")))
        if full != base and not full.startswith(base + "\\"):
            self._headers(403)
            return
        if not os.path.exists(full):
            self._headers(404)
            return
        try:
            if play or os.path.isdir(full):
                os.startfile(full)          # default player, or Explorer for a folder
            elif not _explorer_select(full):
                # Selecting the exact file failed - fall back to its parent folder.
                os.startfile(os.path.dirname(full))
            self._headers(200)
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] opened: {full}" + (" (play)" if play else ""))
        except Exception as e:
            print(f"open failed: {e}")
            self._headers(500)


def start_open_server():
    if not WIN_BASE:
        print("win_base isn't set in client_config.json - the 'open' helper is disabled "
              "(clipboard watching still works).")
        return
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", OPEN_PORT), OpenHandler)
    except OSError as e:
        print(f"Couldn't start the open-helper server (port {OPEN_PORT} in use?): {e}")
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Open-helper listening on 127.0.0.1:{OPEN_PORT} (mapped to: {WIN_BASE})")


def main():
    start_open_server()
    root = tk.Tk()
    root.withdraw()  # no window - only used for clipboard access
    last = ""
    # Baseline against whatever's already on the clipboard at startup, so a
    # restart doesn't resend old content.
    last_seq = clip_seq()
    print(f"Watching clipboard -> {SERVER}  (Ctrl+C to stop)")
    while True:
        try:
            text = root.clipboard_get()
        except tk.TclError:
            text = ""   # clipboard empty, or not text
        except KeyboardInterrupt:
            break

        seq = clip_seq()
        if seq is not None:
            changed = seq != last_seq   # detects a re-copy of identical text too
            last_seq = seq
        else:
            changed = text != last      # fallback if the sequence number isn't available
        if text and changed:
            last = text
            urls = list(dict.fromkeys(URL_RE.findall(text)))  # de-dup, keep order
            if urls:
                try:
                    n = post_urls("\n".join(urls))
                    stamp = time.strftime("%H:%M:%S")
                    print(f"[{stamp}] found {len(urls)} URL(s) -> {n} added")
                except urllib.error.URLError as e:
                    print(f"couldn't reach the server: {e}")
                except Exception as e:
                    print(f"send error: {e}")

        try:
            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            break

    print("stopped")


if __name__ == "__main__":
    main()
