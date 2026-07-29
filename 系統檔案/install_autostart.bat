@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "START_SCRIPT=%~dp0start.bat"
set "SHORTCUT_NAME=Shioaji Preopen Scheduler.lnk"

if not exist "%START_SCRIPT%" (
    echo 找不到 start.bat：%START_SCRIPT%
    if not defined SCHEDULER_NO_PAUSE pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $startup = [Environment]::GetFolderPath('Startup'); $shortcutPath = Join-Path $startup $env:SHORTCUT_NAME; $target = [IO.Path]::GetFullPath($env:START_SCRIPT); $shell = New-Object -ComObject WScript.Shell; $shortcut = $shell.CreateShortcut($shortcutPath); $shortcut.TargetPath = $target; $shortcut.WorkingDirectory = Split-Path -Parent $target; $shortcut.Description = 'Folder-resident Shioaji preopen scheduler'; $shortcut.Save()"
if errorlevel 1 (
    echo 建立開機自動啟動捷徑失敗。
    if not defined SCHEDULER_NO_PAUSE pause
    exit /b 1
)

echo 已在目前使用者的「啟動」資料夾建立捷徑。
echo 捷徑會指回本資料夾內的 start.bat，不需要系統管理員權限。
echo 若搬動專案資料夾，請在新位置重新執行本檔。
if not defined SCHEDULER_NO_PAUSE pause
exit /b 0
