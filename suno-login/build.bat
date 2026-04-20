@echo off
setlocal
title suno-login build
cd /d "%~dp0"

echo Building suno-login.exe (release)...
echo This takes 5-10 minutes the first time.
echo.

REM Check Rust
cargo --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Rust not found. Install from https://rustup.rs
    pause & exit /b 1
)

REM Check Tauri CLI
tauri --version >nul 2>&1
if errorlevel 1 (
    echo Installing Tauri CLI...
    npm install -g @tauri-apps/cli
)

REM Build
tauri build --bundles none 2>&1
if errorlevel 1 (
    echo.
    echo Build failed. Check errors above.
    pause & exit /b 1
)

REM Copy EXE to project root
set SRC=src-tauri\target\release\suno-login.exe
if exist "%SRC%" (
    copy /y "%SRC%" "..\suno-login.exe" >nul
    echo.
    echo [OK] Built: suno-login.exe
    echo      Copied to: %~dp0..\suno-login.exe
) else (
    echo ERROR: EXE not found at %SRC%
    pause & exit /b 1
)

echo.
echo Done. Run suno-login.exe to authenticate.
pause
