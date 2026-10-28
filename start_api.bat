@echo off
call .venv\Scripts\activate.bat
uvicorn app.main:app --reload
pause
