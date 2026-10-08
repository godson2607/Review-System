@echo off
cd /d "%~dp0"
if not exist venv (
    echo Setting up for the first time, please wait...
    python -m venv venv
    venv\Scripts\python -m pip install -q -r requirements.txt
)
start "" http://127.0.0.1:5000
venv\Scripts\python app.py
pause
