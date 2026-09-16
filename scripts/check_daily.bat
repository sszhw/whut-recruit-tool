@echo off
REM ============================================================
REM  WHUT recruitment tool - daily incremental update driver
REM  Called by Windows Task Scheduler (task: WHUT_Preach_Daily_Check, daily 09:00).
REM  Runs app\run_daily.py, which in turn:
REM    1) check_recruit_update.py - new recruitment info / job fairs
REM    2) check_preach_update.py  - new preach meetings + work-flow refresh
REM  All output is appended to data\preach_update_log.txt (UTF-8 with BOM)
REM  by run_daily.py, so the log never gets mixed-encoding garbage.
REM ============================================================
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0..\app"
python run_daily.py
