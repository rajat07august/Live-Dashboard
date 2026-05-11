@echo off
echo Installing dependencies...
pip install -r requirements.txt
echo.
echo Starting Live Dashboard...
echo Open http://127.0.0.1:8000 in your browser
echo.
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
pause
