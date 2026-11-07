@echo off
rem ─────────────────────────────────────────────────────────────────────────
rem  Startup report. This window opens at logon on purpose: it shows the
rem  command centre (today's slots, node health, and how many clips are left
rem  in each raw video) and then runs the pipeline with its output visible.
rem  It never asks for input and closes itself 5 minutes after the run ends.
rem ─────────────────────────────────────────────────────────────────────────
title omfplays - startup report
setlocal EnableExtensions
cd /d "%~dp0"

set "PIPELINE_NODE=local"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%~dp0logs" mkdir "%~dp0logs"

echo ============================================================
echo    OMFPLAYS STARTUP REPORT   %DATE% %TIME%
echo ============================================================
"%PY%" -u "%~dp0pipeline_status.py"
echo.
echo --- pipeline starting now (raw is never downloaded automatically) ---
echo.
"%PY%" -u "%~dp0run_local_pipeline.py"
set "RC=%ERRORLEVEL%"
echo.
echo ============================================================
echo    pipeline exit code: %RC%
echo    closing this window in 5 minutes
echo ============================================================
timeout /t 300
exit /b %RC%
