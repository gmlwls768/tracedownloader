"""
Builds the Windows executable from app.py. Must run on Windows -
PyInstaller doesn't cross-compile.

    pip install -r requirements.txt -r requirements-dev.txt
    python deploy/build_windows.py

Output: dist/ytdlp-gallery-dl-gui.exe
"""
import os
import PyInstaller.__main__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PyInstaller.__main__.run([
    os.path.join(ROOT, "app.py"),
    "--name=ytdlp-gallery-dl-gui",
    "--onefile",
    "--console",
    f"--add-data={os.path.join(ROOT, 'static')}{os.pathsep}static",
    "--collect-submodules=uvicorn",
    "--collect-submodules=fastapi",
    "--distpath=" + os.path.join(ROOT, "dist"),
    "--workpath=" + os.path.join(ROOT, "build"),
    "--specpath=" + ROOT,
    "--noconfirm",
])
