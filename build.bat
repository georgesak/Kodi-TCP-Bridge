@echo off
REM Double-clickable wrapper around build.ps1.
REM Bypasses the execution policy for this one invocation only; it does not
REM change any machine or user policy setting.

setlocal
set "SCRIPT_DIR=%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%build.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo Build FAILED with exit code %EXITCODE%.
)

echo.
pause
exit /b %EXITCODE%
