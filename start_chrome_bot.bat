@echo off
setlocal
set "PROFILE_DIR=%LOCALAPPDATA%\iPhoneBot-v10-Chrome"
set "CHROME="

if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

if not defined CHROME (
  echo Google Chrome was not found in the standard Windows locations.
  echo Edit start_chrome_bot.bat and set CHROME to your chrome.exe path.
  pause
  exit /b 1
)

echo Starting Chrome for iPhoneBot v10...
echo Profile: %PROFILE_DIR%
echo CDP: http://127.0.0.1:9222
start "iPhoneBot Chrome" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%PROFILE_DIR%"
endlocal
