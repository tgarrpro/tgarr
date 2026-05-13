# Sonarr / Radarr / Lidarr integration

tgarr appears to your *arr stack as:

1. **A Newznab indexer** — Sonarr does not need to know that the underlying source is Telegram rather than Usenet.
2. **A SABnzbd download client** — tgarr emulates SAB so Sonarr can manage queue, history, and completed-download handling exactly as it would for a real Usenet downloader.

You configure both in the same Sonarr instance. No Sonarr modifications, plugins, or restarts required.

---

## 1. Add tgarr as an indexer

Sonarr **→ Settings → Indexers → ➕ Add → Newznab → Custom**

| Field | Value |
|---|---|
| Name | `tgarr` |
| Enable RSS | ✅ |
| Enable Automatic Search | ✅ |
| Enable Interactive Search | ✅ |
| URL | `https://tgarr.me/newznab/` (or your self-hosted origin URL) |
| API Path | `/api` (the default) |
| API Key | any string — tgarr does not enforce per-key auth in v0 |
| Categories | `5030, 5040, 5045` (TV) — Sonarr fills these automatically after Test |

Click **Test** — should return ✓.

Radarr uses categories `2030, 2040, 2045` (Movies). Same URL otherwise.

## 2. Add tgarr as a SABnzbd download client

Sonarr **→ Settings → Download Clients → ➕ Add → SABnzbd**

| Field | Value |
|---|---|
| Name | `tgarr` |
| Enable | ✅ |
| Host | `tgarr.me` (or your origin host) |
| Port | `443` |
| URL Base | `/sabnzbd` |
| Use SSL | ✅ |
| API Key | any string (same caveat as above) |
| Username / Password | (leave blank) |
| Category | `tv` for Sonarr, `movies` for Radarr |
| Recent Priority | Default |
| Older Priority | Default |
| Use SSL | ✅ if behind https |

Click **Test** — should return ✓.

## 3. Tell Sonarr where the completed file lives

When tgarr finishes a download, the file lands on the tgarr host at:

    /downloads/tgarr/<release-name>/<original-file-name>.<ext>

Sonarr needs to be able to read that path. Choose one:

### Option A — share via NFS (recommended for separate hosts)

On the tgarr host (`tgarr.me`):

```bash
apt install -y nfs-kernel-server
echo "/var/www/tgarr.me/app/_data/downloads  192.168.0.0/16(ro,sync,no_subtree_check)" >> /etc/exports
exportfs -ra
```

On your Sonarr host:

```bash
apt install -y nfs-common
mount -t nfs tgarr.me:/var/www/tgarr.me/app/_data/downloads /mnt/tgarr
```

Then in Sonarr **→ Settings → Download Clients → tgarr → Remote Path Mapping**:

| Host | Remote Path | Local Path |
|---|---|---|
| `tgarr.me` | `/downloads` | `/mnt/tgarr` |

### Option B — co-locate (single host)

If Sonarr and tgarr run on the same host (e.g., the same CasaOS / Synology), just mount the volume into Sonarr's container:

```yaml
sonarr:
  volumes:
    - /your/tgarr/path/_data/downloads:/downloads:ro
```

No remote path mapping needed.

### Option C — push to Sonarr's import folder via rsync (no shared mount)

Add a hook script to tgarr's worker that runs after each completed download. Out of scope for v0.1.

## 4. Test end-to-end

1. Sonarr **→ Series → ➕ Add → search for a show**.
2. Click the **interactive search** icon (magnifying glass on the episode row).
3. tgarr-indexed releases should appear.
4. Click ⬇ on one to grab it.
5. Watch **Activity → Queue** — should show the download with `tgarr` as client.
6. After the file lands on disk, Sonarr's CDH imports it into your library automatically.

## Troubleshooting

- **Sonarr "Test" on indexer fails with timeout** — your origin URL is wrong, or Cloudflare is blocking the path. Try `https://tgarr.me/newznab/api?t=caps` in a browser; should return XML.
- **Searches return 0 results** — check `tgarr` has crawled and indexed the channels you care about. Visit `https://tgarr.me/` for live stats; visit `https://tgarr.me/api/admin/releases` to browse indexed releases.
- **Download stays "downloading" forever** — `docker compose logs crawler` to see worker progress. Telegram throttles uploads on large files (Pyrogram FloodWait is normal); expect ~5–20 MB/s for video.
- **File downloaded but Sonarr does not import** — Sonarr cannot read the path. Check Remote Path Mapping in §3.

## Lidarr / Readarr

Same pattern. Use categories `3000-3999` for music (Lidarr), `7000-8050` for books (Readarr). tgarr's filename parser does not (yet) recognize music or book release patterns deeply; expect mixed results until v0.3.
