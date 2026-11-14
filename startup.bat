@echo off
title omfplays Startup Processor

setlocal enabledelayedexpansion

:: Set console buffer size (proper, not full screen)
mode con:cols=95 lines=40

:: Log file for unattended runs
set LOGFILE=C:\omfplays\logs\startup.log
mkdir "%LOGFILE:~0,-12%\.." 2>nul
echo [%date% %time%] Startup initiated >> "%LOGFILE%"

echo ============================================
echo   omfplays Startup Processor
echo   Started: %date% %time%
echo ============================================
echo [%date% %time%] Startup initiated >> "%LOGFILE%"

:: ═════════════════════════════════════════════════════════════════════════════════
# PRE-FLIGHT CHECKS
# ═════════════════════════════════════════════════════════════════════════════════

:: Check Python availability and version
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Install Python and try again.
    echo [%date% %time%] FATAL: Python not found >> "%LOGFILE%"
    pause
    exit /b 1
)

python --version 2>nul | findstr /c:"3." >nul 2>&1
if errorlevel 1 (
    echo [WARN] Python 3.x not detected. Some features may fail.
    echo [%date% %time%] WARN: Python version check failed >> "%LOGFILE%"
)

:: Check junction path exists
if not exist "C:\omfplays\run_local_pipeline.py" (
    echo [ERROR] C:\omfplays junction not found.
    echo         Run: mklink /J C:\omfplays "F:\✈Content Engine\omfplays"
    echo [%date% %time%] FATAL: C:\omfplays junction not found >> "%LOGFILE%"
    pause
    exit /b 1
)

:: Check config.json exists
if not exist "C:\omfplays\config.json" (
    echo [ERROR] config.json not found at C:\omfplays\config.json
    echo [%date% %time%] FATAL: config.json not found >> "%LOGFILE%"
    pause
    exit /b 1
)

:: Check slots.json exists
if not exist "C:\omfplays\slots.json" (
    echo [ERROR] slots.json not found at C:\omfplays\slots.json
    echo [%date% %time%] FATAL: slots.json not found >> "%LOGFILE%"
    pause
    exit /b 1
)

:: Check pipeline_status.py exists
if not exist "C:\omfplays\pipeline_status.py" (
    echo [ERROR] pipeline_status.py not found at C:\omfplays\pipeline_status.py
    echo [%date% %time%] FATAL: pipeline_status.py not found >> "%LOGFILE%"
    pause
    exit /b 1
)

:: Check build_shorts_omf.py exists
if not exist "C:\omfplays\build_shorts_omf.py" (
    echo [ERROR] build_shorts_omf.py not found at C:\omfplays\build_shorts_omf.py
    echo [%date% %time%] FATAL: build_shorts_omf.py not found >> "%LOGFILE%"
    pause
    exit /b 1
)

:: Check ffmpeg is available (needed for build step)
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [WARN] ffmpeg not found in PATH. Build step will fail.
    echo         Install ffmpeg and add to PATH.
    echo [%date% %time%] WARN: ffmpeg not found >> "%LOGFILE%"
)

:: ═════════════════════════════════════════════════════════════════════════════════
# RUN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════════

echo [INFO] Starting pipeline...
echo [%date% %time%] INFO: Starting pipeline >> "%LOGFILE%"

python "C:\omfplays\run_local_pipeline.py"

set ERRORLEVEL=%ERRORLEVEL%

echo ============================================
echo   Pipeline finished with exit code: %ERRORLEVEL%
echo   Finished: %date% %time%
echo ============================================
echo [%date% %time%] Pipeline finished with exit code: %ERRORLEVEL% >> "%LOGFILE%"

if %ERRORLEVEL% equ 0 (
    echo [SUCCESS] Pipeline completed cleanly.
    echo [%date% %time%] SUCCESS: Pipeline completed cleanly >> "%LOGFILE%"
) else (
    echo [FAILED] Pipeline exited with errors. Check logs/pipeline.log.
    echo [%date% %time%] FAILED: Pipeline exited with errors >> "%LOGFILE%"
)

:: ═════════════════════════════════════════════════════════════════════════════════
# START TELEGRAM BOT DAEMON (interactive buttons)
# ══════════════════════════════════════════════════════════════════════════════════

echo [INFO] Starting Telegram bot daemon...
echo [%date% %time%] INFO: Starting Telegram bot daemon >> "%LOGFILE%"

start "omfplays Bot" python "C:\omfplays\telegram_bot.py" --daemon

echo [%date% %time%] INFO: Telegram bot daemon started in separate window >> "%LOGFILE%"

pause
exit /b %ERRORLEVEL%
