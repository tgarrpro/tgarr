# tgarr press kit

## 50-word description (tweet length)

> tgarr is an open-source bridge that turns Telegram channels into a Newznab-compatible indexer for Sonarr, Radarr, and the *arr stack. Self-hosted, MIT licensed, no BitTorrent IP exposure, no DMCA exposure. Runs on Synology, CasaOS, Unraid, TrueNAS, QNAP, Portainer, plain Docker.

## 200-word description (blog intro / article lede)

> tgarr is an open-source bridge that makes Telegram channels indexable by the *arr media-automation stack — Sonarr, Radarr, and friends — using the standard Newznab API. The result is that Sonarr and Radarr can grab releases from Telegram without knowing the source has changed.
>
> The project addresses two long-standing gaps in self-hosted media tooling. First, the NZB indexer ecosystem serves only ~200,000 paid users globally and single-indexer outages routinely break Sonarr/Radarr workflows. Second, the BitTorrent alternative exposes the user's IP to thousands of peers, triggers ISP DMCA notices, and invites DPI throttling. tgarr eliminates both: it indexes a billion-user content layer (Telegram) and delivers files over encrypted MTProto, which is indistinguishable from ordinary Telegram chat traffic to an observer.
>
> The stack is Python (Pyrogram + asyncpg + FastAPI), Postgres 16, Meilisearch, packaged as Docker compose. Multi-arch images (amd64 + arm64) run on Synology DSM, CasaOS, Unraid, TrueNAS Scale, QNAP, Portainer, plain Linux, or Kubernetes. tgarr is MIT licensed, content-neutral, and explicitly does not host files or encourage copyright infringement — full statement in the project's RESPONSIBLE_USE.md.

## 500-word description (full article / Wikipedia-style entry)

> **tgarr** is an open-source self-hosted software bridge that exposes Telegram channels as a Newznab-compatible indexer for the *arr media-automation stack (Sonarr, Radarr, Lidarr, Readarr, Bazarr). It is implemented in Python on top of the Pyrogram MTProto client library and is published under the MIT License.
>
> ## Background
>
> The *arr ecosystem — a family of open-source media-library managers built around Sonarr (TV) and Radarr (movies) — has historically supported two release sources: NZB indexers, which serve roughly 200,000 paid users globally across ~10 commercial sites such as NZBGeek, and BitTorrent trackers via integrations like qBittorrent + Jackett. Both sources have well-documented operational issues. NZB indexers experience single-source outages that break Sonarr/Radarr searches for hours, gate new entrants behind years-long community trust-building, and serve a stable but shrinking niche. BitTorrent exposes the user's IP address to every peer on the swarm (including rights-holder honeypots), provokes ISP DMCA notices under US 5-strike enforcement regimes, and is increasingly throttled at the DPI layer.
>
> Meanwhile, Telegram has emerged as one of the largest single distribution layers for media of every type. Telegram operates approximately 950 million monthly active users globally, channels exist for every language and niche, files are retained indefinitely by Telegram's own infrastructure, and traffic from the user to Telegram is encrypted HTTPS to `app.telegram.org` — indistinguishable from ordinary chat traffic to any network observer. Despite this, no integration existed to connect Telegram channels to the *arr stack until tgarr.
>
> ## Architecture
>
> tgarr consists of four cooperating components: a Pyrogram-based crawler that listens to channels the user has joined under their own Telegram user account; a Postgres + Meilisearch index that stores parsed release metadata; a FastAPI-based Newznab XML API server that Sonarr/Radarr query; and a worker that streams completed files from Telegram's CDN via MTProto and delivers them to the user's *arr completed folder, where Sonarr or Radarr import them as if they had arrived from any NZB indexer.
>
> The Newznab API contract means Sonarr and Radarr require no special configuration — tgarr appears in their indexer list as a generic Newznab source. Existing Quality Profiles, search behavior, and Plex/Jellyfin import workflows all continue to function unchanged.
>
> ## Deployment
>
> tgarr ships as a Docker Compose stack with multi-architecture images for `linux/amd64` and `linux/arm64`. Installation guides are provided for Synology DSM (Container Manager), CasaOS, Unraid (Compose Manager), TrueNAS Scale (Custom Apps), QNAP (Container Station), Portainer, and plain Linux. A Kubernetes Helm chart is planned for v0.2.
>
> Telegram API credentials are required (free, obtained from `my.telegram.org/apps`). Operators are advised to use a dedicated Telegram account separate from their primary identity. Telegram Premium ($5/month) lifts rate limits and doubles the file-size limit to 4 GB.
>
> ## Position on copyright
>
> tgarr is described by its authors as a content-neutral tool. It does not host content (files remain on Telegram's infrastructure), does not seed files (it is not peer-to-peer), and does not encourage copyright infringement. Suggested use cases listed in the project's RESPONSIBLE_USE.md include public-domain archives, open-source software distribution, language-learning material, course recordings, and the operator's own creative work. Users are responsible for the channels they choose to index under their own Telegram account.

## Tagline options

- **Primary:** "NZB-tier quality for the few who knew. Telegram-grade safety for the millions who never could."
- Alt: "The *arr you didn't know you needed."
- Alt: "Telegram-as-source for Sonarr, Radarr & friends."
- Alt: "A Newznab bridge to a billion-user content layer."
- Alt: "Sonarr without BitTorrent. Radarr without ISP letters."

## Brand colors

- Primary: Telegram blue `#229ED9`
- Background: Telegram dark `#0e1621`
- Card background: `#17212B`
- Accent: `#5eb6e5`
- Body text: `#f0f3f5`
- Muted: `#8b9aa8`

## Screenshots to prepare

- [ ] Landing page hero (tgarr.me homepage)
- [ ] Sonarr indexer settings panel showing tgarr added as Newznab
- [ ] Sonarr search result showing tgarr-sourced release
- [ ] tgarr web UI dashboard (v0.2)
- [ ] tgarr crawler log streaming new channel messages
- [ ] Architecture diagram (already in README)

## Key facts (for journalists)

- **Project name:** tgarr
- **Website:** https://tgarr.me
- **GitHub:** https://github.com/tgarrpro/tgarr
- **Codeberg mirror:** https://codeberg.org/tgarrpro/tgarr
- **First release:** v0.1.0-alpha, 2026
- **License:** MIT
- **Language:** Python (Pyrogram, FastAPI, asyncpg)
- **Datastore:** PostgreSQL 16 + Meilisearch
- **Deployment:** Docker Compose, multi-arch (amd64/arm64)
- **Maintainers:** tgarr contributors (open-source community)
- **Contact:** support@tgarr.me / abuse@tgarr.me (rights-holder reports)

## Logos & assets

- Landing page favicon: `/favicon.ico` (todo — generate from tg-blue + arr typography)
- 512×512 logo: todo — geometric "tg" + "arr" stacking
- README banner: todo — 1280×640 wide for social cards (og:image)

These land in v0.2.

## Quotable lines

- "The *arr stack has spoken to Usenet for ten years. tgarr is the bridge to the next decade."
- "Telegram is a billion-user content layer that has sat tooling-orphaned next to the *arr stack."
- "tgarr eliminates the BitTorrent toll: no peer exposure, no DMCA letter, no ratio."
- "Sonarr doesn't know — and doesn't care — that the source is Telegram."

## Pronunciation

- **tgarr** — `/ˈtiː ɡɑːr/` ("tee-gar"), rhymes with "see-bar" and "free-jar"
- The lowercase styling matches the *arr family (sonarr, radarr, lidarr). Always lowercase except at start of sentence.

## Related projects to mention

- **Sonarr** — TV automation manager that tgarr serves as an indexer for
- **Radarr** — Movie automation manager that tgarr serves as an indexer for
- **NZBHydra2** — Meta-indexer that can use tgarr as one of its backends
- **Pyrogram** — MTProto Python library that tgarr is built on
- **Jellyfin / Plex** — media servers downstream from the *arr stack

## Embargo / press preview policy

For journalist preview access ahead of public launch: email `support@tgarr.me` with publication name and intended publication date. Preview access includes a dedicated chat thread with maintainers, working demo install, and exclusive metrics from the operator's own tgarr instance (with the operator's permission).
