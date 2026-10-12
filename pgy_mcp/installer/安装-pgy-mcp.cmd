@echo off
rem Double-click this file to install the Pgy MCP service for WorkBuddy.
rem
rem Keep this file ASCII-only and in the same folder as pgy-mcp.exe.
rem (A .cmd with Chinese *content* gets garbled by cmd.exe's OEM code page;
rem  all Chinese output is printed by the PowerShell script instead.)
setlocal
set "HERE=%~dp0"

if not exist "%HERE%install-pgy-mcp.ps1" (
  echo [ERROR] install-pgy-mcp.ps1 is missing next to this file.
  echo         Keep the installer files together with pgy-mcp.exe.
  echo.
  pause
  exit /b 1
)
if not exist "%HERE%pgy-mcp.exe" (
  echo [ERROR] pgy-mcp.exe is missing next to this file.
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%install-pgy-mcp.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo Done. If anything looks wrong, re-run this file and read the messages above.
) else (
  echo Finished with issues - see the messages above ^(exit code %RC%^).
)
echo.
pause
exit /b %RC%
