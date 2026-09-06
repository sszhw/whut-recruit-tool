@echo off
REM ============================================================
REM  WHUT recruitment tool - Web UI startup script
REM  Entry point. Double-click to start the server on port 8765.
REM  It does NOT auto-open the browser; open the printed URL manually.
REM ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0app"

REM ---- Check whether the server is already listening on port 8765 ----
netstat -ano | findstr /r /c:":8765 .*LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [OK] Web UI is ALREADY running.
    echo      Do not start a duplicate instance.
    echo      Open in your browser:  http://127.0.0.1:8765
    echo.
    pause
    exit /b 0
)

REM ---- Start the server (foreground; Ctrl+C to stop) ----
echo [..] Starting WHUT Web UI on http://127.0.0.1:8765 ...
echo [..] Keep this window open. Open the URL in your browser when ready.
echo.
python server.py --port 8765

echo.
echo [OK] Server stopped.
pause
endlocal
