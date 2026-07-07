@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   AetherStack Sender - Build
echo ============================================
echo.

REM ---- Clean previous build artifacts -------------------------------------
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM ---- Build the EXE (onedir; see sender.spec) -----------------------------
echo Running PyInstaller...
python -m PyInstaller --noconfirm sender.spec
if errorlevel 1 (
    echo PyInstaller failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Build complete.
echo   Output folder: dist\aetherstack-sender
echo   Run aetherstack-sender.exe inside that folder.
echo   Zip the whole folder to ship it.
echo ============================================
pause
endlocal
