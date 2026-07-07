@echo off
REM Run the companion Sender app directly from source (no build).
REM This is the tiny app that runs on the INTERVIEW computer in helper-network
REM mode — captures mic + WASAPI loopback and streams to the helper laptop.

python -m sender.sender_app %*
if errorlevel 1 pause
