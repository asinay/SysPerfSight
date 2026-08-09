@echo off
echo Stopping any running uvicorn processes...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq uvicorn*" >nul 2>&1
for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":8002 " ^| findstr "LISTENING"') do (
    echo Killing PID %%p on port 8002
    taskkill /F /PID %%p >nul 2>&1
)
echo Starting pButtons Parser on http://127.0.0.1:8002
cd /d %~dp0
call venv\Scripts\activate.bat
start "pbuttons-parser" venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8002
echo Done. App starting at http://127.0.0.1:8002
