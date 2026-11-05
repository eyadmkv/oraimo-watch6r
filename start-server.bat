@echo off
cd /d "%~dp0"

echo Starting Oraimo Watch 6R Bridge...
echo.

".venv\Scripts\python.exe" -u -m bridge %*

echo.
echo Bridge exited with code %ERRORLEVEL%.
pause