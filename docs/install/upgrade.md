# Updating tgarr

tgarr can update itself. If the Dashboard says a newer version is available
and this install has not updated in hours, its updater is missing or not
running. This page gets you onto the current version and makes the next
updates automatic.

Any version can go straight to the newest one — there is no need to step
through the versions in between. Your index, settings and Telegram sign-in
are kept; the database is brought up to date on start.

## 1. Update now

In the folder that holds your `docker-compose.yml`:

```bash
docker compose pull
docker compose up -d
```

If `pull` says there is nothing to pull for `crawler` and `api`, your install
builds tgarr from source instead of using the published images — see
[Installs made by cloning the repository](#installs-made-by-cloning-the-repository).

## 2. Make updates automatic

Check the updater:

```bash
docker ps -a --filter name=tgarr-watchtower
```

**No such container.** Your compose file has no updater, or never started it
(true of most installs made before 0.5.6). Replace the compose file with the
current one — your data lives in `_data/` next to it and is not touched:

```bash
cp docker-compose.yml docker-compose.yml.old
curl -fsSLO https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml
docker compose up -d
```

If you had changed the old file (a different port, a downloads folder
elsewhere), set those in `.env` instead — see `.env.example` for
`TGARR_PORT` and `TGARR_DOWNLOADS` — rather than editing the new file.

**`Restarting`.** The updater cannot talk to your Docker engine. Look at why:

```bash
docker logs --tail 5 tgarr-watchtower
```

`client version 1.25 is too old` means your compose file predates the fix
of 12 November 2026: download it again as above. If the message names a
different version, set the one your engine accepts (`docker version` shows
it) in `.env`:

```
TGARR_DOCKER_API=1.44
```

**`Up`, but tgarr still does not update.** You may have pinned a version:
look for `TGARR_TAG=` in `.env` and remove the line to follow new versions.

## Installs made by cloning the repository

Before 0.5.6 the quick start had you clone the repository and build the
images yourself. Such an install only changes when you update it by hand.
To move to the published images (and automatic updates), from the folder
you cloned into:

```bash
docker compose down
git pull
docker compose up -d
```

`git pull` brings the new `docker-compose.yml`, which uses the published
images and includes the updater. `_data/` and `.env` stay as they are. If
`git pull` refuses because you edited `docker-compose.yml`, keep a copy of
your version (`cp docker-compose.yml docker-compose.yml.old`), run
`git checkout docker-compose.yml`, and pull again.

To keep building from source, use `docker-compose.dev.yml`:

```bash
git pull
docker compose -f docker-compose.dev.yml up -d --build
```

## Not wanting the reminder

Set `TGARR_UPDATE_CHECK=false` in `.env`. tgarr then no longer asks
tgarr.me which version is current. (The question sends nothing about your
install.)
