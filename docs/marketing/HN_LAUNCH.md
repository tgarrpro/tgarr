# Hacker News launch post

## Submission

**Title:**
> `Show HN: Tgarr – Telegram-as-source for Sonarr and Radarr`

**URL:**
> `https://github.com/tgarrpro/tgarr`

**Text:** (HN allows a first comment on Show HN, write this:)

---

Hi HN — tgarr is an open-source bridge that makes Telegram channels look like a Newznab indexer to the *arr stack (Sonarr, Radarr, etc).

The *arr ecosystem has lived in a Usenet-shaped world for a decade. NZB indexers serve ~200K power users globally; single-indexer outages routinely break Sonarr for hours. Meanwhile Telegram has become a primary distribution layer for media of every kind — 950M+ users, permanent retention, every language — but no tool plugs that into the *arr stack. BitTorrent fills the gap for most people, with the well-documented side effect of DMCA letters and IP exposure to every honeypot peer on a public tracker.

tgarr is the missing bridge. A Pyrogram MTProto crawler joins channels under your own Telegram user account, ingests filenames + captions into Postgres + Meilisearch, and exposes a Newznab XML API. Sonarr adds it as a regular indexer; on grab, the worker streams the file via MTProto and drops it in your SABnzbd completed folder.

From the ISP's perspective the traffic is opaque HTTPS to `app.telegram.org` — same as ordinary chat. No P2P, no peer-list exposure, no DMCA chain.

Stack: Python (Pyrogram + asyncpg + FastAPI), Postgres 16, Meilisearch, Docker compose. Multi-arch images. Install guides for Synology / CasaOS / Unraid / TrueNAS / QNAP / Portainer.

Status: v0.1.0-alpha, MIT licensed. Web UI lands in v0.2. RESPONSIBLE_USE.md statement on the repo covers the content-neutral framing — tgarr does not host files, does not seed, does not encourage infringement. Users are responsible for the channels they choose to index.

Repo: https://github.com/tgarrpro/tgarr  
Website: https://tgarr.me  
Codeberg mirror: https://codeberg.org/tgarrpro/tgarr

Happy to answer technical questions about MTProto rate limits, Newznab API compatibility quirks, parser strategy, or anything else.

---

## Timing

- Submit between **08:00–09:30 Pacific** weekday (most likely to hit frontpage)
- Don't submit on Friday (HN frontpage decays into weekend)
- Avoid Mon morning (frontpage is full of stale weekend posts)
- Best: **Tue / Wed / Thu morning Pacific**

## After submission

1. **First 30 min are critical** — early votes determine whether it reaches frontpage at all
2. **Don't** ping friends to upvote (HN flagging system catches this and shadowbans)
3. **Do** answer comments fast — engagement boost helps the post stay on frontpage
4. **Don't** argue with cynical commenters — let other commenters defend (always do)
5. **Do** post the first technical clarification comment yourself within 5 min — anchors the discussion

## Pre-emptive answers in comments

- "Why not use Bot API?" → MTProto user-account is necessary for reading channel messages without admin bot status + 2GB (4GB Premium) file downloads vs Bot API's 20MB cap.
- "What about Telegram's anti-abuse?" → Passive listening to joined channels is below Telegram's automated thresholds; dedicated crawler account isolates risk from the operator's main identity.
- "Just use BitTorrent + VPN?" → That's the BitTorrent + tax-on-VPN treadmill. tgarr removes the need.
- "DMCA?" → No DMCA letter has been observed against MTProto traffic. Telegram does not respond to most takedown requests. ISP sees encrypted HTTPS to `app.telegram.org`.
- "Why open source?" → Self-hosting tools should be self-hostable. No SaaS upsell, no gated features. MIT.

## If it makes frontpage

Expect **~10K-50K visits in 12 hours** + ~500-2000 GitHub stars + 50-200 issues filed.

Prepare:
- README first-paragraph reads cleanly (it's the only thing 80% of visitors read)
- Quickstart works on first attempt (test on a clean Synology / CasaOS box before submission)
- Issues template ready (`.github/ISSUE_TEMPLATE/`)
