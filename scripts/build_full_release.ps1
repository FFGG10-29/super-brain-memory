# One-shot build: Flutter SDK -> Flutter Release -> frozen backend -> dist\ -> installer.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\build_full_release.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build_full_release.ps1 -FlutterSdk 'D:\flutter' -InstallSdk -Launch
# -InstallSdk clones Flutter stable to D:\flutter if missing (~1.5GB; first flutter command
#  additionally downloads the Dart SDK). Requires git + network.
param(
    [string]$FlutterSdk = 'D:\flutter',
    [switch]$InstallSdk,
    [switch]$SkipFrontend,
    [switch]$SkipBackend,
    [switch]$Launch
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$flutterApp = Join-Path $root 'flutter_app'

function Resolve-Flutter {
    $bat = Join-Path $FlutterSdk 'bin\flutter.bat'
    if (Test-Path $bat) { return $bat }
    $cmd = Get-Command flutter -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$flutter = Resolve-Flutter
if (-not $flutter -and $InstallSdk) {
    Write-Output "Cloning Flutter stable -> $FlutterSdk (large download)..."
    & git clone --depth 1 -b stable 'https://github.com/flutter/flutter.git' $FlutterSdk
    $flutter = Resolve-Flutter
}
if (-not $flutter) {
    throw "Flutter SDK not found. Pass -FlutterSdk 'D:\path\to\flutter' or -InstallSdk to clone it."
}
Write-Output "Flutter SDK: $flutter"

if (-not $SkipFrontend) {
    Push-Location $flutterApp
    try {
        & $flutter pub get
        & $flutter analyze
        & $flutter build windows --release
    } finally { Pop-Location }
}

if (-not $SkipBackend) {
    & $venvPython 'scripts\build_portable.py' '--headless'
    & $venvPython '-m' 'PyInstaller' 'super-brain-backend.spec' '--noconfirm'
    & $venvPython 'scripts\build_portable.py' '--trim' 'dist\super-brain-backend'
    & $venvPython 'scripts\build_portable.py' '--link-models' 'dist\super-brain-backend'
}

# Assemble dist\ (Flutter exe + backend\headless-server.exe) and compile installer if ISCC exists.
& powershell -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\installer\build.ps1')

$exe = Join-Path $flutterApp 'build\windows\x64\runner\Release\super_brain.exe'
Write-Output "Frontend: $exe"
Write-Output 'Backend:  dist\backend\headless-server.exe  (launched by the frontend at startup)'
if ($Launch -and (Test-Path $exe)) { Start-Process $exe }
