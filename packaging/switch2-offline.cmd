@echo off
rem Runs the switch2-offline command line from this folder (Python 3.9+ needed).
rem Example, in an administrator terminal:  switch2-offline.cmd start
setlocal
set "PYTHONPATH=%~dp0."
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 -m switch2_offline %*
) else (
  python -m switch2_offline %*
)
exit /b %errorlevel%
