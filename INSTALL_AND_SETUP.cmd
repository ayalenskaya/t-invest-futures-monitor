@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"

py -3 --version >nul 2>&1
if not errorlevel 1 goto :use_py

python --version >nul 2>&1
if errorlevel 1 goto :no_python
python -m venv .venv
goto :after_venv

:use_py
py -3 -m venv .venv

:after_venv
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error
".venv\Scripts\python.exe" setup_bot.py
if errorlevel 1 goto :error

echo.
echo Setup complete. Now run TEST_ONCE.cmd.
pause
exit /b 0

:no_python
echo.
echo Python was not found.
echo Install Python 3.11 or 3.12 from python.org and enable Add Python to PATH.
pause
exit /b 1

:error
echo.
echo Setup failed. Copy the error above and send it to Codex.
pause
exit /b 1
