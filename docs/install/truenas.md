# Install on TrueNAS Scale

> Tested on TrueNAS Scale 23.x / 24.x. Native Kubernetes (k3s)–based — install via docker-compose mode (Custom App) or wait for TrueCharts catalog entry.

## Recommended (today): Custom App via docker-compose

TrueNAS Scale supports docker-compose syntax in Custom Apps.

1. **Apps → Discover Apps → Custom App**
2. Application Name: `tgarr`
3. Image:  
   - Image repository: `ghcr.io/tgarrpro/tgarr-crawler`
   - Image tag: `latest`
   - Pull Policy: IfNotPresent
4. Add **Environment Variables**:
   - `TG_API_ID` = your Telegram API ID
   - `TG_API_HASH` = your Telegram API hash
   - `TG_PHONE` = your Telegram phone, international format
   - `DB_DSN` = `postgresql://tgarr:<DB_PASSWORD>@db:5432/tgarr`
   - `MEILI_URL` = `http://meili:7700`
   - `MEILI_MASTER_KEY` = your Meilisearch master key
5. Add **Storage** (Host Path):
   - `/mnt/<pool>/tgarr/session` → mount path `/app/session`
6. Networking → expose port `8765` (Newznab API)
7. Apply.

Repeat for `db` (postgres:16-alpine) and `meili` (getmeili/meilisearch:v1.11) — see [docker-compose.yml](https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml) for the exact env / volume layout.

## Alternative: docker-compose via SSH

If you prefer the standard docker-compose flow:

```bash
ssh root@truenas
mkdir -p /mnt/<pool>/tgarr && cd /mnt/<pool>/tgarr
curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/.env.example -o .env
chmod 600 .env
$EDITOR .env
docker compose run --rm crawler python login.py   # interactive SMS code
docker compose up -d
```

Note: TrueNAS Scale's `docker` CLI is provided through the system but isn't officially supported by iX — prefer the Custom App route for production.

## TrueCharts catalog (coming)

A community catalog item is being prepared at https://github.com/truecharts/charts. Once merged, tgarr will install in one click from TrueNAS Scale's TrueCharts catalog.

Subscribe to release notifications at https://github.com/tgarrpro/tgarr/releases.
