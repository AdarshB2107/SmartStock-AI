@echo off
setlocal
py -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo Setup complete. Run start_api.bat and start_dashboard.bat in separate terminals.
pause
