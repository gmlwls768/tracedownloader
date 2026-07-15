"""
Settings get/set, cookies, and the state snapshot sent to the UI.
"""

import threading
import os
from datetime import datetime

from .models import *  # noqa: F401,F403 - internal package, see models.py __all__


class _SettingsMixin:
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
            "auto_update_tools":    self._cfg_auto_update_tools,
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
        self._cfg_auto_update_tools   = bool(s.get("auto_update_tools", self._cfg_auto_update_tools))
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
        self.db.set_meta("auto_update_tools",   "1" if self._cfg_auto_update_tools else "0")
        self._save_cookies(s)
        self._request_refresh()

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
