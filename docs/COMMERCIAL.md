# The app and the hosted registry

tgarr is two things:

1. **`tgarr`** — the self-hosted app in this repository. MIT-licensed, free forever, no telemetry, no nag screens, no "go pro" prompts in the UI.
2. **The registry at `tgarr.me`** — a hosted federation registry. Its source code is in `registry/` (also MIT).

The two are decoupled. Anyone can run the registry server from `registry/` against a community of their own.

Today the hosted registry is open to every install, anonymous and rate-limited. Nothing is sold and no prices are set.

## What the OSS app guarantees

- Forever free.
- Forever installable from this repo without a registry account.
- Works without `registry.tgarr.me`: every user's tgarr instance crawls the channels their own Telegram account has joined. The registry is an enrichment, not a requirement.
- `TGARR_CONTRIBUTE=false` env opts entirely out of any data leaving the instance.
- Self-host the registry server (`registry/`) against your own community if you don't want to use `registry.tgarr.me`.

## Why two pieces

The app does not host content and does not require an account. The registry holds names and pointers that installs contribute, never files. Keeping them apart keeps each of them simple to reason about.

## Trademark

`tgarr` is a project name; we do not (yet) hold a registered trademark. If we register one in the future it will be licensed permissively for the OSS project's use and restricted to identify the official hosted service.

## Contributing

The project accepts contributions from anyone under the existing MIT license. Contributors retain copyright; submitting a PR grants the project and downstream users the MIT-license rights. Contributor copyright is not assigned to anyone — what you contribute stays MIT.

Bug reports, feature requests, and code: GitHub issues + PRs.
Questions: `support@tgarr.me`.
Abuse / DMCA queries: `abuse@tgarr.me`.
