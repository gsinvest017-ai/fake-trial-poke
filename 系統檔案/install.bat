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
set "LOGIN_SMOKE=%PROJECT_DIR%scripts\verify_login.py"
set "PYTHON_BOOTSTRAP=%PROJECT_DIR%scripts\ensure_python.ps1"
set "PYTHON_PATH_FILE=%TEMP%\preopen_python_%RANDOM%_%RANDOM%.txt"
set "INSTALL_MARKER=%PROJECT_DIR%.install_complete"
set "PYTHON312_EXE="

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
if not exist "%LOGIN_SMOKE%" (
    echo [FAILED] scripts\verify_login.py was not found.
    goto :failed
)
if not exist "%PYTHON_BOOTSTRAP%" (
    echo [FAILED] scripts\ensure_python.ps1 was not found.
    goto :failed
)

echo [1/5] Locating 64-bit Python 3.12 ...
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ^
    -File "%PYTHON_BOOTSTRAP%" -OutputFile "%PYTHON_PATH_FILE%"
if errorlevel 1 (
    echo [FAILED] Automatic Python setup did not succeed.
    echo Please install Python 3.12 first, then run this installer again.
    goto :failed
)
if not exist "%PYTHON_PATH_FILE%" (
    echo [FAILED] Python setup did not return an executable path.
    echo Please install Python 3.12 first, then run this installer again.
    goto :failed
)
set /p "PYTHON312_EXE="<"%PYTHON_PATH_FILE%"
del /q "%PYTHON_PATH_FILE%" >nul 2>nul

if not defined PYTHON312_EXE (
    echo [FAILED] Python setup returned an empty executable path.
    goto :failed
)
if not exist "%PYTHON312_EXE%" (
    echo [FAILED] The detected Python executable does not exist.
    goto :failed
)
"%PYTHON312_EXE%" -c "import sys,struct; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P') * 8 == 64 else 1)" >nul 2>nul
if errorlevel 1 (
    echo [FAILED] The detected interpreter is not 64-bit Python 3.12.
    goto :failed
)
echo [1/5] Python 3.12 ready: "%PYTHON312_EXE%"

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" -c "import sys,struct; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P') * 8 == 64 else 1)" >nul 2>nul && (
        echo [1/5] Existing Python 3.12 .venv found. Reusing it.
        goto :venv_ready
    )
    if "%INSTALL_DRY_RUN%"=="1" (
        echo [FAILED] Existing .venv is not a usable 64-bit Python 3.12 environment.
        echo [DRY RUN] No files were changed.
        goto :failed
    )
    echo [1/5] Existing .venv is unusable. Rebuilding it ...
    call :remove_venv
    if errorlevel 1 (
        echo [FAILED] Unable to remove the unusable .venv.
        goto :failed
    )
) else if exist "%VENV_DIR%\" (
    if "%INSTALL_DRY_RUN%"=="1" (
        echo [FAILED] Existing .venv is incomplete.
        echo [DRY RUN] No files were changed.
        goto :failed
    )
    echo [1/5] Existing .venv is incomplete. Rebuilding it ...
    call :remove_venv
    if errorlevel 1 (
        echo [FAILED] Unable to remove the incomplete .venv.
        goto :failed
    )
)

if "%INSTALL_DRY_RUN%"=="1" (
    echo [FAILED] INSTALL_DRY_RUN requires an existing usable Python 3.12 .venv.
    goto :failed
)

echo [1/5] Creating the folder-local .venv ...
call :create_venv
if errorlevel 1 (
    echo [FAILED] Unable to create .venv.
    echo The detected Python path was "%PYTHON312_EXE%".
    goto :failed
)

:venv_ready
if "%INSTALL_DRY_RUN%"=="1" (
    echo [DRY RUN] Python installation was not needed.
    echo [DRY RUN] Existing .venv was validated and reused.
    echo [DRY RUN] Skipped pip, dependencies, login, and scheduled tasks.
    goto :success
)

echo [2/5] Upgrading pip ...
"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
    echo [FAILED] pip upgrade failed. Check the network and Python installation.
    goto :failed
)

echo [3/5] Installing requirements.txt ...
"%VENV_PYTHON%" -m pip install -r "%REQUIREMENTS%"
if errorlevel 1 (
    echo [FAILED] Dependency installation failed. Review the messages above.
    goto :failed
)

echo [4/5] Verifying Shioaji credentials ...
"%VENV_PYTHON%" "%LOGIN_SMOKE%"
if errorlevel 1 (
    echo [FAILED] Credential verification failed. Review .env without sharing it.
    goto :failed
)

echo [5/5] Registering the 08:25 scheduled recording task ...
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

:success
if not "%INSTALL_DRY_RUN%"=="1" (
    >"%INSTALL_MARKER%" echo installed
)
echo.
if "%INSTALL_DRY_RUN%"=="1" (
    echo [SUCCESS] Dry-run validation completed without system changes.
) else (
    echo [SUCCESS] Environment and the daily 08:25 scheduled task are ready.
    echo Next: double-click the Chinese one-click VBS launcher or launch.vbs.
    echo The computer must be on or allowed to wake when the task is due.
)
echo.
call :pause_if_needed
exit /b 0

:create_venv
"%PYTHON312_EXE%" -m venv "%VENV_DIR%"
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
if exist "%PYTHON_PATH_FILE%" del /q "%PYTHON_PATH_FILE%" >nul 2>nul
echo.
echo Installation did not complete. Fix the issue and run it again.
echo The installer is idempotent and safe to rerun.
echo.
call :pause_if_needed
exit /b 1

:pause_if_needed
if "%INSTALL_DRY_RUN%"=="1" exit /b 0
if defined INSTALL_NO_PAUSE exit /b 0
if defined SCHEDULER_NO_PAUSE exit /b 0
pause
exit /b 0
