#Requires -Version 7
<#
.SYNOPSIS
  sysup für Windows – Gegenstück zum Linux-sysup aus den Dotfiles.

.DESCRIPTION
  Aktualisiert alles, was sich ohne Neustart aktualisieren lässt, und schreibt
  am Ende eine Zusammenfassung wie das Linux-sysup (↑ Update, + neu, − entfernt).

  Jobs:
    dotfiles  git pull des Dotfiles-Repos (~\Projects\dotfiles, Forgejo = SSOT)
    winget    alle winget-Pakete (Ausnahmen: ~/.config/sysup/winget-skip.txt)
    windows   Windows Update inkl. Treiber (installiert, startet NIE neu;
              Feature-Upgrades werden übersprungen)
    store     Microsoft-Store-Apps (Store-Updatescan anstossen)
    defender  Defender-Signaturen
    wsl       WSL-Kernel + apt in allen WSL-Distros
    dev       rustup, npm -g, scoop, choco (startet erst nach winget, damit sich
              die MSI-Installer nicht gegenseitig blockieren)

  Logs und Zusammenfassungen: %LOCALAPPDATA%\sysup (die letzten 10 Läufe).
  Während des Laufs bricht ein Wächter angekündigte (zeitverzögerte) Neustarts ab.
  Sofortige Neustarts eines Installers kann er nicht abfangen; solche Pakete
  stehen mit "~" in winget-skip.txt und laufen nur mit -Attended.

.EXAMPLE
  sysup                       # alles
  sysup -Only winget,windows  # nur diese Jobs
  sysup -Skip store           # alles ausser Store
  sysup -Attended             # auch Hersteller-Installer, die evtl. sofort neu starten
#>
[CmdletBinding()]
param(
    [string[]]$Only,
    [string[]]$Skip,
    [switch]$Attended
)
# "a,b" kommt über pwsh -File als ein String an -> aufteilen und prüfen
$valid = 'dotfiles','winget','windows','store','defender','wsl','dev'
$Only = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object Trim | Where-Object { $_ })
$Skip = @($Skip | ForEach-Object { $_ -split ',' } | ForEach-Object Trim | Where-Object { $_ })
$bad = @($Only + $Skip | Where-Object { $_ -notin $valid })
if ($bad) { Write-Host "Unbekannte Jobs: $($bad -join ', ') (gültig: $($valid -join ', '))" -ForegroundColor Red; exit 2 }

$ErrorActionPreference = 'Continue'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'sysup braucht Admin-Rechte – starte erhöht neu …' -ForegroundColor Yellow
    $a = @('-NoProfile','-NoExit','-File',"`"$PSCommandPath`"")
    if ($Only) { $a += '-Only'; $a += ($Only -join ',') }
    if ($Skip) { $a += '-Skip'; $a += ($Skip -join ',') }
    if ($Attended) { $a += '-Attended' }
    Start-Process (Get-Process -Id $PID).Path -Verb RunAs -ArgumentList $a
    return
}

$userHome = $env:USERPROFILE   # nicht $PWD: erhöht startet die Shell oft in System32
$cfgDir   = Join-Path $userHome '.config\sysup'
$logDir   = Join-Path $userHome 'AppData\Local\sysup'
$stamp    = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType Directory -Force $logDir, $cfgDir | Out-Null

$jobs = $valid
if ($Only) { $jobs = $jobs | Where-Object { $_ -in $Only } }
if ($Skip) { $jobs = $jobs | Where-Object { $_ -notin $Skip } }
$jobs = @($jobs)

# --- Ausnahmeliste für winget ---------------------------------------------------
# Eine ID pro Zeile, Text nach # = Begründung. "~ID" = nur mit -Attended.
$skipFile = Join-Path $cfgDir 'winget-skip.txt'
if (-not (Test-Path $skipFile)) {
@'
# winget-IDs, die sysup NICHT aktualisiert (eine pro Zeile, Text nach # = Begründung)
# "~ID" = nur mit "sysup -Attended" (Installer kann sofort neu starten)
TeamSpeakSystems.TeamSpeakClient.Beta.6   # winget kennt die installierte Beta-Version falsch (5.0.0) -> würde downgraden
'@ | Set-Content $skipFile -Encoding utf8
}
$skipNotes = @()
try {
    $entries = Get-Content $skipFile -EA Stop | ForEach-Object { ($_ -replace '#.*$','').Trim() } | Where-Object { $_ }
    $wingetSkip = @($entries | Where-Object { $_ -notlike '~*' })
    $attendedOnly = @($entries | Where-Object { $_ -like '~*' } | ForEach-Object { $_.Substring(1).Trim() })
    if (-not $Attended) { $wingetSkip += $attendedOnly }
} catch {
    # ohne Ausnahmeliste lieber gar nicht als z.B. TeamSpeak downgraden
    $skipNotes += "  ✗ winget-skip.txt nicht lesbar ($($_.Exception.Message)) – winget-Job ausgelassen"
    $jobs = @($jobs | Where-Object { $_ -ne 'winget' })
}

# Pakete, die die laufende Shell abschiessen würden: nur aktualisieren, wenn sysup
# nicht selbst daraus läuft (MSIX-Update beendet alle Prozesse des Pakets).
$selfHosted = @()
$exe = (Get-Process -Id $PID).Path
if ($exe -like '*WindowsApps*PowerShell*') { $selfHosted += 'Microsoft.PowerShell' }
if (Get-Process WindowsTerminal -EA SilentlyContinue) { $selfHosted += 'Microsoft.WindowsTerminal' }

# --- Job-Definitionen -----------------------------------------------------------
# Jeder Job läuft als eigener pwsh-Prozess mit Log; Zeilen mit "  " vorne sind
# Berichtszeilen für die Zusammenfassung, der Rest ist Fortschritt.
$jobScripts = @{}

$jobScripts.dotfiles = {
    param($repo)
    if (-not (Test-Path (Join-Path $repo '.git'))) { "  ⊘ kein Dotfiles-Repo unter $repo"; return }
    $old = git -C $repo rev-parse HEAD
    $o = git -C $repo pull --ff-only 2>&1 | Out-String
    $rc = $LASTEXITCODE
    if ($rc -ne 0) { "  ✗ git pull: $(($o.Trim() -split "`r?`n")[0])"; return }
    $new = git -C $repo rev-parse HEAD
    if ($old -eq $new) { "  ✓ Dotfiles aktuell"; return }
    "    ↑ Dotfiles $($old.Substring(0,7)) → $($new.Substring(0,7)) ($(git -C $repo rev-list --count "$old..$new") Commits)"
    git -C $repo log --format='      %s' "$old..$new" -- windows
}

$jobScripts.winget = {
    param($skip, $selfHosted, $snapBefore, $snapAfter)
    # Modul muss zur App-Installer-Version passen, sonst "Failed to create instance"
    try { Update-PSResource Microsoft.WinGet.Client -Scope AllUsers -TrustRepository -EA Stop }
    catch { try { Install-PSResource Microsoft.WinGet.Client -Scope AllUsers -TrustRepository -EA Stop } catch { "… Modul-Update fehlgeschlagen: $_" } }
    Import-Module Microsoft.WinGet.Client -EA Stop
    try { $null = Get-WinGetPackage -Id Microsoft.AppInstaller -EA Stop }
    catch { throw "WinGet-Modul $((Get-Module Microsoft.WinGet.Client).Version) spricht nicht mit winget $(winget --version): $_ (Abhilfe: Repair-WinGetPackageManager -Latest)" }
    # MSIX-Pakete ohne Quelle haben die Version in der ID -> herausnehmen, sonst
    # erscheint jedes Update als "+ neu" und "− entfernt"
    $snap = { Get-WinGetPackage -EA SilentlyContinue | Where-Object Id | ForEach-Object {
        [pscustomobject]@{ Id = ($_.Id -replace '^(MSIX\\[^_]+)_\d+(\.\d+)*_', '$1_'); Name = $_.Name; Version = $_.InstalledVersion } } }
    & $snap | Export-Clixml $snapBefore
    $ups = Get-WinGetPackage | Where-Object { $_.IsUpdateAvailable }
    foreach ($p in $ups) {
        if ($p.Id -in $skip)       { "  ⊘ übersprungen (winget-skip.txt): $($p.Name) $($p.InstalledVersion) -> $($p.AvailableVersions[0])"; continue }
        if ($p.Id -in $selfHosted) { "  ⊘ übersprungen (läuft gerade, sysup würde sich selbst beenden): $($p.Name)"; continue }
        "… $($p.Name) $($p.InstalledVersion) -> $($p.AvailableVersions[0])"
        foreach ($try in 1, 2) {
            $out = winget upgrade --id $p.Id --exact --silent --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | Out-String
            $rc = $LASTEXITCODE
            $out -split "`r?`n" | Where-Object { $_ -match '\S' -and $_ -notmatch '^\s*[-\\|/]\s*$' -and $_ -notmatch '[█▒]' } | Select-Object -Last 4
            # 0x8A150102 = Installer meldet Fehler, meist 1618 (andere Installation läuft) -> einmal wiederholen
            if ($rc -eq -1978334974 -and $try -eq 1) { "… andere Installation läuft, neuer Versuch in 30 s"; Start-Sleep 30; continue }
            break
        }
        # 0x8A15002B = kein passendes Update mehr (z.B. schon von choco/WU erledigt) -> kein Fehler
        if ($rc -ne 0 -and $rc -ne -1978335189) {
            $why = switch ($rc) { -1978335226 { 'Installer fehlgeschlagen' } -1978334974 { 'Installer meldet Fehler/läuft gerade' } default { 'winget-Fehler' } }
            "  ! $($p.Name): $why (Exit-Code 0x$('{0:X8}' -f $rc))"
        }
    }
    & $snap | Export-Clixml $snapAfter
}

$jobScripts.windows = {
    $s = New-Object -ComObject Microsoft.Update.Session
    $s.ClientApplicationID = 'sysup'
    $searcher = $s.CreateUpdateSearcher()
    "… suche Updates (Software + Treiber)"
    try { $r = $searcher.Search("IsInstalled=0 and IsHidden=0") } catch { "  ✗ Suche fehlgeschlagen: $_"; return }
    $todo = New-Object -ComObject Microsoft.Update.UpdateColl
    $pending = 0
    foreach ($u in $r.Updates) {
        # schon installiert, wartet nur auf den Neustart (zählt bis dahin als "nicht installiert")
        if ($u.RebootRequired) { "  ⟳ wartet auf Neustart: $($u.Title)"; $pending++; continue }
        # Feature-Upgrades brauchen mehrere Neustarts und stellen gern die Bootreihenfolge um
        if (@($u.Categories | ForEach-Object Name) -contains 'Upgrades') { "  ⊘ Feature-Upgrade übersprungen (manuell einspielen): $($u.Title)"; continue }
        # Treiber (Type 2) markieren sich oft als "kann Eingabe verlangen", installieren aber still
        if ($u.InstallationBehavior.CanRequestUserInput -and $u.Type -ne 2) { "  ⊘ braucht Eingabe, übersprungen: $($u.Title)"; continue }
        if (-not $u.EulaAccepted) { $u.AcceptEula() }
        [void]$todo.Add($u)
    }
    if ($todo.Count -eq 0) { if (-not $pending) { "  ✓ Windows ist aktuell" }; return }
    "… lade $($todo.Count) Updates"
    $d = $s.CreateUpdateDownloader(); $d.Updates = $todo; [void]$d.Download()
    $inst = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($u in $todo) { if ($u.IsDownloaded) { [void]$inst.Add($u) } else { "  ! Download fehlgeschlagen: $($u.Title)" } }
    if ($inst.Count -eq 0) { return }
    "… installiere $($inst.Count) Updates"
    $i = $s.CreateUpdateInstaller(); $i.Updates = $inst; $i.ForceQuiet = $true
    $res = $i.Install()
    for ($n = 0; $n -lt $inst.Count; $n++) {
        $rc = $res.GetUpdateResult($n).ResultCode   # 2=ok 3=ok mit Fehlern 4=Fehler 5=abgebrochen
        $t = $inst.Item($n).Title
        switch ($rc) { 2 { "    ↑ $t" } 3 { "    ↑ $t (mit Fehlern)" } default { "  ✗ $t (Ergebnis $rc)" } }
    }
    if ($res.RebootRequired) { "  ⟳ Neustart nötig, um die Windows-Updates abzuschliessen (sysup startet nie selbst neu)" }
}

$jobScripts.store = {
    "… stosse Store-Updatescan an"
    try {
        $c = Get-CimInstance -Namespace root\cimv2\mdm\dmmap -ClassName MDM_EnterpriseModernAppManagement_AppManagement01 -EA Stop
        $r = Invoke-CimMethod -InputObject $c -MethodName UpdateScanMethod -EA Stop
        "  ✓ Store-Updates angestossen (laufen im Hintergrund weiter, Ergebnis $($r.ReturnValue))"
    } catch { "  ✗ Store-Scan fehlgeschlagen: $($_.Exception.Message)" }
}

$jobScripts.defender = {
    $before = (Get-MpComputerStatus).AntivirusSignatureVersion
    try { Update-MpSignature -EA Stop } catch { "  ✗ $($_.Exception.Message)"; return }
    $after = (Get-MpComputerStatus).AntivirusSignatureVersion
    if ($before -ne $after) { "    ↑ Defender-Signaturen  $before → $after" } else { "  ✓ Defender-Signaturen aktuell ($after)" }
}

$jobScripts.wsl = {
    # WSL_UTF8=1: wsl.exe gibt UTF-8 statt UTF-16 aus
    "… wsl --update"
    $o = wsl.exe --update 2>&1 | Out-String
    $o.Trim() -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 3 | ForEach-Object { "  $_" }
    $distros = (wsl.exe -l -q 2>&1 | Out-String) -split "`r?`n" | ForEach-Object Trim | Where-Object { $_ -and $_ -notlike 'docker-desktop*' }
    foreach ($d in $distros) {
        "… apt in $d"
        $o = wsl.exe -d $d -u root -- sh -c 'command -v apt-get >/dev/null || { echo "kein apt"; exit 0; }; export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get -y -qq -o Dpkg::Options::=--force-confold full-upgrade | grep -E "^(Setting up|Inst|E:)" ; apt-get -y -qq autoremove >/dev/null' 2>&1
        $set = @($o | Where-Object { $_ -like 'Setting up*' })
        $err = @($o | Where-Object { $_ -like 'E:*' })
        if ($set.Count) { "    ↑ $d`: $($set.Count) Pakete aktualisiert" } else { "  ✓ $d aktuell" }
        $err | ForEach-Object { "  ! $d`: $_" }
    }
}

$jobScripts.dev = {
    if (Get-Command rustup -EA SilentlyContinue) {
        "… rustup update"
        $o = rustup update 2>&1 | Out-String
        ($o -split "`r?`n") | Where-Object { $_ -match 'updated -|installed -' } | ForEach-Object { "    ↑ rustup: $($_.Trim())" }
        if ($o -match 'unchanged') { "  ✓ Rust-Toolchains aktuell" }
    }
    if (Get-Command npm -EA SilentlyContinue) {
        "… npm -g update"
        $out = (npm outdated -g --json 2>$null | Out-String)
        $old = if ($out.Trim()) { $out | ConvertFrom-Json } else { $null }
        if ($old -and $old.PSObject.Properties.Count) {
            npm update -g 2>&1 | Select-Object -Last 3
            foreach ($p in $old.PSObject.Properties) { "    ↑ npm: $($p.Name)  $($p.Value.current) → $($p.Value.latest)" }
        } else { "  ✓ npm-Globalpakete aktuell" }
    }
    if (Get-Command scoop -EA SilentlyContinue) {
        "… scoop update"
        # scoop schreibt per Write-Host -> *>&1, sonst landet nichts in $o
        scoop update *>&1 | Out-Null
        $o = scoop update '*' *>&1 | Out-String
        ($o -split "`r?`n") | Where-Object { $_ -match "^Updating '|was updated from" } | ForEach-Object { "    ↑ scoop: $($_.Trim())" }
        if ($o -match 'Latest versions for all apps are installed') { "  ✓ Scoop-Apps aktuell" }
    }
    if (Get-Command choco -EA SilentlyContinue) {
        "… choco upgrade all"
        $o = choco upgrade all -y --no-progress 2>&1 | Out-String
        ($o -split "`r?`n") | Where-Object { $_ -match 'upgraded \d+/\d+|^ - ' -and $_ -notmatch 'pending system reboot' } | ForEach-Object { "  $($_.Trim())" }
    }
}

# --- Jobs starten ---------------------------------------------------------------
$snapB = Join-Path $logDir "winget-before-$stamp.xml"
$snapA = Join-Path $logDir "winget-after-$stamp.xml"
$running = @{}
$start = Get-Date

function Start-SysupJob($j) {
    $log = Join-Path $logDir "$j-$stamp.log"
    $args_ = switch ($j) {
        'winget'   { ,@($wingetSkip, $selfHosted, $snapB, $snapA) }
        'dotfiles' { ,@(,(Join-Path $userHome 'Projects\dotfiles')) }
        default    { ,@() }
    }
    $script:running[$j] = @{
        Log = $log
        Job = Start-Job -Name "sysup-$j" -ScriptBlock {
            param($sb, $a, $log)
            # Jobs erben sonst Codepage 850 -> git/winget-Umlaute und ↑ kämen kaputt an
            [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
            $env:WSL_UTF8 = '1'
            $sb = [scriptblock]::Create($sb)
            try { & $sb @a *>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $log -Append | Out-Null; "__SYSUP_RC=0" | Add-Content $log }
            catch { "  ✗ Abbruch: $_" | Add-Content $log; "__SYSUP_RC=1" | Add-Content $log }
        } -ArgumentList $jobScripts[$j].ToString(), $args_, $log
    }
}

# dev (choco) erst nach winget: parallele MSI-Installer scheitern mit 1618
$deferred = if ('dev' -in $jobs -and 'winget' -in $jobs) { 'dev' } else { $null }

try {
    # Neustart-Wächter: bricht angekündigte Shutdowns während des Laufs ab
    Start-Job -Name 'sysup-guard' { while ($true) { shutdown.exe /a 2>$null | Out-Null; Start-Sleep 3 } } | Out-Null

    foreach ($j in $jobs) { if ($j -ne $deferred) { Start-SysupJob $j } }

    # Fortschritt: ein Balken je Job mit dessen letzter Logzeile
    while ($deferred -or ($running.Values.Job | Where-Object State -eq 'Running')) {
        if ($deferred -and $running.winget.Job.State -ne 'Running') { Start-SysupJob $deferred; $deferred = $null }
        $secs = [int]((Get-Date) - $start).TotalSeconds
        for ($i = 0; $i -lt $jobs.Count; $i++) {
            $j = $jobs[$i]
            if (-not $running.ContainsKey($j)) { $st = 'wartet auf winget'; $last = '' }
            else {
                $st = if ($running[$j].Job.State -eq 'Running') { "läuft ($secs s)" } else { 'fertig' }
                $last = if (Test-Path $running[$j].Log) { "$(Get-Content $running[$j].Log -Tail 1 -EA SilentlyContinue)".Trim() } else { 'startet …' }
            }
            # die kompakte Fortschrittsanzeige von pwsh 7 zeigt nur Activity + Status -> letzte Logzeile anhängen
            $txt = if ($last -and $running[$j].Job.State -eq 'Running') { "$st – $($last -replace '^[…\s]+','')" } else { $st }
            Write-Progress -Id ($i + 1) -Activity $j.PadRight(8) -Status $txt.Substring(0, [Math]::Min(80, $txt.Length))
        }
        Start-Sleep 2
    }
} finally {
    # auch bei Ctrl+C: Wächter und laufende Jobs nicht als Leichen zurücklassen
    for ($i = 0; $i -lt $jobs.Count; $i++) { Write-Progress -Id ($i + 1) -Activity $jobs[$i] -Completed }
    Get-Job -Name 'sysup-*' | Where-Object State -eq 'Running' | Stop-Job
    Get-Job -Name 'sysup-guard' | Remove-Job -Force
}

# --- Zusammenfassung ------------------------------------------------------------
$summary = New-Object System.Collections.Generic.List[string]
$skipNotes | ForEach-Object { $summary.Add($_) }
foreach ($j in $jobs) {
    $summary.Add("--- $j ---")
    $log = $running[$j].Log
    $content = if (Test-Path $log) { Get-Content $log } else { @() }
    if ($j -eq 'winget' -and (Test-Path $snapB) -and (Test-Path $snapA)) {
        $b = @{}; Import-Clixml $snapB | ForEach-Object { $b[$_.Id] = $_ }
        $a = @{}; Import-Clixml $snapA | ForEach-Object { $a[$_.Id] = $_ }
        $up = foreach ($k in $a.Keys) { if ($b.ContainsKey($k) -and $b[$k].Version -ne $a[$k].Version) { '    ↑ {0,-45} {1} → {2}' -f $a[$k].Name, $b[$k].Version, $a[$k].Version } }
        $new = foreach ($k in $a.Keys) { if (-not $b.ContainsKey($k)) { '    + {0,-45} {1}' -f $a[$k].Name, $a[$k].Version } }
        $gone = foreach ($k in $b.Keys) { if (-not $a.ContainsKey($k)) { '    − {0,-45} {1}' -f $b[$k].Name, $b[$k].Version } }
        $hdr = @(); if ($up) { $hdr += "↑ $(@($up).Count) Updates" }; if ($new) { $hdr += "+ $(@($new).Count) neu" }; if ($gone) { $hdr += "− $(@($gone).Count) entfernt" }
        $summary.Add('  ' + $(if ($hdr) { $hdr -join '  ' } else { 'keine Paketänderungen' }))
        @($up) + @($new) + @($gone) | Where-Object { $_ } | Sort-Object | ForEach-Object { $summary.Add($_) }
        $content | Where-Object { $_ -match '^  [!⊘✗]' } | ForEach-Object { $summary.Add($_) }
    } else {
        $content | Where-Object { $_ -match '^  ' } | ForEach-Object { $summary.Add($_) }
    }
    if (-not ($content -match '^__SYSUP_RC=')) { $summary.Add('  ✗ nicht regulär beendet') }
    $summary.Add("  volles Log: $log")
    $summary.Add('')
}
$reboot = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or
          (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')
if ($reboot) { $summary.Add('⟳ Neustart ausstehend – bald bewusst neu starten und Windows im Bootmenü wählen (sysup startet nie selbst neu)') }
$summary.Add("✓ Done in $([int]((Get-Date)-$start).TotalSeconds)s")

$sumFile = Join-Path $logDir "summary-$stamp.txt"
$summary | Set-Content $sumFile -Encoding utf8
foreach ($l in $summary) {
    $c = switch -Regex ($l) {
        '^---'        { 'White'; break }
        'volles Log'  { 'DarkGray'; break }
        '✗'           { 'Red'; break }
        '^\s*↑'       { 'Cyan'; break }
        '^\s*\+'      { 'Green'; break }
        '^\s*−'       { 'Red'; break }
        '!|⊘|⟳'       { 'Yellow'; break }
        '↑'           { 'Cyan'; break }
        default       { 'Gray' }
    }
    Write-Host $l -ForegroundColor $c
}
Write-Host "(Zusammenfassung: $sumFile)" -ForegroundColor DarkGray
Get-Job -Name 'sysup-*' | Remove-Job -Force

# Aufräumen: je Art die letzten 10 Läufe behalten
Get-ChildItem $logDir -File | Group-Object { $_.Name -replace '-\d{8}-\d{6}.*$','' } | ForEach-Object {
    $_.Group | Sort-Object LastWriteTime -Descending | Select-Object -Skip 10 | Remove-Item -Force
}
