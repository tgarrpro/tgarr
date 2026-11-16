# Install on Synology DSM (Container Manager)

> Tested on DSM 7.2 with Container Manager (formerly Docker package).

## Prerequisites

- Synology with Container Manager installed (tested on DSM 7.2)
- SSH enabled (Control Panel → Terminal & SNMP → ✅ Enable SSH service)
- A shared folder for tgarr data, e.g. `docker/tgarr`

## Steps

1. SSH into your Synology and become root:

   ```bash
   ssh admin@synology.local
   sudo -i
   cd /volume1/docker
   mkdir tgarr && cd tgarr
   ```

2. Download config files:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml -o docker-compose.yml
   curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/.env.example -o .env
   chmod 600 .env
   ```

3. Edit `.env`:

   ```
   TG_API_ID=<from my.telegram.org/apps>
   TG_API_HASH=<from my.telegram.org/apps>
   DB_PASSWORD=<openssl rand -hex 24>
   MEILI_MASTER_KEY=<openssl rand -hex 32>
   ```

   The two Telegram values may be left empty: tgarr then asks for them in the browser when they are first needed.

4. Start the stack:

   ```bash
   docker compose up -d
   ```

5. Sign in:

   Open `http://<synology-ip>:8765` in a browser and sign in — scan the QR code with the Telegram app, or enter your phone number and the code Telegram sends you. Nothing is typed in a terminal, and the code never touches a file.

6. Add tgarr to Sonarr/Radarr:
   - Indexer: Settings → Indexers → + → Newznab → URL `http://<synology-ip>:8765/newznab/`, API Path `/api`
   - Download client: Settings → Download Clients → + → SABnzbd → Host `<synology-ip>`, Port `8765`, URL Base `/sabnzbd`
   - API Key: any text, unless you set `TGARR_API_KEY` in `.env` - then that key

   The Dashboard shows the exact address. Field by field: [docs/integration/sonarr.md](../integration/sonarr.md).

## Updating

New releases are installed automatically (the `tgarr-watchtower` container). To update by hand:

```bash
cd /volume1/docker/tgarr
docker compose pull
docker compose up -d
```

## Resource footprint

- Postgres: ~150 MB RAM, scales with index size
- Meilisearch: ~200 MB RAM
- Crawler: ~80 MB RAM idle, more during back-fill
- Disk: 1-5 GB for a year of indexed channels (varies with channel count)

Comfortable on Synology DS220+, DS920+, DS923+, DS1522+, and similar 4 GB+ models.
