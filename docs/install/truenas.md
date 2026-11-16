# Install on TrueNAS Scale

> Tested on TrueNAS Scale 23.x / 24.x. Install with docker compose over SSH.

## docker compose via SSH

```bash
ssh root@truenas
mkdir -p /mnt/<pool>/tgarr && cd /mnt/<pool>/tgarr
curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/.env.example -o .env
chmod 600 .env
$EDITOR .env
docker compose up -d
# then sign in in the browser: http://<your-host>:8765
```

In `.env`, fill in `DB_PASSWORD` and `MEILI_MASTER_KEY`. `TG_API_ID` and `TG_API_HASH` may be left empty: tgarr then asks for the two values in the browser when they are first needed.

Note: TrueNAS Scale's `docker` CLI is provided through the system but isn't officially supported by iX.

## Custom App

tgarr is several containers (database, search, crawler, api, updater), all defined by [docker-compose.yml](https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml). A single Custom App of one image is not enough: one container alone does not give a working install.

## TrueCharts catalog (coming)

A community catalog item is being prepared at https://github.com/truecharts/charts. Once merged, tgarr will install in one click from TrueNAS Scale's TrueCharts catalog.

Subscribe to release notifications at https://github.com/tgarrpro/tgarr/releases.
