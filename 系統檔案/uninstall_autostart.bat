@echo off
chcp 65001 >nul
setlocal

set "SHORTCUT_NAME=Shioaji Preopen Scheduler.lnk"
set "TASK_NAME=假試撮盤前監控"
set "UNINSTALL_FAILED=0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $startup = [Environment]::GetFolderPath('Startup'); $shortcutPath = Join-Path $startup $env:SHORTCUT_NAME; if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath -Force }"
if errorlevel 1 (
    echo 移除開機自動啟動捷徑失敗。
    set "UNINSTALL_FAILED=1"
) else (
    echo 已移除本系統的開機自動啟動捷徑（若原先存在）。
)

rem schedule_morning.ps1 的舊版在任務不存在時會回傳非零；
rem 此處再次查詢，讓解除安裝保持冪等且不把「原本就不存在」視為錯誤。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0schedule_morning.ps1" -Mode Unregister >nul 2>&1
if errorlevel 1 (
    schtasks.exe /Query /TN "%TASK_NAME%" >nul 2>&1
    if errorlevel 1 (
        echo 每日排程原本即不存在，無需移除。
    ) else (
        echo 移除每日排程失敗，任務仍存在：%TASK_NAME%
        set "UNINSTALL_FAILED=1"
    )
) else (
    echo 每日排程已移除。
)

if "%UNINSTALL_FAILED%"=="1" (
    echo 解除安裝未完全成功，請依上方訊息處理。
    if not defined SCHEDULER_NO_PAUSE pause
    exit /b 1
)

echo 每日排程已移除、機器將不再自動登入券商。
echo 專案資料夾與其中資料均未刪除。
if not defined SCHEDULER_NO_PAUSE pause
exit /b 0
