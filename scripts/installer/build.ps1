# Prepare dist\ for Inno Setup and optionally compile the installer.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\installer\build.ps1
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Force -Path $dist | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $dist 'app') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $dist 'runtime') | Out-Null

# 1) application sources (no venv, no data, no build output)
foreach ($name in @('v3','brain','core','config.py','main.py','models','storage','engine','sync','gateway','workers','requirements.txt')) {
    Copy-Item -Recurse -Force (Join-Path $root $name) (Join-Path $dist 'app')
}

# 2) dependencies into dist\runtime (pythonw.exe from the venv is used by WinSW)
$python = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path $python) {
    & $python -m pip install --disable-pip-version-check -q -r (Join-Path $root 'requirements.txt') --target $dist\runtime
    Copy-Item (Join-Path $root '.venv\Scripts\pythonw.exe') (Join-Path $dist 'runtime\pythonw.exe')
    Copy-Item (Join-Path $root '.venv\Scripts\python311.dll') (Join-Path $dist 'runtime\python311.dll') -ErrorAction SilentlyContinue
}

# 3) WinSW binary (download once; not committed)
$winsw = Join-Path $dist 'winsw.exe'
if (-not (Test-Path $winsw)) {
    Write-Output 'NOTE: place WinSW v3 x64 binary at dist\winsw.exe before compiling the installer'
    Write-Output '      https://github.com/winsw/winsw/releases'
}

# 4) compile installer if ISCC is available
$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($iscc) {
    & $iscc.Source (Join-Path $PSScriptRoot 'setup.iss')
    Write-Output 'installer built in release\'
} else {
    Write-Output 'ISCC.exe not found; dist\ is ready. Install Inno Setup 6 and run: ISCC.exe scripts\installer\setup.iss'
}
