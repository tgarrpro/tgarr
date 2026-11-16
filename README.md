# tgarr — Telegram, Usenet and the Internet Archive as sources for Sonarr and Radarr

> **NZB-tier quality for the few who knew. Telegram-grade reach for the millions who never could.**  
> Make Telegram channels, Usenet and the Internet Archive look like a Newznab indexer — Sonarr and Radarr discover, grab, and import without knowing the difference.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/deploy-docker--compose-2496ED.svg)](docker-compose.yml)
[![GitHub stars](https://img.shields.io/github/stars/tgarrpro/tgarr?style=social)](https://github.com/tgarrpro/tgarr)
[![Codeberg mirror](https://img.shields.io/badge/mirror-codeberg-2185D0.svg)](https://codeberg.org/tgarr-pro/tgarr)

---

## Three sources, and a household

**New in 0.5.13: books open as they are.** A book, a document, a slide deck or a sheet opens in tgarr's own reader the moment it is here - type you can size, pages that fit a phone, Read aloud - with nothing to convert first and no converter to download (calibre, and the gigabyte it brought, are gone).

| Opens in the reader | |
|---|---|
| Books | `.epub` `.mobi` `.azw` `.azw3` `.prc` `.fb2` `.txt` `.chm` |
| Documents | `.pdf` `.docx` `.odt` `.rtf` `.html` `.md` (Markdown, laid out: headings, lists, code, tables) |
| Slides and sheets | `.pptx` `.xlsx` `.csv` (text, pictures and tables - not the layout, not charts) |
| Notebooks and data | `.ipynb` (notes, code, and what the code printed or drew) `.json` `.yaml` `.toml` |
| Pictures | JPEG, PNG, GIF, WebP, AVIF as they are; a phone's HEIC and JPEG XL shown as a JPEG made of them |
| Scanned books | `.djvu` (made into pages the first time it is opened) |
| Comics | `.cbz` `.cbr` (page by page, a real RAR too) |

Not shown, only kept for download: `.lit` `.lrf` `.pdb`, old `.doc` `.xls` `.ppt`, and Kindle books that are copy-protected.

**New in 0.5.9: it works before you give it any account.** The Internet Archive is a third source - old films, radio plays, audiobooks, books and magazines that anybody may download: search it on the Archive page, press Get, and Radarr can ask for its films through tgarr with nothing configured but tgarr itself. Public Telegram channels can be followed and browsed without signing in; the Telegram app id is asked for when you first want to play a file, not before. Small things posted whole on Usenet - a book, an album, a set of pictures - are fetched by tgarr itself. And what a browser cannot play (a DVD or Blu-ray image, an avi, an mkv with sound it has no decoder for) is converted while it is watched, so a phone or a tablet can play it.

**Telegram and Usenet, one indexer.** Since 0.5.8 tgarr reads Usenet as well: give it a Usenet account (Settings → Usenet) and it reads newsgroup *headers*, keeps a card for every named film, episode, album and book, and makes the NZB itself when Sonarr or Radarr wants one - no outside NZB indexer, no invite. Your downloader (SABnzbd, NZBGet) does the downloading of films and series; tgarr itself fetches only small things that were posted whole - a book, a song, a picture - straight into its Library, Music and Gallery, and never video. Like the Telegram index, the reading is shared: every install reads a different stretch and all of them can search what any of them found - names and pointers, never files. [Choosing a Usenet provider](https://tgarr.me/usenet/).

**A media center the family can use.** Films, series, music, books, photos and your own uploaded files, played in the browser. Set a PIN and every other browser at home gets only the Media Center, with only what you ticked - whole channels or single files - and with time rules: opening hours, minutes a day, a break after so long in one go. Adult material is labelled and left out unless you switch it on; sexual material involving children is never indexed.

## The thesis

**NZB's curated quality + Telegram's mass-scale reach, finally bridged into the *arr stack.**

- **NZB indexers** like NZBGeek delivered organized, parsed, high-def releases — but to roughly 200,000 power users globally, behind invite-only communities and $10–80/yr paywalls. A niche craft for the few who knew.
- **Telegram channels** are where the modern audience already is — 950M+ users, communities for every language, niche, and interest, with files retained indefinitely. The *arr ecosystem has never had a bridge into that layer. Sonarr and Radarr live in a Usenet-shaped tooling world while a billion-user content layer sits beside them, tooling-orphaned.

tgarr takes Telegram's reach and gives it the *arr-stack ergonomics that NZB indexers earned over a decade. Sonarr searches → tgarr returns Newznab XML → on grab, tgarr fetches the file from Telegram → SAB-style import → Jellyfin/Plex picks it up. Usenet and the Internet Archive are searched through the same indexer.

## Three real problems tgarr solves

### 1. The *arr ecosystem has never had a Telegram bridge

Every existing Telegram-related self-hosting tool is a stand-alone bot or a Plex-sync script. None integrate with the Sonarr → SABnzbd → Jellyfin pipeline most self-hosters already run. tgarr fills that exact gap as a drop-in Newznab indexer.

### 2. BitTorrent shows your address to everybody else

To download with BitTorrent is to join a swarm: every other peer sees your IP address, and you upload to them while you download. Private trackers add a ratio to keep up and an invitation to get first.

**tgarr has no peers.** Nothing is uploaded to anybody, no address is announced, there is no ratio and no invitation. A file comes from Telegram, from your Usenet provider or from archive.org, to your machine and nowhere else.

### 3. NZB indexers are aging, fragile, and closed

200K global paid users across ~10 indexers. Single-indexer outages routinely break Sonarr/Radarr for hours. Parser accuracy plateaued years ago. New indexer entrants can't bootstrap because the community-trust moat is decades old. The niche works for those already inside it; it doesn't scale to the median Plex/Jellyfin user.

## How it works

```
┌──────────────────────────────────────────────────────────────────┐
│  Your Telegram user account                                      │
│    Joins the channels you want indexed                           │
└─────────────────────┬────────────────────────────────────────────┘
                      │ MTProto
                      ▼
┌──────────────────────────────────────────────────────────────────┐
│  tgarr-crawler   (Kurigram, async)                               │
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
│    GET /newznab/api?t=tvsearch&q=...   → Newznab XML             │
│    GET /newznab/api?t=movie&imdbid=... → Newznab XML             │
│    GET /newznab/api?t=get&id=GUID      → triggers download       │
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
| Ratio requirement | ❌ no | n/a | ✅ maintain forever | ❌ no |
| Invite required | ❌ no | ❌ no | ✅ years to get in | ⚠️ many invite-only |
| File retention | ✅ permanent | ⚠️ dies when seeders quit | ✅ usually permanent | ✅ 5000+ days |
| Cost | ✅ free (Premium optional $5/mo) | ✅ free | ✅ free (after invite grind) | ⚠️ $10-80/yr |
| Plug-and-play *arr | ✅ via tgarr | ⚠️ qBittorrent + jackett | ⚠️ qBittorrent + jackett | ✅ native |
| Mass-user accessible | ✅ 950M Telegram users | ❌ tech enthusiasts | ❌ tech enthusiasts | ❌ <200K globally |

## Quick start

**On a computer of your own** (new in 0.5.11) - download one file from the [latest release](https://github.com/tgarrpro/tgarr/releases/latest) and double-click it:

| | |
|---|---|
| **Windows 10 / 11** | `install-tgarr.cmd`. Windows asks once for permission. It switches on WSL2 (the Linux that Windows carries - one restart the first time, after which it goes on by itself) and installs tgarr inside it. |
| **macOS** | `install-tgarr-macos.zip`, then `install-tgarr.command` inside it. It sets up [Colima](https://github.com/abiosoft/colima) (a small Linux machine with Docker in it; needs [Homebrew](https://brew.sh)) and installs tgarr inside it. |
| **Linux** | in a terminal: `curl -fsSL https://raw.githubusercontent.com/tgarrpro/tgarr/main/install/install.sh \| bash` |

When it is done, tgarr opens at `http://localhost:8765`. No account of any kind is needed to begin. The files are short scripts you can read before running; Windows and macOS warn about any downloaded script that is not signed by a company ("Run anyway" / right-click → Open). What each does, its settings, and how to remove it again: [docs/install/desktop.md](docs/install/desktop.md). On the television and on a phone's home screen: [docs/install/tv.md](docs/install/tv.md).

**By hand, or on a NAS:**

> Requires Docker + docker compose v2. Runs on any platform that runs Docker — Synology, CasaOS, Unraid, TrueNAS, QNAP, Portainer, plain Linux, etc.

```bash
mkdir tgarr && cd tgarr
curl -fsSLO https://raw.githubusercontent.com/tgarrpro/tgarr/main/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/tgarrpro/tgarr/main/.env.example -o .env

# 1. Get Telegram API credentials at https://my.telegram.org/apps
# 2. Fill in .env: TG_API_ID, TG_API_HASH and two secrets of your own
$EDITOR .env

# 3. Start it
docker compose up -d

# 4. Open http://<your-host>:8765 and sign in (QR code, or phone number + code)
```

Those two files are the whole install — no checkout of the source. New
releases are picked up automatically (a `watchtower` container that only
touches tgarr's own containers); set `TGARR_TAG` in `.env` to stay on one
version. To build from source instead, clone the repository and use
`docker-compose.dev.yml`.

Once running:

1. **Telegram side**: from your authenticated user account, join the channels you want indexed.
2. **Sonarr/Radarr side**: Settings → Indexers → add Newznab → URL `http://<your-host>:8765/newznab/` (the dashboard shows the exact address).
3. tgarr's crawler picks up every new message; Sonarr/Radarr search against the index just like any NZB indexer.

## Platform support

| Platform | Status | Install guide |
|---|---|---|
| Plain docker / docker-compose | ✅ ready | `README` quickstart |
| Linux, macOS, Windows (a computer of your own) | ✅ one-line installer (macOS and Windows: new, see the guide) | [docs/install/desktop.md](docs/install/desktop.md) |
| Synology DSM (Container Manager) | ✅ docker-compose | [docs/install/synology.md](docs/install/synology.md) |
| CasaOS | ✅ app store entry coming | [docs/install/casaos.md](docs/install/casaos.md) |
| Unraid | ✅ Community Apps XML coming | [docs/install/unraid.md](docs/install/unraid.md) |
| TrueNAS Scale | ✅ TrueCharts catalog coming | [docs/install/truenas.md](docs/install/truenas.md) |
| QNAP (Container Station) | ✅ docker-compose | [docs/install/qnap.md](docs/install/qnap.md) |
| Portainer | ✅ stack template | [docs/install/portainer.md](docs/install/portainer.md) |

Container images are multi-arch (`linux/amd64`, `linux/arm64`) — every modern NAS, Raspberry Pi 4/5, M-series Mac, and cloud ARM host runs them.

## Component matrix

| Component | Purpose | Stack |
|---|---|---|
| `crawler/` | Talks to Telegram: indexes channels into Postgres, fetches a file when somebody opens it | Python · Kurigram · asyncpg |
| `api/` | Every page, reader and player; Usenet and the Internet Archive; the Newznab and SABnzbd endpoints for Sonarr/Radarr | Python · FastAPI |
| `install/` | Installers for Linux, Windows (WSL2) and macOS (Colima) | sh · PowerShell |
| `tv/` | The Android TV app: the Media Center on a television | Java |
| `api/schema.sql` | PostgreSQL base schema (applied by the api on start) | SQL |

## FAQ

**What kinds of channels do people index with tgarr?**  
tgarr is content-neutral. Common use cases include language-learning material, public-domain film archives, conference recordings, podcast back-catalogs, Linux ISO distributions, course recordings, fan-translation projects, and personal channels members share with each other. What you choose to index is your responsibility and is governed by Telegram's terms of service and the laws of your jurisdiction.

**Will my Telegram account get banned?**  
Heavy automated channel-joining can trigger Telegram's anti-spam systems. Run the crawler under a dedicated Telegram account, not your primary identity. Telegram Premium ($5/mo) doubles file-size limits (2 → 4 GB) and lifts some rate limits — strongly recommended for active deployments.

**Does it work with private channels?**  
Yes. As long as the authenticated user account has joined the channel, tgarr can index its messages.

**Why MTProto and not the Telegram Bot API?**  
Bot API can't read channel messages unless the bot is admin and is limited to 20 MB file downloads. MTProto (user-account) sees every message in joined channels and downloads up to 2 GB (4 GB with Premium).

**How does this compare to NZBHydra2?**  
NZBHydra2 is a *meta-indexer* — it aggregates queries across existing NZB indexers. tgarr is a *primary source* — it indexes content directly from Telegram, Usenet and the Internet Archive. They're complementary; you can run NZBHydra2 with tgarr configured as one of its backends.

**Why open source?**  
Self-hosting tools work best as open source. tgarr will never be SaaS, gated, or rent-seeking. MIT license — fork it, modify it, build on it.

## Responsible use & copyright

tgarr is a content-neutral indexer of channels you have voluntarily joined. tgarr does **not** host content, does **not** seed files, does **not** solicit infringing material, and does **not** encourage copyright infringement.

We expect tgarr operators to comply with the copyright law of their jurisdiction and Telegram's Terms of Service. Suggested use cases include public-domain archives, open-source distributions, language-learning material, course recordings, community channels, and the operator's own creative work.

If you are a rights-holder and believe a Telegram channel is being indexed in a way that contributes to actionable infringement, see [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md) and contact `abuse@tgarr.me`. Files remain on Telegram's platform; the canonical address for takedowns is [Telegram's DMCA process](https://telegram.org/dmca).

## Status

Active development, released as it goes - see [Releases](https://github.com/tgarrpro/tgarr/releases) for what each version brought and [Issues](https://github.com/tgarrpro/tgarr/issues) for what is being worked on.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and pull requests welcome. For larger changes, open an issue first to discuss the design.

Email: [support@tgarr.me](mailto:support@tgarr.me) · Website: [tgarr.me](https://tgarr.me)

---

**Keywords**: sonarr telegram indexer · radarr telegram source · *arr telegram bridge · telegram newznab · self-hosted telegram downloader · sonarr without bittorrent · radarr without bittorrent · jellyfin plex telegram integration · pyrogram indexer · casaos sonarr · unraid telegram source · synology sonarr telegram · media automation 2026
