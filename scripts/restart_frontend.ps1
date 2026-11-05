# restart_frontend.ps1 - kill old instance of this project and start a fresh one
$root = 'B:\Desktop\BaiduSyncdisk\desktop\VSProject\my_brain_backend'
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*ui.desktop*' -and $_.CommandLine -like '*my_brain_backend*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
Start-Process -FilePath "$root\.venv\Scripts\pythonw.exe" `
    -ArgumentList '-m', 'ui.desktop' -WorkingDirectory $root
Start-Sleep -Seconds 5
$alive = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*ui.desktop*' -and $_.CommandLine -like '*my_brain_backend*' }
if ($alive) { Write-Output "frontend running: pid $($alive.ProcessId)" }
else { Write-Output 'frontend FAILED to start (run: .venv\Scripts\python.exe -m ui.desktop to see the error)' }
