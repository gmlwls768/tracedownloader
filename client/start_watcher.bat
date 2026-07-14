@echo off
REM Runs in the background (no console window). Put a shortcut to this file
REM in shell:startup to launch it automatically at login.
start "" pythonw "%~dp0clipboard_watcher.py"
