@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :not_installed

title T-Invest Futures Monitor
".venv\Scripts\python.exe" monitor.py
echo.
echo The bot stopped. Copy the error above and send it to Codex.
pause
exit /b 1

:not_installed
echo First run INSTALL_AND_SETUP.cmd.
pause
exit /b 1
