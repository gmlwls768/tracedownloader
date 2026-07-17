"""
Download engine for TraceDownloader - package entry point.

This package has no UI code in it - server.py exposes it over HTTP/SSE,
app.py drives it directly for the desktop build. It owns the task queue,
the SQLite task list, and every subprocess call to yt-dlp / gallery-dl.

Status text is never hardcoded here. Every user-facing message is built
with M(code, **params) (see models.py) and stored as "code" or
"code:{json params}". Each front end resolves the final sentence from its
own i18n table, so a single engine can drive an English or Korean UI
without knowing which one is active.

Split across this package by concern - see each module's docstring:
  models.py       DB, Task, module-level constants/helpers, M()
  ephemeral.py    session-only video/gallery download
  resolve.py      URL intake + persistent-group playlist resolve
  workers.py      download queue/workers, start-stop-reorder actions
  maintenance.py  done-tab tools, delete, apply_action() dispatch
  updater.py      background yt-dlp/gallery-dl self-update
  settings.py     settings get/set, cookies, state snapshot
This file (__init__.py) only keeps __init__ and the handful of methods
several of the above equally depend on (load/save, shutdown, notify).
"""

import threading
import concurrent.futures
import itertools
import queue
import os
import time
import re
import shutil
import tempfile
from collections import deque

from .models import *  # noqa: F401,F403 - internal package, see models.py __all__

from .ephemeral import _EphemeralMixin
from .resolve import _ResolveMixin
from .workers import _QueueMixin
from .maintenance import _MaintenanceMixin
from .updater import _UpdaterMixin
from .settings import _SettingsMixin


class Engine(_EphemeralMixin, _ResolveMixin, _QueueMixin, _MaintenanceMixin,
             _UpdaterMixin, _SettingsMixin):
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
        self._cfg_site_output_folders = self._parse_site_folders(
            self.db.get_meta("site_output_folders", ""))
        self._cfg_auto_update_tools   = self.db.get_bool("auto_update_tools", True)
        self._cfg_recheck_days        = self.db.get_int("recheck_interval_days", 0)
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

        # Tool version cache for the Settings screen: path -> (mtime, version).
        # Warmed in the background below so the first Settings open is instant.
        self._tool_ver_cache = {}

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
        threading.Thread(target=self._update_check_loop, daemon=True).start()
        threading.Thread(target=self._auto_recheck_loop, daemon=True).start()
        threading.Thread(target=self._warm_tool_versions, daemon=True).start()

        if self._cfg_autostart:
            threading.Timer(0.6, self._start_all).start()

    # ══════════════════════════════════════════
    #  NOTIFY / LOAD-SAVE / SHUTDOWN
    #  (shared by enough of the mixins above that they stay here rather
    #  than in any one of them)
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

    def _output_template(self, url=""):
        d = self._resolve_output_base(url)
        return OUTPUT_TEMPLATE_TPL.replace("{dir}", d)

    def _ephemeral_video_template(self, url=""):
        d = self._resolve_output_base(url)
        return os.path.join(d, "%(extractor_key)s", "%(uploader)s",
                             "%(upload_date>%Y-%m-%d)s - %(title)s [%(id)s].%(ext)s")

    def _gallery_output_dir(self, url=""):
        """Gallery downloads go under a "gallery" subfolder of the default
        output dir — but when a per-site output folder matched, the user
        already said exactly where this site's stuff belongs, so that
        subfolder is used as-is (no extra "gallery" level)."""
        base = self._resolve_output_base(url)
        if url and base != (self._cfg_output_dir or DEFAULT_OUTPUT_DIR):
            return base
        return os.path.join(base, "gallery")

    def _gallery_dirfmt(self, url=""):
        """The user's gallery folder template, translated to gallery-dl's
        format-string syntax so listing downloads (artist/tag pages) name
        every child gallery the same way the single-gallery path does.

        Field-driven, not site-driven: the placeholders map to whichever of
        several common gallery-dl field names is present, each with an empty
        fallback so a missing field yields "" rather than the literal
        "None"/"Unknown". A site whose galleries lack these fields simply
        gets emptier names — nothing site-specific is hardcoded."""
        tpl = self._cfg_gallery_template or DEFAULT_GALLERY_TEMPLATE
        return (tpl.replace("{artist}", "{artist|tags_artist|group|uploader:J, }")
                   .replace("{title}", "{title|title_jpn|'untitled'}")
                   .replace("{id}", "{gallery_id|gid|id|''}"))

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
