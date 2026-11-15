@echo off
rem tgarr for Windows 10 / 11 - double-click to install.
rem It fetches tgarr's installer (a PowerShell script you can read) into your temporary folder
rem and runs it; Windows asks once for permission.
rem What it does, step by step: https://github.com/tgarrpro/tgarr/blob/main/docs/install/desktop.md
title tgarr
set "TGARR_PS1=%TEMP%\tgarr-install.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = 'Tls12'; Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/tgarrpro/tgarr/main/install/install.ps1' -OutFile $env:TGARR_PS1"
if errorlevel 1 (echo Could not fetch the installer. Is this PC online? & pause & exit /b 1)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%TGARR_PS1%" -KeepOpen
