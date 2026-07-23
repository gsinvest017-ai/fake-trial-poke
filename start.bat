@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "LOCAL_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%LOCAL_PYTHON%" (
    set "PYTHON_EXE=%LOCAL_PYTHON%"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo 找不到 Python。請先安裝 64 位元 Python 3.12，再執行 setup.bat。
        if not defined SCHEDULER_NO_PAUSE pause
        exit /b 1
    )
    set "PYTHON_EXE=python"
)

set "PYTHONUTF8=1"
echo 正在啟動即時偵測器常駐服務...
echo 請用瀏覽器開啟 http://127.0.0.1:8900/
echo.
"%PYTHON_EXE%" "%~dp0service.py"

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo service.py 已結束，exit=%EXIT_CODE%。
    if not defined SCHEDULER_NO_PAUSE pause
)
exit /b %EXIT_CODE%
