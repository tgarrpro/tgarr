# tgarr on a computer of your own: Linux, macOS, Windows

tgarr is Linux software in containers. On Linux the installer starts them. On a Mac and on
Windows there is no way around a small Linux machine underneath - every "Docker for the
desktop" is one - so the installer sets one up and puts tgarr inside it: **Colima** on macOS,
**WSL2** on Windows. You get a page at `http://localhost:8765`; what runs underneath need not
concern you again.

| | Tried on | State |
|---|---|---|
| Linux | a new Ubuntu 24.04 with nothing on it | works |
| Windows 10 / 11 (WSL2) | a Windows 11 PC, from the double-click to the page: WSL2 switched on, the restart, going on by itself after it, starting with Windows, reachable from the other machines in the house. Not yet on Windows 10. | works; new in 0.5.11 |
| macOS (Colima) | **not yet run on a real Mac** | new in 0.5.11, unverified |

If an installer goes wrong for you - the macOS one above all - what it printed is what we need:
[open an issue](https://github.com/tgarrpro/tgarr/issues).

## Linux

```bash
curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/install/install.sh | bash
```

It installs into `~/tgarr`. If Docker is missing it asks before installing it (Docker's own
script from get.docker.com); without a terminal to ask on, it stops and says so.

## macOS

Download `install-tgarr-macos.zip` from the [latest release](https://github.com/tgarrpro/tgarr/releases/latest), open it, and double-click `install-tgarr.command` (the first time: right-click → *Open*, since it is not signed) - or paste the Linux line into Terminal. It needs [Homebrew](https://brew.sh). It installs `colima` and
`docker`, starts Colima (2 processors, 4 GB of memory) and has it start again at login, then
installs tgarr inside Colima's Linux, in `/opt/tgarr`. The database lives there, on the
Linux side, where it is fast and its permissions are right. What tgarr fetches is put in
`~/tgarr/downloads` on the Mac, where Finder sees it.

- `docker ps`, `docker logs -f tgarr-api` work from the Mac's Terminal.
- `colima stop` / `colima start` stop and start everything.
- More memory for a big library: `TGARR_COLIMA_MEMORY=8` before the install line, or
  `colima stop && colima start --memory 8`.

## Windows 10 (2004 or later) and 11

Download `install-tgarr.cmd` from the [latest release](https://github.com/tgarrpro/tgarr/releases/latest)
and double-click it. (Or, in PowerShell: `irm https://raw.githubusercontent.com/tgarrpro/tgarr/main/install/install.ps1 | iex`.)
Windows asks once whether the installer may make changes; it need not be started "as administrator".

(The installer was written so that antivirus software has nothing to object to - it does not
hide anything it does, and it was checked against Windows' own. If yours blocks it all the
same, tell us what it said.)

Windows may first say that the file is not commonly downloaded, or "Windows protected your
PC": it says that of every script that no company has paid to sign. *More info → Run
anyway*. The file is four lines; open it in Notepad to see them.

1. If WSL2 is not switched on, it switches it on and offers to restart Windows; when you
   have logged in again it goes on by itself (and asks for permission once more).
2. It installs Ubuntu in WSL (if there is none), and Docker and tgarr inside it, in `/opt/tgarr`.
3. It adds a scheduled task, `tgarr`, that starts it with Windows - in the background, before
   anybody logs in, without a window.
4. It asks whether phones, tablets and televisions at home may reach it (Enter is yes). If
   yes, it adds a firewall rule that lets in home-network addresses only, never the internet,
   and a port forward for port 8765. Say no and tgarr answers on that PC only.

tgarr's files are at `\\wsl$\Ubuntu\opt\tgarr` in Explorer; what it fetched is in
`_data\downloads` there. How to remove everything again is written at the end of
[install.ps1](../../install/install.ps1).

## Settings

All optional; set them before the install line (`NAME=value` in front of `bash` on Linux and
macOS). On Windows: `$env:NAME = "value"` before running, or as parameters of `install.ps1`
(`-Port`, `-Distro`, `-Lan`, `-Ref`).

| | |
|---|---|
| `TGARR_PORT` | port of the page (8765) |
| `TGARR_DIR` | Linux: where to install (`~/tgarr`) |
| `TGARR_DOWNLOADS` | Linux, macOS: where fetched files are kept |
| `TGARR_VERSION` | stay on one version, for example `0.5.11`, instead of following new ones |
| `TGARR_INSTALL_DOCKER=1` | Linux: install Docker without asking |
| `TGARR_LAN=yes` / `no` | Windows: reachable from other devices, without being asked |
| `TGARR_DISTRO` | Windows: the WSL distribution to use (`Ubuntu`) |

## Afterwards

- **Updates** come by themselves within the hour of a release. *Settings → Updating tgarr* has it look at once.
- **Run the installer again** at any time: it brings `docker-compose.yml` up to date, starts
  what is not running, and leaves `.env` and your data alone.
- **Your data** is the `_data` folder beside `docker-compose.yml`. Copy it, with `.env`, and you have a backup.
