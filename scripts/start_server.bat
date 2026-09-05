@echo off
REM Start the WHUT recruitment tool web UI.
REM Usage: double-click or run from scripts\ ; requires Python 3.9+ with requirements.txt installed.
chcp 65001 >nul
cd /d "%~dp0..\app"
python server.py --port 8765
