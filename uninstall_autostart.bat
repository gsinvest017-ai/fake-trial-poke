@echo off
chcp 65001 >nul
setlocal

set "SHORTCUT_NAME=Shioaji Preopen Scheduler.lnk"

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $startup = [Environment]::GetFolderPath('Startup'); $shortcutPath = Join-Path $startup $env:SHORTCUT_NAME; if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath -Force }"
if errorlevel 1 (
    echo 移除開機自動啟動捷徑失敗。
    if not defined SCHEDULER_NO_PAUSE pause
    exit /b 1
)

echo 已移除本系統的開機自動啟動捷徑（若原先存在）。
echo 專案資料夾與其中資料均未刪除。
if not defined SCHEDULER_NO_PAUSE pause
exit /b 0
