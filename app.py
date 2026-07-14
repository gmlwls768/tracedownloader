"""
Desktop app for the packaged Windows build (see deploy/build_windows.py).

A native tkinter GUI, not a browser window - it imports engine.py directly
and calls it in-process. No HTTP server, no port, nothing to open in a
browser. The Linux deployment is unrelated to this file; it runs
server.py/static/index.html as a small web server instead (see
deploy/install.sh) - both front ends share the exact same engine.py.

Handles what a double-clicked .exe needs on top of that:
- A data folder that lives next to the .exe, not PyInstaller's throwaway
  temp extraction folder, so the database survives between runs.
- Auto-downloading yt-dlp/gallery-dl/ffmpeg on first run.
"""

import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from tkinter import filedialog, messagebox, ttk

from i18n import Translator


def app_dir():
    """Folder the .exe (or this script) lives in."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE = app_dir()


def _redirect_output_for_windowed_build():
    """A --windowed build has no console, so sys.stdout/stderr are None and
    any print() call would crash the app outright. Send them to a log file
    next to the exe instead."""
    if sys.stdout is None:
        log = open(os.path.join(BASE, "app.log"), "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log


_redirect_output_for_windowed_build()

DATA_DIR = os.path.join(BASE, "data")
OUTPUT_DIR = os.path.join(BASE, "download")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.environ.setdefault("APP_HOME", DATA_DIR)
os.environ.setdefault("APP_BIN_DIR", BASE)
os.environ.setdefault("APP_DEFAULT_OUTPUT", OUTPUT_DIR)

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

# Small local file for GUI-only preferences that have no place in engine.py's
# shared, cross-platform settings (the web UI keeps language in the
# browser's localStorage for the same reason - it's a per-client choice).
LOCAL_CONFIG_PATH = os.path.join(BASE, "gui_config.json")


def load_local_config():
    try:
        with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_local_config(cfg):
    try:
        with open(LOCAL_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def _download(url, dest):
    print(f"Downloading {os.path.basename(dest)} ...")
    urllib.request.urlretrieve(url, dest)


def ensure_dependencies(on_progress=None):
    os.makedirs(BIN_DIR, exist_ok=True)
    for filename, url in EXE_DOWNLOADS.items():
        dest = os.path.join(BIN_DIR, filename)
        if os.path.isfile(dest):
            continue
        if on_progress:
            on_progress(filename)
        try:
            _download(url, dest)
        except Exception as e:
            print(f"Couldn't download {filename}: {e}\nPlace it in {BIN_DIR} manually.")

    ffmpeg_dest  = os.path.join(BIN_DIR, "ffmpeg.exe")
    ffprobe_dest = os.path.join(BIN_DIR, "ffprobe.exe")
    if os.path.isfile(ffmpeg_dest) and os.path.isfile(ffprobe_dest):
        return
    if on_progress:
        on_progress("ffmpeg.exe")
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


def fmt_speed(bps):
    if not bps or bps <= 0:
        return "0 B/s"
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    v = float(bps)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}"
        v /= 1024


def explorer_select(path):
    """Open Explorer with `path` pre-selected (Shell API), same technique as
    client/clipboard_watcher.py's remote-open helper - this is the local,
    in-process equivalent for the desktop app."""
    try:
        shell32 = ctypes.windll.shell32
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.ole32.CoInitialize(None)
        pidl = shell32.ILCreateFromPathW(path)
        if not pidl:
            return False
        try:
            return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
        finally:
            shell32.ILFree(pidl)
    except Exception:
        return False


STATE_ICON = {"completed": "✓", "error": "✗", "downloading": "⬇",
              "paused": "⏸", "skipped": "⤤", "queued": "·",
              "cancelled": "✕", "resolving": "…"}
STATE_COLOR = {"completed": "#3fae76", "error": "#e05555", "downloading": "#4a9eff",
               "paused": "#d8a637", "skipped": "#8b8d98", "queued": "#8b8d98",
               "resolving": "#a58cd6", "cancelled": "#8b8d98"}
MIN_REFRESH_INTERVAL = 0.5   # matches the web UI's own SSE-driven refresh throttle
DUMMY_CHILD_TAG = "__lazy__"


class App:
    def __init__(self, root, engine):
        self.root = root
        self.engine = engine
        cfg = load_local_config()
        self.t = Translator(cfg.get("language", "en"))
        self.search_text = ""
        self.tab = "active"
        self.expanded = set(cfg.get("expanded", []))
        self.done_sort = "modified"
        self.done_asc = False
        self.snapshot = {"active": [], "done": [], "orphans": [], "ephemeral": [],
                          "stats": {}, "done_status": "", "output_dir": ""}
        self.row_meta = {}       # tree item id -> {"id":, "kind":, "url":}
        self.last_toast_id = 0
        self.toast_job = None
        self.drag_item = None

        root.title("yt-dlp & gallery-dl GUI")
        root.geometry(cfg.get("geometry", "1180x760"))
        root.protocol("WM_DELETE_WINDOW", self.on_quit)

        self._build_style()
        self._build_topbar()
        self._build_bar2()
        self._build_tree()
        self._build_statusbar()
        self._build_ctx_menu()
        self.apply_static_text()

        self.refresh_queue = queue.Queue()
        self.root.after(100, self._drain_refresh_queue)
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        self.refresh()

    # ══════════════════════════════════════════
    #  BUILD UI
    # ══════════════════════════════════════════
    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24)

    def _build_topbar(self):
        top = ttk.Frame(self.root, padding=6)
        top.pack(side="top", fill="x")
        self.url_box = tk.Text(top, height=2, wrap="word")
        self.url_box.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.url_box.bind("<Return>", self._url_box_enter)

        self.btn_add = ttk.Button(top, command=self.add_urls)
        self.btn_add.pack(side="left", padx=2)
        self.btn_start_all = ttk.Button(top, command=lambda: self.engine._start_all())
        self.btn_start_all.pack(side="left", padx=2)
        self.btn_stop_all = ttk.Button(top, command=lambda: self.engine._stop_all())
        self.btn_stop_all.pack(side="left", padx=2)
        self.btn_save = ttk.Button(top, command=lambda: self.engine._save_all(silent=False))
        self.btn_save.pack(side="left", padx=2)
        self.btn_settings = ttk.Button(top, command=self.open_settings)
        self.btn_settings.pack(side="left", padx=2)
        self.btn_quit = ttk.Button(top, command=self.on_quit)
        self.btn_quit.pack(side="left", padx=2)

    def _url_box_enter(self, event):
        if event.state & 0x0001:  # Shift held - allow a literal newline
            return
        self.add_urls()
        return "break"

    def _build_bar2(self):
        bar = ttk.Frame(self.root, padding=(6, 0, 6, 6))
        bar.pack(side="top", fill="x")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(bar, textvariable=self.search_var, width=28)
        self.search_entry.pack(side="left", padx=(0, 8))
        self.search_var.trace_add("write", self._on_search_changed)
        self._search_job = None

        self.tab_buttons = {}
        for key in ("active", "done", "ephemeral"):
            b = ttk.Button(bar, command=lambda k=key: self.set_tab(k))
            b.pack(side="left", padx=2)
            self.tab_buttons[key] = b

        self.done_controls = ttk.Frame(bar)
        self.done_controls.pack(side="left", padx=(12, 0))
        self.done_sort_var = tk.StringVar(value="modified")
        self.done_sort_combo = ttk.Combobox(self.done_controls, textvariable=self.done_sort_var,
                                            state="readonly", width=12)
        self.done_sort_combo.pack(side="left", padx=2)
        self.done_sort_combo.bind("<<ComboboxSelected>>", lambda e: self._on_done_sort_changed())
        self.btn_asc = ttk.Button(self.done_controls, command=self._toggle_asc)
        self.btn_asc.pack(side="left", padx=2)
        self.btn_recheck_all = ttk.Button(self.done_controls, command=self.confirm_recheck_all)
        self.btn_recheck_all.pack(side="left", padx=2)
        self.btn_retry_all = ttk.Button(self.done_controls, command=self.confirm_retry_all)
        self.btn_retry_all.pack(side="left", padx=2)
        self.btn_redownload_all = ttk.Button(self.done_controls, command=self.confirm_redownload_all)
        self.btn_redownload_all.pack(side="left", padx=2)
        self.btn_res_check_all = ttk.Button(self.done_controls, command=self.confirm_res_check_all)
        self.btn_res_check_all.pack(side="left", padx=2)
        self.btn_size_check_all = ttk.Button(self.done_controls, command=self.confirm_size_check_all)
        self.btn_size_check_all.pack(side="left", padx=2)
        self.btn_missing_check_all = ttk.Button(self.done_controls, command=self.confirm_missing_check_all)
        self.btn_missing_check_all.pack(side="left", padx=2)
        self.done_controls.pack_forget()   # only shown while on the Done tab

    def _build_tree(self):
        wrap = ttk.Frame(self.root)
        wrap.pack(side="top", fill="both", expand=True)
        cols = ("state", "progress", "speed", "message")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings", selectmode="extended")
        self.tree.column("#0", width=420, stretch=True)
        self.tree.column("state", width=100, anchor="w")
        self.tree.column("progress", width=130, anchor="w")
        self.tree.column("speed", width=110, anchor="w")
        self.tree.column("message", width=380, anchor="w")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        for s, c in STATE_COLOR.items():
            self.tree.tag_configure(f"st_{s}", foreground=c)

        self.tree.bind("<<TreeviewSelect>>", self._on_select_change)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.tree.bind("<<TreeviewClose>>", self._on_tree_close)
        self.tree.bind("<Button-3>", self._show_ctx_menu)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<ButtonPress-1>", self._drag_start)
        self.tree.bind("<B1-Motion>", self._drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._drag_release)

    def _build_statusbar(self):
        bar = ttk.Frame(self.root, padding=4)
        bar.pack(side="bottom", fill="x")
        self.stats_label = ttk.Label(bar)
        self.stats_label.pack(side="left", padx=6)
        self.speed_label = ttk.Label(bar)
        self.speed_label.pack(side="left", padx=6)
        self.outdir_label = ttk.Label(bar, cursor="hand2")
        self.outdir_label.pack(side="left", padx=6)
        self.outdir_label.bind("<Button-1>", lambda e: self.copy_output_dir())
        self.toast_label = ttk.Label(bar, foreground="#d8a637")
        self.toast_label.pack(side="right", padx=6)

    # ══════════════════════════════════════════
    #  STATIC TEXT / LANGUAGE
    # ══════════════════════════════════════════
    def apply_static_text(self):
        t = self.t
        self.btn_add.config(text=t("btn_add"))
        self.btn_start_all.config(text=t("btn_start_all"))
        self.btn_stop_all.config(text=t("btn_stop_all"))
        self.btn_save.config(text=t("btn_save"))
        self.btn_settings.config(text=t("btn_settings"))
        self.btn_quit.config(text=t("btn_shutdown"))
        self.tab_buttons["active"].config(text=t("tab_active"))
        self.tab_buttons["done"].config(text=t("tab_done"))
        self.tab_buttons["ephemeral"].config(text=t("tab_ephemeral"))
        self.done_sort_combo.config(values=[t("sort_modified"), t("sort_created"), t("sort_name")])
        self.done_sort_combo.set([t("sort_modified"), t("sort_created"), t("sort_name")][
            ["modified", "created", "name"].index(self.done_sort)])
        self.btn_asc.config(text=t("asc_asc") if self.done_asc else t("asc_desc"))
        self.btn_recheck_all.config(text=t("done_recheck_all"))
        self.btn_retry_all.config(text=t("done_retry_all"))
        self.btn_redownload_all.config(text=t("done_redownload_all"))
        self.btn_res_check_all.config(text=t("done_res_check"))
        self.btn_size_check_all.config(text=t("done_size_check"))
        self.btn_missing_check_all.config(text=t("done_missing_check"))
        self.tree.heading("#0", text=t("col_name"))
        self.tree.heading("state", text=t("col_state"))
        self.tree.heading("progress", text=t("col_progress"))
        self.tree.heading("speed", text=t("col_speed"))
        self.tree.heading("message", text=t("col_message"))
        self._update_tab_highlight()
        self._render()

    def set_language(self, lang):
        self.t = Translator(lang)
        cfg = load_local_config()
        cfg["language"] = self.t.lang
        save_local_config(cfg)
        self.apply_static_text()

    def _update_tab_highlight(self):
        for key, btn in self.tab_buttons.items():
            btn.state(["pressed"] if key == self.tab else ["!pressed"])

    # ══════════════════════════════════════════
    #  TABS / SEARCH / SORT
    # ══════════════════════════════════════════
    def set_tab(self, tab):
        self.tab = tab
        self.tree.selection_remove(self.tree.selection())
        if tab == "done":
            self.done_controls.pack(side="left", padx=(12, 0))
        else:
            self.done_controls.pack_forget()
        self._update_tab_highlight()
        self._render()

    def _on_search_changed(self, *_a):
        if self._search_job:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(300, self._commit_search)

    def _commit_search(self):
        self.search_text = self.search_var.get().strip()
        self.refresh()

    def _on_done_sort_changed(self):
        idx = self.done_sort_combo.current()
        self.done_sort = ["modified", "created", "name"][idx]
        self._render()

    def _toggle_asc(self):
        self.done_asc = not self.done_asc
        self.btn_asc.config(text=self.t("asc_asc") if self.done_asc else self.t("asc_desc"))
        self._render()

    # ══════════════════════════════════════════
    #  LIVE UPDATES  (background thread -> queue -> main-thread render)
    #  Same shape as the web UI's SSE: a change signal triggers a throttled
    #  re-fetch, never touching widgets off the main thread.
    # ══════════════════════════════════════════
    def _refresh_loop(self):
        last_ver = -1
        last_toast_id = 0
        while True:
            ver = self.engine.wait_change(last_ver, timeout=25.0)
            last_ver = ver
            toasts = self.engine.toasts_since(last_toast_id)
            if toasts:
                last_toast_id = toasts[-1][0]
            self.refresh_queue.put([m for _, m in toasts])
            time.sleep(MIN_REFRESH_INTERVAL)

    def _drain_refresh_queue(self):
        got_update = False
        toasts = []
        try:
            while True:
                toasts.extend(self.refresh_queue.get_nowait())
                got_update = True
        except queue.Empty:
            pass
        if got_update:
            self.refresh()
        for m in toasts:
            self.show_toast(self.t.decode(m))
        self.root.after(150, self._drain_refresh_queue)

    def refresh(self):
        self.snapshot = self.engine.snapshot(q=self.search_text, expanded=list(self.expanded))
        self._render()

    def show_toast(self, msg):
        if not msg:
            return
        self.toast_label.config(text=msg)
        if self.toast_job:
            try:
                self.root.after_cancel(self.toast_job)
            except (tk.TclError, ValueError):
                pass
        self.toast_job = self.root.after(4000, lambda: self.toast_label.config(text=""))

    # ══════════════════════════════════════════
    #  RENDER
    # ══════════════════════════════════════════
    def _render(self):
        sel_ids = set(self.tree.selection())
        for item in self.tree.get_children(""):
            self.tree.delete(item)
        self.row_meta = {}

        snap = self.snapshot
        if self.tab == "ephemeral":
            for e in snap.get("ephemeral", []):
                self._insert_ephemeral_row(e)
        else:
            groups = snap["active"] if self.tab == "active" else list(snap["done"])
            if self.tab == "done":
                key_fn = {
                    "created": lambda g: g["created_at"] or 0,
                    "modified": lambda g: g["modified_at"] or 0,
                    "name": lambda g: (g["url"] or "").lower(),
                }[self.done_sort]
                groups.sort(key=key_fn, reverse=not self.done_asc)
            for g in groups:
                self._insert_group_row(g)
            if self.tab == "active":
                for v in snap.get("orphans", []):
                    self._insert_video_row("", v)

        still_there = [i for i in sel_ids if self.tree.exists(i)]
        if still_there:
            self.tree.selection_set(still_there)

        self._render_statusbar()

    def _state_label(self, state):
        return self.t(f"state_{state}") or state

    def _insert_group_row(self, g):
        gid = g["id"]
        iid = f"g_{gid}"
        name = ("★ " if g.get("priority") else "") + (g["url"] or "")
        expected = g.get("expected")
        cnt = f"{g['completed']}/{expected if expected is not None else '?'} ({g.get('pct', 0)}%)"
        dl = str(g["dling"]) if g.get("dling") else ""
        msg = self.t.decode(g.get("message", ""))
        is_open = gid in self.expanded
        self.tree.insert("", "end", iid=iid, text=name,
                         values=(self._state_label(g["state"]), cnt, dl, msg),
                         tags=(f"st_{g['state']}",), open=is_open)
        self.row_meta[iid] = {"id": gid, "kind": "group", "url": g["url"]}
        children = g.get("children")
        if is_open and children is not None:
            for v in children:
                self._insert_video_row(iid, v)
        elif expected:
            self.tree.insert(iid, "end", iid=f"dummy_{gid}", text="…", tags=(DUMMY_CHILD_TAG,))

    def _insert_video_row(self, parent, v):
        vid = v["id"]
        iid = f"v_{vid}"
        pct = v.get("pct")
        pct_str = f"{pct}%" if pct is not None else ""
        speed = fmt_speed(v.get("speed_bps") or 0) if v.get("speed_bps") else ""
        msg = self.t.decode(v.get("message", ""))
        name = v.get("title") or v["url"]
        self.tree.insert(parent, "end", iid=iid, text=name,
                         values=(self._state_label(v["state"]), pct_str, speed, msg),
                         tags=(f"st_{v['state']}",))
        self.row_meta[iid] = {"id": vid, "kind": "video", "url": v["url"]}

    def _insert_ephemeral_row(self, e):
        eid = e["id"]
        iid = f"e_{eid}"
        kind_label = self.t("kind_gallery") if e["kind"] == "gallery" else self.t("kind_video")
        name = f"{kind_label} — {e.get('title') or e['url']}"
        msg = self.t.decode(e.get("message", ""))
        self.tree.insert("", "end", iid=iid, text=name,
                         values=(self._state_label(e["state"]), "", "", msg),
                         tags=(f"st_{e['state']}",))
        self.row_meta[iid] = {"id": eid, "kind": "ephemeral", "url": e["url"]}

    def _render_statusbar(self):
        s = self.snapshot.get("stats") or {}
        self.stats_label.config(text=self.t(
            "stats_summary", done=s.get("video_done", 0), total=s.get("video_total", 0),
            skip=s.get("skipped", 0), dling=s.get("downloading", 0), cum=s.get("cum_total", 0)))
        if s.get("downloading"):
            self.speed_label.config(text=self.t(
                "stats_speed", speed=fmt_speed(s.get("avg_speed_bps", 0)), dling=s.get("downloading")))
        else:
            self.speed_label.config(text=self.t("stats_speed_idle"))
        self.outdir_label.config(text="📂 " + (self.snapshot.get("output_dir") or ""))

    # ══════════════════════════════════════════
    #  TREE EVENTS
    # ══════════════════════════════════════════
    def _on_select_change(self, event):
        pass  # selection itself is read directly from the tree when needed

    def _on_tree_open(self, event):
        meta = self.row_meta.get(self.tree.focus())
        if meta and meta["kind"] == "group":
            self.expanded.add(meta["id"])
            self._save_expanded()
            self.refresh()

    def _on_tree_close(self, event):
        meta = self.row_meta.get(self.tree.focus())
        if meta and meta["kind"] == "group":
            self.expanded.discard(meta["id"])
            self._save_expanded()

    def _save_expanded(self):
        cfg = load_local_config()
        cfg["expanded"] = list(self.expanded)
        save_local_config(cfg)

    def _on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and self.row_meta.get(iid, {}).get("kind") == "video":
            self.tree.selection_set(iid)
            self.open_selected(play=True)

    def selected_items(self):
        return [self.row_meta[i] for i in self.tree.selection() if i in self.row_meta]

    def selected_ids(self):
        return [m["id"] for m in self.selected_items()]

    # ══════════════════════════════════════════
    #  DRAG REORDER (Active tab, group rows only)
    # ══════════════════════════════════════════
    def _drag_start(self, event):
        iid = self.tree.identify_row(event.y)
        self.drag_item = iid if (self.tab == "active"
                                 and self.row_meta.get(iid, {}).get("kind") == "group") else None

    def _drag_motion(self, event):
        pass

    def _drag_release(self, event):
        drag, self.drag_item = self.drag_item, None
        if not drag:
            return
        target = self.tree.identify_row(event.y)
        if not target or target == drag or self.row_meta.get(target, {}).get("kind") != "group":
            return
        order = [self.row_meta[i]["id"] for i in self.tree.get_children("")
                if self.row_meta.get(i, {}).get("kind") == "group"]
        drag_id, target_id = self.row_meta[drag]["id"], self.row_meta[target]["id"]
        order = [i for i in order if i != drag_id]
        order.insert(order.index(target_id), drag_id)
        self.engine.reorder_groups(order)

    # ══════════════════════════════════════════
    #  OPEN FILE  (play video / show in Explorer - no remote helper needed,
    #  this app already runs on the machine the files are on)
    # ══════════════════════════════════════════
    def open_selected(self, play):
        sel = self.selected_items()
        if not sel:
            return
        r = self.engine.locate_path(sel[0]["id"])
        if not r:
            self.show_toast(self.t("open_no_file"))
            return
        full = os.path.normpath(os.path.join(os.path.abspath(r["base"]), r["rel"]))
        try:
            if play or r["kind"] == "dir":
                os.startfile(full)
            elif not explorer_select(full):
                os.startfile(os.path.dirname(full))
        except OSError as e:
            self.show_toast(f"⚠ {e}")

    def copy_output_dir(self):
        path = self.snapshot.get("output_dir") or ""
        self.root.clipboard_clear()
        self.root.clipboard_append(path)
        self.show_toast(self.t("copy_ok"))

    def copy_urls(self):
        urls = [m["url"] for m in self.selected_items() if m.get("url")]
        if not urls:
            self.show_toast(self.t("copy_urls_none"))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(urls))
        self.show_toast(self.t("copy_urls_ok", n=len(urls)))

    # ══════════════════════════════════════════
    #  CONFIRM DIALOGS
    # ══════════════════════════════════════════
    def confirm(self, title, body):
        return messagebox.askyesno(title, body, parent=self.root)

    def confirm_with_preview(self, title, body, items, ok_label):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=body, wraplength=460, justify="left").pack(padx=14, pady=(14, 8), anchor="w")
        if items:
            frame = ttk.Frame(win)
            frame.pack(padx=14, pady=4, fill="both", expand=True)
            txt = tk.Text(frame, height=min(14, len(items)), width=64, wrap="none")
            txt.insert("1.0", "\n".join(items))
            txt.config(state="disabled")
            txt.pack(side="left", fill="both", expand=True)
            sb = ttk.Scrollbar(frame, command=txt.yview)
            txt.config(yscrollcommand=sb.set)
            sb.pack(side="left", fill="y")
        result = {"ok": False}
        def on_ok():
            result["ok"] = True
            win.destroy()
        btns = ttk.Frame(win)
        btns.pack(pady=12)
        ttk.Button(btns, text=self.t("modal_cancel"), command=win.destroy).pack(side="left", padx=6)
        ttk.Button(btns, text=ok_label, command=on_ok).pack(side="left", padx=6)
        win.wait_window()
        return result["ok"]

    # ══════════════════════════════════════════
    #  CONTEXT MENU
    # ══════════════════════════════════════════
    def _build_ctx_menu(self):
        self.ctx_menu = tk.Menu(self.root, tearoff=0)

    def _show_ctx_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        if not self.selected_items():
            return
        t = self.t
        m = self.ctx_menu
        m.delete(0, "end")
        m.add_command(label=t("ctx_start"), command=lambda: self.do_action("start"))
        m.add_command(label=t("ctx_pause"), command=lambda: self.do_action("pause"))
        m.add_command(label=t("ctx_stop"), command=lambda: self.do_action("stop"))
        m.add_separator()
        m.add_command(label=t("ctx_retry"), command=lambda: self.do_action("retry"))
        m.add_command(label=t("ctx_recheck"), command=lambda: self.do_action("recheck"))
        m.add_command(label=t("ctx_fresh"), command=self.confirm_fresh)
        m.add_command(label=t("ctx_res_check"), command=self.confirm_res_check_sel)
        m.add_command(label=t("ctx_size_check"), command=self.confirm_size_check_sel)
        m.add_command(label=t("ctx_missing_check"), command=self.confirm_missing_check_sel)
        m.add_separator()
        m.add_command(label=t("ctx_priority"), command=lambda: self.do_action("priority"))
        m.add_command(label=t("ctx_move_active"), command=lambda: self.do_action("move_active"))
        m.add_command(label=t("ctx_copy_url"), command=self.copy_urls)
        m.add_separator()
        m.add_command(label=t("ctx_open_video"), command=lambda: self.open_selected(True))
        m.add_command(label=t("ctx_open_folder"), command=lambda: self.open_selected(False))
        m.add_separator()
        m.add_command(label=t("ctx_delete"), command=lambda: self.confirm_delete(False))
        m.add_command(label=t("ctx_delete_files"), command=lambda: self.confirm_delete(True))
        m.tk_popup(event.x_root, event.y_root)

    def do_action(self, action):
        ids = self.selected_ids()
        if not ids:
            return
        try:
            self.engine.apply_action(ids, action)
        except ValueError as e:
            self.show_toast(f"⚠ {e}")

    # ══════════════════════════════════════════
    #  ADD URLS
    # ══════════════════════════════════════════
    def add_urls(self):
        text = self.url_box.get("1.0", "end").strip()
        if not text:
            return
        self.url_box.delete("1.0", "end")
        self.engine.add_urls(text)

    # ══════════════════════════════════════════
    #  DONE-TAB / CONTEXT BULK ACTIONS  (each confirms first, like the web UI)
    # ══════════════════════════════════════════
    def confirm_fresh(self):
        ids = self.selected_ids()
        if ids and self.confirm(self.t("fresh_confirm_title"), self.t("fresh_confirm_body", n=len(ids))):
            self.engine.apply_action(ids, "fresh")

    def confirm_recheck_all(self):
        n = len(self.snapshot.get("done", []))
        if self.confirm(self.t("recheck_all_title"), self.t("recheck_all_body", n=n)):
            self.engine._recheck_all_done()

    def confirm_retry_all(self):
        if self.confirm(self.t("retry_all_title"), self.t("retry_all_body")):
            self.engine._retry_all_errors_skipped()

    def confirm_redownload_all(self):
        n = len(self.snapshot.get("done", []))
        if self.confirm(self.t("redownload_all_title"), self.t("redownload_all_body", n=n)):
            self.engine._redownload_all_done()

    def confirm_res_check_all(self):
        s = self.engine.get_settings()
        if self.confirm(self.t("done_res_check"), self.t("res_check_all_body", height=s["res_filter_height"])):
            self.engine._res_check_all_done()

    def confirm_size_check_all(self):
        s = self.engine.get_settings()
        if self.confirm(self.t("done_size_check"), self.t("size_check_all_body", size=s["size_filter_mb"])):
            self.engine._size_check_all_done()

    def confirm_res_check_sel(self):
        ids = self.selected_ids()
        if not ids:
            return
        s = self.engine.get_settings()
        if self.confirm(self.t("done_res_check"), self.t("res_check_sel_body", n=len(ids), height=s["res_filter_height"])):
            groups = self.engine._groups_for_ids(ids)
            if groups:
                self.engine._res_check_groups(groups)

    def confirm_size_check_sel(self):
        ids = self.selected_ids()
        if not ids:
            return
        s = self.engine.get_settings()
        if self.confirm(self.t("done_size_check"), self.t("size_check_sel_body", n=len(ids), size=s["size_filter_mb"])):
            groups = self.engine._groups_for_ids(ids)
            if groups:
                self.engine._size_check_groups(groups)

    def confirm_missing_check_all(self):
        if self.confirm(self.t("missing_check_all_title"), self.t("missing_check_all_body")):
            self._run_missing_check(all_done=True)

    def confirm_missing_check_sel(self):
        ids = self.selected_ids()
        if ids and self.confirm(self.t("missing_check_sel_title"), self.t("missing_check_sel_body", n=len(ids))):
            self._run_missing_check(ids=ids)

    def _run_missing_check(self, all_done=False, ids=None):
        self.show_toast(self.t("missing_scan_toast"))
        r = self.engine.missing_check() if all_done else self.engine.missing_check(ids)
        if r.get("error"):
            self.show_toast(self.t.decode(r["error"]))
            return
        noid = r.get("noid") or 0
        extra = self.t("missing_noid_suffix", n=noid) if noid else ""
        missing = r.get("missing") or []
        if not missing:
            self.show_toast(self.t("missing_none_found", n=r["checked"], extra=extra))
            return
        preview_items = [m["title"] for m in missing[:50]]
        body = self.t("missing_redownload_body", missing=len(missing), checked=r["checked"], extra=extra, preview="")
        if self.confirm_with_preview(self.t("missing_redownload_title"), body, preview_items,
                                     self.t("missing_redownload_label")):
            self.engine.confirm_missing_redownload(r["token"])

    def confirm_delete(self, with_files):
        ids = self.selected_ids()
        if not ids:
            return
        if with_files:
            title, body = self.t("delete_files_title"), self.t("delete_files_body", n=len(ids))
        else:
            title, body = self.t("delete_task_title"), self.t("delete_task_body", n=len(ids))
        if not self.confirm(title, body):
            return
        token, files = self.engine.delete_tasks(ids, with_files=with_files)
        if token and files:
            preview_items = files[:50]
            body2 = self.t("delete_files_confirm_body", n=len(files), preview="")
            if self.confirm_with_preview(self.t("delete_files_confirm_title"), body2, preview_items,
                                         self.t("delete_confirm_label")):
                self.engine.confirm_delete_files(token)

    # ══════════════════════════════════════════
    #  QUIT
    # ══════════════════════════════════════════
    def on_quit(self):
        if not self.confirm(self.t("quit_title"), self.t("quit_body")):
            return
        cfg = load_local_config()
        cfg["geometry"] = self.root.geometry()
        cfg["expanded"] = list(self.expanded)
        cfg["language"] = self.t.lang
        save_local_config(cfg)
        self.engine.shutdown()
        self.root.destroy()

    # ══════════════════════════════════════════
    #  SETTINGS DIALOG
    # ══════════════════════════════════════════
    def open_settings(self):
        s = self.engine.get_settings()
        t = self.t
        win = tk.Toplevel(self.root)
        win.title(t("settings_title"))
        win.transient(self.root)
        win.grab_set()
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)

        row = [0]
        def add(label_text, widget):
            ttk.Label(frm, text=label_text).grid(row=row[0], column=0, sticky="w", pady=3)
            widget.grid(row=row[0], column=1, sticky="w", pady=3, padx=(10, 0))
            row[0] += 1

        lang_var = tk.StringVar(value=self.t.lang)
        add(t("s_language"), ttk.Combobox(frm, textvariable=lang_var, state="readonly",
                                          width=10, values=["en", "ko"]))

        max_var = tk.IntVar(value=s["max_concurrent"])
        add(t("s_max_concurrent"), ttk.Spinbox(frm, from_=1, to=64, textvariable=max_var, width=10))

        autosave_var = tk.IntVar(value=s["autosave_minutes"])
        add(t("s_autosave"), ttk.Spinbox(frm, from_=0, to=120, textvariable=autosave_var, width=10))

        outdir_var = tk.StringVar(value=s["output_dir"])
        outdir_frame = ttk.Frame(frm)
        ttk.Entry(outdir_frame, textvariable=outdir_var, width=38).pack(side="left")
        def browse():
            d = filedialog.askdirectory(initialdir=outdir_var.get() or BASE, parent=win)
            if d:
                outdir_var.set(d)
        ttk.Button(outdir_frame, text=t("s_browse"), command=browse).pack(side="left", padx=(6, 0))
        add(t("s_outdir"), outdir_frame)

        res_var = tk.IntVar(value=s["res_filter_height"])
        add(t("s_res"), ttk.Spinbox(frm, from_=144, to=4320, textvariable=res_var, width=10))

        size_var = tk.IntVar(value=s["size_filter_mb"])
        add(t("s_size"), ttk.Spinbox(frm, from_=1, to=100000, textvariable=size_var, width=10))

        autostart_var = tk.BooleanVar(value=s["autostart"])
        add("", ttk.Checkbutton(frm, text=t("s_autostart"), variable=autostart_var))
        saveonadd_var = tk.BooleanVar(value=s["save_on_add"])
        add("", ttk.Checkbutton(frm, text=t("s_saveonadd"), variable=saveonadd_var))
        small_var = tk.BooleanVar(value=s["small_group_first"])
        add("", ttk.Checkbutton(frm, text=t("s_small_first"), variable=small_var))
        hp_var = tk.BooleanVar(value=s["high_progress_first"])
        add("", ttk.Checkbutton(frm, text=t("s_hp_first"), variable=hp_var))

        autoupdate_var = tk.BooleanVar(value=s["auto_update_tools"])
        autoupdate_frame = ttk.Frame(frm)
        ttk.Checkbutton(autoupdate_frame, text=t("s_auto_update"), variable=autoupdate_var).pack(side="left")
        ttk.Button(autoupdate_frame, text=t("s_check_updates_now"),
                  command=lambda: self.check_tool_updates_now()).pack(side="left", padx=(10, 0))
        add("", autoupdate_frame)

        font_labels = [t("font_normal"), t("font_large"), t("font_xl"), t("font_xxl")]
        font_values = [10, 13, 16, 20]
        font_var = tk.StringVar(value=font_labels[font_values.index(s["font_scale"])]
                                if s["font_scale"] in font_values else font_labels[0])
        add(t("s_font"), ttk.Combobox(frm, textvariable=font_var, state="readonly",
                                      width=14, values=font_labels))

        ttk.Label(frm, text=t("s_gallery_template_label")).grid(row=row[0], column=0, columnspan=2,
                                                                  sticky="w", pady=(12, 0))
        row[0] += 1
        gallery_var = tk.StringVar(value=s["gallery_folder_template"])
        ttk.Entry(frm, textvariable=gallery_var, width=52).grid(
            row=row[0], column=0, columnspan=2, sticky="we", pady=2)
        row[0] += 1
        ttk.Label(frm, text=t("s_gallery_template_hint"), foreground="#888").grid(
            row=row[0], column=0, columnspan=2, sticky="w")
        row[0] += 1

        ttk.Label(frm, text=t("s_persist_label")).grid(row=row[0], column=0, columnspan=2,
                                                        sticky="w", pady=(12, 0))
        row[0] += 1
        persist_box = tk.Text(frm, width=52, height=3)
        persist_box.insert("1.0", s["persist_patterns"])
        persist_box.grid(row=row[0], column=0, columnspan=2, sticky="we", pady=2)
        row[0] += 1
        ttk.Label(frm, text=t("s_persist_hint"), foreground="#888", wraplength=440,
                 justify="left").grid(row=row[0], column=0, columnspan=2, sticky="w")
        row[0] += 1

        cookies_status = s.get("cookies_status") or {}
        cookies_label = (t("s_cookies_saved", at=cookies_status.get("at"))
                         if cookies_status.get("saved") else t("s_cookies_none"))
        ttk.Label(frm, text=f'{t("s_cookies_label")}: {cookies_label}').grid(
            row=row[0], column=0, columnspan=2, sticky="w", pady=(12, 0))
        row[0] += 1
        cookies_box = tk.Text(frm, width=52, height=3)
        cookies_box.grid(row=row[0], column=0, columnspan=2, sticky="we", pady=2)
        row[0] += 1
        cookies_clear_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text=t("s_cookies_clear"), variable=cookies_clear_var).grid(
            row=row[0], column=0, columnspan=2, sticky="w")
        row[0] += 1

        btns = ttk.Frame(frm)
        btns.grid(row=row[0], column=0, columnspan=2, pady=(14, 0))

        def save():
            self.engine.set_settings({
                "max_concurrent": max_var.get(),
                "autosave_minutes": autosave_var.get(),
                "output_dir": outdir_var.get(),
                "res_filter_height": res_var.get(),
                "size_filter_mb": size_var.get(),
                "autostart": autostart_var.get(),
                "save_on_add": saveonadd_var.get(),
                "small_group_first": small_var.get(),
                "high_progress_first": hp_var.get(),
                "auto_update_tools": autoupdate_var.get(),
                "font_scale": font_values[font_labels.index(font_var.get())],
                "gallery_folder_template": gallery_var.get(),
                "persist_patterns": persist_box.get("1.0", "end").strip(),
                "cookies": cookies_box.get("1.0", "end").strip(),
                "cookies_clear": cookies_clear_var.get(),
            })
            if lang_var.get() != self.t.lang:
                self.set_language(lang_var.get())
            self.show_toast(self.t("settings_saved_toast"))
            self.refresh()
            win.destroy()

        ttk.Button(btns, text=t("settings_cancel"), command=win.destroy).pack(side="left", padx=6)
        ttk.Button(btns, text=t("settings_save"), command=save).pack(side="left", padx=6)

    def check_tool_updates_now(self):
        threading.Thread(target=lambda: self.engine.check_tool_updates(notify_no_change=True),
                         daemon=True).start()


def _build_main_app(root):
    for w in root.winfo_children():
        w.destroy()
    import engine as eng
    App(root, eng.Engine())


def main():
    root = tk.Tk()
    root.title("yt-dlp & gallery-dl GUI")
    root.geometry("440x120")
    status_var = tk.StringVar(value="Starting…")
    ttk.Label(root, textvariable=status_var, padding=24).pack(expand=True)

    done = threading.Event()

    def worker():
        ensure_dependencies(
            on_progress=lambda name: root.after(0, status_var.set, f"Downloading {name}…"))
        done.set()

    threading.Thread(target=worker, daemon=True).start()

    def poll():
        if done.is_set():
            _build_main_app(root)
        else:
            root.after(200, poll)

    root.after(200, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
