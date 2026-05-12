# Install on CasaOS

tgarr is being submitted to the Big Bear AppStore and the official IceWhale AppStore. Until those PRs merge, install via the manual docker-compose route below.

## Manual install (current method)

1. SSH into your CasaOS host.
2. Create a directory for tgarr:

   ```bash
   sudo mkdir -p /DATA/AppData/tgarr && cd /DATA/AppData/tgarr
   ```

3. Pull the latest `docker-compose.yml`:

   ```bash
   sudo curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml -o docker-compose.yml
   sudo curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/.env.example -o .env
   sudo chmod 600 .env
   sudo $EDITOR .env
   ```

4. Fill in `TG_API_ID`, `TG_API_HASH`, `TG_PHONE` (from https://my.telegram.org/apps), and generate `DB_PASSWORD` / `MEILI_MASTER_KEY` with `openssl rand -hex 24` / `openssl rand -hex 32`.

5. One-time login:

   ```bash
   sudo docker compose run --rm crawler python login.py
   ```

   Type the SMS code that Telegram sends you. Session is persisted to `./_data/session/`.

6. Bring up the stack:

   ```bash
   sudo docker compose up -d
   ```

7. tgarr Newznab API will be on `http://<casaos-ip>:8765/api`. Point Sonarr/Radarr at it (Settings → Indexers → Add Newznab).

## CasaOS AppStore (coming soon)

Once the PR to Big Bear AppStore merges, you'll be able to install tgarr from the CasaOS web UI in one click:

1. Open CasaOS → App Store → search "tgarr"
2. Click Install
3. Fill in Telegram API credentials in the install wizard
4. Done

Subscribe to release notifications at https://github.com/tgarrpro/tgarr/releases.
