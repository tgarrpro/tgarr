# Influencer / YouTuber outreach

The self-hosting / *arr-stack YouTube community is small but extremely targeted. A single review video from the right channel produces more lasting impact than a Reddit launch — videos rank in Google search permanently, and the audience is the precise self-hosting power-user demographic.

## Target list (in priority order)

| Channel | Subs | Why it fits |
|---|---|---|
| **TechHut** | 200K+ | *arr stack focus, Jellyfin / Plex tutorials, weekly new-tool coverage |
| **Jim's Garage** | 100K+ | Self-hosted infrastructure, frequent docker-compose deep-dives |
| **TechnoTim** | 350K+ | Self-hosting + Kubernetes, broad audience |
| **NetworkChuck** | 4M+ | Big reach,less *arr-specific,but his "self-host everything" videos do well |
| **Wolfgang's Channel** | 200K+ | NAS / homelab reviews,Synology + Unraid heavy audience |
| **Christian Lempa** | 200K+ | Self-hosted apps + tutorials |
| **Awesome Open Source** | 80K+ | Reviews OSS projects, lower bar |
| **DB Tech** | 200K+ | Docker / self-hosted apps, focused niche |
| **The Linux Cast** | 50K+ | Linux + self-hosting tutorials |
| **Mariushosting** (blog) | High DA | Synology-specific blog, indexed well on Google for "synology X tutorial" |
| **smarthomebeginner** | 40K+ | Tutorials specifically for Docker/Compose users |
| **HomeNetworkGuy** | 60K+ | Plex/Jellyfin/*arr deep dives |
| **/u/MichaelEvangelista** (r/selfhosted regular) | n/a | Reddit power user, his approval gets cross-posted |

## Email template

**Subject:** `Heads-up: tgarr — Telegram-as-source for Sonarr/Radarr (alpha)`

```
Hi <Name>,

Following your work on <recent video title relevant to *arr or self-hosting>.

I'm releasing an open-source tool you might find interesting: tgarr (https://tgarr.me),
a Pyrogram-based MTProto bridge that makes Telegram channels look like a Newznab indexer
to Sonarr / Radarr. Sonarr adds it like any indexer; on grab, the worker streams the file
from Telegram's CDN into your SAB completed folder. The *arr ecosystem has talked to
Usenet for a decade — tgarr is what bridges it to Telegram for the first time.

Two things I think your audience will care about:
  1. Self-host friendly — Docker compose, multi-arch images, install guides for
     Synology, CasaOS, Unraid, TrueNAS, QNAP, Portainer.
  2. No BitTorrent IP exposure, no DMCA letters, no peer-list participation.
     ISP sees encrypted HTTPS to Telegram — same as ordinary chat.

GitHub: https://github.com/tgarrpro/tgarr (MIT, public alpha v0.1.0)

If you want a preview install for video coverage, happy to help — I can spin up a
demo environment, walk you through Sonarr indexer config, or hop on a call.

No urgency / no expectation — just thought you might want to be first.

Cheers,
Tom
support@tgarr.me
```

Customize `<Name>` and `<recent video title>` per recipient. Generic mail looks like spam; one specific reference (a recent video they made) doubles the response rate.

## When to send

- After alpha is verified working end-to-end (so the demo doesn't fail)
- Before the Reddit / HN launch (so they can have a video ready to drop in the same week)
- Send on **Tuesday morning** local time (highest open rate)
- **One email, no follow-up for 7 days.** If they don't respond, they don't respond — chasing reads as spammy.

## What to send with the email

Include or link in the email:
1. **30-second demo screencast** — record one,silent, just showing the flow (Sonarr search → tgarr returns → grab → file lands → Sonarr imports)
2. **PRESS_KIT.md** link — saves them writing intro paragraphs
3. **Optional: free private API access / hosted demo** — most won't take it,but offering shows you're serious

## After someone agrees

Make their life easy:
- Send them a working `docker-compose.yml` pre-filled with safe demo values
- 5-min written setup instructions specific to their platform (most use Unraid)
- Offer a Discord/email check-in once they have it running
- Don't dictate what they say in the video — let them form an opinion

Best case: they review honestly, viewers click through, GitHub gets 500-5000 stars.

## Worst case

They don't respond. That's the median outcome. ~80% of cold influencer outreach gets ignored. Don't take it personally — they receive 20+ pitches a day.

The ones who reply once will probably reply again on v0.2 / v1.0 — keep the relationship warm with a single, low-effort update email per major release.

## Track in a simple sheet

| Name | Channel | Date sent | Replied? | Outcome |
|---|---|---|---|---|
| TechHut | YouTube | | | |
| Jim's Garage | YouTube | | | |
| ... | | | | |

A markdown file in this repo is fine — don't need anything fancy. The point is just to avoid double-emailing the same person.
