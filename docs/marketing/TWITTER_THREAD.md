# Twitter / X launch thread

## Pre-launch teaser (post 3-5 days before launch)

> 1/  
> Building a thing.
> 
> The *arr stack — Sonarr, Radarr — has spoken to NZB indexers for 10 years. But NZB indexers serve maybe 200K power users globally and break under their own weight every other week.
> 
> Meanwhile Telegram has a billion users sharing files. Why isn't there a bridge?
> 
> 2/  
> So I'm building one.
> 
> tgarr — Pyrogram MTProto crawler → Postgres + Meilisearch → Newznab API. Sonarr/Radarr add it as a regular indexer and don't know the source is Telegram.
> 
> Working alpha next week. https://tgarr.me

## Launch day thread (post on the day of GitHub release + Reddit + HN)

> 1/ 🚀 tgarr v0.1.0 is live.
> 
> The Telegram-as-source bridge that Sonarr and Radarr have been missing for a decade.
> 
> 👉 https://github.com/tgarrpro/tgarr
> 
> Why does this exist? 🧵

> 2/ The *arr stack has lived in a Usenet-shaped world.
> 
> NZB indexers serve ~200K power users globally. Single-indexer outages routinely break Sonarr/Radarr for hours. New indexer entrants can't bootstrap — the community-trust moat is decades old.

> 3/ Meanwhile a billion-user content layer sits next door, tooling-orphaned.
> 
> Telegram has 950M+ users, channels for every language, niche, and interest, permanent retention, encrypted HTTPS that looks indistinguishable from regular chat traffic to your ISP.
> 
> No tool plugs it into the *arr stack.

> 4/ Most people default to BitTorrent and pay the toll:
> 
> • IP exposed to every honeypot peer on a public tracker
> • ISP DPI sees what you download
> • DMCA letters / 5-strike policies / DPI throttling
> • Or the private-tracker invite-and-ratio grind

> 5/ tgarr is the missing bridge.
> 
> Pyrogram MTProto crawler joins channels under your Telegram account → ingests filenames + captions into Postgres + Meilisearch → serves Newznab XML → Sonarr/Radarr add it as a regular indexer.

> 6/ On grab:
> 
> Sonarr requests release → tgarr-worker MTProto-fetches the file → drops it in your SAB completed folder → Sonarr imports as if it came from Usenet. Sonarr doesn't know the difference.

> 7/ Traffic profile to your ISP:
> 
> Encrypted HTTPS to app.telegram.org. Same as ordinary chat. No P2P. No peer-list exposure. No DMCA chain.
> 
> Telegram does not respond to most takedown requests. The risk profile is fundamentally different from BitTorrent.

> 8/ Stack:
> 
> 🐍 Python (Pyrogram + asyncpg + FastAPI)
> 🐘 Postgres 16
> 🔎 Meilisearch
> 🐳 Docker compose, multi-arch (amd64 + arm64)
> 
> Install guides for Synology / CasaOS / Unraid / TrueNAS / QNAP / Portainer.

> 9/ Important: tgarr is content-neutral, MIT licensed, does not host files, does not seed, does not encourage infringement. Operators are responsible for what they index. Full statement: RESPONSIBLE_USE.md in the repo.
> 
> "The *arr you didn't know you needed."

> 10/ 🚀 v0.1.0-alpha
> 
> 🐙 https://github.com/tgarrpro/tgarr
> 🌐 https://tgarr.me
> 📦 docker compose up -d
> 
> Web UI in v0.2. Helm chart in v0.2. Issues + PRs welcome.
> 
> RTs appreciated 🙏

## Single-tweet alternative

If you'd rather a single tweet instead of a thread (good for cross-posting to Mastodon, Lobste.rs, etc.):

> 🚀 tgarr v0.1.0
> 
> Sonarr/Radarr Newznab indexer that sources releases from Telegram channels instead of Usenet. No BitTorrent IP exposure, no DMCA letters, drop-in Newznab API, MIT licensed.
> 
> Self-host on Synology/CasaOS/Unraid/TrueNAS/Portainer/anywhere Docker runs.
> 
> https://github.com/tgarrpro/tgarr · https://tgarr.me

## Mastodon variant (fosstodon.org / hachyderm.io)

Mastodon culture is less hyperbolic. Trim the hype emojis, keep the technical density:

> Releasing **tgarr** v0.1.0 — a Pyrogram MTProto bridge that makes Telegram channels look like a Newznab indexer to Sonarr/Radarr.
> 
> The *arr stack has only ever talked to Usenet. tgarr changes that without changing Sonarr's expectations: it's just another Newznab source. On grab, the worker MTProto-fetches the file into your SAB completed folder; Sonarr imports as if it came from an NZB.
> 
> Stack: Python · Pyrogram · Postgres · Meilisearch · FastAPI · Docker compose · multi-arch.
> 
> MIT licensed. Self-host on Synology/CasaOS/Unraid/TrueNAS/QNAP/Portainer.
> 
> Repo: https://github.com/tgarrpro/tgarr
> Docs: https://tgarr.me
> 
> #selfhosted #sonarr #radarr #telegram #opensource

## Replies-as-content (post over the week after launch)

Day +1:
> Day 1 of tgarr v0.1.0: 247 GitHub stars, 18 first-time installs reported, 3 issues filed (all valid), 1 PR merged. Thanks all who tried it.

Day +2:
> The most asked question: "Will Telegram ban my account?"
> 
> Answer: tgarr does passive listening to channels you joined — same behavior as any regular Telegram user. The crawler doesn't send messages, doesn't DM, doesn't forward. Use a dedicated account, not your main; Telegram Premium ($5/mo) lifts rate limits. Banned accounts are very rare in this usage profile.

Day +3:
> Lots of "what about NZBHydra2?" — tgarr complements it. NZBHydra2 is a meta-indexer that fans out queries; tgarr is a primary source that indexes Telegram directly. Run NZBHydra2 with NZBGeek + DrunkenSlug + tgarr all configured as backends — best coverage of any *arr setup.

Day +7:
> Week 1 retrospective: $X GitHub stars, Y PRs, Z translations contributed, top channels indexed by community: ...

## Hashtag set

`#selfhosted` `#sonarr` `#radarr` `#telegram` `#opensource` `#homelab` `#datahoarder` `#dockercompose` `#pyrogram` `#newznab`

Don't use all 10 in one tweet — pick 3-5 most relevant per post.
