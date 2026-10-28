@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Project Python environment was not found.
    pause
    exit /b 1
)
set "PYTHONUTF8=1"
"%~dp0.venv\Scripts\python.exe" -m tools.integrations --diagnostics
set "REPOLAUNCH_EXIT=%ERRORLEVEL%"
echo.
echo This diagnostic only reads the RepoLaunch integration project.
echo No services were started, stopped, restarted, or deleted.
echo Press any key to close this window.
pause >nul
exit /b %REPOLAUNCH_EXIT%
