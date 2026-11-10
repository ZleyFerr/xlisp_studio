@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 studio.pyw
  if errorlevel 1 pause
  exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
  python studio.pyw
  if errorlevel 1 pause
  exit /b
)
echo Install Python 3.10 or newer from https://www.python.org/downloads/windows/
echo Enable "Add python.exe to PATH" and keep "tcl/tk and IDLE" enabled.
echo Then run this file again.
pause
