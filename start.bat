@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Voice-Pilot virtual environment was not found.
    echo Create it with: py -3.12 -m venv .venv
    pause
    exit /b 1
)

if not exist ".env" (
    echo .env was not found. Copy .env.example to .env and add MODEL_API_KEY.
    pause
    exit /b 1
)

echo Starting Voice-Pilot at http://127.0.0.1:8002
echo Keep this window open while the application is running.
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8002

echo.
echo Voice-Pilot stopped.
pause
