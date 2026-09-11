@echo off
REM Run the interview-assistant app directly from source (no build).
REM First time: .\install.ps1, then set ANTHROPIC_API_KEY or OPENAI_API_KEY.

python app.py %*
if errorlevel 1 pause
