# 把开发工作区清回「全新安装」状态（每次开发完跑一次）。
# 只删运行期数据与缓存，源码、脚本、sidecar、node_modules 一律保留。
#
# 用法： powershell -NoProfile -ExecutionPolicy Bypass -File build\clean-dev-data.ps1
param(
  [switch]$KeepBackup,      # 不备份账号库（默认会备份到工作区外）
  [switch]$KeepCredentials, # 不清理 Windows 凭据管理器里的 pgy:// 条目
  [switch]$KeepAccountStore # 保留账号库与凭据（用户刚添加的账号还在里面，清掉就得重加）
)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if ($KeepAccountStore) { $KeepBackup = $true; $KeepCredentials = $true }

function SizeMB($p) {
  if (Test-Path $p) { [math]::Round(((Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum)/1MB,1) } else { 0 }
}

Write-Host "=== 清理前 ===" -ForegroundColor Cyan
"profiles={0}MB asset-cache={1}MB pgy_logs={2}MB build={3}MB" -f (SizeMB profiles),(SizeMB asset-cache),(SizeMB pgy_logs),(SizeMB build)

if (-not $KeepBackup -and (Test-Path "pgy.db")) {
  $backup = Join-Path (Split-Path -Parent $root) ("backup-pgy-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
  New-Item -ItemType Directory -Force -Path $backup | Out-Null
  Copy-Item "pgy.db" (Join-Path $backup "dev-pgy.db") -Force
  if (Test-Path "pgy_logs\pgy.log") { Copy-Item "pgy_logs\pgy.log" (Join-Path $backup "dev-pgy.log") -Force }
  $inst = Join-Path $env:LOCALAPPDATA "Pgy\pgy.db"
  if (Test-Path $inst) { Copy-Item $inst (Join-Path $backup "installed-pgy.db") -Force }
  Write-Host "账号库已备份到：$backup" -ForegroundColor Yellow
}

if (-not $KeepCredentials) {
  $targets = (cmdkey /list) | Select-String "target=pgy://" | ForEach-Object { ($_.Line -replace '.*target=', '').Trim() }
  foreach ($t in $targets) { cmdkey /delete:$t *> $null }
  Write-Host ("已清理系统凭据 pgy:// 条目 {0} 条" -f $targets.Count) -ForegroundColor Yellow
}

Write-Host "=== 运行期数据 ===" -ForegroundColor Cyan
foreach ($p in @("profiles","pgy_logs","asset-cache","browser-endpoints")) {
  if (Test-Path $p) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue; Write-Host "  - $p" }
}
Get-ChildItem -File -Filter "pgy.db*" | ForEach-Object {
  if ($KeepAccountStore) { Write-Host ("  = 保留账号库 " + $_.Name) }
  else { Remove-Item $_.FullName -Force; Write-Host ("  - " + $_.Name) }
}
Get-ChildItem -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
foreach ($p in @(".pytest_cache","frontend\dist")) { if (Test-Path $p) { Remove-Item $p -Recurse -Force; Write-Host "  - $p" } }

Write-Host "=== build/ 运行记录（保留脚本）===" -ForegroundColor Cyan
foreach ($pat in @("*.log","*.out","*.png","*.txt","*.pid","sidecar-prev-*.exe")) {
  Get-ChildItem build -File -Filter $pat -ErrorAction SilentlyContinue | ForEach-Object { Remove-Item $_.FullName -Force }
}
foreach ($d in @("build\pyinstaller","build\smoke")) { if (Test-Path $d) { Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue } }

Write-Host "=== 已安装客户端的运行数据（保留程序本体）===" -ForegroundColor Cyan
$instDir = Join-Path $env:LOCALAPPDATA "Pgy"
foreach ($p in @("pgy.db","pgy.db-shm","pgy.db-wal","pgy_logs")) {
  $full = Join-Path $instDir $p
  if (Test-Path $full) { Remove-Item $full -Recurse -Force -ErrorAction SilentlyContinue; Write-Host "  - $full" }
}

Write-Host "=== 清理后 ===" -ForegroundColor Green
"profiles={0}MB asset-cache={1}MB pgy_logs={2}MB build={3}MB" -f (SizeMB profiles),(SizeMB asset-cache),(SizeMB pgy_logs),(SizeMB build)
Write-Host ("侧车保留：{0} MB" -f [math]::Round((Get-Item "frontend\src-tauri\binaries\pgy-backend-x86_64-pc-windows-msvc.exe" -ErrorAction SilentlyContinue).Length/1MB,1))
