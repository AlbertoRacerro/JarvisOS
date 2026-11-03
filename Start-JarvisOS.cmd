@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python 3.11 or newer is required to start JarvisOS.
  echo Install Python, reopen this launcher, and try again.
  echo.
  pause
  exit /b 1
)

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

echo Starting JarvisOS backend on http://localhost:8000 ...
start "JarvisOS Backend" cmd /k ""%~dp0Start-JarvisOS-Backend.cmd""

echo Starting JarvisOS frontend on http://localhost:5173 ...
timeout /t 3 /nobreak >nul
start "JarvisOS Frontend" cmd /k ""%~dp0Start-JarvisOS-Frontend.cmd""

echo.
echo JarvisOS is starting. The browser should open shortly at:
echo http://localhost:5173
echo.
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
