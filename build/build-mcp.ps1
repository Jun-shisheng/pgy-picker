# 打包 Pgy MCP 服务（WorkBuddy 连接器）为独立 exe。
#
# 与 build-sidecar.ps1 同一套路（PATH 收敛 + 冒烟自检），但产物更小：
# pgy_mcp 刻意零第三方依赖，所以不需要 --collect-all，打包只要几秒。
#
# 产物：
#   release\pgy-mcp.exe          —— 服务本体（和安装包放同一个目录，一个目录拿全）
#   release\安装-pgy-mcp.cmd      —— 同事双击这一个：装 exe + 写好 WorkBuddy 配置
#   release\install-pgy-mcp.ps1   —— 安装脚本本体（被上面那个 cmd 调用）
#   release\mcp.json              —— **手动配置用的模板**（路径含占位符，照抄是错的）
#   frontend\src-tauri\binaries\pgy-mcp-x86_64-pc-windows-msvc.exe
#       —— 带 target triple，是 Tauri `externalBin` 要的命名；将来随安装包一起发。
#
# 为什么配置要由安装脚本生成、而不是直接发一份 mcp.json（用户 2026-10-09 提出）：
# MCP 的 command 必须是**绝对路径**，而绝对路径含用户名、盘符、以及每个人把文件
# 放在哪。发一份写死路径的 json，在别人机器上一定是错的，而且 WorkBuddy 只会显示
# 「连接失败」，完全不提示是路径问题。
#
# exe 的落地位置：%LOCALAPPDATA%\PgyMCP\（用户目录，**不需要管理员**；C:\PgyMCP
# 那种位置普通用户建不了）。刻意避开 %LOCALAPPDATA%\Pgy —— 那是 Pgy 的安装目录
# 兼数据目录，installer-hooks.nsh 每次装包/升级都会 RMDir /r 整个删掉。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File build\build-mcp.ps1
#   # 用指定的解释器（例如临时 venv 里装了 PyInstaller）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File build\build-mcp.ps1 -Python "D:\venv\Scripts\python.exe"
[CmdletBinding()]
param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$tauri = Join-Path $root "frontend\src-tauri"
$binaries = Join-Path $tauri "binaries"
$buildDir = Join-Path $root "build"
$releaseDir = Join-Path $root "release"
$target = "pgy-mcp-x86_64-pc-windows-msvc"
$bundled = Join-Path $binaries ("{0}.exe" -f $target)
$handout = Join-Path $releaseDir "pgy-mcp.exe"
$config = Join-Path $releaseDir "mcp.json"
# 交付的 json 只是**模板**：MCP 的 command 必须是绝对路径，而绝对路径含用户名
# （%USERPROFILE%），所以在别人机器上不可能写对。真正能用的一份由
# 「安装-pgy-mcp.cmd」在本机生成。这里保留模板，只服务「不想跑脚本、想自己配」的人。
# 占位符照抄即错是有意的——它比一个看起来能用的错路径更难被误用。
$templateCommand = 'C:\Users\<用户名>\AppData\Local\PgyMCP\pgy-mcp.exe'
$installTarget = '%LOCALAPPDATA%\PgyMCP\pgy-mcp.exe'

New-Item -ItemType Directory -Force -Path $binaries, $releaseDir | Out-Null

# 与出包脚本同样的 PATH 收敛：避免把 Git/MSYS2 自带的同名 DLL 打进包
# （真机事故：与解释器不同源的 libcrypto 会让产物启动即 ImportError）。
$pythonDir = (& $Python -c "import sys, os; print(os.path.dirname(sys.executable))").Trim()
$previousPath = $env:PATH
$env:PATH = @(
  $pythonDir,
  (Join-Path $pythonDir "DLLs"),
  (Join-Path $pythonDir "Library\bin"),
  (Join-Path $pythonDir "Scripts"),
  (Join-Path $env:SystemRoot "system32"),
  $env:SystemRoot
) -join ";"

# 注意 **没有 --noconsole**：这是 stdio 协议服务，stdout 就是协议通道。
# GUI 子系统拿不到有效 stdout 时 sys.stdout 会是 None，工具一个也说不了话
# （pgy_mcp/server.py 的 _binary_stdout 会因此直接报错退出）。
$pyinstallerArgs = @(
  "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
  "--name", $target,
  "--distpath", $binaries,
  "--workpath", (Join-Path $buildDir "pyinstaller-mcp"),
  "--specpath", $buildDir,
  "--log-level", "WARN",
  (Join-Path $root "mcp_main.py")
)
try {
  Write-Host "[1/2] 打包 MCP 服务（PyInstaller onefile）…" -ForegroundColor Cyan
  & $Python @pyinstallerArgs
  $pyinstallerExit = $LASTEXITCODE
} finally {
  $env:PATH = $previousPath
}
if ($pyinstallerExit -ne 0) { throw "MCP 服务打包失败（详见 $buildDir\pyinstaller-mcp\*.log）" }
if (-not (Test-Path -LiteralPath $bundled)) { throw "打包失败：没有找到 $bundled" }
Write-Host ("      MCP 服务：{0} MB" -f [math]::Round((Get-Item -LiteralPath $bundled).Length / 1MB, 1))

Copy-Item -LiteralPath $bundled -Destination $handout -Force
Write-Host ("      交付 exe：{0}" -f $handout)

# 生成交付用的 mcp.json（与 exe 同目录，一起发给同事）。
# **必须写无 BOM 的 UTF-8**：PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会带
# BOM，带 BOM 的 mcp.json 会让 WorkBuddy 解析配置失败，而且报错信息完全不指向
# 这里——是那种最费时间的坑。
$json = @"
{
  "mcpServers": {
    "pgy": {
      "type": "stdio",
      "command": "$($templateCommand.Replace('\', '\\'))",
      "args": []
    }
  }
}
"@
[System.IO.File]::WriteAllText($config, $json, (New-Object System.Text.UTF8Encoding $false))
Write-Host ("      配置模板：{0}" -f $config)

# 交付安装脚本：同事双击一次，exe 和配置一次搞定。
# 必须由脚本生成配置，而不是发一份写死路径的 json——command 是绝对路径，含用户名，
# 在别人机器上一定是错的，而 WorkBuddy 只会显示「连接失败」，不提示是路径问题。
$installerSrc = Join-Path $root "pgy_mcp\installer"
foreach ($name in @("install-pgy-mcp.ps1", "安装-pgy-mcp.cmd")) {
  Copy-Item -LiteralPath (Join-Path $installerSrc $name) -Destination (Join-Path $releaseDir $name) -Force
}
Write-Host ("      安装脚本：{0}" -f (Join-Path $releaseDir "安装-pgy-mcp.cmd"))

# 说明书随交付一起走：同事拿到的是一个能自解释的目录，不用回头找仓库。
$manual = Join-Path $releaseDir "安装说明.md"
Copy-Item -LiteralPath (Join-Path $root "pgy_mcp\安装说明.md") -Destination $manual -Force
Write-Host ("      说明书：{0}" -f $manual)

Write-Host "[2/2] 冒烟自检：拉起 exe，走一遍 MCP 握手…" -ForegroundColor Cyan
# 这一步刻意**不依赖 Pgy 是否在运行**：握手（initialize / tools/list）根本不碰后端，
# 所以「没开客户端」也能证明 exe 本身是好的——把「exe 坏了」和「客户端没开」
# 这两类问题分开，用户报问题时才知道该查哪边。
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $handout
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.UseShellExecute = $false
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
$proc = [System.Diagnostics.Process]::Start($psi)
try {
  $proc.StandardInput.WriteLine('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}')
  $proc.StandardInput.Flush()
  $read = $proc.StandardOutput.ReadLineAsync()
  if (-not $read.Wait(20000)) { throw "冒烟失败：exe 20 秒内没有回应 initialize（stderr: $($proc.StandardError.ReadToEnd())）" }
  $hello = $read.Result | ConvertFrom-Json
  if ($hello.result.serverInfo.name -ne "pgy") { throw "冒烟失败：initialize 应答里的服务名不是 pgy（实得 $($hello.result.serverInfo.name)）" }

  $proc.StandardInput.WriteLine('{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
  $proc.StandardInput.Flush()
  $read2 = $proc.StandardOutput.ReadLineAsync()
  if (-not $read2.Wait(20000)) { throw "冒烟失败：exe 20 秒内没有回应 tools/list" }
  $tools = ($read2.Result | ConvertFrom-Json).result.tools
  if ($tools.Count -lt 1) { throw "冒烟失败：一个工具都没暴露" }
} finally {
  if (-not $proc.HasExited) { $proc.Kill() }
  $proc.Dispose()
}

Write-Host ("      握手正常：协议版本 {0}，暴露 {1} 个工具" -f $hello.result.protocolVersion, $tools.Count) -ForegroundColor Green
Write-Host ""
Write-Host ("交付物（{0} 下，整个目录发给同事）：" -f $releaseDir) -ForegroundColor Cyan
Write-Host "  安装说明.md             同事先看这个（含「谁开着谁关着」、点信任的时机、排查表）"
Write-Host "  安装-pgy-mcp.cmd       同事双击这个（自动装 exe + 写好 WorkBuddy 配置）"
Write-Host "  pgy-mcp.exe            服务本体"
Write-Host "  install-pgy-mcp.ps1    安装脚本本体（被上面那个 cmd 调用）"
Write-Host "  mcp.json                **手动配置用的模板**，不用脚本的人才需要（路径含占位符，照抄是错的）"
Write-Host ""
Write-Host "同事那台机器（推荐路径，只有一步）：" -ForegroundColor Cyan
Write-Host "  1) 解压/拷出这一整个目录，双击「安装-pgy-mcp.cmd」"
Write-Host "  2) 按脚本提示去 WorkBuddy 点「信任」并重启会话；保持 Pgy 客户端开着（可最小化，别退进程）"
Write-Host ("  exe 会落到 {0}（用户目录，不需要管理员；" -f $installTarget)
Write-Host "   刻意避开 %LOCALAPPDATA%\Pgy —— 那个目录每次装包/升级都会被整个删掉）"
