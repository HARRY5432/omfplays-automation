@echo off
cd /d "%~dp0"
call "F:\contentfarming\omfplays\.venv\Scripts\activate.bat" 2>nul || echo "No venv found, using system python"
python run_local_pipeline.py
if %errorlevel% neq 0 (
    echo Pipeline exited with error %errorlevel%
) else (
    echo Pipeline completed successfully.
)
pause