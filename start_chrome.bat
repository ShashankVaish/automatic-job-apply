@echo off
REM Launch Chrome with remote debugging so the bot can attach over CDP.
REM Uses a dedicated profile (.\chrome_bot_profile) so your normal Chrome
REM windows and cookies are untouched. Log in to the job sites in THIS window.

setlocal
set "PORT=9222"
set "PROFILE=%~dp0chrome_bot_profile"

set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"

if not defined CHROME (
  echo Could not find chrome.exe in the usual places.
  echo Edit this file and set CHROME to your Chrome path.
  pause
  exit /b 1
)

if not exist "%PROFILE%" mkdir "%PROFILE%"

echo Starting Chrome with debugging port %PORT%
echo Profile: %PROFILE%
echo Leave this Chrome window open while the bot runs.
start "" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check
endlocal
