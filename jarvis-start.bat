@echo off
REM Phase 3 autostart launcher: always uses the project venv, no relaunch wrapper.
cd /d "%~dp0"
".venv\Scripts\python.exe" jarvis.py %*
if errorlevel 1 pause
