# scripts/restart_frontend.ps1 —— 杀掉本项目旧前端实例并重新启动（每次改完代码后运行）
$root = Split-Path -Parent $PSScriptRoot
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*ui.desktop*' -and $_.CommandLine -like '*my_brain_backend*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
Start-Process -FilePath (Join-Path $root '.venv\Scripts\pythonw.exe') `
    -ArgumentList '-m', 'ui.desktop' -WorkingDirectory $root
Start-Sleep -Seconds 5
$alive = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*ui.desktop*' -and $_.CommandLine -like '*my_brain_backend*' }
if ($alive) { Write-Output "frontend running: pid $($alive.ProcessId)" }
else { Write-Output 'frontend FAILED to start (run: .venv\Scripts\python.exe -m ui.desktop 看报错)' }
