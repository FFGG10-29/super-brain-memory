# Run the Flutter client in debug mode (also generates the Windows runner if missing).
#   powershell -ExecutionPolicy Bypass -File flutter_app\scripts\run_windows.ps1
$ErrorActionPreference = 'Stop'
$app = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $app

$flutter = Get-Command flutter -ErrorAction SilentlyContinue
if (-not $flutter) { throw 'flutter not found in PATH.' }

if (-not (Test-Path (Join-Path $app 'windows\CMakeLists.txt'))) {
    Copy-Item 'lib\main.dart' 'lib\main.dart.bak' -Force
    Copy-Item 'pubspec.yaml' 'pubspec.yaml.bak' -Force
    try {
        & flutter create --platforms=windows --project-name super_brain --org com.superbrain . 2>&1 | Out-Host
        if ((Get-Content 'lib\main.dart' -Raw) -ne (Get-Content 'lib\main.dart.bak' -Raw)) {
            Copy-Item 'lib\main.dart.bak' 'lib\main.dart' -Force
        }
        Copy-Item 'pubspec.yaml.bak' 'pubspec.yaml' -Force
    } finally {
        Remove-Item 'lib\main.dart.bak' -ErrorAction SilentlyContinue
        Remove-Item 'pubspec.yaml.bak' -ErrorAction SilentlyContinue
    }
}

& flutter pub get 2>&1 | Out-Host
Write-Output 'Starting v3 daemon is required first: .venv\Scripts\python.exe scripts\run_v3.py'
& flutter run -d windows 2>&1 | Out-Host
