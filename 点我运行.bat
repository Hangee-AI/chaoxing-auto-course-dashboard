@echo off
cd /d "%~dp0"

echo ================================================
echo        Chaoxing Learning Assistant (GUI)
echo        Starting, please wait...
echo ================================================

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo  [ERROR] Python not found.
    echo  Please install Python 3.10+ from:
    echo  https://www.python.org/downloads/
    echo  Remember to check "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

python -c "import requests,loguru,bs4,lxml,tqdm,fontTools,urllib3,httpx,chardet,tenacity,pyaes" >nul 2>nul
if errorlevel 1 (
    echo.
    echo  First run: installing dependencies, please wait...
    echo.
    python -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo.
        echo  [ERROR] Failed to install dependencies. Check your network and retry.
        pause
        exit /b 1
    )
)

rem Use pythonw from the same folder as python, so no console window appears
for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)"') do set "PYEXE=%%i"
set "PYW=%PYEXE:python.exe=pythonw.exe%"

if exist "%PYW%" (
    start "" "%PYW%" "%~dp0gui.py"
) else (
    start "" "%PYEXE%" "%~dp0gui.py"
)
exit /b 0
