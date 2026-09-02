@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :not_installed

".venv\Scripts\python.exe" monitor.py --once --notify-always
set result=%errorlevel%
echo.
if %result%==0 echo Test complete. Check Telegram.
if not %result%==0 echo Test failed. Copy the error above and send it to Codex.
pause
exit /b %result%

:not_installed
echo First run INSTALL_AND_SETUP.cmd.
pause
exit /b 1
