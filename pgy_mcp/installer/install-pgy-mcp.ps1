<#
.SYNOPSIS
    把 Pgy MCP 服务装到本机，并写好 WorkBuddy 的 MCP 配置。

.DESCRIPTION
    使用者只需要双击同目录的「安装-pgy-mcp.cmd」，不需要手改任何路径。

    为什么必须由脚本生成配置、而不是直接发一份 mcp.json：
    MCP 配置里的 command 是**绝对路径**，而绝对路径里含用户名（%USERPROFILE%）、
    盘符、以及每个人把文件放在哪。发一份写死路径的 json，在别人机器上一定是错的，
    而且 WorkBuddy 只会显示「连接失败」，不会提示是路径问题——这是最费时间的一类坑。

    安装位置与理由：
      exe  → %LOCALAPPDATA%\PgyMCP\pgy-mcp.exe
             用户目录，**不需要管理员**（C:\PgyMCP 那种位置普通用户建不了）；
             且刻意避开 %LOCALAPPDATA%\Pgy——那是 Pgy 的安装目录兼数据目录，
             installer-hooks.nsh 每次装包/升级都会 RMDir /r 整个删掉。
      配置 → %USERPROFILE%\.workbuddy\mcp.json（合并进去，不覆盖别人的连接器）

.PARAMETER SourceExe
    要安装的 exe；默认取本脚本同目录下的 pgy-mcp.exe。

.PARAMETER InstallDir
    安装目录；默认 %LOCALAPPDATA%\PgyMCP。

.PARAMETER ConfigPath
    WorkBuddy 的 MCP 配置文件；默认 %USERPROFILE%\.workbuddy\mcp.json。
    如果 WorkBuddy 界面里显示的路径不是这个，用界面里的那个再跑一次。

.PARAMETER NoSelfTest
    装完不跑自检。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File install-pgy-mcp.ps1
#>
[CmdletBinding()]
param(
  [string]$SourceExe = "",
  [string]$InstallDir = "",
  [string]$ConfigPath = "",
  [switch]$NoSelfTest
)

$ErrorActionPreference = "Stop"

function Write-Head($text) { Write-Host ""; Write-Host $text -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Write-Warn2($text){ Write-Host "  [注意] $text" -ForegroundColor Yellow }
function Write-Bad($text)  { Write-Host "  [失败] $text" -ForegroundColor Red }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SourceExe) { $SourceExe = Join-Path $here "pgy-mcp.exe" }
if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA "PgyMCP" }
if (-not $ConfigPath) { $ConfigPath = Join-Path $env:USERPROFILE ".workbuddy\mcp.json" }
$targetExe = Join-Path $InstallDir "pgy-mcp.exe"

Write-Host "Pgy MCP 服务安装器" -ForegroundColor White

# ---- 1) 放 exe ----------------------------------------------------------
Write-Head "[1/3] 安装 exe"
if (-not (Test-Path -LiteralPath $SourceExe)) {
  Write-Bad "找不到要安装的 exe：$SourceExe"
  Write-Host "       请确认 pgy-mcp.exe 与安装脚本放在同一个目录里。"
  exit 1
}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$sameFile = $false
try {
  $sameFile = ([System.IO.Path]::GetFullPath($SourceExe) -eq [System.IO.Path]::GetFullPath($targetExe))
} catch { $sameFile = $false }
if ($sameFile) {
  # 有人直接从安装位置再跑一次脚本时走这条：Copy-Item 覆盖自身会直接抛 IOException，
  # 用户看到的是红字堆栈而不是「已经装好了」。这里只需继续往下写配置。
  Write-Ok "exe 已经在该位置，跳过复制：$targetExe"
} elseif (-not (Test-Path -LiteralPath $targetExe)) {
  Copy-Item -LiteralPath $SourceExe -Destination $targetExe -Force
  Write-Ok "已安装到 $targetExe"
} else {
  # 覆盖前先探活：如果正有 WorkBuddy 会话在用这个 exe，Windows 会拒绝覆盖，
  # 这时要说人话，而不是抛一堆英文堆栈。
  try {
    Copy-Item -LiteralPath $SourceExe -Destination $targetExe -Force
    Write-Ok "已更新 $targetExe"
  } catch {
    Write-Bad "无法覆盖 $targetExe（多半是 WorkBuddy 正在使用它）。"
    Write-Host "       请先在 WorkBuddy 里停用 pgy 连接器、或完全退出 WorkBuddy，然后重跑本脚本。"
    exit 3
  }
}

# ---- 2) 合并 WorkBuddy 配置 ---------------------------------------------
Write-Head "[2/3] 写入 WorkBuddy 配置"
$configDir = Split-Path -Parent $ConfigPath
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

$root = @{}
if (Test-Path -LiteralPath $ConfigPath) {
  $raw = [System.IO.File]::ReadAllText($ConfigPath)
  if ($raw.Trim()) {
    try {
      $existing = $raw | ConvertFrom-Json -ErrorAction Stop
      foreach ($property in $existing.PSObject.Properties) { $root[$property.Name] = $property.Value }
    } catch {
      # 绝不猜、也绝不覆盖：这份文件是用户手写的，宁可让他自己合并。
      Write-Bad "现有的 $ConfigPath 不是合法 JSON，没有动它。"
      Write-Host "       请把下面这段手动合并进 mcpServers（注意不要写成 .mcp.json、JSON 不支持注释）："
      Write-Host ""
      Write-Host "       { `"pgy`": { `"type`": `"stdio`", `"command`": `"$($targetExe.Replace('\', '\\'))`", `"args`": [] } }"
      Write-Host ""
      exit 2
    }
  }
  $backup = "$ConfigPath.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
  Copy-Item -LiteralPath $ConfigPath -Destination $backup -Force
  Write-Ok "已备份原配置：$backup"
}

$servers = @{}
if ($root.ContainsKey("mcpServers") -and $root["mcpServers"]) {
  foreach ($property in $root["mcpServers"].PSObject.Properties) { $servers[$property.Name] = $property.Value }
}
$replaced = $servers.ContainsKey("pgy")
# 键名固定为 pgy：换名字会让已经配好的机器多出一份失效的连接器。
#
# **`type` 必须有，且必须是 `stdio`**（真机 2026-10-09 踩到）：WorkBuddy 的解析里，
# 只有 `type === "stdio"` 的条目才会被认成一个本地服务；没有 `type` 时它不会按
# stdio 处理，而条目里又没有 `url`，于是这条被**静默丢掉**——服务列表里什么都没有，
# 也不报错、不写日志，是那种最难查的「配了但没反应」。
$servers["pgy"] = @{ type = "stdio"; command = $targetExe; args = @() }
$root["mcpServers"] = $servers

# 必须写**无 BOM** 的 UT：PowerShell 5.1 的 Set-Content -Encoding UTF8 会带 BOM，
# 带 BOM 的 mcp.json 会让 WorkBuddy 解析配置失败，而且报错完全不指向这里。
$json = $root | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($ConfigPath, $json, (New-Object System.Text.UTF8Encoding $false))
Write-Ok ("已{0} {1}" -f $(if ($replaced) { "更新" } else { "写入" }), $ConfigPath)

# ---- 3) 自检 -----------------------------------------------------------
Write-Head "[3/3] 自检"
if ($NoSelfTest) {
  Write-Warn2 "已跳过自检"
} else {
  # 自检回答的是「现在能不能用」，最可能的失败原因是 Pgy 客户端没开——
  # 在这里就说清楚，省得他配完 WorkBuddy 才发现连不上却不知道查哪边。
  $selfTest = & $targetExe --self-test 2>&1
  $exit = $LASTEXITCODE
  $selfTest | ForEach-Object { Write-Host "        $_" }
  if ($exit -ne 0) {
    Write-Warn2 "自检没通过：多数情况是 Pgy 客户端没开着。开起来（可以最小化，但别退进程）后再跑一次本脚本的自检："
    Write-Host "        `"$targetExe`" --self-test"
  }
}

Write-Head "接下来只剩两步"
Write-Host "  1) 打开 WorkBuddy → 左侧「插件」→ 右上角「MCP 服务器」→ 找到 pgy → 点「信任」"
Write-Host "     （不点信任会一直显示失败；点完要重启当前会话或新开一个会话，工具才会出现）"
Write-Host "  2) 保持 Pgy 客户端开着（可最小化，别退出进程）——MCP 是连它的本地服务。"
Write-Host ""
Write-Host "    想撤掉：删掉 $InstallDir 和 $ConfigPath 里的 pgy 这一段即可。" -ForegroundColor DarkGray
