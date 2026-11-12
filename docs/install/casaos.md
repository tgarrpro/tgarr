# Install tgarr on CasaOS

tgarr runs as a 3-container docker-compose stack (postgres + meilisearch + api/crawler). CasaOS supports docker-compose apps via its "Custom Install" wizard or via SSH.

**Recommended layout**: deploy tgarr on the same CasaOS box that already runs Sonarr / Radarr / Jellyfin. Sonarr can read tgarr's `/downloads/` directly with no NFS or remote-path mapping.

## Prerequisites

- CasaOS ≥ 0.4
- Docker Compose v2 (included with CasaOS)
- A Telegram account (your personal one is fine — tgarr is self-hosted and only your account ever logs in)
- ~10 GB free disk space for postgres + meilisearch indexes
- Enough free space for whatever media you plan to download

## Step 1 — get the two files

SSH into your CasaOS box:

```bash
sudo mkdir -p /DATA/AppData/tgarr/app
sudo chown -R $USER /DATA/AppData/tgarr
cd /DATA/AppData/tgarr/app
curl -fsSLO https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/tgarrpro/tgarr/main/.env.example -o .env
```

## Step 2 — get Telegram API credentials

1. Visit <https://my.telegram.org/apps>.
2. Sign in with your phone number.
3. Create a new app (any name — "tgarr-self-host" works).
4. Copy the `api_id` and `api_hash`.

## Step 3 — configure `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Fill in:

```bash
TG_API_ID=12345678
TG_API_HASH=abcdef0123456789abcdef0123456789
TG_PHONE=+12815551234

# Random secrets — generate fresh, do not reuse
DB_PASSWORD=<openssl rand -hex 24>
MEILI_MASTER_KEY=<openssl rand -hex 32>

# Where releases link back to (Sonarr fetches NZB stubs from here)
TGARR_BASE_URL=http://192.168.2.124:8765

# Optional: change the external port (default 8765)
TGARR_PORT=8765
```

Generate the secrets:

```bash
echo "DB_PASSWORD=$(openssl rand -hex 24)"        # paste into .env
echo "MEILI_MASTER_KEY=$(openssl rand -hex 32)"   # paste into .env
```

## Step 4 — start it

```bash
docker compose up -d
```

## Step 5 — sign in

In a browser, open `http://<your-host>:8765` in a browser and sign in — scan the QR code with the Telegram app, or enter your phone number and the code Telegram sends you. Nothing is typed in a terminal, and the code never touches a file.

The session is saved in `_data/session/`; you sign in once.

Expected logs:

```
[tgarr] connected as @yourhandle (id 1234567890)
[tgarr] [worker] download worker started; root=/downloads/tgarr
[tgarr] starting backfill (limit 5000/channel)...
```

The backfill walks every channel your account has joined. Active piracy channels with 10K+ messages may take 30 minutes to several hours.

## Step 6 — open the dashboard

In your browser: **`http://192.168.2.124:8765/`**

You should see:

- Stats banner (channels / messages indexed / parsed releases / pending / downloading / completed)
- Recent downloads table — every grab from Sonarr/Radarr shows up here with live progress
- Recent releases — last 30 parsed releases
- Indexed channels — what your account has joined
- Auto-refresh every 10 s

## Step 7 — wire up Sonarr / Radarr on the same box

See [`docs/integration/sonarr.md`](../integration/sonarr.md) for field-by-field instructions. Short version:

**Indexer**: Sonarr → Settings → Indexers → ➕ Newznab → URL `http://localhost:8765/newznab/`, API Key `tgarr`.

**Download client**: Sonarr → Settings → Download Clients → ➕ SABnzbd → Host `localhost`, Port `8765`, URL Base `/sabnzbd`, API Key `tgarr`, Category `tv`.

Because Sonarr and tgarr run on the same host, `/downloads/tgarr/...` is the same path inside both containers — **no remote-path mapping needed**. Sonarr's CDH imports finished files automatically.

## Joining channels

tgarr only indexes channels your Telegram account has joined. To grow your index:

- Open Telegram on your phone or desktop.
- Join channels (cinema, TV, books — anything with file attachments).
- tgarr's crawler picks them up within minutes, then back-fills history.

A starter list of channels is at <https://tgarr.me/channels/>.

## Common issues

| Symptom | Fix |
|---|---|
| Dashboard 404 | Container failed to start — `docker compose logs api` |
| 0 releases after 30 min | All joined channels are link-only or have no media. Join a real piracy/media channel. |
| Crawler logs `FloodWait` | Telegram rate-limit — Pyrogram handles automatically. Reduce `TG_BACKFILL_LIMIT` for faster startup. |
| Download stuck "downloading" | Big file or slow Telegram DC. Watch `_data/downloads/tgarr/<release>/*.temp` size. |
| Sonarr "test" indexer fails | Bad URL. From the CasaOS box, `curl http://localhost:8765/newznab/api?t=caps` should return XML. |

## Upgrade

New releases are installed automatically. To update by hand:

```bash
cd /DATA/AppData/tgarr/app
docker compose pull
docker compose up -d
```

Sessions, database, and downloads persist across upgrades.
