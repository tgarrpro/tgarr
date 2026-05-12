# Install on QNAP (Container Station)

> Tested on QTS 5.x with Container Station 3.x. Standard docker-compose works — no special template.

## Steps

1. Open **Container Station** → Applications → **Create**.
2. **Application Name**: `tgarr`
3. **YAML**: paste contents of [docker-compose.yml](https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml).
4. **Environment variables**: edit YAML to inject your values (or use a `.env` file via SSH — see below).
5. Click **Validate YAML** → **Create**.

## SSH-based setup (more reliable for first login)

```bash
ssh admin@qnap
sudo -i
mkdir -p /share/Container/tgarr && cd /share/Container/tgarr
curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/.env.example -o .env
chmod 600 .env
$EDITOR .env
```

Fill in:
- `TG_API_ID` / `TG_API_HASH` — from https://my.telegram.org/apps
- `TG_PHONE` — your Telegram phone, international format
- `DB_PASSWORD` — `openssl rand -hex 24`
- `MEILI_MASTER_KEY` — `openssl rand -hex 32`

Then the standard flow:

```bash
docker compose run --rm crawler python login.py   # SMS code prompt
docker compose up -d
docker compose logs -f crawler
```

## Resource notes

Comfortable on QNAP TS-451+, TS-453D, TS-464, TS-673A, and similar 4 GB+ models. Pin the container's `_data/` volume to a SSD-backed shared folder for faster Meilisearch indexing.

## Newznab endpoint

Once running, the Newznab-compatible API is on `http://<qnap-ip>:8765/api`. Point Sonarr/Radarr there in Settings → Indexers → Add Newznab.
