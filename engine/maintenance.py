"""
Done-tab maintenance tools: resolution/size re-check, missing-file scan,
delete, priority/reorder, and the apply_action() dispatch table.
"""

import subprocess
import threading
import concurrent.futures
import queue
import os
import time
import re
import shutil
import secrets
from collections import deque

from .models import *  # noqa: F401,F403 - internal package, see models.py __all__


class _MaintenanceMixin:
    def _probe_min_side(self, filepath):
        """Return the shorter of (width, height) via ffprobe, or None on failure."""
        try:
            r = subprocess.run(
                [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", filepath],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, creationflags=SUBPROC_FLAGS)
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
        self._set_done_status(M("res_check_collecting", dirs=0))
        threading.Thread(target=self._res_check_worker,
                         args=(targets, base, threshold), daemon=True).start()

    def _set_done_status(self, msg):
        self.done_status = msg
        self._request_refresh()

    def _scan_id_files(self, base, progress_key=None, video_only=True):
        """One os.walk over the output folder -> {video id: file path}.

        Every maintenance tool needs the same map and the walk is the expensive
        part on a networked library, so the shape lives here and callers only
        vary the progress label. Synology metadata/recycle folders are pruned
        rather than descended into: an @eaDir is one empty directory per media
        file, so on a large NAS they dominate the walk while yielding nothing,
        and #recycle would otherwise report deleted files as still present."""
        id_file, scanned = {}, 0
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SCAN_SKIP_DIRS]
            scanned += 1
            if progress_key and scanned % 200 == 0:
                self._set_done_status(M(progress_key, dirs=scanned))
            for fn in filenames:
                if video_only and os.path.splitext(fn)[1].lower() not in VIDEO_FILE_EXTS:
                    continue
                m = RES_FILE_ID_RE.search(fn)
                if m:
                    id_file[m.group(1)] = os.path.join(dirpath, fn)
        return id_file

    def _collect_below_threshold(self, targets, base, collecting_key, progress_key,
                                 log_tag, measure, is_low, report_every):
        """Shared body of the resolution and size checks.

        One folder scan, match each target to its file, then measure every match
        in parallel and keep the ones under the threshold. A file that can't be
        read counts as under it — either way the video has to be fetched again.
        Only the measurement and the labels differ between the two checks.

        Returns (files checked, [(task, path)] to redownload, targets with no
        file on disk)."""
        id_file = {}
        try:
            id_file = self._scan_id_files(base, collecting_key)
        except Exception as e:
            print(f"[{log_tag}] walk: {e}")

        pairs, missing = [], 0
        for t in targets:
            vid = self._extract_vid_id(t)
            fp = id_file.get(vid) if vid else None
            if fp:
                pairs.append((t, fp))
            else:
                missing += 1

        total = len(pairs)
        done_box, plock = [0], threading.Lock()

        def measure_one(pair):
            t, fp = pair
            value = measure(fp)
            with plock:
                done_box[0] += 1
                d = done_box[0]
            if d % report_every == 0 or d == total:
                self._set_done_status(M(progress_key, done=d, total=total))
            return t, fp, value

        low = []   # [(task, filepath)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for t, fp, value in ex.map(measure_one, pairs):
                if is_low(value):
                    low.append((t, fp))
        return total, low, missing

    def _res_check_worker(self, targets, base, threshold):
        total, low, missing = self._collect_below_threshold(
            targets, base,
            collecting_key="res_check_collecting", progress_key="res_check_progress",
            log_tag="res_filter", measure=self._probe_min_side,
            is_low=lambda side: side is None or side < threshold,
            report_every=25)
        if low:
            self._apply_res_redownload(low)
        summary = (M("res_check_summary_missing", total=total, redownload=len(low), missing=missing)
                   if missing else M("res_check_summary", total=total, redownload=len(low)))
        self._set_done_status(summary)
        self._show_toast(summary)

    def _apply_res_redownload(self, low, msg=None):
        """Reset below-threshold videos for re-download (clears history, marks
        them to bypass the download archive, re-queues). Files aren't deleted —
        yt-dlp --force-overwrites replaces them."""
        if msg is None:
            msg = M("resolution_low_requeue")
        low = [t for t, _fp in low]
        gids = set()
        with self.lock:
            self._mark_for_redownload(low, msg)
            for t in low:
                if t.parent_group_id:
                    gids.add(t.parent_group_id)
            # Move completed groups back to active, bypassing the completed-state guard.
            for g in self.tasks:
                if g.kind == "group" and g.id in gids and g.state == "completed":
                    g.state = "queued"
        vids = [v for v in (self._extract_vid_id(t) for t in low) if v]
        self.db.delete_history_many(vids)
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
        self._set_done_status(M("size_check_collecting", dirs=0))
        threading.Thread(target=self._size_check_worker,
                         args=(targets, base, threshold), daemon=True).start()

    def _size_check_worker(self, targets, base, threshold):
        def size_of(fp):
            try:
                return os.path.getsize(fp)
            except Exception:
                return None

        total, low, missing = self._collect_below_threshold(
            targets, base,
            collecting_key="size_check_collecting", progress_key="size_check_progress",
            log_tag="size_filter", measure=size_of,
            is_low=lambda size: size is None or size <= threshold,
            report_every=200)
        if low:
            self._apply_res_redownload(low, msg=M("size_low_requeue"))
        threshold_mb = threshold // (1024 * 1024)
        summary = (M("size_check_summary_missing", total=total, redownload=len(low),
                     threshold_mb=threshold_mb, missing=missing)
                   if missing else
                   M("size_check_summary", total=total, redownload=len(low), threshold_mb=threshold_mb))
        self._set_done_status(summary)
        self._show_toast(summary)

    def missing_check(self, ids=None):
        """Find completed/skipped videos whose file is missing from the output
        folder. The scan is a full os.walk that can take minutes on a large/
        networked library, so it runs on a background thread (like the
        resolution/size checks) instead of blocking the HTTP request. The result
        is published to snapshot()'s `missing_prompt` field, and the front end
        shows the redownload confirm dialog from there; confirm_missing_redownload
        (token) does the actual re-download. ids=None checks every completed group."""
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
            self._show_toast(M("no_completed_to_check"))
            return {"started": False}
        base = os.path.abspath(self._cfg_output_dir or DEFAULT_OUTPUT_DIR)
        if not os.path.isdir(base):
            self._show_toast(M("output_dir_missing", path=base))
            return {"started": False}
        self._missing_prompt = None
        self._set_done_status(M("missing_scanning", dirs=0))
        threading.Thread(target=self._missing_check_worker,
                         args=(targets, base), daemon=True).start()
        return {"started": True}

    def _missing_check_worker(self, targets, base):
        # Always a fresh full scan (no cache) — a stale cache could report an
        # existing file as missing.
        present = set()
        try:
            present = set(self._scan_id_files(base, "missing_scanning"))
        except Exception as e:
            self._set_done_status("")
            self._show_toast(M("folder_scan_failed", error=str(e)))
            return
        if not present:
            # An unmounted output folder would otherwise look "100% missing" — abort instead.
            self._set_done_status("")
            self._show_toast(M("output_dir_empty_abort"))
            return
        missing, noid = [], 0
        for t in targets:
            vid = self._extract_vid_id(t)
            if not vid:
                noid += 1   # can't verify without an id — excluded
                continue
            if vid not in present:
                missing.append(t)
        self._set_done_status("")
        if not missing:
            self._show_toast(M("missing_none_found", n=len(targets), extra=""))
            self._request_refresh()
            return
        token = secrets.token_hex(8)
        self._pending_missing[token] = missing
        while len(self._pending_missing) > 8:
            self._pending_missing.pop(next(iter(self._pending_missing)))
        # Published to snapshot; the front end opens the redownload dialog and
        # clears it via confirm / dismiss.
        self._missing_prompt = {"token": token, "checked": len(targets), "noid": noid,
                                "missing": [{"title": t.title or t.url} for t in missing]}
        self._request_refresh()

    def dismiss_missing_prompt(self):
        self._missing_prompt = None
        self._request_refresh()

    def confirm_missing_redownload(self, token):
        self._missing_prompt = None
        tasks = self._pending_missing.pop(token, None)
        if not tasks:
            return 0
        # Skip anything whose state changed since the check (e.g. already re-queued).
        low = [(t, None) for t in tasks if t.state in ("completed", "skipped")]
        if low:
            self._apply_res_redownload(low, msg=M("missing_requeue_msg"))
            self._show_toast(M("missing_requeue", count=len(low)))
        return len(low)

    def _extract_vid_id(self, task):
        """The download-archive key for a video ("<extractor> <id>" -> the id).

        extractor_id is only populated for videos resolved since that column was
        added; older rows (the bulk of an existing library) have it NULL. Without
        a valid id here, archive removal is a no-op, so a fresh redownload /
        resolution / size re-download would find the id still in the archive and
        get skipped ("has already been recorded"). We recover it from the output
        filename's "[ID]" bracket (exactly the %(id)s yt-dlp wrote to the archive),
        then fall back to the URL."""
        if getattr(task, "extractor_id", None):
            return task.extractor_id
        fp = getattr(task, "filepath", None)
        if fp:
            m = FILE_ID_RE.search(os.path.basename(fp))
            if m:
                return m.group(1)
        url = task.url or ""
        m = re.search(r'(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})', url)
        if m:
            return m.group(1)
        # Last resort: the id is usually the final path segment of the video URL.
        seg = url.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]
        return seg or None

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
            # No extension filter here: this map also answers "open folder" for
            # non-video downloads.
            id_file = self._scan_id_files(base, video_only=False)
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
        base  = os.path.abspath(self._cfg_output_dir or DEFAULT_OUTPUT_DIR)
        # Resolve symlinks for the containment check: a filepath recorded under
        # one alias (e.g. /media/…) must still match an output_dir given under
        # another that points into the same share (e.g. /library). Without this
        # the fast path is skipped and locate falls back to a full, very slow
        # tree walk that can time the request out.
        rbase = os.path.realpath(base)
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
            rfp = os.path.realpath(fp)
            target = rfp if task.kind == "video" else os.path.dirname(rfp)
            return {"base": rbase, "rel": os.path.relpath(target, rbase),
                    "kind": "file" if task.kind == "video" else "dir"}

        for fp in paths:
            # Skip stale paths (output folder changed, or file was deleted).
            rfp = os.path.realpath(fp)
            if rfp.startswith(rbase + os.sep) and os.path.isfile(rfp):
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

    def _delete_scan_worker(self, vid_ids):
        """Background os.walk() pass to find the files behind the selected ids
        (a full scan can take minutes on a large/networked library, so it must
        not block the request). Publishes the file list to snapshot's
        `delete_prompt`; the front end then shows the delete-confirm dialog and
        confirm_delete_files(token) does the actual delete."""
        base = os.path.abspath(self._cfg_output_dir or DEFAULT_OUTPUT_DIR)
        # Match the "[ID]" form from the output template exactly - a plain
        # substring match risks matching an unrelated file whose name
        # happens to contain a short id.
        vid_markers = {f"[{v}]" for v in vid_ids}
        files, scanned = [], 0
        try:
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in SCAN_SKIP_DIRS]
                scanned += 1
                if scanned % 200 == 0:
                    self._set_done_status(M("delete_scanning", dirs=scanned))
                for fn in filenames:
                    for marker in vid_markers:
                        if marker in fn:
                            files.append(os.path.join(dirpath, fn))
                            break
        except Exception as e:
            print(f"[walk] {e}")
        files = list(dict.fromkeys(files))
        self._set_done_status("")
        if not files:
            self._request_refresh()
            return
        token = secrets.token_hex(8)
        self._pending_deletes[token] = files
        # Keep only the most recent 8 pending confirmations.
        while len(self._pending_deletes) > 8:
            self._pending_deletes.pop(next(iter(self._pending_deletes)))
        self._delete_prompt = {"token": token, "files": files}
        self._request_refresh()

    def dismiss_delete_prompt(self):
        self._delete_prompt = None
        self._request_refresh()

    def confirm_delete_files(self, token):
        self._delete_prompt = None
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
        (token, files) for a 2-step confirmation. Ids that don't match a
        tracked (DB) task are treated as session-only entries instead - see
        _remove_ephemeral()."""
        ids = set(ids)
        with self.lock:
            tasks = [t for t in self.tasks if t.id in ids]
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

        remaining = ids - {t.id for t in tasks}
        if remaining:
            self._remove_ephemeral(remaining)

        if with_files and vid_ids:
            # The file scan is a full os.walk — run it async and deliver the
            # result via snapshot's delete_prompt (see _delete_scan_worker).
            self._delete_prompt = None
            self._set_done_status(M("delete_scanning", dirs=0))
            threading.Thread(target=self._delete_scan_worker,
                             args=(vid_ids,), daemon=True).start()
            return {"scanning": True}
        return {"scanning": False}

    def _remove_ephemeral(self, ids):
        """Remove session-only entries (This session tab) by id - these
        never lived in self.tasks/the database, so delete_tasks() couldn't
        reach them without this."""
        with self._ephemeral_lock:
            before = len(self._ephemeral)
            kept = deque((e for e in self._ephemeral if e["id"] not in ids), maxlen=200)
            self._ephemeral = kept
            removed = before - len(self._ephemeral)
        if removed:
            self._request_refresh()
        return removed

    def _recheck_all_done(self):
        with self.lock:
            done_groups = [t for t in self.tasks
                           if t.kind == "group" and t.state == "completed"]
        targets  = [g for g in done_groups if not g.no_recheck]
        excluded = len(done_groups) - len(targets)
        if not targets:
            self._set_done_status(M("no_done_groups"))
            return
        # Each group is re-resolved on the shared resolve queue; the workers
        # report back via _recheck_batch_tick, which advances this live counter
        # and swaps in a summary once every group is done.
        with self._recheck_batch_lock:
            self._recheck_batch = {"ids": {g.id for g in targets},
                                   "total": len(targets), "done": 0,
                                   "new": 0, "excluded": excluded}
        self._set_done_status(M("bulk_recheck_progress", done=0, total=len(targets)))
        for g in targets:
            if not self._recheck_group(g):
                # Already resolving (rare): it won't be re-queued, so account
                # for it now or the batch would never reach 100%.
                self._recheck_batch_tick(g)

    def _recheck_batch_tick(self, group):
        """A group finished (re-)resolving. If it belongs to the running
        "re-check all" batch, advance the done-tab progress line; when the last
        group lands, show a summary that auto-clears."""
        with self._recheck_batch_lock:
            b = self._recheck_batch
            if not b or group.id not in b["ids"]:
                return
            b["ids"].discard(group.id)
            b["done"] += 1
            b["new"]  += max(0, getattr(group, "new_count", 0) or 0)
            done, total, new, excluded = b["done"], b["total"], b["new"], b["excluded"]
            finished = not b["ids"]
            if finished:
                self._recheck_batch = None
        if finished:
            summary = (M("bulk_recheck_summary_excluded", total=total, new=new, excluded=excluded)
                       if excluded else M("bulk_recheck_summary", total=total, new=new))
            self._set_done_status(summary)
            self._show_toast(summary)
        else:
            self._set_done_status(M("bulk_recheck_progress", done=done, total=total))

    def _auto_recheck_loop(self):
        """Scheduled bulk re-check: every N days (Settings, 0 = off) run the
        same "re-check all completed groups" the Done tab button offers."""
        while not self._closing.wait(600):
            self._auto_recheck_tick()

    def _auto_recheck_tick(self):
        days = self._cfg_recheck_days
        if days <= 0:
            return
        try:
            last = float(self.db.get_meta("last_auto_recheck", "") or 0)
        except ValueError:
            last = 0
        now = time.time()
        if not last:
            # Just enabled: schedule N days from now instead of kicking
            # off a full re-check the moment the setting is saved.
            self.db.set_meta("last_auto_recheck", str(now))
            return
        if now - last < days * 86400:
            return
        self.db.set_meta("last_auto_recheck", str(now))
        self._show_toast(M("auto_recheck_run"))
        self._recheck_all_done()

    def _retry_all_errors_skipped(self, categories=None):
        with self.lock:
            done_groups = [t for t in self.tasks
                           if t.kind == "group" and t.state == "completed"]
        if not done_groups:
            self._set_done_status(M("no_done_groups"))
            return 0
        retried = excluded = 0
        for g in done_groups:
            r, x = self._retry_errors_skipped(g, categories)
            retried += r; excluded += x
        summary = M("bulk_retry_summary_excluded", retried=retried, excluded=excluded) if excluded \
                  else M("bulk_retry_summary", retried=retried)
        self._set_done_status(summary)
        # The status line alone is easy to miss — say so out loud as the other
        # maintenance tools do.
        self._show_toast(summary)
        return retried

    def retry_scan(self, ids=None):
        """Count the failures behind the chosen groups per category and offer
        them for selection. The actual retry happens in confirm_retry, so the
        kinds that always fail the same way (private, removed, ...) are opt-in
        rather than retried on every pass."""
        with self.lock:
            if ids:
                groups = {t.id for t in self.tasks
                          if t.kind == "group" and t.id in set(ids)}
            else:
                groups = {t.id for t in self.tasks
                          if t.kind == "group" and t.state == "completed"}
            errors = [t for t in self.tasks
                      if t.kind == "video" and t.state == "error"
                      and t.parent_group_id in groups]
        if not errors:
            self._retry_prompt = None
            self._show_toast(M("retry_none_found"))
            self._request_refresh()
            return {"started": False}
        counts = {}
        for t in errors:
            key = classify_error(t)
            counts[key] = counts.get(key, 0) + 1
        token = secrets.token_hex(8)
        self._pending_retry[token] = list(groups)
        while len(self._pending_retry) > 8:
            self._pending_retry.pop(next(iter(self._pending_retry)))
        self._retry_prompt = {
            "token": token,
            "total": len(errors),
            "categories": [{"key": k, "count": counts[k]}
                           for k in ERROR_CATEGORIES if counts.get(k)],
        }
        self._request_refresh()
        return {"started": True}

    def dismiss_retry_prompt(self):
        self._retry_prompt = None
        self._request_refresh()

    def confirm_retry(self, token, categories):
        self._retry_prompt = None
        gids = self._pending_retry.pop(token, None)
        if gids is None or not categories:
            self._request_refresh()
            return 0
        with self.lock:
            groups = [t for t in self.tasks
                      if t.kind == "group" and t.id in set(gids)]
        retried = excluded = 0
        for g in groups:
            r, x = self._retry_errors_skipped(g, categories)
            retried += r; excluded += x
        summary = M("bulk_retry_summary_excluded", retried=retried, excluded=excluded) if excluded \
                  else M("bulk_retry_summary", retried=retried)
        self._set_done_status(summary)
        self._show_toast(summary)
        return retried

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
        elif action == "toggle_recheck_exclude":
            changed = []
            for g in self._groups_for_ids(ids):
                with self.lock:
                    g.no_recheck = 0 if g.no_recheck else 1
                changed.append(g)
            for g in changed:
                try:
                    self.db.upsert_group(g.to_group_dict())
                except Exception as e:
                    self._show_toast(M("db_save_failed", error=str(e)))
            if changed:
                self._show_toast(M("recheck_excluded" if changed[0].no_recheck
                                   else "recheck_included"))
            self._request_refresh()
        elif action == "move_active":
            for t in sel:
                if t.kind == "group": self._move_to_active(t)
        elif action == "mark_done":
            for t in sel:
                if t.kind == "group": self._mark_group_done(t)
        elif action == "res_check":
            groups = self._groups_for_ids(ids)
            if groups:
                self._res_check_groups(groups)
        else:
            raise ValueError(f"unknown action: {action}")
