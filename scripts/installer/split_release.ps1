# Split a large installer into <2GB parts for GitHub Releases (per-asset limit 2GiB).
#   powershell -ExecutionPolicy Bypass -File scripts\installer\split_release.ps1
#   powershell ... -File ... -Source release\SuperBrainSetup.exe -PartMB 1900
# Produces: <Source>.part01 ... + reassemble.cmd + <Source>.sha256
param(
    [string]$Source = 'release\SuperBrainSetup.exe',
    [int]$PartMB = 1900
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not [System.IO.Path]::IsPathRooted($Source)) { $Source = Join-Path $root $Source }
if (-not (Test-Path $Source)) { throw "installer not found: $Source" }

$dir = Split-Path -Parent $Source
$name = Split-Path -Leaf $Source
$parts = Get-ChildItem -Path $dir -Filter "$name.part*" -ErrorAction SilentlyContinue
foreach ($p in $parts) { Remove-Item $p.FullName -Force }

$size = (Get-Item $Source).Length
$chunk = [int64]$PartMB * 1MB
$count = [math]::Ceiling($size / $chunk)
$index = 0
$input = [System.IO.File]::OpenRead($Source)
try {
    $buffer = New-Object byte[] (4MB)
    for ($i = 0; $i -lt $count; $i++) {
        $index++
        $outPath = Join-Path $dir ('{0}.part{1:D2}' -f $name, $index)
        $remaining = [math]::Min($chunk, $size - $i * $chunk)
        $out = [System.IO.File]::Create($outPath)
        try {
            while ($remaining -gt 0) {
                $want = [int][math]::Min($buffer.Length, $remaining)
                $read = $input.Read($buffer, 0, $want)
                if ($read -le 0) { throw "unexpected EOF at part $index" }
                $out.Write($buffer, 0, $read)
                $remaining -= $read
            }
        } finally { $out.Close() }
        Write-Output ('part {0:D2}: {1:N2} MB' -f $index, ((Get-Item $outPath).Length / 1MB))
    }
} finally { $input.Close() }

# SHA256 of the whole installer (verify after reassembly)
$hash = (Get-FileHash -Algorithm SHA256 -Path $Source).Hash.ToLower()
"$hash  $name" | Set-Content -Path (Join-Path $dir "$name.sha256") -Encoding ascii

# One-click reassembly for end users
$cmd = @"
@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 正在合并 $name 分卷...
if exist "$name" del /q "$name"
copy /b "$name.part*" "$name" >nul
if not exist "$name" ( echo 合并失败：缺少分卷文件 & pause & exit /b 1 )
echo.
echo 合并完成：$name
echo 校验 SHA256（应与 $name.sha256 一致）：
certutil -hashfile "$name" SHA256
echo.
if exist "$name" start "" "$name"
pause
"@
Set-Content -Path (Join-Path $dir 'reassemble.cmd') -Value $cmd -Encoding utf8

Write-Output "OK: $count parts, sha256=$hash"
Write-Output "output dir: $dir"
