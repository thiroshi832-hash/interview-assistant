@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   AetherStack Interview Assistant - Build
echo ============================================
echo.

REM ---- Clean only THIS product's build artifacts --------------------------
REM Remove only our own subfolders, NOT all of build\ or dist\ — the sender
REM builds into different subfolders, and nuking everything would destroy its
REM build and fail whenever an unrelated file there is locked. rmdir can also
REM fail ("directory not empty") on a locked file, so retry once then abort
REM with a clear message rather than letting PyInstaller die on base_library.zip.
if exist "build\app" rmdir /s /q "build\app" 2>nul
if exist "build\app" rmdir /s /q "build\app" 2>nul
if exist "dist\aetherstack-interview-assistant" rmdir /s /q "dist\aetherstack-interview-assistant" 2>nul
if exist "dist\aetherstack-interview-assistant" rmdir /s /q "dist\aetherstack-interview-assistant" 2>nul
if exist "dist\aetherstack-interview-assistant" (
    echo ERROR: Could not remove dist\aetherstack-interview-assistant ^(is the app still running?^).
    echo Close it and any Explorer window open on that folder, then re-run.
    pause
    exit /b 1
)

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
