@echo off
setlocal enabledelayedexpansion
title suno-cli setup

echo ============================================
echo  suno-cli setup
echo ============================================
echo.

REM ── Check Python ─────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ from https://python.org
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('python --version') do set PYVER=%%v
echo [OK] %PYVER%

REM ── Install Python dependencies ───────────────────────────────────────────────
echo.
echo Installing Python dependencies...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet requests browser-cookie3
if errorlevel 1 (
    echo ERROR: pip install failed. Check your internet connection.
    pause & exit /b 1
)
echo [OK] requests, browser-cookie3 installed

REM ── Verify imports ───────────────────────────────────────────────────────────
python -c "import requests; import json; print('[OK] imports OK')" 2>nul
if errorlevel 1 (
    echo ERROR: imports failed after install.
    pause & exit /b 1
)

REM ── Create ~/.suno config directory ──────────────────────────────────────────
if not exist "%USERPROFILE%\.suno" (
    mkdir "%USERPROFILE%\.suno"
    echo [OK] Created %USERPROFILE%\.suno\
) else (
    echo [OK] %USERPROFILE%\.suno\ already exists
)

REM ── Check for session cookie ─────────────────────────────────────────────────
echo.
set CONFIG=%USERPROFILE%\.suno\config.json
if exist "%CONFIG%" (
    python -c "import json; c=json.load(open(r'%CONFIG%')); v=c.get('session_cookie',''); print('[OK] session_cookie found (' + str(len(v)) + ' chars)') if v else print('[!!] config.json exists but no session_cookie — run suno-login.exe')"
) else (
    echo [ ] No config.json yet — run suno-login.exe to authenticate
)

REM ── Check for suno-login.exe ─────────────────────────────────────────────────
if exist "%~dp0suno-login.exe" (
    echo [OK] suno-login.exe found
) else (
    echo [ ] suno-login.exe not found in this directory
    echo     Build it: cd suno-login ^& tauri build
)

REM ── Quick API test (only if session_cookie is present) ───────────────────────
echo.
if exist "%CONFIG%" (
    python -c "
import json, sys
c = json.load(open(r'%CONFIG%'))
if not c.get('session_cookie'):
    sys.exit(0)
import requests
h = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
try:
    r = requests.get('https://clerk.suno.com/v1/client?_clerk_js_version=5.35.1',
                     headers={**h, 'Cookie': '__session=' + c['session_cookie']}, timeout=8)
    if r.status_code == 200 and r.json().get('response', {}).get('sessions'):
        print('[OK] Clerk session valid — ready to generate')
    elif r.status_code == 401:
        print('[!!] Session cookie expired — run suno-login.exe to re-authenticate')
    else:
        print('[??] Clerk returned', r.status_code)
except Exception as e:
    print('[!!] Network check failed:', e)
" 2>nul
)

echo.
echo ============================================
echo  Setup complete.
echo.
echo  Next steps:
echo    1. If no session_cookie: run suno-login.exe
echo    2. Generate a song:
echo       python suno.py generate --style "acoustic banjo" --title "Test" --wait
echo ============================================
echo.
pause
