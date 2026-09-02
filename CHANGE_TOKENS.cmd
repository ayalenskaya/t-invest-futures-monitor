@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Bot is not installed yet. Run INSTALL_AND_SETUP.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" setup_bot.py --tinvest-only
if errorlevel 1 (
  echo.
  echo Setup failed. Check the message above.
  pause
  exit /b 1
)
echo.
echo T-Invest token updated. Now run TEST_ONCE.cmd.
pause
