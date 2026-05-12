# tgarr — Telegram-as-source for Sonarr, Radarr & friends

> The *arr you didn't know you needed.  
> Make Telegram channels look like a Newznab indexer — Sonarr, Radarr, and the rest of the *arr stack discover, grab, and import without knowing the difference.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/deploy-docker--compose-2496ED.svg)](docker-compose.yml)
[![GitHub stars](https://img.shields.io/github/stars/tgarrpro/tgarr?style=social)](https://github.com/tgarrpro/tgarr)

---

## Why tgarr exists

NZB indexers (NZBGeek, DrunkenSlug, etc.) serve roughly **200,000 power users globally** and have stagnated for a decade. Telegram, meanwhile, has become the largest single distribution channel for movies, TV shows, software, and books — **hundreds of millions of users**, channels with permanent retention, files up to 4 GB per part, no metadata loss, no DMCA-driven shutdowns.

The *arr ecosystem (Sonarr / Radarr / Lidarr / Readarr / Bazarr) has never had a Telegram integration. Every existing Telegram-piracy tool is either a stand-alone bot or a Plex sync script — none plug into the Sonarr → SAB → Plex/Jellyfin pipeline most self-hosters already run.

**tgarr fills that gap.** It crawls Telegram channels you subscribe to, indexes every release into a searchable database, and exposes a Newznab-compatible HTTP API. Point Sonarr or Radarr at it, and they treat it as just another indexer — but the source is Telegram, not Usenet.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Telegram user account (yours)                                   │
│    Joins the channels you want to monitor                        │
└─────────────────────┬────────────────────────────────────────────┘
                      │ MTProto API
                      ▼
┌──────────────────────────────────────────────────────────────────┐
│  tgarr-crawler   (Pyrogram, async)                               │
│    Listens for new messages + back-fills history                 │
│    Parses filename + caption → release metadata                  │
└─────────────────────┬────────────────────────────────────────────┘
                      ▼
┌──────────────────────────────────────────────────────────────────┐
│  PostgreSQL + Meilisearch                                        │
│    Full-text search over release names                           │
└─────────────────────┬────────────────────────────────────────────┘
                      ▼
┌──────────────────────────────────────────────────────────────────┐
│  tgarr-api       (FastAPI, Newznab XML)                          │
│    GET /api?t=tvsearch&q=...   → Newznab XML                     │
│    GET /api?t=movie&imdbid=... → Newznab XML                     │
│    GET /api?t=get&id=GUID      → triggers download               │
└─────────────────────┬────────────────────────────────────────────┘
                      ▼
┌──────────────────────────────────────────────────────────────────┐
│  Sonarr / Radarr (your existing setup)                           │
│    Sees tgarr as a regular Newznab indexer.                      │
│    On grab, tgarr-worker MTProto-fetches the file and drops it   │
│    into the SAB completed folder — Sonarr imports as usual.      │
└──────────────────────────────────────────────────────────────────┘
```

## Quick start

> Requires Docker + docker compose v2.

```bash
git clone https://github.com/tgarrpro/tgarr.git
cd tgarr

# 1. Get Telegram API credentials at https://my.telegram.org/apps
# 2. Copy .env template and fill in TG_API_ID, TG_API_HASH, TG_PHONE
cp .env.example .env
$EDITOR .env

# 3. One-time interactive login — Telegram prompts for SMS code in the terminal,
#    the code never appears in any config file or chat.
docker compose run --rm crawler python login.py

# 4. Run the full stack
docker compose up -d

# 5. Watch ingestion in real time
docker compose logs -f crawler
```

Once running:

1. **Telegram side**: from your authenticated user account, join the channels you want indexed.
2. **Sonarr/Radarr side**: Settings → Indexers → add Newznab → URL `http://<your-host>:8765/api`.
3. tgarr's crawler picks up every new message; Sonarr/Radarr search against the index just like any NZB indexer.

## Component matrix

| Component | Purpose | Stack |
|---|---|---|
| `crawler/` | MTProto channel listener — ingests messages to Postgres | Python · Pyrogram · asyncpg |
| `api/` | Newznab-compatible HTTP API — Sonarr/Radarr query target | Python · FastAPI |
| `worker/` | Download executor — pulls files from Telegram, delivers to *arr | Python · Pyrogram |
| `ui/` | Web admin — channel management, stats, manual search | React · Vite |
| `schema/` | PostgreSQL DDL + migrations | SQL |

## FAQ

**Is this legal?**  
tgarr is a generic Telegram-to-Newznab bridge, like `youtube-dl` or `gallery-dl`. It does not host any content. What you choose to index is governed by Telegram's terms of service and the laws of your jurisdiction.

**Will my Telegram account get banned?**  
Heavy automated channel-joining can trigger Telegram's anti-spam systems. Run the crawler under a dedicated Telegram account, not your primary identity. Premium ($5/mo) doubles your file-size limit (2 → 4 GB) and lifts some rate limits — recommended for active deployments.

**Does it work with private channels?**  
Yes. As long as the authenticated user account has joined the channel, tgarr can index its messages.

**Does it work with public channels via search?**  
Telegram's global search is limited to public channels and is rate-limited. tgarr's primary mode is *subscribe-then-index* (push), not global crawl (pull).

**Why MTProto and not the Bot API?**  
Bot API can't read messages from channels unless the bot is admin, and is limited to 20 MB file downloads. MTProto (user-account) sees every message in joined channels and downloads up to 2 GB (4 GB with Premium).

**How does this compare to NZBHydra2?**  
NZBHydra2 is a *meta-indexer* — it aggregates queries across existing NZB indexers. tgarr is a *primary source* — it indexes content directly from Telegram. They're complementary; tgarr can sit alongside NZBHydra2 as one of its backends.

## Status

Active development. Public alpha targeted end of week — see [Project Board](https://github.com/tgarrpro/tgarr/projects).

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and pull requests welcome. For larger changes, please open an issue first to discuss what you'd like to change.

Email: [support@tgarr.me](mailto:support@tgarr.me)  
Website: [tgarr.me](https://tgarr.me)

---

**Keywords**: sonarr telegram indexer, radarr telegram, *arr telegram, telegram newznab, telegram nzb alternative, self-hosted telegram downloader, sonarr radarr alternative source, telegram media automation, jellyfin plex telegram, pyrogram indexer.
