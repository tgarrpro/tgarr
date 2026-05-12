# Install on Unraid

> Tested on Unraid 6.12+. Submission to Community Apps in progress — until merged, install via Compose Manager plugin (recommended) or manual docker-compose.

## Recommended: Compose Manager plugin

1. Install **Compose Manager** plugin from Unraid Community Apps (if not already).
2. Compose tab → Add New Stack → Name it `tgarr`.
3. Edit stack → paste contents of [docker-compose.yml](https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml).
4. Edit stack → switch to "Environment Variables" tab → paste from [.env.example](https://raw.githubusercontent.com/tgarrpro/tgarr/main/.env.example) and fill values:
   - `TG_API_ID` / `TG_API_HASH` — from https://my.telegram.org/apps
   - `TG_PHONE` — your Telegram phone number, international format `+1XXXXXXXXXX`
   - `DB_PASSWORD` — `openssl rand -hex 24`
   - `MEILI_MASTER_KEY` — `openssl rand -hex 32`
5. First-time login (need a terminal session):

   ```bash
   docker compose --project-directory /boot/config/plugins/compose.manager/projects/tgarr run --rm crawler python login.py
   ```

   Telegram sends SMS code to your phone; type it at the prompt.
6. In the Compose tab UI, click **Compose Up**.

## Community Apps template (coming soon)

Once the PR to https://github.com/Squidly271/AppFeed merges, tgarr will show up in Community Apps search. One-click install with form-driven environment configuration.

Subscribe to release notifications at https://github.com/tgarrpro/tgarr/releases.

## Resource notes

Comfortable on any Unraid host with 4 GB+ RAM and 10 GB+ array/cache space. The Telegram session and database live on the array; consider pinning the `_data/` volume to your cache pool for faster crawler ingest.
