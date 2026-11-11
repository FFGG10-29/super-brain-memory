# Prepare dist\ for Inno Setup: Flutter frontend + frozen headless backend.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\installer\build.ps1
# Prerequisites (run once on a dev machine, see README):
#   powershell -ExecutionPolicy Bypass -File flutter_app\scripts\build_windows.ps1
#   .\.venv\Scripts\python.exe scripts\build_portable.py --headless
#   .\.venv\Scripts\python.exe -m PyInstaller super-brain-backend.spec --noconfirm
# 用 Continue：PyInstaller/ISCC 进度日志走 stderr，Stop 会被当成 NativeCommandError 掐死构建。
$ErrorActionPreference = 'Continue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Force -Path $dist | Out-Null

# 1) Flutter Release -> dist root (super_brain.exe, flutter_windows.dll, data\)
$flutterRelease = Join-Path $root 'flutter_app\build\windows\x64\runner\Release'
$flutterExe = Join-Path $flutterRelease 'super_brain.exe'
if (Test-Path $flutterExe) {
    # 不用 Copy-Item -Recurse '<dir>\*'：通配符展开会把子目录内容拍平到目标根。
    & robocopy $flutterRelease $dist /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy Flutter failed with exit code $LASTEXITCODE" }
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
    & robocopy $backend $target /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy backend failed with exit code $LASTEXITCODE" }
    Write-Output "Backend: $target\headless-server.exe"
} else {
    Write-Output 'WARN: backend build missing; run scripts\build_portable.py --headless then PyInstaller super-brain-backend.spec.'
}

# 3) Brand icon: Flutter 前端的品牌图标（app_icon.ico）。
#    dist\app.ico -> installer root / uninstall entry; backend\app.ico -> Win32 tray。
$brandIcon = Join-Path $root 'flutter_app\windows\runner\resources\app_icon.ico'
if (Test-Path $brandIcon) {
    Copy-Item -Force $brandIcon (Join-Path $dist 'app.ico')
    New-Item -ItemType Directory -Force -Path (Join-Path $dist 'backend') | Out-Null
    Copy-Item -Force $brandIcon (Join-Path $dist 'backend\app.ico')
    Write-Output "Brand icon: $brandIcon"
} else {
    Write-Output 'WARN: brand icon missing; tray falls back to default Windows icon.'
}

# 4) Compile installer if Inno Setup is available.
#    winget 默认装到用户级目录且不会加入 PATH，只查 Get-Command 会永远跳过编译。
$issFile = Join-Path $PSScriptRoot 'setup.iss'
$isccCmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
$isccPath = if ($isccCmd) { $isccCmd.Source } else { $null }
if (-not $isccPath) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { $isccPath = $candidate; break }
    }
}
if ($isccPath) {
    Write-Output "ISCC: $isccPath"
    & $isccPath $issFile
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }
    Write-Output 'installer built in release\'
} else {
    Write-Output 'ISCC.exe not found; dist\ is ready. Install Inno Setup 6 and run: ISCC.exe scripts\installer\setup.iss'
}
