# Reddit launch post drafts

## Primary: r/selfhosted

### Title

> `[Release] tgarr v0.1.0 — Telegram-as-source for Sonarr / Radarr (open-source Newznab bridge)`

### Body

I built tgarr because two things bothered me about the *arr stack:

1. **NZB indexers serve ~200K power users globally** and they've been the only "civilized" media source for Sonarr/Radarr for a decade. Single-indexer outages routinely break my Sonarr for hours, and onboarding new indexers is a years-long community-trust grind.

2. **Everyone else uses BitTorrent** — and gets ISP DMCA letters, DPI throttling, and IP exposure to every honeypot peer on a public tracker.

Meanwhile **Telegram channels** have become a primary distribution layer for media of every kind — 950M+ users, files retained indefinitely, every language and niche covered, encrypted HTTPS to Telegram's CDN looks indistinguishable from regular chat traffic to your ISP. But no tool plugs Telegram into the *arr stack.

**tgarr is that bridge.** It crawls Telegram channels you've joined via MTProto (user-account, not bot), indexes filenames + captions into Postgres+Meilisearch, and exposes a Newznab-compatible HTTP API. Sonarr / Radarr add it like any other indexer — they don't know the source is Telegram. On grab, tgarr's worker downloads the file via MTProto and drops it in your SAB completed folder.

**How it compares:**

|                          | tgarr | BitTorrent | PT | NZB |
|--------------------------|:-----:|:----------:|:--:|:---:|
| IP exposed to peers      | ❌ never | ✅ yes | ✅ yes | ❌ |
| ISP sees what you DL     | ❌ HTTPS to Telegram | ✅ trivially | ⚠️ w/o VPN | ❌ |
| DMCA letters             | ❌ none | ✅ common | ⚠️ rare | ❌ |
| Ratio requirement        | ❌ no | n/a | ✅ forever | ❌ |
| Invite gate              | ❌ no | ❌ | ✅ years | ⚠️ many |
| File retention           | ✅ permanent | ⚠️ dies | ✅ usually | ✅ 5000+ days |
| Plug-and-play *arr       | ✅ via tgarr | ⚠️ qBit+jackett | ⚠️ qBit+jackett | ✅ native |

**Stack:** Python (Pyrogram) + Postgres + Meilisearch + FastAPI. Docker compose, multi-arch (amd64/arm64). Runs on Synology, CasaOS, Unraid, TrueNAS, QNAP, Portainer, plain Linux.

**Status:** v0.1.0-alpha. Crawler + indexer + Newznab API + download worker all functional. Web UI in v0.2.

**MIT licensed**, content-neutral, copyright respected — full statement in [RESPONSIBLE_USE.md](https://github.com/tgarrpro/tgarr/blob/main/RESPONSIBLE_USE.md).

- GitHub: https://github.com/tgarrpro/tgarr
- Docs: https://tgarr.me
- Install guides (Synology / CasaOS / Unraid / TrueNAS / QNAP / Portainer): https://github.com/tgarrpro/tgarr/tree/main/docs/install

Happy to answer questions / take PRs / hear what channel types people want first-class metadata parsing for.

---

## Cross-post variants

### r/DataHoarder

Same body, retitled:

> `tgarr — index Telegram channels into Sonarr/Radarr (open-source, Newznab API)`

Tone: emphasize permanent retention + file-collecting angle in the post intro.

### r/sonarr

Cut comparison table down to 4 rows. Title:

> `[Release] tgarr — a Telegram-as-source Newznab indexer for Sonarr`

Emphasize the "drop-in indexer, Sonarr doesn't know the difference" point.

### r/radarr

Same as r/sonarr, radarr-centric.

### r/jellyfin / r/Plex

Lighter technical, more "here's why your library will be easier to fill". Headline:

> `Filling your Jellyfin library when NZB indexers break — tgarr bridges Telegram into the *arr stack`

### r/usenet

Slightly more technical, NZB-respectful tone. Mention "complements your NZB indexers, doesn't replace them — run both for coverage." Title:

> `tgarr — pairs with your NZB indexers as a Telegram-backed Newznab source`

### r/homeland / r/HomeServer

Generic infrastructure framing. Title:

> `New self-hosted tool: tgarr (Telegram → Sonarr/Radarr bridge, MIT)`

## Posting cadence

- **Hour 0**: r/selfhosted (primary)
- **Hour 2-3**: r/DataHoarder + r/sonarr + r/radarr cross-posts
- **Hour 8**: r/jellyfin + r/Plex
- **Day +1**: r/usenet (less aggressive, more "alternative" framing)
- **Day +2**: r/homeland / r/HomeServer

Spread out — Reddit anti-spam dislikes burst cross-posting.

## Reply playbook

Common questions / canned responses:

> **"Is this legal?"**  
> tgarr is content-neutral software like youtube-dl or gallery-dl. It does not host files; channels live on Telegram's platform. The RESPONSIBLE_USE.md in the repo covers the project's stance. What you index is your responsibility.

> **"Will Telegram ban my account?"**  
> If you mass-join hundreds of channels per day, yes. The crawler does passive listening to channels you joined — same behavior profile as a regular Telegram user. Use a dedicated account, not your main; Telegram Premium ($5/mo) lifts rate limits.

> **"How does this compare to NZBGeek?"**  
> Different source, same interface. tgarr indexes Telegram instead of Usenet. You can run both — Sonarr/Radarr treat them as parallel Newznab indexers and pick whichever returns a better release.

> **"DMCA?"**  
> No DMCA letter has ever been observed for MTProto traffic. Telegram does not respond to most takedown requests. The traffic profile to your ISP is encrypted HTTPS to `app.telegram.org` — same as ordinary chat.

> **"Why open source?"**  
> Self-hosting tools should be self-hostable. MIT license, no SaaS upsell, no gated features.

## Post-launch follow-up

- Watch comments for 24h, respond fast and on-topic
- Don't argue with hostile commenters (downvotes are fine, replies inflame)
- Pin a "FAQ" reply to the top of the thread with stuck answers
- After 48h, "edit:" the OP with running tally (stars / GitHub issues / install reports)
