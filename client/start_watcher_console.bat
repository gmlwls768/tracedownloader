@echo off
chcp 65001 >nul
REM Runs with a visible console window so you can see the logs (for
REM debugging). Close the window to stop it.
python "%~dp0clipboard_watcher.py"
if errorlevel 1 pause
