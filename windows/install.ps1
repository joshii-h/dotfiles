<#
  Richtet die Windows-Seite der Dotfiles ein (Dualboot-Battlestation).
  Erhöht ausführen:  pwsh -File "$env:USERPROFILE\Projects\dotfiles\windows\install.ps1"

  - verlinkt die Skripte nach %USERPROFILE%\scripts (Symlinks -> `git pull` bzw.
    der sysup-Job "dotfiles" hält sie aktuell)
  - legt %USERPROFILE%\.config\sysup\winget-skip.txt an (Symlink auf das Repo)
  - trägt die Funktion `sysup` in Documents\PowerShell\CTTcustom.ps1 ein
    (das Chris-Titus-Profil überschreibt sich selbst, CTTcustom.ps1 bleibt)
  - setzt den Neustart-Schutz: kein Auto-Neustart nach Updates, solange jemand
    angemeldet ist (Neustart würde in Gentoo landen)
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

'Neustart-Schutz:'
$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
New-Item $au -Force | Out-Null
Set-ItemProperty $au NoAutoRebootWithLoggedOnUsers 1 -Type DWord
Set-ItemProperty $au AlwaysAutoRebootAtScheduledTime 0 -Type DWord
$wu = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
Set-ItemProperty $wu SetActiveHours 1 -Type DWord
Set-ItemProperty $wu ActiveHoursStart 6 -Type DWord
Set-ItemProperty $wu ActiveHoursEnd 2 -Type DWord
'  ✓ kein Auto-Neustart, Nutzungszeit 06–02'

'Uhr:'
New-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\TimeZoneInformation' -Name RealTimeIsUniversal -Value 1 -PropertyType QWord -Force | Out-Null
'  ✓ Hardware-Uhr = UTC (Gentoo: /etc/conf.d/hwclock clock="UTC")'
