# tgarr — Telegram-as-source for Sonarr, Radarr & friends

> **NZB-tier quality for the few who knew. Telegram-grade safety for the millions who never could.**  
> Make Telegram channels look like a Newznab indexer — Sonarr, Radarr, and the rest of the *arr stack discover, grab, and import without knowing the difference.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/deploy-docker--compose-2496ED.svg)](docker-compose.yml)
[![GitHub stars](https://img.shields.io/github/stars/tgarrpro/tgarr?style=social)](https://github.com/tgarrpro/tgarr)

---

## The thesis

**NZB's curated quality + Telegram's mass-scale reach, finally bridged into the *arr stack.**

- **NZB indexers** like NZBGeek delivered organized, parsed, high-def releases — but to roughly 200,000 power users globally, behind invite-only communities and $10–80/yr paywalls. A niche craft for the few who knew.
- **Telegram channels** are where the modern audience already is — 950M+ users, communities for every language, niche, and interest, with files retained indefinitely. The *arr ecosystem has never had a bridge into that layer. Sonarr and Radarr live in a Usenet-shaped tooling world while a billion-user content layer sits beside them, tooling-orphaned.
- **BitTorrent** is where most people end up by default — and where ISP DMCA letters, 5-strike policies, and DPI throttling happen.

tgarr takes Telegram's reach + safety profile and gives it the *arr-stack ergonomics that NZB indexers earned over a decade. Sonarr searches → tgarr returns Newznab XML → on grab, the file streams from Telegram's CDN over plain HTTPS → SAB-style import → Jellyfin/Plex picks it up.

## Three real problems tgarr solves

### 1. The *arr ecosystem has never had a Telegram bridge

Every existing Telegram-related self-hosting tool is a stand-alone bot or a Plex-sync script. None integrate with the Sonarr → SABnzbd → Jellyfin pipeline most self-hosters already run. tgarr fills that exact gap as a drop-in Newznab indexer.

### 2. BitTorrent exposes your IP and traffic to your ISP

Public-tracker users have their IP logged by tens of thousands of peers. US ISPs (Comcast, AT&T, Verizon, Spectrum) issue DMCA notices, throttle BitTorrent traffic at the DPI layer, and in some cases terminate accounts. Private trackers are safer but require maintaining ratios and surviving invite politics for years.

**tgarr eliminates the P2P exposure entirely.** You never advertise an IP. You never seed. You never connect to a peer. From your ISP's view, you're talking HTTPS to Telegram — the same traffic profile 80% of the planet uses for chat. Telegram MTProto traffic looks indistinguishable from ordinary messaging.

### 3. NZB indexers are aging, fragile, and closed

200K global paid users across ~10 indexers. Single-indexer outages routinely break Sonarr/Radarr for hours. Parser accuracy plateaued years ago. New indexer entrants can't bootstrap because the community-trust moat is decades old. The niche works for those already inside it; it doesn't scale to the median Plex/Jellyfin user.

## How it works

```
┌──────────────────────────────────────────────────────────────────┐
│  Your Telegram user account                                      │
│    Joins the channels you want indexed                           │
└─────────────────────┬────────────────────────────────────────────┘
                      │ MTProto · HTTPS to Telegram
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
│    in the SABnzbd completed folder — Sonarr imports as usual.    │
└──────────────────────────────────────────────────────────────────┘
```

## How tgarr compares

| | tgarr | BitTorrent (public) | Private Tracker | NZB indexer |
|---|---|---|---|---|
| IP exposed to peers | ❌ never | ✅ every download | ✅ every download | ❌ never |
| ISP DPI sees content | ❌ HTTPS to Telegram | ✅ trivially | ⚠️ visible without VPN | ❌ HTTPS to indexer |
| DMCA letters / strikes | ❌ none observed | ✅ common | ⚠️ rare but possible | ❌ none |
| Ratio requirement | ❌ no | n/a | ✅ maintain forever | ❌ no |
| Invite required | ❌ no | ❌ no | ✅ years to get in | ⚠️ many invite-only |
| File retention | ✅ permanent | ⚠️ dies when seeders quit | ✅ usually permanent | ✅ 5000+ days |
| Speed | ✅ Telegram CDN, ~10-30 MB/s | ⚠️ depends on seeders | ✅ usually fast | ✅ usually fast |
| Cost | ✅ free (Premium optional $5/mo) | ✅ free | ✅ free (after invite grind) | ⚠️ $10-80/yr |
| Plug-and-play *arr | ✅ via tgarr | ⚠️ qBittorrent + jackett | ⚠️ qBittorrent + jackett | ✅ native |
| Mass-user accessible | ✅ 950M Telegram users | ❌ tech enthusiasts | ❌ tech enthusiasts | ❌ <200K globally |

## Quick start

> Requires Docker + docker compose v2. Runs on any platform that runs Docker — Synology, CasaOS, Unraid, TrueNAS, QNAP, Portainer, plain Linux, etc.

```bash
git clone https://github.com/tgarrpro/tgarr.git
cd tgarr

# 1. Get Telegram API credentials at https://my.telegram.org/apps
# 2. Copy .env template and fill in TG_API_ID, TG_API_HASH, TG_PHONE
cp .env.example .env
$EDITOR .env

# 3. One-time interactive login — Telegram prompts for SMS code in the terminal,
#    the code never appears in any config file.
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

## Platform support

| Platform | Status | Install guide |
|---|---|---|
| Plain docker / docker-compose | ✅ ready | `README` quickstart |
| Synology DSM (Container Manager) | ✅ docker-compose | [docs/install/synology.md](docs/install/synology.md) |
| CasaOS | ✅ app store entry coming | [docs/install/casaos.md](docs/install/casaos.md) |
| Unraid | ✅ Community Apps XML coming | [docs/install/unraid.md](docs/install/unraid.md) |
| TrueNAS Scale | ✅ TrueCharts catalog coming | [docs/install/truenas.md](docs/install/truenas.md) |
| QNAP (Container Station) | ✅ docker-compose | [docs/install/qnap.md](docs/install/qnap.md) |
| Portainer | ✅ stack template | [docs/install/portainer.md](docs/install/portainer.md) |
| Kubernetes / k3s | 🛠 Helm chart v0.2 | [docs/install/kubernetes.md](docs/install/kubernetes.md) |

Container images are multi-arch (`linux/amd64`, `linux/arm64`) — every modern NAS, Raspberry Pi 4/5, M-series Mac, and cloud ARM host runs them.

## Component matrix

| Component | Purpose | Stack |
|---|---|---|
| `crawler/` | MTProto channel listener — ingests messages to Postgres | Python · Pyrogram · asyncpg |
| `api/` | Newznab-compatible HTTP API — Sonarr/Radarr query target | Python · FastAPI |
| `worker/` | Download executor — pulls files from Telegram, delivers to *arr | Python · Pyrogram |
| `ui/` | Web admin — channel management, stats, manual search | React · Vite |
| `schema/` | PostgreSQL DDL + migrations | SQL |

## FAQ

**What kinds of channels do people index with tgarr?**  
tgarr is content-neutral. Common use cases include language-learning material, public-domain film archives, conference recordings, podcast back-catalogs, Linux ISO distributions, course recordings, fan-translation projects, and personal channels members share with each other. What you choose to index is your responsibility and is governed by Telegram's terms of service and the laws of your jurisdiction.

**Will my ISP see what tgarr is downloading?**  
No. All Telegram traffic is encrypted HTTPS (MTProto over TLS) to Telegram's CDN endpoints. Your ISP sees the same traffic profile as ordinary Telegram chat — opaque encrypted flows to `app.telegram.org` and related domains.

**Will my Telegram account get banned?**  
Heavy automated channel-joining can trigger Telegram's anti-spam systems. Run the crawler under a dedicated Telegram account, not your primary identity. Telegram Premium ($5/mo) doubles file-size limits (2 → 4 GB) and lifts some rate limits — strongly recommended for active deployments.

**Does it work with private channels?**  
Yes. As long as the authenticated user account has joined the channel, tgarr can index its messages.

**Why MTProto and not the Telegram Bot API?**  
Bot API can't read channel messages unless the bot is admin and is limited to 20 MB file downloads. MTProto (user-account) sees every message in joined channels and downloads up to 2 GB (4 GB with Premium).

**How does this compare to NZBHydra2?**  
NZBHydra2 is a *meta-indexer* — it aggregates queries across existing NZB indexers. tgarr is a *primary source* — it indexes content directly from Telegram. They're complementary; you can run NZBHydra2 with tgarr configured as one of its backends.

**Why open source?**  
Self-hosting tools work best as open source. tgarr will never be SaaS, gated, or rent-seeking. MIT license — fork it, modify it, build on it.

## Responsible use & copyright

tgarr is a content-neutral indexer of channels you have voluntarily joined. tgarr does **not** host content, does **not** seed files, does **not** solicit infringing material, and does **not** encourage copyright infringement.

We expect tgarr operators to comply with the copyright law of their jurisdiction and Telegram's Terms of Service. Suggested use cases include public-domain archives, open-source distributions, language-learning material, course recordings, community channels, and the operator's own creative work.

If you are a rights-holder and believe a Telegram channel is being indexed in a way that contributes to actionable infringement, see [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md) and contact `abuse@tgarr.me`. Files remain on Telegram's platform; the canonical address for takedowns is [Telegram's DMCA process](https://telegram.org/dmca).

## Status

Active development. Public alpha targeted end of week — see [Issues](https://github.com/tgarrpro/tgarr/issues) for the live roadmap.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and pull requests welcome. For larger changes, open an issue first to discuss the design.

Email: [support@tgarr.me](mailto:support@tgarr.me) · Website: [tgarr.me](https://tgarr.me)

---

**Keywords**: sonarr telegram indexer · radarr telegram source · *arr telegram bridge · telegram newznab · self-hosted telegram downloader · sonarr without bittorrent · radarr without bittorrent · dmca-free media automation · jellyfin plex telegram integration · pyrogram indexer · casaos sonarr · unraid telegram source · synology sonarr telegram · media automation 2026
