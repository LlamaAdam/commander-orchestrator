@echo off
REM Double-clickable launcher for the Commander Orchestrator.
REM Uses this machine's venv Python (lightweight launcher: no bundling).
REM Opens the interactive menu; pass args to run a command directly, e.g.
REM     Orchestrator.cmd audit
REM     Orchestrator.cmd set-repo C:\dev\commander-builder
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [orchestrator] venv Python not found at:
  echo     %PY%
  echo.
  echo Create the venv first, from this folder:
  echo     py -m venv .venv
  echo     .venv\Scripts\python -m pip install -e .
  echo.
  pause
  exit /b 1
)

"%PY%" -m orchestrator.launcher %*
set "RC=%ERRORLEVEL%"

REM Keep the window open when double-clicked (no args) so output is readable.
if "%~1"=="" pause
exit /b %RC%
