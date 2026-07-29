@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto :workdir_failed

set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "REQUIREMENTS=%PROJECT_DIR%requirements.txt"
set "SCHEDULE_SCRIPT=%PROJECT_DIR%schedule_morning.ps1"
set "ENV_FILE=%PROJECT_DIR%.env"

echo.
echo ========================================
echo   Preopen Recorder - One-click Install
echo ========================================
echo.

if not exist "%REQUIREMENTS%" (
    echo [FAILED] requirements.txt was not found.
    goto :failed
)
if not exist "%SCHEDULE_SCRIPT%" (
    echo [FAILED] schedule_morning.ps1 was not found.
    goto :failed
)
if not exist "%ENV_FILE%" (
    echo [FAILED] The built-in .env file was not found.
    echo Obtain the complete folder from the system provider. Never share .env.
    goto :failed
)

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" -c "import sys" >nul 2>nul && (
        echo [1/4] Existing .venv found. Reusing it.
        goto :venv_ready
    )
    echo [1/4] Existing .venv is unusable. Rebuilding it ...
    call :remove_venv
    if errorlevel 1 (
        echo [FAILED] Unable to remove the unusable .venv.
        goto :failed
    )
) else if exist "%VENV_DIR%\" (
    echo [1/4] Existing .venv is incomplete. Rebuilding it ...
    call :remove_venv
    if errorlevel 1 (
        echo [FAILED] Unable to remove the incomplete .venv.
        goto :failed
    )
)

echo [1/4] Creating the folder-local .venv ...
call :create_venv
if errorlevel 1 (
    echo [FAILED] Unable to create .venv.
    echo Install 64-bit Python 3.12 with Add Python to PATH, then retry.
    goto :failed
)

:venv_ready
echo [2/4] Upgrading pip ...
"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
    echo [FAILED] pip upgrade failed. Check the network and Python installation.
    goto :failed
)

echo [3/4] Installing requirements.txt ...
"%VENV_PYTHON%" -m pip install -r "%REQUIREMENTS%"
if errorlevel 1 (
    echo [FAILED] Dependency installation failed. Review the messages above.
    goto :failed
)

echo [4/4] Registering the 08:25 scheduled recording task ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCHEDULE_SCRIPT%" -Mode Register -Port 8900
if errorlevel 1 (
    echo [FAILED] Python is ready, but scheduled-task registration failed.
    echo Retry this installer and keep the error text above. Never paste .env.
    goto :failed
)

echo Verifying that the scheduled task exists ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCHEDULE_SCRIPT%" -Mode Status -Port 8900 >nul
if errorlevel 1 (
    echo [FAILED] Registration returned success, but the task cannot be queried.
    echo Retry the installer or ask an administrator to inspect Task Scheduler.
    goto :failed
)

echo.
echo [SUCCESS] Environment and the daily 08:25 scheduled task are ready.
echo Next: double-click the Chinese one-click VBS launcher or launch.vbs.
echo The computer must be on or allowed to wake when the task is due.
echo.
call :pause_if_needed
exit /b 0

:create_venv
where py.exe >nul 2>nul
if not errorlevel 1 (
    py.exe -3.12 -m venv "%VENV_DIR%"
    if exist "%VENV_PYTHON%" exit /b 0
    py.exe -3 -m venv "%VENV_DIR%"
    if exist "%VENV_PYTHON%" exit /b 0
)

where python.exe >nul 2>nul
if errorlevel 1 exit /b 1
python.exe -m venv "%VENV_DIR%"
if exist "%VENV_PYTHON%" exit /b 0
exit /b 1

:remove_venv
if not exist "%VENV_DIR%\" exit /b 0
rmdir /s /q "%VENV_DIR%"
if exist "%VENV_DIR%\" exit /b 1
exit /b 0

:workdir_failed
echo [FAILED] Cannot switch to the system folder.
goto :failed

:failed
echo.
echo Installation did not complete. Fix the issue and run it again.
echo The installer is idempotent and safe to rerun.
echo.
call :pause_if_needed
exit /b 1

:pause_if_needed
if defined INSTALL_NO_PAUSE exit /b 0
if defined SCHEDULER_NO_PAUSE exit /b 0
pause
exit /b 0
