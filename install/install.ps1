<#
tgarr installer for Windows 10 (2004 or later) and Windows 11.

Download install-tgarr.cmd from tgarr's releases page and double-click it; it fetches this
file and runs it. How to run it by hand, and what it does step by step:
https://github.com/tgarrpro/tgarr/blob/main/docs/install/desktop.md

Windows asks once for permission ("Do you want to allow this app to make changes?") - the
script asks for that itself, it need not be started "as Administrator". tgarr itself is Linux
software; on Windows it runs inside WSL2, the Linux that Windows carries. This script:

  1. switches WSL2 on if it is not (Windows then needs a restart; the script goes on by itself
     when you have logged in again),
  2. makes sure there is an Ubuntu in it,
  3. runs tgarr's Linux installer inside that Ubuntu (Docker, and tgarr's containers),
  4. has it start with Windows (a scheduled task named "tgarr"),
  5. if you say so, lets the other devices at home reach it (a firewall rule and a port forward).

Nothing else on the machine is changed. To remove it all again: see the end of this file.

Settings, all optional - as parameters, or as environment variables TGARR_PORT, TGARR_DISTRO,
TGARR_LAN, TGARR_REF set before running:
    -Port 8765          port of the web page
    -Distro Ubuntu      which WSL distribution to use
    -Lan yes | no       reachable from other devices, without being asked
    -Ref main           which branch or tag the files are taken from
#>
param([int]$Port = 0, [string]$Distro = "", [string]$Lan = "", [string]$Ref = "", [switch]$KeepOpen)
$ErrorActionPreference = "Stop"

function Say($t)  { Write-Host "==> $t" -ForegroundColor Cyan }
function Note($t) { Write-Host "    $t" }
function Done($code) {
    if ($KeepOpen) { Write-Host ""; Read-Host "Press Enter to close this window" | Out-Null }
    exit $code
}
function Die($t)  { Write-Host "tgarr install: $t" -ForegroundColor Red; Done 1 }

if (-not $Port)   { $Port   = if ($env:TGARR_PORT)   { [int]$env:TGARR_PORT } else { 8765 } }
if (-not $Distro) { $Distro = if ($env:TGARR_DISTRO) { $env:TGARR_DISTRO }    else { "Ubuntu" } }
if (-not $Ref)    { $Ref    = if ($env:TGARR_REF)    { $env:TGARR_REF }       else { "main" } }
if (-not $Lan)    { $Lan    = "$env:TGARR_LAN" }
if ($Port -lt 1 -or $Port -gt 65535) { Die "the port must be between 1 and 65535" }
if ($Distro -notmatch '^[A-Za-z0-9._-]+$') { Die "'$Distro' is not a distribution's name" }
if ($Ref -notmatch '^[A-Za-z0-9._/-]+$')   { Die "'$Ref' is not a branch or a tag" }
if ($Lan -and $Lan -notmatch '^(y|yes|n|no)$') { Die "-Lan is yes or no" }
$Raw = "https://raw.githubusercontent.com/tgarrpro/tgarr/$Ref"

# This script as a file on this machine. It is started a second time with administrator's
# rights, and a third time after the restart - from the file, with the same settings.
$Home_ = Join-Path $env:LOCALAPPDATA "tgarr"
$Self  = $PSCommandPath
if (-not $Self) {
    New-Item -ItemType Directory -Path $Home_ -Force | Out-Null
    $Self = Join-Path $Home_ "install.ps1"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri "$Raw/install/install.ps1" -OutFile $Self
}
$Args_ = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$Self`"", "-Port", "$Port", "-Distro", $Distro, "-Ref", $Ref, "-KeepOpen")
if ($Lan) { $Args_ += @("-Lan", $Lan) }

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    # WSL, the scheduled task and the firewall rule need an administrator: ask Windows for it
    Say "asking Windows for permission to install (the installation goes on in a window of its own)"
    try {
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $Args_
    } catch {
        Die "permission was not given. Nothing was changed."
    }
    exit 0
}
if ([Environment]::OSVersion.Version.Build -lt 19041) { Die "WSL2 needs Windows 10 version 2004 or later" }

# wsl.exe writes UTF-16; without this its output cannot be read here
$env:WSL_UTF8 = "1"
function WslOut([string[]]$a) { $o = & wsl.exe @a 2>$null; if ($LASTEXITCODE -ne 0) { return $null }; return ($o -join "`n") }

# ---- 1. WSL2 -----------------------------------------------------------------------------------
$feat = @("Microsoft-Windows-Subsystem-Linux", "VirtualMachinePlatform") |
    ForEach-Object { Get-WindowsOptionalFeature -Online -FeatureName $_ }
if ($feat | Where-Object { $_.State -ne "Enabled" }) {
    Say "switching on WSL2 (the Linux that Windows carries)"
    & wsl.exe --install --no-distribution
    # After the restart, at the next log-in, go on from here: Windows runs what is named under
    # RunOnce a single time and forgets it. (An entry there may be 260 characters long at
    # most, so it names a small file that holds the command.)
    New-Item -ItemType Directory -Path $Home_ -Force | Out-Null
    $resume = Join-Path $Home_ "resume-install.cmd"
    Set-Content -Path $resume -Encoding ASCII -Value "@echo off`r`ntitle tgarr`r`npowershell.exe $($Args_ -join ' ')`r`n"
    $key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce"
    if (-not (Test-Path $key)) { New-Item -Path $key | Out-Null }       # (never -Force here: it would empty a key that exists)
    Set-ItemProperty -Path $key -Name "tgarr-install" -Value "`"$resume`""
    Write-Host ""
    Say "Windows has to restart once. When you have logged in again the installation goes on by itself"
    Note "(Windows will ask for permission once more). Save your work first."
    $now = Read-Host "Restart now? [y/N]"
    if ($now -match '^(y|yes)$') {
        # not forced: a program with unsaved work may still hold the restart back and ask
        & shutdown.exe /r /t 10 /c "Restarting to switch on WSL2; tgarr's installation goes on after you log in."
        exit 0
    }
    Note "Restart when it suits you."
    Done 0
}
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce" -Name "tgarr-install" -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $Home_ "resume-install.cmd") -ErrorAction SilentlyContinue
& wsl.exe --set-default-version 2 *> $null

# ---- 2. an Ubuntu ------------------------------------------------------------------------------
$list = WslOut @("-l", "-q")
$names = @(); if ($list) { $names = $list -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ } }
if ($names -notcontains $Distro) {
    Say "installing $Distro in WSL (a few hundred MB)"
    & wsl.exe --install -d $Distro --no-launch
    if ($LASTEXITCODE -ne 0) { Die "could not install $Distro. Try 'wsl --update' and run this again." }
    # the first start sets it up; as root, so that no user name and password are asked for
    & wsl.exe -d $Distro -u root -- true
    if ($LASTEXITCODE -ne 0) { Die "$Distro was installed but does not start. Open it once from the Start menu, then run this again." }
}
$ver = WslOut @("-l", "-v")
if ($ver -and ($ver -split "`n" | Where-Object { $_ -match "\b$([regex]::Escape($Distro))\b" -and $_ -match "\b1\s*$" })) {
    Say "$Distro is a WSL1 distribution: converting it to WSL2"
    & wsl.exe --set-version $Distro 2
}

# ---- 3. tgarr, inside it -----------------------------------------------------------------------
Say "installing tgarr inside $Distro (Docker first, if it is not there)"
$inside = "export TGARR_DIR=/opt/tgarr TGARR_PORT=$Port TGARR_REF='$Ref' TGARR_INSTALL_DOCKER=1 TGARR_INSIDE=wsl; " +
          "command -v curl >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq curl ca-certificates); " +
          "curl -fsSL '$Raw/install/install.sh' | bash"
& wsl.exe -d $Distro -u root -- bash -lc $inside
if ($LASTEXITCODE -ne 0) { Die "the installer inside $Distro did not finish. What it said is above." }

# ---- 4. started with Windows ---------------------------------------------------------------------
# WSL stops a distribution nobody is using; this keeps one quiet process in it, and starts
# Docker where there is no systemd to do it.
Say "starting it with Windows (scheduled task 'tgarr')"
$keep = "service docker start >/dev/null 2>&1; systemctl start docker >/dev/null 2>&1; exec sleep infinity"
$act  = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distro -u root -- sh -c `"$keep`""
$trg  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$set  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$prn  = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "tgarr" -Action $act -Trigger $trg -Settings $set -Principal $prn -Force | Out-Null
Start-ScheduledTask -TaskName "tgarr"

# ---- 5. the other devices at home ----------------------------------------------------------------
$lan = $Lan
if (-not $lan) {
    Write-Host ""
    Write-Host "tgarr answers on this PC only. Phones, tablets and televisions at home reach it if Windows"
    Write-Host "lets them in on port $Port (a firewall rule for private networks, and a port forward)."
    $lan = Read-Host "Let the other devices at home reach it? [y/N]"
}
$opened = $false
if ($lan -match '^(y|yes)$') {
    Say "letting the home network in on port $Port"
    & netsh.exe interface portproxy delete v4tov4 listenport=$Port listenaddress=0.0.0.0 *> $null
    & netsh.exe interface portproxy add v4tov4 listenport=$Port listenaddress=0.0.0.0 connectport=$Port connectaddress=127.0.0.1 | Out-Null
    Get-NetFirewallRule -DisplayName "tgarr" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName "tgarr" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
    $opened = $true
}

Write-Host ""
Say "tgarr is running"
Note "here:               http://localhost:$Port"
if ($opened) {
    $ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.PrefixOrigin -in "Dhcp", "Manual" -and $_.IPAddress -notlike "169.254.*" -and $_.InterfaceAlias -notlike "vEthernet*" } |
           Select-Object -First 1).IPAddress
    if ($ip) { Note "from other devices: http://${ip}:$Port   (on a network Windows calls 'Private')" }
}
Note "its files:          \\wsl$\$Distro\opt\tgarr   (fetched files in _data\downloads)"
Note "logs:               wsl -d $Distro -u root -- docker logs -f tgarr-api"
Note "It keeps itself up to date. Nothing needs an account to begin with: open the page."
if ($KeepOpen) { Start-Process "http://localhost:$Port" }
Done 0

<#
To remove tgarr again (PowerShell as Administrator):

    Unregister-ScheduledTask -TaskName tgarr -Confirm:$false
    Get-NetFirewallRule -DisplayName tgarr | Remove-NetFirewallRule
    netsh interface portproxy delete v4tov4 listenport=8765 listenaddress=0.0.0.0
    wsl -d Ubuntu -u root -- sh -c "cd /opt/tgarr && docker compose down"
    wsl -d Ubuntu -u root -- rm -rf /opt/tgarr        # this deletes tgarr's data

If the Ubuntu was installed only for tgarr:   wsl --unregister Ubuntu
#>
