@echo off
REM MT5 node client launcher (Windows)
setlocal
cd /d %~dp0

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

echo Installing dependencies...
pip install -r requirements.txt

echo Starting node client...
python node_client.py

endlocal
