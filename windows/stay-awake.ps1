# Wach-Wächter: hält den PC wach und bricht angekündigte Neustarts/Shutdowns ab
# (z.B. von Installern). Gedacht für unbeaufsichtigte Läufe, etwa per Fernsteuerung,
# weil ein Neustart in Gentoo landet (Bootreihenfolge bleibt so).
#
# Starten:  Start-Process pwsh -WindowStyle Hidden -ArgumentList '-NoProfile','-File',"$env:USERPROFILE\scripts\stay-awake.ps1"
# Beenden:  New-Item "$env:USERPROFILE\scripts\stay-awake.stop"   (oder Prozess beenden)
Add-Type -Namespace W -Name P -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f);'
$dir  = Join-Path $env:USERPROFILE 'scripts'
$stop = Join-Path $dir 'stay-awake.stop'
$log  = Join-Path $dir 'stay-awake.log'
Remove-Item $stop -EA SilentlyContinue
"$(Get-Date -f s) gestartet (PID $PID)" | Add-Content $log
while (-not (Test-Path $stop)) {
    [void][W.P]::SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    & shutdown.exe /a 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { "$(Get-Date -f s) Shutdown/Neustart abgebrochen" | Add-Content $log }
    Start-Sleep -Seconds 3
}
Remove-Item $stop -EA SilentlyContinue
"$(Get-Date -f s) beendet" | Add-Content $log
