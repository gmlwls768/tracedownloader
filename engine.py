"""
Download engine for the yt-dlp & gallery-dl GUI.

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
from collections import deque
from datetime import datetime, timezone

UTC = timezone.utc

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
FFPROBE_BIN         = _find_bin("ffprobe")
DEFAULT_OUTPUT_DIR  = os.environ.get("APP_DEFAULT_OUTPUT", "download")
OUTPUT_TEMPLATE_TPL = '{dir}/%(uploader)s/%(upload_date>%Y-%m-%d)s - %(title)s [%(id)s].%(ext)s'
DB_FILE             = os.path.join(BASE_DIR, "app.db")
ARCHIVE_FILE        = os.path.join(BASE_DIR, "downloaded_archive.txt")
# Cookies (cookies.txt / Netscape format), managed from the settings screen.
# Sent to both yt-dlp and gallery-dl on every call - cookies are only ever
# transmitted to the domain they belong to, so one shared file covering
# several logged-in sites is safe to reuse everywhere.
COOKIES_FILE        = os.path.join(BASE_DIR, "cookies.txt")
AUTOSAVE_INTERVAL   = 30
DEFAULT_GALLERY_TEMPLATE = "[{artist}] {title} ({id})"

ILLEGAL_FS_CHARS_RE = re.compile(r'[\\/:*?"<>|]')
GENERIC_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)


def M(code, **params):
    """Build a localizable status message. The client looks `code` up in its
    i18n table and interpolates `params` into the matching template; unknown
    codes are shown verbatim so nothing silently disappears."""
    if params:
        return f"{code}:{json.dumps(params, ensure_ascii=False)}"
    return code


def canon_url(url):
    return (url or "").strip()


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
                    (id,url,state,expected_count,completed_count,last_message,sort_order,created_at,updated_at)
                    VALUES(:id,:url,:state,:expected_count,:completed_count,:last_message,:sort_order,:now,:updated_at)
                    ON CONFLICT(id) DO UPDATE SET state=excluded.state,
                    expected_count=excluded.expected_count,
                    completed_count=excluded.completed_count,
                    last_message=excluded.last_message,
                    sort_order=excluded.sort_order,
                    updated_at=excluded.updated_at
                """, [{**g, "now": now} for g in groups])
            if videos:
                c.executemany("""INSERT INTO videos
                    (id,group_id,url,state,last_message,extractor_id,title,filepath,created_at,updated_at)
                    VALUES(:id,:group_id,:url,:state,:last_message,:extractor_id,:title,:filepath,:now,:now)
                    ON CONFLICT(id) DO UPDATE SET state=excluded.state,
                    last_message=excluded.last_message,
                    extractor_id=COALESCE(excluded.extractor_id, videos.extractor_id),
                    title=COALESCE(NULLIF(excluded.title,''), videos.title),
                    filepath=COALESCE(excluded.filepath, videos.filepath),
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
                    updated_at=datetime.fromtimestamp(self.modified_at, UTC).isoformat())

    def to_video_dict(self):
        return dict(id=self.id, group_id=self.parent_group_id,
                    url=self.url, state=self.state,
                    last_message=self.last_message,
                    extractor_id=self.extractor_id,
                    title=self.title or "",
                    filepath=self.filepath)

    @staticmethod
    def from_group_row(r):
        def _ts(s):
            try: return datetime.fromisoformat(s).timestamp()
            except: return time.time()
        ca = _ts(r["created_at"])  if r["created_at"]  else time.time()
        ma = _ts(r["updated_at"])  if r["updated_at"]  else ca
        return Task(r["url"], r["id"], kind="group", state=r["state"],
                    last_message=r["last_message"] or "",
                    expected_count=r["expected_count"],
                    completed_count=r["completed_count"] or 0,
                    created_at=ca, modified_at=ma,
                    sort_order=r["sort_order"] if "sort_order" in r.keys() else 0)

    @staticmethod
    def from_video_row(r):
        t = Task(r["url"], r["id"], kind="video", state=r["state"],
                 last_message=r["last_message"] or "",
                 parent_group_id=r["group_id"],
                 extractor_id=r["extractor_id"] if "extractor_id" in r.keys() else None)
        t.title = r["title"] if "title" in r.keys() else ""
        t.filepath = r["filepath"] if "filepath" in r.keys() else None
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


# ──────────────────────────────────────────────
#  ENGINE
# ──────────────────────────────────────────────
class Engine:
    def __init__(self):
        self.db    = DB(DB_FILE)
        self.tasks = []
        self.lock  = threading.Lock()

        self.global_stop   = threading.Event()
        self._procs        = {}
        self._procs_lock   = threading.Lock()
        self._requeue_lock = threading.Lock()  # serialises drain+enqueue ops

        # settings (persisted in the meta table)
        self._cfg_autostart      = self.db.get_bool("autostart", True)
        self._cfg_max_concurrent = self.db.get_int("max_concurrent", 4)
        self._cfg_autosave_min   = self.db.get_int("autosave_minutes", 0)
        self._cfg_output_dir     = self.db.get_meta("output_dir", DEFAULT_OUTPUT_DIR)
        self._cfg_save_on_add    = self.db.get_bool("save_on_add", True)
        self._cfg_font_size      = self.db.get_int("font_scale", 10)
        self._cfg_small_first         = self.db.get_bool("small_group_first", False)
        self._cfg_high_progress_first = self.db.get_bool("high_progress_first", False)
        self._cfg_res_height          = self.db.get_int("res_filter_height", 1080)
        self._cfg_size_mb             = self.db.get_int("size_filter_mb", 100)
        self._cfg_gallery_template    = self.db.get_meta("gallery_folder_template",
                                                           DEFAULT_GALLERY_TEMPLATE)
        self._cfg_persist_patterns    = self._parse_patterns(
            self.db.get_meta("persist_patterns", ""))
        self._autosave_interval  = (self._cfg_autosave_min * 60
                                    if self._cfg_autosave_min > 0 else AUTOSAVE_INTERVAL)

        # If output_dir looks like a path from a different OS (e.g. a DB
        # copied over from a Windows install), fall back to the default
        # rather than trying to download into an unreachable path.
        if self._cfg_output_dir and ("\\" in self._cfg_output_dir
                                     or re.match(r"^[A-Za-z]:", self._cfg_output_dir)):
            self._cfg_output_dir = DEFAULT_OUTPUT_DIR
            self.db.set_meta("output_dir", DEFAULT_OUTPUT_DIR)

        self._cum_total = self.db.get_int("completed_total", 0)

        self.sem          = threading.Semaphore(self._cfg_max_concurrent)
        # Executor pool is intentionally oversized - the semaphore is the
        # sole throttle for download concurrency, and it can be adjusted
        # live without touching the executor.
        self.executor      = concurrent.futures.ThreadPoolExecutor(max_workers=64)
        self.job_queue      = queue.Queue()   # FIFO download queue
        self._resolve_queue = queue.Queue()   # FIFO resolve queue (separate from downloads)
        self._add_queue     = queue.Queue()   # durable, sequential URL add queue

        self._closing = threading.Event()

        # ── change notification: version counter + condition ──
        self.version   = 0
        self._ver_cond = threading.Condition()
        # toast queue (delivered over SSE): (id, message)
        self._toasts      = deque(maxlen=100)
        self._toast_seq   = itertools.count(1)
        # status line for done-tab bulk operations (_set_done_status)
        self.done_status  = ""
        # pending 2-step delete-with-files confirmations: token -> [files]
        self._pending_deletes = {}
        self._pending_missing = {}

        # URLs that don't match a persist pattern: downloaded once, shown in
        # the "this session" list, never written to the database.
        self._ephemeral      = deque(maxlen=200)
        self._ephemeral_lock = threading.Lock()
        self._ephemeral_seq  = itertools.count(1)

        self._load_from_db()

        threading.Thread(target=self._worker_loop,      daemon=True).start()
        self._start_resolve_workers(self._cfg_max_concurrent)
        threading.Thread(target=self._autosave_loop,    daemon=True).start()
        threading.Thread(target=self._add_queue_worker, daemon=True).start()
        threading.Thread(target=self._filepath_backfill_worker, daemon=True).start()

        if self._cfg_autostart:
            threading.Timer(0.6, self._start_all).start()

    # ══════════════════════════════════════════
    #  NOTIFY
    # ══════════════════════════════════════════
    def _request_refresh(self):
        with self._ver_cond:
            self.version += 1
            self._ver_cond.notify_all()

    def wait_change(self, since, timeout=25.0):
        """For SSE: block until version exceeds `since`. Returns current version."""
        with self._ver_cond:
            if self.version <= since:
                self._ver_cond.wait(timeout)
            return self.version

    def _show_toast(self, msg, ms=3000):
        self._toasts.append((next(self._toast_seq), msg))
        self._request_refresh()

    def toasts_since(self, last_id):
        return [(i, m) for i, m in list(self._toasts) if i > last_id]

    # ══════════════════════════════════════════
    #  LOAD / SAVE
    # ══════════════════════════════════════════
    @staticmethod
    def _parse_patterns(text):
        return [ln.strip().lower() for ln in (text or "").splitlines() if ln.strip()]

    def _matches_persist(self, url):
        """True if `url` matches one of the user's "track forever" patterns
        (plain case-insensitive substring match - see settings)."""
        if not self._cfg_persist_patterns:
            return False
        ul = url.lower()
        return any(p in ul for p in self._cfg_persist_patterns)

    def _load_from_db(self):
        groups = [Task.from_group_row(r) for r in self.db.all_groups()]
        vid_by_group: dict = {}
        orphans = []
        for r in self.db.all_videos():
            t = Task.from_video_row(r)
            if t.state == "downloading": t.state = "queued"
            if t.parent_group_id:
                vid_by_group.setdefault(t.parent_group_id, []).append(t)
            else:
                orphans.append(t)

        # Completed groups with stale "queued" children (crash mid-download) must
        # not trigger a done→active transition, so demote those children to "paused".
        completed_gids = {g.id for g in groups if g.state == "completed"}

        tasks = []
        seen_gids: set = set()
        for g in groups:
            tasks.append(g)
            children = vid_by_group.get(g.id, [])
            if g.id in completed_gids:
                for c in children:
                    if c.state == "queued":
                        c.state = "paused"
            tasks.extend(children)
            seen_gids.add(g.id)
        # Videos whose parent group was deleted — keep them as orphans rather than losing them.
        for gid, vids in vid_by_group.items():
            if gid not in seen_gids:
                orphans.extend(vids)
        tasks.extend(orphans)

        with self.lock:
            self.tasks = tasks
        # Re-queue any groups that were mid-resolve when the app last closed.
        for g in groups:
            if g.state == "resolving":
                self._resolve_queue.put(g)

    def _save_all(self, silent=False):
        with self.lock:
            groups = [t.to_group_dict() for t in self.tasks if t.kind == "group"]
            videos = [t.to_video_dict() for t in self.tasks if t.kind == "video"]
        try:
            self.db.save_snapshot(groups, videos)
        except Exception as e:
            self._show_toast(M("db_save_failed", error=str(e)))
            return False
        if not silent:
            self._show_toast(M("manual_save_done"))
        return True

    def _autosave_loop(self):
        while True:
            time.sleep(self._autosave_interval)
            if self._closing.is_set():
                return
            ok = self._save_all(silent=True)
            if ok:
                self._show_toast(M("autosave_done"))

    def _output_template(self):
        d = self._cfg_output_dir or DEFAULT_OUTPUT_DIR
        return OUTPUT_TEMPLATE_TPL.replace("{dir}", d)

    def _ephemeral_video_template(self):
        d = self._cfg_output_dir or DEFAULT_OUTPUT_DIR
        return os.path.join(d, "%(extractor_key)s", "%(uploader)s",
                             "%(upload_date>%Y-%m-%d)s - %(title)s [%(id)s].%(ext)s")

    def _gallery_output_dir(self):
        return os.path.join(self._cfg_output_dir or DEFAULT_OUTPUT_DIR, "gallery")

    def _format_gallery_name(self, artist, title, gid):
        tpl = self._cfg_gallery_template or DEFAULT_GALLERY_TEMPLATE
        if gid:
            try:
                name = tpl.format(artist=artist, title=title, id=gid)
            except (KeyError, IndexError):
                name = tpl
        else:
            # No gallery id available - drop a lone "({id})" group along
            # with the placeholder so the name doesn't end in empty parens.
            stripped = re.sub(r'\(\s*\{id\}\s*\)', '', tpl).replace('{id}', '')
            try:
                name = stripped.format(artist=artist, title=title)
            except (KeyError, IndexError):
                name = stripped
        return _sanitize_filename(name.strip())

    def _cookies_tempcopy(self):
        """Return a private per-run copy of the cookies file, or None if
        there isn't one. yt-dlp/gallery-dl may rewrite --cookies on exit, so
        concurrent runs sharing one file could corrupt it - each run gets its
        own throwaway copy, removed when it finishes."""
        try:
            if os.path.getsize(COOKIES_FILE) <= 0:
                return None
        except OSError:
            return None
        fd, tmp = tempfile.mkstemp(prefix="dlgui_ck_", suffix=".txt")
        os.close(fd)
        try:
            shutil.copyfile(COOKIES_FILE, tmp)
        except OSError:
            try: os.remove(tmp)
            except OSError: pass
            return None
        return tmp

    @staticmethod
    def _cleanup_cookies_tmp(path):
        if path:
            try: os.remove(path)
            except OSError: pass

    # ══════════════════════════════════════════
    #  ADD TASK
    # ══════════════════════════════════════════
    def add_urls(self, text):
        """Feed several URLs from pasted text into the add queue sequentially
        (also used by the clipboard-watcher helper)."""
        urls = GENERIC_URL_RE.findall(text or "")
        urls = list(dict.fromkeys(canon_url(u) for u in urls))  # normalize + de-dup, keep order
        if not urls:
            return 0
        with self.lock:
            existing = {canon_url(t.url) for t in self.tasks}
        new_urls = [u for u in urls if u not in existing]
        # Existing group URLs are handled (as a re-check) by _add_task too,
        # so everything goes through the queue; exact duplicates just get an
        # informational toast from _add_task itself.
        for u in urls:
            self._add_queue.put(u)
        if len(urls) > 1:
            self._show_toast(M("add_batch_progress", count=len(urls)))
        return len(new_urls)

    def _add_task(self, url):
        """Register a new URL. Safe to call from any thread."""
        url = canon_url(url)
        if not url:
            return
        if not self._matches_persist(url):
            # Not on the "track forever" list: download once, show it in the
            # session-only list, and forget it on restart. Whether it's a
            # single video or an image gallery is auto-detected per URL, not
            # decided by a site whitelist.
            self._add_ephemeral(url)
            return
        dup = None
        with self.lock:
            for t in self.tasks:
                if t.kind == "group" and canon_url(t.url) == url:
                    dup = t
                    break
            if dup is None:
                group = Task(url, kind="group", state="resolving",
                             last_message=M("resolving_list"))
                self.tasks.append(group)
        # Handled outside the lock — _recheck_group re-acquires it, and this
        # lock is non-reentrant, so calling it while held deadlocks the app.
        if dup is not None:
            if dup.state == "resolving":
                self._show_toast(M("group_already_rechecking"))
            else:
                # Re-check regardless of current state — pasting an existing
                # group's URL again looks for new items, same as the
                # right-click "re-check" action, and un-pauses paused items.
                self._recheck_group(dup)
                self._show_toast(M("group_recheck_existing"))
            return
        try:
            self.db.upsert_group(group.to_group_dict())
        except Exception as e:
            self._show_toast(M("db_save_failed", error=str(e)))
        self._request_refresh()
        self._resolve_queue.put(group)

    def _add_queue_worker(self):
        while True:
            url = self._add_queue.get()
            try:
                self._add_task(url)
            except Exception as e:
                print(f"[add_queue] {e}")
            finally:
                self._add_queue.task_done()

    # ══════════════════════════════════════════
    #  EPHEMERAL DOWNLOADS  (session-only: not on a persist pattern)
    #  Never written to the database — gone when the server restarts.
    # ══════════════════════════════════════════
    def _ephemeral_new(self, url, kind):
        entry = {
            "id": f"eph{next(self._ephemeral_seq)}",
            "url": url, "kind": kind, "title": url,
            "state": "resolving", "message": "",
            "created_at": time.time(),
        }
        with self._ephemeral_lock:
            self._ephemeral.appendleft(entry)
        self._request_refresh()
        return entry

    def _ephemeral_update(self, entry, **kw):
        with self._ephemeral_lock:
            entry.update(kw)
        self._request_refresh()

    def _add_ephemeral(self, url):
        entry = self._ephemeral_new(url, "video")
        threading.Thread(target=self._run_ephemeral_auto, args=(entry, url), daemon=True).start()

    def _gallerydl_probe(self, url, cookies_tmp):
        """Run `gallery-dl -j <url>` and return its metadata dict, or None if
        gallery-dl has no extractor for this URL."""
        cmd = [GALLERYDL_BIN, "-j", url]
        if cookies_tmp:
            cmd += ["--cookies", cookies_tmp]
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=120)
            data = json.loads(out)
        except Exception:
            return None
        for e in data:
            if isinstance(e, list) and len(e) >= 2 and e[0] in (2, 3) and isinstance(e[-1], dict):
                return e[-1]
        return None

    def _run_ephemeral_auto(self, entry, url):
        """Decide video vs. gallery per URL instead of a site whitelist: ask
        yt-dlp first, and only defer to gallery-dl when yt-dlp can't match a
        real (non-generic) extractor for it."""
        cookies_tmp = self._cookies_tempcopy()
        probe_cmd = [YTDLP_BIN, "-J", "--flat-playlist", "--no-warnings"]
        if cookies_tmp:
            probe_cmd += ["--cookies", cookies_tmp]
        probe_cmd.append(url)
        extractor, probe_err = "", ""
        try:
            proc = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=60)
            if proc.returncode == 0:
                try:
                    extractor = (json.loads(proc.stdout).get("extractor_key") or "").lower()
                except Exception:
                    extractor = ""
            else:
                probe_err = (proc.stderr or "").strip()
                # yt-dlp tags nearly every log line with the matched
                # extractor, e.g. "ERROR: [youtube] id: Video unavailable".
                # That means yt-dlp *does* support this site and the failure
                # is about the content (private/deleted/region-locked), not
                # about the URL itself - very different from "no extractor
                # matched at all", so it shouldn't fall through to gallery-dl.
                m = re.search(r'\[([\w:.-]+)\]', probe_err)
                if m and m.group(1).lower() != "generic":
                    extractor = m.group(1).lower()
        except Exception as e:
            probe_err = str(e)
        finally:
            self._cleanup_cookies_tmp(cookies_tmp)

        if extractor and extractor != "generic":
            self._run_ephemeral_video(entry, url)
            return

        gallery_cookies = self._cookies_tempcopy()
        try:
            meta = self._gallerydl_probe(url, gallery_cookies)
        finally:
            self._cleanup_cookies_tmp(gallery_cookies)
        if meta is not None:
            entry["kind"] = "gallery"
            self._run_gallery_download(entry, url, meta)
            return

        if extractor == "generic":
            # yt-dlp's generic scraper found *something* - good enough when
            # gallery-dl has nothing to offer for this URL.
            self._run_ephemeral_video(entry, url)
            return

        tail = probe_err.strip().splitlines()[-1][:150] if probe_err.strip() else ""
        self._ephemeral_update(entry, state="error", message=M("url_unsupported", reason=tail))

    def _run_gallery_download(self, entry, url, meta):
        artist_str, title, gid = _gallery_meta_fields(meta)
        folder_name = self._format_gallery_name(artist_str, title, gid)
        count = meta.get("count")
        out_dir = self._gallery_output_dir()

        # Self dedup — some network filesystems don't reliably honor
        # gallery-dl's own built-in skip, so treat an existing non-empty
        # folder as "already downloaded" and don't touch it again.
        target_dir = os.path.join(out_dir, folder_name)
        try:
            already = os.path.isdir(target_dir) and bool(os.listdir(target_dir))
        except OSError:
            already = False
        if already:
            self._ephemeral_update(entry, title=folder_name, state="completed",
                                    message=M("gallery_already_exists", path=target_dir))
            return

        self._ephemeral_update(
            entry, title=folder_name, state="downloading",
            message=M("gallery_downloading_count", count=count) if count else M("gallery_downloading"))

        cookies_tmp = self._cookies_tempcopy()
        cmd = [GALLERYDL_BIN,
               "-o", f"base-directory={out_dir}",
               "-o", f'directory=["{folder_name}"]']
        if cookies_tmp:
            cmd += ["--cookies", cookies_tmp]
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=3600)
        except Exception as e:
            self._ephemeral_update(entry, state="error", message=M("download_exception", error=str(e)))
            return
        finally:
            self._cleanup_cookies_tmp(cookies_tmp)

        if proc.returncode == 0:
            self._ephemeral_update(entry, state="completed",
                                    message=M("gallery_done", path=target_dir))
        else:
            tail = (proc.stdout or "").strip().splitlines()
            reason = tail[-1][:150] if tail else ""
            self._ephemeral_update(entry, state="error", message=_exit_code_msg(proc.returncode, reason))

    def _run_ephemeral_video(self, entry, url):
        self._ephemeral_update(entry, state="downloading", message=M("starting"))
        cookies_tmp = self._cookies_tempcopy()
        cmd = [YTDLP_BIN, "-c", "--force-overwrites",
               "-f", "bestvideo+bestaudio/best",
               "-o", self._ephemeral_video_template(), "--no-warnings"]
        if cookies_tmp:
            cmd += ["--cookies", cookies_tmp]
        cmd.append(url)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
        except FileNotFoundError:
            self._cleanup_cookies_tmp(cookies_tmp)
            self._ephemeral_update(entry, state="error", message=M("binary_not_found", tool=YTDLP_BIN))
            return

        last_error_line, done_count = "", 0
        try:
            for line in proc.stdout:
                line = line.rstrip()
                m = PROGRESS_LINE_RE.search(line)
                if m:
                    try:
                        unit = m.group(3).upper()
                        speed = _fmt_speed(float(m.group(2)) * UNIT_MULT.get(unit, 1))
                        self._ephemeral_update(entry, message=M("downloading_progress", pct=m.group(1), speed=speed))
                    except Exception:
                        pass
                    continue
                dm = DEST_LINE_RE.search(line) or ALREADY_LINE_RE.search(line)
                if dm:
                    done_count += 1
                    tm = FILENAME_TITLE_RE.match(os.path.basename(dm.group(1)))
                    if tm:
                        self._ephemeral_update(entry, title=tm.group(1))
                if line:
                    last_error_line = line
            proc.wait()
        except Exception as e:
            self._ephemeral_update(entry, state="error", message=M("exception", error=str(e)))
            return
        finally:
            self._cleanup_cookies_tmp(cookies_tmp)

        if proc.returncode == 0:
            done_msg = M("video_done_multi", count=done_count) if done_count > 1 else M("video_done")
            self._ephemeral_update(entry, state="completed", message=done_msg)
        else:
            reason = last_error_line[:150] if last_error_line else ""
            self._ephemeral_update(entry, state="error", message=_exit_code_msg(proc.returncode, reason))

    # ══════════════════════════════════════════
    #  PLAYLIST RESOLVE  (persistent groups only — see _matches_persist)
    # ══════════════════════════════════════════
    def _resolve_worker(self):
        while not self._closing.is_set():
            try:
                group = self._resolve_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._fetch_and_create_inner(group)
            except Exception as e:
                with self.lock:
                    group.state        = "error"
                    group.last_message = M("resolve_error", reason=str(e))
                self._request_refresh()
            finally:
                self._resolve_queue.task_done()

    def _start_resolve_workers(self, max_concurrent: int):
        n = max(2, max_concurrent // 4)
        for _ in range(n):
            threading.Thread(target=self._resolve_worker, daemon=True).start()

    def _fetch_and_create_inner(self, group):
        cookies_tmp = self._cookies_tempcopy()
        cmd = [YTDLP_BIN, "--flat-playlist", "-J", "--no-warnings"]
        if cookies_tmp:
            cmd += ["--cookies", cookies_tmp]
        cmd.append(group.url)
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=600)
            if proc.returncode != 0:
                tail = (proc.stderr or "").strip().splitlines()
                raise RuntimeError(tail[-1][:200] if tail else f"exit code {proc.returncode}")
            j = json.loads(proc.stdout)
        except Exception as e:
            with self.lock:
                children = [t for t in self.tasks
                            if t.parent_group_id == group.id and t.kind == "video"]
            if children:
                # Re-check failed but group was already resolved — restore derived state.
                self._update_group_state(group.id)
                with self.lock:
                    group.last_message = M("recheck_failed", error=str(e))
                self._request_refresh()
            else:
                self._set_group_state(group, "error", M("resolve_error", reason=str(e)))
            return
        finally:
            self._cleanup_cookies_tmp(cookies_tmp)

        entries = j.get("entries")
        if not isinstance(entries, list) or not entries:
            entries = [j]

        existing_urls = self.db.video_urls_for_group(group.id)
        new_tasks, skip_cnt = [], 0
        url_title = {}   # backfill title on existing videos that don't have one

        # Bulk-load history/archive once — avoids one DB round trip / file
        # read per entry.
        history_ids = self.db.all_history_ids()
        archive_ids = self._load_archive_ids()

        for e in entries:
            vurl = e.get("webpage_url") or e.get("url") or e.get("id","")
            if not vurl: continue
            title = e.get("title") or ""
            if title:
                url_title[vurl] = title
            if vurl in existing_urls: continue
            vid_id = e.get("id","")
            state, msg = "queued", ""
            if vid_id and (vid_id in history_ids or vid_id in archive_ids):
                state, msg = "skipped", M("already_downloaded")
                skip_cnt += 1
            t = Task(vurl, kind="video", state=state,
                     last_message=msg, parent_group_id=group.id,
                     extractor_id=vid_id or None)
            t.title = title
            new_tasks.append(t)

        with self.lock:
            # Insert new videos immediately after their parent group (and after
            # any existing children from a previous re-check), so self.tasks
            # always maintains [Group, Vid1, Vid2, ...] interleaved order.
            insert_at = len(self.tasks)
            for i, t in enumerate(self.tasks):
                if t.id == group.id or t.parent_group_id == group.id:
                    insert_at = i + 1
            self.tasks[insert_at:insert_at] = new_tasks
            children = [t for t in self.tasks
                        if t.parent_group_id == group.id and t.kind == "video"]
            # Backfill titles on existing videos that don't have one yet.
            title_updates = []
            for c in children:
                if not c.title and url_title.get(c.url):
                    c.title = url_title[c.url]
                    title_updates.append(c)
            # A re-check revives paused videos back to queued.
            revived = []
            is_recheck = bool(existing_urls)
            if is_recheck:
                for c in children:
                    if c.state == "paused":
                        c.state = "queued"
                        c._paused = False
                        c._cancelled = False
                        revived.append(c)
            group.expected_count  = len(children)
            group.completed_count = sum(1 for c in children
                                        if c.state in ("completed","skipped"))
            group.new_count   = len([t for t in new_tasks if t.state == "queued"])
            group.skip_count  = skip_cnt
            group.state       = _derive_group_state(children)
            group.last_message = self._group_progress_message(children)
            group.modified_at  = time.time()

        try:
            self.db.save_snapshot([group.to_group_dict()],
                                   [v.to_video_dict() for v in new_tasks + title_updates])
        except Exception as e:
            self._show_toast(M("db_save_failed", error=str(e)))
        self._request_refresh()

        if not self.global_stop.is_set():
            queued = [t for t in new_tasks if t.state == "queued"] + revived
            if queued:
                # If a sort option is active, reorder self.tasks (screen +
                # priority order) first, then always rebuild the queue from
                # self.tasks order.
                if self._cfg_high_progress_first:
                    self._reorder_tasks_high_progress_first()
                elif self._cfg_small_first:
                    self._reorder_tasks_small_first()
                self._requeue_by_tasks_order(queued)

    def _load_archive_ids(self):
        """Read the archive file once, returning its ids as a set (bulk lookup for resolve)."""
        ids = set()
        try:
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if parts:
                        ids.add(parts[-1])
        except OSError:
            pass
        return ids

    def _remove_from_archive(self, vid_id):
        self._remove_from_archive_many([vid_id])

    def _remove_from_archive_many(self, vid_ids):
        """Remove many ids by rewriting the archive file once, not per id."""
        vid_ids = {v for v in vid_ids if v}
        if not vid_ids or not os.path.exists(ARCHIVE_FILE): return
        tmp = ARCHIVE_FILE + ".tmp"
        try:
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            with open(tmp, "w", encoding="utf-8") as f:
                for line in lines:
                    parts = line.split()
                    if parts and parts[-1] in vid_ids:
                        continue
                    f.write(line)
            os.replace(tmp, ARCHIVE_FILE)
        except OSError as e:
            print(f"[archive] {e}")

    # ══════════════════════════════════════════
    #  QUEUE
    # ══════════════════════════════════════════
    def _queue_put(self, task):
        """Push a single task onto the FIFO job queue."""
        task._cancelled = False
        task._paused    = False
        if task.state in ("paused", "error"):
            task.state        = "queued"
            task.last_message = ""
        self.job_queue.put(task)

    def _enqueue_tasks(self, tasks):
        for t in tasks:
            self._queue_put(t)

    def _requeue_by_tasks_order(self, new_tasks: list):
        """Drain queue, merge with new_tasks, re-enqueue in self.tasks order."""
        with self._requeue_lock:
            pending = []
            while True:
                try:
                    t = self.job_queue.get_nowait()
                    self.job_queue.task_done()
                    pending.append(t)
                except queue.Empty:
                    break

            seen = set()
            merged = []
            for t in pending + new_tasks:
                if t.id not in seen:
                    seen.add(t.id)
                    merged.append(t)

            with self.lock:
                pos = {t.id: i for i, t in enumerate(self.tasks)}

            merged.sort(key=lambda t: pos.get(t.id, 999999))

            for t in merged:
                self._queue_put(t)

    def _drain_queue(self):
        while True:
            try: self.job_queue.get_nowait(); self.job_queue.task_done()
            except queue.Empty: break

    def _start_all(self):
        self.global_stop.clear()

        # Only reset paused/error → queued. Skip videos in completed groups (done tab).
        with self.lock:
            completed_gids = {t.id for t in self.tasks
                              if t.kind == "group" and t.state == "completed"}
            for t in self.tasks:
                if t.kind == "video" and t.parent_group_id in completed_gids:
                    continue
                if t.kind == "video" and t.state in ("paused", "error"):
                    t._cancelled = False
                    t._paused    = False
                    t.state, t.last_message = "queued", ""
                elif t.kind == "video" and t.state == "queued":
                    t._cancelled = False
                    t._paused    = False

        if self._cfg_high_progress_first:
            self._reorder_tasks_high_progress_first()
        elif self._cfg_small_first:
            self._reorder_tasks_small_first()

        # Drain + re-enqueue in priority order, serialised with resolve workers.
        with self._requeue_lock:
            self._drain_queue()
            with self.lock:
                tasks = [t for t in self.tasks
                         if t.kind == "video" and t.state == "queued"
                         and t.parent_group_id not in completed_gids]
            self._enqueue_tasks(tasks)
        self._request_refresh()

    def _reorder_tasks_small_first(self):
        """Reorder self.tasks so groups with fewest queued videos sort first."""
        with self.lock:
            queued_per_group: dict = {}
            for t in self.tasks:
                if t.kind == "video" and t.parent_group_id and t.state == "queued":
                    gid = t.parent_group_id
                    queued_per_group[gid] = queued_per_group.get(gid, 0) + 1

            groups, vmap, orphans = [], {}, []
            for t in self.tasks:
                if t.kind == "group":
                    groups.append(t)
                elif t.parent_group_id:
                    vmap.setdefault(t.parent_group_id, []).append(t)
                else:
                    orphans.append(t)

            orig_pos = {g.id: i for i, g in enumerate(groups)}
            # A "top priority" group always sorts first.
            groups.sort(key=lambda g: (
                -g.priority,
                1 if g.state == "resolving" else 0,
                queued_per_group.get(g.id, 0),
                orig_pos[g.id],
            ))

            known_gids = {g.id for g in groups}
            for gid, vids in vmap.items():
                if gid not in known_gids:
                    orphans.extend(vids)

            ordered = []
            for g in groups:
                ordered.append(g)
                ordered.extend(vmap.get(g.id, []))
            ordered.extend(orphans)
            self.tasks = ordered

    def _reorder_tasks_high_progress_first(self):
        """Reorder self.tasks so groups with highest completion rate sort first."""
        with self.lock:
            groups, vmap, orphans = [], {}, []
            for t in self.tasks:
                if t.kind == "group":
                    groups.append(t)
                elif t.parent_group_id:
                    vmap.setdefault(t.parent_group_id, []).append(t)
                else:
                    orphans.append(t)

            orig_pos = {g.id: i for i, g in enumerate(groups)}
            def _hp_key(g):
                if g.state == "resolving" or not g.expected_count:
                    return (-g.priority, 1, 0.0, 0, orig_pos[g.id])
                rate = (g.completed_count or 0) / g.expected_count
                queued_cnt = sum(1 for t in vmap.get(g.id, []) if t.state == "queued")
                return (-g.priority, 0, -rate, queued_cnt, orig_pos[g.id])
            groups.sort(key=_hp_key)

            known_gids = {g.id for g in groups}
            for gid, vids in vmap.items():
                if gid not in known_gids:
                    orphans.extend(vids)

            ordered = []
            for g in groups:
                ordered.append(g)
                ordered.extend(vmap.get(g.id, []))
            ordered.extend(orphans)
            self.tasks = ordered

    def _stop_all(self):
        self.global_stop.set()
        self._drain_queue()
        with self._procs_lock:
            for p in list(self._procs.values()):
                try: p.terminate()
                except OSError: pass
        with self.lock:
            for t in self.tasks:
                if t.kind == "video" and t.state in ("downloading","queued"):
                    t._cancelled = True
                    t.state, t.last_message = "paused", M("stopped_by_user")
        self._request_refresh(); self._save_all(silent=True)

    def _stop_group(self, group):
        with self.lock:
            ch = [t for t in self.tasks
                  if t.parent_group_id == group.id and t.kind == "video"]
        for t in ch:
            t._cancelled = True
            with self._procs_lock:
                proc = self._procs.get(t.id)
            if proc:
                try: proc.terminate()
                except OSError: pass
            with self.lock:
                if t.state in ("downloading","queued"):
                    t.state, t.last_message = "paused", M("group_stopped")
        self._update_group_state(group.id)

    def _start_group(self, group):
        self.global_stop.clear()
        with self.lock:
            ch = [t for t in self.tasks
                  if t.parent_group_id == group.id and t.kind == "video"
                  and t.state in ("queued","paused","error")]
        self._enqueue_tasks(ch)
        self._update_group_state(group.id)
        self._request_refresh()

    # ══════════════════════════════════════════
    #  WORKER
    # ══════════════════════════════════════════
    def _worker_loop(self):
        while True:
            task = self.job_queue.get()
            if task is None: break
            if self.global_stop.is_set() or task._cancelled or task._paused:
                self.job_queue.task_done()
                if task.parent_group_id:
                    self._update_group_state(task.parent_group_id)
                continue
            if task.state not in ("queued","error","paused"):
                self.job_queue.task_done(); continue
            self.sem.acquire()
            f = self.executor.submit(self._run_download, task)
            f.add_done_callback(lambda _: self.sem.release())
            self.job_queue.task_done()

    def _run_download(self, task):
        if task.kind != "video" or task.state == "skipped":
            self._update_group_state(task.parent_group_id); return
        if task._cancelled:
            self._update_group_state(task.parent_group_id); return

        self._set_video_state(task, "downloading", M("starting"))
        cookies_tmp = self._cookies_tempcopy()
        cmd = [YTDLP_BIN, "-c", "--force-overwrites",
               "-f", "bestvideo+bestaudio/best",
               "--download-archive", ARCHIVE_FILE,
               "-o", self._output_template(),
               "--no-warnings"]
        if cookies_tmp:
            cmd += ["--cookies", cookies_tmp]
        cmd.append(task.url)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
        except FileNotFoundError:
            self._cleanup_cookies_tmp(cookies_tmp)
            self._set_video_state(task, "error", M("binary_not_found", tool=YTDLP_BIN))
            self._update_group_state(task.parent_group_id); return

        with self._procs_lock:
            self._procs[task.id] = proc

        last_error_line = ""
        dest_path = None   # final file path, for instant "locate"
        exc = None
        try:
            for line in proc.stdout:
                line = line.rstrip()
                m = PROGRESS_LINE_RE.search(line)
                with self.lock:
                    task.last_message = line
                    if m:
                        try:
                            task.progress_pct = float(m.group(1))
                            unit = m.group(3).upper()
                            task.speed_bps = float(m.group(2)) * UNIT_MULT.get(unit, 1)
                        except Exception:
                            pass
                # flat-playlist doesn't always provide a title — recover one
                # from the output filename.
                if not task.title and not m:
                    dm = DEST_LINE_RE.search(line) or ALREADY_LINE_RE.search(line)
                    if dm:
                        tm = FILENAME_TITLE_RE.match(os.path.basename(dm.group(1)))
                        if tm:
                            with self.lock:
                                task.title = tm.group(1)
                # Track the final output path: Merger (merged output) beats
                # Destination (excluding ".fNNN" fragments) beats an
                # already-downloaded file.
                if not m:
                    mm = MERGER_LINE_RE.search(line)
                    if mm:
                        dest_path = mm.group(1)
                    else:
                        dm = DEST_LINE_RE.search(line) or ALREADY_LINE_RE.search(line)
                        if dm and not PART_FILE_RE.search(dm.group(1)):
                            dest_path = dm.group(1)
                # Track last non-progress line for error reporting
                if line and not m:
                    last_error_line = line
                self._request_refresh()
                # Pausing also just terminates the process — yt-dlp -c resumes on restart.
                if self.global_stop.is_set() or task._cancelled or task._paused:
                    proc.terminate(); break
            proc.wait()
        except Exception as e:
            exc = e
            try: proc.terminate()
            except OSError: pass
        finally:
            self._cleanup_cookies_tmp(cookies_tmp)
            with self._procs_lock:
                self._procs.pop(task.id, None)
            with self.lock:
                task.speed_bps = 0.0

        if exc is not None:
            self._set_video_state(task, "error", M("exception", error=str(exc)))
        elif task._cancelled or self.global_stop.is_set():
            self._set_video_state(task, "paused", M("stopped_task"))
        elif task._paused:
            self._set_video_state(task, "paused", M("paused_msg"))
        elif "has already been recorded" in (task.last_message or ""):
            self._set_video_state(task, "skipped", M("already_downloaded"))
        elif proc.returncode == 0:
            with self.lock:
                task.progress_pct = 100.0
                if dest_path:
                    task.filepath = os.path.abspath(dest_path)
            self._set_video_state(task, "completed", M("video_completed"))
            self._register_filepath(task)
            self._on_video_completed(task)
        else:
            reason = last_error_line[:120] if last_error_line else ""
            self._set_video_state(task, "error", _exit_code_msg(proc.returncode, reason))

        self._update_group_state(task.parent_group_id)
        if task.state in ("completed","error","skipped","paused"):
            try:
                self.db.upsert_video(task.to_video_dict())
            except Exception as e:
                self._show_toast(M("db_save_failed", error=str(e)))

    def _on_video_completed(self, task):
        vid = self._extract_vid_id(task)
        if vid: self.db.mark_history(vid, task.url)
        with self.lock:   # multiple downloads can finish at the same instant
            self._cum_total += 1
            total = self._cum_total
        self.db.set_meta("completed_total", total)

    def _group_progress_message(self, children):
        """Recomputed every call, not cached, so it's always current."""
        remaining_new = sum(1 for c in children if c.state in ("queued","downloading","error","paused"))
        done_cnt       = sum(1 for c in children if c.state in ("completed","skipped"))
        return M("group_progress", remaining=remaining_new, done=done_cnt)

    def _update_group_state(self, gid):
        if not gid: return
        with self.lock:
            g = None
            ch = []
            for t in self.tasks:
                if t.id == gid and t.kind == "group":
                    g = t
                elif t.parent_group_id == gid and t.kind == "video":
                    ch.append(t)
            if g is None or (g.state == "error" and g.expected_count is None):
                pass  # group missing, or never resolved — keep red
            else:
                old_state, old_cnt = g.state, g.completed_count
                g.completed_count = sum(
                    1 for c in ch if c.state in ("completed","skipped"))
                new_state = _derive_group_state(ch)
                # Don't auto-demote a completed group back to queued/paused.
                if g.state == "completed" and new_state in ("queued", "paused"):
                    new_state = "completed"
                if new_state == "completed":
                    g.priority = 0   # a completed group loses top-priority status
                g.state = new_state
                g.last_message = self._group_progress_message(ch)
                if g.state != old_state or g.completed_count != old_cnt:
                    g.modified_at = time.time()
        self._request_refresh()

    # ══════════════════════════════════════════
    #  STATE HELPERS
    # ══════════════════════════════════════════
    def _set_video_state(self, task, state, msg=""):
        with self.lock:
            task.state, task.last_message = state, msg
        self._request_refresh()

    def _set_group_state(self, group, state, msg=""):
        with self.lock:
            group.state, group.last_message = state, msg
            group.modified_at = time.time()
        self._request_refresh()

    # ══════════════════════════════════════════
    #  TASK ACTIONS
    # ══════════════════════════════════════════
    def _pause_task(self, task):
        if task.state not in ("downloading","queued"): return
        task._paused = True; task._cancelled = False
        with self._procs_lock:
            proc = self._procs.get(task.id)
        if proc:
            try: proc.terminate()
            except OSError: pass
        self._set_video_state(task, "paused", M("paused_msg"))
        self._update_group_state(task.parent_group_id)

    def _stop_task(self, task):
        task._cancelled = True; task._paused = False
        with self._procs_lock:
            proc = self._procs.get(task.id)
        if proc:
            try: proc.terminate()
            except OSError: pass
        self._set_video_state(task, "paused", M("stopped_task"))
        self._update_group_state(task.parent_group_id)

    def _start_or_resume_task(self, task):
        if task.kind != "video": return
        task._paused = False; task._cancelled = False
        with self.lock:
            task.state, task.last_message = "queued", M("resuming")
        self.global_stop.clear()
        self._queue_put(task)
        self._update_group_state(task.parent_group_id)
        self._request_refresh()

    def _recheck_group(self, group):
        with self.lock:
            if group.state == "resolving":
                return  # already re-checking - queuing again could duplicate videos
            group.state = "resolving"
            group.last_message = M("rechecking")
            group.modified_at = time.time()
        self._request_refresh()
        self._resolve_queue.put(group)

    def _retry_errors_skipped(self, group):
        """Re-queue only error/skipped videos in a group without re-checking the
        playlist. Private/404 errors are excluded since they always fail again.
        Returns (retried count, excluded count)."""
        with self.lock:
            candidates = [t for t in self.tasks
                          if t.parent_group_id == group.id
                          and t.kind == "video"
                          and t.state in ("error", "skipped")]
        targets  = [t for t in candidates if not _is_permanent_error(t)]
        excluded = len(candidates) - len(targets)
        if not targets:
            return 0, excluded
        self.global_stop.clear()
        vids = []
        for t in targets:
            vid = self._extract_vid_id(t)
            if vid:
                vids.append(vid)
            t._paused = False; t._cancelled = False
            with self.lock:
                t.state, t.last_message = "queued", ""
        self.db.delete_history_many(vids)
        self._remove_from_archive_many(vids)
        self._enqueue_tasks(targets)
        self._request_refresh()
        return len(targets), excluded

    def _fresh_download(self, task):
        if task.kind == "group":
            with self.lock:
                children = [t for t in self.tasks if t.parent_group_id == task.id]
            # Batch-remove archive/history entries once instead of per video.
            vids = []
            for c in children:
                vid = self._extract_vid_id(c)
                if vid:
                    vids.append(vid)
                c._paused = False; c._cancelled = False
                with self.lock:
                    c.state, c.last_message = "queued", M("fresh_restart")
            self.db.delete_history_many(vids)
            self._remove_from_archive_many(vids)
            self.global_stop.clear()
            self._enqueue_tasks(children)
            self._recheck_group(task)
            return
        if task.kind != "video": return
        vid = self._extract_vid_id(task)
        if vid:
            self.db.delete_history(vid)
            self._remove_from_archive(vid)
        task._paused = False; task._cancelled = False
        with self.lock:
            task.state, task.last_message = "queued", M("fresh_restart")
        self.global_stop.clear()
        self._queue_put(task)
        self._update_group_state(task.parent_group_id)
        self._request_refresh()

    def _move_to_active(self, group):
        with self.lock:
            group.state = "queued"
            group.last_message = M("moved_to_active")
        self._request_refresh()

    # ══════════════════════════════════════════
    #  RESOLUTION FILTER  (redownload anything below the configured resolution)
    # ══════════════════════════════════════════
    def _probe_min_side(self, filepath):
        """Return the shorter of (width, height) via ffprobe, or None on failure."""
        try:
            r = subprocess.run(
                [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", filepath],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15)
            line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
            parts = [p for p in line.split(",") if p.strip().isdigit()]
            if len(parts) >= 2:
                w, h = int(parts[0]), int(parts[1])
                if w > 0 and h > 0:
                    return min(w, h)
        except Exception:
            pass
        return None

    def _res_check_all_done(self):
        with self.lock:
            done_groups = [t for t in self.tasks
                           if t.kind == "group" and t.state == "completed"]
        if not done_groups:
            self._set_done_status(M("no_done_groups"))
            return
        self._res_check_groups(done_groups)

    def _res_check_groups(self, groups):
        """Check completed/skipped videos in the given groups and re-download
        anything below the configured resolution threshold."""
        if FFPROBE_BIN == "ffprobe" and shutil.which("ffprobe") is None:
            self._show_toast(M("ffprobe_missing"))
            return
        with self.lock:
            gids = {g.id for g in groups}
            targets = [t for t in self.tasks
                       if t.kind == "video" and t.parent_group_id in gids
                       and t.state in ("completed", "skipped")]
        if not targets:
            self._show_toast(M("res_check_no_targets"))
            return
        base = os.path.abspath(self._cfg_output_dir or DEFAULT_OUTPUT_DIR)
        threshold = self._cfg_res_height
        self._set_done_status(M("res_check_collecting"))
        threading.Thread(target=self._res_check_worker,
                         args=(targets, base, threshold), daemon=True).start()

    def _set_done_status(self, msg):
        self.done_status = msg
        self._request_refresh()

    def _res_check_worker(self, targets, base, threshold):
        # 1) One os.walk pass builds a video-id -> filepath map.
        id_file = {}
        try:
            for dirpath, _, filenames in os.walk(base):
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() not in VIDEO_FILE_EXTS:
                        continue
                    m = RES_FILE_ID_RE.search(fn)
                    if m:
                        id_file[m.group(1)] = os.path.join(dirpath, fn)
        except Exception as e:
            print(f"[res_filter] walk: {e}")

        # 2) Match targets to files.
        pairs, missing = [], 0
        for t in targets:
            vid = self._extract_vid_id(t)
            fp = id_file.get(vid) if vid else None
            if fp:
                pairs.append((t, fp))
            else:
                missing += 1

        # 3) Parallel ffprobe — a short side below threshold, or an unreadable
        # (corrupt) file, both trigger a re-download.
        total = len(pairs)
        done_box = [0]
        plock = threading.Lock()

        def probe_one(pair):
            t, fp = pair
            side = self._probe_min_side(fp)
            with plock:
                done_box[0] += 1
                d = done_box[0]
            if d % 25 == 0 or d == total:
                self._set_done_status(M("res_check_progress", done=d, total=total))
            return t, fp, side

        low = []   # [(task, filepath)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for t, fp, side in ex.map(probe_one, pairs):
                if side is None or side < threshold:
                    low.append((t, fp))

        if low:
            self._apply_res_redownload(low)
        summary = (M("res_check_summary_missing", total=total, redownload=len(low), missing=missing)
                   if missing else M("res_check_summary", total=total, redownload=len(low)))
        self._set_done_status(summary)
        self._show_toast(summary)

    def _apply_res_redownload(self, low, msg=None):
        """Reset below-threshold videos for re-download (clears archive/history,
        re-queues). Files aren't deleted — yt-dlp --force-overwrites replaces them."""
        if msg is None:
            msg = M("resolution_low_requeue")
        low = [t for t, _fp in low]
        gids = set()
        with self.lock:
            for t in low:
                t._paused = False
                t._cancelled = False
                t.state = "queued"
                t.last_message = msg
                if t.parent_group_id:
                    gids.add(t.parent_group_id)
            # Move completed groups back to active, bypassing the completed-state guard.
            for g in self.tasks:
                if g.kind == "group" and g.id in gids and g.state == "completed":
                    g.state = "queued"
        vids = [v for v in (self._extract_vid_id(t) for t in low) if v]
        self.db.delete_history_many(vids)
        self._remove_from_archive_many(vids)
        try:
            with self.lock:
                gdicts = [g.to_group_dict() for g in self.tasks
                          if g.kind == "group" and g.id in gids]
            self.db.save_snapshot(gdicts, [t.to_video_dict() for t in low])
        except Exception as e:
            print(f"[res_filter] save failed: {e}")
        if not self.global_stop.is_set():
            self._enqueue_tasks(low)
        for gid in gids:
            self._update_group_state(gid)
        self._request_refresh()

    # ══════════════════════════════════════════
    #  SIZE FILTER — re-download videos at or below a size threshold (MB)
    #  (mirrors the resolution filter above)
    # ══════════════════════════════════════════
    def _size_check_all_done(self):
        with self.lock:
            done_groups = [t for t in self.tasks
                           if t.kind == "group" and t.state == "completed"]
        if not done_groups:
            self._set_done_status(M("no_done_groups"))
            return
        self._size_check_groups(done_groups)

    def _size_check_groups(self, groups):
        with self.lock:
            gids = {g.id for g in groups}
            targets = [t for t in self.tasks
                       if t.kind == "video" and t.parent_group_id in gids
                       and t.state in ("completed", "skipped")]
        if not targets:
            self._show_toast(M("size_check_no_targets"))
            return
        base = os.path.abspath(self._cfg_output_dir or DEFAULT_OUTPUT_DIR)
        threshold = self._cfg_size_mb * 1024 * 1024
        self._set_done_status(M("size_check_collecting"))
        threading.Thread(target=self._size_check_worker,
                         args=(targets, base, threshold), daemon=True).start()

    def _size_check_worker(self, targets, base, threshold):
        # 1) One os.walk pass builds a video-id -> filepath map (same as res_check).
        id_file = {}
        try:
            for dirpath, _, filenames in os.walk(base):
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() not in VIDEO_FILE_EXTS:
                        continue
                    m = RES_FILE_ID_RE.search(fn)
                    if m:
                        id_file[m.group(1)] = os.path.join(dirpath, fn)
        except Exception as e:
            print(f"[size_filter] walk: {e}")

        # 2) Match targets to files.
        pairs, missing = [], 0
        for t in targets:
            vid = self._extract_vid_id(t)
            fp = id_file.get(vid) if vid else None
            if fp:
                pairs.append((t, fp))
            else:
                missing += 1

        # 3) Parallel stat — at/below threshold, or unreadable, triggers a re-download.
        total = len(pairs)
        done_box = [0]
        plock = threading.Lock()

        def size_one(pair):
            t, fp = pair
            try:
                size = os.path.getsize(fp)
            except Exception:
                size = None
            with plock:
                done_box[0] += 1
                d = done_box[0]
            if d % 200 == 0 or d == total:
                self._set_done_status(M("size_check_progress", done=d, total=total))
            return t, fp, size

        low = []   # [(task, filepath)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for t, fp, size in ex.map(size_one, pairs):
                if size is None or size <= threshold:
                    low.append((t, fp))

        if low:
            self._apply_res_redownload(low, msg=M("size_low_requeue"))
        threshold_mb = threshold // (1024 * 1024)
        summary = (M("size_check_summary_missing", total=total, redownload=len(low),
                     threshold_mb=threshold_mb, missing=missing)
                   if missing else
                   M("size_check_summary", total=total, redownload=len(low), threshold_mb=threshold_mb))
        self._set_done_status(summary)
        self._show_toast(summary)

    # ══════════════════════════════════════════
    #  MISSING FILE CHECK — completed in the DB but the file itself is gone (2-step confirm)
    # ══════════════════════════════════════════
    def missing_check(self, ids=None):
        """Find completed/skipped videos whose file is missing from the output
        folder (synchronous scan). Doesn't re-download by itself - returns a
        result + token, and confirm_missing_redownload(token) does that.
        ids=None checks every completed group."""
        with self.lock:
            if ids is None:
                gids = {t.id for t in self.tasks
                        if t.kind == "group" and t.state == "completed"}
            else:
                gids = {t.id for t in self.tasks
                        if t.kind == "group" and t.id in set(ids)}
            targets = [t for t in self.tasks
                       if t.kind == "video" and t.parent_group_id in gids
                       and t.state in ("completed", "skipped")]
        if not targets:
            return {"error": M("no_completed_to_check")}
        base = os.path.abspath(self._cfg_output_dir or DEFAULT_OUTPUT_DIR)
        if not os.path.isdir(base):
            return {"error": M("output_dir_missing", path=base)}
        # Always a fresh full scan (no cache) — a stale cache could report an
        # existing file as missing.
        present = set()
        try:
            for dirpath, dirnames, filenames in os.walk(base):
                # Synology metadata/recycle bin folders aren't real content — skip them.
                dirnames[:] = [d for d in dirnames if d not in ("@eaDir", "#recycle")]
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() not in VIDEO_FILE_EXTS:
                        continue
                    m = RES_FILE_ID_RE.search(fn)
                    if m:
                        present.add(m.group(1))
        except Exception as e:
            return {"error": M("folder_scan_failed", error=str(e))}
        if not present:
            # An unmounted output folder would otherwise look "100% missing" — abort instead.
            return {"error": M("output_dir_empty_abort")}
        missing, noid = [], 0
        for t in targets:
            vid = self._extract_vid_id(t)
            if not vid:
                noid += 1   # can't verify without an id — excluded
                continue
            if vid not in present:
                missing.append(t)
        result = {"checked": len(targets), "noid": noid, "token": None,
                  "missing": [{"title": t.title or t.url} for t in missing]}
        if missing:
            token = secrets.token_hex(8)
            self._pending_missing[token] = missing
            while len(self._pending_missing) > 8:
                self._pending_missing.pop(next(iter(self._pending_missing)))
            result["token"] = token
        return result

    def confirm_missing_redownload(self, token):
        tasks = self._pending_missing.pop(token, None)
        if not tasks:
            return 0
        # Skip anything whose state changed since the check (e.g. already re-queued).
        low = [(t, None) for t in tasks if t.state in ("completed", "skipped")]
        if low:
            self._apply_res_redownload(low, msg=M("missing_requeue_msg"))
            self._show_toast(M("missing_requeue", count=len(low)))
        return len(low)

    # ── delete helpers ──
    def _extract_vid_id(self, task):
        if getattr(task, "extractor_id", None):
            return task.extractor_id
        url = task.url or ""
        m = re.search(r'(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})', url)
        return m.group(1) if m else None

    def _filepath_backfill_worker(self):
        """One-time backfill for older rows missing `filepath`: a single full
        scan fills them in so "locate" (open in explorer) answers instantly
        afterwards. A meta flag prevents repeating this on every boot (newly
        completed videos already record filepath at download time)."""
        if self.db.get_bool("filepath_backfill_done", False):
            return
        time.sleep(20)   # let autostart downloads settle first
        with self.lock:
            missing = [t for t in self.tasks
                       if t.kind == "video" and t.state in ("completed", "skipped")
                       and not t.filepath]
        updates = []
        if missing:
            base = os.path.abspath(self._cfg_output_dir or DEFAULT_OUTPUT_DIR)
            id_file = self._locate_id_map(base)
            with self.lock:
                for t in missing:
                    vid = self._extract_vid_id(t)
                    fp = id_file.get(vid) if vid else None
                    if fp:
                        t.filepath = fp
                        updates.append((fp, t.id))
            if updates:
                try:
                    self.db.set_filepaths(updates)
                except Exception as e:
                    self._show_toast(M("filepath_backfill_save_failed", error=str(e)))
                    return   # flag not set — retried on next boot
            self._show_toast(M("filepath_backfill_done", count=len(updates)))
        self.db.set_meta("filepath_backfill_done", "1")

    def _register_filepath(self, task):
        """Update the locate cache immediately for a just-completed video."""
        if not task.filepath:
            return
        vid = self._extract_vid_id(task)
        cached = getattr(self, "_locate_cache", None)
        if vid and cached:
            cached[2][vid] = task.filepath

    def _locate_id_map(self, base):
        """video-id -> filepath map (same os.walk scan as res_check) — a
        fallback for older completed videos with no filepath in the DB. A
        full scan can take a while on a large/networked folder, so it's
        cached for 30 minutes. (Newly completed videos record filepath
        directly and never reach this fallback.)"""
        cached = getattr(self, "_locate_cache", None)
        if cached and cached[0] == base and time.time() - cached[1] < 1800:
            return cached[2]
        id_file = {}
        try:
            for dirpath, _, filenames in os.walk(base):
                for fn in filenames:
                    m = RES_FILE_ID_RE.search(fn)
                    if m:
                        id_file[m.group(1)] = os.path.join(dirpath, fn)
        except Exception as e:
            print(f"[locate] walk: {e}")
        self._locate_cache = (base, time.time(), id_file)
        return id_file

    def locate_path(self, tid):
        """Find the real file (video) / folder (group) path for a task, for
        the desktop "open" helper.
        1) filepath recorded in the DB (set at completion / backfilled) — instant
        2) fallback: full scan map (older data, or a file that moved)
        Returns {"base", "rel", "kind"} or None."""
        base = os.path.abspath(self._cfg_output_dir or DEFAULT_OUTPUT_DIR)
        with self.lock:
            task = next((t for t in self.tasks if t.id == tid), None)
            if not task:
                return None
            if task.kind == "video":
                cands = [task]
            else:
                cands = [t for t in self.tasks
                         if t.kind == "video" and t.parent_group_id == task.id]
            paths = [t.filepath for t in cands if t.filepath]
            vids  = [self._extract_vid_id(t) for t in cands]

        def _result(fp):
            if task.kind == "video":
                return {"base": base, "rel": os.path.relpath(fp, base),
                        "kind": "file"}
            return {"base": base,
                    "rel": os.path.relpath(os.path.dirname(fp), base),
                    "kind": "dir"}

        for fp in paths:
            # Skip stale paths (output folder changed, or file was deleted).
            if fp.startswith(base + os.sep) and os.path.isfile(fp):
                return _result(fp)

        vids = [v for v in vids if v]
        if not vids:
            return None
        id_file = self._locate_id_map(base)
        for v in vids:
            fp = id_file.get(v)
            if fp:
                return _result(fp)
        return None

    def _delete_task_only(self, task):
        task._cancelled = True
        with self._procs_lock:
            proc = self._procs.pop(task.id, None)
        if proc:
            try: proc.terminate()
            except OSError: pass
        group_to_save = None
        with self.lock:
            if task.kind == "group":
                cids = {t.id for t in self.tasks if t.parent_group_id == task.id}
                self.tasks = [t for t in self.tasks
                              if t.id != task.id and t.id not in cids]
                self.db.delete_videos_for_group(task.id)
                self.db.delete_group(task.id)
            else:
                self.tasks = [t for t in self.tasks if t.id != task.id]
                self.db.delete_video(task.id)
                gid = task.parent_group_id
                if gid:
                    for g in self.tasks:
                        if g.id == gid and g.kind == "group":
                            ch = [t for t in self.tasks
                                  if t.parent_group_id == gid and t.kind == "video"]
                            g.expected_count  = len(ch)
                            g.completed_count = sum(
                                1 for c in ch if c.state in ("completed","skipped"))
                            group_to_save = g.to_group_dict()
                            break
        if group_to_save:
            try:
                self.db.upsert_group(group_to_save)
            except Exception:
                pass
        self._request_refresh()

    def scan_files_for_delete(self, vid_ids):
        """Single os.walk() pass — returns the files to delete and issues a
        token. The actual delete happens in confirm_delete_files(token)."""
        base = os.path.abspath(self._cfg_output_dir or DEFAULT_OUTPUT_DIR)
        # Match the "[ID]" form from the output template exactly - a plain
        # substring match risks matching an unrelated file whose name
        # happens to contain a short id.
        vid_markers = {f"[{v}]" for v in vid_ids}
        files = []
        try:
            for dirpath, dirnames, filenames in os.walk(base):
                for fn in filenames:
                    for marker in vid_markers:
                        if marker in fn:
                            files.append(os.path.join(dirpath, fn))
                            break
        except Exception as e:
            print(f"[walk] {e}")
        files = list(dict.fromkeys(files))
        if not files:
            return None, []
        token = secrets.token_hex(8)
        self._pending_deletes[token] = files
        # Keep only the most recent 8 pending confirmations.
        while len(self._pending_deletes) > 8:
            self._pending_deletes.pop(next(iter(self._pending_deletes)))
        return token, files

    def confirm_delete_files(self, token):
        files = self._pending_deletes.pop(token, None)
        if not files:
            return False
        threading.Thread(target=self._run_file_delete, args=(files,), daemon=True).start()
        return True

    def _run_file_delete(self, files):
        total = len(files)
        errors = []
        for i, f in enumerate(files, 1):
            try:
                if os.path.isfile(f):  os.remove(f)
                elif os.path.isdir(f): shutil.rmtree(f)
            except Exception as e:
                errors.append(f"{f}: {e}")
            if i % 50 == 0:
                self._set_done_status(M("file_delete_progress", done=i, total=total))
        deleted = total - len(errors)
        msg = M("file_delete_done_failed", deleted=deleted, failed=len(errors)) if errors \
              else M("file_delete_done", deleted=deleted)
        self._set_done_status("")
        self._show_toast(msg)

    def delete_tasks(self, ids, with_files=False):
        """Delete tasks. If with_files, scans for files first and returns
        (token, files) for a 2-step confirmation."""
        with self.lock:
            tasks = [t for t in self.tasks if t.id in set(ids)]
        vid_ids = []
        if with_files:
            for t in tasks:
                if t.kind == "group":
                    with self.lock:
                        for c in self.tasks:
                            if c.parent_group_id == t.id:
                                v = self._extract_vid_id(c)
                                if v: vid_ids.append(v)
                else:
                    v = self._extract_vid_id(t)
                    if v: vid_ids.append(v)
            vid_ids = list(dict.fromkeys(vid_ids))
        for t in tasks:
            self._delete_task_only(t)
        if with_files and vid_ids:
            return self.scan_files_for_delete(vid_ids)
        return None, []

    # ══════════════════════════════════════════
    #  BULK OPS (done tab)
    # ══════════════════════════════════════════
    def _recheck_all_done(self):
        with self.lock:
            done_groups = [t for t in self.tasks
                           if t.kind == "group" and t.state == "completed"]
        if not done_groups:
            self._set_done_status(M("no_done_groups"))
            return
        for g in done_groups:
            self._recheck_group(g)
        self._set_done_status(M("bulk_recheck_progress", count=len(done_groups)))

    def _retry_all_errors_skipped(self):
        with self.lock:
            done_groups = [t for t in self.tasks
                           if t.kind == "group" and t.state == "completed"]
        if not done_groups:
            self._set_done_status(M("no_done_groups"))
            return
        retried = excluded = 0
        for g in done_groups:
            r, x = self._retry_errors_skipped(g)
            retried += r; excluded += x
        summary = M("bulk_retry_summary_excluded", retried=retried, excluded=excluded) if excluded \
                  else M("bulk_retry_summary", retried=retried)
        self._set_done_status(summary)

    def _redownload_all_done(self):
        with self.lock:
            done_groups = [t for t in self.tasks
                           if t.kind == "group" and t.state == "completed"]
        if not done_groups:
            self._set_done_status(M("no_done_groups"))
            return
        self._set_done_status(M("bulk_redownload_progress", count=len(done_groups)))
        for g in done_groups:
            self._fresh_download(g)

    # ══════════════════════════════════════════
    #  PRIORITY / REORDER  (context menu + drag-and-drop)
    # ══════════════════════════════════════════
    def set_top_priority(self, ids):
        with self.lock:
            sel = [t for t in self.tasks if t.id in set(ids)]
        target_gids = set()
        for t in sel:
            if t.kind == "group":
                target_gids.add(t.id)
            elif t.parent_group_id:
                target_gids.add(t.parent_group_id)
        if not target_gids:
            return
        with self.lock:
            target_ids = {t.id for t in self.tasks
                          if (t.kind == "group" and t.id in target_gids) or
                             (t.kind == "video" and t.parent_group_id in target_gids)}
            priority = [t for t in self.tasks if t.id in target_ids]
            rest     = [t for t in self.tasks if t.id not in target_ids]
            self.tasks = priority + rest
            # priority flag persists through _reorder_tasks_* calls
            for t in priority:
                if t.kind == "group":
                    t.priority = 1
            groups_ordered = [t for t in self.tasks if t.kind == "group"]
        for i, g in enumerate(groups_ordered):
            g.sort_order = i
        threading.Thread(target=self._persist_group_order,
                         args=(groups_ordered,), daemon=True).start()
        # Rebuild the queue from self.tasks order so priority groups download first.
        self._requeue_by_tasks_order([])
        self._request_refresh()

    def reorder_groups(self, ordered_gids):
        """Apply a drag-and-drop reorder: rearrange self.tasks to match
        ordered_gids and persist it."""
        order = {gid: i for i, gid in enumerate(ordered_gids)}
        with self.lock:
            groups, vmap, orphans = [], {}, []
            for t in self.tasks:
                if t.kind == "group":
                    groups.append(t)
                elif t.parent_group_id:
                    vmap.setdefault(t.parent_group_id, []).append(t)
                else:
                    orphans.append(t)
            groups.sort(key=lambda g: order.get(g.id, 999999))
            ordered = []
            for g in groups:
                ordered.append(g)
                ordered.extend(vmap.get(g.id, []))
            ordered.extend(orphans)
            self.tasks = ordered
            for i, g in enumerate(groups):
                g.sort_order = i
            groups_ordered = list(groups)
        threading.Thread(target=self._persist_group_order,
                         args=(groups_ordered,), daemon=True).start()
        self._requeue_by_tasks_order([])
        self._request_refresh()

    def _persist_group_order(self, groups):
        try:
            self.db.save_snapshot([g.to_group_dict() for g in groups], [])
        except Exception as e:
            print(f"[persist_order] {e}")

    def _find_group(self, gid):
        with self.lock:
            return next((t for t in self.tasks if t.id == gid and t.kind=="group"), None)

    def _groups_for_ids(self, ids):
        """Resolve a selection of groups/videos down to their unique parent groups."""
        with self.lock:
            gmap = {t.id: t for t in self.tasks if t.kind == "group"}
            sel  = [t for t in self.tasks if t.id in set(ids)]
        groups, seen = [], set()
        for t in sel:
            gid = t.id if t.kind == "group" else t.parent_group_id
            if gid and gid not in seen:
                seen.add(gid)
                g = gmap.get(gid)
                if g:
                    groups.append(g)
        return groups

    # ══════════════════════════════════════════
    #  API DISPATCH (context menu actions)
    # ══════════════════════════════════════════
    def apply_action(self, ids, action):
        with self.lock:
            sel = [t for t in self.tasks if t.id in set(ids)]
        if action == "start":
            for t in sel:
                if t.kind == "group": self._start_group(t)
                else: self._start_or_resume_task(t)
        elif action == "pause":
            for t in sel:
                if t.kind == "video": self._pause_task(t)
                else: self._stop_group(t)
        elif action == "stop":
            for g in self._groups_for_ids(ids):
                self._stop_group(g)
        elif action == "stop_video":
            for t in sel:
                if t.kind == "video": self._stop_task(t)
        elif action == "retry":
            retried = excluded = 0
            for g in self._groups_for_ids(ids):
                r, x = self._retry_errors_skipped(g)
                retried += r; excluded += x
            if excluded:
                self._show_toast(M("retry_summary_excluded", retried=retried, excluded=excluded))
        elif action == "recheck":
            for t in sel:
                if t.kind == "group": self._recheck_group(t)
                elif t.parent_group_id:
                    g = self._find_group(t.parent_group_id)
                    if g: self._recheck_group(g)
        elif action == "fresh":
            for t in sel:
                self._fresh_download(t)
        elif action == "priority":
            self.set_top_priority(ids)
        elif action == "move_active":
            for t in sel:
                if t.kind == "group": self._move_to_active(t)
        elif action == "res_check":
            groups = self._groups_for_ids(ids)
            if groups:
                self._res_check_groups(groups)
        else:
            raise ValueError(f"unknown action: {action}")

    # ══════════════════════════════════════════
    #  SETTINGS API
    # ══════════════════════════════════════════
    def get_settings(self):
        return {
            "autostart":            self._cfg_autostart,
            "max_concurrent":       self._cfg_max_concurrent,
            "autosave_minutes":     self._cfg_autosave_min,
            "output_dir":           self._cfg_output_dir,
            "save_on_add":          self._cfg_save_on_add,
            "small_group_first":    self._cfg_small_first,
            "high_progress_first":  self._cfg_high_progress_first,
            "res_filter_height":    self._cfg_res_height,
            "size_filter_mb":       self._cfg_size_mb,
            "font_scale":           self._cfg_font_size,
            "gallery_folder_template": self._cfg_gallery_template,
            "persist_patterns":     "\n".join(self._cfg_persist_patterns),
            "cookies_status":       self._cookies_status(),
        }

    @staticmethod
    def _cookies_status():
        try:
            st = os.stat(COOKIES_FILE)
            if st.st_size > 0:
                return {"saved": True,
                        "at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")}
        except OSError:
            pass
        return {"saved": False, "at": None}

    def _save_cookies(self, s):
        """Handle the cookies field of a settings save — only replaces the
        file when a non-empty value is sent, so an empty textarea doesn't
        wipe out a previously saved file."""
        if s.get("cookies_clear"):
            try: os.remove(COOKIES_FILE)
            except OSError: pass
            self._show_toast(M("cookies_cleared"))
            return
        ck = (s.get("cookies") or "").strip()
        if not ck:
            return
        lines = ck.replace("\r\n", "\n").split("\n")
        # A cookies.txt (Netscape) line has at least 7 tab-separated fields.
        if not any(l.count("\t") >= 6 for l in lines):
            self._show_toast(M("cookies_invalid_format"))
            return
        body = "\n".join(lines)
        if "HTTP Cookie File" not in lines[0]:
            # yt-dlp expects this magic header — add it if missing.
            body = "# Netscape HTTP Cookie File\n" + body
        tmp = COOKIES_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(body + "\n")
            os.replace(tmp, COOKIES_FILE)
            self._show_toast(M("cookies_saved"))
        except OSError as e:
            self._show_toast(M("cookies_save_failed", error=str(e)))

    def set_settings(self, s):
        old_max = self._cfg_max_concurrent
        new_max = max(1, min(64, int(s.get("max_concurrent", old_max))))
        self._cfg_max_concurrent = new_max
        # Apply concurrency change immediately without restart:
        # increase = release extra tokens; decrease = acquire the surplus
        # in a background thread (blocks until running downloads free slots).
        if new_max > old_max:
            for _ in range(new_max - old_max):
                self.sem.release()
        elif new_max < old_max:
            def _shrink(n=old_max - new_max):
                for _ in range(n):
                    self.sem.acquire()
            threading.Thread(target=_shrink, daemon=True).start()

        self._cfg_autostart      = bool(s.get("autostart", self._cfg_autostart))
        self._cfg_autosave_min   = max(0, int(s.get("autosave_minutes", self._cfg_autosave_min)))
        self._cfg_output_dir     = (s.get("output_dir") or "").strip() or DEFAULT_OUTPUT_DIR
        self._cfg_save_on_add    = bool(s.get("save_on_add", self._cfg_save_on_add))
        self._cfg_small_first         = bool(s.get("small_group_first", self._cfg_small_first))
        self._cfg_high_progress_first = bool(s.get("high_progress_first", self._cfg_high_progress_first))
        self._cfg_res_height          = int(s.get("res_filter_height", self._cfg_res_height))
        self._cfg_size_mb             = max(1, int(s.get("size_filter_mb", self._cfg_size_mb)))
        self._cfg_font_size           = int(s.get("font_scale", self._cfg_font_size))
        self._cfg_gallery_template    = (s.get("gallery_folder_template") or "").strip() \
                                        or DEFAULT_GALLERY_TEMPLATE
        if "persist_patterns" in s:
            self._cfg_persist_patterns = self._parse_patterns(s.get("persist_patterns"))
        self._autosave_interval  = (self._cfg_autosave_min * 60
                                    if self._cfg_autosave_min > 0 else AUTOSAVE_INTERVAL)
        self.db.set_meta("autostart",           "1" if self._cfg_autostart else "0")
        self.db.set_meta("max_concurrent",      str(new_max))
        self.db.set_meta("autosave_minutes",    str(self._cfg_autosave_min))
        self.db.set_meta("output_dir",          self._cfg_output_dir)
        self.db.set_meta("save_on_add",         "1" if self._cfg_save_on_add else "0")
        self.db.set_meta("small_group_first",   "1" if self._cfg_small_first else "0")
        self.db.set_meta("high_progress_first", "1" if self._cfg_high_progress_first else "0")
        self.db.set_meta("res_filter_height",   str(self._cfg_res_height))
        self.db.set_meta("size_filter_mb",      str(self._cfg_size_mb))
        self.db.set_meta("font_scale",          str(self._cfg_font_size))
        self.db.set_meta("gallery_folder_template", self._cfg_gallery_template)
        self.db.set_meta("persist_patterns",    "\n".join(self._cfg_persist_patterns))
        self._save_cookies(s)
        self._request_refresh()

    # ══════════════════════════════════════════
    #  STATE SNAPSHOT (sent to the client as JSON)
    # ══════════════════════════════════════════
    def _matches_search(self, t, ql):
        if not ql:
            return True
        return (ql in (t.url or "").lower()
                or ql in (t.title or "").lower()
                or ql in (t.last_message or "").lower())

    def snapshot(self, q="", expanded=()):
        """Collapsed groups don't send their children down. While searching,
        every child is scanned to decide a match, but only matching groups
        (and their matching children) are returned."""
        with self.lock:
            all_tasks = list(self.tasks)

        ql = (q or "").strip().lower()
        expanded_ids = set(expanded)
        need_all_vbg = bool(ql)

        groups_active, groups_done, orphans = [], [], []
        vbg: dict = {}
        dling_per_group: dict = {}
        dling_tasks = []
        total = done = skip = 0
        for t in all_tasks:
            if t.kind == "group":
                if t.state == "completed":
                    groups_done.append(t)
                else:
                    groups_active.append(t)
            else:
                st = t.state
                if st == "skipped":
                    skip += 1
                else:
                    total += 1
                    if st == "completed":
                        done += 1
                    elif st == "downloading":
                        dling_tasks.append(t)
                        if t.parent_group_id:
                            dling_per_group[t.parent_group_id] = \
                                dling_per_group.get(t.parent_group_id, 0) + 1
                gid = t.parent_group_id
                if gid:
                    if need_all_vbg or gid in expanded_ids:
                        vbg.setdefault(gid, []).append(t)
                else:
                    orphans.append(t)

        if ql:
            groups_active = [g for g in groups_active
                             if self._matches_search(g, ql) or
                             any(self._matches_search(v, ql) for v in vbg.get(g.id, []))]
            groups_done = [g for g in groups_done
                           if self._matches_search(g, ql) or
                           any(self._matches_search(v, ql) for v in vbg.get(g.id, []))]

        def g_json(g, rank=None):
            pct = 0
            if g.expected_count:
                pct = int(round(100 * (g.completed_count or 0) / g.expected_count))
            d = {
                "id": g.id, "kind": "group", "url": g.url,
                "state": g.state,
                "expected": g.expected_count, "completed": g.completed_count,
                "pct": pct,
                "dling": dling_per_group.get(g.id, 0),
                "message": (g.last_message or "")[:200],
                "priority": g.priority,
                "created_at": g.created_at, "modified_at": g.modified_at,
                "expanded": g.id in expanded_ids,
            }
            if rank is not None:
                d["rank"] = rank
            if g.id in expanded_ids:
                kids = vbg.get(g.id, [])
                # While searching: matching groups show every child, others
                # only show the children that themselves matched.
                if ql and not self._matches_search(g, ql):
                    kids = [v for v in kids if self._matches_search(v, ql)]
                d["children"] = [v_json(v) for v in kids]
            return d

        def v_json(v):
            return {
                "id": v.id, "kind": "video", "url": v.url,
                "title": v.title or "",
                "state": v.state,
                "pct": round(v.progress_pct, 1) if v.state == "downloading" else None,
                "speed_bps": v.speed_bps if v.state == "downloading" else 0,
                "message": (v.last_message or "")[:200],
            }

        active = [g_json(g, rank=i+1) for i, g in enumerate(groups_active)]
        done_g = [g_json(g) for g in groups_done]
        orphan_rows = [v_json(v) for v in orphans if self._matches_search(v, ql)]

        dling = len(dling_tasks)
        speeds = [t.speed_bps for t in dling_tasks if t.speed_bps]
        avg_speed = sum(speeds) / len(speeds) if speeds else 0

        with self._ephemeral_lock:
            ephemeral = [
                {"id": e["id"], "kind": e["kind"], "url": e["url"],
                 "title": e.get("title") or e["url"],
                 "state": e["state"], "message": e.get("message", "")}
                for e in self._ephemeral
            ]

        return {
            "version":     self.version,
            "active":      active,
            "done":        done_g,
            "orphans":     orphan_rows,
            "ephemeral":   ephemeral,
            "stats": {
                "video_done": done, "video_total": total, "skipped": skip,
                "downloading": dling, "cum_total": self._cum_total,
                "avg_speed_bps": avg_speed,
            },
            "done_status": self.done_status,
            "output_dir":  self._cfg_output_dir,
        }

    # ══════════════════════════════════════════
    #  SHUTDOWN
    # ══════════════════════════════════════════
    def shutdown(self):
        if self._closing.is_set():
            return
        self._closing.set()
        self.global_stop.set()
        self._drain_queue()
        with self._procs_lock:
            for p in self._procs.values():
                try: p.terminate()
                except Exception: pass
        self._save_all(silent=True)
        try: self.executor.shutdown(wait=False)
        except Exception: pass
        try: self.db.close()
        except Exception: pass
