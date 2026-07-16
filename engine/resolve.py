"""
URL intake, persist-pattern routing, and persistent-group playlist resolve.
"""

import subprocess
import threading
import queue
import json
import os
import time
import re

from .models import *  # noqa: F401,F403 - internal package, see models.py __all__


class _ResolveMixin:
    @staticmethod
    def _parse_patterns(text):
        """One pattern per line. A leading "!" marks an exclusion; a leading
        http(s):// scheme is dropped from the pattern (it never helps a
        substring match, and "https://youtube.com" would otherwise silently
        fail to match "https://www.youtube.com/...")."""
        pats = []
        for ln in (text or "").splitlines():
            ln = ln.strip().lower()
            if not ln:
                continue
            neg = ln.startswith("!")
            body = ln[1:].strip() if neg else ln
            body = re.sub(r'^https?://', '', body)
            if body:
                pats.append("!" + body if neg else body)
        return pats

    def _matches_persist(self, url):
        """True if `url` matches one of the user's "track forever" patterns
        (plain case-insensitive substring match - see settings). A lone "*"
        pattern means "track everything". "!pattern" lines exclude matching
        URLs and win over any include, so "*" + "!youtube.com" tracks
        everything except YouTube."""
        pats = self._cfg_persist_patterns
        if not pats:
            return False
        ul = url.lower()
        if any(p[1:] in ul for p in pats if p.startswith("!")):
            return False
        includes = [p for p in pats if not p.startswith("!")]
        if "*" in includes:
            return True
        return any(p in ul for p in includes)

    @staticmethod
    def _parse_site_folders(text):
        """Each line is "pattern => subfolder" - same one-per-line style as
        the persist patterns above, just with a destination attached."""
        pairs = []
        for ln in (text or "").splitlines():
            ln = ln.strip()
            if not ln or "=>" not in ln:
                continue
            pattern, folder = ln.split("=>", 1)
            pattern = re.sub(r'^https?://', '', pattern.strip().lower())
            folder  = folder.strip().strip("/\\")
            if pattern and folder:
                pairs.append((pattern, folder))
        return pairs

    def _resolve_output_base(self, url):
        """Base output directory for `url`: the configured default, unless a
        site folder pattern matches, in which case that subfolder is used
        instead (e.g. one site's videos going to a differently-named folder
        than the default)."""
        base = self._cfg_output_dir or DEFAULT_OUTPUT_DIR
        if url and self._cfg_site_output_folders:
            ul = url.lower()
            for pattern, folder in self._cfg_site_output_folders:
                if pattern in ul:
                    return os.path.join(base, folder)
        return base

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
        if group.media == "gallery":
            # Known gallery group (from a previous resolve): skip the yt-dlp
            # probe entirely and go straight to the gallery-dl path.
            self._setup_gallery_group(group)
            return
        cookies_tmp = self._cookies_tempcopy()
        cmd = [YTDLP_BIN, "--flat-playlist", "-J", "--no-warnings"]
        if cookies_tmp:
            cmd += ["--cookies", cookies_tmp]
        cmd.append(group.url)
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=600,
                                  creationflags=SUBPROC_FLAGS)
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
                # yt-dlp can't list it, but an image site (artist page,
                # gallery) may still be fully supported by gallery-dl —
                # same split-coverage situation the ephemeral path handles.
                g_cookies = self._cookies_tempcopy()
                try:
                    meta = self._gallerydl_probe(group.url, g_cookies)
                finally:
                    self._cleanup_cookies_tmp(g_cookies)
                if meta is not None:
                    self._setup_gallery_group(group, meta)
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

    def _setup_gallery_group(self, group, meta=None):
        """Persistent tracking for URLs only gallery-dl understands (artist
        pages, galleries). One child task stands for the whole URL and is
        downloaded by gallery-dl with a shared --download-archive, so a
        re-check simply runs the same command again and only newly added
        files actually transfer."""
        title = ""
        if meta:
            artist, gtitle, gid = _gallery_meta_fields(meta)
            title = self._format_gallery_name(artist, gtitle, gid)
        with self.lock:
            group.media = "gallery"
            child = next((t for t in self.tasks
                          if t.parent_group_id == group.id and t.kind == "video"), None)
            created = child is None
            if created:
                child = Task(group.url, kind="video", parent_group_id=group.id)
            child.media = "gallery"
            if title and not child.title:
                child.title = title
            # (Re-)check: run gallery-dl again unless it's already running.
            if child.state != "downloading":
                child._paused = child._cancelled = False
                child.state, child.last_message = "queued", ""
            if created:
                idx = next((i for i, t in enumerate(self.tasks) if t.id == group.id),
                           len(self.tasks) - 1)
                self.tasks.insert(idx + 1, child)
            group.expected_count  = 1
            group.completed_count = 1 if child.state in ("completed", "skipped") else 0
            group.state        = _derive_group_state([child])
            group.last_message = self._group_progress_message([child])
            group.modified_at  = time.time()
        try:
            self.db.save_snapshot([group.to_group_dict()], [child.to_video_dict()])
        except Exception as e:
            self._show_toast(M("db_save_failed", error=str(e)))
        self._request_refresh()
        if not self.global_stop.is_set() and child.state == "queued":
            self._requeue_by_tasks_order([child])

    def _recheck_group(self, group):
        with self.lock:
            if group.state == "resolving":
                return  # already re-checking - queuing again could duplicate videos
            group.state = "resolving"
            group.last_message = M("rechecking")
            group.modified_at = time.time()
        self._request_refresh()
        self._resolve_queue.put(group)
