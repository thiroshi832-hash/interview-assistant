@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   AetherStack Interview Assistant - Build
echo ============================================
echo.

REM ---- Clean previous build artifacts -------------------------------------
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM ---- Build the EXE (onedir; see app.spec for why not onefile) -----------
echo Running PyInstaller...
python -m PyInstaller --noconfirm app.spec
if errorlevel 1 (
    echo PyInstaller failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Build complete.
echo   Output folder: dist\aetherstack-interview-assistant
echo   Run aetherstack-interview-assistant.exe inside that folder.
echo   Zip the whole folder to ship it.
echo ============================================
pause
endlocal
