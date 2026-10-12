# 只重打内置服务 sidecar（不出安装包）：开发态验证后端改动用。
# 与 build-installer.ps1 的 [1/4][2/4] 两步保持一致（含 PATH 收敛与冒烟探活），
# 少了它，run-test.bat 的开发客户端启动的仍是上一次打出来的旧后端。
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$tauri = Join-Path $root "frontend\src-tauri"
$binaries = Join-Path $tauri "binaries"
$sidecar = Join-Path $binaries "pgy-backend-x86_64-pc-windows-msvc.exe"
$buildDir = Join-Path $root "build"

New-Item -ItemType Directory -Force -Path $binaries | Out-Null
$previousSidecar = $null
if (Test-Path -LiteralPath $sidecar) {
  $previousSidecar = Join-Path $buildDir ("sidecar-prev-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".exe")
  Copy-Item -LiteralPath $sidecar -Destination $previousSidecar -Force
  Write-Host ("旧 sidecar 已备份：{0}" -f $previousSidecar)
}

# 与出包脚本同样的 PATH 收敛：避免把 Git/MSYS2 自带的 OpenSSL 打进包
# （真机事故：与解释器不同源的 libcrypto 会让 sidecar 启动即 ImportError）。
$pythonDir = (python -c "import sys, os; print(os.path.dirname(sys.executable))").Trim()
$previousPath = $env:PATH
$env:PATH = @(
  $pythonDir,
  (Join-Path $pythonDir "DLLs"),
  (Join-Path $pythonDir "Library\bin"),
  (Join-Path $pythonDir "Scripts"),
  (Join-Path $env:SystemRoot "system32"),
  $env:SystemRoot
) -join ";"

$pyinstallerArgs = @(
  "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--noconsole",
  "--name", "pgy-backend-x86_64-pc-windows-msvc",
  "--distpath", $binaries,
  "--workpath", (Join-Path $buildDir "pyinstaller"),
  "--specpath", $buildDir,
  "--log-level", "WARN",
  "--collect-all", "playwright",
  # backend/app.py 里的选号相关模块是**函数内惰性导入**的，PyInstaller 的静态
  # 分析扫不到它们；不显式收进来，打出的 exe 一调选号就 ModuleNotFoundError。
  "--collect-submodules", "backend",
  "--collect-all", "openpyxl",
  (Join-Path $root "backend_sidecar.py")
)
try {
  Write-Host "[1/2] 打包内置服务（PyInstaller sidecar）…" -ForegroundColor Cyan
  & python @pyinstallerArgs
  $pyinstallerExit = $LASTEXITCODE
} finally {
  $env:PATH = $previousPath
}
if ($pyinstallerExit -ne 0) { throw "内置服务打包失败（详见 $buildDir\pyinstaller\*.log）" }
if (-not (Test-Path -LiteralPath $sidecar)) { throw "sidecar 构建失败：$sidecar" }
Write-Host ("      内置服务：{0} MB" -f [math]::Round((Get-Item -LiteralPath $sidecar).Length / 1MB, 1))

Write-Host "[2/2] 冒烟自检：临时数据目录启动并探活…" -ForegroundColor Cyan
$smokeDir = Join-Path $buildDir "smoke"
$smokePort = 18082                     # 与装机版/dev 用的 18081 隔离：否则旧服务在跑时
Remove-Item -LiteralPath $smokeDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $smokeDir | Out-Null
$env:PGY_DATA_DIR = $smokeDir
$env:PGY_PORT = "$smokePort"          # 探活的 200 可能是旧服务给的＝假通过（自检 2026-09-28）
$smoke = Start-Process -FilePath $sidecar -PassThru -WindowStyle Hidden
try {
  $deadline = (Get-Date).AddSeconds(90)
  $ok = $false
  while ((Get-Date) -lt $deadline) {
    try {
      $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$smokePort/api/health"
      if ($response.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 800
  }
  if (-not $ok) {
    $hint = ""
    $errFile = Join-Path $smokeDir "pgy_logs\pgy-startup-error.log"
    if (Test-Path -LiteralPath $errFile) { $hint = "；启动错误：" + (Get-Content -LiteralPath $errFile -Tail 3 | Out-String).Trim() }
    throw "sidecar 冒烟探活失败（90 秒未就绪）$hint"
  }
  Write-Host "      /api/health 200，sidecar 可用" -ForegroundColor Green
} finally {
  Remove-Item Env:\PGY_DATA_DIR -ErrorAction SilentlyContinue
  Remove-Item Env:\PGY_PORT -ErrorAction SilentlyContinue
  if ($smoke -and -not $smoke.HasExited) { taskkill /PID $smoke.Id /T /F | Out-Null }
}

Write-Host ("完成：{0}" -f (Get-Date -Format "HH:mm:ss")) -ForegroundColor Green
Write-Host "现在可以用 run-test.bat -> [1] 启动开发客户端验证后端改动了。"
