# Commercial model

tgarr is two things:

1. **`tgarr`** — the self-hosted app in this repository. MIT-licensed, free forever, no telemetry, no nag screens, no "go pro" prompts in the UI.
2. **`registry.tgarr.me`** — a hosted federation registry plus optional commercial subscription tiers. Source code in `registry/` (also MIT). The hosted instance is commercial.

The two are intentionally decoupled. Anyone can run the registry server from `registry/` for free against a community of their own. The price tag at `registry.tgarr.me` reflects what it costs us to operate that specific deployment — moderation, abuse handling, age verification, payment processing, legal compliance, infrastructure — not the source code itself.

## What the OSS app guarantees

- Forever free.
- Forever installable from this repo without a registry account.
- Works without `registry.tgarr.me`: every user's tgarr instance crawls the channels their own Telegram account has joined. The registry is an enrichment, not a requirement.
- `TGARR_CONTRIBUTE=false` env opts entirely out of any data leaving the instance.
- Self-host the registry server (`registry/`) against your own community if you don't want to use `registry.tgarr.me`.

## What the hosted service offers

See [PRICING.md](PRICING.md) for the tier table. Briefly:

- **Free** — verified SFW channel list. Anonymous, rate-limited.
- **$9.99 / year Standard** — full SFW registry + federated release search.
- **$29.99 / year Plus** — adds NSFW. Age-verified at checkout.

## Why this structure

The Sonarr / Radarr ecosystem is exactly this shape: free OSS apps + paid third-party indexers. tgarr mirrors the convention. Users already understand it.

This split also keeps the legal surface clean: the OSS code base does not host content, does not solicit payment, does not require an account. The commercial entity that runs `registry.tgarr.me` is structurally separate from the project's MIT codebase.

## Trademark

`tgarr` is a project name; we do not (yet) hold a registered trademark. If we register one in the future it will be licensed permissively for the OSS project's use and restricted to identify the official hosted service.

## Project ↔ business separation

The plan, when revenue justifies it:

| Entity | Role |
|---|---|
| **tgarr (project)** | MIT codebase, GitHub org `tgarrpro`, all PRs welcome |
| **tgarr-plus (company)** | Operates `registry.tgarr.me` hosted service + subscriptions. Separate LLC in an adult-content-friendly jurisdiction. Contracts the project for nothing — uses the public OSS the same way any user would. |

This keeps the OSS project clean if the commercial side ever has to wind down or face takedown pressure.

## Contributing

The OSS project (this repo) accepts contributions from anyone under the existing MIT license. Contributors retain copyright; submitting a PR grants the project and downstream users the MIT-license rights. We do not assign contributor copyright to any commercial entity — what you contribute stays MIT, forever.

Bug reports, feature requests, and code: GitHub issues + PRs.
Commercial-tier support: `support@tgarr.me`.
Abuse / DMCA / age-verification queries: `abuse@tgarr.me`.
