<#
  PgyPicker 客户端一键出包：Python sidecar（PyInstaller）+ Tauri 安装包（NSIS）。

  产物：
    安装包  release\PgyPicker_<版本>_x64-setup.exe
    内置服务 frontend\src-tauri\binaries\pgy-backend-x86_64-pc-windows-msvc.exe

  为什么脚本这么啰嗦：
  - 每一步都显式失败：出包脚本最怕「静默产出一个旧包」，因此任何一步失败立即终止；
  - 打包前把 PATH 收敛到当前 Python 环境：PATH 里往往混有 Git(MSYS2)、MySQL 等自带
    的 OpenSSL DLL，PyInstaller 会按 PATH 解析依赖并打进不兼容的版本，真机表现就是
    「本机编译正常、其它电脑上 _ssl 加载失败、客户端打不开」；
  - 打包后立刻冒烟自检：用临时数据目录 + 测试端口启动内置服务并探活，
    不通就带着启动错误日志直接失败——绝不让一个跑不起来的服务进入安装包。
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$tauri = Join-Path $root "frontend\src-tauri"
$binaries = Join-Path $tauri "binaries"
$sidecar = Join-Path $binaries "pgy-backend-x86_64-pc-windows-msvc.exe"
$buildDir = Join-Path $root "build"
$releaseDir = Join-Path $root "release"
# 冒烟端口必须与主端口（18081）隔离，否则「200 通过」可能是机器上正在跑的那个
# 旧服务给的＝假通过。build\build-sidecar.ps1 用 18082，这里用 18083。
$smokePort = 18083

# 中文路径与中文日志在 Windows 默认 GBK 下容易写坏，统一强制 UTF-8。
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Assert-Command($name, $hint) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    throw "缺少命令 $name：$hint"
  }
}

Assert-Command python "请安装 Python 3.11+ 并加入 PATH（安装时勾选 Add python.exe to PATH）"
Assert-Command npm "请安装 Node.js 20+（https://nodejs.org）"

# ---- 版本号一致性校验：安装包名 / 应用列表 / 后端自报版本必须同一口径 ----
# 历史上这里漂过：安装包一直叫 0.1.0（脚手架初值），后端却已自报 0.3.0，
# 用户看到「服务版本 0.3.0」和「PgyPicker_0.1.0_x64-setup.exe」对不上，无从判断测试的是哪版。
$appVersion = ([regex]'"version"\s*:\s*"([^"]+)"').Match((Get-Content -LiteralPath (Join-Path $tauri 'tauri.conf.json') -Raw)).Groups[1].Value
$cargoVersion = ([regex]'(?m)^version\s*=\s*"([^"]+)"').Match((Get-Content -LiteralPath (Join-Path $tauri 'Cargo.toml') -Raw)).Groups[1].Value
$packageVersion = ([regex]'"version"\s*:\s*"([^"]+)"').Match((Get-Content -LiteralPath (Join-Path $root 'frontend\package.json') -Raw)).Groups[1].Value
$backendVersion = ([regex]'FastAPI\(title=[^,]+,\s*version="([^"]+)"').Match((Get-Content -LiteralPath (Join-Path $root 'backend\app.py') -Raw)).Groups[1].Value
Write-Host ("版本号：安装包={0} Cargo={1} package.json={2} 后端={3}" -f $appVersion, $cargoVersion, $packageVersion, $backendVersion)
if (-not $appVersion -or $appVersion -ne $cargoVersion -or $appVersion -ne $packageVersion -or $appVersion -ne $backendVersion) {
  throw "版本号不一致：tauri.conf.json=$appVersion / Cargo.toml=$cargoVersion / package.json=$packageVersion / backend\app.py=$backendVersion。请先统一版本号再出包。"
}


New-Item -ItemType Directory -Force -Path $binaries | Out-Null
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null

Write-Host "[1/4] 安装后端依赖…" -ForegroundColor Cyan
python -m pip install --upgrade pip | Out-Null
python -m pip install -r (Join-Path $root "backend\requirements.txt") pyinstaller
if ($LASTEXITCODE -ne 0) { throw "后端依赖安装失败" }

Write-Host "[2/4] 打包内置服务（PyInstaller sidecar）…" -ForegroundColor Cyan
# 把 PATH 收敛到「当前解释器自身 + 系统目录」：避免解析到 Git/MSYS2、MySQL 自带的
# 同名 OpenSSL/其他运行库，保证打进包里的依赖与解释器是同源的。
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

# 参数写成数组再展开：避免反引号续行在 Windows PowerShell 下被中文编码问题吃掉。
# --onefile：Tauri 的 externalBin 只接收单个可执行文件；
# --noconsole：后台服务不能让用户看到黑框，因此所有日志必须落到文件（见 pgy_logging）。
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
  "--collect-all", "openpyxl"
)
# 报价档位默认值：若仓库根有 pricing.local.json（gitignore，可含真实价格），
# 复制一份随 sidecar 打进安装包。装机后第一次启动用它初始化 <数据目录>/pricing.json。
$pricingDefault = Join-Path $root "backend\providers\pugongying\pricing.default.json"
$pricingSource = Join-Path $root "pricing.local.json"
if (Test-Path -LiteralPath $pricingSource) {
  Copy-Item -LiteralPath $pricingSource -Destination $pricingDefault -Force
}
if (Test-Path -LiteralPath $pricingDefault) {
  $pyinstallerArgs += @("--add-data", "$pricingDefault;backend\providers\pugongying")
}
$pyinstallerArgs += (Join-Path $root "backend_sidecar.py")
try {
  & python @pyinstallerArgs
  $pyinstallerExit = $LASTEXITCODE
} finally {
  # npm / node 在后面还要用，必须把 PATH 恢复成原样。
  $env:PATH = $previousPath
}
if ($pyinstallerExit -ne 0) { throw "内置服务打包失败（详见 $buildDir\pyinstaller\*.log）" }
if (-not (Test-Path -LiteralPath $sidecar)) { throw "后端 sidecar 构建失败：$sidecar" }
Write-Host ("      内置服务：{0} MB" -f [math]::Round((Get-Item -LiteralPath $sidecar).Length / 1MB, 1))

Write-Host "[3/4] 冒烟自检：用临时数据目录启动内置服务并探活…" -ForegroundColor Cyan
$smokeDir = Join-Path $buildDir "smoke"
Remove-Item -LiteralPath $smokeDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $smokeDir | Out-Null
$previousDataDir = $env:PGY_DATA_DIR
$previousPort = $env:PGY_PORT
$env:PGY_DATA_DIR = $smokeDir
$env:PGY_PORT = "$smokePort"
$smoke = Start-Process -FilePath $sidecar -PassThru -WindowStyle Hidden
$healthy = $false
try {
  for ($attempt = 0; $attempt -lt 90; $attempt++) {
    Start-Sleep -Milliseconds 1000
    try {
      $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$smokePort/api/health"
      if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
  }
  if (-not $healthy) {
    $errorLog = Join-Path $smokeDir "pgy_logs\pgy-startup-error.log"
    $detail = if (Test-Path $errorLog) { Get-Content -LiteralPath $errorLog -Raw } else { "（没有启动错误日志，可能进程未启动）" }
    throw "内置服务冒烟自检失败，未通过探活：`n$detail"
  }
  Write-Host "      探活成功：http://127.0.0.1:$smokePort/api/health" -ForegroundColor Green
} finally {
  if ($smoke -and -not $smoke.HasExited) {
    & taskkill /PID $smoke.Id /T /F 2>$null | Out-Null
  }
  $env:PGY_DATA_DIR = $previousDataDir
  $env:PGY_PORT = $previousPort
}

# MSVC 环境（vcvars64）：**必须显式进**，不能靠 PATH 里本来就有的 Visual Studio 变量。
# 真机事故（2026-09-24）：从 Git Bash 之类的环境出包时，PATH 里 Git/MSYS2 的
# `usr\bin` 排在前面，rustc 会把 Git 自带的 **GNU link.exe** 当成链接器 →
# `link: missing operand after '\xff\xfe'`，Tauri 构建整段失败；就算侥幸选对了
# link.exe，缺 LIB 路径也会 `LNK1181: 无法打开输入文件"kernel32.lib"`。
# 进一次 vcvars64 就把 PATH/LIB/INCLUDE 全部摆正——PyInstaller 那步已经做了同类的
# PATH 收敛（防 OpenSSL 混源），Tauri 这步同理。
function Resolve-VcVars {
  $candidates = @()
  $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
  if (Test-Path -LiteralPath $vswhere) {
    $installed = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
    $candidates += $installed
  }
  $candidates += @(
    (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\2022\BuildTools"),
    (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\2022\Community"),
    (Join-Path $env:ProgramFiles "Microsoft Visual Studio\2022\BuildTools"),
    (Join-Path $env:ProgramFiles "Microsoft Visual Studio\2022\Community")
  )
  foreach ($item in $candidates) {
    if (-not $item) { continue }
    $bat = Join-Path $item "VC\Auxiliary\Build\vcvars64.bat"
    if (Test-Path -LiteralPath $bat) { return $bat }
  }
  return $null
}

Write-Host "[4/4] 构建桌面客户端与安装包（Tauri）…" -ForegroundColor Cyan
$vcvars = Resolve-VcVars
if (-not $vcvars) {
  throw "找不到 MSVC 环境（VC\Auxiliary\Build\vcvars64.bat）：请安装 Visual Studio 2022 生成工具，勾选「使用 C++ 的桌面开发」。"
}
Write-Host ("      使用 MSVC 环境：{0}" -f $vcvars)
Push-Location (Join-Path $root "frontend")
try {
  npm ci
  if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败" }
  # 在 vcvars64 环境里跑 tauri build：PATH 会被摆成 MSVC/SDK 在前，rustc 才会选对链接器。
  & cmd /c "call `"$vcvars`" >nul && npm run tauri build"
  if ($LASTEXITCODE -ne 0) { throw "Tauri 构建失败" }
} finally {
  Pop-Location
}

# 把安装包复制到 release\：出包产物只有一个固定位置，避免每次都要去 target 目录里翻。
$installer = Get-ChildItem -Path (Join-Path $tauri "target\release\bundle\nsis") -Filter "*-setup.exe" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($null -eq $installer) {
  throw "没有找到生成的安装包（frontend\src-tauri\target\release\bundle\nsis）"
}
Copy-Item -LiteralPath $installer.FullName -Destination $releaseDir -Force
$target = Join-Path $releaseDir $installer.Name
# 按版本保留历史安装包，便于复测和回退；同版本重编译仍由上面的
# Copy-Item -Force 精确覆盖，不触碰 release\ 中其他版本。
Write-Host ""
Write-Host "完成：$target" -ForegroundColor Green
Write-Host ("安装包大小：{0} MB" -f [math]::Round((Get-Item -LiteralPath $target).Length / 1MB, 1))
Write-Host "用户机器上的运行日志：%LOCALAPPDATA%\Pgy\pgy_logs\pgy.log"
