# Scripts and tools

The Media Center PIN keeps a child's browser to the Media Center. It is not the lock on tgarr's settings or on
downloading: a script, a cron job or another tool can do those with the install's own key.

1. Set a key in tgarr's `.env` and restart:

   ```
   TGARR_API_KEY=some-long-random-text
   ```

2. Send it in the `X-Api-Key` header. With it, the PIN does not apply:

   ```
   curl -X POST -H "X-Api-Key: some-long-random-text" -d "enabled=1" http://tgarr.local:8765/api/usenet/settings
   ```

Only in the header: tgarr does not take it from the address, so it is not left in URLs, browser history or logs.
The same key is the one Sonarr, Radarr and Prowlarr use (`apikey=` for `/newznab`, `/sabnzbd` and `/api`).

Keep the key away from the children's devices: whoever has it can change every setting.

**Before you set it, if Sonarr, Radarr or Prowlarr already use tgarr:** without a key tgarr accepts any `apikey`.
Once `TGARR_API_KEY` is set, `/newznab`, `/sabnzbd` and `/api` accept only that key, so put the same key in every
*arr entry for tgarr (indexer and download client) at the same time, or their searches and downloads stop.
