@echo off
setlocal
title suno-captcha build
cd /d "%~dp0"

echo Building suno-captcha.exe (release)...
echo.

cargo --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Rust not found. Install from https://rustup.rs
    pause & exit /b 1
)

tauri --version >nul 2>&1
if errorlevel 1 (
    echo Installing Tauri CLI...
    npm install -g @tauri-apps/cli
)

tauri build --bundles none 2>&1
if errorlevel 1 (
    echo.
    echo Build failed. Check errors above.
    pause & exit /b 1
)

set SRC=src-tauri\target\release\suno-captcha.exe
if exist "%SRC%" (
    copy /y "%SRC%" "..\suno-captcha.exe" >nul
    echo.
    echo [OK] Built: suno-captcha.exe
    echo      Copied to: %~dp0..\suno-captcha.exe
) else (
    echo ERROR: EXE not found at %SRC%
    pause & exit /b 1
)

echo.
echo Done. Double-click suno-captcha.exe to start the captcha service.
pause
