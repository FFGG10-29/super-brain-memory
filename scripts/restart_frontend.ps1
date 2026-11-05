# restart_frontend.ps1 - kill old instance of this project and start a fresh one
# 用法：powershell -File scripts/restart_frontend.ps1 [--debug]
#   --debug 用 python.exe（有控制台，能看到异常）；默认 pythonw.exe（无控制台）
$root = Split-Path -Parent $PSScriptRoot
$exe = if ($args -contains '--debug') { 'python.exe' } else { 'pythonw.exe' }

Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*ui.desktop*' -and $_.CommandLine -like "*$root*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Start-Process -FilePath "$root\.venv\Scripts\$exe" `
    -ArgumentList '-m', 'ui.desktop' -WorkingDirectory $root
Start-Sleep -Seconds 6

$alive = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*ui.desktop*' -and $_.CommandLine -like "*$root*" }
if ($alive) { Write-Output "frontend running: pid $(($alive.ProcessId) -join ', ')" }
else { Write-Output 'frontend FAILED to start (run: powershell -File scripts/restart_frontend.ps1 --debug)' }
