"""
Download queue, worker pool, and per-task/per-group start-stop-reorder actions.
"""

import subprocess
import queue
import os
import time
import re

from .models import *  # noqa: F401,F403 - internal package, see models.py __all__


class _QueueMixin:
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
               "-o", self._output_template(task.url),
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

    def _set_video_state(self, task, state, msg=""):
        with self.lock:
            task.state, task.last_message = state, msg
        self._request_refresh()

    def _set_group_state(self, group, state, msg=""):
        with self.lock:
            group.state, group.last_message = state, msg
            group.modified_at = time.time()
        self._request_refresh()

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
