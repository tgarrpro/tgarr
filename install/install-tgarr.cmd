@echo off
rem tgarr for Windows 10 / 11 - double-click to install.
rem It fetches tgarr's installer and runs it; Windows asks once for permission.
rem What it does, step by step: https://github.com/tgarrpro/tgarr/blob/main/docs/install/desktop.md
title tgarr
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = 'Tls12'; irm 'https://raw.githubusercontent.com/tgarrpro/tgarr/main/install/install.ps1' | iex"
if errorlevel 1 pause
