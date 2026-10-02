@echo off
setlocal
chcp 65001 >nul 2>nul
if errorlevel 1 (
  chcp 936 >nul 2>nul
  set "YXY_CONSOLE_ENCODING=gbk"
) else (
  set "YXY_CONSOLE_ENCODING=utf-8"
)
set "PYTHONUTF8=1"
set "ROOT=%~dp0"
cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%backend\src;%PYTHONPATH%"

where python >nul 2>nul || (echo Python was not found. Install Python and add it to PATH. & pause & exit /b 1)
python -m dgutbot.app.browser_launcher
if errorlevel 1 (
  echo.
  echo Browser launcher failed. See the error above and browser-launcher.log.
  pause
  exit /b 1
)
