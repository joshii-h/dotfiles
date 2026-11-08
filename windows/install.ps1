#Requires -Version 7
<#
  Richtet die Windows-Seite der Dotfiles ein (Dualboot-Battlestation).
  Erhöht ausführen:  pwsh -File "$env:USERPROFILE\Projects\dotfiles\windows\install.ps1"

  - verlinkt die Skripte nach %USERPROFILE%\scripts (Symlinks -> `git pull` bzw.
    der sysup-Job "dotfiles" hält sie aktuell)
  - legt %USERPROFILE%\.config\sysup\winget-skip.txt an (Symlink auf das Repo)
  - trägt die Funktion `sysup` in Documents\PowerShell\CTTcustom.ps1 ein
    (das Chris-Titus-Profil überschreibt sich selbst, CTTcustom.ps1 bleibt)
  - installiert das PowerShell-Modul Microsoft.WinGet.Client (braucht der winget-Job)
  - setzt den Neustart-Schutz: Windows Update lädt nur noch herunter und meldet,
    installiert und startet aber nie selbst (das übernimmt sysup, ohne Neustart);
    ein Neustart würde in Gentoo landen
  - Hardware-Uhr in UTC (RealTimeIsUniversal), passend zu Gentoo clock="UTC"
#>
$ErrorActionPreference = 'Stop'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw 'Bitte erhöht ausführen (Symlinks und HKLM brauchen Admin).' }

$src = $PSScriptRoot
function Link($target, $link) {
    New-Item -ItemType Directory -Force (Split-Path $link) | Out-Null
    $item = Get-Item $link -EA SilentlyContinue
    if ($item -and $item.LinkType -eq 'SymbolicLink' -and $item.Target -eq $target) { "  = $link"; return }
    if ($item) { Move-Item $link "$link.bak-$(Get-Date -f yyyyMMddHHmmss)" -Force; "  (altes $link gesichert)" }
    New-Item -ItemType SymbolicLink -Path $link -Target $target | Out-Null
    "  → $link"
}

'Skripte:'
Link (Join-Path $src 'sysup.ps1')       (Join-Path $env:USERPROFILE 'scripts\sysup.ps1')
Link (Join-Path $src 'stay-awake.ps1')  (Join-Path $env:USERPROFILE 'scripts\stay-awake.ps1')
Link (Join-Path $src 'winget-skip.txt') (Join-Path $env:USERPROFILE '.config\sysup\winget-skip.txt')

'PowerShell-Profil:'
$ctt = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'PowerShell\CTTcustom.ps1'
if (-not ((Test-Path $ctt) -and (Select-String -Path $ctt -Pattern 'function sysup' -Quiet))) {
    New-Item -ItemType Directory -Force (Split-Path $ctt) | Out-Null
    Add-Content $ctt "`n# sysup – Windows-Gegenstück zum Linux-sysup (dotfiles/windows)`nfunction sysup { & `"`$env:USERPROFILE\scripts\sysup.ps1`" @args }"
    "  + sysup in $ctt"
} else { "  = sysup schon in $ctt" }
# Windows PowerShell 5.1 hat ein eigenes Profil -> dort an pwsh weiterreichen
$ps5 = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'WindowsPowerShell\Microsoft.PowerShell_profile.ps1'
if (-not ((Test-Path $ps5) -and (Select-String -Path $ps5 -Pattern 'function sysup' -Quiet))) {
    New-Item -ItemType Directory -Force (Split-Path $ps5) | Out-Null
    Add-Content $ps5 "`n# sysup – Windows-Gegenstück zum Linux-sysup (dotfiles/windows); braucht PowerShell 7`nfunction sysup { pwsh -NoProfile -File `"`$env:USERPROFILE\scripts\sysup.ps1`" @args }"
    "  + sysup in $ps5 (leitet an pwsh weiter)"
} else { "  = sysup schon in $ps5" }

'winget-Modul:'
if (Get-Module -ListAvailable Microsoft.WinGet.Client) { '  = Microsoft.WinGet.Client vorhanden' }
else { Install-PSResource Microsoft.WinGet.Client -Scope AllUsers -TrustRepository; '  + Microsoft.WinGet.Client installiert' }

'Neustart-Schutz:'
$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
New-Item $au -Force | Out-Null
Set-ItemProperty $au NoAutoUpdate 0 -Type DWord
Set-ItemProperty $au AUOptions 3 -Type DWord          # 3 = herunterladen + melden, nie selbst installieren
Set-ItemProperty $au NoAutoRebootWithLoggedOnUsers 1 -Type DWord
Set-ItemProperty $au AlwaysAutoRebootAtScheduledTime 0 -Type DWord
$wu = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
Set-ItemProperty $wu SetActiveHours 1 -Type DWord
Set-ItemProperty $wu ActiveHoursStart 8 -Type DWord   # Windows erlaubt höchstens 18 h
Set-ItemProperty $wu ActiveHoursEnd 2 -Type DWord
'  ✓ WU installiert/startet nie selbst, Nutzungszeit 08–02'

'Uhr:'
$tz = 'HKLM:\SYSTEM\CurrentControlSet\Control\TimeZoneInformation'
Remove-ItemProperty $tz RealTimeIsUniversal -EA SilentlyContinue   # evtl. alter QWORD-Wert
New-ItemProperty $tz -Name RealTimeIsUniversal -Value 1 -PropertyType DWord -Force | Out-Null
'  ✓ Hardware-Uhr = UTC (Gentoo: /etc/conf.d/hwclock clock="UTC")'
