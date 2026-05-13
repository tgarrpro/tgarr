"""tgarr API — Newznab indexer + SABnzbd download-client emulation.

Sonarr/Radarr/Lidarr configure tgarr in two places:
1. Settings > Indexers > Add > Newznab → URL `http://<tgarr-host>:8088/newznab/`
2. Settings > Download Clients > Add > SABnzbd → Host `<tgarr-host>` Port `8088`
   URL Base `/sabnzbd/`

Sonarr search → newznab feed.
Sonarr grab → SAB `addurl` → row in `downloads` table → crawler's worker fetches
via MTProto → drops file in /downloads/tgarr/<release>/ → Sonarr's CDH imports.
"""
import html
import os
import time
from typing import Optional

import asyncpg
from fastapi import FastAPI, Form, Header, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

import login  # local module

DB_DSN = os.environ["DB_DSN"]
TGARR_VERSION = "0.2.0"
ANY_API_KEY_ACCEPTED = True

app = FastAPI(title="tgarr", version=TGARR_VERSION)
db_pool: Optional[asyncpg.Pool] = None


@app.on_event("startup")
async def _startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=8)


@app.on_event("shutdown")
async def _shutdown():
    if db_pool:
        await db_pool.close()


# ════════════════════════════════════════════════════════════════════
# Login (Telegram QR + SMS)
# ════════════════════════════════════════════════════════════════════
LOGIN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>tgarr — sign in to Telegram</title>
<style>
:root { --bg:#0e1621; --bg2:#17212B; --fg:#f0f3f5; --muted:#8b9aa8; --accent:#5eb6e5; --tg-blue:#229ED9; --border:#2a3849; --ok:#5cd97e; --bad:#e57373; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font:14px/1.5 -apple-system,Segoe UI,system-ui,sans-serif; background:var(--bg); color:var(--fg); min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px; }
.card { background:var(--bg2); border-radius:8px; padding:32px; max-width:520px; width:100%; }
h1 { color:var(--tg-blue); font-size:28px; letter-spacing:-1px; margin-bottom:6px; }
h1 span { color:var(--fg); }
.sub { color:var(--muted); margin-bottom:24px; font-size:13px; }
.tabs { display:flex; gap:0; margin-bottom:24px; border-bottom:1px solid var(--border); }
.tab { padding:10px 18px; cursor:pointer; color:var(--muted); border-bottom:2px solid transparent; font-weight:600; font-size:13px; user-select:none; }
.tab.active { color:var(--accent); border-bottom-color:var(--accent); }
.panel { display:none; }
.panel.active { display:block; }
#qrimg { display:block; margin:16px auto; max-width:280px; padding:12px; background:#fff; border-radius:8px; }
.status { padding:12px 16px; border-radius:6px; background:rgba(94,182,229,0.10); color:var(--accent); font-size:13px; margin-top:16px; min-height:44px; }
.status.error { background:rgba(229,115,115,0.12); color:var(--bad); }
.status.ok { background:rgba(92,217,126,0.12); color:var(--ok); }
input[type=text], input[type=tel], input[type=password] { width:100%; padding:10px 12px; background:var(--bg); color:var(--fg); border:1px solid var(--border); border-radius:4px; font-size:14px; font-family:inherit; margin-top:6px; }
input:focus { outline:none; border-color:var(--accent); }
label { display:block; margin-top:14px; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:1px; }
button { padding:10px 22px; background:var(--tg-blue); color:#fff; border:none; border-radius:4px; font-weight:600; cursor:pointer; font-size:14px; margin-top:14px; }
button:hover { opacity:0.9; }
button:disabled { opacity:0.4; cursor:not-allowed; }
.hint { color:var(--muted); font-size:12px; margin-top:8px; line-height:1.4; }
code { background:rgba(94,182,229,0.13); padding:2px 6px; border-radius:3px; color:var(--accent); font-family:ui-monospace,SF Mono,Menlo,monospace; font-size:12px; }
</style>
</head><body>
<div class="card">
  <h1>tg<span>arr</span> · sign in</h1>
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
// Auto-start QR on page load
qrStart();
</script>
</body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    if login.session_exists():
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


# ════════════════════════════════════════════════════════════════════
# Root / health / admin
# ════════════════════════════════════════════════════════════════════
@app.get("/")
async def root(accept: Optional[str] = Header(None)):
    async with db_pool.acquire() as conn:
        stats = await conn.fetchrow(
            """SELECT (SELECT count(*) FROM channels)                     AS channels,
                      (SELECT count(*) FROM messages)                     AS messages,
                      (SELECT count(*) FROM releases)                     AS releases,
                      (SELECT count(*) FROM downloads
                         WHERE status='pending')                          AS pending,
                      (SELECT count(*) FROM downloads
                         WHERE status='downloading')                      AS downloading,
                      (SELECT count(*) FROM downloads
                         WHERE status='completed')                        AS completed""")
        dls = await conn.fetch(
            """SELECT d.id, d.status, d.local_path, d.requested_at, d.finished_at,
                      d.error_message, r.name, r.size_bytes
               FROM downloads d
               JOIN releases r ON r.id = d.release_id
               ORDER BY d.requested_at DESC
               LIMIT 50""")
        rels = await conn.fetch(
            """SELECT name, category, season, episode, quality, size_bytes,
                      posted_at, parse_score
               FROM releases
               ORDER BY posted_at DESC NULLS LAST
               LIMIT 30""")
        chans = await conn.fetch(
            """SELECT c.tg_chat_id, c.username, c.title, c.backfilled,
                      (SELECT count(*) FROM messages m WHERE m.channel_id = c.id)
                                                                AS msg_count
               FROM channels c
               ORDER BY title""")

    wants_html = accept and "text/html" in accept
    if not wants_html:
        return {"tgarr": TGARR_VERSION, "authed": login.session_exists(),
                **dict(stats)}

    # Browser hit without a Telegram session yet → redirect to login wizard
    if not login.session_exists():
        return RedirectResponse("/login")

    def _fmt_size(n):
        if not n:
            return ""
        for u in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {u}"
            n /= 1024
        return f"{n:.1f} PB"

    def _fmt_time(t):
        return t.strftime("%Y-%m-%d %H:%M") if t else ""

    dl_rows = "\n".join(
        f"<tr class='{d['status']}'>"
        f"<td>{d['id']}</td>"
        f"<td class='s-{d['status']}'>{d['status']}</td>"
        f"<td>{html.escape(d['name'])}</td>"
        f"<td>{_fmt_size(d['size_bytes'])}</td>"
        f"<td>{_fmt_time(d['requested_at'])}</td>"
        f"<td>{_fmt_time(d['finished_at'])}</td>"
        f"<td title='{html.escape(d['local_path'] or '')}'>"
        f"{html.escape((d['local_path'] or '').rsplit('/', 1)[-1])}</td>"
        f"<td>{html.escape(d['error_message'] or '')}</td>"
        f"</tr>"
        for d in dls
    )
    if not dl_rows:
        dl_rows = "<tr><td colspan='8' class='empty'>no downloads yet — grab something from Sonarr/Radarr</td></tr>"

    rel_rows = "\n".join(
        f"<tr>"
        f"<td>{html.escape(r['name'])}</td>"
        f"<td>{r['category']}</td>"
        f"<td>{'S{:02}E{:02}'.format(r['season'], r['episode']) if r['season'] is not None and r['episode'] is not None else (str(r['season']) if r['season'] else '')}</td>"
        f"<td>{r['quality'] or ''}</td>"
        f"<td>{_fmt_size(r['size_bytes'])}</td>"
        f"<td>{_fmt_time(r['posted_at'])}</td>"
        f"<td>{r['parse_score']:.2f}</td>"
        f"</tr>"
        for r in rels
    )

    chan_rows = "\n".join(
        f"<tr>"
        f"<td>{html.escape(c['title'] or '')}</td>"
        f"<td>{('@' + c['username']) if c['username'] else ''}</td>"
        f"<td>{c['msg_count']}</td>"
        f"<td>{'✓' if c['backfilled'] else '…'}</td>"
        f"</tr>"
        for c in chans
    )

    body = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>tgarr — dashboard</title>
<meta http-equiv="refresh" content="10">
<style>
:root {{ --bg:#0e1621; --bg2:#17212B; --fg:#f0f3f5; --muted:#8b9aa8; --accent:#5eb6e5; --tg-blue:#229ED9; --border:#2a3849; --ok:#5cd97e; --warn:#d4a83a; --bad:#e57373; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font:14px/1.5 -apple-system,Segoe UI,system-ui,sans-serif; background:var(--bg); color:var(--fg); padding:24px; }}
header {{ display:flex; align-items:baseline; gap:16px; margin-bottom:24px; flex-wrap:wrap; }}
h1 {{ font-size:28px; color:var(--tg-blue); letter-spacing:-1px; }}
h1 span {{ color:var(--fg); }}
.ver {{ color:var(--muted); font-size:12px; }}
.stats {{ display:flex; gap:24px; flex-wrap:wrap; padding:16px 20px; background:var(--bg2); border-radius:6px; margin-bottom:24px; }}
.stat {{ }}
.stat .n {{ font-size:24px; font-weight:600; color:var(--accent); }}
.stat .l {{ font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:1px; }}
h2 {{ font-size:12px; text-transform:uppercase; color:var(--accent); letter-spacing:2px; margin:32px 0 12px; }}
table {{ width:100%; border-collapse:collapse; background:var(--bg2); border-radius:6px; overflow:hidden; font-size:13px; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid var(--border); }}
th {{ background:rgba(94,182,229,0.08); color:var(--accent); font-size:11px; text-transform:uppercase; letter-spacing:1px; font-weight:600; }}
tr:last-child td {{ border-bottom:none; }}
tr:hover td {{ background:rgba(94,182,229,0.04); }}
.s-pending {{ color:var(--warn); font-weight:600; }}
.s-downloading {{ color:var(--accent); font-weight:600; }}
.s-completed {{ color:var(--ok); font-weight:600; }}
.s-failed {{ color:var(--bad); font-weight:600; }}
.s-cancelled {{ color:var(--muted); }}
.empty {{ color:var(--muted); text-align:center; padding:24px !important; font-style:italic; }}
footer {{ margin-top:32px; color:var(--muted); font-size:12px; text-align:center; }}
footer a {{ color:var(--accent); }}
code {{ background:rgba(94,182,229,0.13); padding:2px 6px; border-radius:3px; color:var(--accent); font-family:ui-monospace,SF Mono,Menlo,monospace; }}
</style>
</head><body>

<header>
  <h1>tg<span>arr</span></h1>
  <span class="ver">v{TGARR_VERSION} · auto-refresh 10s</span>
</header>

<div class="stats">
  <div class="stat"><div class="n">{stats['channels']}</div><div class="l">channels</div></div>
  <div class="stat"><div class="n">{stats['messages']:,}</div><div class="l">messages indexed</div></div>
  <div class="stat"><div class="n">{stats['releases']:,}</div><div class="l">parsed releases</div></div>
  <div class="stat"><div class="n">{stats['pending']}</div><div class="l">pending</div></div>
  <div class="stat"><div class="n">{stats['downloading']}</div><div class="l">downloading</div></div>
  <div class="stat"><div class="n">{stats['completed']}</div><div class="l">completed</div></div>
</div>

<h2>Downloads</h2>
<table>
  <thead><tr>
    <th>#</th><th>status</th><th>release</th><th>size</th>
    <th>requested</th><th>finished</th><th>file</th><th>error</th>
  </tr></thead>
  <tbody>
{dl_rows}
  </tbody>
</table>

<h2>Recent releases (last 30)</h2>
<table>
  <thead><tr>
    <th>name</th><th>cat</th><th>se/ep</th><th>quality</th><th>size</th><th>posted</th><th>score</th>
  </tr></thead>
  <tbody>
{rel_rows}
  </tbody>
</table>

<h2>Indexed channels</h2>
<table>
  <thead><tr><th>title</th><th>username</th><th>messages</th><th>backfilled</th></tr></thead>
  <tbody>
{chan_rows}
  </tbody>
</table>

<footer>
  Newznab indexer: <code>http://&lt;host&gt;:8765/newznab/</code> ·
  SAB download client: <code>/sabnzbd</code> on the same host/port ·
  <a href="https://tgarr.me">tgarr.me</a> · <a href="https://github.com/tgarrpro/tgarr">github</a>
</footer>
</body></html>"""
    return HTMLResponse(body)


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
        where = f"WHERE name ILIKE $1"
    sql = f"""SELECT id, guid, name, category, season, episode, quality,
                     size_bytes, posted_at, parse_score
              FROM releases {where}
              ORDER BY posted_at DESC NULLS LAST
              LIMIT {max(1, min(limit, 500))}"""
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
    pubdate = (r["posted_at"].strftime("%a, %d %b %Y %H:%M:%S +0000")
               if r["posted_at"] else "")
    size = r["size_bytes"] or 0
    cat_id = _category_id(r)
    title = html.escape(r["name"])
    return (
        f"    <item>\n"
        f"      <title>{title}</title>\n"
        f"      <guid isPermaLink=\"false\">{guid}</guid>\n"
        f"      <link>{link}</link>\n"
        f"      <comments>{link}</comments>\n"
        f"      <pubDate>{pubdate}</pubDate>\n"
        f"      <category>{cat_id}</category>\n"
        f"      <size>{size}</size>\n"
        f"      <description>tgarr release via Telegram MTProto</description>\n"
        f"      <enclosure url=\"{link}\" length=\"{size}\" type=\"application/x-nzb\"/>\n"
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
    base_url = os.environ.get("TGARR_BASE_URL", "")  # set in compose if needed
    if t == "caps":
        return Response(CAPS_XML, media_type="application/xml")

    if t == "get":
        return await _handle_grab(id)

    where_parts = ["1=1"]
    params = []

    if q:
        # Split into words, require all to match (ILIKE %word%). Lets users
        # type "our planet" and match release names with any separator
        # (Our.Planet.S01E01..., Our Planet S01E01...).
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
    sql = f"""SELECT id, guid, name, category, season, episode, quality,
                     source, codec, size_bytes, posted_at
              FROM releases
              WHERE {" AND ".join(where_parts)}
              ORDER BY posted_at DESC NULLS LAST
              LIMIT {limit_val} OFFSET {max(0, offset)}"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    items = "\n".join(_item_xml(r, base_url, apikey or "tgarr") for r in rows)
    return Response(
        _feed_envelope(items, f"search t={t} q={q or ''}", base_url),
        media_type="application/xml",
    )


async def _handle_grab(guid: Optional[str]) -> Response:
    """Sonarr is grabbing — queue download + return stub NZB."""
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
@app.get("/api")  # SAB API is sometimes hit at /api directly
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
    if mode == "addurl":
        return await _sab_addurl(name or value or "", nzbname, cat)
    if mode == "addfile":
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
               FROM downloads d
               JOIN releases r ON r.id = d.release_id
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
        "version": "3.7.2",
        "paused": False,
        "noofslots_total": len(slots),
        "noofslots": len(slots),
        "slots": slots,
        "speedlimit": "",
        "speedlimit_abs": "",
        "speed": "0 K",
        "kbpersec": "0",
        "size": "0 MB",
        "sizeleft": "0 MB",
        "mb": "0",
        "mbleft": "0",
        "diskspace1": "1000",
        "diskspace2": "1000",
        "diskspace1_norm": "1 T",
        "diskspace2_norm": "1 T",
        "diskspacetotal1": "2000",
        "diskspacetotal2": "2000",
        "limit": "0",
        "have_warnings": "0",
        "pause_int": "0",
        "loadavg": "",
    }}


async def _sab_history():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT d.id, d.status, d.local_path, d.finished_at,
                      r.name, r.size_bytes, r.guid, d.error_message
               FROM downloads d
               JOIN releases r ON r.id = d.release_id
               WHERE d.status IN ('completed','failed')
               ORDER BY d.finished_at DESC NULLS LAST
               LIMIT 200""")
    slots = []
    for r in rows:
        size_mb = (r["size_bytes"] or 0) / 1048576
        slots.append({
            "nzo_id": f"tgarr-{r['guid']}",
            "name": r["name"],
            "category": "tv",
            "storage": r["local_path"] or "/downloads/tgarr",
            "status": "Completed" if r["status"] == "completed" else "Failed",
            "size": f"{size_mb:.1f} MB",
            "bytes": r["size_bytes"] or 0,
            "completed": int(r["finished_at"].timestamp()) if r["finished_at"] else 0,
            "fail_message": r["error_message"] or "",
            "action_line": "",
            "show_details": "True",
            "script_log": "",
            "report": "",
            "pp": "3",
            "script": "None",
            "url": "",
            "id": str(r["id"]),
        })
    return {"history": {
        "noofslots": len(slots),
        "slots": slots,
        "total_size": "0 B",
        "month_size": "0 B",
        "week_size": "0 B",
        "day_size": "0 B",
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
