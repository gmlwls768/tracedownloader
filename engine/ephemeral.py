"""
Session-only (non-persistent) video/gallery auto-detect and download.
"""

import subprocess
import threading
import json
import os
import time

from .models import *  # noqa: F401,F403 - internal package, see models.py __all__


class _EphemeralMixin:
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
        yt-dlp first, and only trust it outright when its probe actually
        succeeds (real, downloadable video info). Any failure - including a
        real extractor matching but finding no video - falls through to
        gallery-dl, since several sites (Twitter/X, Reddit, Pixiv...) have
        both video and image-only posts and the two tools split coverage of
        them. Only if gallery-dl also has nothing is it truly unsupported."""
        cookies_tmp = self._cookies_tempcopy()
        probe_cmd = [YTDLP_BIN, "-J", "--flat-playlist", "--no-warnings"]
        if cookies_tmp:
            probe_cmd += ["--cookies", cookies_tmp]
        probe_cmd.append(url)
        probe_ok, probe_err = False, ""
        try:
            proc = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=60)
            probe_ok = proc.returncode == 0
            if not probe_ok:
                probe_err = (proc.stderr or "").strip()
        except Exception as e:
            probe_err = str(e)
        finally:
            self._cleanup_cookies_tmp(cookies_tmp)

        if probe_ok:
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

        # Neither tool could do anything with it. Surface yt-dlp's own
        # reason (still shown via {reason}) even though a real extractor
        # match failing for a concrete reason isn't quite "unsupported" -
        # gallery-dl having nothing either means there's nothing more to try.
        tail = probe_err.strip().splitlines()[-1][:150] if probe_err.strip() else ""
        self._ephemeral_update(entry, state="error", message=M("url_unsupported", reason=tail))

    def _run_gallery_download(self, entry, url, meta):
        artist_str, title, gid = _gallery_meta_fields(meta)
        folder_name = self._format_gallery_name(artist_str, title, gid)
        count = meta.get("count")
        out_dir = self._gallery_output_dir(url)

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
               "-o", self._ephemeral_video_template(url), "--no-warnings"]
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
