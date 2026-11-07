@echo off
rem Double-click to open the screening app in your browser. Leave this window open while you work.
cd /d "%~dp0"
title Paper screening
docker info >nul 2>&1
if errorlevel 1 (
  echo Docker Desktop is not running. Start Docker Desktop, wait until it says it is running,
  echo then double-click this file again.
  pause
  exit /b 1
)
docker image inspect paper-screening >nul 2>&1
if errorlevel 1 (
  echo Setting up for the first time. This downloads about 2 GB and takes 10 to 20 minutes, once.
  docker build -t paper-screening . || (echo Setup failed. Check the internet connection and try again. & pause & exit /b 1)
)
echo Opening http://127.0.0.1:8000 in your browser. Close this window to stop the app.
start "" cmd /c "timeout /t 6 >nul & start http://127.0.0.1:8000"
docker run --rm -p 127.0.0.1:8000:8000 -v paper-screening-data:/data paper-screening serve --host 0.0.0.0 --by "%USERNAME%"
pause
