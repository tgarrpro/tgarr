"""tgarr API — Newznab indexer + SABnzbd download-client emulation.

Sonarr/Radarr/Lidarr configure tgarr in two places:
1. Settings > Indexers > Add > Newznab → URL `http://<tgarr-host>:8765/newznab/`
2. Settings > Download Clients > Add > SABnzbd → Host `<tgarr-host>` Port `8765`
   URL Base `/sabnzbd/`

Sonarr search → newznab feed.
Sonarr grab → SAB `addurl` → row in `downloads` table → crawler's worker fetches
via MTProto → drops file in /downloads/tgarr/<release>/ → Sonarr's CDH imports.
"""
import asyncio
import html
import logging
import os
import re
import time
from typing import Optional

import asyncpg
from fastapi import FastAPI, Form, Header, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

import login  # local module
import metadata as md  # local module

DB_DSN = os.environ["DB_DSN"]
TGARR_VERSION = "0.4.0"
ANY_API_KEY_ACCEPTED = True

app = FastAPI(title="tgarr", version=TGARR_VERSION)
db_pool: Optional[asyncpg.Pool] = None


@app.on_event("startup")
async def _startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=8)
    await _migrate_schema()
    # Seed TMDB key from env into config table on first run only
    env_key = os.environ.get("TMDB_API_KEY", "").strip()
    if env_key:
        async with db_pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT value FROM config WHERE key='tmdb_api_key'")
            if not existing:
                await conn.execute(
                    "INSERT INTO config (key, value) VALUES ('tmdb_api_key', $1) "
                    "ON CONFLICT (key) DO NOTHING", env_key)
    asyncio.create_task(_metadata_worker())


async def _migrate_schema():
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            ALTER TABLE releases ADD COLUMN IF NOT EXISTS poster_url TEXT;
            ALTER TABLE releases ADD COLUMN IF NOT EXISTS canonical_title TEXT;
            ALTER TABLE releases ADD COLUMN IF NOT EXISTS overview TEXT;
            ALTER TABLE releases ADD COLUMN IF NOT EXISTS metadata_source TEXT;
            ALTER TABLE releases ADD COLUMN IF NOT EXISTS metadata_lookup_at TIMESTAMPTZ;
            CREATE INDEX IF NOT EXISTS idx_releases_meta_source ON releases (metadata_source);
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_type TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS thumb_path TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS thumb_md5 TEXT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS members_count INTEGER;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS meta_updated_at TIMESTAMPTZ;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS audience TEXT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS audience_manual BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS subscribed BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS last_polled_at TIMESTAMPTZ;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS subscribe_error TEXT;
            CREATE INDEX IF NOT EXISTS idx_channels_subscribed ON channels (subscribed, last_polled_at) WHERE subscribed;

            -- Channels pulled from registry.tgarr.me but NOT yet subscribed.
            -- Pure local catalog; user clicks Subscribe in /discover to start
            -- the no-join subscription_poller against them.
            CREATE TABLE IF NOT EXISTS discovered (
                username TEXT PRIMARY KEY,
                title TEXT,
                members_count INTEGER,
                media_count INTEGER,
                audience TEXT,
                language TEXT,
                category TEXT,
                distinct_contributors INTEGER,
                verified BOOLEAN,
                first_pulled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_pulled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dismissed BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE INDEX IF NOT EXISTS idx_discovered_audience ON discovered (audience);
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS local_path TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_title TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_performer TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_duration_sec INTEGER;
            CREATE INDEX IF NOT EXISTS idx_messages_media_type ON messages (media_type);
            CREATE INDEX IF NOT EXISTS idx_messages_thumb ON messages (thumb_path) WHERE thumb_path IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_messages_md5 ON messages (thumb_md5) WHERE thumb_md5 IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_messages_local ON messages (local_path) WHERE local_path IS NOT NULL;
        """)


async def _metadata_worker():
    """Continuously enrich releases with TMDB (if key) or Wikipedia metadata.
    Picks oldest-unfilled release, looks it up, persists. Sleeps 30s when no
    work. Short delay between lookups to respect rate limits.
    """
    wlog = logging.getLogger("tgarr.metaworker")
    wlog.info("metadata worker started")
    while True:
        try:
            async with db_pool.acquire() as conn:
                tmdb_key = await conn.fetchval(
                    "SELECT value FROM config WHERE key='tmdb_api_key'")
                row = await conn.fetchrow("""
                    SELECT id, category,
                           COALESCE(NULLIF(series_title,''), NULLIF(movie_title,''), name) AS title,
                           movie_year
                    FROM releases
                    WHERE metadata_source IS NULL
                    ORDER BY posted_at DESC NULLS LAST
                    LIMIT 1
                """)
            if not row:
                await asyncio.sleep(30)
                continue
            result = await md.lookup(
                row["category"] or "movie",
                row["title"] or "",
                row["movie_year"],
                tmdb_key=tmdb_key,
            )
            async with db_pool.acquire() as conn:
                if result:
                    await conn.execute("""
                        UPDATE releases SET
                          poster_url=$1, canonical_title=$2, overview=$3,
                          metadata_source=$4, metadata_lookup_at=NOW()
                        WHERE id=$5
                    """, result.get("poster_url"), result.get("canonical_title"),
                         result.get("overview"), result.get("source"), row["id"])
                else:
                    await conn.execute("""
                        UPDATE releases SET metadata_source='miss',
                          metadata_lookup_at=NOW()
                        WHERE id=$1
                    """, row["id"])
            await asyncio.sleep(0.3)
        except Exception as e:
            wlog.exception("metadata worker error: %s", e)
            await asyncio.sleep(5)


@app.on_event("shutdown")
async def _shutdown():
    if db_pool:
        await db_pool.close()


# ════════════════════════════════════════════════════════════════════
# Favicon (paper-plane SVG, served as /favicon.svg and /favicon.ico)
# ════════════════════════════════════════════════════════════════════
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="12" fill="#229ED9"/>'
    '<path d="M5.5 17 L19 12 L5.5 7 L5 11 L13 12 L5 13 Z" fill="white"/>'
    '</svg>'
)


@app.get("/favicon.svg")
@app.get("/favicon.ico")  # browsers auto-request this; serve same SVG payload
async def favicon():
    return Response(FAVICON_SVG, media_type="image/svg+xml")


# ════════════════════════════════════════════════════════════════════
# Thumb serving — crawler writes /downloads/thumbs/<uid>.jpg
# ════════════════════════════════════════════════════════════════════
import re as _re
from fastapi.responses import FileResponse
THUMBS_DIR = "/downloads/thumbs"
_THUMB_SAFE = _re.compile(r"^[A-Za-z0-9_\-]+\.jpg$")


@app.get("/thumbs/{fname}")
async def serve_thumb(fname: str):
    if not _THUMB_SAFE.match(fname):
        return Response("bad name", status_code=400)
    path = os.path.join(THUMBS_DIR, fname)
    if not os.path.exists(path):
        return Response("not found", status_code=404)
    return FileResponse(path, media_type="image/jpeg",
                       headers={"Cache-Control": "public, max-age=604800"})


@app.get("/media/{msg_id}")
async def serve_media(msg_id: int):
    """Serve a cached audio/document. FileResponse handles HTTP Range so audio
    players can seek mid-stream."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT local_path, file_name, mime_type, media_type
               FROM messages WHERE id=$1""", msg_id)
    if not row or not row["local_path"] or row["local_path"].startswith("__"):
        return Response("not cached", status_code=404)
    path = os.path.join("/downloads", row["local_path"])
    if not os.path.exists(path):
        return Response("file missing", status_code=404)
    mime = row["mime_type"] or (
        "audio/mpeg" if row["media_type"] == "audio" else "application/octet-stream")
    headers = {"Cache-Control": "private, max-age=3600"}
    fn = row["file_name"]
    return FileResponse(path, media_type=mime, filename=fn, headers=headers)


# ════════════════════════════════════════════════════════════════════
# Login (Telegram QR + SMS)
# ════════════════════════════════════════════════════════════════════
LOGIN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>tgarr — sign in to Telegram</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<style>
:root { --bg:#f5f7fa; --bg2:#ffffff; --fg:#1e293b; --muted:#64748b; --accent:#229ED9; --tg-blue:#229ED9; --border:#e2e8f0; --ok:#15803d; --bad:#b91c1c; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font:16px/1.55 -apple-system,Segoe UI,system-ui,sans-serif; background:var(--bg); color:var(--fg); min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px; }
.card { background:var(--bg2); border:1px solid var(--border); border-radius:10px; padding:40px; max-width:560px; width:100%; box-shadow:0 4px 12px rgba(15,23,42,0.06); }
.brand-row { display:flex; align-items:center; gap:14px; margin-bottom:8px; }
.brand-row .mark { width:52px; height:52px; color:var(--tg-blue); flex-shrink:0; }
.brand-row h1 { margin-bottom:0; }
h1 { color:var(--tg-blue); font-size:52px; font-weight:900; letter-spacing:-2.5px; margin-bottom:8px; line-height:0.9; }
h1 span { color:var(--fg); }
.tag { color:var(--muted); font-size:15px; text-transform:uppercase; letter-spacing:2px; font-weight:700; margin-bottom:24px; }
.sub { color:var(--muted); margin-bottom:28px; font-size:16px; line-height:1.55; }
.tabs { display:flex; gap:0; margin-bottom:28px; border-bottom:1px solid var(--border); }
.tab { padding:13px 24px; cursor:pointer; color:var(--muted); border-bottom:2px solid transparent; font-weight:600; font-size:16px; user-select:none; }
.tab.active { color:var(--accent); border-bottom-color:var(--accent); }
.panel { display:none; }
.panel.active { display:block; }
#qrimg { display:block; margin:20px auto; max-width:300px; padding:14px; background:#fff; border:1px solid var(--border); border-radius:8px; }
.status { padding:14px 18px; border-radius:6px; background:#e0f2fe; color:#0369a1; font-size:15px; margin-top:18px; min-height:48px; line-height:1.5; }
.status.error { background:#fee2e2; color:var(--bad); }
.status.ok { background:#dcfce7; color:var(--ok); }
input[type=text], input[type=tel], input[type=password] { width:100%; padding:12px 14px; background:#fff; color:var(--fg); border:1px solid var(--border); border-radius:6px; font-size:16px; font-family:inherit; margin-top:8px; }
input:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(34,158,217,0.15); }
label { display:block; margin-top:16px; font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:1px; font-weight:700; }
button { padding:12px 26px; background:var(--tg-blue); color:#fff; border:none; border-radius:6px; font-weight:600; cursor:pointer; font-size:16px; margin-top:18px; }
button:hover { background:#1e88c5; }
button:disabled { opacity:0.4; cursor:not-allowed; }
.hint { color:var(--muted); font-size:15px; margin-top:10px; line-height:1.55; }
code { background:#f1f5f9; padding:3px 8px; border-radius:4px; color:#0369a1; font-family:ui-monospace,SF Mono,Menlo,monospace; font-size:14px; border:1px solid var(--border); }
</style>
</head><body>
<div class="card">
  <div class="brand-row">
    <svg class="mark" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
    <h1>tg<span>arr</span></h1>
  </div>
  <div class="tag">sign in to Telegram</div>
  <div class="sub">tgarr needs to log in to your Telegram account once. Session is stored locally — never leaves your tgarr instance.</div>

  <div class="tabs">
    <div class="tab active" onclick="show('qr')">Scan QR</div>
    <div class="tab" onclick="show('sms')">Phone + SMS</div>
  </div>

  <div id="panel-qr" class="panel active">
    <p class="hint">Open Telegram on your phone → <b>Settings → Devices → Link Desktop Device</b>, then scan:</p>
    <img id="qrimg" alt="QR code" />
    <div id="qr-status" class="status">starting…</div>
  </div>

  <div id="panel-sms" class="panel">
    <p class="hint">tgarr will send a code to your Telegram account.</p>
    <label>Phone (with country code)</label>
    <input id="phone" type="tel" placeholder="+12815551234" />
    <button id="send-btn" onclick="smsSend()">Send code</button>
    <div id="code-row" style="display:none">
      <label>Code from Telegram</label>
      <input id="code" type="text" placeholder="12345" />
      <label id="pw-label" style="display:none">2FA password (if you set one)</label>
      <input id="password" type="password" style="display:none" />
      <button onclick="smsVerify()">Sign in</button>
    </div>
    <div id="sms-status" class="status"></div>
  </div>
</div>
<script>
function show(t) {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('panel-' + t).classList.add('active');
  if (t === 'qr' && !window._qrStarted) qrStart();
}
async function qrStart() {
  window._qrStarted = true;
  const r = await fetch('/api/login/qr/start', {method: 'POST'}).then(r => r.json());
  if (r.qr_png_base64) {
    document.getElementById('qrimg').src = 'data:image/png;base64,' + r.qr_png_base64;
    setStatus('qr', r.message, '');
    qrPoll();
  } else if (r.status === 'already_authed') {
    setStatus('qr', 'already signed in — redirecting…', 'ok');
    setTimeout(() => location.href = '/', 1000);
  } else {
    setStatus('qr', r.message || 'error', 'error');
  }
}
async function qrPoll() {
  while (true) {
    await new Promise(r => setTimeout(r, 2000));
    const s = await fetch('/api/login/qr/status').then(r => r.json());
    if (s.status === 'success') {
      setStatus('qr', s.message + ' — redirecting…', 'ok');
      setTimeout(() => location.href = '/', 1500);
      return;
    }
    if (s.status === 'error') {
      setStatus('qr', s.message, 'error');
      return;
    }
  }
}
async function smsSend() {
  const phone = document.getElementById('phone').value.trim();
  if (!phone) return;
  document.getElementById('send-btn').disabled = true;
  const r = await fetch('/api/login/sms/send', {
    method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: 'phone=' + encodeURIComponent(phone),
  }).then(r => r.json());
  if (r.status === 'sms_sent') {
    setStatus('sms', r.message, 'ok');
    document.getElementById('code-row').style.display = 'block';
  } else {
    setStatus('sms', r.message || 'error', 'error');
    document.getElementById('send-btn').disabled = false;
  }
}
async function smsVerify() {
  const code = document.getElementById('code').value.trim();
  const password = document.getElementById('password').value;
  if (!code) return;
  const body = 'code=' + encodeURIComponent(code) +
    (password ? '&password=' + encodeURIComponent(password) : '');
  const r = await fetch('/api/login/sms/verify', {
    method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: body,
  }).then(r => r.json());
  if (r.status === 'success') {
    setStatus('sms', r.message + ' — redirecting…', 'ok');
    setTimeout(() => location.href = '/', 1500);
  } else if (r.status === 'needs_2fa') {
    document.getElementById('pw-label').style.display = 'block';
    document.getElementById('password').style.display = 'block';
    setStatus('sms', '2FA password required — enter then click Sign in again', '');
  } else {
    setStatus('sms', r.message || 'error', 'error');
  }
}
function setStatus(tab, msg, cls) {
  const e = document.getElementById(tab + '-status');
  e.textContent = msg;
  e.className = 'status' + (cls ? ' ' + cls : '');
}
qrStart();
</script>
</body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page(preview: Optional[int] = None):
    if login.session_exists() and not preview:
        return RedirectResponse("/")
    return HTMLResponse(LOGIN_HTML)


@app.post("/api/login/qr/start")
async def api_login_qr_start():
    return await login.qr_start()


@app.get("/api/login/qr/status")
async def api_login_qr_status():
    return login.qr_status()


@app.post("/api/login/sms/send")
async def api_login_sms_send(phone: str = Form(...)):
    return await login.sms_send(phone)


@app.post("/api/login/sms/verify")
async def api_login_sms_verify(code: str = Form(...), password: Optional[str] = Form(None)):
    return await login.sms_verify(code, password)


@app.post("/api/login/logout")
async def api_login_logout():
    return login.logout()


@app.post("/api/photo/{msg_id}/delete")
async def api_photo_delete(msg_id: int):
    """Delete the cached file + mark all rows sharing the MD5 as deleted.
    Cascading by md5 prevents 404s in gallery when one viral image was
    referenced by many channel rows. Worker skips `__deleted__` so it won't
    reappear — unless POST /api/photo/<id>/redownload is called."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT thumb_path, thumb_md5 FROM messages WHERE id=$1", msg_id)
        if not row:
            return JSONResponse({"status": "not_found"}, status_code=404)
        if row["thumb_path"] and not row["thumb_path"].startswith("__"):
            path = os.path.join(THUMBS_DIR, row["thumb_path"])
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        if row["thumb_md5"]:
            n = await conn.fetchval(
                """UPDATE messages SET thumb_path='__deleted__'
                   WHERE thumb_md5 = $1
                   RETURNING (SELECT count(*) FROM messages WHERE thumb_md5=$1)""",
                row["thumb_md5"])
        else:
            await conn.execute(
                "UPDATE messages SET thumb_path='__deleted__' WHERE id=$1", msg_id)
            n = 1
    return {"status": "deleted", "rows_marked": n or 1}


@app.post("/api/photo/{msg_id}/redownload")
async def api_photo_redownload(msg_id: int):
    """Force re-download: clear thumb_path so worker picks it up again."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE messages SET thumb_path=NULL WHERE id=$1 "
            "AND thumb_path IS NOT NULL", msg_id)
    return {"status": "queued"}


@app.post("/api/grab/{guid}")
async def api_grab(guid: str):
    """User clicks Grab in tgarr UI → queue download directly. Avoids the
    Newznab `t=get` path that returns an .nzb file (only useful to Sonarr).
    """
    async with db_pool.acquire() as conn:
        rel = await conn.fetchrow(
            "SELECT id, name FROM releases WHERE guid = $1", guid)
        if not rel:
            return JSONResponse({"status": "error", "message": "release not found"},
                              status_code=404)
        # Don't double-queue
        existing = await conn.fetchval(
            """SELECT status FROM downloads
               WHERE release_id = $1
                 AND status IN ('pending','downloading','completed')
               LIMIT 1""", rel["id"])
        if existing:
            return {"status": "exists", "existing_status": existing, "name": rel["name"]}
        await conn.execute(
            "INSERT INTO downloads (release_id, status) VALUES ($1, 'pending')",
            rel["id"])
    return {"status": "queued", "name": rel["name"]}


async def _is_nsfw_enabled() -> bool:
    """Adult content is gated behind an explicit user opt-in. Off by default."""
    async with db_pool.acquire() as conn:
        v = await conn.fetchval("SELECT value FROM config WHERE key='nsfw_enabled'")
    return v == "true"


@app.post("/api/settings/nsfw_enabled")
async def settings_nsfw_enabled(value: str = Form("false")):
    val = "true" if value == "true" else "false"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO config (key, value) VALUES ('nsfw_enabled', $1)
               ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=NOW()""", val)
    return RedirectResponse("/settings", status_code=302)


@app.post("/api/settings/tmdb_key")
async def settings_tmdb_key(value: str = Form("")):
    value = value.strip()
    # Masked echo ("abcdef…1234") — user didn't change it
    if "…" in value or value.count("*") >= 4:
        return RedirectResponse("/settings", status_code=302)
    async with db_pool.acquire() as conn:
        if not value:
            await conn.execute("DELETE FROM config WHERE key='tmdb_api_key'")
        else:
            await conn.execute("""
                INSERT INTO config (key, value) VALUES ('tmdb_api_key', $1)
                ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=NOW()
            """, value)
        # Reset wikipedia/miss results so the next worker pass retries with new key
        await conn.execute("""
            UPDATE releases SET metadata_source=NULL, poster_url=NULL,
                   canonical_title=NULL, overview=NULL, metadata_lookup_at=NULL
            WHERE metadata_source IN ('miss', 'wikipedia')
        """)
    return RedirectResponse("/settings", status_code=302)


# ════════════════════════════════════════════════════════════════════
# UI — sidebar + topbar layout (Sonarr/Radarr-style)
# ════════════════════════════════════════════════════════════════════
CSS = """
:root {
  --bg:#f5f7fa; --surface:#ffffff; --surface2:#f8fafc; --border:#e2e8f0;
  --fg:#1e293b; --muted:#64748b;
  --accent:#229ED9; --accent-hi:#0f7fb8;
  --ok:#15803d; --warn:#a16207; --bad:#b91c1c;
  --shadow:0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04);
}
* { box-sizing:border-box; margin:0; padding:0; }
html, body { height:100%; }
body { font:16px/1.55 -apple-system,Segoe UI,system-ui,sans-serif; background:var(--bg); color:var(--fg); display:flex; }

aside.nav { width:260px; min-width:260px; background:var(--surface); border-right:1px solid var(--border); display:flex; flex-direction:column; }
.brand { padding:28px 22px 24px; border-bottom:1px solid var(--border); }
.brand .logo-row { display:flex; align-items:center; gap:12px; }
.brand .mark { width:46px; height:46px; flex-shrink:0; color:var(--accent); }
.brand .logo { font-size:48px; font-weight:900; letter-spacing:-2.5px; color:var(--accent); line-height:0.9; }
.brand .logo span { color:var(--fg); }
.brand .ver { font-size:12px; color:var(--muted); margin-top:10px; text-transform:uppercase; letter-spacing:1.5px; font-weight:700; }
.navlinks { padding:16px 0; flex:1; overflow-y:auto; }
.navlink { display:flex; align-items:center; gap:14px; padding:13px 22px; color:#475569; text-decoration:none; font-weight:500; border-left:3px solid transparent; font-size:16px; }
.navlink:hover { background:#f1f5f9; color:var(--fg); }
.navlink.active { color:var(--accent); background:#e0f2fe; border-left-color:var(--accent); font-weight:600; }
.navlink svg { width:22px; height:22px; flex-shrink:0; }
.user { padding:18px 22px; border-top:1px solid var(--border); font-size:14px; color:var(--muted); }
.user .name { color:var(--fg); font-weight:600; font-size:15px; }
.user a { color:var(--accent); text-decoration:none; font-size:14px; font-weight:600; }

main.main { flex:1; display:flex; flex-direction:column; min-width:0; }
.topbar { height:72px; background:var(--surface); border-bottom:1px solid var(--border); display:flex; align-items:center; padding:0 32px; gap:24px; flex-shrink:0; }
.topbar h1 { font-size:26px; font-weight:700; }
.topbar .actions { margin-left:auto; display:flex; gap:12px; align-items:center; }
.topbar input.search { background:#fff; border:1px solid var(--border); color:var(--fg); padding:10px 16px; border-radius:6px; width:340px; font-size:16px; font-family:inherit; }
.topbar input.search:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(34,158,217,0.15); }

.content { flex:1; overflow-y:auto; padding:36px 40px; }

.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:16px; margin-bottom:36px; }
.stat-card { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:24px 26px; box-shadow:var(--shadow); }
.stat-card .n { font-size:38px; font-weight:700; color:var(--accent); line-height:1; }
.stat-card .l { font-size:14px; color:var(--muted); text-transform:uppercase; letter-spacing:1.5px; margin-top:12px; font-weight:600; }

h2.section { font-size:15px; text-transform:uppercase; letter-spacing:2px; color:var(--fg); margin:40px 0 18px; font-weight:700; display:flex; align-items:center; gap:14px; }
h2.section .count { color:var(--accent); font-size:15px; font-weight:700; }
h2.section .extra { margin-left:auto; }
h2.section a { color:var(--accent); text-decoration:none; font-size:14px; font-weight:600; }

.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:18px; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:8px; overflow:hidden; box-shadow:var(--shadow); transition:transform 0.1s, box-shadow 0.1s; }
.card:hover { transform:translateY(-2px); box-shadow:0 8px 20px rgba(15,23,42,0.10); }
.card .avatar { aspect-ratio:16/9; display:flex; align-items:center; justify-content:center; font-size:54px; font-weight:700; color:#fff; letter-spacing:-2px; text-shadow:0 2px 4px rgba(0,0,0,0.18); }
.card .body { padding:14px 18px; }
.card .title { font-weight:700; font-size:16px; line-height:1.35; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.card .sub { font-size:14px; color:var(--muted); margin-top:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.card .footer { display:flex; gap:6px; padding:10px 18px; background:var(--surface2); border-top:1px solid var(--border); flex-wrap:wrap; }

.pill { display:inline-block; padding:3px 12px; border-radius:11px; font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:0.8px; }
.pill.ok { background:#dcfce7; color:var(--ok); }
.pill.warn { background:#fef3c7; color:var(--warn); }
.pill.bad { background:#fee2e2; color:var(--bad); }
.pill.muted { background:#f1f5f9; color:#475569; }
.pill.accent { background:#e0f2fe; color:#0369a1; }

table { width:100%; border-collapse:collapse; background:var(--surface); border:1px solid var(--border); border-radius:8px; overflow:hidden; font-size:15px; box-shadow:var(--shadow); }
thead th { background:var(--surface2); color:#475569; font-size:13px; text-transform:uppercase; letter-spacing:1.5px; font-weight:700; padding:14px 16px; text-align:left; border-bottom:1px solid var(--border); }
tbody td { padding:14px 16px; border-bottom:1px solid var(--border); color:var(--fg); }
tbody tr:last-child td { border-bottom:none; }
tbody tr:hover td { background:#f8fafc; }
tbody td.right { text-align:right; }
tbody td.num { font-variant-numeric:tabular-nums; color:#475569; }
tbody td.name { font-weight:600; color:var(--fg); }

.dl-list { background:var(--surface); border:1px solid var(--border); border-radius:8px; overflow:hidden; box-shadow:var(--shadow); }
.dl-item { padding:18px 22px; border-bottom:1px solid var(--border); display:grid; grid-template-columns:28px 1fr auto; gap:18px; align-items:center; }
.dl-item:last-child { border-bottom:none; }
.dl-item:hover { background:#f8fafc; }
.dl-item .icon { font-size:22px; line-height:1; }
.dl-item .info .name { font-weight:600; font-size:16px; color:var(--fg); }
.dl-item .info .meta { font-size:14px; color:var(--muted); margin-top:5px; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.dl-item .info .err { font-size:13px; color:var(--bad); margin-top:5px; font-family:ui-monospace,monospace; }
.dl-item .right { text-align:right; font-size:14px; color:var(--muted); white-space:nowrap; }

.empty-state { text-align:center; padding:56px 24px; color:var(--muted); background:var(--surface); border:1px dashed var(--border); border-radius:8px; font-size:16px; }
.empty-state .icon { font-size:42px; opacity:0.4; margin-bottom:14px; }

input[type=text], input[type=tel], input[type=password] { background:#fff; border:1px solid var(--border); color:var(--fg); padding:11px 14px; border-radius:6px; font-size:16px; font-family:inherit; }
input:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(34,158,217,0.15); }
button, .btn { padding:10px 22px; background:var(--accent); color:#fff; border:none; border-radius:6px; font-weight:600; font-size:16px; cursor:pointer; text-decoration:none; display:inline-block; font-family:inherit; box-shadow:var(--shadow); }
button:hover, .btn:hover { background:var(--accent-hi); }
button.ghost, .btn.ghost { background:#fff; border:1px solid var(--border); color:var(--fg); box-shadow:none; }
button.ghost:hover, .btn.ghost:hover { border-color:var(--accent); color:var(--accent); background:#f8fafc; }

code { background:#f1f5f9; padding:3px 8px; border-radius:4px; color:#0369a1; font-family:ui-monospace,SF Mono,Menlo,monospace; font-size:14px; border:1px solid var(--border); }

/* ── Poster grid (Sonarr/Radarr style) ─────────────────── */
.poster-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:18px; }
.poster-card { background:var(--surface); border:1px solid var(--border); border-radius:8px; overflow:hidden; box-shadow:var(--shadow); display:flex; flex-direction:column; transition:transform 0.12s, box-shadow 0.12s; }
.poster-card:hover { transform:translateY(-3px); box-shadow:0 10px 24px rgba(15,23,42,0.12); border-color:var(--accent); }
.poster-card .poster { aspect-ratio:2/3; background:#f1f5f9; background-size:cover; background-position:center; position:relative; }
.poster-card .poster .fallback { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:56px; color:#cbd5e1; }
.poster-card .poster .badge { position:absolute; top:8px; right:8px; padding:3px 10px; border-radius:11px; background:rgba(15,23,42,0.78); color:#fff; font-size:11px; font-weight:700; letter-spacing:0.5px; text-transform:uppercase; backdrop-filter:blur(4px); }
.poster-card .info { padding:12px 14px 8px; flex:1; }
.poster-card .info .title { font-weight:700; font-size:15px; line-height:1.3; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; min-height:39px; color:var(--fg); }
.poster-card .info .sub { font-size:13px; color:var(--muted); margin-top:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.poster-card .pills { padding:0 14px 10px; display:flex; gap:5px; flex-wrap:wrap; }
.poster-card .grab-row { padding:8px 14px 12px; border-top:1px solid var(--border); margin-top:auto; background:var(--surface2); }
.poster-card .grab-row .btn { width:100%; text-align:center; padding:8px; font-size:13px; }
.view-toggle { display:inline-flex; border:1px solid var(--border); border-radius:6px; overflow:hidden; }
.view-toggle a { padding:6px 14px; color:var(--muted); text-decoration:none; font-size:13px; font-weight:600; }
.view-toggle a.active { background:var(--accent); color:#fff; }

/* ── Image gallery ─────────────────── */
.gallery { columns:5 240px; column-gap:14px; }
.gallery .item { break-inside:avoid; margin-bottom:14px; position:relative; border-radius:8px; overflow:hidden; cursor:zoom-in; box-shadow:var(--shadow); }
.gallery .item img { width:100%; display:block; background:#f1f5f9; transition:transform 0.15s; }
.gallery .item:hover img { transform:scale(1.02); }
.gallery .item .meta { position:absolute; left:0; right:0; bottom:0; padding:18px 12px 8px; background:linear-gradient(transparent, rgba(0,0,0,0.78)); color:#fff; font-size:12px; opacity:0; transition:opacity 0.15s; }
.gallery .item:hover .meta { opacity:1; }
.gallery .item .meta .ch { font-weight:700; }
.gallery .item .meta .cap { margin-top:4px; line-height:1.35; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.gallery .item .dup-badge { position:absolute; top:8px; right:8px; padding:3px 10px; border-radius:11px; background:rgba(34,158,217,0.92); color:#fff; font-size:11px; font-weight:800; letter-spacing:0.5px; backdrop-filter:blur(4px); }

/* ── Lightbox + slideshow ─────────────────── */
.lightbox { position:fixed; inset:0; background:rgba(15,23,42,0.96); display:none; align-items:center; justify-content:center; z-index:9999; padding:20px; overflow:hidden; }
.lightbox.open { display:flex; }
.lightbox img { max-width:94vw; max-height:88vh; border-radius:6px; box-shadow:0 16px 50px rgba(0,0,0,0.5); will-change:transform,opacity,filter; }
.lightbox .lb-meta { position:absolute; left:32px; right:32px; bottom:80px; color:#fff; font-size:15px; line-height:1.55; max-width:660px; pointer-events:none; transition:opacity 0.3s; }
.lightbox .lb-meta .ch { color:var(--accent-hi); font-weight:700; font-size:13px; text-transform:uppercase; letter-spacing:1px; margin-bottom:4px; }
.lightbox .lb-close { position:absolute; top:18px; right:24px; color:#fff; font-size:36px; cursor:pointer; opacity:0.6; line-height:1; padding:6px 12px; background:none; border:none; }
.lightbox .lb-close:hover { opacity:1; }
.lightbox .lb-prev, .lightbox .lb-next { position:absolute; top:50%; transform:translateY(-50%); width:56px; height:56px; border-radius:50%; background:rgba(255,255,255,0.10); color:#fff; border:none; font-size:32px; cursor:pointer; display:flex; align-items:center; justify-content:center; padding-bottom:4px; backdrop-filter:blur(6px); transition:background 0.2s, transform 0.2s; opacity:0.7; }
.lightbox .lb-prev { left:24px; } .lightbox .lb-next { right:24px; }
.lightbox .lb-prev:hover, .lightbox .lb-next:hover { opacity:1; background:rgba(255,255,255,0.20); }
.lightbox .lb-controls { position:absolute; bottom:24px; left:50%; transform:translateX(-50%); display:flex; align-items:center; gap:14px; padding:8px 16px; background:rgba(255,255,255,0.08); border-radius:24px; backdrop-filter:blur(8px); }
.lightbox .lb-controls button { background:none; border:none; color:#fff; font-size:20px; cursor:pointer; padding:4px 10px; line-height:1; opacity:0.85; }
.lightbox .lb-controls button:hover { opacity:1; }
.lightbox .lb-count { color:#fff; font-size:13px; font-variant-numeric:tabular-nums; opacity:0.85; min-width:60px; text-align:center; }
.lightbox .lb-progress { position:absolute; top:0; left:0; right:0; height:3px; background:rgba(255,255,255,0.10); }
.lightbox .lb-progress-bar { height:100%; background:var(--accent); width:0; transition:width linear; }

/* ── 8 transition effects ─────────────────── */
@keyframes fxFade        { from { opacity:0 } to { opacity:1 } }
@keyframes fxZoomIn      { from { opacity:0; transform:scale(0.65) } to { opacity:1; transform:scale(1) } }
@keyframes fxZoomOut     { from { opacity:0; transform:scale(1.35) } to { opacity:1; transform:scale(1) } }
@keyframes fxSlideRight  { from { opacity:0; transform:translateX(100px) } to { opacity:1; transform:translateX(0) } }
@keyframes fxSlideLeft   { from { opacity:0; transform:translateX(-100px) } to { opacity:1; transform:translateX(0) } }
@keyframes fxFlip        { from { opacity:0; transform:perspective(1000px) rotateY(70deg) } to { opacity:1; transform:perspective(1000px) rotateY(0) } }
@keyframes fxRotateScale { from { opacity:0; transform:rotate(-12deg) scale(0.6) } to { opacity:1; transform:rotate(0) scale(1) } }
@keyframes fxBlur        { from { opacity:0; filter:blur(28px); transform:scale(1.08) } to { opacity:1; filter:blur(0); transform:scale(1) } }

.fxFade        { animation:fxFade        0.7s cubic-bezier(0.4,0,0.2,1) both; }
.fxZoomIn      { animation:fxZoomIn      0.8s cubic-bezier(0.34,1.56,0.64,1) both; }
.fxZoomOut     { animation:fxZoomOut     0.75s cubic-bezier(0.4,0,0.2,1) both; }
.fxSlideRight  { animation:fxSlideRight  0.7s cubic-bezier(0.16,1,0.3,1) both; }
.fxSlideLeft   { animation:fxSlideLeft   0.7s cubic-bezier(0.16,1,0.3,1) both; }
.fxFlip        { animation:fxFlip        0.9s cubic-bezier(0.4,0,0.2,1) both; }
.fxRotateScale { animation:fxRotateScale 0.85s cubic-bezier(0.34,1.56,0.64,1) both; }
.fxBlur        { animation:fxBlur        0.7s cubic-bezier(0.4,0,0.2,1) both; }

/* ── Music page ─────────────────── */
.music-list tr { cursor:pointer; transition:background 0.1s; }
.music-list .play-cell { width:48px; text-align:center; }
.music-list .play-ico { width:34px; height:34px; line-height:34px; border-radius:50%; background:#e0f2fe; color:var(--accent); display:inline-block; font-size:14px; }
.music-list tr:hover .play-ico { background:var(--accent); color:#fff; }
.music-list tr.playing td { background:#e0f2fe; }
.music-list tr.playing .play-ico { background:var(--ok); color:#fff; }
.music-list .title-cell .t { font-weight:600; font-size:15px; color:var(--fg); }
.music-list .title-cell .a { font-size:13px; color:var(--muted); margin-top:3px; }
.player-bar { position:fixed; bottom:0; left:260px; right:0; background:var(--surface); border-top:1px solid var(--border); padding:14px 24px; display:flex; align-items:center; gap:16px; box-shadow:0 -6px 16px rgba(15,23,42,0.06); z-index:100; }
.player-bar .np { display:flex; align-items:center; gap:12px; min-width:220px; }
.player-bar .np-icon { width:46px; height:46px; background:var(--surface2); border:1px solid var(--border); border-radius:6px; display:flex; align-items:center; justify-content:center; font-size:22px; }
.player-bar .np-t { font-weight:600; font-size:14px; max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.player-bar .np-a { font-size:13px; color:var(--muted); max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:2px; }
.player-bar audio { height:42px; }
.player-bar button { padding:9px 14px; font-size:18px; line-height:1; }
.music-list + .player-bar + .content { padding-bottom:100px; }
body:has(.player-bar) .content { padding-bottom:110px; }

/* ── Library page ─────────────────── */
.book-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(380px, 1fr)); gap:16px; }
.book-card { display:flex; gap:16px; padding:18px 20px; background:var(--surface); border:1px solid var(--border); border-radius:8px; box-shadow:var(--shadow); align-items:center; transition:transform 0.1s, box-shadow 0.1s; }
.book-card:hover { transform:translateY(-2px); box-shadow:0 8px 20px rgba(15,23,42,0.10); }
.book-card .book-ico { font-size:48px; flex-shrink:0; line-height:1; }
.book-card .book-body { flex:1; min-width:0; }
.book-card .book-title { font-weight:700; font-size:15px; line-height:1.35; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.book-card .book-meta { margin-top:8px; font-size:13px; color:var(--muted); display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.book-card .book-actions { display:flex; flex-direction:column; gap:6px; flex-shrink:0; }
.book-card .book-actions .btn { padding:6px 14px; font-size:13px; text-align:center; min-width:96px; }
"""

NAV_ITEMS = [
    ("/",          "Dashboard", "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"),
    ("/channels",  "Channels",  "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm-1-13h2v6h-2zm0 8h2v2h-2z"),
    ("/discover",  "Discover",  "M12 10.9c-.61 0-1.1.49-1.1 1.1s.49 1.1 1.1 1.1c.61 0 1.1-.49 1.1-1.1s-.49-1.1-1.1-1.1zM12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm2.19 12.19L6 18l3.81-8.19L18 6l-3.81 8.19z"),
    ("/releases",  "Releases",  "M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-1 9H9V9h10v2zm-4 4H9v-2h6v2zm4-8H9V5h10v2z"),
    ("/gallery",   "Gallery",   "M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"),
    ("/music",     "Music",     "M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"),
    ("/library",   "Library",   "M18 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 18H6V4h7v6l2-1 2 1V4h1v16z"),
    ("/downloads", "Downloads", "M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"),
    ("/search",    "Search",    "M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"),
    ("/settings",  "Settings",  "M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94 0 .31.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"),
]


def _icon(svg_path: str) -> str:
    return f'<svg viewBox="0 0 24 24" fill="currentColor"><path d="{svg_path}"/></svg>'


def _fmt_size(n):
    if not n:
        return ""
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}".replace(".0 ", " ")
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_time(t):
    return t.strftime("%Y-%m-%d %H:%M") if t else ""


def _color_for(s: str) -> str:
    """Deterministic accent color for an avatar."""
    palette = ["#229ED9", "#7c3aed", "#06b6d4", "#10b981", "#f59e0b",
               "#ec4899", "#ef4444", "#8b5cf6", "#14b8a6", "#f97316"]
    return palette[sum(ord(c) for c in (s or "?")) % len(palette)]


def _user_block() -> str:
    if login.session_exists():
        user = login.state.user_info or {}
        name = user.get("username") or user.get("first_name") or "anonymous"
        return f'<div class="name">@{html.escape(str(name))}</div><div>signed in</div>'
    return '<div class="name">not signed in</div><a href="/login">sign in →</a>'


def _layout(title: str, active: str, body_html: str, *, page_title: Optional[str] = None,
            top_actions_html: Optional[str] = None) -> str:
    nav = "".join(
        f'<a href="{path}" class="navlink{" active" if path == active else ""}">'
        f'{_icon(svg)}<span>{name}</span></a>'
        for path, name, svg in NAV_ITEMS
    )
    if top_actions_html is None:
        top_actions_html = (
            '<form action="/search" method="GET" style="margin-left:auto">'
            '<input type="text" name="q" class="search" placeholder="Search releases…" />'
            '</form>'
        )
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>{html.escape(title)} · tgarr</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<style>{CSS}</style>
</head><body>
<aside class="nav">
  <div class="brand">
    <div class="logo-row">
      <svg class="mark" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
      <div class="logo">tg<span>arr</span></div>
    </div>
    <div class="ver">v{TGARR_VERSION}</div>
  </div>
  <div class="navlinks">{nav}</div>
  <div class="user">{_user_block()}</div>
</aside>
<main class="main">
  <header class="topbar">
    <h1>{html.escape(page_title or title)}</h1>
    {top_actions_html}
  </header>
  <div class="content">{body_html}</div>
</main>
<script>
async function tgGrab(btn) {{
  const guid = btn.dataset.guid;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = '...';
  try {{
    const r = await fetch('/api/grab/' + guid, {{method: 'POST'}});
    const j = await r.json();
    if (j.status === 'queued') {{
      btn.textContent = '✓ Queued';
      btn.style.background = 'var(--ok)';
    }} else if (j.status === 'exists') {{
      btn.textContent = '· already ' + j.existing_status;
      btn.style.background = 'var(--muted)';
    }} else {{
      btn.textContent = '✗ ' + (j.message || 'error');
      btn.style.background = 'var(--bad)';
      setTimeout(() => {{ btn.textContent = orig; btn.style.background = ''; btn.disabled = false; }}, 2500);
    }}
  }} catch (e) {{
    btn.textContent = '✗ network';
    btn.style.background = 'var(--bad)';
    setTimeout(() => {{ btn.textContent = orig; btn.style.background = ''; btn.disabled = false; }}, 2500);
  }}
}}
</script>
</body></html>"""


# ════════════════════════════════════════════════════════════════════
# Page: Dashboard
# ════════════════════════════════════════════════════════════════════
@app.get("/")
async def root(accept: Optional[str] = Header(None)):
    async with db_pool.acquire() as conn:
        stats = await conn.fetchrow(
            """SELECT (SELECT count(*) FROM channels)                AS channels,
                      (SELECT count(*) FROM messages)                AS messages,
                      (SELECT count(*) FROM releases)                AS releases,
                      (SELECT count(*) FROM downloads
                         WHERE status='pending')                     AS pending,
                      (SELECT count(*) FROM downloads
                         WHERE status='downloading')                 AS downloading,
                      (SELECT count(*) FROM downloads
                         WHERE status='completed')                   AS completed""")

    # An indexed-channel count is a strong fallback indicator that the
    # crawler is authed — even if the disk session file got truncated by
    # restarts. Prevents bouncing a working instance back to /login.
    authed = login.session_exists() or stats["channels"] > 0

    wants_html = accept and "text/html" in accept
    if not wants_html:
        return {"tgarr": TGARR_VERSION, "authed": authed, **dict(stats)}

    if not authed:
        return RedirectResponse("/login")

    async with db_pool.acquire() as conn:
        recent = await conn.fetch(
            """SELECT d.id, d.status, d.local_path, d.requested_at, d.finished_at,
                      d.error_message, r.name, r.size_bytes
               FROM downloads d
               JOIN releases r ON r.id = d.release_id
               ORDER BY d.requested_at DESC LIMIT 10""")

    s = dict(stats)
    stats_html = "".join(
        f'<div class="stat-card"><div class="n">{v:,}</div><div class="l">{label}</div></div>'
        for v, label in [
            (s["channels"], "channels"),
            (s["messages"], "messages indexed"),
            (s["releases"], "parsed releases"),
            (s["pending"], "pending"),
            (s["downloading"], "downloading"),
            (s["completed"], "completed"),
        ]
    )

    if recent:
        items = []
        for d in recent:
            icon = ({"pending": "⏳", "downloading": "⬇", "completed": "✓",
                     "failed": "✗", "cancelled": "—"}).get(d["status"], "?")
            pill_cls = ({"pending": "warn", "downloading": "accent",
                        "completed": "ok", "failed": "bad", "cancelled": "muted"}
                       ).get(d["status"], "muted")
            err = (f'<div class="err">{html.escape(d["error_message"])}</div>'
                   if d["error_message"] else "")
            items.append(
                f'<div class="dl-item">'
                f'<div class="icon">{icon}</div>'
                f'<div class="info">'
                f'<div class="name">{html.escape(d["name"])}</div>'
                f'<div class="meta">'
                f'<span class="pill {pill_cls}">{d["status"]}</span>'
                f'<span>· {_fmt_size(d["size_bytes"])}</span>'
                f'<span>· {_fmt_time(d["requested_at"])}</span>'
                f'</div>{err}'
                f'</div>'
                f'<div class="right">{_fmt_time(d["finished_at"]) or "—"}</div>'
                f'</div>'
            )
        recent_html = f'<div class="dl-list">{"".join(items)}</div>'
    else:
        recent_html = (
            '<div class="empty-state">'
            '<div class="icon">⬇</div>'
            '<div>No downloads yet — grab something from Sonarr or Radarr to see activity here.</div>'
            '</div>'
        )

    base_url = os.environ.get("TGARR_BASE_URL") or "http://&lt;host&gt;:8765"

    body = f"""
<div class="stats">{stats_html}</div>

<h2 class="section">Recent activity <span class="extra"><a href="/downloads">view all →</a></span></h2>
{recent_html}

<h2 class="section">Wiring info</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">🔌</div>
    <div class="info">
      <div class="name">Newznab indexer</div>
      <div class="meta">For Sonarr / Radarr / Lidarr → Settings → Indexers → ➕ Newznab</div>
    </div>
    <div class="right"><code>{base_url}/newznab/</code></div>
  </div>
  <div class="dl-item">
    <div class="icon">⬇</div>
    <div class="info">
      <div class="name">SABnzbd download client</div>
      <div class="meta">For Sonarr / Radarr → Settings → Download Clients → ➕ SABnzbd</div>
    </div>
    <div class="right"><code>/sabnzbd</code> · same host/port</div>
  </div>
</div>
"""
    return HTMLResponse(_layout("Dashboard", "/", body))


# ════════════════════════════════════════════════════════════════════
# Page: Channels
# ════════════════════════════════════════════════════════════════════
# Federation eligibility — channels matching both rules get pushed to
# registry.tgarr.me. Friend chats, small groups, and content-sparse channels
# stay private; only sizeable resource channels seed the central moat.
CONTRIB_MIN_MEMBERS = 500
CONTRIB_MIN_MEDIA = 100


# Heuristic NSFW keyword classifier. Multi-script — Telegram resource
# channels span many languages.
import re as _re_aud
NSFW_RX = _re_aud.compile(
    r"(porn|xxx|nsfw|adult|18\+|hentai|erotic|nude|naked|onlyfan|onlyfans|"
    r"sexy|sex\b|"
    r"\b色情|\b成人|\b18禁|\b裸\b|\b淫|"
    r"эротик|порно|секс|"
    r"اباحي|سكس)",
    _re_aud.IGNORECASE,
)

# CSAM hardcoded block — never displayable, never federated, regardless of
# any user opt-in. Conservative keyword set; reports any match to
# /var/log/tgarr-csam-flags.log for human + IWF/NCMEC review.
CSAM_RX = _re_aud.compile(
    r"\b(loli|lolicon|shota|shotacon|child\s*porn|kid\s*porn|"
    r"pre[\s_-]*teen|under[\s_-]*age|\bcp\d+|\bcp_)\b",
    _re_aud.IGNORECASE,
)


def classify_audience(title: str, username: str) -> str:
    """Returns 'blocked_csam' (hard ban), 'nsfw' (gated), or 'sfw'."""
    blob = (title or "") + " " + (username or "")
    if CSAM_RX.search(blob):
        # Hard block. NEVER show. NEVER federate. Logged for review.
        try:
            with open("/tmp/tgarr-csam-flags.log", "a") as f:
                f.write(f"{time.time()}\t{title!r}\t{username!r}\n")
        except Exception:
            pass
        return "blocked_csam"
    return "nsfw" if NSFW_RX.search(blob) else "sfw"


@app.post("/api/channel/subscribe")
async def api_channel_subscribe(username: str = Form(...)):
    """Subscribe to a public Telegram channel WITHOUT joining it.

    The crawler's subscription_poller resolves the username, validates it,
    and starts a get_chat_history backfill. The user's Telegram account stays
    small (no mass-join ban risk), and we can index thousands of public
    channels limited only by Telegram rate limits.
    """
    u = username.strip().lstrip("@")
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]{4,31}$", u):
        return JSONResponse({"status": "error",
                            "message": "invalid telegram username format"}, 400)
    async with db_pool.acquire() as conn:
        # Insert with a placeholder negative chat_id; poller will replace it
        # with the real one after get_chat resolves the username.
        existing = await conn.fetchval(
            "SELECT id FROM channels WHERE username ILIKE $1", u)
        if existing:
            await conn.execute(
                """UPDATE channels SET subscribed=TRUE,
                   last_polled_at=NULL, subscribe_error=NULL
                   WHERE id=$1""", existing)
            return {"status": "ok", "message": f"already known — re-queued @{u}",
                    "channel_id": existing}
        # Synthesize a placeholder tg_chat_id below the legal range to avoid
        # collision; poller fixes it on first scan.
        placeholder = -abs(hash(u)) // 1000 - 1_000_000_000_000
        new_id = await conn.fetchval(
            """INSERT INTO channels (tg_chat_id, username, title,
                                     subscribed, backfilled)
               VALUES ($1, $2, $3, TRUE, FALSE)
               ON CONFLICT (tg_chat_id) DO NOTHING
               RETURNING id""",
            placeholder, u, f"@{u} (resolving…)")
    return {"status": "queued", "message": f"@{u} queued for first scan",
            "channel_id": new_id}


@app.post("/api/channel/unsubscribe/{tg_chat_id}")
async def api_channel_unsubscribe(tg_chat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE channels SET subscribed=FALSE WHERE tg_chat_id=$1
               AND subscribed=TRUE""", tg_chat_id)
    return {"status": "ok"}


@app.post("/api/channel/{tg_chat_id}/audience")
async def api_set_audience(tg_chat_id: int, value: str = Form(...)):
    if value not in ("sfw", "nsfw", "unknown"):
        return JSONResponse({"status": "error", "message": "value must be sfw/nsfw/unknown"},
                          status_code=400)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE channels SET audience=$1, audience_manual=TRUE WHERE tg_chat_id=$2",
            None if value == "unknown" else value, tg_chat_id)
    return {"status": "ok", "audience": value}


@app.get("/channels", response_class=HTMLResponse)
async def page_channels(min_members: int = 500,
                        max_members: Optional[int] = None,
                        eligible: int = 0,
                        audience: str = ""):
    if not login.session_exists():
        return RedirectResponse("/login")
    # Range filter: COALESCE NULL members to a big number so unresolved
    # channels stay in the default "500+" view rather than disappearing.
    where_extra = f"AND COALESCE(c.members_count, 999999) >= {max(0, min_members)}"
    if max_members:
        where_extra += f" AND COALESCE(c.members_count, 0) < {max_members}"
    if eligible:
        where_extra += (
            f" AND c.members_count >= {CONTRIB_MIN_MEMBERS} "
            f"AND (SELECT count(*) FROM messages m WHERE m.channel_id = c.id "
            f"AND m.file_name IS NOT NULL) >= {CONTRIB_MIN_MEDIA}"
        )
    nsfw_on = await _is_nsfw_enabled()
    if audience == "sfw":
        where_extra += " AND COALESCE(c.audience, 'sfw') = 'sfw'"
    elif audience == "nsfw" and nsfw_on:
        where_extra += " AND c.audience = 'nsfw'"
    elif audience == "unknown":
        where_extra += " AND c.audience IS NULL"
    if not nsfw_on:
        where_extra += " AND COALESCE(c.audience, 'sfw') <> 'nsfw'"
    # CSAM hard-block — no setting can unblock this.
    where_extra += " AND COALESCE(c.audience, 'sfw') <> 'blocked_csam'"
    async with db_pool.acquire() as conn:
        # Lazy classify (Python-side) — pulls untagged rows and tags them
        # before listing. Manual overrides untouched.
        untagged = await conn.fetch(
            """SELECT tg_chat_id, title, username FROM channels
               WHERE audience IS NULL AND audience_manual = FALSE
                 AND (title IS NOT NULL OR username IS NOT NULL)""")
        for u in untagged:
            await conn.execute(
                "UPDATE channels SET audience=$1 WHERE tg_chat_id=$2",
                classify_audience(u["title"], u["username"]), u["tg_chat_id"])

        rows = await conn.fetch(
            f"""SELECT c.tg_chat_id, c.username, c.title, c.backfilled,
                      c.members_count, c.audience, c.audience_manual,
                      (SELECT count(*) FROM messages m WHERE m.channel_id = c.id)
                                                                AS msg_count,
                      (SELECT count(*) FROM messages m
                         WHERE m.channel_id = c.id AND m.file_name IS NOT NULL)
                                                                AS media_count,
                      (c.members_count >= {CONTRIB_MIN_MEMBERS} AND
                       (SELECT count(*) FROM messages m WHERE m.channel_id = c.id
                          AND m.file_name IS NOT NULL) >= {CONTRIB_MIN_MEDIA}
                      ) AS eligible_moat
               FROM channels c
               WHERE 1=1 {where_extra}
               ORDER BY COALESCE(c.members_count, 0) DESC, msg_count DESC""")
        total = await conn.fetchval("SELECT count(*) FROM channels")
        with_meta = await conn.fetchval(
            "SELECT count(*) FROM channels WHERE members_count IS NOT NULL")
        eligible_total = await conn.fetchval(
            f"""SELECT count(*) FROM channels c
               WHERE c.members_count >= {CONTRIB_MIN_MEMBERS}
                 AND (SELECT count(*) FROM messages m WHERE m.channel_id = c.id
                      AND m.file_name IS NOT NULL) >= {CONTRIB_MIN_MEDIA}""")

    def _chip(lo: int, hi: Optional[int], label: str) -> str:
        active = (min_members == lo and max_members == hi and not eligible)
        style = ('background:rgba(94,182,229,0.15);color:var(--accent-hi);'
                'border-color:var(--accent);') if active else ''
        href = f"/channels?min_members={lo}"
        if hi is not None:
            href += f"&max_members={hi}"
        return (f'<a class="btn ghost" href="{href}" '
               f'style="{style}padding:6px 12px;font-size:13px">{label}</a>')

    def _eligible_chip() -> str:
        active = bool(eligible)
        style = ('background:rgba(74,222,128,0.18);color:var(--ok);'
                'border-color:var(--ok);') if active else ''
        return (f'<a class="btn ghost" href="/channels?eligible=1" '
               f'style="{style}padding:6px 12px;font-size:13px">'
               f'✓ moat ({eligible_total})</a>')

    def _aud_chip(val: str, label: str, color: str = "var(--accent)") -> str:
        active = (audience == val)
        style = (f'background:{color}22;color:{color};border-color:{color};') if active else ''
        return (f'<a class="btn ghost" href="/channels?audience={val}" '
               f'style="{style}padding:6px 12px;font-size:13px">{label}</a>')

    filter_bar = (
        '<div style="display:flex;gap:6px;margin-bottom:20px;align-items:center;flex-wrap:wrap">'
        f'{_chip(0, None, f"all ({total})")}'
        f'{_chip(0, 20, "20")}'
        f'{_chip(20, 50, "50")}'
        f'{_chip(50, 100, "100")}'
        f'{_chip(100, 500, "500")}'
        f'{_chip(500, 1000, "1K")}'
        f'{_chip(1000, 5000, "5K")}'
        f'{_chip(500, None, "500+ ⭐")}'
        f'{_eligible_chip()}'
        '<span style="border-left:1px solid var(--border);margin:0 4px;height:24px"></span>'
        f'{_aud_chip("sfw", "✓ SFW", "#15803d")}'
        f'{_aud_chip("nsfw", "🔞 NSFW", "#b91c1c")}'
        + (f'<span style="margin-left:auto;color:var(--muted);font-size:12px">'
           f'members resolved {with_meta}/{total}</span>' if with_meta < total else '')
        + '</div>'
        f'<div style="font-size:12px;color:var(--muted);margin-bottom:18px">'
        f'<strong>✓ moat</strong> = ≥{CONTRIB_MIN_MEMBERS} members AND ≥{CONTRIB_MIN_MEDIA} media files. '
        f'Only these get pushed to <code>registry.tgarr.me</code> when federation is enabled. '
        f'Private chats and small groups stay local.'
        f'</div>'
    )

    if not rows:
        body = (f'<h2 class="section">Indexed channels <span class="count">0 of {total}</span></h2>'
               + filter_bar +
               '<div class="empty-state">'
               '<div class="icon">📡</div>'
               f'<div>No channels with ≥{min_msgs} messages.</div>'
               '<div style="margin-top:8px;font-size:13px">Try a lower threshold above, or join more active channels in Telegram.</div>'
               '</div>')
    else:
        cards_html = []
        for r in rows:
            title = r["title"] or "(untitled)"
            initial = (title.strip() or "?")[0].upper()
            color = _color_for(title)
            handle = ("@" + r["username"]) if r["username"] else f"id {r['tg_chat_id']}"
            status_pill = ('<span class="pill ok">backfilled</span>' if r["backfilled"]
                          else '<span class="pill warn">backfilling…</span>')
            members_pill = ''
            if r["members_count"] is not None:
                m = r["members_count"]
                m_str = (f"{m/1000:.1f}K" if m >= 1000 else str(m)).replace(".0K", "K")
                members_pill = f'<span class="pill accent">👥 {m_str}</span>'
            moat_pill = ('<span class="pill ok" title="eligible for federation">✓ moat</span>'
                        if r["eligible_moat"] else '')
            cards_html.append(
                f'<div class="card">'
                f'<div class="avatar" style="background:{color}">{html.escape(initial)}</div>'
                f'<div class="body">'
                f'<div class="title">{html.escape(title)}</div>'
                f'<div class="sub">{html.escape(handle)}</div>'
                f'</div>'
                f'<div class="footer">'
                f'{members_pill}'
                f'<span class="pill muted">{r["msg_count"]:,} msgs</span>'
                f'<span class="pill muted">{r["media_count"]:,} media</span>'
                f'{moat_pill}'
                f'{status_pill}'
                f'</div>'
                f'</div>'
            )
        body = (f'<h2 class="section">Indexed channels <span class="count">{len(rows)} of {total}</span></h2>'
               + filter_bar
               + f'<div class="cards">{"".join(cards_html)}</div>')
    return HTMLResponse(_layout("Channels", "/channels", body))


# ════════════════════════════════════════════════════════════════════
# Page: Discover (registry-pulled channels awaiting subscription)
# ════════════════════════════════════════════════════════════════════
@app.get("/discover", response_class=HTMLResponse)
async def page_discover(audience: str = "sfw"):
    async with db_pool.acquire() as conn:
        # Auth check
        n_channels = await conn.fetchval("SELECT count(*) FROM channels")
        if not login.session_exists() and not n_channels:
            return RedirectResponse("/login")
        # Auto-subscribed check
        known = {r["username"]: r["subscribed"] for r in await conn.fetch(
            "SELECT username, subscribed FROM channels WHERE username IS NOT NULL")}
        where = ["dismissed = FALSE"]
        if audience in ("sfw", "nsfw"):
            where.append(f"audience = '{audience}'")
        rows = await conn.fetch(f"""
            SELECT username, title, members_count, media_count, audience,
                   language, category, distinct_contributors, verified,
                   last_pulled_at
            FROM discovered
            WHERE {' AND '.join(where)}
            ORDER BY verified DESC, distinct_contributors DESC,
                     COALESCE(members_count, 0) DESC
            LIMIT 200
        """)
        total = await conn.fetchval("SELECT count(*) FROM discovered")
        last_pull = await conn.fetchval(
            "SELECT max(last_pulled_at) FROM discovered")

    if not rows:
        body = (
            f'<h2 class="section">Discover <span class="count">'
            f'0 of {total or 0}</span></h2>'
            '<div class="empty-state">'
            '<div class="icon">🔭</div>'
            '<div>No channels pulled from registry yet.</div>'
            '<div style="margin-top:8px;font-size:13px">'
            'The registry_puller fires every 12h on a deterministic offset '
            '(or hit <code>POST /api/registry/pull-now</code> to force it).'
            '</div></div>'
        )
        return HTMLResponse(_layout("Discover", "/discover", body))

    def _card(r):
        u = r["username"]
        state = known.get(u)
        if state is True:
            btn = (f'<button class="ghost" disabled '
                  f'style="font-size:12px;padding:6px 12px">✓ subscribed</button>')
        else:
            btn = (f'<button data-uname="{html.escape(u)}" '
                  f'onclick="tgDiscoverSubscribe(this)" '
                  f'style="font-size:12px;padding:6px 12px">+ Subscribe</button>')
        members = (f"{r['members_count']/1000:.1f}K".replace(".0K", "K")
                  if r['members_count'] and r['members_count'] >= 1000
                  else str(r['members_count'] or '-'))
        verified_pill = ('<span class="pill ok">✓ verified</span>'
                       if r["verified"] else '')
        return (
            f'<div class="dl-item">'
            f'<div class="icon">📡</div>'
            f'<div class="info">'
            f'<div class="name">@{html.escape(u)}'
            f'{" — " + html.escape(r["title"]) if r["title"] else ""}</div>'
            f'<div class="meta">'
            f'{verified_pill}'
            f'<span class="pill accent">👥 {members}</span>'
            f'<span class="pill muted">{r["media_count"] or 0} media</span>'
            f'<span class="pill muted">{r["audience"]}</span>'
            f'<span>· seen by {r["distinct_contributors"] or 1} instances</span>'
            f'</div></div>'
            f'<div class="right">{btn}</div>'
            f'</div>'
        )

    body = (
        f'<h2 class="section">Discover <span class="count">'
        f'{len(rows)} of {total} from registry.tgarr.me</span>'
        + (f'<span class="extra" style="color:var(--muted);font-size:12px">'
           f'last pull {_fmt_time(last_pull)}</span>'
           if last_pull else '')
        + '</h2>'
        '<div style="margin-bottom:18px;display:flex;gap:8px">'
        '  <form method="POST" action="/api/registry/pull-now">'
        '    <button type="submit" class="ghost" style="font-size:13px">↻ Pull now</button>'
        '  </form>'
        '</div>'
        f'<div class="dl-list">{"".join(_card(r) for r in rows)}</div>'
        '<script>'
        '(() => {'
        '  window.tgDiscoverSubscribe = async function(btn) {'
        '    const u = btn.dataset.uname;'
        '    btn.disabled = true; btn.textContent = "...";'
        '    try {'
        '      const r = await fetch("/api/channel/subscribe", {method:"POST", '
        '          headers:{"Content-Type":"application/x-www-form-urlencoded"}, '
        '          body: "username=" + encodeURIComponent(u)});'
        '      const j = await r.json();'
        '      if (j.status === "queued" || j.status === "ok") {'
        '        btn.textContent = "✓ queued";'
        '        btn.style.background = "var(--ok)"; btn.style.color = "#fff";'
        '      } else { btn.textContent = "✗ " + (j.message || "error"); }'
        '    } catch(e) { btn.textContent = "✗ network"; }'
        '  };'
        '})();'
        '</script>'
    )
    return HTMLResponse(_layout("Discover", "/discover", body))


@app.post("/api/registry/pull-now")
async def api_registry_pull_now():
    """Force the next registry_puller tick to fire immediately. Writes a
    sentinel to the config table; the running puller polls for it every 60s.
    303 See Other so the form-POST → GET-redirect follows correctly.
    """
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO config (key, value) VALUES ('registry_pull_force', $1)
               ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=NOW()""",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return RedirectResponse("/discover", status_code=303)


# ════════════════════════════════════════════════════════════════════
# Page: Gallery (Telegram-shared photos)
# ════════════════════════════════════════════════════════════════════
@app.get("/gallery", response_class=HTMLResponse)
async def page_gallery(channel: Optional[str] = None, limit: int = 240):
    if not login.session_exists():
        # Skip redirect if we have data — crawler may be authed in-memory
        async with db_pool.acquire() as conn:
            n = await conn.fetchval("SELECT count(*) FROM channels")
        if not n:
            return RedirectResponse("/login")

    where = ["m.thumb_path IS NOT NULL",
             "m.thumb_path <> '__failed__'",
             "m.thumb_path <> '__deleted__'"]
    params = []
    if channel:
        params.append(channel)
        where.append(f"c.username = ${len(params)}")
    # Gate NSFW unless user has explicitly opted in via /settings.
    if not await _is_nsfw_enabled():
        where.append("COALESCE(c.audience, 'sfw') <> 'nsfw'")
    # CSAM hard-block — overrides every setting, always.
    where.append("COALESCE(c.audience, 'sfw') <> 'blocked_csam'")

    async with db_pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT count(*) FROM messages WHERE thumb_path IS NOT NULL "
            "AND thumb_path NOT LIKE '__%'")
        unique_total = await conn.fetchval(
            "SELECT count(DISTINCT thumb_md5) FROM messages "
            "WHERE thumb_path IS NOT NULL AND thumb_path NOT LIKE '__%' "
            "AND thumb_md5 IS NOT NULL")
        pending = await conn.fetchval(
            """SELECT count(*) FROM messages
               WHERE thumb_path IS NULL AND (media_type='photo' OR
                     (media_type IS NULL AND file_name IS NULL
                      AND COALESCE(mime_type,'')='' AND file_unique_id IS NOT NULL))""")
        # DISTINCT ON thumb_md5 → one row per unique image. Rows without md5
        # (legacy thumbs not yet hashed) fall through with NULL bucket; that
        # group will be deduped to one row, which is OK for now and the hash
        # backfill task fills them in.
        rows = await conn.fetch(f"""
            SELECT * FROM (
              SELECT DISTINCT ON (COALESCE(m.thumb_md5, 'id-' || m.id::text))
                     m.id, m.thumb_path, m.thumb_md5, m.caption, m.posted_at,
                     c.title AS ch_title, c.username AS ch_user,
                     (SELECT count(*) FROM messages m2
                       WHERE m2.thumb_md5 = m.thumb_md5 AND m2.thumb_md5 IS NOT NULL) AS dup_count
              FROM messages m
              JOIN channels c ON c.id = m.channel_id
              WHERE {' AND '.join(where)}
              ORDER BY COALESCE(m.thumb_md5, 'id-' || m.id::text), m.posted_at DESC NULLS LAST
            ) sub
            ORDER BY posted_at DESC NULLS LAST
            LIMIT {max(1, min(limit, 500))}
        """, *params)

    if not rows:
        body = (
            '<h2 class="section">Gallery <span class="count">0</span></h2>'
            '<div class="empty-state">'
            '<div class="icon">🖼</div>'
            '<div>No photos cached yet.</div>'
            f'<div style="margin-top:8px;font-size:13px">{pending:,} photo messages indexed — '
            'crawler is downloading thumbnails in the background. Refresh in a minute.</div>'
            '</div>'
        )
        return HTMLResponse(_layout("Gallery", "/gallery", body))

    items = "".join(
        f'<figure class="item" data-id="{r["id"]}" '
        f'data-src="/thumbs/{html.escape(r["thumb_path"])}" '
        f'data-cap="{html.escape((r["caption"] or "")[:300])}" '
        f'data-ch="{html.escape(r["ch_title"] or r["ch_user"] or "")}">'
        f'<img src="/thumbs/{html.escape(r["thumb_path"])}" loading="lazy" />'
        + (f'<div class="dup-badge" title="shared across {r["dup_count"]} channels">×{r["dup_count"]}</div>'
           if r.get("dup_count") and r["dup_count"] > 1 else '')
        + f'<figcaption class="meta">'
        f'<div class="ch">{html.escape(r["ch_title"] or "")}</div>'
        f'<div class="cap">{html.escape((r["caption"] or "")[:200])}</div>'
        f'</figcaption>'
        f'</figure>'
        for r in rows
    )

    dup_saved = max(0, total - unique_total) if unique_total else 0
    body = (
        f'<h2 class="section">Gallery <span class="count">{len(rows)} unique · {unique_total:,} total dedup</span>'
        + (f' · <span style="color:var(--muted);font-size:13px">{dup_saved:,} dup'
           f'{f" · {pending:,} pending" if pending else ""}</span>'
           if dup_saved or pending else '')
        + ' <span class="extra" style="color:var(--muted);font-size:13px">'
        '← → arrows · Space pause · Del delete · Esc close</span></h2>'
        f'<div class="gallery">{items}</div>'
        '<div class="lightbox">'
        '  <div class="lb-progress"><div class="lb-progress-bar" id="lbProgress"></div></div>'
        '  <button class="lb-close" id="lbClose">×</button>'
        '  <button class="lb-prev" id="lbPrev">‹</button>'
        '  <button class="lb-next" id="lbNext">›</button>'
        '  <img id="lbImg" alt="" />'
        '  <div class="lb-controls">'
        '    <button id="lbPlay" title="play / pause (Space)">▶</button>'
        '    <span class="lb-count" id="lbCount">– / –</span>'
        '    <button id="lbDel" title="delete (Del) — will not be redownloaded">🗑</button>'
        '  </div>'
        '  <div class="lb-meta"><div class="ch" id="lbCh"></div><div id="lbCap"></div></div>'
        '</div>'
        '<script>'
        '(() => {'
        '  const items = [...document.querySelectorAll(".gallery .item")];'
        '  const photos = items.map(el => ({el, id:el.dataset.id, src:el.dataset.src, cap:el.dataset.cap, ch:el.dataset.ch}));'
        '  if (!photos.length) return;'
        '  const lb = document.querySelector(".lightbox");'
        '  const img = document.getElementById("lbImg");'
        '  const chEl = document.getElementById("lbCh");'
        '  const capEl = document.getElementById("lbCap");'
        '  const counter = document.getElementById("lbCount");'
        '  const playBtn = document.getElementById("lbPlay");'
        '  const prog = document.getElementById("lbProgress");'
        '  const FX = ["fxFade","fxZoomIn","fxZoomOut","fxSlideRight","fxSlideLeft","fxFlip","fxRotateScale","fxBlur"];'
        '  const DELAY = 4500;'
        '  let cur = -1, playing = false, timer = null, startTimer = null, lastFx = -1;'
        '  function show(idx) {'
        '    if (!photos.length) return;'
        '    idx = ((idx % photos.length) + photos.length) % photos.length;'
        '    cur = idx;'
        '    const p = photos[idx];'
        '    FX.forEach(c => img.classList.remove(c));'
        '    void img.offsetHeight;'
        '    img.src = p.src;'
        '    let fx; do { fx = Math.floor(Math.random() * FX.length); } while (fx === lastFx && FX.length > 1);'
        '    lastFx = fx;'
        '    img.classList.add(FX[fx]);'
        '    chEl.textContent = p.ch || "";'
        '    capEl.textContent = p.cap || "";'
        '    counter.textContent = (idx+1) + " / " + photos.length;'
        '    updateProg();'
        '  }'
        '  function updateProg() {'
        '    prog.style.transition = "none"; prog.style.width = "0";'
        '    if (playing) { void prog.offsetHeight; prog.style.transition = "width " + DELAY + "ms linear"; prog.style.width = "100%"; }'
        '  }'
        '  function play() { playing = true; playBtn.textContent = "⏸"; clearInterval(timer); timer = setInterval(() => show(cur+1), DELAY); updateProg(); }'
        '  function pause() { playing = false; playBtn.textContent = "▶"; clearInterval(timer); prog.style.transition = "none"; prog.style.width = "0"; }'
        '  async function del() {'
        '    const p = photos[cur];'
        '    if (!confirm("Delete this photo? Will not be redownloaded.")) return;'
        '    try {'
        '      const r = await fetch("/api/photo/" + p.id + "/delete", {method:"POST"});'
        '      if (!r.ok) throw new Error("HTTP " + r.status);'
        '      p.el.remove(); photos.splice(cur, 1);'
        '      if (!photos.length) { close(); return; }'
        '      show(cur);'
        '    } catch (e) { alert("Delete failed: " + e.message); }'
        '  }'
        '  function open(id) {'
        '    const idx = photos.findIndex(p => p.id === id);'
        '    if (idx < 0) return;'
        '    lb.classList.add("open");'
        '    show(idx);'
        '    clearTimeout(startTimer);'
        '    startTimer = setTimeout(play, 5000);'
        '  }'
        '  function close() { pause(); clearTimeout(startTimer); lb.classList.remove("open"); }'
        '  items.forEach(it => it.addEventListener("click", () => open(it.dataset.id)));'
        '  document.getElementById("lbPrev").addEventListener("click", e => { e.stopPropagation(); pause(); show(cur-1); });'
        '  document.getElementById("lbNext").addEventListener("click", e => { e.stopPropagation(); pause(); show(cur+1); });'
        '  playBtn.addEventListener("click", e => { e.stopPropagation(); playing ? pause() : play(); });'
        '  document.getElementById("lbDel").addEventListener("click", e => { e.stopPropagation(); del(); });'
        '  document.getElementById("lbClose").addEventListener("click", e => { e.stopPropagation(); close(); });'
        '  lb.addEventListener("click", e => { if (e.target === lb) close(); });'
        '  document.addEventListener("keydown", e => {'
        '    if (!lb.classList.contains("open")) return;'
        '    if (e.key === "Escape") close();'
        '    else if (e.key === "ArrowRight") { pause(); show(cur+1); }'
        '    else if (e.key === "ArrowLeft") { pause(); show(cur-1); }'
        '    else if (e.key === " ") { e.preventDefault(); playing ? pause() : play(); }'
        '    else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); del(); }'
        '  });'
        '})();'
        '</script>'
    )
    return HTMLResponse(_layout("Gallery", "/gallery", body))


# ════════════════════════════════════════════════════════════════════
# Page: Music
# ════════════════════════════════════════════════════════════════════
def _fmt_dur(secs):
    if not secs:
        return ""
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


@app.get("/music", response_class=HTMLResponse)
async def page_music(limit: int = 200):
    async with db_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM channels")
    if not login.session_exists() and not n:
        return RedirectResponse("/login")

    async with db_pool.acquire() as conn:
        cached_n = await conn.fetchval(
            """SELECT count(*) FROM messages
               WHERE media_type='audio' AND local_path IS NOT NULL
                 AND local_path NOT LIKE '\\_\\_%' ESCAPE '\\'""")
        pending_n = await conn.fetchval(
            "SELECT count(*) FROM messages WHERE media_type='audio' AND local_path IS NULL")
        rows = await conn.fetch(f"""
            SELECT m.id, m.audio_title, m.audio_performer, m.audio_duration_sec,
                   m.file_name, m.file_size, m.posted_at,
                   c.title AS ch_title, c.username AS ch_user
            FROM messages m
            JOIN channels c ON c.id = m.channel_id
            WHERE m.media_type = 'audio'
              AND m.local_path IS NOT NULL
              AND m.local_path NOT LIKE '\\_\\_%' ESCAPE '\\'
            ORDER BY m.posted_at DESC NULLS LAST
            LIMIT {max(1, min(limit, 500))}
        """)

    if not rows:
        body = (
            f'<h2 class="section">Music <span class="count">0 cached</span></h2>'
            '<div class="empty-state">'
            '<div class="icon">🎵</div>'
            '<div>No audio cached yet.</div>'
            f'<div style="margin-top:8px;font-size:13px">{pending_n:,} audio messages indexed — crawler is downloading in the background (capped to recent 300 tracks).</div>'
            '</div>'
        )
        return HTMLResponse(_layout("Music", "/music", body))

    rows_html = "".join(
        f'<tr data-id="{r["id"]}" onclick="tgMusicPlay(this)">'
        f'<td class="play-cell"><div class="play-ico">▶</div></td>'
        f'<td class="title-cell">'
        f'<div class="t">{html.escape(r["audio_title"] or r["file_name"] or "(untitled)")}</div>'
        f'<div class="a">{html.escape((r["audio_performer"] or "") + (" · " + r["ch_title"] if r["ch_title"] else ""))}</div>'
        f'</td>'
        f'<td class="num">{_fmt_dur(r["audio_duration_sec"])}</td>'
        f'<td class="num">{_fmt_size(r["file_size"])}</td>'
        f'<td class="num">{_fmt_time(r["posted_at"])[:10]}</td>'
        f'</tr>'
        for r in rows
    )

    body = (
        f'<h2 class="section">Music <span class="count">{len(rows)} cached</span>'
        + (f' · <span style="color:var(--muted);font-size:13px">{pending_n:,} pending</span>'
           if pending_n else '')
        + '</h2>'
        '<table class="music-list">'
        '<thead><tr><th style="width:40px"></th><th>title</th><th>duration</th><th>size</th><th>posted</th></tr></thead>'
        f'<tbody>{rows_html}</tbody></table>'
        '<div class="player-bar">'
        '  <div class="np">'
        '    <div class="np-icon">🎵</div>'
        '    <div class="np-text"><div class="np-t" id="npT">— select a track —</div><div class="np-a" id="npA"></div></div>'
        '  </div>'
        '  <audio id="audio" controls preload="metadata" style="flex:1;min-width:0"></audio>'
        '  <button class="ghost" onclick="tgMusicNext()" title="next (n)">⏭</button>'
        '  <button class="ghost" id="shuffleBtn" onclick="tgMusicShuffle(this)" title="shuffle">🔀</button>'
        '</div>'
        '<script>'
        '(() => {'
        '  const rows = [...document.querySelectorAll(".music-list tbody tr")];'
        '  const ids = rows.map(r => r.dataset.id);'
        '  const audio = document.getElementById("audio");'
        '  let cur = -1, shuffled = false;'
        '  window.tgMusicPlay = function(row) {'
        '    cur = rows.indexOf(row);'
        '    rows.forEach(r => r.classList.remove("playing"));'
        '    row.classList.add("playing");'
        '    audio.src = "/media/" + row.dataset.id;'
        '    document.getElementById("npT").textContent = row.querySelector(".t").textContent;'
        '    document.getElementById("npA").textContent = row.querySelector(".a").textContent;'
        '    audio.play().catch(() => {});'
        '  };'
        '  window.tgMusicNext = function() {'
        '    if (!rows.length) return;'
        '    let nxt = shuffled ? Math.floor(Math.random()*rows.length) : (cur+1) % rows.length;'
        '    tgMusicPlay(rows[nxt]);'
        '  };'
        '  window.tgMusicShuffle = function(btn) {'
        '    shuffled = !shuffled;'
        '    btn.style.background = shuffled ? "var(--accent)" : "";'
        '    btn.style.color = shuffled ? "#fff" : "";'
        '  };'
        '  audio.addEventListener("ended", tgMusicNext);'
        '  document.addEventListener("keydown", e => {'
        '    if (e.target.tagName === "INPUT") return;'
        '    if (e.key === "n") tgMusicNext();'
        '    else if (e.key === " " && audio.src) { e.preventDefault(); audio.paused ? audio.play() : audio.pause(); }'
        '  });'
        '})();'
        '</script>'
    )
    return HTMLResponse(_layout("Music", "/music", body))


# ════════════════════════════════════════════════════════════════════
# Page: Library (ebooks)
# ════════════════════════════════════════════════════════════════════
@app.get("/library", response_class=HTMLResponse)
async def page_library(limit: int = 200):
    async with db_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM channels")
    if not login.session_exists() and not n:
        return RedirectResponse("/login")

    async with db_pool.acquire() as conn:
        cached_n = await conn.fetchval(
            r"""SELECT count(*) FROM messages
               WHERE media_type='document' AND local_path IS NOT NULL
                 AND local_path NOT LIKE '\_\_%' ESCAPE '\'""")
        pending_n = await conn.fetchval(
            r"""SELECT count(*) FROM messages
               WHERE media_type='document' AND local_path IS NULL
                 AND file_name ~* '\.(pdf|epub|mobi|azw3?|djvu|fb2|cbr|cbz|lit|txt)$'""")
        rows = await conn.fetch(f"""
            SELECT m.id, m.file_name, m.file_size, m.mime_type, m.caption, m.posted_at,
                   c.title AS ch_title, c.username AS ch_user
            FROM messages m
            JOIN channels c ON c.id = m.channel_id
            WHERE m.media_type = 'document'
              AND m.local_path IS NOT NULL
              AND m.local_path NOT LIKE '\\_\\_%' ESCAPE '\\'
              AND m.file_name ~* '\\.(pdf|epub|mobi|azw3?|djvu|fb2|cbr|cbz|lit|txt)$'
            ORDER BY m.posted_at DESC NULLS LAST
            LIMIT {max(1, min(limit, 500))}
        """)

    def _ext(fn):
        return (fn or "").rsplit(".", 1)[-1].upper() if "." in (fn or "") else "DOC"
    def _ico(ext):
        return {"PDF": "📄", "EPUB": "📘", "MOBI": "📕", "AZW3": "📕", "AZW": "📕",
                "DJVU": "📃", "CBR": "📚", "CBZ": "📚", "FB2": "📖",
                "TXT": "📝"}.get(ext, "📄")

    if not rows:
        body = (
            '<h2 class="section">Library <span class="count">0 cached</span></h2>'
            '<div class="empty-state">'
            '<div class="icon">📚</div>'
            '<div>No ebooks cached yet.</div>'
            f'<div style="margin-top:8px;font-size:13px">{pending_n:,} ebook messages indexed — crawler is downloading in the background.</div>'
            '</div>'
        )
        return HTMLResponse(_layout("Library", "/library", body))

    items = "".join(
        f'<div class="book-card">'
        f'<div class="book-ico">{_ico(_ext(r["file_name"]))}</div>'
        f'<div class="book-body">'
        f'<div class="book-title">{html.escape(r["file_name"] or "(untitled)")}</div>'
        f'<div class="book-meta">'
        f'<span class="pill accent">{_ext(r["file_name"])}</span>'
        f'<span class="pill muted">{_fmt_size(r["file_size"])}</span>'
        f'<span>· {html.escape(r["ch_title"] or "")}</span>'
        f'</div>'
        f'</div>'
        f'<div class="book-actions">'
        f'<a class="btn" href="/media/{r["id"]}" target="_blank">Open</a>'
        f'<a class="btn ghost" href="/media/{r["id"]}" download="{html.escape(r["file_name"] or "")}">Download</a>'
        f'</div>'
        f'</div>'
        for r in rows
    )

    body = (
        f'<h2 class="section">Library <span class="count">{len(rows)} cached</span>'
        + (f' · <span style="color:var(--muted);font-size:13px">{pending_n:,} pending</span>'
           if pending_n else '')
        + '</h2>'
        f'<div class="book-grid">{items}</div>'
    )
    return HTMLResponse(_layout("Library", "/library", body))


# ════════════════════════════════════════════════════════════════════
# Page: Releases
# ════════════════════════════════════════════════════════════════════
def _release_card(r) -> str:
    """Sonarr/Radarr-style poster card."""
    title_disp = r["canonical_title"] or r["name"].replace(".", " ")
    poster = r["poster_url"]
    se = ""
    if r["season"] and r["episode"]:
        se = f"S{r['season']:02}E{r['episode']:02}"
    elif r["season"]:
        se = f"Season {r['season']}"
    posted = _fmt_time(r["posted_at"])[:10] if r["posted_at"] else ""
    sub_bits = [b for b in (se, posted) if b]
    cat_pill = ("accent" if r["category"] == "movie"
                else "ok" if r["category"] == "tv" else "muted")
    poster_style = (f'style="background-image:url(\'{html.escape(poster)}\')"'
                    if poster else '')
    fallback = '<div class="fallback">🎬</div>' if not poster else ""
    quality_badge = (f'<div class="badge">{html.escape(r["quality"])}</div>'
                     if r["quality"] else "")
    return (
        f'<div class="poster-card">'
        f'<div class="poster" {poster_style}>{fallback}{quality_badge}</div>'
        f'<div class="info">'
        f'<div class="title">{html.escape(title_disp)}</div>'
        f'<div class="sub">{" · ".join(html.escape(x) for x in sub_bits)}</div>'
        f'</div>'
        f'<div class="pills">'
        f'<span class="pill {cat_pill}">{r["category"]}</span>'
        f'<span class="pill muted">{_fmt_size(r["size_bytes"])}</span>'
        f'</div>'
        f'<div class="grab-row">'
        f'<button class="btn grab-btn" data-guid="{r["guid"]}" onclick="tgGrab(this)">⬇ Grab</button>'
        f'</div>'
        f'</div>'
    )


@app.get("/releases", response_class=HTMLResponse)
async def page_releases(q: Optional[str] = None, cat: Optional[str] = None,
                        view: str = "grid", limit: int = 120,
                        min_mb: int = 100):
    if not login.session_exists():
        return RedirectResponse("/login")
    where = ["1=1"]
    params = []
    if q:
        for w in q.split():
            params.append(f"%{w}%")
            where.append(f"name ILIKE ${len(params)}")
    if cat in ("movie", "tv"):
        where.append(f"category = '{cat}'")
    # Hide trailers / samples / IMG-junk by default — Tom asked.
    # Pass min_mb=0 to see everything (debugging).
    if min_mb and min_mb > 0:
        where.append(f"COALESCE(size_bytes,0) >= {min_mb * 1024 * 1024}")
    sql = (f"SELECT id, guid, name, category, season, episode, quality, "
          f"size_bytes, posted_at, parse_score, poster_url, canonical_title "
          f"FROM releases WHERE {' AND '.join(where)} "
          f"ORDER BY posted_at DESC NULLS LAST LIMIT {max(1, min(limit, 500))}")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    def _qs(**overrides) -> str:
        bits = []
        for k, v in {"q": q, "cat": cat, "view": view, **overrides}.items():
            if v:
                bits.append(f"{k}={html.escape(str(v))}")
        return "?" + "&".join(bits) if bits else ""

    def _chip(val: str, label: str) -> str:
        active = (cat or "") == val
        href = f"/releases{_qs(cat=val if val else None)}"
        style = ('background:rgba(94,182,229,0.15);color:var(--accent-hi);'
                'border-color:var(--accent);') if active else ''
        return (f'<a class="btn ghost" href="{href}" '
               f'style="{style}padding:5px 12px;font-size:12px">{label}</a>')

    grid_active = "active" if view != "list" else ""
    list_active = "active" if view == "list" else ""
    toolbar = (
        f'<div style="display:flex;gap:10px;margin-bottom:22px;align-items:center;flex-wrap:wrap">'
        f'{_chip("","all")}{_chip("movie","movies")}{_chip("tv","tv")}'
        f'<div style="margin-left:auto" class="view-toggle">'
        f'<a class="{grid_active}" href="/releases{_qs(view=None)}">▦ Grid</a>'
        f'<a class="{list_active}" href="/releases{_qs(view="list")}">☰ List</a>'
        f'</div></div>'
    )

    if not rows:
        body_html = ('<div class="empty-state"><div class="icon">🎬</div>'
                    '<div>No releases match this filter.</div></div>')
    elif view == "list":
        def _row_thumb(url):
            if url:
                return (f'<img src="{html.escape(url)}" loading="lazy" '
                       f'style="width:44px;height:64px;object-fit:cover;'
                       f'border-radius:3px;display:block;background:#f1f5f9" />')
            return ('<div style="width:44px;height:64px;background:#f1f5f9;'
                   'border:1px solid var(--border);border-radius:3px;'
                   'display:flex;align-items:center;justify-content:center;'
                   'font-size:18px;color:#cbd5e1">🎬</div>')
        body_rows = "".join(
            f'<tr>'
            f'<td style="width:60px;padding:8px 12px">{_row_thumb(r["poster_url"])}</td>'
            f'<td class="name">'
            f'{(html.escape(r["canonical_title"]) + "<br>") if r["canonical_title"] and r["canonical_title"] != r["name"] else ""}'
            f'<span style="color:var(--muted);font-weight:400;font-size:13px">{html.escape(r["name"])}</span>'
            f'</td>'
            f'<td><span class="pill {"accent" if r["category"]=="movie" else "ok" if r["category"]=="tv" else "muted"}">{r["category"]}</span></td>'
            f'<td>{("S%02dE%02d" % (r["season"], r["episode"])) if r["season"] and r["episode"] else ""}</td>'
            f'<td>{r["quality"] or ""}</td>'
            f'<td class="num">{_fmt_size(r["size_bytes"])}</td>'
            f'<td class="num">{_fmt_time(r["posted_at"])}</td>'
            f'</tr>'
            for r in rows
        )
        body_html = (f'<table><thead><tr>'
                    f'<th></th><th>name</th><th>cat</th><th>S/E</th><th>quality</th>'
                    f'<th>size</th><th>posted</th>'
                    f'</tr></thead><tbody>{body_rows}</tbody></table>')
    else:
        body_html = (f'<div class="poster-grid">'
                    f'{"".join(_release_card(r) for r in rows)}'
                    f'</div>')

    body = (f'<h2 class="section">Parsed releases <span class="count">{len(rows)} shown</span></h2>'
           f'{toolbar}{body_html}')
    actions = (f'<form action="/releases" method="GET" style="margin-left:auto">'
              f'<input type="text" name="q" class="search" placeholder="filter…" '
              f'value="{html.escape(q) if q else ""}" /></form>')
    return HTMLResponse(_layout("Releases", "/releases", body, top_actions_html=actions))


# ════════════════════════════════════════════════════════════════════
# Page: Downloads (queue + history)
# ════════════════════════════════════════════════════════════════════
@app.get("/downloads", response_class=HTMLResponse)
async def page_downloads():
    if not login.session_exists():
        return RedirectResponse("/login")
    async with db_pool.acquire() as conn:
        active = await conn.fetch(
            """SELECT d.id, d.status, d.requested_at, r.name, r.size_bytes
               FROM downloads d JOIN releases r ON r.id = d.release_id
               WHERE d.status IN ('pending','downloading')
               ORDER BY d.requested_at""")
        done = await conn.fetch(
            """SELECT d.id, d.status, d.local_path, d.requested_at, d.finished_at,
                      d.error_message, r.name, r.size_bytes
               FROM downloads d JOIN releases r ON r.id = d.release_id
               WHERE d.status IN ('completed','failed','cancelled')
               ORDER BY d.finished_at DESC NULLS LAST LIMIT 100""")

    def _render(rows, show_finished):
        if not rows:
            return ('<div class="empty-state"><div class="icon">⬇</div>'
                   '<div>nothing here yet</div></div>')
        items = []
        for d in rows:
            icon = ({"pending": "⏳", "downloading": "⬇", "completed": "✓",
                    "failed": "✗", "cancelled": "—"}).get(d["status"], "?")
            pill_cls = ({"pending": "warn", "downloading": "accent",
                        "completed": "ok", "failed": "bad", "cancelled": "muted"}
                       ).get(d["status"], "muted")
            err_html = ""
            error_msg = d["error_message"] if "error_message" in d.keys() else None
            if error_msg:
                err_html = f'<div class="err">{html.escape(error_msg)}</div>'
            local = ""
            if show_finished and "local_path" in d.keys() and d["local_path"]:
                local = " · " + html.escape(d["local_path"].rsplit("/", 1)[-1])
            finished = d["finished_at"] if "finished_at" in d.keys() else None
            items.append(
                f'<div class="dl-item">'
                f'<div class="icon">{icon}</div>'
                f'<div class="info">'
                f'<div class="name">{html.escape(d["name"])}</div>'
                f'<div class="meta">'
                f'<span class="pill {pill_cls}">{d["status"]}</span>'
                f'<span>· {_fmt_size(d["size_bytes"])}</span>'
                f'<span>· {_fmt_time(d["requested_at"])}{local}</span>'
                f'</div>{err_html}'
                f'</div>'
                f'<div class="right">{_fmt_time(finished) if show_finished else ""}</div>'
                f'</div>'
            )
        return f'<div class="dl-list">{"".join(items)}</div>'

    body = (f'<h2 class="section">Queue <span class="count">{len(active)}</span></h2>'
           f'{_render(active, show_finished=False)}'
           f'<h2 class="section">History <span class="count">last 100</span></h2>'
           f'{_render(done, show_finished=True)}')
    return HTMLResponse(_layout("Downloads", "/downloads", body))


# ════════════════════════════════════════════════════════════════════
# Page: Search
# ════════════════════════════════════════════════════════════════════
@app.get("/search", response_class=HTMLResponse)
async def page_search(q: Optional[str] = None):
    if not login.session_exists():
        return RedirectResponse("/login")

    results_html = ""
    if q:
        where = ["1=1"]
        params = []
        for w in q.split():
            params.append(f"%{w}%")
            where.append(f"name ILIKE ${len(params)}")
        sql = (f"SELECT id, guid, name, category, season, episode, quality, "
              f"size_bytes, posted_at, parse_score, poster_url, canonical_title "
              f"FROM releases WHERE {' AND '.join(where)} "
              f"ORDER BY posted_at DESC NULLS LAST LIMIT 120")
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        if not rows:
            results_html = ('<div class="empty-state">'
                          '<div class="icon">🔍</div>'
                          f'<div>No matches for <strong>{html.escape(q)}</strong></div>'
                          '<div style="margin-top:8px;font-size:12px">Try fewer words or different spelling.</div>'
                          '</div>')
        else:
            results_html = (
                f'<h2 class="section">Results <span class="count">{len(rows)}</span></h2>'
                f'<div class="poster-grid">'
                f'{"".join(_release_card(r) for r in rows)}'
                f'</div>'
            )

    body = f"""
<form action="/search" method="GET" style="margin-bottom:24px;display:flex;gap:10px">
  <input type="text" name="q" value="{html.escape(q) if q else ''}"
         placeholder="Search parsed releases… (multi-word AND)"
         style="flex:1;font-size:15px;padding:11px 16px" autofocus />
  <button type="submit">Search</button>
</form>
{results_html}"""
    return HTMLResponse(_layout("Search", "/search", body, top_actions_html=""))


# ════════════════════════════════════════════════════════════════════
# Page: Settings
# ════════════════════════════════════════════════════════════════════
@app.get("/settings", response_class=HTMLResponse)
async def page_settings():
    authed = login.session_exists()
    user = login.state.user_info or {}
    async with db_pool.acquire() as conn:
        tmdb_raw = await conn.fetchval(
            "SELECT value FROM config WHERE key='tmdb_api_key'")
        meta_stats = await conn.fetchrow("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE metadata_source='tmdb') AS tmdb,
                   count(*) FILTER (WHERE metadata_source='wikipedia') AS wiki,
                   count(*) FILTER (WHERE metadata_source='miss') AS missed,
                   count(*) FILTER (WHERE metadata_source IS NULL) AS pending
            FROM releases
        """)
    tmdb_masked = (tmdb_raw[:6] + "…" + tmdb_raw[-4:]) if tmdb_raw else ""
    nsfw_on = await _is_nsfw_enabled()
    user_html = (
        f'<div class="name">@{html.escape(str(user.get("username") or user.get("first_name") or "-"))} '
        f'<span class="pill ok">signed in</span></div>'
        if authed else
        '<div class="name">not signed in <span class="pill bad">no session</span></div>'
    )
    user_right = (
        '<a class="btn" href="/login">Sign in</a>' if not authed
        else '<button class="ghost" onclick="if(confirm(\'Sign out and remove session?\'))fetch(\'/api/login/logout\',{method:\'POST\'}).then(()=>location.href=\'/login\')">Sign out</button>'
    )

    body = f"""
<h2 class="section">Telegram account</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">👤</div>
    <div class="info">{user_html}
      <div class="meta">API ID <code>{os.environ.get("TG_API_ID","-")}</code> · session at <code>_data/session/tgarr.session</code></div>
    </div>
    <div class="right">{user_right}</div>
  </div>
</div>

<h2 class="section">Crawler</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">⚙</div>
    <div class="info"><div class="name">Parser score threshold</div>
      <div class="meta">Messages below this score don't become releases</div></div>
    <div class="right"><code>{os.environ.get("TG_PARSE_SCORE_MIN","0.30")}</code></div>
  </div>
  <div class="dl-item">
    <div class="icon">📁</div>
    <div class="info"><div class="name">Download root</div>
      <div class="meta">Where MTProto-fetched files land</div></div>
    <div class="right"><code>{os.environ.get("TG_DOWNLOAD_ROOT","/downloads/tgarr")}</code></div>
  </div>
  <div class="dl-item">
    <div class="icon">🔢</div>
    <div class="info"><div class="name">Backfill limit per channel</div>
      <div class="meta">Max historical messages scanned on first run</div></div>
    <div class="right"><code>{os.environ.get("TG_BACKFILL_LIMIT","5000")}</code></div>
  </div>
</div>

<h2 class="section">External endpoints</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">🔌</div>
    <div class="info"><div class="name">Newznab indexer URL</div>
      <div class="meta">Paste into Sonarr / Radarr → Settings → Indexers → ➕ Newznab</div></div>
    <div class="right"><code>{os.environ.get("TGARR_BASE_URL","")}/newznab/</code></div>
  </div>
  <div class="dl-item">
    <div class="icon">⬇</div>
    <div class="info"><div class="name">SABnzbd URL base</div>
      <div class="meta">Paste into Sonarr / Radarr → Download Clients → ➕ SABnzbd → URL Base</div></div>
    <div class="right"><code>/sabnzbd</code></div>
  </div>
</div>

<h2 class="section">Content policy</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">🔞</div>
    <div class="info">
      <div class="name">Adult content (NSFW) — <span class="pill {'ok' if nsfw_on else 'muted'}">{'enabled' if nsfw_on else 'hidden (default)'}</span></div>
      <div class="meta">
        By default, channels flagged adult are hidden everywhere in tgarr.
        You can opt in below — only enable if you are <strong>18+</strong> and
        understand you are responsible for compliance with your local laws.
        tgarr is content-neutral self-hosted software; <strong>we are not
        responsible for materials shared by others on Telegram</strong>.
      </div>
      <form method="POST" action="/api/settings/nsfw_enabled" style="margin-top:12px">
        <label style="display:flex;align-items:flex-start;gap:8px;cursor:pointer;font-size:14px;line-height:1.55">
          <input type="checkbox" name="value" value="true" {'checked' if nsfw_on else ''} onchange="this.form.submit()" style="margin-top:4px" />
          <span>I am 18+ and want to view channels tgarr has classified as adult.
            I understand tgarr is a viewer/indexer of Telegram content I have already joined,
            not the publisher.</span>
        </label>
      </form>
    </div>
  </div>
  <div class="dl-item">
    <div class="icon">🛑</div>
    <div class="info">
      <div class="name">CSAM hard block — <span class="pill bad">always on, cannot be disabled</span></div>
      <div class="meta">
        Child Sexual Abuse Material. Channels matching a hardcoded keyword
        blocklist (loli/shota/child-porn/etc. across multiple scripts) are
        permanently blocked regardless of any setting. They are never displayed
        in tgarr, never federated to <code>registry.tgarr.me</code>, and never
        downloaded. Matches are logged at <code>/tmp/tgarr-csam-flags.log</code>
        for review. If you encounter CSAM, report to
        <a href="https://report.cybertip.org" target="_blank">NCMEC CyberTipline</a>
        or your jurisdiction's hotline via
        <a href="https://www.inhope.org" target="_blank">INHOPE</a>.
      </div>
    </div>
  </div>
</div>

<h2 class="section">Metadata sources</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">🔑</div>
    <div class="info">
      <div class="name">TMDB API key <span class="pill {('ok' if tmdb_raw else 'muted')}">{('configured' if tmdb_raw else 'not set — using Wikipedia fallback')}</span></div>
      <div class="meta">Posters &amp; canonical titles from <a href="https://www.themoviedb.org/settings/api" target="_blank">themoviedb.org</a> (free, personal use). If empty, tgarr falls back to free Wikipedia REST — lower coverage but no key needed.</div>
      <form method="POST" action="/api/settings/tmdb_key" style="margin-top:12px;display:flex;gap:10px;max-width:600px">
        <input type="text" name="value" placeholder="paste your TMDB API key (or leave blank to clear)" value="{html.escape(tmdb_masked)}" style="flex:1" />
        <button type="submit">Save</button>
      </form>
    </div>
  </div>
  <div class="dl-item">
    <div class="icon">📊</div>
    <div class="info">
      <div class="name">Enrichment progress</div>
      <div class="meta">
        <span class="pill accent">TMDB {meta_stats['tmdb']:,}</span>
        <span class="pill ok">Wikipedia {meta_stats['wiki']:,}</span>
        <span class="pill warn">Not found {meta_stats['missed']:,}</span>
        <span class="pill muted">Pending {meta_stats['pending']:,}</span>
        / total {meta_stats['total']:,}
      </div>
    </div>
  </div>
</div>

<h2 class="section">About</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">ℹ</div>
    <div class="info"><div class="name">tgarr v{TGARR_VERSION}</div>
      <div class="meta">Self-hosted Telegram-as-source bridge · MIT license</div></div>
    <div class="right"><a class="btn ghost" href="https://tgarr.me" target="_blank">tgarr.me ↗</a></div>
  </div>
</div>
"""
    return HTMLResponse(_layout("Settings", "/settings", body))


# ════════════════════════════════════════════════════════════════════
# Admin JSON endpoints
# ════════════════════════════════════════════════════════════════════
@app.get("/api/admin/channels")
async def list_channels():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.id, c.tg_chat_id, c.username, c.title, c.category,
                      c.enabled, c.backfilled,
                      (SELECT count(*) FROM messages m WHERE m.channel_id = c.id)
                                                                AS msg_count
               FROM channels c ORDER BY title""")
    return [dict(r) for r in rows]


@app.get("/api/admin/releases")
async def list_releases(limit: int = 50, q: Optional[str] = None):
    where = ""
    params = []
    if q:
        params.append(f"%{q}%")
        where = "WHERE name ILIKE $1"
    sql = (f"SELECT id, guid, name, category, season, episode, quality, "
          f"size_bytes, posted_at, parse_score FROM releases {where} "
          f"ORDER BY posted_at DESC NULLS LAST LIMIT {max(1, min(limit, 500))}")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════
# Newznab indexer
# ════════════════════════════════════════════════════════════════════
CAPS_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="{TGARR_VERSION}" title="tgarr" />
  <limits max="200" default="50"/>
  <searching>
    <search          available="yes" supportedParams="q"/>
    <tv-search       available="yes" supportedParams="q,season,ep,tvdbid,rid"/>
    <movie-search    available="yes" supportedParams="q,imdbid,tmdbid"/>
    <audio-search    available="no"  supportedParams="q"/>
    <book-search     available="no"  supportedParams="q"/>
  </searching>
  <categories>
    <category id="2000" name="Movies">
      <subcat id="2030" name="Movies/SD"/>
      <subcat id="2040" name="Movies/HD"/>
      <subcat id="2045" name="Movies/UHD"/>
      <subcat id="2050" name="Movies/Foreign"/>
    </category>
    <category id="5000" name="TV">
      <subcat id="5030" name="TV/SD"/>
      <subcat id="5040" name="TV/HD"/>
      <subcat id="5045" name="TV/UHD"/>
      <subcat id="5070" name="TV/Anime"/>
    </category>
  </categories>
</caps>"""


def _category_id(rel) -> str:
    cat = rel["category"]
    q = (rel["quality"] or "").lower()
    if cat == "movie":
        if "2160" in q:
            return "2045"
        if "1080" in q or "720" in q:
            return "2040"
        return "2030"
    if cat == "tv":
        if "2160" in q:
            return "5045"
        if "1080" in q or "720" in q:
            return "5040"
        return "5030"
    return "0"


def _item_xml(r, base_url: str, apikey: str) -> str:
    guid = str(r["guid"])
    link = f"{base_url}/newznab/api?t=get&id={guid}&apikey={apikey}"
    # XML 1.0 requires & to be escaped as &amp; even inside element body —
    # Sonarr's strict XDocument parser bails out on raw &id= otherwise.
    link_text = html.escape(link)
    link_attr = html.escape(link, quote=True)
    pubdate = (r["posted_at"].strftime("%a, %d %b %Y %H:%M:%S +0000")
               if r["posted_at"] else "")
    size = r["size_bytes"] or 0
    cat_id = _category_id(r)
    title = html.escape(r["name"])
    return (
        f"    <item>\n"
        f"      <title>{title}</title>\n"
        f"      <guid isPermaLink=\"false\">{guid}</guid>\n"
        f"      <link>{link_text}</link>\n"
        f"      <comments>{link_text}</comments>\n"
        f"      <pubDate>{pubdate}</pubDate>\n"
        f"      <category>{cat_id}</category>\n"
        f"      <size>{size}</size>\n"
        f"      <description>tgarr release via Telegram MTProto</description>\n"
        f"      <enclosure url=\"{link_attr}\" length=\"{size}\" type=\"application/x-nzb\"/>\n"
        f"      <newznab:attr name=\"category\" value=\"{cat_id}\"/>\n"
        f"      <newznab:attr name=\"size\" value=\"{size}\"/>\n"
        f"      <newznab:attr name=\"guid\" value=\"{guid}\"/>\n"
        f"    </item>"
    )


def _feed_envelope(items_xml: str, title: str, base_url: str) -> str:
    return (
        f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        f"<rss version=\"2.0\" xmlns:newznab=\"http://www.newznab.com/DTD/2010/feeds/attributes/\">\n"
        f"  <channel>\n"
        f"    <title>tgarr</title>\n"
        f"    <description>{html.escape(title)}</description>\n"
        f"    <link>{base_url}/newznab/api</link>\n"
        f"{items_xml}\n"
        f"  </channel>\n"
        f"</rss>"
    )


@app.get("/newznab/api", response_class=PlainTextResponse)
@app.get("/newznab", response_class=PlainTextResponse)
async def newznab_api(
    t: str = Query("search"),
    q: Optional[str] = Query(None),
    season: Optional[int] = None,
    ep: Optional[int] = None,
    imdbid: Optional[str] = None,
    tmdbid: Optional[str] = None,
    tvdbid: Optional[str] = None,
    cat: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    apikey: Optional[str] = "tgarr",
    id: Optional[str] = None,
):
    base_url = os.environ.get("TGARR_BASE_URL", "")
    if t == "caps":
        return Response(CAPS_XML, media_type="application/xml")

    if t == "get":
        return await _handle_grab(id)

    where_parts = ["1=1"]
    params = []

    if q:
        for word in q.split():
            params.append(f"%{word}%")
            where_parts.append(f"name ILIKE ${len(params)}")

    if t == "tvsearch":
        where_parts.append("category = 'tv'")
        if season is not None:
            params.append(season)
            where_parts.append(f"season = ${len(params)}")
        if ep is not None:
            params.append(ep)
            where_parts.append(f"episode = ${len(params)}")
    elif t == "movie":
        where_parts.append("category = 'movie'")

    limit_val = max(1, min(limit, 200))
    sql = (f"SELECT id, guid, name, category, season, episode, quality, "
          f"source, codec, size_bytes, posted_at FROM releases WHERE "
          f"{' AND '.join(where_parts)} ORDER BY posted_at DESC NULLS LAST "
          f"LIMIT {limit_val} OFFSET {max(0, offset)}")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    items = "\n".join(_item_xml(r, base_url, apikey or "tgarr") for r in rows)
    return Response(
        _feed_envelope(items, f"search t={t} q={q or ''}", base_url),
        media_type="application/xml",
    )


async def _handle_grab(guid: Optional[str]) -> Response:
    if not guid:
        return Response("missing id", status_code=400)
    async with db_pool.acquire() as conn:
        rel = await conn.fetchrow(
            "SELECT id, name FROM releases WHERE guid = $1", guid)
        if not rel:
            return Response("release not found", status_code=404)
        await conn.execute(
            """INSERT INTO downloads (release_id, status)
               SELECT $1, 'pending'
               WHERE NOT EXISTS (
                 SELECT 1 FROM downloads
                 WHERE release_id = $1 AND status IN ('pending','downloading','completed')
               )""", rel["id"])
    stub = (
        f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        f"<!DOCTYPE nzb PUBLIC \"-//newzBin//DTD NZB 1.1//EN\" "
        f"\"http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd\">\n"
        f"<nzb xmlns=\"http://www.newzbin.com/DTD/2003/nzb\">\n"
        f"  <file poster=\"tgarr\" date=\"{int(time.time())}\" "
        f"subject=\"{html.escape(rel['name'])}\">\n"
        f"    <groups><group>alt.binaries.tgarr</group></groups>\n"
        f"    <segments><segment bytes=\"1\" number=\"1\">tgarr-{guid}</segment></segments>\n"
        f"  </file>\n"
        f"</nzb>"
    )
    return Response(
        stub,
        media_type="application/x-nzb",
        headers={"Content-Disposition": f'attachment; filename="{rel["name"]}.nzb"'},
    )


# ════════════════════════════════════════════════════════════════════
# SABnzbd download-client emulation
# ════════════════════════════════════════════════════════════════════
@app.get("/sabnzbd/api")
@app.get("/api")
async def sab_api(
    mode: str = Query("queue"),
    name: Optional[str] = None,
    value: Optional[str] = None,
    cat: Optional[str] = None,
    nzbname: Optional[str] = None,
    apikey: Optional[str] = None,
    output: str = Query("json"),
):
    if mode == "version":
        return {"version": "3.7.2"}
    if mode == "auth":
        return {"auth": "apikey"}
    if mode == "get_config":
        return {"config": {
            "misc": {
                "complete_dir": "/downloads",
                "download_dir": "/incomplete-downloads",
                "history_retention": "0",
            },
            "categories": [{"name": "*", "dir": "tgarr"},
                           {"name": "tv", "dir": "tv"},
                           {"name": "movies", "dir": "movies"}],
        }}
    if mode == "queue":
        return await _sab_queue()
    if mode == "history":
        return await _sab_history()
    if mode in ("addurl", "addfile"):
        return await _sab_addurl(name or value or "", nzbname, cat)
    if mode == "delete":
        return await _sab_delete(name or value or "")
    if mode == "switch":
        return {"status": True}
    return JSONResponse({"status": False, "error": f"unknown mode: {mode}"})


async def _sab_queue():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT d.id, d.status, r.name, r.size_bytes, r.guid
               FROM downloads d JOIN releases r ON r.id = d.release_id
               WHERE d.status IN ('pending','downloading')
               ORDER BY d.requested_at""")
    slots = []
    for r in rows:
        size_mb = (r["size_bytes"] or 0) / 1048576
        slots.append({
            "nzo_id": f"tgarr-{r['guid']}",
            "filename": r["name"],
            "cat": "tv",
            "status": "Downloading" if r["status"] == "downloading" else "Queued",
            "mb": f"{size_mb:.1f}",
            "mbleft": f"{size_mb:.1f}" if r["status"] != "downloading" else "0",
            "percentage": "0",
            "size": f"{size_mb:.1f} MB",
            "sizeleft": f"{size_mb:.1f} MB",
            "timeleft": "0:01:00",
            "priority": "Normal",
            "script": "None",
            "msgid": "",
            "index": 0,
        })
    return {"queue": {
        "status": "Idle" if not slots else "Downloading",
        "version": "3.7.2", "paused": False,
        "noofslots_total": len(slots), "noofslots": len(slots),
        "slots": slots, "speedlimit": "", "speedlimit_abs": "",
        "speed": "0 K", "kbpersec": "0",
        "size": "0 MB", "sizeleft": "0 MB", "mb": "0", "mbleft": "0",
        "diskspace1": "1000", "diskspace2": "1000",
        "diskspace1_norm": "1 T", "diskspace2_norm": "1 T",
        "diskspacetotal1": "2000", "diskspacetotal2": "2000",
        "limit": "0", "have_warnings": "0", "pause_int": "0", "loadavg": "",
    }}


async def _sab_history():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT d.id, d.status, d.local_path, d.finished_at,
                      r.name, r.size_bytes, r.guid, d.error_message
               FROM downloads d JOIN releases r ON r.id = d.release_id
               WHERE d.status IN ('completed','failed')
               ORDER BY d.finished_at DESC NULLS LAST LIMIT 200""")
    slots = []
    for r in rows:
        size_mb = (r["size_bytes"] or 0) / 1048576
        slots.append({
            "nzo_id": f"tgarr-{r['guid']}",
            "name": r["name"], "category": "tv",
            "storage": r["local_path"] or "/downloads/tgarr",
            "status": "Completed" if r["status"] == "completed" else "Failed",
            "size": f"{size_mb:.1f} MB",
            "bytes": r["size_bytes"] or 0,
            "completed": int(r["finished_at"].timestamp()) if r["finished_at"] else 0,
            "fail_message": r["error_message"] or "",
            "action_line": "", "show_details": "True", "script_log": "",
            "report": "", "pp": "3", "script": "None", "url": "",
            "id": str(r["id"]),
        })
    return {"history": {
        "noofslots": len(slots), "slots": slots,
        "total_size": "0 B", "month_size": "0 B", "week_size": "0 B", "day_size": "0 B",
        "version": "3.7.2",
    }}


async def _sab_addurl(url: str, nzbname: Optional[str], cat: Optional[str]):
    guid = None
    if "id=" in url:
        guid = url.split("id=")[1].split("&")[0]
    if not guid:
        return {"status": False, "error": "no guid in url"}
    async with db_pool.acquire() as conn:
        rel = await conn.fetchrow("SELECT id FROM releases WHERE guid = $1", guid)
        if not rel:
            return {"status": False, "error": "release not found"}
        await conn.execute(
            """INSERT INTO downloads (release_id, status)
               SELECT $1, 'pending'
               WHERE NOT EXISTS (
                 SELECT 1 FROM downloads
                 WHERE release_id = $1 AND status IN ('pending','downloading')
               )""", rel["id"])
    return {"status": True, "nzo_ids": [f"tgarr-{guid}"]}


async def _sab_delete(nzo_id: str):
    if not nzo_id.startswith("tgarr-"):
        return {"status": False, "error": "bad nzo_id"}
    guid = nzo_id[6:]
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE downloads SET status='cancelled'
               WHERE release_id = (SELECT id FROM releases WHERE guid = $1)
                 AND status IN ('pending','downloading')""", guid)
    return {"status": True}
