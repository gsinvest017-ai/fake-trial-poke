@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "LOCAL_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%LOCAL_PYTHON%" (
    "%LOCAL_PYTHON%" "%~dp0scheduler.py" %*
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo 找不到 Python。請先安裝 64 位元 Python 3.12，再執行 setup.bat。
        if not defined SCHEDULER_NO_PAUSE pause
        exit /b 1
    )
    python "%~dp0scheduler.py" %*
)

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo scheduler.py 已結束，exit=%EXIT_CODE%。
    if not defined SCHEDULER_NO_PAUSE pause
)
exit /b %EXIT_CODE%
