@echo off
REM Start the worker service (execution pool) in this window so you can see logs.
cd /d "%~dp0"
python worker.py
