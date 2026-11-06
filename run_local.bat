@echo off
rem ─────────────────────────────────────────────────────────────────────────
rem  omfplays local pipeline launcher.
rem  Started by Task Scheduler / at logon, so it must never block on input.
rem  Interactive extras: run_local.bat --interactive
rem ─────────────────────────────────────────────────────────────────────────
setlocal EnableExtensions
cd /d "%~dp0"

set "PIPELINE_NODE=local"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%~dp0logs" mkdir "%~dp0logs"

if /I "%~1"=="--interactive" set "PAUSE_AT_END=1"
if /I "%~1"=="--interactive" shift

echo [%DATE% %TIME%] run_local.bat start (python: %PY%)
"%PY%" -u "%~dp0run_local_pipeline.py" %*
set "RC=%ERRORLEVEL%"
echo [%DATE% %TIME%] run_local.bat end (exit %RC%)

if defined PAUSE_AT_END pause
exit /b %RC%
