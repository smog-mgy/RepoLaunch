@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-integrations.ps1" %*
set "REPOLAUNCH_EXIT=%ERRORLEVEL%"
echo.
if not "%REPOLAUNCH_EXIT%"=="0" echo Integration deployment did not finish. Existing data was preserved.
echo Press any key to close this window.
pause >nul
exit /b %REPOLAUNCH_EXIT%
