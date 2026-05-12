# Install on Synology DSM (Container Manager)

> Tested on DSM 7.2 with Container Manager (formerly Docker package).

## Prerequisites

- Synology with DSM 7.0+ and Container Manager installed
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
   TG_PHONE=+1XXXXXXXXXX
   DB_PASSWORD=<openssl rand -hex 24>
   MEILI_MASTER_KEY=<openssl rand -hex 32>
   ```

4. One-time interactive Telegram login (SMS code stays in your terminal, never lands in any file):

   ```bash
   docker compose run --rm crawler python login.py
   ```

5. Start the stack:

   ```bash
   docker compose up -d
   ```

6. Add tgarr as a Newznab indexer in Sonarr/Radarr:
   - URL: `http://<synology-ip>:8765/api`
   - API Key: (none required for self-hosted)

## Updating

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
