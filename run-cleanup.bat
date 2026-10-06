@echo off
REM Evict idle chat cache (working_dir/) once. Schedule hourly with Task Scheduler.
cd /d "%~dp0"
python cleanup.py %*
