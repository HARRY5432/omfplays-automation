@echo off
rem ─────────────────────────────────────────────────────────────────────────
rem  Install / inspect / remove the Windows scheduled task that runs the
rem  omfplays pipeline at logon and hourly through the day (07:05-19:05).
rem
rem    install_autostart.bat            install (or refresh)
rem    install_autostart.bat -Status    show current state
rem    install_autostart.bat -Remove    uninstall
rem ─────────────────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" %*
echo.
pause
