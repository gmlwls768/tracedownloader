"""
Download engine for TraceDownloader.

This module has no UI code in it - server.py exposes it over HTTP/SSE, and
a future frontend could reuse it as-is. It owns the task queue, the SQLite
task list, and every subprocess call to yt-dlp / gallery-dl.

Status text is never hardcoded here. Every user-facing message is built
with M(code, **params) below and stored as "code" or "code:{json params}".
The web client resolves the final sentence from its own i18n table, so a
single engine can drive an English or Korean UI without knowing which one
is active.
"""

import subprocess
import threading
import concurrent.futures
import itertools
import queue
import json
import os
import time
import re
import sqlite3
import shutil
import secrets
import tempfile
import urllib.request
from collections import deque
from datetime import datetime, timezone

UTC = timezone.utc

APP_VERSION = "1.1.0"
# This app's own GitHub repo, for the in-app "check for updates" feature.
APP_REPO_URL      = "https://github.com/gmlwls768/tracedownloader"
APP_RELEASES_API  = "https://api.github.com/repos/gmlwls768/tracedownloader/releases/latest"

BASE_DIR = os.environ.get("APP_HOME") or os.path.dirname(os.path.abspath(__file__))
# Where bundled/auto-downloaded tool binaries live. Deliberately separate
# from BASE_DIR (the *data* folder, e.g. an APP_HOME pointed at a mounted
# volume): app.py and deploy/install.sh both drop yt-dlp/gallery-dl/ffmpeg
# next to the app itself, not next to the database.
BIN_SEARCH_DIR = os.environ.get("APP_BIN_DIR") or BASE_DIR


def _find_bin(name):
    """Prefer a copy bundled/auto-downloaded next to the app; fall back to PATH.
    A bare name on the fallback path relies on the OS to apply PATHEXT
    (Windows) or PATH lookup (Linux/Mac), so only the explicit bin/
    candidates need an .exe suffix spelled out."""
    names = (name, name + ".exe") if os.name == "nt" else (name,)
    for folder in (os.path.join(BIN_SEARCH_DIR, "bin"), BIN_SEARCH_DIR):
        for n in names:
            candidate = os.path.join(folder, n)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return name


YTDLP_BIN           = _find_bin("yt-dlp")
GALLERYDL_BIN       = _find_bin("gallery-dl")
FFMPEG_BIN          = _find_bin("ffmpeg")
FFPROBE_BIN         = _find_bin("ffprobe")

# yt-dlp and gallery-dl both change often enough (site breakage, new
# extractors) that a copy downloaded once and never touched again goes
# stale quickly - they're updated as single-file binaries.
TOOL_UPDATE_URLS = {
    "yt-dlp": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
              + ("yt-dlp.exe" if os.name == "nt" else "yt-dlp_linux"),
    "gallery-dl": "https://github.com/gdl-org/builds/releases/latest/download/"
                  + ("gallery-dl_windows.exe" if os.name == "nt" else "gallery-dl_linux"),
}
TOOL_UPDATE_INTERVAL = 24 * 3600
# Package names on PyPI, for the pip fallback in _update_tool_via_pip().
TOOL_PIP_NAMES = {"yt-dlp": "yt-dlp", "gallery-dl": "gallery-dl"}
# Shown in Settings next to each tool's version/update date.
TOOL_REPO_URLS = {
    "yt-dlp": "https://github.com/yt-dlp/yt-dlp",
    "gallery-dl": "https://github.com/mikf/gallery-dl",
    "ffmpeg": "https://github.com/yt-dlp/FFmpeg-Builds",
}
# ffmpeg/ffprobe come as a zip, not a single binary, so they update through
# their own path (_update_ffmpeg). Only the copies WE manage in bin/ (the
# Windows build) are touched; a system ffmpeg (apt on Linux) is left alone.
# The GitHub release is tagged by date, so the release tag is compared
# against the last one we installed to avoid re-downloading 100MB+ when
# nothing changed.
FFMPEG_BUILD_TAG_API = "https://api.github.com/repos/yt-dlp/FFmpeg-Builds/releases/latest"
FFMPEG_BUILD_ZIP_URLS = [
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
]


def _managed_bin_path(name):
    """Where our own downloaded copy of `name` would live, regardless of
    whether it's actually there yet. Only ever used to decide what
    check_tool_updates() is allowed to overwrite - a binary found via PATH
    instead (apt, a manual install, ...) is left alone."""
    fname = name + ".exe" if os.name == "nt" else name
    return os.path.join(BIN_SEARCH_DIR, "bin", fname)


def _pip_fallback_path():
    """pip from the venv this app is conventionally installed alongside
    (deploy/install.sh's layout - APP root/venv/bin/pip), if there is one."""
    candidate = os.path.join(BIN_SEARCH_DIR, "venv",
                             "Scripts" if os.name == "nt" else "bin",
                             "pip.exe" if os.name == "nt" else "pip")
    return candidate if os.path.isfile(candidate) else None
DEFAULT_OUTPUT_DIR  = os.environ.get("APP_DEFAULT_OUTPUT", "download")
OUTPUT_TEMPLATE_TPL = '{dir}/%(uploader)s/%(upload_date>%Y-%m-%d)s - %(title)s [%(id)s].%(ext)s'
DB_FILE             = os.path.join(BASE_DIR, "app.db")
ARCHIVE_FILE        = os.path.join(BASE_DIR, "downloaded_archive.txt")
# gallery-dl's own --download-archive (SQLite). Used by persistent gallery
# groups so a re-check re-runs the same URL and only new files transfer.
GALLERY_ARCHIVE     = os.path.join(BASE_DIR, "gallery_archive.sqlite3")
# Cookies (cookies.txt / Netscape format), managed from the settings screen.
# Sent to both yt-dlp and gallery-dl on every call - cookies are only ever
# transmitted to the domain they belong to, so one shared file covering
# several logged-in sites is safe to reuse everywhere.
COOKIES_FILE        = os.path.join(BASE_DIR, "cookies.txt")
AUTOSAVE_INTERVAL   = 30
DEFAULT_GALLERY_TEMPLATE = "[{artist}] {title} ({id})"

ILLEGAL_FS_CHARS_RE = re.compile(r'[\\/:*?"<>|]')
GENERIC_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)

# yt-dlp/gallery-dl/ffprobe are console executables: launched from the
# windowed (no-console) Windows build, each call would flash a black
# console window on screen. Passed as creationflags to every tool
# subprocess; 0 elsewhere, where the flag has no meaning.
SUBPROC_FLAGS = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW


def M(key, **params):
    """Build a localizable status message. The client looks `key` up in its
    i18n table and interpolates `params` into the matching template; unknown
    keys are shown verbatim so nothing silently disappears.
    The first parameter must not be named after any template placeholder:
    M("exit_code", code=...) passing code= as a param has to keep working."""
    if params:
        return f"{key}:{json.dumps(params, ensure_ascii=False)}"
    return key


# A generic listing path (an artist/tag/group/... collection page) on some
# gallery sites is only recognized in a canonical "...-all.html" form; the
# short URL people naturally type or copy matches no extractor. This is a
# host-agnostic heuristic - it names no particular site, and a site that
# doesn't use that convention just fails the same way the short URL already
# would. The path segments below are generic collection-path terminology.
_LISTING_SHORT_RE = re.compile(
    r'^(https?://[^/]+/(?:tag|artist|group|series|type|character)/[^/?#]+)$',
    re.IGNORECASE)


def canon_url(url):
    url = (url or "").strip()
    m = _LISTING_SHORT_RE.match(url)
    if m and not url.lower().endswith(".html"):
        return url + "-all.html"
    return url


def _exit_code_msg(code, reason):
    """yt-dlp/gallery-dl exited non-zero. `reason` (a tail line of its own
    output) may be empty, so this picks the template that matches."""
    return M("exit_code_reason", code=code, reason=reason) if reason else M("exit_code", code=code)


def _sanitize_filename(name):
    name = ILLEGAL_FS_CHARS_RE.sub("_", name or "").strip(" .")
    return name[:180] if name else "untitled"


def _gallery_meta_fields(meta):
    """gallery-dl field names vary by site (e.g. one site's `artist`, another's
    `tags_artist`), so normalize to (artist, title, id) here."""
    artists = (meta.get("tags_artist") or meta.get("artist")
               or meta.get("group") or meta.get("uploader"))
    if isinstance(artists, str):
        artists = [artists]
    artists = [str(a).strip() for a in (artists or []) if a and str(a).strip()]
    artist_str = ", ".join(a.capitalize() for a in artists) if artists else "Unknown"
    title = str(meta.get("title") or meta.get("title_jpn") or "untitled").strip()
    gid   = str(meta.get("gallery_id") or meta.get("gid") or "").strip()
    return artist_str, title, gid


PROGRESS_LINE_RE = re.compile(
    r'\[download\]\s+([\d.]+)%.*?at\s+([\d.]+)\s*([KMGT]?i?B)/s', re.IGNORECASE)
DEST_LINE_RE    = re.compile(r'\[download\] Destination: (.+)$')
ALREADY_LINE_RE = re.compile(r'\[download\] (.+) has already been downloaded')
# Extracts the title back out of our own output template "DATE - TITLE [ID].ext"
# (also matches ".fNNN" intermediate fragment files).
FILENAME_TITLE_RE = re.compile(r'^\d{4}-\d{2}-\d{2} - (.+?) \[[^\]]+\](?:\.f\d+)?\.[^.]+$')
MERGER_LINE_RE  = re.compile(r'\[Merger\] Merging formats into "(.+)"$')
PART_FILE_RE    = re.compile(r'\.f\d+\.[^.]+$')
RES_FILE_ID_RE  = re.compile(r'\[([A-Za-z0-9_-]+)\]\.[^.]+$')
VIDEO_FILE_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov"}

UNIT_MULT = {"B":1, "KIB":1024, "MIB":1024**2, "GIB":1024**3, "TIB":1024**4,
             "KB":1000, "MB":1000**2, "GB":1000**3, "TB":1000**4}


def _utcnow():
    return datetime.now(UTC).isoformat()


def _fmt_speed(bytes_per_sec):
    if bytes_per_sec <= 0:
        return "0 B/s"
    units = ["B/s","KB/s","MB/s","GB/s"]
    v = float(bytes_per_sec)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}"
        v /= 1024


# ──────────────────────────────────────────────
#  DATABASE
# ──────────────────────────────────────────────
class DB:
    def __init__(self, path):
        self.path = path
        self._q   = queue.Queue()
        self._t   = threading.Thread(target=self._loop, daemon=True, name="db-thread")
        self._t.start()
        self._run_sync(self._init_schema)

    def _loop(self):
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.commit()
        while True:
            fn, args, ev, box = self._q.get()
            if fn is None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(FULL)")
                except Exception:
                    pass
                self._conn.close()
                if ev: ev.set()
                break
            try:
                box.append(("ok", fn(*args)))
            except Exception as e:
                box.append(("err", e))
            finally:
                if ev: ev.set()

    def _run_sync(self, fn, *args):
        box = []; ev = threading.Event()
        self._q.put((fn, args, ev, box))
        ev.wait()
        kind, val = box[0]
        if kind == "err": raise val
        return val

    def _run_async(self, fn, *args):
        self._q.put((fn, args, None, []))

    def _exec(self, sql, params=()):
        c = self._conn.execute(sql, params); self._conn.commit(); return c
    def _fall(self, sql, params=()):
        return self._conn.execute(sql, params).fetchall()
    def _fone(self, sql, params=()):
        return self._conn.execute(sql, params).fetchone()

    def execute(self, sql, params=()):  self._run_async(self._exec, sql, params)
    def execute_sync(self, sql, params=()): return self._run_sync(self._exec, sql, params)
    def fetchall(self, sql, params=()): return self._run_sync(self._fall, sql, params)
    def fetchone(self, sql, params=()): return self._run_sync(self._fone, sql, params)

    def _init_schema(self):
        self._exec("""CREATE TABLE IF NOT EXISTS groups(
            id TEXT PRIMARY KEY, url TEXT NOT NULL,
            state TEXT DEFAULT 'queued',
            expected_count INTEGER, completed_count INTEGER DEFAULT 0,
            last_message TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT, updated_at TEXT)""")
        try:
            self._exec("ALTER TABLE groups ADD COLUMN sort_order INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # already exists
        try:
            # 1 = leave this group out of "Re-check all" / scheduled re-checks.
            self._exec("ALTER TABLE groups ADD COLUMN no_recheck INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            # '' = resolved/downloaded by yt-dlp, 'gallery' = gallery-dl.
            self._exec("ALTER TABLE groups ADD COLUMN media TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        self._exec("""CREATE TABLE IF NOT EXISTS videos(
            id TEXT PRIMARY KEY, group_id TEXT, url TEXT NOT NULL,
            state TEXT DEFAULT 'queued', last_message TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT, extractor_id TEXT,
            FOREIGN KEY(group_id) REFERENCES groups(id))""")
        try:
            self._exec("ALTER TABLE videos ADD COLUMN extractor_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self._exec("ALTER TABLE videos ADD COLUMN title TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            # Cached final file path, set at completion time so "locate" can
            # answer instantly instead of walking the whole output tree.
            self._exec("ALTER TABLE videos ADD COLUMN filepath TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self._exec("ALTER TABLE videos ADD COLUMN media TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        self._exec("""CREATE TABLE IF NOT EXISTS history(
            video_id TEXT PRIMARY KEY, url TEXT, completed_at TEXT)""")
        self._exec("""CREATE TABLE IF NOT EXISTS meta(
            key TEXT PRIMARY KEY, value TEXT)""")

    # ── batched, transactional save ──
    def _save_snapshot(self, groups, videos):
        now = _utcnow()
        c = self._conn
        c.execute("BEGIN")
        try:
            if groups:
                # updated_at stores each group's real modification time
                # (Task.modified_at) - overwriting with `now` in bulk would
                # make "sort by last modified" meaningless.
                c.executemany("""INSERT INTO groups
                    (id,url,state,expected_count,completed_count,last_message,sort_order,no_recheck,media,created_at,updated_at)
                    VALUES(:id,:url,:state,:expected_count,:completed_count,:last_message,:sort_order,:no_recheck,:media,:now,:updated_at)
                    ON CONFLICT(id) DO UPDATE SET state=excluded.state,
                    expected_count=excluded.expected_count,
                    completed_count=excluded.completed_count,
                    last_message=excluded.last_message,
                    sort_order=excluded.sort_order,
                    no_recheck=excluded.no_recheck,
                    media=excluded.media,
                    updated_at=excluded.updated_at
                """, [{**g, "now": now} for g in groups])
            if videos:
                c.executemany("""INSERT INTO videos
                    (id,group_id,url,state,last_message,extractor_id,title,filepath,media,created_at,updated_at)
                    VALUES(:id,:group_id,:url,:state,:last_message,:extractor_id,:title,:filepath,:media,:now,:now)
                    ON CONFLICT(id) DO UPDATE SET state=excluded.state,
                    last_message=excluded.last_message,
                    extractor_id=COALESCE(excluded.extractor_id, videos.extractor_id),
                    title=COALESCE(NULLIF(excluded.title,''), videos.title),
                    filepath=COALESCE(excluded.filepath, videos.filepath),
                    media=excluded.media,
                    updated_at=excluded.updated_at
                """, [{**v, "now": now} for v in videos])
            c.commit()
        except Exception:
            c.rollback()
            raise

    def save_snapshot(self, groups, videos):
        """Synchronous, transactional save. Raises on failure (never silent)."""
        return self._run_sync(self._save_snapshot, groups, videos)

    # groups
    def upsert_group(self, g):
        self.save_snapshot([g], [])

    def all_groups(self):
        return self.fetchall("SELECT * FROM groups ORDER BY sort_order, created_at")

    def delete_group(self, gid):
        self.execute("DELETE FROM groups WHERE id=?", (gid,))

    # videos
    def upsert_video(self, v):
        self.save_snapshot([], [v])

    def all_videos(self):
        return self.fetchall("SELECT * FROM videos ORDER BY created_at")

    def video_urls_for_group(self, gid):
        return {r["url"] for r in
                self.fetchall("SELECT url FROM videos WHERE group_id=?", (gid,))}

    def delete_video(self, vid):
        self.execute("DELETE FROM videos WHERE id=?", (vid,))

    def delete_videos_for_group(self, gid):
        self.execute("DELETE FROM videos WHERE group_id=?", (gid,))

    # history
    def mark_history(self, vid_id, url):
        self.execute(
            "INSERT OR IGNORE INTO history(video_id,url,completed_at) VALUES(?,?,?)",
            (vid_id, url, _utcnow()))

    def all_history_ids(self):
        """All history video_ids as a set - for bulk lookups during resolve."""
        return {r["video_id"] for r in
                self.fetchall("SELECT video_id FROM history")}

    def delete_history(self, vid_id):
        self.execute("DELETE FROM history WHERE video_id=?", (vid_id,))

    def delete_history_many(self, vid_ids):
        """Delete many ids in a single transaction instead of one commit each."""
        vid_ids = [v for v in vid_ids if v]
        if not vid_ids:
            return
        def _bulk():
            c = self._conn
            c.execute("BEGIN")
            try:
                for i in range(0, len(vid_ids), 500):
                    chunk = vid_ids[i:i+500]
                    ph = ",".join("?" * len(chunk))
                    c.execute(f"DELETE FROM history WHERE video_id IN ({ph})", chunk)
                c.commit()
            except Exception:
                c.rollback()
                raise
        self._run_sync(_bulk)

    def set_filepaths(self, pairs):
        """Bulk-write (filepath, video_id) pairs in one transaction (backfill)."""
        pairs = [p for p in pairs if p[0] and p[1]]
        if not pairs:
            return
        def _bulk():
            c = self._conn
            c.execute("BEGIN")
            try:
                c.executemany("UPDATE videos SET filepath=? WHERE id=?", pairs)
                c.commit()
            except Exception:
                c.rollback()
                raise
        self._run_sync(_bulk)

    # meta
    def get_meta(self, key, default=None):
        row = self.fetchone("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.execute_sync(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    def get_bool(self, key, default=False):
        v = self.get_meta(key)
        return default if v is None else v.lower() in ("1","true","yes")

    def get_int(self, key, default=0):
        v = self.get_meta(key)
        try: return int(v)
        except: return default

    def close(self):
        """Block until the DB thread has flushed and closed the connection."""
        ev = threading.Event(); box = []
        self._q.put((None, (), ev, box))
        ev.wait(timeout=5.0)


# ──────────────────────────────────────────────
#  TASK MODEL
# ──────────────────────────────────────────────
class Task:
    _seq_counter = itertools.count()

    def __init__(self, url, id=None, kind="video", state="queued",
                 last_message="", parent_group_id=None,
                 expected_count=None, completed_count=0,
                 extractor_id=None, created_at=None, modified_at=None,
                 sort_order=0):
        self.url             = url
        self.extractor_id    = extractor_id
        self.seq             = next(Task._seq_counter)
        self.created_at      = created_at  if created_at  is not None else time.time()
        self.modified_at     = modified_at if modified_at is not None else self.created_at
        self.id              = id or f"{int(time.time()*1000)}_{abs(hash(url))&0xFFFF}"
        self.kind            = kind
        self.state           = state
        self.last_message    = last_message
        self.parent_group_id = parent_group_id
        self.expected_count  = expected_count
        self.completed_count = completed_count
        self.sort_order      = sort_order
        self.title           = ""
        self.filepath        = None  # final path once completed (instant "locate")
        self.media           = ""    # '' = yt-dlp, 'gallery' = gallery-dl
        self.no_recheck      = 0     # groups: 1 = skip in bulk/scheduled re-check
        self.priority        = 0     # 1 = always sorts first, regardless of sort option
        self.new_count       = 0
        self.skip_count      = 0
        self.progress_pct    = 0.0
        self.speed_bps       = 0.0
        self._paused         = False
        self._cancelled      = False

    def to_group_dict(self):
        return dict(id=self.id, url=self.url, state=self.state,
                    expected_count=self.expected_count,
                    completed_count=self.completed_count,
                    last_message=self.last_message,
                    sort_order=self.sort_order,
                    no_recheck=self.no_recheck,
                    media=self.media,
                    updated_at=datetime.fromtimestamp(self.modified_at, UTC).isoformat())

    def to_video_dict(self):
        return dict(id=self.id, group_id=self.parent_group_id,
                    url=self.url, state=self.state,
                    last_message=self.last_message,
                    extractor_id=self.extractor_id,
                    title=self.title or "",
                    filepath=self.filepath,
                    media=self.media)

    @staticmethod
    def from_group_row(r):
        def _ts(s):
            try: return datetime.fromisoformat(s).timestamp()
            except: return time.time()
        ca = _ts(r["created_at"])  if r["created_at"]  else time.time()
        ma = _ts(r["updated_at"])  if r["updated_at"]  else ca
        t = Task(r["url"], r["id"], kind="group", state=r["state"],
                 last_message=r["last_message"] or "",
                 expected_count=r["expected_count"],
                 completed_count=r["completed_count"] or 0,
                 created_at=ca, modified_at=ma,
                 sort_order=r["sort_order"] if "sort_order" in r.keys() else 0)
        t.no_recheck = r["no_recheck"] if "no_recheck" in r.keys() and r["no_recheck"] else 0
        t.media = r["media"] if "media" in r.keys() and r["media"] else ""
        return t

    @staticmethod
    def from_video_row(r):
        t = Task(r["url"], r["id"], kind="video", state=r["state"],
                 last_message=r["last_message"] or "",
                 parent_group_id=r["group_id"],
                 extractor_id=r["extractor_id"] if "extractor_id" in r.keys() else None)
        t.title = r["title"] if "title" in r.keys() else ""
        t.filepath = r["filepath"] if "filepath" in r.keys() else None
        t.media = r["media"] if "media" in r.keys() and r["media"] else ""
        return t


# ──────────────────────────────────────────────
#  GROUP STATE LOGIC
# ──────────────────────────────────────────────
def _is_private_error(task):
    """Members-only/private-content errors - retrying never helps without
    matching login cookies, so these are excluded from bulk retry."""
    return "private video" in (task.last_message or "").lower()


def _is_404_error(task):
    """Deleted/missing content (HTTP 404) - won't come back, so excluded
    from bulk retry the same way private content is."""
    return "HTTP Error 404" in (task.last_message or "")


def _is_permanent_error(task):
    """An error that will keep failing for the same reason no matter how
    many times it's retried."""
    return _is_private_error(task) or _is_404_error(task)


def _derive_group_state(children: list) -> str:
    """
    Rules (priority order):
    1. No children yet         -> 'queued'
    2. Actively downloading    -> 'downloading'
    3. Any queued (pending)    -> 'queued'
    4. Any paused              -> 'paused'
    5. All done (completed / skipped / error) -> 'completed' (GREEN)
       (individual video errors are acceptable; group itself is fine)
    """
    if not children:
        return "queued"

    states = {c.state for c in children}

    if "downloading" in states:
        return "downloading"
    if "queued" in states:
        return "queued"
    if "paused" in states:
        return "paused"

    return "completed"


# Everything above is importable via `from .models import *` by the mixin
# modules in this package - internal wiring, not a public API.
__all__ = [
    "ALREADY_LINE_RE",
    "APP_RELEASES_API",
    "APP_REPO_URL",
    "APP_VERSION",
    "ARCHIVE_FILE",
    "AUTOSAVE_INTERVAL",
    "BASE_DIR",
    "BIN_SEARCH_DIR",
    "COOKIES_FILE",
    "DB",
    "DB_FILE",
    "DEFAULT_GALLERY_TEMPLATE",
    "DEFAULT_OUTPUT_DIR",
    "DEST_LINE_RE",
    "FFMPEG_BIN",
    "FFMPEG_BUILD_TAG_API",
    "FFMPEG_BUILD_ZIP_URLS",
    "FFPROBE_BIN",
    "FILENAME_TITLE_RE",
    "GALLERYDL_BIN",
    "GALLERY_ARCHIVE",
    "GENERIC_URL_RE",
    "ILLEGAL_FS_CHARS_RE",
    "M",
    "MERGER_LINE_RE",
    "OUTPUT_TEMPLATE_TPL",
    "PART_FILE_RE",
    "PROGRESS_LINE_RE",
    "RES_FILE_ID_RE",
    "SUBPROC_FLAGS",
    "TOOL_PIP_NAMES",
    "TOOL_REPO_URLS",
    "TOOL_UPDATE_INTERVAL",
    "TOOL_UPDATE_URLS",
    "Task",
    "UNIT_MULT",
    "UTC",
    "VIDEO_FILE_EXTS",
    "YTDLP_BIN",
    "_derive_group_state",
    "_exit_code_msg",
    "_find_bin",
    "_fmt_speed",
    "_gallery_meta_fields",
    "_is_404_error",
    "_is_permanent_error",
    "_is_private_error",
    "_managed_bin_path",
    "_pip_fallback_path",
    "_sanitize_filename",
    "_utcnow",
    "canon_url"
]
