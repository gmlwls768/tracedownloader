"""
Background yt-dlp/gallery-dl self-update (see TOOL_UPDATE_URLS in models.py).
"""

import subprocess
import os
import urllib.request

from .models import *  # noqa: F401,F403 - internal package, see models.py __all__


class _UpdaterMixin:
    @staticmethod
    def _tool_version(path):
        try:
            return subprocess.run([path, "--version"], capture_output=True,
                                  text=True, timeout=15).stdout.strip()
        except Exception:
            return None

    def _update_tool_binary(self, name, url):
        """Download the latest build of `name` over our own managed copy.
        Returns True if the version string actually changed, False if the
        download/replace failed, or None if we don't manage this binary
        (not present under BIN_SEARCH_DIR/bin - e.g. installed via apt or
        found on PATH instead, which we never touch)."""
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
        return new_version != old_version

    def _update_tool_via_pip(self, name, target, old_version):
        """Fallback for when the standalone build won't run on this system
        (e.g. an older glibc than the build targets, seen on some LXC/older
        distros): if `target` is a symlink into a local venv - the layout
        deploy/install.sh sets up when it can't get a standalone binary
        either - upgrade the package there instead of giving up."""
        pip = _pip_fallback_path()
        pkg = TOOL_PIP_NAMES.get(name)
        if not pip or not pkg or not os.path.islink(target):
            return False
        try:
            subprocess.run([pip, "install", "-q", "--upgrade", pkg],
                          check=True, timeout=180, capture_output=True, text=True)
        except Exception as e:
            print(f"[update] {name} pip fallback failed: {e}")
            return False
        return self._tool_version(target) != old_version

    def check_tool_updates(self, notify_no_change=False):
        """Refresh yt-dlp/gallery-dl in place. Safe to call anytime - a file
        that's currently in use (a download running from it, mainly a
        Windows concern) just fails the replace and is retried next cycle."""
        results = {name: self._update_tool_binary(name, url)
                   for name, url in TOOL_UPDATE_URLS.items()}
        updated = [n for n, r in results.items() if r is True]
        failed  = [n for n, r in results.items() if r is False]
        if updated:
            self._show_toast(M("tools_updated", names=", ".join(updated)))
        if failed:
            self._show_toast(M("tools_update_failed", names=", ".join(failed)))
        if notify_no_change and not updated and not failed:
            self._show_toast(M("tools_already_latest"))
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
