@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo [1/3] 建立資料夾內虛擬環境 .venv ...
    set "VENV_CREATED="
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3.12 -m venv "%VENV_DIR%"
        if not errorlevel 1 set "VENV_CREATED=1"
    )
    if not defined VENV_CREATED (
        where python >nul 2>nul
        if errorlevel 1 (
            echo 找不到 Python。請先安裝 64 位元 Python 3.12。
            if not defined SCHEDULER_NO_PAUSE pause
            exit /b 1
        )
        python -m venv "%VENV_DIR%"
        if not errorlevel 1 set "VENV_CREATED=1"
    )
    if not defined VENV_CREATED (
        echo 建立 .venv 失敗。
        if not defined SCHEDULER_NO_PAUSE pause
        exit /b 1
    )
) else (
    echo [1/3] 已有 .venv，保留現有環境。
)

echo [2/3] 安裝 requirements.txt ...
"%VENV_PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo 安裝相依套件失敗，請確認網路與 Python 版本。
    if not defined SCHEDULER_NO_PAUSE pause
    exit /b 1
)

echo [3/3] 準備金鑰設定檔 ...
if not exist "%~dp0.env" (
    copy /Y "%~dp0.env.example" "%~dp0.env" >nul
    echo 已建立 .env。請用文字編輯器填入接手人自己的 Shioaji API 金鑰。
) else (
    echo 已有 .env，未覆寫。
)

echo.
echo 初始化完成。填妥 .env 後，請先執行：
echo   .venv\Scripts\python.exe scheduler.py --once
echo 驗證成功後可雙擊 start.bat 啟動常駐排程。
if not defined SCHEDULER_NO_PAUSE pause
exit /b 0
