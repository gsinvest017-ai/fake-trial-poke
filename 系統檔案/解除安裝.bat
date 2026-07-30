@echo off
chcp 65001 >nul
call "%~dp0uninstall_autostart.bat"
exit /b %ERRORLEVEL%
