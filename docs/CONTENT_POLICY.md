# Content policy

tgarr is content-neutral self-hosted software for indexing Telegram channels the user has already joined. We are a viewer/indexer, **not a publisher** of the material it surfaces. The user is responsible for compliance with their jurisdiction's laws.

Three classes of content are treated differently throughout the system.

## Tier 1 — SFW (default)

Channels with no explicit adult markers in their `title` or `username`. Visible in all tgarr UIs by default. Eligible for federation contribution. Auto-classified by keyword regex during channel meta refresh; can be manually overridden in `/channels`.

## Tier 2 — NSFW (gated)

Channels matching adult-content keywords across English, Chinese, Russian, and Arabic scripts (`porn|xxx|nsfw|adult|18+|hentai|erotic|nude|naked|onlyfan|sexy|sex|色情|成人|18禁|裸|淫|эротик|порно|секс|اباحي|سكس` etc).

- **Hidden by default** everywhere in the UI.
- User must explicitly opt in via the `/settings` page checkbox affirming they are 18+ and accept compliance responsibility.
- Federated only if the operator's instance has not disabled contribution.
- The hosted `registry.tgarr.me` exposes NSFW only to the Plus tier ($29.99/yr) which requires age verification at checkout.

## Tier 3 — CSAM (hard block, zero tolerance)

Channels with usernames or titles matching CSAM-indicative keywords are **permanently blocked**:

```
loli, lolicon, shota, shotacon, child porn, kid porn, preteen, underage,
cp\d+, cp_
```

This rule:

- Is enforced on **both** the client (in tgarr's own classifier) and the central registry (defense in depth — even a tampered or stale client cannot bypass).
- Cannot be disabled. No setting, query parameter, tier, or build flag overrides it.
- Logs every match to `/tmp/tgarr-csam-flags.log` for human review.
- Never displays the channel anywhere in tgarr.
- Never federates the channel to `registry.tgarr.me`.
- Never permits download of any file from such a channel.

### Reporting

If you encounter CSAM on Telegram while using tgarr, report it to:

- **NCMEC CyberTipline** (US, accepts global): https://report.cybertip.org
- **IWF** (UK + international): https://www.iwf.org.uk/report
- **INHOPE** (network of national hotlines worldwide): https://www.inhope.org

tgarr does not store or host any such material; the report is about the upstream Telegram channel. Telegram itself runs PhotoDNA hash matching and cooperates with authorities; ours is a second line of defense aimed at channel-name signals.

## Channel-by-channel manual override

Any user can manually mark a channel as `sfw`, `nsfw`, or `unknown` from `/channels`. Manual overrides survive auto-reclassification — once you set it, the keyword classifier leaves it alone.

The `blocked_csam` audience is **not** user-settable; it is reached only by the hardcoded keyword regex.

## Federation eligibility

Only channels meeting **all** of the following are pushed upstream to `registry.tgarr.me`:

- `members_count >= 500`
- `media_count >= 100`
- `audience IN ('sfw', 'nsfw')`
- `audience != 'blocked_csam'`

This rule excludes personal chats, friend groups, work groups, and content-sparse channels by construction. The user's private Telegram footprint never reaches the central server.

## Why we make these distinctions

| | Why hide by default | Why block hard |
|---|---|---|
| **SFW** | n/a — visible | n/a |
| **NSFW** | Most users don't want it; legal compliance for displaying adult content varies by jurisdiction; opt-in keeps the default safe for everyone | n/a — adults can opt in |
| **CSAM** | n/a | Possessing or distributing is criminal in essentially every jurisdiction. Letting it surface is unacceptable on every dimension: legal, ethical, brand. |

Pavel Durov was arrested in France in 2024 in part because Telegram was perceived as slow to respond to CSAM. Our hard block is the bare minimum, not a feature.

## Disclaimer

tgarr is open-source self-hosted software. The user installs it, the user joins the Telegram channels, the user is the publisher of any submission to the registry. The maintainers of tgarr and the operators of `registry.tgarr.me` are content-neutral intermediaries who:

- Do not host the content of any Telegram channel.
- Do not endorse, recommend, or curate based on content (beyond the policy above).
- Are not responsible for material shared by third parties on Telegram.
- Respond to valid takedown requests routed through the registry's abuse contact.

See [RESPONSIBLE_USE.md](../RESPONSIBLE_USE.md) for the operator-facing version.
