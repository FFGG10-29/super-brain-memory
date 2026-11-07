# Prepare dist\ for Inno Setup: Flutter frontend + frozen headless backend.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\installer\build.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\installer\build.ps1 -IncludeDebugConsole
# Prerequisites (run once on a dev machine, see README):
#   powershell -ExecutionPolicy Bypass -File flutter_app\scripts\build_windows.ps1
#   .\.venv\Scripts\python.exe scripts\build_portable.py --headless
#   .\.venv\Scripts\python.exe -m PyInstaller super-brain-backend.spec --noconfirm
param([switch]$IncludeDebugConsole)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Force -Path $dist | Out-Null

# 1) Flutter Release -> dist root (super_brain.exe, flutter_windows.dll, data\)
$flutterRelease = Join-Path $root 'flutter_app\build\windows\x64\runner\Release'
$flutterExe = Join-Path $flutterRelease 'super_brain.exe'
if (Test-Path $flutterExe) {
    Copy-Item -Recurse -Force (Join-Path $flutterRelease '*') $dist
    Write-Output "Flutter frontend: $flutterExe"
} else {
    Write-Output 'WARN: Flutter Release not found; run flutter_app\scripts\build_windows.ps1 (requires Flutter SDK).'
}

# 2) Frozen backend -> dist\backend\headless-server.exe (Flutter launches it on startup)
$backend = Join-Path $root 'dist\super-brain-backend'
$backendExe = Join-Path $backend 'headless-server.exe'
if (Test-Path $backendExe) {
    $target = Join-Path $dist 'backend'
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item -Recurse -Force (Join-Path $backend '*') $target
    Write-Output "Backend: $target\headless-server.exe"
} else {
    Write-Output 'WARN: backend build missing; run scripts\build_portable.py --headless then PyInstaller super-brain-backend.spec.'
}

# 3) Optional hidden debug console (PySide6). NOT shipped by default:
#    it opens the same Kuzu DB as the backend, so both must never run together.
if ($IncludeDebugConsole) {
    $debug = Join-Path $dist 'debug-console'
    New-Item -ItemType Directory -Force -Path $debug | Out-Null
    foreach ($name in @('ui', 'brain', 'core', 'storage', 'engine', 'sync', 'gateway',
                        'models', 'workers', 'v3', 'config.py', 'main.py',
                        'headless_server.py', 'requirements.txt')) {
        $source = Join-Path $root $name
        if (Test-Path $source) { Copy-Item -Recurse -Force $source $debug }
    }
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path $python) {
        & $python -m pip install --disable-pip-version-check -q -r (Join-Path $root 'requirements.txt') `
            --target (Join-Path $debug 'runtime')
        Copy-Item (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $debug 'python.exe')
    }
    Write-Output "Debug console (hidden entry): $debug  ->  python -m ui.desktop"
}

# 4) Compile installer if Inno Setup is available
$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($iscc) {
    & $iscc.Source (Join-Path $PSScriptRoot 'setup.iss')
    Write-Output 'installer built in release\'
} else {
    Write-Output 'ISCC.exe not found; dist\ is ready. Install Inno Setup 6 and run: ISCC.exe scripts\installer\setup.iss'
}
