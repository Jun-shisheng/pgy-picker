@echo off
rem 必须保持 GBK/ANSI 编码且不要加 chcp 65001，否则 cmd 解析中文行会错位。
setlocal
cd /d "%~dp0"

REM ============================================================
REM  蒲公英达人选号 · 工作区启动（开发/手测用，**不出包**）
REM
REM  做三件事：
REM    1. 停掉占着 18081 的旧后端（避免下一个进程绑不上端口）
REM    2. 源码起后端（uvicorn）+ 起前端（vite），各占一个窗口
REM    3. 自动打开浏览器
REM
REM  改代码后：后端要重启本脚本；前端 Vite 会热更新，不用重启。
REM  关掉那两个黑窗口就等于停服。
REM ============================================================

set "PGY_PORT=18081"
set "WEB_PORT=5173"
set "PGY_DATA_DIR=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ============================================================
echo  蒲公英达人选号 · 工作区启动
echo ============================================================
echo.

REM ---- 0. 环境自检：缺什么直接说清楚，别让用户对着报错猜 ----
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 python。请确认已装 Python 并加入 PATH。
  pause & exit /b 1
)
where npm >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 npm。请确认已装 Node.js 并加入 PATH。
  pause & exit /b 1
)
if not exist "frontend\node_modules" (
  echo [提示] 前端依赖还没装，先跑一次 npm install（在 frontend 目录）…
  pushd frontend
  call npm install --no-audit --no-fund
  popd
)

REM ---- 1. 停掉旧后端 ----
echo [1/4] 检查端口 %PGY_PORT% 是否被旧后端占着…
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr ":%PGY_PORT% " ^| findstr "LISTENING"') do (
  echo       端口被 PID %%P 占着，先结束它（否则新后端绑不上）。
  taskkill /PID %%P /F >nul 2>nul
)
echo       端口已就绪。

REM ---- 2. 起后端（源码，非打包） ----
echo [2/4] 启动本地服务（源码 uvicorn，端口 %PGY_PORT%）…
start "Pgy-后端(18081)" cmd /k "cd /d "%~dp0" && set PGY_DATA_DIR=%~dp0&& set PGY_PORT=%PGY_PORT%&& set PYTHONUTF8=1&& set PYTHONIOENCODING=utf-8&& python -m uvicorn backend.app:app --host 127.0.0.1 --port %PGY_PORT%"

echo       等后端就绪…
powershell -NoProfile -Command "$d=(Get-Date).AddSeconds(60); while((Get-Date) -lt $d){ try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%PGY_PORT%/api/health'; if($r.StatusCode -eq 200){ exit 0 } } catch {}; Start-Sleep -Milliseconds 700 }; exit 1"
if errorlevel 1 (
  echo.
  echo [错误] 后端 60 秒内没起来。看那个「Pgy-后端」窗口里的报错，
  echo        或查日志：%PGY_DATA_DIR%pgy_logs\pgy.log
  pause & exit /b 1
)
echo       后端已就绪：http://127.0.0.1:%PGY_PORT%

REM ---- 3. 起前端（Vite dev） ----
echo [3/4] 启动前端（Vite dev，端口 %WEB_PORT%）…
start "Pgy-前端(5173)" cmd /k "cd /d "%~dp0frontend" && npm run dev"

echo       等前端就绪（首次可能要几秒）…
powershell -NoProfile -Command "$d=(Get-Date).AddSeconds(60); while((Get-Date) -lt $d){ try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://localhost:%WEB_PORT%/'; if($r.StatusCode -eq 200){ exit 0 } } catch {}; Start-Sleep -Milliseconds 700 }; exit 1"
if errorlevel 1 (
  echo.[错误] 前端 60 秒内没起来，看「Pgy-前端」窗口里的报错。
  pause & exit /b 1
)

REM ---- 4. 打开浏览器 ----
echo [4/4] 打开界面…
start "" "http://localhost:%WEB_PORT%/"

echo.
echo ============================================================
echo  已启动。界面：http://localhost:%WEB_PORT%/
echo.
echo  · 这两个黑窗口**别关**，关了就等于停服。
echo  · 改后端代码：关掉「Pgy-后端」窗口，重跑本脚本。
echo  · 改前端代码：Vite 自动热更新，无需重启。
echo  · 浏览器里操作的是**真数据**（会真去蒲公英检索，只读）。
echo ============================================================
echo.
pause
