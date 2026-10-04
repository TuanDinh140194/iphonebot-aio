@echo off
setlocal
if not exist .venv (
  python -m venv .venv
  if errorlevel 1 exit /b 1
)
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 exit /b 1
echo.
echo Setup complete.
echo 1. Run start_chrome_bot.bat
 echo 2. Log in to Apple in that Chrome profile if needed.
echo 3. Run start_bot.bat
pause
endlocal
