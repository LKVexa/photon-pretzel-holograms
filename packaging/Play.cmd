@echo off
setlocal
cd /d "%~dp0"
if not exist "OpticalPlayer.exe" (
  echo Extract the complete Windows package before opening Play.cmd.
  pause
  exit /b 1
)
"%~dp0OpticalPlayer.exe" %*
if errorlevel 1 (
  echo The reader could not start. Read the message above or READ-ME-FIRST.txt.
  pause
)
