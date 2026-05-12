# Install via Portainer

> Portainer Community Edition 2.20+. Works on any Docker host Portainer manages — Synology, Unraid, plain Linux, Proxmox LXC, etc.

## Stack template (recommended)

1. **Stacks → Add stack** → Name: `tgarr`
2. **Build method: Repository**
3. **Repository URL**: `https://github.com/tgarrpro/tgarr.git`
4. **Repository reference**: `refs/heads/main`
5. **Compose path**: `docker-compose.yml`
6. **Environment variables** (Advanced mode → load from `.env` or enter manually):

   ```
   TG_API_ID=<from my.telegram.org/apps>
   TG_API_HASH=<from my.telegram.org/apps>
   TG_PHONE=+1XXXXXXXXXX
   DB_PASSWORD=<openssl rand -hex 24>
   MEILI_MASTER_KEY=<openssl rand -hex 32>
   ```

7. **Enable automatic updates** (optional) → Polling interval `6h` → auto-pull new tgarr versions.
8. **Deploy the stack**.

## First-time Telegram login (interactive SMS)

Portainer's web UI can't accept the SMS code interactively. You need a one-shot terminal session against the crawler container:

1. SSH into the Docker host.
2. Find the project directory (Portainer puts it under `/data/compose/<stack-id>/` by default; check Portainer logs for the exact path).
3. Run:

   ```bash
   cd /data/compose/<stack-id>
   docker compose run --rm crawler python login.py
   ```

4. Type the SMS code Telegram sends to your phone.
5. The session is now persisted in the `_data/session/` volume — Portainer can restart the stack as much as it wants without re-logging in.

## Stack template URL (for App Templates)

A Portainer App Template JSON file is published at:

```
https://raw.githubusercontent.com/tgarrpro/tgarr/main/portainer-template.json
```

Add this to Portainer's App Template list (Settings → App Templates → URL field) for a guided install with form fields instead of editing YAML.

(Template file lands in v0.2.)

## Newznab endpoint

After login + `up`, the API is on `http://<host-ip>:8765/api`. Add to Sonarr/Radarr → Settings → Indexers → Newznab.
