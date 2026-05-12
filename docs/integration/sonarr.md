# Sonarr integration

tgarr appears to Sonarr as a regular Newznab indexer. Sonarr does not need to know — and does not care — that the underlying source is Telegram rather than Usenet.

## Add tgarr as an indexer

1. In Sonarr: **Settings → Indexers → +** → **Newznab → Generic Newznab**.
2. Fill in:

   | Field | Value |
   |---|---|
   | Name | `tgarr` |
   | Enable RSS Sync | ✅ |
   | Enable Automatic Search | ✅ |
   | Enable Interactive Search | ✅ |
   | URL | `http://<tgarr-host>:8765` |
   | API Path | `/api` |
   | API Key | (none — leave blank, or use the key you set in `.env` if you enabled auth) |
   | Categories | `5030,5040,5045` (Standard TV / HD TV / UHD TV) |
   | Anime Categories | `5070` (optional) |
   | Animation: Standard Format Search | ❌ |
   | Additional Parameters | leave blank |
   | Indexer Priority | `25` (mid — Sonarr prefers lower priority if you also run NZBGeek) |
   | Download Client | leave blank to use Sonarr's default |
   | Tags | (optional) |

3. Click **Test** → should respond ✅ within a second.
4. Click **Save**.

## Configure a download client (the crucial step)

tgarr ships its own download worker that fetches Telegram files and drops them in your *arr completed folder. From Sonarr's perspective, **it's looking at a SABnzbd-style download client.**

The simplest setup: keep using your existing SABnzbd, and have tgarr's worker write completed files directly into SAB's `complete/tv/` folder — Sonarr's existing import logic picks them up exactly as it does with NZB downloads.

Sonarr → **Settings → Download Clients → +** (if not already configured):

| Field | Value |
|---|---|
| Name | `SABnzbd` |
| Host | `sabnzbd` or `<sab-host>` |
| Port | `8080` |
| URL Base | (blank) |
| API Key | your SAB API key |
| Category | `tv` |

tgarr's worker writes files using the same `category` convention, so Sonarr's `tv` category from SAB and tgarr both flow into the same import pipeline.

## What Sonarr sees during a search

```
Sonarr searches "Star Trek Strange New Worlds S03"
     ↓
GET http://tgarr:8765/api?t=tvsearch&q=Star+Trek+Strange+New+Worlds&season=3&apikey=...
     ↓
tgarr-api responds with Newznab XML:
  <item>
    <title>Star.Trek.Strange.New.Worlds.S03.2160p.WEB.H265-iVy</title>
    <link>http://tgarr:8765/api?t=get&id=GUID-1</link>
    <size>26214400000</size>
    <category>5045</category>
    ...
  </item>
     ↓
Sonarr applies its Quality Profile filters, picks the best release
     ↓
Sonarr GETs the .nzb file from http://tgarr:8765/api?t=get&id=GUID-1
     ↓
tgarr returns a stub NZB file pointing at SAB's "fake" mode
     ↓
SAB calls tgarr-worker → MTProto fetch → file streams to /downloads/complete/tv/
     ↓
Sonarr's import scanner picks up the new file, renames, moves to /tv library
     ↓
Jellyfin / Plex scans, episode shows up.
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Test` returns "Unable to connect" | tgarr-api not running, or wrong URL/port | `docker compose ps tgarr-api` |
| Searches return 0 results | tgarr crawler hasn't indexed enough channels yet | Check `docker compose logs crawler`, join more channels |
| Grab succeeds but no file arrives | tgarr-worker not running or path mismatch | Check worker logs + verify shared volume between tgarr-worker and SAB |
| Sonarr imports but renames wrong | Filename parser missed metadata | File an issue with the release name on GitHub |

## Quality profile recommendation

If you're transitioning away from NZB indexers, the same Quality Profile that worked for NZBGeek will work for tgarr — Telegram release names follow scene/web conventions virtually identical to what's posted on Usenet.

If you're new to *arr-style profiles, the typical starting point for HD media:

- **HD-1080p**: WEBDL-1080p · Bluray-1080p · WEBRip-1080p · HDTV-1080p
- **HD - 720p/1080p**: same as above + 720p variants (fallback for older shows where 1080p is rare)
- **Cutoff**: HDTV-720p (stop searching once we have 720p, only upgrade if explicitly requested)

The tgarr index doesn't currently expose anime category 5070 by default — that lands in v0.2.
