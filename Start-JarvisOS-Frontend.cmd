@echo off
setlocal

cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  call :NodeMissing
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  call :NodeMissing
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-frontend.ps1"
if errorlevel 1 (
  echo.
  echo JarvisOS frontend stopped with an error.
  echo.
  pause
  exit /b 1
)

endlocal
exit /b 0

:NodeMissing
echo.
echo Node.js LTS with npm is required to start the JarvisOS frontend.
echo Install it from the official Node.js website:
echo https://nodejs.org/
echo.
echo After installation, reopen this launcher from File Explorer.
echo.
pause
exit /b 0
