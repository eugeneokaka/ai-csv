@echo off
REM Start the API in this window.
cd /d "%~dp0"
uvicorn main:app
