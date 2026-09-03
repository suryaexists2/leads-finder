@echo off
cd /d "%~dp0"
echo ============================================
echo   LinkedIn Lead Scheduler - Nonstop
echo   Finds 5+ fresh leads per hour, sends to Telegram
echo   Close this window to stop the scheduler
echo ============================================
python scheduler_worker.py
pause
