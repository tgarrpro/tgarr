# Federation — how registry.tgarr.me works

tgarr's federated registry is the moat. The self-hosted crawler in each user's tgarr instance discovers and indexes Telegram channels. Those instances optionally contribute their findings to `registry.tgarr.me`, which deduplicates and surfaces a community-verified canonical list back to everyone.

This document describes the wire protocol, the data model, and the privacy guarantees.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│   registry.tgarr.me   (tgarr.me host, profile=server) │
│                                                     │
│   POST /api/v1/contribute   ← instances submit      │
│   GET  /api/v1/registry     → instances pull        │
│   GET  /api/v1/stats        → aggregate stats       │
│   GET  /                    → status page           │
│                                                     │
│   ╶ instance UUIDs SHA-256 hashed before storage    │
│   ╶ CSAM keyword block (defense in depth)           │
│   ╶ per-IP-hash rate limit                          │
│   ╶ distinct_contributors ≥ 3 → auto-verified       │
└─────────────────────────────────────────────────────┘
              ↑ POST eligible          ↓ GET
              │                        │
┌─────────────────────────────────────────────────────┐
│   end-user tgarr instances (anywhere)               │
│                                                     │
│   crawler.contribute_to_registry every 6 hours      │
│   ╶ rotated UUID (weekly)                           │
│   ╶ TGARR_CONTRIBUTE=false to opt out               │
│   ╶ only ≥500 members AND ≥100 media + sfw/nsfw     │
└─────────────────────────────────────────────────────┘
```

## What gets sent

Every 6 hours the instance batches up channels that pass these gates:

- `members_count >= 500` (skip personal chats, friend groups, internal channels)
- `media_count >= 100` (skip text-only / sparse channels)
- `audience IN ('sfw', 'nsfw')` — never `blocked_csam`

The submitted payload looks like:

```json
{
  "instance_uuid": "<random 32-hex>",
  "tgarr_version": "0.3.6",
  "channels": [
    {
      "username": "linuxhandbook",
      "title": "Linux Handbook",
      "members_count": 5200,
      "media_count": 300,
      "audience": "sfw",
      "language": "en",
      "category": "tech"
    }
  ]
}
```

Specifically **not** in the payload: your phone number, your Telegram user ID, message contents, captions, files, friends, private groups, recently-watched, watch history, anything that identifies you personally.

## Privacy guarantees

| Concern | Mitigation |
|---|---|
| Server learns who the user is | UUID is random hex, not tied to user. Rotated every 7 days. SHA-256 hashed on the server before storage. |
| Server correlates submissions over time | Rotated UUID breaks linkability after a week. |
| Network adversary sees what user submitted | HTTPS + Cloudflare. Submission is per-channel @username only, no per-user content. |
| Server learns what user has DOWNLOADED | Downloads are local on the instance. Never reported. |
| User accidentally outs a private/personal channel | Eligibility gate (≥500 members AND ≥100 media) excludes the kinds of channels (friend groups, family chats, internal work channels) where this matters. Private channels and small groups stay private. |
| User submits something they shouldn't (bug or attack) | Server-side keyword block on CSAM. Per-IP rate limit (60 submissions / hour). Channels with `distinct_contributors >= 3` get auto-verified — single submitter cannot game the public list. |

## What clients pull back

```
GET /api/v1/registry?audience=sfw&only_verified=1&limit=5000
```

Returns canonical channel list ordered by `distinct_contributors DESC, members_count DESC`. Filterable by `audience` (sfw / nsfw), `only_verified` (true = require ≥3 contributors), `since` (only updated channels), `min_contributors`.

Free-tier clients (no API key) can pull only `only_verified=1, audience=sfw`. Paid tiers unlock larger pulls, NSFW, and per-channel detail. See [PRICING.md](PRICING.md).

## Server-side hardening

- **CSAM keyword block**: any incoming channel whose `username` or `title` matches the hardcoded blocklist regex is permanently marked `audience=blocked_csam, blocked=true` and never returned to any caller, regardless of tier or query.
- **Per-IP rate limit**: 60 contributions per hour per hashed CF-Connecting-IP. Exceeding returns HTTP 429.
- **Submission size cap**: 500 channels per request, 300-character title cap.
- **Username format validation**: must match `^[A-Za-z][A-Za-z0-9_]{4,31}$` (Telegram's own constraint).
- **Auto-verification**: a channel needs `distinct_contributors >= 3` to flip `verified=true`. One attacker submitting 1000 fakes from 1 instance does not flood the public list — they all share one `instance_hash`.

## Opt-out

Set `TGARR_CONTRIBUTE=false` in the tgarr instance's `.env`. The crawler skips the contribute task on the next startup. Existing submissions are not retroactively removed (the server has no way to identify "yours" — UUID is anonymized).

## Source

- Server: [`registry/main.py`](../registry/main.py), [`registry/schema.sql`](../registry/schema.sql)
- Client: `contribute_to_registry` task in [`crawler/main.py`](../crawler/main.py)
