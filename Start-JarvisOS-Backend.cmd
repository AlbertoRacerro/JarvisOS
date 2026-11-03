@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python 3.11 or newer is required to start the JarvisOS backend.
  echo Install Python, reopen this launcher, and try again.
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-backend.ps1"
if errorlevel 1 (
  echo.
  echo JarvisOS backend stopped with an error.
  echo.
  pause
  exit /b 1
)

endlocal
