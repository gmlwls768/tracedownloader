"""
Builds the Windows executable from app.py. Must run on Windows -
PyInstaller doesn't cross-compile.

    pip install -r requirements-dev.txt
    python deploy/build_windows.py

Output: dist/ytdlp-gallery-dl-gui.exe

app.py + engine/ + i18n.py are plain-stdlib (tkinter, sqlite3, subprocess...)
- no FastAPI/uvicorn/static assets needed for this build, unlike the Linux
web deployment (see requirements.txt / deploy/install.sh for that side).
"""
import os
import PyInstaller.__main__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PyInstaller.__main__.run([
    os.path.join(ROOT, "app.py"),
    "--name=ytdlp-gallery-dl-gui",
    "--onefile",
    "--windowed",
    "--distpath=" + os.path.join(ROOT, "dist"),
    "--workpath=" + os.path.join(ROOT, "build"),
    "--specpath=" + ROOT,
    "--noconfirm",
])
