@echo off
cd /d "%~dp0"

rem 开发态数据目录 = 项目根（pgy.db / profiles / pgy_logs 都在项目里）。
rem 不设这一行的话，壳会退回 %LOCALAPPDATA%\Pgy，和安装版共用同一个
rem 数据目录与浏览器 Profile——日志会跑偏，两边还会抢同一个 Profile。
if not defined PGY_DATA_DIR set "PGY_DATA_DIR=%~dp0"
REM 若自动登录打不开网站、但默认浏览器能打开，取消下面一行注释并改成你的代理地址：
REM set "PGY_BROWSER_PROXY=http://127.0.0.1:7890"
REM 补主页数据节奏（秒）。默认 0/0 = 直接下一个，不等待；若要放慢可调大：

REM set "PGY_ENRICH_INTERVAL=0"

REM set "PGY_ENRICH_JITTER=0"

REM set "PGY_ENRICH_PAUSE_EVERY=0"

REM set "PGY_ENRICH_LONG_PAUSE_EVERY=0"

REM set "PGY_ENRICH_WARMUP_MIN=0"

REM set "PGY_NOTES_CACHE_DAYS=3"

REM set "PGY_NOTES_WAIT=20"



title Pgy 客户端人工测试

rem 保持 GBK/ANSI 编码，不要加 chcp 65001（否则 cmd 解析中文行会错位）。

set "SIDECAR=frontend\src-tauri\binaries\pgy-backend-x86_64-pc-windows-msvc.exe"
if not exist "%SIDECAR%" (
  echo 找不到后端 sidecar：%SIDECAR%
  echo 请先运行 build-installer.ps1 出一次包。
  pause
  exit /b 1
)

rem 18081 被占用时客户端壳会直接复用那个旧服务，测到的就不是当前代码。
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr ":18081 " ^| findstr "LISTENING"') do (
  echo 端口 18081 已被 PID %%P 占用（安装版或上一轮留下的旧进程）。
  echo 客户端会直接复用它，你测到的就不是当前代码。请先关掉那个 Pgy 再双击本脚本。
  pause
  exit /b 1
)

rem 5173 若被上一轮的 Vite 占着，Tauri 的 beforeDevCommand 会起不来；先收掉。

for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr ":5173 " ^| findstr "LISTENING"') do (

  taskkill /PID %%P /F >nul 2>nul

)

echo 正在启动**客户端**（不是网页），首次编译要几分钟，编译完会自动弹出窗口…
rem —— MSVC 环境（vcvars64）：cargo 重编译要用 link.exe；普通终端/双击启动时它不在
rem    PATH 里（真机 2026-09-28：改版本号触发前端 crate 重编译 → linker `link.exe`
rem    not found）。跟出包脚本 build-installer.ps1 同一套规则：先问 VS 的 vswhere，
rem    再试常见安装路径；都找不到就跳过（不阻断启动）。
set "PF86=%ProgramFiles(x86)%"
set "VSDIR="
if exist "%PF86%\Microsoft Visual Studio\Installer\vswhere.exe" "%PF86%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath > "%TEMP%\pgy-vsdir.txt" 2>nul
if exist "%TEMP%\pgy-vsdir.txt" set /p VSDIR=<"%TEMP%\pgy-vsdir.txt"
del "%TEMP%\pgy-vsdir.txt" 2>nul
set "VCVARS="
if defined VSDIR if exist "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%VSDIR%\VC\Auxiliary\Build\vcvars64.bat"
if not defined VCVARS if exist "%PF86%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%PF86%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if not defined VCVARS if exist "%PF86%\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%PF86%\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
if not defined VCVARS if exist "%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if not defined VCVARS if exist "%ProgramFiles%\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%ProgramFiles%\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
if defined VCVARS call "%VCVARS%" >nul
if defined VCVARS echo     MSVC 环境已载入：%VCVARS%
pushd frontend
call npm run tauri dev
popd
