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

## First-time Telegram login

Once the stack is up, open `http://<your-host>:8765` in a browser and sign in — scan the QR code with the Telegram app, or enter your phone number and the code Telegram sends you. Nothing is typed in a terminal, and the code never touches a file.

## Stack template URL (for App Templates)

A Portainer App Template JSON file is published at:

```
https://raw.githubusercontent.com/tgarrpro/tgarr/main/portainer-template.json
```

Add this to Portainer's App Template list (Settings → App Templates → URL field) for a guided install with form fields instead of editing YAML.

(Template file lands in v0.2.)

## Newznab endpoint

After login + `up`, the API is on `http://<host-ip>:8765/api`. Add to Sonarr/Radarr → Settings → Indexers → Newznab.
