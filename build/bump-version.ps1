# 版本号一次性改全 —— 四处必须一致，漏一处 build-installer.ps1 就会拒绝出包。
#
# 用法：
#   powershell -File build\bump-version.ps1 -Bump patch    # 0.1.0 -> 0.1.1
#   powershell -File build\bump-version.ps1 -Bump minor    # 0.1.1 -> 0.2.0
#   powershell -File build\bump-version.ps1 -Version 0.2.3 # 直接指定
#
# ⚠️⚠️ **本脚本 2026-10-12 出过一次严重事故，改动前务必读这段** ⚠️⚠️
#
# 事故：第一版用 `Get-Content -Raw` 读文件。Windows PowerShell 5.1 的 `Get-Content`
# **默认按系统 ANSI 代码页（中文机器上是 936/GBK）解码**，而这些文件是 UTF-8。
# 于是：读出即乱码 → `WriteAllText(UTF8)` 写回 → **乱码被固化，四个文件的中文全毁**。
# 更糟的是 GBK 对某些字节对无对应，解码时替换成 `?`，**信息有损、无法机械还原**。
#
# 教训（两条，都要遵守）：
#   1. **凡是读写源码/配置，只走 `[System.IO.File]::ReadAllText/WriteAllText` 并显式指定
#      编码**，永远不用 `Get-Content` / `Set-Content` 的默认编码。
#   2. 脚本改完文件后**必须复核关键中文还在**（见文件末尾的断言），
#      不然"命令成功了"会骗过你 —— 那次就是四个文件全毁却一路打印 [OK]。
#
# 另：本文件自身含中文，**必须以 UTF-8 with BOM 保存**，否则 PS 5.1 按 GBK 读会解析报错。

param(
  [ValidateSet("patch", "minor", "major")][string]$Bump,
  [string]$Version
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$utf8 = New-Object System.Text.UTF8Encoding $false
$targets = @(
  @{ Path = "frontend\src-tauri\tauri.conf.json"
     Pattern = '("version"\s*:\s*")[^"]+(")';  Note = "tauri.conf.json" },
  @{ Path = "frontend\src-tauri\Cargo.toml"
     Pattern = '^(version\s*=\s*")[^"]+(")';  Note = "Cargo.toml"; Multiline = $true },
  @{ Path = "frontend\package.json"
     Pattern = '("version"\s*:\s*")[^"]+(")'; Note = "package.json" },
  # app.py 里的版本是 FastAPI(...) 的**字面量** —— build-installer.ps1 就是用正则从这里读的，
  # 所以它必须保持字面量，不能被改成常量引用。
  @{ Path = "backend\app.py"
     Pattern = '(FastAPI\(title="[^"]*",\s*version=")[^"]+(")'; Note = "app.py" }
)

function Read-Utf8([string]$relative) {
  # 唯一正确的读法：显式 UTF-8。用 Get-Content 会把中文读坏（见文件头的事故说明）。
  return [System.IO.File]::ReadAllText((Join-Path $root $relative), [System.Text.Encoding]::UTF8)
}

function Get-CurrentVersion {
  $conf = Read-Utf8 "frontend\src-tauri\tauri.conf.json"
  if ($conf -match '"version"\s*:\s*"([^"]+)"') { return $Matches[1] }
  throw "读不出当前版本号"
}

$current = Get-CurrentVersion
if (-not $Version) {
  if (-not $Bump) { throw "必须给 -Bump 或 -Version。当前版本 $current" }
  $parts = $current.Split(".")
  $major = [int]$parts[0]; $minor = [int]$parts[1]; $patch = [int]$parts[2]
  switch ($Bump) {
    "major" { $major++; $minor = 0; $patch = 0 }
    "minor" { $minor++; $patch = 0 }
    "patch" { $patch++ }
  }
  $Version = "$major.$minor.$patch"
}

Write-Host "版本：$current -> $Version" -ForegroundColor Cyan
foreach ($t in $targets) {
  $full = Join-Path $root $t.Path
  $text = Read-Utf8 $t.Path
  # 改之前先确认这个文件里有中文（除 app.py 外都该有）—— 没有说明上一步已经把它读坏了
  if (-not $text.Contains([char]0x84B2 + [char]0x516C + [char]0x82F1) -and $t.Note -ne "app.py") {
    throw ("【{0}】里找不到「蒲公英」—— 文件可能已被读坏。停下来人工检查，不要继续写。" -f $t.Note)
  }
  $options = if ($t.Multiline) { [System.Text.RegularExpressions.RegexOptions]::Multiline }
             else { [System.Text.RegularExpressions.RegexOptions]::None }
  $new = [regex]::Replace($text, $t.Pattern, ('${1}' + $Version + '${2}'), $options)
  if ($new -eq $text) { throw "【$($t.Note)】没匹配到版本号，正则要跟着改：$($t.Pattern)" }
  [System.IO.File]::WriteAllText($full, $new, $utf8)
  Write-Host ("  已更新 {0}" -f $t.Note) -ForegroundColor Green
}

# 复核：四处版本一致 **且** 中文没被读坏。只有两条都过才算成功。
Write-Host "`n复核：" -ForegroundColor Cyan
$allOk = $true
foreach ($t in $targets) {
  $text = Read-Utf8 $t.Path
  $hasVersion = $text.Contains($Version)
  if ($t.Note -eq "app.py") {
    $hasChinese = $text.Contains([char]0x672C + [char]0x5730 + [char]0x670D + [char]0x52A1)
  } else {
    $hasChinese = $text.Contains([char]0x84B2 + [char]0x516C + [char]0x82F1)
  }
  if ($hasVersion -and $hasChinese) {
    Write-Host ("  [OK] {0}  版本={1} 中文完好" -f $t.Note, $Version) -ForegroundColor Green
  } else {
    $allOk = $false
    Write-Host ("  [!!] {0}  版本={1} 中文完好={2}" -f $t.Note, $hasVersion, $hasChinese) -ForegroundColor Red
  }
}
if (-not $allOk) {
  throw "复核未通过：版本不一致或中文被读坏。**不要出包**，先修好。"
}
Write-Host "`n完成。现在可以出包：powershell -File build-installer.ps1" -ForegroundColor Cyan
