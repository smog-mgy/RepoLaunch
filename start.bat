@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "REPOLAUNCH_EXIT=%ERRORLEVEL%"
if not "%REPOLAUNCH_EXIT%"=="0" (
  echo.
  echo RepoLaunch could not start. Please read startup.log in this folder.
)
echo.
echo The RepoLaunch launcher has finished. Press any key to close this window.
pause >nul
exit /b %REPOLAUNCH_EXIT%
