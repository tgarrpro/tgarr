# Sonarr / Radarr integration

tgarr appears to Sonarr and Radarr as:

1. **A Newznab indexer** - Sonarr searches it like any other indexer.
2. **A SABnzbd download client** - tgarr emulates SABnzbd so Sonarr can manage queue, history, and completed-download handling as it would for a real downloader.

You configure both in the same Sonarr instance. The addresses below are also shown on tgarr's Dashboard, under "Connect Sonarr / Radarr". `<your-host>` is the machine tgarr runs on; `8765` is the default port (`TGARR_PORT` in `.env`).

tgarr serves films and series, to Sonarr and Radarr only: its Newznab capabilities offer general, TV and movie search, and no audio or book search.

---

## 1. Add tgarr as an indexer

Sonarr **→ Settings → Indexers → ➕ Add → Newznab → Custom**

| Field | Value |
|---|---|
| Name | `tgarr` |
| URL | `http://<your-host>:8765/newznab/` |
| API Path | `/api` (the default) |
| API Key | see below |
| Categories | `5030, 5040, 5045` (TV) |

Click **Test** - should return ✓.

Radarr uses categories `2030, 2040, 2045` (Movies). Same URL otherwise.

**API key.** If `TGARR_API_KEY` is set in `.env`, enter that key: `/newznab`, `/sabnzbd` and `/api` refuse requests without it. If it is not set, any text is accepted.

## 2. Add tgarr as a SABnzbd download client

Sonarr **→ Settings → Download Clients → ➕ Add → SABnzbd**

| Field | Value |
|---|---|
| Name | `tgarr` |
| Host | `<your-host>` |
| Port | `8765` |
| URL Base | `/sabnzbd` |
| API Key | the same as for the indexer |
| Username / Password | (leave blank) |
| Category | `tv` for Sonarr, `movies` for Radarr |

Click **Test** - should return ✓.

## 3. Usenet: a second indexer

If you gave tgarr a Usenet account (Settings → Usenet), add a second Newznab indexer beside the "tgarr" one:

| Field | Value |
|---|---|
| URL | `http://<your-host>:8765/newznab/usenet` |
| API Path | `/api` |
| API Key | the same as for "tgarr" |
| Download client | your own SABnzbd or NZBGet - not "tgarr" |

A Usenet grab is an NZB. tgarr does not download films and series from Usenet itself: Sonarr hands the NZB to your Usenet downloader. An indexer goes to one download client, which is why Usenet needs its own entry. The indexer returns nothing while Usenet is switched off in tgarr.

## 4. Tell Sonarr where the completed file lives

When tgarr finishes a download, the file is inside tgarr's containers at:

    /downloads/tgarr/<release-name>/

On the host, `/downloads` is the folder set as `TGARR_DOWNLOADS` in `.env` (default `./_data/downloads`, next to `docker-compose.yml`). Sonarr needs to be able to read it.

### Same host

Mount the same folder into Sonarr's container as `/downloads`:

```yaml
sonarr:
  volumes:
    - /your/tgarr/path/_data/downloads:/downloads:ro
```

No remote path mapping needed.

### Separate hosts

Share the downloads folder with the Sonarr host (NFS, SMB) and mount it there, for example at `/mnt/tgarr`. Then in Sonarr **→ Settings → Download Clients → Remote Path Mappings**:

| Host | Remote Path | Local Path |
|---|---|---|
| `<your-host>` | `/downloads` | `/mnt/tgarr` |

Or, instead of a mapping, tell tgarr where its downloads are as Sonarr/Radarr see them: `TGARR_ARR_DOWNLOAD_ROOT=/mnt/tgarr/tgarr` in tgarr's `.env` (the folder that holds what tgarr downloads for the *arr). Without it tgarr sends its own path, `/downloads/tgarr/…`.

## 5. Test end-to-end

1. Sonarr **→ Series → ➕ Add → search for a show**.
2. Click the **interactive search** icon (magnifying glass on the episode row).
3. tgarr-indexed releases should appear.
4. Click ⬇ on one to grab it.
5. Watch **Activity → Queue** - should show the download with `tgarr` as client.
6. After the file lands on disk, Sonarr imports it into your library.

## Troubleshooting

- **Sonarr "Test" on indexer fails** - the URL is wrong or tgarr is not reachable from Sonarr. From the Sonarr machine, `curl "http://<your-host>:8765/newznab/api?t=caps"` should return XML.
- **Test fails with 401** - `TGARR_API_KEY` is set in `.env` and the API Key in Sonarr is not the same.
- **Searches return 0 results** - open `http://<your-host>:8765/releases` to see what tgarr has indexed.
- **Download stays "downloading"** - `docker compose logs crawler`, in the folder that holds your `docker-compose.yml`, shows the worker's progress. A `FloodWait` line is Telegram's rate limit; tgarr waits it out.
- **File downloaded but Sonarr does not import** - Sonarr cannot read the path. Check §4.
