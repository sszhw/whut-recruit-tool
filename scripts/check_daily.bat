@echo off
REM Run the incremental daily update for recruitment info and preach meetings.
REM Scheduled daily at 09:00 (Windows Task Scheduler example).
chcp 65001 >nul
cd /d "%~dp0..\app"
python check_recruit_update.py
python check_preach_update.py
