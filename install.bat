@echo off
echo Installing Amazon Tracker dependencies...
pip install -r requirements.txt
playwright install chromium
echo.
echo Installation complete! Run start.bat to launch the app.
pause
