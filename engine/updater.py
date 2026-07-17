"""
Background self-update for the download tools (yt-dlp / gallery-dl single
binaries, and ffmpeg via its build zip - see the *_URLS constants in
models.py), plus an in-app "is there a newer TraceDownloader?" check.
"""

import json
import re
import subprocess
import os
import tempfile
import zipfile
import urllib.request

from .models import *  # noqa: F401,F403 - internal package, see models.py __all__


class _UpdaterMixin:
    @staticmethod
    def _tool_version(path):
        try:
            return subprocess.run([path, "--version"], capture_output=True,
                                  text=True, timeout=15,
                                  creationflags=SUBPROC_FLAGS).stdout.strip()
        except Exception:
            return None

    @staticmethod
    def _ffmpeg_version(path):
        """First line of `ffmpeg -version`, trimmed to just the version token
        (e.g. "ffmpeg version N-125551-g... " -> "N-125551-g...")."""
        try:
            out = subprocess.run([path, "-version"], capture_output=True,
                                 text=True, timeout=15,
                                 creationflags=SUBPROC_FLAGS).stdout
            first = (out or "").splitlines()[0] if out else ""
            m = re.match(r'ffmpeg version (\S+)', first)
            return m.group(1) if m else (first.strip() or None)
        except Exception:
            return None

    def _tool_version_cached(self, path):
        """Version lookup for the Settings screen. Running "--version" on the
        packaged Windows tools takes seconds each (PyInstaller startup), which
        made opening Settings visibly hang - so the result is cached against
        the binary's mtime and only re-read after the file actually changes
        (i.e. after an update). Warmed in the background at startup.
        ffmpeg reports its version differently, hence the per-name probe."""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        hit = self._tool_ver_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        version = (self._ffmpeg_version(path)
                   if os.path.basename(path).startswith(("ffmpeg", "ffprobe"))
                   else self._tool_version(path))
        self._tool_ver_cache[path] = (mtime, version)
        return version

    def _warm_tool_versions(self):
        for path in (YTDLP_BIN, GALLERYDL_BIN, FFMPEG_BIN):
            self._tool_version_cached(path)

    def _update_tool_binary(self, name, url):
        """Download the latest build of `name` over our own managed copy.
        Returns "updated"/"latest"/"failed", or None if we don't manage
        this binary (not present under BIN_SEARCH_DIR/bin - e.g. installed
        via apt or found on PATH instead, which we never touch)."""
        target = _managed_bin_path(name)
        if not os.path.isfile(target):
            return None
        old_version = self._tool_version(target)
        tmp = target + ".update"
        try:
            urllib.request.urlretrieve(url, tmp)
            if os.name != "nt":
                os.chmod(tmp, 0o755)
            new_version = self._tool_version(tmp)
            if not new_version:
                # The downloaded build doesn't even run here (wrong glibc,
                # wrong OS/arch, corrupt download...) - keep the working
                # copy instead of replacing it with a broken one.
                raise RuntimeError("downloaded build did not run (--version failed)")
            os.replace(tmp, target)
        except Exception as e:
            try: os.remove(tmp)
            except OSError: pass
            print(f"[update] {name} standalone build failed: {e}")
            return self._update_tool_via_pip(name, target, old_version)
        return self._mark_if_changed(name, old_version, new_version)

    def _update_tool_via_pip(self, name, target, old_version):
        """Fallback for when the standalone build won't run on this system
        (e.g. an older glibc than the build targets, seen on some LXC/older
        distros): if `target` is a symlink into a local venv - the layout
        deploy/install.sh sets up when it can't get a standalone binary
        either - upgrade the package there instead of giving up."""
        pip = _pip_fallback_path()
        pkg = TOOL_PIP_NAMES.get(name)
        if not pip or not pkg or not os.path.islink(target):
            return "failed"
        try:
            subprocess.run([pip, "install", "-q", "--upgrade", pkg],
                          check=True, timeout=180, capture_output=True, text=True,
                          creationflags=SUBPROC_FLAGS)
        except Exception as e:
            print(f"[update] {name} pip fallback failed: {e}")
            return "failed"
        return self._mark_if_changed(name, old_version, self._tool_version(target))

    def _mark_if_changed(self, name, old_version, new_version):
        if new_version != old_version:
            self.db.set_meta(f"{name}_updated_at", _utcnow())
            return "updated"
        return "latest"

    # ── ffmpeg (zip, not a single binary) ──
    def _update_ffmpeg(self, force=False):
        """Refresh the ffmpeg/ffprobe pair WE manage (the Windows build in
        bin/). A system ffmpeg (apt on Linux) isn't ours to touch -> None.
        The build is a 100MB+ zip tagged by date, so the latest release tag
        is compared against the one we last installed and nothing is
        downloaded unless it actually changed (or force=True)."""
        target = _managed_bin_path("ffmpeg")
        probe_target = _managed_bin_path("ffprobe")
        if not os.path.isfile(target):
            return None   # not our copy (system/PATH ffmpeg) - leave alone

        latest_tag = self._github_latest_tag(FFMPEG_BUILD_TAG_API)
        installed_tag = self.db.get_meta("ffmpeg_build_tag", "")
        if not force and latest_tag and latest_tag == installed_tag:
            return "latest"

        bin_dir = os.path.dirname(target)
        got = False
        for url in FFMPEG_BUILD_ZIP_URLS:
            tmp_zip = os.path.join(bin_dir, "_ffmpeg_update.zip")
            try:
                urllib.request.urlretrieve(url, tmp_zip)
                with zipfile.ZipFile(tmp_zip) as z:
                    for info in z.infolist():
                        base = os.path.basename(info.filename)
                        want = ("ffmpeg.exe", "ffprobe.exe") if os.name == "nt" \
                               else ("ffmpeg", "ffprobe")
                        if base in want:
                            dst = os.path.join(bin_dir, base + ".new")
                            with z.open(info) as src, open(dst, "wb") as out:
                                out.write(src.read())
                            if os.name != "nt":
                                os.chmod(dst, 0o755)
            except Exception as e:
                print(f"[update] ffmpeg from {url}: {e}")
            finally:
                try: os.remove(tmp_zip)
                except OSError: pass
            new_ff = target + ".new"
            new_fp = probe_target + ".new"
            if os.path.isfile(new_ff) and os.path.isfile(new_fp):
                got = True
                break
            for p in (new_ff, new_fp):
                try: os.remove(p)
                except OSError: pass

        if not got:
            return "failed"

        old_version = self._ffmpeg_version(target)
        try:
            if not self._ffmpeg_version(target + ".new"):
                raise RuntimeError("downloaded ffmpeg did not run")
            os.replace(target + ".new", target)
            os.replace(probe_target + ".new", probe_target)
        except Exception as e:
            for p in (target + ".new", probe_target + ".new"):
                try: os.remove(p)
                except OSError: pass
            print(f"[update] ffmpeg swap failed: {e}")
            return "failed"
        if latest_tag:
            self.db.set_meta("ffmpeg_build_tag", latest_tag)
        return self._mark_if_changed("ffmpeg", old_version, self._ffmpeg_version(target))

    @staticmethod
    def _github_latest_tag(api_url):
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": "TraceDownloader"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r).get("tag_name") or ""
        except Exception:
            return ""

    # ── app self-update check (informational; applying it differs per front end) ──
    def check_app_update(self):
        """Is there a newer TraceDownloader release on GitHub? Returns
        {current, latest, newer, url}. Doesn't install anything - the Windows
        build swaps its own .exe, the web server runs `git pull` (see
        deploy/update.sh); both are user actions surfaced in Settings."""
        latest = self._github_latest_tag(APP_RELEASES_API)
        latest_clean = latest.lstrip("v") if latest else ""
        newer = bool(latest_clean) and self._version_gt(latest_clean, APP_VERSION)
        return {"current": APP_VERSION, "latest": latest_clean,
                "newer": newer, "url": APP_REPO_URL + "/releases/latest"}

    @staticmethod
    def _version_gt(a, b):
        """True if version string a is newer than b (numeric dotted compare;
        unknown/garbage sorts as not-newer so we never nag on a bad parse)."""
        def parts(v):
            out = []
            for p in v.split("."):
                m = re.match(r'\d+', p)
                out.append(int(m.group()) if m else 0)
            return out
        try:
            pa, pb = parts(a), parts(b)
            pa += [0] * (len(pb) - len(pa))
            pb += [0] * (len(pa) - len(pb))
            return pa > pb
        except Exception:
            return False

    def check_tool_updates(self):
        """Refresh yt-dlp/gallery-dl in place. Safe to call anytime - a file
        that's currently in use (a download running from it, mainly a
        Windows concern) just fails the replace and is retried next cycle.
        Always reports what happened (updated/already latest/failed) even
        from the background loop, since a silent "failed" on an LXC with an
        incompatible glibc looks identical to one that's actually broken."""
        results = {name: self._update_tool_binary(name, url)
                   for name, url in TOOL_UPDATE_URLS.items()}
        # ffmpeg comes as a zip and is skipped entirely when it's a system
        # copy (None); only counted when we actually manage it.
        ff = self._update_ffmpeg()
        if ff is not None:
            results["ffmpeg"] = ff
        updated = [n for n, r in results.items() if r == "updated"]
        latest  = [n for n, r in results.items() if r == "latest"]
        failed  = [n for n, r in results.items() if r == "failed"]
        if updated:
            self._show_toast(M("tools_updated", names=", ".join(updated)))
        if failed:
            self._show_toast(M("tools_update_failed", names=", ".join(failed)))
        if latest:
            self._show_toast(M("tools_already_latest", names=", ".join(latest)))
        self.db.set_meta("last_update_check", _utcnow())
        return results

    def _update_check_loop(self):
        if self._closing.wait(5):   # brief delay after startup, then run every interval
            return
        while True:
            if self._cfg_auto_update_tools:
                try:
                    self.check_tool_updates()
                except Exception as e:
                    print(f"[update] check failed: {e}")
            if self._closing.wait(TOOL_UPDATE_INTERVAL):
                return
