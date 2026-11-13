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
import json as _json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import asyncpg
from fastapi import FastAPI, Form, Header, Query, Response, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

import login  # local module
import access  # local module
import screentime  # local module
import contextvars


# ── adult (NSFW) classification ────────────────────────────────────────
# PostgreSQL regexes (used with ~*). Keep the three copies (crawler, api,
# registry) identical. A channel is NSFW when its title matches
# ADULT_TITLE_RX, or when >= 8% of its named files / captions (at least
# 20 of them, at least 5 hits) match ADULT_RX. On live data adult channels
# score 10-90% and ordinary ones 0-3%.
ADULT_RX = '(约炮|外围女|外围小|探花|口交|抽插|内射|无码|無碼|有码|啪啪|做爱|做愛|性爱|性愛|自慰|巨乳|爆乳|美乳|大奶|奶子|福利姬|淫|骚货|骚逼|骚穴|小骚|少妇|人妻|嫩穴|嫩逼|操逼|肉棒|后入|後入|颜射|潮吹|调教|援交|楼凤|包养|裸聊|裸舞|黄片|黄油|成人视频|成人影片|色情|情色|乱伦|亂倫|强奸|迷奸|偷拍|换妻|群交|约啪|反差婊|母狗|国产自拍|麻豆传媒|天美传媒|91大神|porn|xxx|onlyfans|only fans|hentai|brazzers|blowjob|creampie|milf|nsfw|18\\+|uncensored|порно|секс|эротик|хентай|سکس|سكس|پورن|بورن|ポルノ|無修正|야동)'
ADULT_TITLE_RX = '(约炮|外围女|外围小|探花|口交|抽插|内射|无码|無碼|有码|啪啪|做爱|做愛|性爱|性愛|自慰|巨乳|爆乳|美乳|大奶|奶子|福利姬|淫|骚货|骚逼|骚穴|小骚|少妇|人妻|嫩穴|嫩逼|操逼|肉棒|后入|後入|颜射|潮吹|调教|援交|楼凤|包养|裸聊|裸舞|黄片|黄油|成人视频|成人影片|色情|情色|乱伦|亂倫|强奸|迷奸|偷拍|换妻|群交|约啪|反差婊|母狗|国产自拍|麻豆传媒|天美传媒|91大神|porn|xxx|onlyfans|only fans|hentai|brazzers|blowjob|creampie|milf|nsfw|18\\+|uncensored|порно|секс|эротик|хентай|سکس|سكس|پورن|بورن|ポルノ|無修正|야동|反差|熟女|糖心|麻豆|海角|伦理片|91国产|(^|[^a-z])av([^a-z]|$)|adult|erotic)'
ADULT_MIN_FILES = 20
ADULT_MIN_HITS = 5
ADULT_MIN_RATIO = 0.08


# ── request guard: cross-site protection + optional credentials ────────
# tgarr listens on the LAN without a login. Two things follow from that:
#  1. Any web page open in the user's browser could make that browser send
#     requests here (sign out, queue downloads, drive Telegram fetches). Those
#     cross-site requests are refused. Sonarr/Radarr and scripts are not
#     browsers and are unaffected.
#  2. Operators who want real access control can set TGARR_API_KEY (required
#     as apikey= by the Newznab / SABnzbd endpoints the *arrs talk to) and/or
#     TGARR_UI_PASSWORD (HTTP Basic for the web UI and its API). Both are off
#     unless set, so an upgrade never locks anyone out.
_API_KEY = os.environ.get("TGARR_API_KEY", "").strip()
_UI_PASSWORD = os.environ.get("TGARR_UI_PASSWORD", "").strip()
_GUARD_OPEN_PATHS = ("/health", "/favicon.svg", "/favicon.ico")


def _is_arr_path(path: str) -> bool:
    return (path == "/api" or path.startswith("/newznab")
            or path.startswith("/sabnzbd"))


def _cross_site(request) -> bool:
    """True for a request a browser is making on behalf of ANOTHER site."""
    h = request.headers
    sfs = (h.get("sec-fetch-site") or "").lower()
    if sfs:
        if sfs != "cross-site":
            return False          # same-origin / same-site / typed-in URL
        # a plain cross-site link click (top-level navigation, GET) is fine
        return not (request.method == "GET"
                    and (h.get("sec-fetch-mode") or "").lower() == "navigate")
    origin = h.get("origin")
    if not origin or origin == "null":
        return False              # non-browser client
    host = (h.get("x-forwarded-host") or h.get("host") or "").split(",")[0].strip().lower()
    try:
        return urllib.parse.urlsplit(origin).netloc.lower() != host
    except Exception:
        return True


# ── nobody signed in to Telegram ───────────────────────────────────────
# What is already indexed from PUBLIC channels, and files already saved on
# this machine, stay readable: none of that needed the account. Two things do
# not stay open:
#   * anything from a private chat or group (no public @username). It was
#     readable only because the account was a member; with the account gone
#     it is nobody's business. Lists leave it out (see _pub) and the
#     by-id endpoints below answer 404 for it.
#   * anything that acts through the account: follow, unfollow, grab, fetch,
#     settings. The pages hide those buttons; this refuses the requests.
_PRIVATE_ID_RX = re.compile(
    r"^/(?:media|watch|api/thumb|api/photo-preview|api/media-status|api/lyrics|api/fav|"
    r"ebook-pdf|ebook-cover|epub|pdf)/(\d+)(?:/|$)")
_PUBLIC_MSG: dict = {}


_SIGNED_IN = [0.0, False]


def _signed_in() -> bool:
    # asked many times while one page is built (every card, every list):
    # look at the session file at most once a second
    now = time.monotonic()
    if now - _SIGNED_IN[0] > 1.0:
        try:
            _SIGNED_IN[1] = bool(login.session_exists() and not _is_revoked())
        except Exception:
            _SIGNED_IN[1] = False
        _SIGNED_IN[0] = now
    return _SIGNED_IN[1]


_ME_ID = [0.0, None]


def _me_id():
    """The Telegram id of the account signed in here, or None."""
    if not _signed_in():
        return None
    now = time.monotonic()
    if now - _ME_ID[0] > 1.0:
        try:
            _ME_ID[1] = login.session_user_id()
        except Exception:
            _ME_ID[1] = None
        _ME_ID[0] = now
    return _ME_ID[1]


def _pub(alias: str = "c") -> str:
    """SQL to append to a WHERE: what the person at the keyboard may see.
    Nobody signed in: public channels only. Somebody signed in: public
    channels and that account's own private chats - not the private chats
    another account left behind on this install (they are kept for it, see
    PRIVATE_KEEP_DAYS, and are nobody else's business)."""
    uid = _me_id()
    if uid is None:
        return f" AND {alias}.username IS NOT NULL"
    return f" AND ({alias}.username IS NOT NULL OR COALESCE({alias}.account_user_id, {uid}) = {uid})"


def _pub_m(alias: str = "m") -> str:
    """The same for a query on messages that may not join channels at all (a
    page's count and facet queries reuse its filter without the join)."""
    uid = _me_id()
    if uid is None:
        return f" AND {alias}.channel_id IN (SELECT id FROM channels WHERE username IS NOT NULL)"
    return (f" AND {alias}.channel_id NOT IN (SELECT id FROM channels WHERE username IS NULL "
            f"AND COALESCE(account_user_id, {uid}) <> {uid})")


# ════════════════════════════════════════════════════════════════════
# Access PIN and the kids' list
#
# Whoever opened this address used to see everything the owner sees: the
# followed channels, the private photos, the settings. With a PIN set
# (config 'access_pin'), a browser that has not entered it is LOCKED: it gets
# the kids' pages and the items the owner put on the kids' list - nothing
# else. This is decided here, for every request, not by hiding links.
# ════════════════════════════════════════════════════════════════════
_LOCKED = contextvars.ContextVar("tgarr_locked", default=False)
_ACCESS = {"at": 0.0, "pin": None, "secret": None}
_GUESSES = access.Guesses()
_KID_OK: dict = {}                     # message id / thumb file -> (allowed, checked at)
_KID_SQL = """(COALESCE(c.audience, 'sfw') = 'sfw'
               AND (m.id IN (SELECT ref FROM kids_allow WHERE kind = 'message')
                    OR m.channel_id IN (SELECT ref FROM kids_allow WHERE kind = 'channel')))"""
_LOCKED_OPEN = ("/mediacenter", "/kids", "/unlock", "/api/unlock")
# open even when the Media Center itself is closed for the moment (time rules)
_LOCKED_ALWAYS = ("/unlock", "/api/unlock", "/lock", "/mediacenter/rest", "/api/mc/ping", "/mc/ping.js")
_MC = {"at": 0.0, "rules": screentime.clean(None)}
_DEVICES = screentime.Devices()
_MC_SEEN: dict = {}                    # (device, item) -> when it was last noted in the record of use


async def _mc_rules(fresh: bool = False) -> dict:
    now = time.monotonic()
    if (fresh or now - _MC["at"] > 5) and db_pool is not None:
        try:
            async with db_pool.acquire() as conn:
                raw = await conn.fetchval("SELECT value FROM config WHERE key = 'mc_rules'")
            _MC.update(at=now, rules=screentime.clean(_json.loads(raw) if raw else None))
        except Exception:
            pass
    return _MC["rules"]


def _mc_who(request: Request) -> str:
    return request.client.host if request.client else "?"


async def _mc_device(who: str, rules: dict) -> None:
    """A device seen for the first time since the start: pick up what it had
    already used today, so that a restart does not hand the day back."""
    day = screentime.local(time.time(), rules.get("tz", 0))[0]
    if who in _DEVICES.by and _DEVICES.by[who]["day"] == day:
        return
    used = 0
    if db_pool is not None:
        async with db_pool.acquire() as conn:
            used = await conn.fetchval("SELECT seconds FROM mc_usage WHERE day = $1 AND who = $2", day, who) or 0
    _DEVICES.get(who, day)["used"] = int(used)


async def _access_cfg(fresh: bool = False) -> dict:
    now = time.monotonic()
    if (fresh or now - _ACCESS["at"] > 3) and db_pool is not None:
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT key, value FROM config WHERE key IN ('access_pin', 'access_secret')")
            d = {r["key"]: r["value"] for r in rows}
            _ACCESS.update(at=now, pin=d.get("access_pin") or None, secret=d.get("access_secret") or None)
        except Exception:
            pass                       # e.g. the very first start, before the tables exist
    return _ACCESS


async def _is_locked(request: Request) -> bool:
    cfg = await _access_cfg()
    if not cfg["pin"]:
        return False                   # no PIN set: as before, everything is open
    return not access.token_ok(cfg["secret"], request.cookies.get(access.COOKIE))


async def _kid_ok(key, sql: str, arg) -> bool:
    hit = _KID_OK.get(key)
    if hit and time.monotonic() - hit[1] < 30:
        return hit[0]
    ok = False
    if db_pool is not None:
        async with db_pool.acquire() as conn:
            ok = bool(await conn.fetchval(sql, arg))
    if len(_KID_OK) > 20000:
        _KID_OK.clear()
    _KID_OK[key] = (ok, time.monotonic())
    return ok


async def _locked_block(request: Request):
    """What a locked browser may not have. None = let it through."""
    path = request.url.path
    if path in _LOCKED_ALWAYS or path in _GUARD_OPEN_PATHS:
        return None
    # Opening hours, minutes a day, breaks. Decided here as well as in the
    # page, so that closing time cannot be got round by typing an address.
    rules = await _mc_rules()
    if rules["on"]:
        who = _mc_who(request)
        await _mc_device(who, rules)
        if not _DEVICES.check(who, rules, time.time())["ok"]:
            if request.method in ("GET", "HEAD") and "text/html" in (request.headers.get("accept") or ""):
                return RedirectResponse("/mediacenter/rest", status_code=303)
            return JSONResponse({"status": "rest", "message": "the Media Center is closed for now"}, status_code=403)
    if path in _LOCKED_OPEN:
        return None
    if request.method in ("GET", "HEAD"):
        m = _PRIVATE_ID_RX.match(path)
        if m and path.startswith("/watch/") and db_pool is not None:
            # the record of use: which item, on which device, when (once per ten minutes)
            k, now = (_mc_who(request), int(m.group(1))), time.time()
            if now - _MC_SEEN.get(k, 0) > 600:
                if len(_MC_SEEN) > 5000:
                    _MC_SEEN.clear()
                _MC_SEEN[k] = now
                try:
                    async with db_pool.acquire() as conn:
                        await conn.execute("INSERT INTO mc_log (who, msg_id) VALUES ($1, $2)", k[0], k[1])
                except Exception:
                    pass
        if m and await _kid_ok(int(m.group(1)), f"""
                SELECT 1 FROM messages m JOIN channels c ON c.id = m.channel_id
                WHERE m.id = $1 AND {_KID_SQL}""", int(m.group(1))):
            return None
        if path.startswith("/thumbs/") and await _kid_ok(path, f"""
                SELECT 1 FROM messages m JOIN channels c ON c.id = m.channel_id
                WHERE m.thumb_path = $1 AND {_KID_SQL} LIMIT 1""", path[len("/thumbs/"):]):
            return None
        if "text/html" in (request.headers.get("accept") or "") and not path.startswith("/api/"):
            return RedirectResponse("/mediacenter", status_code=303)
    return JSONResponse({"status": "locked", "message": "enter the PIN to do this"}, status_code=403)


async def _signed_out_block(request: Request):
    uid = _me_id()
    path = request.url.path
    if _signed_in() and request.method not in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # the PIN and the kids' list belong to this install, not to the
        # Telegram account: they work whether or not anybody is signed in
        if path.startswith("/api/") and not path.startswith(
                ("/api/login", "/api/unlock", "/api/lock", "/api/kids/", "/api/usenet/")):
            return JSONResponse({"status": "error", "message": "sign in to Telegram first"},
                                status_code=401)
        return None
    m = _PRIVATE_ID_RX.match(path)
    if m and db_pool is not None:
        mid = int(m.group(1))
        ok = _PUBLIC_MSG.get((mid, uid))
        if ok is None:
            async with db_pool.acquire() as conn:
                # public, or a private chat of the account signed in right now
                ok = bool(await conn.fetchval(
                    """SELECT c.username IS NOT NULL
                              OR ($2::bigint IS NOT NULL AND COALESCE(c.account_user_id, $2) = $2)
                       FROM messages m JOIN channels c ON c.id = m.channel_id WHERE m.id = $1""", mid, uid))
            if len(_PUBLIC_MSG) > 20000:
                _PUBLIC_MSG.clear()
            _PUBLIC_MSG[(mid, uid)] = ok
        if not ok:
            return PlainTextResponse("not found", status_code=404)
    return None


def _ct_equal(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


async def _request_guard(request: Request, call_next):
    path = request.url.path
    if path in _GUARD_OPEN_PATHS:
        return await call_next(request)
    if _cross_site(request):
        return PlainTextResponse("cross-site request refused", status_code=403)
    if _is_arr_path(path):
        if _API_KEY:
            given = (request.query_params.get("apikey")
                     or request.headers.get("x-api-key") or "")
            if not _ct_equal(given, _API_KEY):
                return Response(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<error code="100" description="Incorrect user credentials"/>',
                    media_type="application/xml", status_code=401)
    elif _UI_PASSWORD:
        import base64 as _b64
        ok = False
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("basic "):
            try:
                _user, _, pw = _b64.b64decode(auth[6:]).decode("utf-8", "replace").partition(":")
                ok = _ct_equal(pw, _UI_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="tgarr"'})
    # Sonarr, Radarr and the like come with their own key (checked above) and
    # carry no browser cookie: the PIN is not for them
    locked = (not _is_arr_path(path)) and await _is_locked(request)
    _LOCKED.set(locked)
    if locked:
        blocked = await _locked_block(request)
        if blocked is not None:
            return blocked
    blocked = await _signed_out_block(request)
    if blocked is not None:
        return blocked
    return await call_next(request)


def _base_url(request) -> str:
    """Public base URL for links we hand to Sonarr/Radarr/browsers.

    Derived from the request itself (honouring reverse-proxy headers) so the
    links always point back at whatever address the caller reached us on.
    TGARR_BASE_URL is only a fallback for requests that carry no Host header:
    a stale or unset env value used to produce dead or relative links, which
    made every grab fail.
    """
    try:
        h = request.headers
        host = (h.get("x-forwarded-host") or h.get("host") or "").split(",")[0].strip()
        if host and re.fullmatch(r"[A-Za-z0-9._\-\[\]:]+", host):
            proto = (h.get("x-forwarded-proto") or request.url.scheme or "http")
            proto = proto.split(",")[0].strip().lower()
            if proto not in ("http", "https"):
                proto = "http"
            return f"{proto}://{host}"
    except Exception:
        pass
    return (os.environ.get("TGARR_BASE_URL") or "").rstrip("/")


_GUID_RX = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


def _js_str(v) -> str:
    """Serialize a Python value as a JS literal safe inside an inline <script>."""
    return (_json.dumps("" if v is None else str(v))
            .replace("</", "<\\/").replace("<!--", "<\\!--")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))

import metadata as md  # local module
import usenet  # local module

DB_DSN = os.environ["DB_DSN"]
MEILI_URL = os.environ.get("MEILI_URL", "http://meili:7700")
MEILI_KEY = os.environ.get("MEILI_MASTER_KEY", "")
TGARR_VERSION = "0.5.7"

# /app/session/.revoked marker — written by crawler on AuthKeyUnregistered /
# SessionRevoked / UserDeactivated, deleted by QR re-login success. While
# present the UI surfaces "re-login required" and authed=False regardless
# of session file or channel count (channels persist in DB after revoke).
REVOKED_MARKER = "/app/session/.revoked"


def _is_revoked() -> bool:
    return os.path.exists(REVOKED_MARKER)


def _clear_revoked_marker() -> None:
    try:
        if os.path.exists(REVOKED_MARKER):
            os.remove(REVOKED_MARKER)
            logging.getLogger("tgarr").info("[revoked] cleared %s", REVOKED_MARKER)
    except Exception as e:
        logging.getLogger("tgarr").warning("[revoked] clear %s: %s", REVOKED_MARKER, e)


async def meili_search(index: str, q: str, *, limit: int = 60,
                       offset: int = 0, filter_: Optional[str] = None,
                       sort: Optional[list] = None) -> dict:
    """Async wrapper around Meili HTTP search. Returns full response dict.
    Empty/missing key → returns {hits:[],estimatedTotalHits:0} so callers degrade gracefully."""
    if not MEILI_KEY:
        return {"hits": [], "estimatedTotalHits": 0}
    body = {"q": q or "", "limit": limit, "offset": offset}
    if filter_:
        body["filter"] = filter_
    if sort:
        body["sort"] = sort

    def _call():
        req = urllib.request.Request(
            f"{MEILI_URL}/indexes/{index}/search",
            data=_json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {MEILI_KEY}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return _json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            logging.getLogger("tgarr.meili").warning("meili search failed: %s", e)
            return {"hits": [], "estimatedTotalHits": 0}

    return await asyncio.to_thread(_call)
ANY_API_KEY_ACCEPTED = True

app = FastAPI(title="tgarr", version=TGARR_VERSION)
app.middleware("http")(_request_guard)   # defined above, next to its helpers
# The module-level logger. It was missing: the cache cleanup removed files and
# then died on its own log line (NameError), once, for good - and the
# "clear everything" button answered 500 after doing its work.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("tgarr")

db_pool: Optional[asyncpg.Pool] = None


async def _ebook_queue_restore() -> None:
    """On boot, scan /downloads/library for .queue markers and re-queue.
    Survives container restart so user's request from hours ago still completes.
    """
    import asyncio, glob
    log = logging.getLogger("tgarr.ebook_restore")
    await asyncio.sleep(20)  # let DB pool + other startup tasks settle
    # Best-effort scan; if /downloads is unavailable just exit
    if not os.path.isdir("/downloads/library"):
        return
    markers = glob.glob("/downloads/library/*.queue")
    if not markers:
        log.info("[ebook-restore] no queued conversions to restore")
        return
    log.info("[ebook-restore] found %d queue markers", len(markers))
    for marker in markers:
        try:
            with open(marker) as f:
                mid = int(f.read().strip())
            src = marker[:-len(".queue")]
            pdf = src + ".pdf"
            if src.lower().endswith(".epub"):
                os.unlink(marker)       # EPUB is read directly now: nothing to convert
                continue
            # If PDF already exists + newer than src, cleanup stale marker
            if os.path.exists(pdf) and os.path.getmtime(pdf) >= os.path.getmtime(src):
                os.unlink(marker)
                continue
            if not os.path.exists(src):
                os.unlink(marker)
                continue
            # Re-queue
            _t_task = asyncio.create_task(_ebook_to_pdf(src, pdf, msg_id=mid))
            _EBOOK_BG_TASKS.add(_t_task)
            _t_task.add_done_callback(_EBOOK_BG_TASKS.discard)
            log.info("[ebook-restore] re-queued msg_id=%s src=%s", mid, src)
        except Exception as e:
            log.warning("[ebook-restore] skip %s: %s", marker, e)


@app.on_event("startup")
async def _startup():
    global db_pool
    # The database may still be starting: wait for it instead of crash-looping.
    for _i in range(60):
        try:
            db_pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=8)
            break
        except (OSError, asyncpg.PostgresError) as e:
            if _i == 59:
                raise
            await asyncio.sleep(2)
    # The base tables. They used to come only from a file mounted into the
    # database container (./schema) - which an install that fetched just the
    # compose file does not have, and nothing else created them. They are
    # part of the image now and (re)applied at every start; every statement
    # is IF NOT EXISTS.
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql"),
                  encoding="utf-8") as _f:
            _base = _f.read()
        async with db_pool.acquire() as conn:
            await conn.execute(_base)
    except FileNotFoundError:
        pass
    # The migration takes brief exclusive locks; against a busy crawler it can
    # lose a deadlock. Retry rather than failing startup.
    for _attempt in range(1, 7):
        try:
            await _migrate_schema()
            break
        except (asyncpg.exceptions.DeadlockDetectedError,
                asyncpg.exceptions.LockNotAvailableError) as e:
            if _attempt == 6:
                raise
            logging.getLogger("tgarr").warning(
                "schema migration lock conflict (%s) — retry %d/5",
                type(e).__name__, _attempt)
            await asyncio.sleep(2 * _attempt)
    # Tell the crawler the schema is in place (it waits for this before it
    # touches the tables - on a first start the two come up together).
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO config (key, value) VALUES ('schema_ready', $1)
               ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()""", TGARR_VERSION)
    # Restore self-user from the session volume so the sidebar shows the real
    # username (not @anonymous) immediately after a restart, before any get_me.
    login.ensure_user_info()
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
    asyncio.create_task(_audio_metadata_worker())
    asyncio.create_task(_stats_snapshot_worker())
    asyncio.create_task(_ebook_queue_restore())
    asyncio.create_task(_arr_alias_sync_worker())
    asyncio.create_task(_usenet_scan_worker())
    asyncio.create_task(_private_keep_worker())
    asyncio.create_task(_cache_gc_worker())
    asyncio.create_task(_update_check_worker())


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
            ALTER TABLE discovered ADD COLUMN IF NOT EXISTS release_files INTEGER;
            ALTER TABLE discovered ADD COLUMN IF NOT EXISTS video_files INTEGER;
            ALTER TABLE discovered ADD COLUMN IF NOT EXISTS book_files INTEGER;
            ALTER TABLE discovered ADD COLUMN IF NOT EXISTS audio_files INTEGER;
            ALTER TABLE discovered ADD COLUMN IF NOT EXISTS last_file_at TIMESTAMPTZ;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS local_path TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_title TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_performer TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_duration_sec INTEGER;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS file_dc INTEGER;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS preview_path TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS thumb_accessed_at TIMESTAMPTZ;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS preview_accessed_at TIMESTAMPTZ;
            CREATE INDEX IF NOT EXISTS idx_messages_thumb_accessed ON messages (thumb_accessed_at) WHERE thumb_path IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_messages_preview_accessed ON messages (preview_accessed_at) WHERE preview_path IS NOT NULL;
            -- One-time grace: give already-cached thumbs/previews a fresh clock so
            -- the 5h TTL GC doesn't wipe them all on first run (idempotent: only NULLs).
            UPDATE messages SET thumb_accessed_at = NOW()
              WHERE thumb_accessed_at IS NULL AND thumb_path IS NOT NULL
                AND thumb_path NOT IN ('__user_queued__','__failed__','__deleted__');
            UPDATE messages SET preview_accessed_at = NOW()
              WHERE preview_accessed_at IS NULL AND preview_path IS NOT NULL
                AND preview_path NOT IN ('__user_queued__','__failed__','__deleted__');
            CREATE TABLE IF NOT EXISTS worker_status (
              worker TEXT PRIMARY KEY,
              last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              last_action TEXT,
              iter_count BIGINT NOT NULL DEFAULT 0,
              error_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              last_error_at TIMESTAMPTZ
            );
            ALTER TABLE worker_status ADD COLUMN IF NOT EXISTS quiet_for_s INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS unfollowed_at TIMESTAMPTZ;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS handle_known BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS web_preview BOOLEAN;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS web_cursor BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS web_done BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS central_pulled_at TIMESTAMPTZ;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS via_web BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS deep_backfilled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS deep_oldest_tg_id BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS deep_last_run_at TIMESTAMPTZ;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS auto_joined BOOLEAN;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS deep_total_pulled BIGINT NOT NULL DEFAULT 0;
            CREATE INDEX IF NOT EXISTS idx_channels_deep_backfilled ON channels (deep_backfilled);
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_msgs BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_photos BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_videos BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_audio BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_documents BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_counts_refreshed_at TIMESTAMPTZ;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS content_category TEXT;
            -- content_category values:
            --   NULL or 'archival' = full backfill (movie/tv/ebook/music)
            --   'news' = 90d cutoff
            --   'sports' = 30d cutoff
            --   'chat' = 30d cutoff (live discussion)
            --   'mixed' = full backfill (default safe)
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS contributed_at TIMESTAMPTZ;
            CREATE INDEX IF NOT EXISTS idx_messages_pending_contrib
                ON messages (id)
                WHERE file_unique_id IS NOT NULL AND contributed_at IS NULL;

            -- A. Auto-grab pipeline (Sonarr/Radarr feature parity)
            CREATE TABLE IF NOT EXISTS quality_profile (
              id SERIAL PRIMARY KEY,
              name TEXT NOT NULL UNIQUE,
              preferred_resolutions TEXT[],
              preferred_codecs TEXT[],
              preferred_sources TEXT[],
              max_size_bytes BIGINT,
              min_size_bytes BIGINT DEFAULT 100000000,
              prefer_hdr BOOLEAN DEFAULT FALSE,
              language_preference TEXT[]
            );
            -- seed default profile
            INSERT INTO quality_profile (name, preferred_resolutions, preferred_codecs,
                preferred_sources, max_size_bytes, min_size_bytes)
              VALUES ('default-1080p',
                      ARRAY['2160p','1080p','720p'],
                      ARRAY['x265','x264'],
                      ARRAY['BluRay','WEBDL','WEBRip','HDTV'],
                      8000000000, 200000000)
              ON CONFLICT (name) DO NOTHING;

            CREATE TABLE IF NOT EXISTS monitored_series (
              id SERIAL PRIMARY KEY,
              tmdb_tv_id INT UNIQUE,
              series_title TEXT NOT NULL,
              quality_profile_id INT REFERENCES quality_profile(id),
              monitored_from_season INT DEFAULT 1,
              jellyfin_library_path TEXT,
              status TEXT NOT NULL DEFAULT 'active',
              added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_monitored_series_title
              ON monitored_series (LOWER(series_title));

            CREATE TABLE IF NOT EXISTS monitored_movies (
              id SERIAL PRIMARY KEY,
              tmdb_movie_id INT UNIQUE,
              title TEXT NOT NULL,
              year INT,
              quality_profile_id INT REFERENCES quality_profile(id),
              jellyfin_library_path TEXT,
              status TEXT NOT NULL DEFAULT 'wanted',
              grabbed_release_id BIGINT REFERENCES releases(id),
              added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_monitored_movies_title
              ON monitored_movies (LOWER(title), year);

            CREATE TABLE IF NOT EXISTS auto_grab_log (
              id BIGSERIAL PRIMARY KEY,
              release_id BIGINT REFERENCES releases(id) ON DELETE CASCADE,
              monitored_kind TEXT NOT NULL,
              monitored_id INT NOT NULL,
              action TEXT NOT NULL,
              reason TEXT,
              decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_auto_grab_log_recent
              ON auto_grab_log (decided_at DESC);

            -- grab_count for B (composite ranking) — derived from downloads table
            ALTER TABLE releases ADD COLUMN IF NOT EXISTS grab_count INT NOT NULL DEFAULT 0;
            ALTER TABLE releases ADD COLUMN IF NOT EXISTS part SMALLINT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS local_accessed_at TIMESTAMPTZ;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_lyrics TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS fetch_wanted_at TIMESTAMPTZ;
            CREATE TABLE IF NOT EXISTS favorites (
              msg_id BIGINT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
              added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS detected_lang TEXT;
            CREATE INDEX IF NOT EXISTS idx_messages_lang ON messages (detected_lang) WHERE detected_lang IS NOT NULL;
            -- Per-download live progress (populated by Pyrogram callback in
            -- download_worker). Refreshes every ~5s so UI can show MB/s + ETA
            -- without per-byte DB hammering.
            ALTER TABLE downloads ADD COLUMN IF NOT EXISTS bytes_done BIGINT NOT NULL DEFAULT 0;
            ALTER TABLE downloads ADD COLUMN IF NOT EXISTS speed_kbps INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE downloads ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMPTZ;
            -- category the *arr asked for on add (tv / movies / …); echoed back
            -- in queue + history so each app only sees its own jobs.
            ALTER TABLE downloads ADD COLUMN IF NOT EXISTS sab_cat TEXT;
            -- running time + frame size as reported by Telegram for videos
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS duration_sec INTEGER;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS width INTEGER;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS height INTEGER;
            -- 450px gallery preview (~76KB JPEG) — bigger than 12KB thumb for
            -- lightbox/slideshow. Same lazy-fetch model: '__user_queued__'
            -- marker triggers preview_downloader on next /api/photo-preview hit.
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS preview_path TEXT;
            CREATE INDEX IF NOT EXISTS idx_messages_preview ON messages (preview_path) WHERE preview_path IS NOT NULL;
            -- Per-account channel ownership: which TG account discovered/has-access
            -- to this channel. Crawler workers filter by current account so an
            -- account switch doesn't try to access channels it can't reach.
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS account_user_id BIGINT;
            -- set when the owning account signs out: kept, hidden, until it returns
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS held_at TIMESTAMPTZ;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS is_joined BOOLEAN NOT NULL DEFAULT FALSE;
            CREATE INDEX IF NOT EXISTS idx_channels_account ON channels (account_user_id);
            -- NULL account_user_id = "not yet claimed by an account"; the crawler
            -- treats it as visible to whichever account is logged in.
            -- FK-lookup indexes: releases.primary_msg_id is hit by every message
            -- delete + several NOT IN subqueries; downloads by the queue workers.
            CREATE INDEX IF NOT EXISTS idx_releases_primary_msg ON releases (primary_msg_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_release ON downloads (release_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads (status);
            -- One-time: metadata used to be taken from the first search result,
            -- so releases carry posters/titles of unrelated things ("1080p",
            -- a university, an actress). Clear every attached match and let
            -- the worker look them up again under the stricter rules. The
            -- +0.3 score boost a "hit" earned is taken back too.
            DO $recheck$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM config WHERE key = 'meta_recheck_0_5_5') THEN
                UPDATE releases
                   SET metadata_source = NULL, metadata_lookup_at = NULL,
                       poster_url = NULL, canonical_title = NULL, overview = NULL,
                       parse_score = GREATEST(COALESCE(parse_score, 0) - 0.3, 0.30)
                 WHERE metadata_source IN ('tmdb', 'wikipedia');
                INSERT INTO config (key, value) VALUES ('meta_recheck_0_5_5', 'done')
                  ON CONFLICT (key) DO NOTHING;
              END IF;
            END $recheck$;
            -- 0.5.5 judged translated titles too strictly (a foreign film under
            -- its Chinese / Russian / ... title has only English titles in
            -- TMDB, so nothing "matched"). Give every 'unmatched' release one
            -- more look under the 0.5.6 rules. No score is touched.
            DO $recheck2$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM config WHERE key = 'meta_recheck_0_5_6') THEN
                UPDATE releases SET metadata_source = NULL, metadata_lookup_at = NULL
                 WHERE metadata_source = 'unmatched';
                INSERT INTO config (key, value) VALUES ('meta_recheck_0_5_6', 'done')
                  ON CONFLICT (key) DO NOTHING;
              END IF;
            END $recheck2$;
            -- One release per file. file_unique_id is Telegram's identity for
            -- the bytes: the same file reposted in a channel, or forwarded to
            -- five others, is still one file. Every copy used to become its
            -- own release (a quarter of all releases on a long-running
            -- install; one file had 228).
            CREATE INDEX IF NOT EXISTS idx_messages_fuid ON messages (file_unique_id)
              WHERE file_unique_id IS NOT NULL;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS dead_at TIMESTAMPTZ;
            DO $dedup$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM config WHERE key = 'dedup_releases_0_5_6') THEN
                -- Keep the copy whose source is the most useful: one that has
                -- download history, then verified metadata, then a channel the
                -- account is still in / follows / that is still alive, then
                -- the better parse, then the newer post.
                CREATE TEMP TABLE _dup_rel ON COMMIT DROP AS
                  SELECT id FROM (
                    SELECT r.id,
                           row_number() OVER (
                             PARTITION BY m.file_unique_id
                             ORDER BY (EXISTS (SELECT 1 FROM downloads d WHERE d.release_id = r.id)) DESC,
                                      (r.metadata_source IN ('tmdb', 'wikidata')) DESC NULLS LAST,
                                      COALESCE(c.is_joined, FALSE) DESC,
                                      COALESCE(c.subscribed, FALSE) DESC,
                                      (c.dead_at IS NULL) DESC,
                                      r.parse_score DESC NULLS LAST,
                                      r.posted_at DESC NULLS LAST,
                                      r.id) AS rn
                    FROM releases r
                    JOIN messages m ON m.id = r.primary_msg_id
                    JOIN channels c ON c.id = m.channel_id
                    WHERE m.file_unique_id IS NOT NULL) x
                  WHERE rn > 1
                    AND NOT EXISTS (SELECT 1 FROM downloads d WHERE d.release_id = x.id);
                UPDATE monitored_movies SET grabbed_release_id = NULL
                 WHERE grabbed_release_id IN (SELECT id FROM _dup_rel);
                DELETE FROM releases WHERE id IN (SELECT id FROM _dup_rel);
                INSERT INTO config (key, value) VALUES ('dedup_releases_0_5_6', 'done')
                  ON CONFLICT (key) DO NOTHING;
              END IF;
            END $dedup$;
            CREATE TABLE IF NOT EXISTS seed_candidates (
              username TEXT PRIMARY KEY,
              title TEXT,
              category TEXT,
              region TEXT,
              language TEXT,
              audience_hint TEXT,
              tags TEXT[],
              source TEXT,
              added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              validated_at TIMESTAMPTZ,
              validation_status TEXT,
              invite_link TEXT,
              validation_attempts INTEGER NOT NULL DEFAULT 0,
              validation_error TEXT
            );
            ALTER TABLE seed_candidates ADD COLUMN IF NOT EXISTS contributed_at TIMESTAMPTZ;
            CREATE INDEX IF NOT EXISTS idx_seed_cand_pending_contrib
                ON seed_candidates (added_at DESC)
                WHERE source IN ('caption-mention','caption-invite') AND contributed_at IS NULL;
            -- Series alias table: translates non-English release names from
            -- tgarr's ingest to the canonical English title that Sonarr/Radarr
            -- know. Populated from Sonarr/Radarr alternateTitles + manual.
            -- Universal across languages — works for any non-English title.
            CREATE TABLE IF NOT EXISTS series_aliases (
              alias_text TEXT PRIMARY KEY,         -- e.g. "舌尖上的中国第三季", "東京喰種", "오징어 게임"
              canonical_title TEXT NOT NULL,        -- e.g. "A Bite of China", "Tokyo Ghoul"
              season INT,                           -- NULL if alias is series-wide; else season # baked into alias
              kind TEXT NOT NULL DEFAULT 'tv',      -- 'tv' or 'movie'
              source TEXT NOT NULL DEFAULT 'manual',-- 'manual' | 'sonarr' | 'radarr' | 'tmdb'
              external_id BIGINT,                   -- tvdbId / tmdbId for traceability
              added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_alias_canon ON series_aliases (canonical_title);
            -- Which series have been asked about (other-language names by
            -- TVDB number), so each is asked once, not every hour.
            -- What a browser that has not entered the PIN may see: whole
            -- channels (kind 'channel', ref = channels.id) and single items
            -- (kind 'message', ref = messages.id).
            -- Usenet: one small card per named release (the NZB is rebuilt
            -- when asked for), and how far each newsgroup has been read.
            CREATE TABLE IF NOT EXISTS usenet_cards (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, size BIGINT, grp TEXT NOT NULL,
              poster TEXT, posted_from BIGINT, posted_to BIGINT, files INT, files_expected INT,
              segments INT, segments_expected INT, complete BOOLEAN, first_message_id TEXT, key TEXT,
              art_lo BIGINT, art_hi BIGINT, alive REAL, added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_usenet_cards_posted ON usenet_cards (posted_from DESC);
            CREATE TABLE IF NOT EXISTS usenet_progress (
              grp TEXT PRIMARY KEY, first_article BIGINT, last_article BIGINT, next_article BIGINT,
              headers BIGINT NOT NULL DEFAULT 0, cards BIGINT NOT NULL DEFAULT 0, bytes BIGINT NOT NULL DEFAULT 0,
              enabled BOOLEAN NOT NULL DEFAULT TRUE, note TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            -- Media Center: seconds used per device per day (day = days since
            -- 1970 in the owner's time zone), and which items were opened.
            CREATE TABLE IF NOT EXISTS mc_usage (
              day INT NOT NULL, who TEXT NOT NULL, seconds INT NOT NULL DEFAULT 0, PRIMARY KEY (day, who)
            );
            CREATE TABLE IF NOT EXISTS mc_log (
              at TIMESTAMPTZ NOT NULL DEFAULT NOW(), who TEXT NOT NULL, msg_id BIGINT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mc_log_at ON mc_log (at DESC);
            CREATE TABLE IF NOT EXISTS kids_allow (
              kind TEXT NOT NULL,
              ref BIGINT NOT NULL,
              added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              PRIMARY KEY (kind, ref)
            );
            CREATE TABLE IF NOT EXISTS series_alias_lookups (
              tvdb_id BIGINT PRIMARY KEY,
              looked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              names INT NOT NULL DEFAULT 0,
              source TEXT
            );
            CREATE TABLE IF NOT EXISTS stats_history (
              snapshot_at TIMESTAMPTZ NOT NULL,
              metric TEXT NOT NULL,
              value BIGINT NOT NULL,
              PRIMARY KEY (snapshot_at, metric)
            );
            CREATE INDEX IF NOT EXISTS idx_stats_hist_metric_time
                ON stats_history (metric, snapshot_at DESC);
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_canonical_title TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_album TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_year INTEGER;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_mbid TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_release_mbid TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_cover_url TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_metadata_source TEXT;
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS audio_metadata_lookup_at TIMESTAMPTZ;
            CREATE INDEX IF NOT EXISTS idx_messages_media_type ON messages (media_type);
            CREATE INDEX IF NOT EXISTS idx_messages_thumb ON messages (thumb_path) WHERE thumb_path IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_messages_md5 ON messages (thumb_md5) WHERE thumb_md5 IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_messages_local ON messages (local_path) WHERE local_path IS NOT NULL;
        """)


async def _audio_metadata_worker():
    """Enrich audio messages with MusicBrainz + Cover Art Archive metadata.
    Polite rate (1.1s/req) matches MB acceptable use policy.
    """
    import urllib.request, urllib.parse, json
    UA = f"tgarr/{TGARR_VERSION} (+https://tgarr.me)"
    wlog = logging.getLogger("tgarr.audio_meta")
    wlog.info("audio metadata worker started")
    await asyncio.sleep(45)  # let other startup tasks settle
    while True:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT id, audio_title, audio_performer, file_name
                    FROM messages
                    WHERE media_type='audio'
                      AND audio_metadata_source IS NULL
                      AND (audio_title IS NOT NULL OR file_name IS NOT NULL)
                    ORDER BY id DESC
                    LIMIT 1
                """)
            if not row:
                await asyncio.sleep(120)
                continue

            raw_title = row["audio_title"] or (
                row["file_name"].rsplit(".", 1)[0] if row["file_name"] else "")
            raw_title = raw_title.strip()
            artist = (row["audio_performer"] or "").strip()
            if not raw_title:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE messages SET audio_metadata_source='none', "
                        "audio_metadata_lookup_at=NOW() WHERE id=$1", row["id"])
                continue

            q_parts = [f'recording:"{raw_title}"']
            if artist:
                q_parts.append(f'artist:"{artist}"')
            q = " AND ".join(q_parts)
            url = ("https://musicbrainz.org/ws/2/recording/?fmt=json&limit=1&query="
                   + urllib.parse.quote(q))

            rec = None
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA, "Accept": "application/json"})
                body = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(req, timeout=15).read())
                data = json.loads(body.decode())
                if data.get("recordings"):
                    rec = data["recordings"][0]
            except Exception as e:
                wlog.warning("[audio-meta] MB query failed for id=%s: %s",
                             row["id"], e)

            canonical = album = year = mbid = release_mbid = cover_url = None
            if rec:
                mbid = rec.get("id")
                canonical = rec.get("title")
                releases = rec.get("releases") or []
                if releases:
                    r = releases[0]
                    album = r.get("title")
                    if r.get("release-group"):
                        release_mbid = r["release-group"].get("id")
                    date = r.get("date") or ""
                    if date[:4].isdigit():
                        year = int(date[:4])
                if release_mbid:
                    cover_url = f"https://coverartarchive.org/release-group/{release_mbid}/front-250"

            async with db_pool.acquire() as conn:
                await conn.execute("""
                    UPDATE messages SET
                        audio_canonical_title=$1, audio_album=$2, audio_year=$3,
                        audio_mbid=$4, audio_release_mbid=$5, audio_cover_url=$6,
                        audio_metadata_source=$7, audio_metadata_lookup_at=NOW()
                    WHERE id=$8
                """, canonical, album, year, mbid, release_mbid, cover_url,
                    "musicbrainz" if rec else "none", row["id"])
            if rec:
                wlog.info("[audio-meta] id=%s → %s · %s · %s",
                          row["id"], canonical, album or "-", year or "-")
            await asyncio.sleep(1.1)
        except Exception as e:
            wlog.exception("[audio-meta] outer: %s", e)
            await asyncio.sleep(30)


async def _stats_snapshot_worker():
    """Hourly snapshot of dashboard metrics into stats_history.

    On first run, also backfills historical messages-by-day from posted_at
    so the line chart shows real history immediately (not 1 dot).
    """
    import asyncio
    log = logging.getLogger("tgarr.stats_snap")
    METRICS_SQL = {
        "channels":  "SELECT count(*) FROM channels",
        "messages":  "SELECT count(*) FROM messages",
        "releases":  "SELECT count(*) FROM releases",
        "videos":    "SELECT count(*) FROM messages WHERE media_type='video'",
        "photos":    "SELECT count(*) FROM messages WHERE media_type='photo'",
        "audio":     "SELECT count(*) FROM messages WHERE media_type='audio'",
        "books":     ("SELECT count(*) FROM messages WHERE media_type='document'"
                      " AND file_name ~* '\\.(pdf|epub|mobi|azw3?|djvu|fb2|cbr|cbz|lit|txt)$'"),
        "completed": "SELECT count(*) FROM downloads WHERE status='completed'",
    }
    await asyncio.sleep(60)  # let startup settle
    # Backfill messages history from posted_at — one-shot, only if empty
    async with db_pool.acquire() as conn:
        has_hist = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM stats_history WHERE metric='messages')")
        if not has_hist:
            log.info("[stats] backfilling messages history from posted_at")
            try:
                await conn.execute("""
                    INSERT INTO stats_history (snapshot_at, metric, value)
                    SELECT date_trunc('day', posted_at) + INTERVAL '23 hours 59 minutes',
                           'messages',
                           sum(count(*)) OVER (ORDER BY date_trunc('day', posted_at))
                    FROM messages
                    WHERE posted_at IS NOT NULL
                      AND posted_at >= NOW() - INTERVAL '90 days'
                    GROUP BY date_trunc('day', posted_at)
                    ON CONFLICT DO NOTHING
                """)
            except Exception as e:
                log.warning("[stats] backfill failed: %s", e)
    while True:
        try:
            async with db_pool.acquire() as conn:
                for metric, sql in METRICS_SQL.items():
                    try:
                        val = await conn.fetchval(sql)
                        await conn.execute(
                            "INSERT INTO stats_history (snapshot_at, metric, value) "
                            "VALUES (date_trunc('hour', NOW()), $1, $2) "
                            "ON CONFLICT (snapshot_at, metric) DO UPDATE SET value = EXCLUDED.value",
                            metric, val or 0)
                    except Exception as e:
                        log.warning("[stats] %s failed: %s", metric, e)
            log.info("[stats] snapshot complete")
        except Exception:
            log.exception("[stats] outer loop")
        # Sleep 1h
        await asyncio.sleep(3600)


async def _metadata_worker():
    """Continuously enrich releases with TMDB (if key) and Wikidata metadata.
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
                    SELECT id, category, parse_score,
                           COALESCE(NULLIF(series_title,''), NULLIF(movie_title,''), name) AS title,
                           movie_year
                    FROM releases
                    WHERE metadata_source IS NULL
                      AND (metadata_lookup_at IS NULL
                           OR metadata_lookup_at < NOW() - INTERVAL '6 hours')
                    ORDER BY posted_at DESC NULLS LAST
                    LIMIT 1
                """)
            if not row:
                await asyncio.sleep(30)
                continue
            try:
                result = await md.lookup(
                    row["category"] or "movie",
                    row["title"] or "",
                    row["movie_year"],
                    tmdb_key=tmdb_key,
                )
            except md.LookupFailed as e:
                # Source unreachable/errored. This says NOTHING about whether
                # the release is real — leave it untouched (metadata_source
                # stays NULL), park it for 6h, and back off so an outage can't
                # chew through the table.
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE releases SET metadata_lookup_at=NOW() WHERE id=$1",
                        row["id"])
                wlog.warning("[meta] lookup failed id=%s (%s) — retry in 6h",
                             row["id"], e)
                await asyncio.sleep(60)
                continue
            current_score = float(row["parse_score"] or 0)
            async with db_pool.acquire() as conn:
                if result is md.UNMATCHED:
                    # The sources answered, but not with this title (or the
                    # "title" is just "1080p"). Attach nothing and judge
                    # nothing: no poster, no score change, no delete.
                    await conn.execute("""
                        UPDATE releases SET metadata_source='unmatched',
                          metadata_lookup_at=NOW()
                        WHERE id=$1
                    """, row["id"])
                elif result:
                    # TMDB hit — boost parse_score (this IS a real movie/show)
                    new_score = min(1.0, current_score + 0.3)
                    await conn.execute("""
                        UPDATE releases SET
                          poster_url=$1, canonical_title=$2, overview=$3,
                          metadata_source=$4, metadata_lookup_at=NOW(),
                          parse_score=$5
                        WHERE id=$6
                    """, result.get("poster_url"), result.get("canonical_title"),
                         result.get("overview"), result.get("source"),
                         new_score, row["id"])
                elif re.search(r"[^\x00-\u024f]", row["title"] or ""):
                    # Non-Latin-script title (CJK, Cyrillic, Arabic, Indic, …)
                    # + TMDB miss. TMDB has poor coverage of Chinese/
                    # Japanese/Korean indie content (docs, regional films), so a
                    # miss is NOT evidence the release is fake — it's just not in
                    # TMDB. KEEP it (no metadata) instead of deleting; otherwise
                    # legit CJK doc/movie releases (e.g. 纪录片爱好者) get wiped.
                    await conn.execute("""
                        UPDATE releases SET metadata_source='miss-cjk',
                          metadata_lookup_at=NOW()
                        WHERE id=$1
                    """, row["id"])
                else:
                    # TMDB miss — title doesn't match any known movie/show.
                    # Drop parse_score. If < 0.30 threshold → delete the release
                    # (not a real release, just looked release-shaped).
                    new_score = max(0.0, current_score - 0.3)
                    has_dl = await conn.fetchval(
                        "SELECT 1 FROM downloads WHERE release_id=$1 LIMIT 1",
                        row["id"])
                    if new_score < 0.30 and not has_dl:
                        await conn.execute(
                            "DELETE FROM releases WHERE id=$1", row["id"])
                        wlog.info("[meta] TMDB miss → deleted weak release id=%s '%s'",
                                  row["id"], row["title"])
                    else:
                        await conn.execute("""
                            UPDATE releases SET metadata_source='miss',
                              metadata_lookup_at=NOW(),
                              parse_score=$1
                            WHERE id=$2
                        """, new_score, row["id"])
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
from fastapi.responses import FileResponse, StreamingResponse
THUMBS_DIR = "/downloads/thumbs"
PREVIEWS_DIR = "/downloads/previews"
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


_TINY_TRANSPARENT_GIF = bytes.fromhex(
    "47494638396101000100800000ffffff00000021f90401000000002c00000000"
    "010001000002024401003b"
)


async def _touch_thumb(msg_id: int) -> None:
    """Bump thumb_accessed_at (throttled to ≤1 write/30min/thumb) so the TTL
    cache GC keeps recently-viewed thumbs and evicts the rest."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET thumb_accessed_at = NOW() "
                "WHERE id = $1 AND (thumb_accessed_at IS NULL "
                "OR thumb_accessed_at < NOW() - INTERVAL '30 minutes')", msg_id)
    except Exception:
        pass


async def _touch_preview(msg_id: int) -> None:
    """Bump preview_accessed_at (throttled) so the TTL GC keeps recently-viewed
    big-image previews and evicts the rest."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET preview_accessed_at = NOW() "
                "WHERE id = $1 AND (preview_accessed_at IS NULL "
                "OR preview_accessed_at < NOW() - INTERVAL '30 minutes')", msg_id)
    except Exception:
        pass


@app.get("/api/thumb/{msg_id}")
async def lazy_thumb(msg_id: int):
    """On-demand thumb materialization. Cached -> serve. Uncached -> mark
    thumb_path='__user_queued__' so crawler thumb_downloader picks it up,
    wait briefly (~2.5s) for the file to land, fallback to 202 + 1x1 GIF.
    """
    import asyncio
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT thumb_path FROM messages WHERE id=$1", msg_id)
    if not row:
        return Response("no msg", status_code=404)
    tp = row["thumb_path"]
    if tp and not tp.startswith("__"):
        fpath = os.path.join(THUMBS_DIR, tp)
        if os.path.exists(fpath):
            await _touch_thumb(msg_id)
            return FileResponse(fpath, media_type="image/jpeg",
                              headers={"Cache-Control": "public, max-age=604800"})
    if tp in ("__failed__", "__deleted__"):
        return Response(_TINY_TRANSPARENT_GIF, status_code=410,
                       media_type="image/gif")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE messages SET thumb_path = $$__user_queued__$$ "
            "WHERE id = $1 AND (thumb_path IS NULL "
            "OR thumb_path = $$__user_queued__$$)",
            msg_id)
        # Wake the crawler's thumb dispatcher instantly (event-driven, no poll).
        await conn.execute("NOTIFY thumb_queued")
    # Short wait only: a browser has ~6 connections per host, and holding
    # each for 12s made a page of uncached thumbnails feel frozen. The page
    # script retries placeholders on its own.
    for _ in range(5):
        await asyncio.sleep(0.5)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT thumb_path FROM messages WHERE id=$1", msg_id)
        if not row:
            break
        tp = row["thumb_path"]
        if tp and not tp.startswith("__"):
            fpath = os.path.join(THUMBS_DIR, tp)
            if os.path.exists(fpath):
                await _touch_thumb(msg_id)
                return FileResponse(fpath, media_type="image/jpeg",
                                  headers={"Cache-Control": "public, max-age=604800"})
        if tp in ("__failed__", "__deleted__"):
            return Response(_TINY_TRANSPARENT_GIF, status_code=410,
                           media_type="image/gif")
    return Response(_TINY_TRANSPARENT_GIF, status_code=202,
                   media_type="image/gif")


@app.get("/api/photo-preview/{msg_id}")
async def lazy_preview(msg_id: int):
    """On-demand 450px preview (~76KB JPEG) for gallery slideshow / lightbox.
    Distinct from /api/thumb which serves the 12KB grid thumbnail. Same
    lazy-queue pattern: cached → serve; missing → mark __user_queued__
    so preview_downloader fetches via t.me embed's larger CDN URL.
    Falls back to thumb if preview not yet available."""
    import asyncio
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT preview_path, thumb_path FROM messages WHERE id=$1", msg_id)
    if not row:
        return Response("no msg", status_code=404)
    pp = row["preview_path"]
    if pp and not pp.startswith("__"):
        fpath = os.path.join(PREVIEWS_DIR, pp)
        if os.path.exists(fpath):
            await _touch_preview(msg_id)
            return FileResponse(fpath, media_type="image/jpeg",
                              headers={"Cache-Control": "public, max-age=604800"})
    if pp == "__failed__":
        # Permanent failure — never going to materialize. Tell client to stop
        # retrying so slideshow can advance without waiting.
        return Response(_TINY_TRANSPARENT_GIF, status_code=410,
                       media_type="image/gif")
    # Queue preview fetch (worker picks up __user_queued__)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE messages SET preview_path='__user_queued__' "
            "WHERE id=$1 AND (preview_path IS NULL OR preview_path='__user_queued__')",
            msg_id)
        # Wake the crawler's preview dispatcher instantly (event-driven).
        await conn.execute("NOTIFY preview_queued")
    # Brief poll — if it arrives within ~6s, serve fresh; else fall back to thumb
    for _ in range(12):
        await asyncio.sleep(0.5)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT preview_path FROM messages WHERE id=$1", msg_id)
        if not row:      # removed while we waited (stale pointer cleaned up)
            return Response(_TINY_TRANSPARENT_GIF, status_code=410,
                            media_type="image/gif")
        pp = row["preview_path"]
        if pp and not pp.startswith("__"):
            fpath = os.path.join(PREVIEWS_DIR, pp)
            if os.path.exists(fpath):
                await _touch_preview(msg_id)
                return FileResponse(fpath, media_type="image/jpeg",
                                  headers={"Cache-Control": "public, max-age=604800"})
        if pp == "__failed__":
            return Response(_TINY_TRANSPARENT_GIF, status_code=410,
                           media_type="image/gif")
    # Preview not ready — return 202 with 1x1 GIF so the JS probe sees
    # naturalWidth==1 and keeps retrying. Lightbox is already showing the
    # thumb (img.src=p.src in show()); duplicating it here would make the
    # probe think the preview arrived and stop retrying.
    return Response(_TINY_TRANSPARENT_GIF, status_code=202,
                   media_type="image/gif")


def _mime_for(media_type, mime):
    if mime:
        return mime
    return ("audio/mpeg" if media_type == "audio"
            else "video/mp4" if media_type == "video"
            else "application/octet-stream")


@app.get("/media/{msg_id}")
async def serve_media(msg_id: int, request: Request):
    """Serve audio/video/document with on-demand materialization + progressive
    range streaming. Supports HTTP Range header so video players can seek
    and play while the underlying file is still being downloaded from TG.
    """
    import asyncio, re as _re
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT local_path, file_name, file_size, mime_type, media_type
               FROM messages WHERE id=$1""", msg_id)
    if not row:
        return Response("no msg", status_code=404)
    if row["local_path"] == "__failed__":
        return Response("download failed", status_code=410)
    await _want_fetch(msg_id)

    # Queue download if not already + wait for file to start appearing
    if not row["local_path"] or row["local_path"] == "__user_queued__":
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET local_path = $$__user_queued__$$ "
                "WHERE id = $1 AND (local_path IS NULL "
                "OR local_path = $$__user_queued__$$)",
                msg_id)
        for _i in range(180):  # wait up to 90s for download to start writing
            await asyncio.sleep(0.5)
            if _i % 20 == 19:
                await _want_fetch(msg_id)
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT local_path, file_name, file_size, mime_type, media_type
                       FROM messages WHERE id=$1""", msg_id)
            if not row:
                return Response("no msg", status_code=404)
            lp = row["local_path"]
            if lp and not lp.startswith("__"):
                break
            if lp == "__failed__":
                return Response("download failed", status_code=410)
        else:
            return Response("still downloading — retry in a moment", status_code=202)

    path = os.path.join("/downloads", row["local_path"])
    mime = _mime_for(row["media_type"], row["mime_type"])
    fn = row["file_name"]
    await _touch_local(msg_id)
    total_size = row["file_size"] or 0  # TG-reported final size

    # Wait for the file to actually exist on disk
    for _ in range(60):  # 30s grace
        if os.path.exists(path):
            break
        await asyncio.sleep(0.5)
    else:
        return Response("file vanished", status_code=410)

    # No Range header? Decide based on media size: if known small (audio/book),
    # serve via FileResponse (waits until fully written if size mismatches).
    range_header = request.headers.get("Range")
    if not range_header:
        if total_size and os.path.getsize(path) >= total_size:
            from urllib.parse import quote
            _fn = fn or "file"
            ascii_fn = _fn.encode("ascii", "replace").decode("ascii")
            disp = (f'inline; filename="{ascii_fn}"; '
                    f"filename*=UTF-8''{quote(_fn)}")
            return FileResponse(path, media_type=mime,
                              headers={"Cache-Control": "private, max-age=3600",
                                       "Accept-Ranges": "bytes",
                                       "Content-Disposition": disp})
        # Partial file or unknown size: fall through to progressive streamer
        start, end = 0, (total_size - 1) if total_size else None
    else:
        m = _re.match(r"bytes=(\d+)-(\d*)", range_header)
        if not m:
            return Response("bad range", status_code=416)
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else (
            total_size - 1 if total_size else None)

    if end is None:
        # Without TG size, just serve whatever\'s in the file now
        end = os.path.getsize(path) - 1

    def _tail_read(pos: int, n: int) -> bytes:
        """Bytes at `pos` from the "<file>.tail" sidecar: the end of a file
        that is still arriving, fetched first because the MP4 index lives
        there (see the crawler). b"" when not available."""
        try:
            with open(path + ".tail", "rb") as tf:
                t0 = int.from_bytes(tf.read(8), "big")
                if pos < t0:
                    return b""
                tf.seek(8 + pos - t0)
                return tf.read(n)
        except OSError:
            return b""

    _lp = row["local_path"]

    async def stream_range():
        import time as _time
        said, revived = _time.time(), False
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            stuck_iters = 0
            while remaining > 0:
                if _time.time() - said > 15:      # still being read: still wanted
                    said = _time.time()
                    await _want_fetch(msg_id)
                current = os.path.getsize(path)
                target_pos = f.tell()
                if current <= target_pos:
                    chunk = _tail_read(target_pos, min(65536, remaining))
                    if chunk:
                        f.seek(target_pos + len(chunk))
                        yield chunk
                        remaining -= len(chunk)
                        stuck_iters = 0
                        continue
                    # Partial file caught up — wait for crawler to write more.
                    # Abort if download appears stalled.
                    stuck_iters += 1
                    if stuck_iters == 60 and not revived:
                        # 30 s without a new byte: the fetch is not running
                        revived = True
                        await _revive_fetch(msg_id, _lp)
                    if stuck_iters > 600:  # 5 min no progress = give up
                        return
                    await asyncio.sleep(0.5)
                    continue
                stuck_iters = 0
                chunk_size = min(65536, remaining, current - target_pos)
                chunk = f.read(chunk_size)
                if not chunk:
                    await asyncio.sleep(0.2)
                    continue
                yield chunk
                remaining -= len(chunk)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Cache-Control": "private, max-age=3600",
    }
    if total_size:
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
    return StreamingResponse(stream_range(), status_code=206,
                           media_type=mime, headers=headers)


# ════════════════════════════════════════════════════════════════════
# Downloaded files (the on-demand cache)
#
# Opening a film, a song or a book fetches the file from Telegram into
# /downloads/{video,audio,library}. Nothing ever removed them: a first user
# who clicks through twenty films has silently spent 40 GB, and nothing in the
# lists said which items were already there (and so open instantly).
#   - lists mark what is saved and can show only that;
#   - a file not opened for `cache_ttl_days` (default 30) is removed, and when
#     the cache is over `cache_max_gb` (default 50) the least recently opened
#     go first. 0 turns either rule off. The index entry stays: opening the
#     item again simply fetches it again.
# Only these three folders are touched - never what Sonarr/Radarr grabbed.
# ════════════════════════════════════════════════════════════════════
_CACHE_PREFIXES = ("video/", "audio/", "library/")
_CACHE_SIDECARS = ("", ".tail", ".pdf", ".cover.jpg", ".queue", ".pdf.inprogress.pdf")
_LOCAL_SQL = "(m.local_path IS NOT NULL AND left(m.local_path, 2) <> '__')"
CACHE_TTL_DAYS_DEFAULT = 30
CACHE_MAX_GB_DEFAULT = 50


def _is_local(lp) -> bool:
    return bool(lp) and not lp.startswith("__")


def _local_pill(lp) -> str:
    if not _is_local(lp):
        return ""
    return ('<span class="pill ok" style="font-size:11px" '
            'title="already downloaded: opens instantly">● saved</span>')


async def _touch_local(msg_id: int) -> None:
    """Remember that a downloaded file was just used (cleanup goes by this)."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE messages SET local_accessed_at = NOW()
                   WHERE id = $1 AND (local_accessed_at IS NULL
                                      OR local_accessed_at < NOW() - INTERVAL '10 minutes')""",
                msg_id)
    except Exception:
        pass


async def _want_fetch(msg_id: int, opened: bool = False) -> None:
    """Say "someone is asking for this file right now". The crawler keeps
    fetching a big file only while this keeps being said (see its
    _ondemand_fetch_one); a player, a reader or a browser download says it
    every few seconds for as long as it is open.

    fetch_wanted_at also carries the two things a person can decide on the
    Downloads page: 'epoch' = paused by hand (a player that is merely still
    open does not undo that; opening the item again does), and a time in the
    future = "finish this one whether or not anyone is watching"."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE messages SET fetch_wanted_at = NOW()
                   WHERE id = $1 AND (fetch_wanted_at IS NULL
                          OR (fetch_wanted_at < NOW() - INTERVAL '15 seconds'
                              AND ($2 OR fetch_wanted_at <> 'epoch')))""",
                msg_id, opened)
    except Exception:
        pass


async def _revive_fetch(msg_id: int, lp: str) -> None:
    """A half-fetched file that has stopped growing (the fetch was stopped
    because nobody was watching, or the crawler was restarted) and is being
    asked for again: put it back in the queue. It continues where it stopped."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET local_path = $$__user_queued__$$, "
                "fetch_wanted_at = GREATEST(fetch_wanted_at, NOW()) "
                "WHERE id = $1 AND local_path = $2 "
                "AND (fetch_wanted_at IS NULL OR fetch_wanted_at <> 'epoch')", msg_id, lp)
    except Exception:
        pass


async def _cache_settings() -> tuple:
    async with db_pool.acquire() as conn:
        rows = {r["key"]: r["value"] for r in await conn.fetch(
            "SELECT key, value FROM config WHERE key IN ('cache_ttl_days', 'cache_max_gb')")}

    def _num(key, default):
        try:
            return max(0, int(float(rows.get(key, default))))
        except (TypeError, ValueError):
            return default
    return _num("cache_ttl_days", CACHE_TTL_DAYS_DEFAULT), _num("cache_max_gb", CACHE_MAX_GB_DEFAULT)


def _cache_stat(lp: str):
    """(bytes on disk incl. sidecars, newest mtime) of one cached item; None if gone."""
    base = os.path.join("/downloads", lp)
    size, mtime, found = 0, 0.0, False
    for suf in _CACHE_SIDECARS:
        try:
            st = os.stat(base + suf)
        except OSError:
            continue
        size += st.st_size
        if suf == "":
            found, mtime = True, st.st_mtime
    return (size, mtime) if found else None


def _cache_unlink(lp: str) -> None:
    base = os.path.join("/downloads", lp)
    for suf in _CACHE_SIDECARS:
        try:
            os.unlink(base + suf)
        except OSError:
            pass


async def _cache_sweep(clear_all: bool = False) -> tuple:
    """Apply the rules (or remove everything). Returns (files removed, bytes freed)."""
    import time as _time
    ttl_days, max_gb = await _cache_settings()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT local_path, max(local_accessed_at) AS used, max(file_size) AS want,
                      bool_or(EXISTS (SELECT 1 FROM favorites f WHERE f.msg_id = messages.id)) AS fav
               FROM messages
               WHERE local_path IS NOT NULL AND left(local_path, 2) <> '__'
               GROUP BY local_path""")
    now = _time.time()
    items, lost, partial = [], [], []
    for r in rows:
        lp = r["local_path"]
        if not lp.startswith(_CACHE_PREFIXES) or ".." in lp:
            continue
        st = await asyncio.to_thread(_cache_stat, lp)
        if st is None:
            lost.append(lp)                 # the file is gone: forget the path
            continue
        size, mtime = st
        if now - mtime < 180:
            continue                        # still arriving, or just finished
        used = max(mtime, r["used"].timestamp() if r["used"] else 0)
        try:
            have = os.path.getsize(os.path.join("/downloads", lp))
        except OSError:
            have = 0
        if r["want"] and have < r["want"] * 0.95 and not clear_all:
            # a fetch stopped half-way: kept a day in case it is continued
            if now - used > 86400:
                partial.append((used, size, lp))
            continue
        if r["fav"] and not clear_all:
            continue                            # a favourite stays until un-favourited
        items.append((used, size, lp))
    doomed = []
    if clear_all:
        doomed = items
    else:
        keep = []
        for it in items:
            (doomed if ttl_days and now - it[0] > ttl_days * 86400 else keep).append(it)
        if max_gb:
            keep.sort()                     # least recently used first
            total = sum(x[1] for x in keep)
            while keep and total > max_gb * 1024 ** 3:
                it = keep.pop(0)
                total -= it[1]
                doomed.append(it)
    doomed += partial
    freed = 0
    for used, size, lp in doomed:
        await asyncio.to_thread(_cache_unlink, lp)
        freed += size
    gone = [x[2] for x in doomed] + lost
    if gone:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET local_path = NULL, local_accessed_at = NULL "
                "WHERE local_path = ANY($1::text[])", gone)
    return len(doomed), freed


async def _cache_gc_worker():
    await asyncio.sleep(600)
    while True:
        try:
            n, freed = await _cache_sweep()
            if n:
                log.info("[cache] removed %d downloaded file(s) not opened lately, freed %s",
                         n, _fmt_size(freed))
        except Exception as e:
            log.warning("[cache] sweep failed: %s", e)
        await asyncio.sleep(3600)


async def _cache_usage() -> list:
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            """SELECT split_part(local_path, '/', 1) AS kind, count(*) AS n,
                      COALESCE(sum(sz), 0) AS bytes
               FROM (SELECT local_path, max(file_size) AS sz FROM messages
                     WHERE local_path IS NOT NULL AND left(local_path, 2) <> '__'
                     GROUP BY local_path) x
               WHERE split_part(local_path, '/', 1) IN ('video', 'audio', 'library')
               GROUP BY 1 ORDER BY 3 DESC""")


# ═════════════════════════════════════════════════
# Is there a newer tgarr, and is this install updating itself?
#
# Until 0.5.7 nothing in tgarr said that a newer version existed. Installs
# whose updater was missing or broken (most installs made before 0.5.6, and
# 0.5.6 itself on a current Docker engine) simply stayed where they were, and
# their owners had no way to know.
#
# Every ~6 hours the registry is asked which version is current (one small
# GET that sends nothing about this install; the answer is cached by the CDN).
# A newer version alone is not news - the updater installs it within the
# hour. It becomes a notice on the Dashboard when this install is STILL
# behind several hours after it first learned of the version: then the
# updater is not doing its job, and the notice says what to run.
# Off with TGARR_UPDATE_CHECK=false.
# ═════════════════════════════════════════════════
UPDATE_CHECK_ENABLED = os.environ.get("TGARR_UPDATE_CHECK", "true").lower() != "false"
UPDATE_REGISTRY_URL = os.environ.get("TGARR_REGISTRY_URL", "https://tgarr.me").rstrip("/")
UPDATE_STALLED_AFTER_S = 4 * 3600
UPDATE_HELP_URL = "https://github.com/tgarrpro/tgarr/blob/main/docs/install/upgrade.md"
_PINNED_TAG = os.environ.get("TGARR_PINNED_TAG", "").strip()
_VERSION_RX = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")


def _ver_tuple(v: str) -> tuple:
    return tuple(int(x) for x in v.split(".")) if v and _VERSION_RX.match(v) else ()


def _update_fetch() -> str:
    req = urllib.request.Request(UPDATE_REGISTRY_URL + "/api/v1/version",
                                 headers={"User-Agent": f"tgarr/{TGARR_VERSION} (+https://tgarr.me)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        v = str(_json.loads(r.read(65536)).get("current_version", ""))
    return v if _VERSION_RX.match(v) else ""


async def _update_note(latest: str, now: float) -> None:
    """Record what the registry said. `update_seen` remembers WHEN this install
    first learned of a version newer than itself; it is what 'still behind
    after N hours' is measured from, and it is kept across restarts."""
    async with db_pool.acquire() as conn:
        async def put(k, v):
            await conn.execute(
                """INSERT INTO config (key, value) VALUES ($1, $2)
                   ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""", k, v)
        await put("update_latest", latest)
        await put("update_checked_at", str(int(now)))
        if _ver_tuple(latest) > _ver_tuple(TGARR_VERSION):
            seen = await conn.fetchval("SELECT value FROM config WHERE key = 'update_seen'") or ""
            if seen.split("|")[0] != latest:
                await put("update_seen", f"{latest}|{int(now)}")
        else:
            await conn.execute("DELETE FROM config WHERE key IN ('update_seen', 'update_dismissed')")


async def _update_check_worker():
    import random
    if not UPDATE_CHECK_ENABLED:
        return
    await asyncio.sleep(random.uniform(45, 300))
    while True:
        try:
            latest = await asyncio.to_thread(_update_fetch)
            if latest:
                await _update_note(latest, time.time())
        except Exception as e:
            log.info("[update] could not ask %s which version is current: %s", UPDATE_REGISTRY_URL, e)
        # 6 h, spread out so installs do not all ask at once
        await asyncio.sleep(21600 + random.uniform(0, 3600))


def _update_state(rows: dict, now: float) -> dict:
    """Pure: what to tell the user, from the stored config values."""
    latest = rows.get("update_latest") or ""
    if not UPDATE_CHECK_ENABLED or _ver_tuple(latest) <= _ver_tuple(TGARR_VERSION):
        return {"kind": "current"}
    seen_v, _, seen_at = (rows.get("update_seen") or "").partition("|")
    try:
        behind_s = max(0, now - float(seen_at)) if seen_v == latest else 0
    except ValueError:
        behind_s = 0
    st = {"latest": latest, "hours": int(behind_s // 3600)}
    if rows.get("update_dismissed") == latest:
        st["kind"] = "dismissed"
    elif _PINNED_TAG and _PINNED_TAG not in ("latest", "edge"):
        st.update(kind="pinned", tag=_PINNED_TAG)
    elif behind_s >= UPDATE_STALLED_AFTER_S:
        st["kind"] = "stalled"
    else:
        st["kind"] = "pending"
    return st


async def _update_banner() -> str:
    async with db_pool.acquire() as conn:
        rows = {r["key"]: r["value"] for r in await conn.fetch(
            "SELECT key, value FROM config WHERE key IN "
            "('update_latest', 'update_seen', 'update_dismissed')")}
    st = _update_state(rows, time.time())
    if st["kind"] in ("current", "dismissed", "pending"):
        return ""
    latest = html.escape(st["latest"])
    hide = ('<form method="POST" action="/api/update/dismiss" style="margin:0">'
            '<button class="abtn" type="submit" title="hide until the next version">Hide</button></form>')
    box = ('<div style="background:{bg};border:1px solid {bd};border-radius:12px;padding:14px 18px;'
           'margin-bottom:16px;line-height:1.6;display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap">'
           '<div style="flex:1;min-width:240px">{body}</div>{hide}</div>')
    if st["kind"] == "pinned":
        body = (f'<b>tgarr {latest} is available.</b> This install stays on {html.escape(TGARR_VERSION)} '
                f'because <code>TGARR_TAG={html.escape(st["tag"])}</code> is set in <code>.env</code>. '
                f'Remove that line and run <code>docker compose up -d</code> to follow new versions.')
        return box.format(bg="#eff6ff", bd="#bfdbfe", body=body, hide=hide)
    body = (f'<b>tgarr {latest} is available, and this install is still on {html.escape(TGARR_VERSION)}.</b> '
            f'It has not updated itself in {st["hours"]} hours, so its updater is probably missing or not running. '
            f'To update now, in the folder that holds <code>docker-compose.yml</code>:'
            f'<div style="margin:6px 0"><code>docker compose pull &amp;&amp; docker compose up -d</code></div>'
            f'<a href="{UPDATE_HELP_URL}" target="_blank" rel="noopener">How to make updates automatic again</a>')
    return box.format(bg="#fffbeb", bd="#fde68a", body=body, hide=hide)


@app.get("/api/me/avatar")
async def me_avatar():
    if not _signed_in() or not os.path.isfile(login.AVATAR_FILE):
        return Response(status_code=404)
    return FileResponse(login.AVATAR_FILE, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/update/dismiss")
async def update_dismiss():
    async with db_pool.acquire() as conn:
        latest = await conn.fetchval("SELECT value FROM config WHERE key = 'update_latest'")
        if latest:
            await conn.execute(
                """INSERT INTO config (key, value) VALUES ('update_dismissed', $1)
                   ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()""", latest)
    return RedirectResponse("/", status_code=303)


@app.get("/api/update")
async def update_status():
    async with db_pool.acquire() as conn:
        rows = {r["key"]: r["value"] for r in await conn.fetch(
            "SELECT key, value FROM config WHERE key IN "
            "('update_latest', 'update_seen', 'update_dismissed', 'update_checked_at')")}
    st = _update_state(rows, time.time())
    st.update(running=TGARR_VERSION, enabled=UPDATE_CHECK_ENABLED,
              checked_at=int(rows["update_checked_at"]) if rows.get("update_checked_at", "").isdigit() else None)
    return st


def _page_nav(path: str, qs, offset: int, size: int, shown: int, total: int) -> str:
    """'showing a-b of n' + prev / next. `qs(offset=...)` builds the query string."""
    if not total or (offset == 0 and shown >= total):
        return ""
    links = []
    if offset > 0:
        prev = max(0, offset - size)
        links.append(f'<a class="btn ghost" href="{path}{qs(offset=prev if prev else None)}">← prev</a>')
    if offset + size < total:
        links.append(f'<a class="btn ghost" href="{path}{qs(offset=offset + size)}">next →</a>')
    return (
        '<div style="display:flex;justify-content:space-between;align-items:center;'
        'margin-top:18px;padding:12px 0 90px;border-top:1px solid var(--border, #e2e8f0)">'
        f'<span style="color:var(--muted, #64748b);font-size:13px">'
        f'showing {offset + 1 if shown else 0}–{offset + shown} of {total:,}</span>'
        f'<div style="display:flex;gap:8px">{"".join(links)}</div></div>')


def _saved_chip(path: str, qs, local: int) -> str:
    style = ("background:rgba(16,185,129,0.15);color:#059669;border-color:#10b981;"
             if local else "")
    return (f'<a class="btn ghost" href="{path}{qs(local=None if local else 1)}" '
            f'style="{style}padding:5px 12px;font-size:12px" '
            f'title="only what is already downloaded">● saved</a>')


@app.post("/api/fav/{msg_id}")
async def fav_toggle(msg_id: int):
    """Toggle a favourite. Favourites get their own filter, and their
    downloaded file is never tidied away."""
    async with db_pool.acquire() as conn:
        gone = await conn.fetchval(
            "DELETE FROM favorites WHERE msg_id = $1 RETURNING msg_id", msg_id)
        if gone is None:
            try:
                await conn.execute(
                    "INSERT INTO favorites (msg_id) VALUES ($1) ON CONFLICT DO NOTHING", msg_id)
            except asyncpg.ForeignKeyViolationError:
                return JSONResponse({"error": "no such item"}, status_code=404)
    return {"fav": gone is None}


_LRC_LINE = _re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def _lyrics_payload(raw: str) -> dict:
    """LRC text -> timed lines; anything else -> plain text."""
    lines = []
    for ln in (raw or "").splitlines():
        stamps = list(_LRC_LINE.finditer(ln))
        if not stamps:
            continue
        text = _LRC_LINE.sub("", ln).strip()
        for m in stamps:
            frac = m.group(3) or "0"
            t = int(m.group(1)) * 60 + int(m.group(2)) + int(frac) / (10 ** len(frac))
            lines.append([round(t, 2), text])
    if len(lines) >= 3:
        lines.sort(key=lambda x: x[0])
        return {"found": True, "synced": True, "lines": lines}
    text = _re.sub(r"\[[^\]\n]{0,24}\]", "", raw or "").strip()
    if text:
        return {"found": True, "synced": False, "text": text[:20000]}
    return {"found": False}


def _flac_embedded_lyrics(path: str):
    """LYRICS / UNSYNCEDLYRICS from a FLAC's Vorbis comments, if any."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"fLaC":
                return None
            while True:
                h = f.read(4)
                if len(h) < 4:
                    return None
                ln = int.from_bytes(h[1:], "big")
                if (h[0] & 0x7F) != 4:
                    if h[0] & 0x80:
                        return None
                    f.seek(ln, 1)
                    continue
                b = f.read(min(ln, 2 * 1024 * 1024))
                off = 4 + int.from_bytes(b[:4], "little")
                n = int.from_bytes(b[off:off + 4], "little")
                off += 4
                for _ in range(min(n, 500)):
                    cl = int.from_bytes(b[off:off + 4], "little")
                    c = b[off + 4:off + 4 + cl].decode("utf-8", "replace")
                    off += 4 + cl
                    k, _, v = c.partition("=")
                    if k.upper() in ("LYRICS", "UNSYNCEDLYRICS", "SYNCEDLYRICS") and v.strip():
                        return v
                return None
    except (OSError, ValueError, IndexError):
        return None


def _lrclib_lookup(title: str, artist: str, duration) -> str:
    """Lyrics from lrclib.net (free, no key). '' when it has none.
    Sends the track title, artist and length - nothing about the user."""
    import json
    import urllib.parse
    import urllib.request
    ua = {"User-Agent": f"tgarr/{TGARR_VERSION} (+https://tgarr.me)"}

    def _get(url):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=8) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return None
    q = {"track_name": title}
    if artist:
        q["artist_name"] = artist
    if artist and duration:
        j = _get("https://lrclib.net/api/get?" + urllib.parse.urlencode({**q, "duration": int(duration)}))
        if isinstance(j, dict) and (j.get("syncedLyrics") or j.get("plainLyrics")):
            return j.get("syncedLyrics") or j.get("plainLyrics") or ""
    j = _get("https://lrclib.net/api/search?" + urllib.parse.urlencode(q))
    if isinstance(j, list):
        best = None
        for it in j[:20]:
            if not isinstance(it, dict):
                continue
            if duration and it.get("duration") and abs(float(it["duration"]) - float(duration)) > 8:
                continue
            if it.get("syncedLyrics"):
                return it["syncedLyrics"]
            best = best or it.get("plainLyrics")
        return best or ""
    return ""


@app.get("/api/lyrics/{msg_id}")
async def lyrics(msg_id: int):
    """Lyrics for a track: the file's own tag first, then lrclib.net. Looked
    up once (when someone opens the lyrics) and kept."""
    async with db_pool.acquire() as conn:
        r = await conn.fetchrow(
            """SELECT audio_lyrics, audio_title, audio_canonical_title, audio_performer,
                      audio_duration_sec, file_name, local_path
               FROM messages WHERE id = $1 AND media_type = 'audio'""", msg_id)
    if not r:
        return JSONResponse({"found": False}, status_code=404)
    raw = r["audio_lyrics"]
    if raw is None:
        raw = ""
        if _is_local(r["local_path"]) and (r["local_path"] or "").lower().endswith(".flac"):
            raw = await asyncio.to_thread(
                _flac_embedded_lyrics, os.path.join("/downloads", r["local_path"])) or ""
        if not raw:
            title = (r["audio_canonical_title"] or r["audio_title"] or "").strip()
            artist = (r["audio_performer"] or "").strip()
            if not title and r["file_name"]:
                stem = r["file_name"].rsplit(".", 1)[0]
                if " - " in stem:
                    artist, _, title = stem.partition(" - ")
                else:
                    title = stem
            if title:
                raw = await asyncio.to_thread(
                    _lrclib_lookup, title.strip(), artist.strip(), r["audio_duration_sec"])
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE messages SET audio_lyrics = $2 WHERE id = $1", msg_id, raw)
    return JSONResponse(_lyrics_payload(raw))


async def _player_fetches() -> list:
    """Files being fetched because someone opened them (not grabs): queued,
    arriving, or stopped half-way."""
    import time as _time
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT m.id, m.file_name, m.file_size, m.media_type, m.local_path, m.file_dc,
                      m.fetch_wanted_at
               FROM messages m
               WHERE m.local_path = '__user_queued__'
                  OR ((m.fetch_wanted_at > NOW() - INTERVAL '1 day' OR m.fetch_wanted_at = 'epoch')
                      AND m.local_path IS NOT NULL AND left(m.local_path, 2) <> '__')
               ORDER BY m.fetch_wanted_at DESC NULLS LAST LIMIT 60""")
    out, now = [], _time.time()
    for r in rows:
        lp, total = r["local_path"], r["file_size"] or 0
        paused = bool(r["fetch_wanted_at"] and r["fetch_wanted_at"].year < 2000)
        pinned = bool(r["fetch_wanted_at"] and r["fetch_wanted_at"].timestamp() > now + 600)
        if lp == "__user_queued__":
            out.append({"r": r, "state": "queued", "have": 0, "pinned": pinned})
            continue
        if not lp.startswith(_CACHE_PREFIXES):
            continue
        try:
            st = os.stat(os.path.join("/downloads", lp))
        except OSError:
            continue
        if total and st.st_size >= total:
            continue                            # complete: not a fetch any more
        out.append({"r": r, "have": st.st_size, "pinned": pinned,
                    "state": ("paused" if paused else
                              "fetching" if now - st.st_mtime < 30 else "stopped")})
    return out


_SAVED_KINDS = {"video": "m.media_type = 'video'", "audio": "m.media_type = 'audio'",
                "book": "m.media_type NOT IN ('video', 'audio')"}
_SAVED_SORTS = {
    "recent": "COALESCE(m.local_accessed_at, m.indexed_at) DESC NULLS LAST, m.id DESC",
    "size": "m.file_size DESC NULLS LAST, m.id DESC",
    "name": "m.file_name ASC NULLS LAST, m.id",
    "oldest": "COALESCE(m.local_accessed_at, m.indexed_at) ASC NULLS FIRST, m.id",
}


async def _player_saved(q=None, kind=None, sort="recent", fav=0, limit=50, offset=0) -> dict:
    """One page of the files that were opened in a player and kept.

    Everything comes from the database (the partial index on local_path
    makes "what is saved" cheap however large the index is); the disk is
    looked at only for the rows on the page, to mark one that is not whole.
    A library of tens of thousands of saved files costs the same as ten."""
    where = ["m.local_path IS NOT NULL", "left(m.local_path, 2) <> '__'",
             "split_part(m.local_path, '/', 1) IN ('video', 'audio', 'library')"]
    params = []
    if q:
        for w in q.split()[:6]:
            params.append(f"%{w}%")
            where.append(f"m.file_name ILIKE ${len(params)}")
    if kind in _SAVED_KINDS:
        where.append(_SAVED_KINDS[kind])
    if fav:
        where.append("EXISTS (SELECT 1 FROM favorites f WHERE f.msg_id = m.id)")
    if _pub_m("m"):
        where.append(_pub_m("m")[5:])           # signed out: public channels only
    wsql = " AND ".join(where)
    order = _SAVED_SORTS.get(sort, _SAVED_SORTS["recent"])
    async with db_pool.acquire() as conn:
        tot = await conn.fetchrow(
            f"SELECT count(*) AS n, COALESCE(sum(m.file_size), 0) AS bytes FROM messages m WHERE {wsql}",
            *params)
        kinds = await conn.fetch(
            f"""SELECT split_part(local_path, '/', 1) AS k, count(*) AS n
               FROM messages WHERE local_path IS NOT NULL AND left(local_path, 2) <> '__'
                 AND split_part(local_path, '/', 1) IN ('video', 'audio', 'library')
                 {_pub_m("messages")}
               GROUP BY 1""")
        rows = await conn.fetch(
            f"""SELECT m.id, m.file_name, m.file_size, m.media_type, m.local_path,
                       COALESCE(m.local_accessed_at, m.indexed_at) AS used,
                       EXISTS (SELECT 1 FROM favorites f WHERE f.msg_id = m.id) AS fav
                FROM messages m WHERE {wsql}
                ORDER BY {order} LIMIT {int(limit)} OFFSET {int(offset)}""", *params)

    def _stat(rows_):
        out = []
        for r in rows_:
            try:
                have = os.path.getsize(os.path.join("/downloads", r["local_path"]))
            except OSError:
                have = None
            out.append(have)
        return out
    have = await asyncio.to_thread(_stat, rows)
    return {"rows": rows, "have": have, "total": tot["n"], "bytes": tot["bytes"],
            "kinds": {r["k"]: r["n"] for r in kinds}}


@app.post("/api/fetch/remove")
async def fetch_remove_many(request: Request):
    """Remove several saved files at once (they stay in the index)."""
    try:
        ids = [int(x) for x in (await request.json()).get("ids", [])][:500]
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not ids:
        return {"removed": 0}
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """UPDATE messages m SET local_path = NULL, local_accessed_at = NULL,
                      fetch_wanted_at = 'epoch'
               FROM (SELECT id, local_path FROM messages WHERE id = ANY($1::bigint[])) o
               WHERE m.id = o.id RETURNING o.local_path""", ids)
    n = 0
    for r in rows:
        lp = r["local_path"]
        if _is_local(lp) and lp.startswith(_CACHE_PREFIXES) and ".." not in lp:
            await asyncio.to_thread(_cache_unlink, lp)
            n += 1
    return {"removed": n}


@app.post("/api/fetch/{msg_id}/pause")
async def fetch_pause(msg_id: int):
    """Stop fetching, keep what has arrived. The crawler notices within
    seconds; a player that is still open does not restart it."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE messages SET fetch_wanted_at = 'epoch' WHERE id = $1", msg_id)
        # not started yet: take it out of the queue (nothing has arrived)
        await conn.execute(
            "UPDATE messages SET local_path = NULL "
            "WHERE id = $1 AND local_path = '__user_queued__'", msg_id)
    return {"ok": True}


@app.post("/api/fetch/{msg_id}/resume")
async def fetch_resume(msg_id: int):
    """Fetch the rest, and finish it whether or not anyone is watching."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE messages SET fetch_wanted_at = NOW() + INTERVAL '1 day',
                      local_path = '__user_queued__'
               WHERE id = $1 AND (local_path IS NULL OR left(local_path, 2) <> '__'
                                  OR local_path = '__user_queued__')""", msg_id)
    return {"ok": True}


@app.post("/api/fetch/{msg_id}/cancel")
async def fetch_cancel(msg_id: int):
    """Stop fetching a file someone opened, and forget what has arrived."""
    async with db_pool.acquire() as conn:
        lp = await conn.fetchval(
            """UPDATE messages SET fetch_wanted_at = 'epoch'
               WHERE id = $1 RETURNING local_path""", msg_id)
        if lp is None:
            return {"ok": True}
        await conn.execute(
            "UPDATE messages SET local_path = NULL, local_accessed_at = NULL "
            "WHERE id = $1 AND local_path = $2", msg_id, lp)
    if _is_local(lp) and lp.startswith(_CACHE_PREFIXES) and ".." not in lp:
        await asyncio.to_thread(_cache_unlink, lp)
    return {"ok": True}


@app.post("/api/settings/cache")
async def settings_cache(ttl_days: str = Form("30"), max_gb: str = Form("50")):
    def _n(v, d):
        try:
            return str(max(0, min(100000, int(float(v)))))
        except (TypeError, ValueError):
            return str(d)
    async with db_pool.acquire() as conn:
        for k, v in (("cache_ttl_days", _n(ttl_days, CACHE_TTL_DAYS_DEFAULT)),
                     ("cache_max_gb", _n(max_gb, CACHE_MAX_GB_DEFAULT))):
            await conn.execute(
                """INSERT INTO config (key, value) VALUES ($1, $2)
                   ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""", k, v)
    return RedirectResponse("/settings#cache", status_code=302)


@app.post("/api/settings/cache_clear")
async def settings_cache_clear():
    n, freed = await _cache_sweep(clear_all=True)
    log.info("[cache] cleared on request: %d file(s), %s", n, _fmt_size(freed))
    return RedirectResponse("/settings#cache", status_code=302)


_MY_DC_CACHE: Optional[int] = None


async def _get_my_dc() -> Optional[int]:
    """Read user\'s home DC from the Pyrogram session sqlite. Cached after
    first read. Used by /api/media-status to flag cross-DC files."""
    global _MY_DC_CACHE
    if _MY_DC_CACHE is not None:
        return _MY_DC_CACHE
    import sqlite3
    sp = os.path.join(login.SESSION_DIR, "tgarr.session")
    if not os.path.exists(sp):
        return None
    try:
        c = sqlite3.connect(f"file:{sp}?mode=ro", uri=True, timeout=2)
        try:
            r = c.execute("SELECT dc_id FROM sessions LIMIT 1").fetchone()
        finally:
            c.close()
        if r and r[0]:
            _MY_DC_CACHE = int(r[0])
            return _MY_DC_CACHE
    except Exception:
        pass
    return None



import zipfile as _zipfile
from urllib.parse import quote as _q, unquote as _uq
import re as _re

_EPUB_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp",
    "css": "text/css", "html": "text/html", "xhtml": "application/xhtml+xml",
    "js": "application/javascript", "ttf": "font/ttf", "otf": "font/otf",
    "woff": "font/woff", "woff2": "font/woff2",
}


def _epub_locate(msg_id, db_row):
    """Resolve disk path of the .epub or return None."""
    if not db_row or not db_row["local_path"] or db_row["local_path"].startswith("__"):
        return None
    p = os.path.join("/downloads", db_row["local_path"])
    return p if os.path.exists(p) else None


def _disk_free_gb(path: str) -> float:
    import shutil
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return 0.0


def _ebook_safe_to_convert(src: str, ext: str) -> bool:
    """Cheap safety check: reject zip-bombs + malformed archives before
    handing the file to calibre. Caps total uncompressed size at 500MB
    and entry count at 20K. Non-zip formats pass through (calibre has
    its own format-specific limits)."""
    if ext not in ("epub", "cbz", "cbr"):
        return True  # mobi/azw/djvu/etc. not zip-based; trust calibre
    import zipfile
    try:
        with zipfile.ZipFile(src) as zf:
            infos = zf.infolist()
            if len(infos) > 20000:
                return False
            total = 0
            for i in infos:
                total += i.file_size
                if total > 500 * 1024 * 1024:
                    return False
                # Per-entry sanity: compression ratio > 100:1 is suspicious
                if i.compress_size > 0 and i.file_size / i.compress_size > 200:
                    return False
            return True
    except (zipfile.BadZipFile, OSError):
        return False


_EBOOK_LOCKS: dict[int, "asyncio.Lock"] = {}
_EBOOK_STATUS_RATE: dict = {}  # ip -> [timestamps within 60s window]

def _ebook_rate_check(ip: str, limit_per_min: int = 60) -> bool:
    import time as _t
    now = _t.time()
    window = _EBOOK_STATUS_RATE.setdefault(ip, [])
    window[:] = [ts for ts in window if now - ts < 60]
    if len(window) >= limit_per_min:
        return False
    window.append(now)
    return True
_EBOOK_BG_TASKS: set = set()
_EBOOK_CONVERT_SEMAPHORE = None  # initialized lazily on first use

def _ebook_semaphore():
    import asyncio
    global _EBOOK_CONVERT_SEMAPHORE
    if _EBOOK_CONVERT_SEMAPHORE is None:
        _EBOOK_CONVERT_SEMAPHORE = asyncio.Semaphore(1)
    return _EBOOK_CONVERT_SEMAPHORE
_EBOOK_JOBS: dict = {}  # msg_id -> {state, started_at}

def _ebook_lock(msg_id: int):
    import asyncio
    if msg_id not in _EBOOK_LOCKS:
        _EBOOK_LOCKS[msg_id] = asyncio.Lock()
    return _EBOOK_LOCKS[msg_id]


_EBOOK_CONVERTIBLE = ("epub", "mobi", "azw", "azw3", "djvu", "fb2",
                      "lit", "cbr", "cbz", "rtf", "lrf", "pdb")

def _ebook_paths(local_path: str):
    """Return (source_abs, pdf_abs, cover_abs) for a cached ebook."""
    src = os.path.join("/downloads", local_path)
    pdf = src + ".pdf"
    cover = src + ".cover.jpg"
    return src, pdf, cover


# calibre renders a book to PDF through a headless Chromium. On some books it
# does not stop growing: a 3 MB illustrated EPUB reached 7.4 GB and took the
# whole host into swap until the kernel's OOM killer stepped in (the comment
# here used to say "mem bound via container cgroup"; nothing sets one).
# So the conversion is watched: its whole process group is killed once it
# holds more memory than EBOOK_MAX_RSS_MB or runs longer than
# EBOOK_MAX_SECONDS, and a conversion stopped this way is not retried.
EBOOK_MAX_RSS_MB = int(os.environ.get("TGARR_EBOOK_MAX_RSS_MB", "1500"))
EBOOK_MAX_SECONDS = int(os.environ.get("TGARR_EBOOK_MAX_SECONDS", "600"))


def _pgroup_rss_mb(pgid: int) -> int:
    """Resident memory of every process in a process group, in MB."""
    page = os.sysconf("SC_PAGE_SIZE")
    total = 0
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return 0
    for d in pids:
        try:
            with open(f"/proc/{d}/stat", "rb") as f:
                st = f.read().decode("ascii", "replace")
            rest = st[st.rindex(")") + 2:].split()
            # after "pid (comm)": state ppid pgrp ... ; rss is field 24 of the line
            if int(rest[2]) == pgid:
                total += int(rest[21]) * page
        except (OSError, ValueError, IndexError):
            continue
    return total // (1024 * 1024)


async def _ebook_watchdog(proc, src: str) -> str:
    """Wait for the conversion; kill its process group if it misbehaves.
    Returns "" when it ended by itself, else why it was stopped."""
    import signal
    import time as _t
    t0 = _t.time()
    while True:
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=2)
            return ""
        except asyncio.TimeoutError:
            pass
        rss = await asyncio.to_thread(_pgroup_rss_mb, proc.pid)
        why = ""
        if rss > EBOOK_MAX_RSS_MB:
            why = f"used {rss} MB of memory (limit {EBOOK_MAX_RSS_MB})"
        elif _t.time() - t0 > EBOOK_MAX_SECONDS:
            why = f"ran longer than {EBOOK_MAX_SECONDS} s"
        if why:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            await proc.wait()
            print(f"[ebook-convert] stopped: {why} src={src}", flush=True)
            return why


async def _ebook_to_pdf(src: str, pdf: str, msg_id: int = 0) -> bool:
    """Run calibre ebook-convert src → pdf. Returns True on success.
    Idempotent: skip if pdf already exists and is newer than src.
    Serialized per msg_id so concurrent /ebook-pdf hits don't fork 2 calibres.
    """
    import asyncio
    if os.path.exists(pdf) and os.path.getmtime(pdf) >= os.path.getmtime(src):
        return True
    import time as _t
    # Mark queued + write disk marker for restart persistence
    if msg_id not in _EBOOK_JOBS or _EBOOK_JOBS[msg_id].get("state") == "failed":
        _EBOOK_JOBS[msg_id] = {"state": "queued", "queued_at": _t.time()}
    try:
        open(src + ".queue", "w").write(str(msg_id))
    except OSError:
        pass
    async with _ebook_semaphore():  # cap concurrent conversions
      async with _ebook_lock(msg_id):
        if os.path.exists(pdf) and os.path.getmtime(pdf) >= os.path.getmtime(src):
            _EBOOK_JOBS.pop(msg_id, None)
            try: os.unlink(src + ".queue")
            except OSError: pass
            return True
        _EBOOK_JOBS[msg_id] = {"state": "converting", "started_at": _t.time()}
        env = dict(os.environ)
        env["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
        env["QTWEBENGINE_CHROMIUM_FLAGS"] = "--no-sandbox --disable-gpu"
        tmp = pdf + ".inprogress.pdf"
        # preexec_fn: RLIMIT_CPU 240s + RLIMIT_FSIZE 200MB; mem bound via container cgroup
        def _set_limits():
            # (an address-space limit is no use here: Chromium maps a terabyte
            # and touches little of it - memory is watched by _ebook_watchdog)
            try:
                os.nice(15)          # never compete with playback or the UI
            except OSError:
                pass
        proc = await asyncio.create_subprocess_exec(
            "ebook-convert", src, tmp,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env, preexec_fn=_set_limits, start_new_session=True)
        stopped = await _ebook_watchdog(proc, src)
        if stopped:
            for _f in (tmp, src + ".queue"):     # not retried: it would do the same again
                try: os.unlink(_f)
                except OSError: pass
            _EBOOK_JOBS[msg_id] = {"state": "failed", "reason":
                                   "this book is too heavy to convert here (" + stopped + ")",
                                   "started_at": _EBOOK_JOBS.get(msg_id, {}).get("started_at", 0)}
            return False
        if proc.returncode != 0:
            print(f'[ebook-convert] rc={proc.returncode} src={src}', flush=True)
        if proc.returncode == 0 and os.path.exists(tmp):
            os.rename(tmp, pdf)
            _EBOOK_JOBS.pop(msg_id, None)
            try: os.unlink(src + ".queue")
            except OSError: pass
            return True
        if os.path.exists(tmp):
            os.unlink(tmp)
        _EBOOK_JOBS[msg_id] = {"state": "failed", "started_at": _EBOOK_JOBS.get(msg_id, {}).get("started_at", 0)}
        # A conversion that failed will fail again: do not retry it on every restart.
        try: os.unlink(src + ".queue")
        except OSError: pass
        return False


async def _ebook_cover(src: str, cover: str) -> bool:
    import asyncio
    if os.path.exists(cover):
        return True
    proc = await asyncio.create_subprocess_exec(
        "ebook-meta", src, f"--get-cover={cover}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return False
    return proc.returncode == 0 and os.path.exists(cover)


@app.get("/api/ebook-status/{msg_id}")
async def ebook_status(msg_id: int, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not _ebook_rate_check(client_ip):
        return {"state": "failed", "reason": "rate limit (60 req/min/ip)"}
    """Return ebook conversion status + trigger background convert if needed.
    States: no_source | ready | converting | queued | failed.
    """
    import asyncio
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT local_path, file_name, file_size FROM messages WHERE id=$1", msg_id)
    if not row or not row["local_path"] or row["local_path"].startswith("__"):
        return {"state": "no_source"}
    ext = (row["file_name"] or "").rsplit(".", 1)[-1].lower()
    src, pdf, _ = _ebook_paths(row["local_path"])
    if not os.path.exists(src):
        return {"state": "no_source"}
    # Still arriving from Telegram: half an archive is not a broken archive.
    if row["file_size"] and os.path.getsize(src) < row["file_size"]:
        return {"state": "downloading", "src_size": os.path.getsize(src)}
    if ext == "pdf":
        return {"state": "ready", "url": f"/media/{msg_id}"}
    if ext not in _EBOOK_CONVERTIBLE:
        return {"state": "failed", "reason": f"unsupported format .{ext}"}
    if not _ebook_safe_to_convert(src, ext):
        return {"state": "failed", "reason": "file rejected: zip-bomb or malformed"}
    if os.path.exists(pdf) and os.path.getmtime(pdf) >= os.path.getmtime(src):
        return {"state": "ready", "url": f"/ebook-pdf/{msg_id}",
                "size": os.path.getsize(pdf)}
    job = _EBOOK_JOBS.get(msg_id)
    if job and job.get("state") == "converting":
        import time as _t
        return {"state": "converting",
                "src_size": os.path.getsize(src),
                "elapsed_s": int(_t.time() - job["started_at"])}
    if job and job.get("state") == "queued":
        import time as _t
        # Position = how many queued/converting jobs queued BEFORE this one
        ahead = sum(1 for mid, j in _EBOOK_JOBS.items()
                    if mid != msg_id
                    and j.get("state") in ("queued", "converting")
                    and j.get("queued_at", 0) < job.get("queued_at", _t.time()))
        src_mb = os.path.getsize(src) / (1024 * 1024)
        return {"state": "queued",
                "position": ahead + 1,
                "src_size": os.path.getsize(src),
                "eta_s": int(60 + ahead * 90 + src_mb * 2),
                "waited_s": int(_t.time() - job["queued_at"])}
    if job and job.get("state") == "failed":
        return {"state": "failed",
                "reason": job.get("reason") or "conversion error — check api logs"}
    # Not started — fire background task. Lock prevents duplicate work.
    free_gb = _disk_free_gb("/downloads")
    if free_gb < 1.0:
        return {"state": "failed",
                "reason": f"insufficient disk space ({free_gb:.1f}GB free, need 1GB)"}
    _t = asyncio.create_task(_ebook_to_pdf(src, pdf, msg_id=msg_id))
    _EBOOK_BG_TASKS.add(_t)
    _t.add_done_callback(_EBOOK_BG_TASKS.discard)
    return {"state": "queued", "src_size": os.path.getsize(src)}


@app.get("/ebook-pdf/{msg_id}")
async def ebook_pdf(msg_id: int):
    """Unified ebook viewer: PDF source served raw; everything else
    converted to PDF via calibre on first hit + cached on disk.
    Browser's native PDF reader handles rendering in an <iframe>.
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT local_path, file_name, file_size FROM messages WHERE id=$1", msg_id)
    if not row or not row["local_path"] or row["local_path"].startswith("__"):
        return Response("not cached — open via /watch to trigger download",
                        status_code=404)
    ext = (row["file_name"] or "").rsplit(".", 1)[-1].lower()
    src, pdf, _ = _ebook_paths(row["local_path"])
    if not os.path.exists(src):
        return Response("source file missing", status_code=410)
    if row["file_size"] and os.path.getsize(src) < row["file_size"]:
        return Response("still downloading — retry in a moment", status_code=202)
    await _touch_local(msg_id)
    # .pdf source: serve directly, no conversion
    if ext == "pdf":
        return FileResponse(src, media_type="application/pdf",
                            headers={"Content-Disposition": "inline",
                                     "Cache-Control": "private, max-age=3600"})
    if ext not in _EBOOK_CONVERTIBLE:
        return Response(f"format .{ext} not supported by ebook-convert",
                        status_code=415)
    if not _ebook_safe_to_convert(src, ext):
        return Response("file rejected: zip-bomb or malformed archive", status_code=422)
    ok = await _ebook_to_pdf(src, pdf, msg_id=msg_id)
    if not ok:
        return Response("conversion failed", status_code=500)
    return FileResponse(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": "inline",
                                 "Cache-Control": "private, max-age=86400"})


@app.get("/ebook-cover/{msg_id}")
async def ebook_cover_unified(msg_id: int):
    """Cover thumb from any cached ebook (epub/mobi/azw/azw3/pdf/...)."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT local_path, file_name FROM messages WHERE id=$1", msg_id)
    if not row or not row["local_path"] or row["local_path"].startswith("__"):
        return Response("not cached", status_code=404)
    src, _, cover = _ebook_paths(row["local_path"])
    if not os.path.exists(src):
        return Response("source missing", status_code=410)
    ok = await _ebook_cover(src, cover)
    if not ok:
        return Response("no cover", status_code=404)
    return FileResponse(cover, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


# ════════════════════════════════════════════════════════════════════
# E-book reader
#
# /epub/{id}          the reader: our page, our script. Pages you turn with
#                     buttons, arrow keys, the wheel or a swipe; contents;
#                     font size; light / sepia / dark; it remembers where you
#                     stopped.
# /epub/{id}/toc      the book's reading order and chapter titles (JSON)
# /epub/{id}/ch/{n}   one chapter, as a bare document
#
# The book itself is somebody else's HTML. It is shown one chapter at a time
# (a 2,000-chapter web novel must not become one 40 MB page) inside a
# sandboxed frame, and served with a Content-Security-Policy that allows no
# script at all - so nothing inside a book can run, whatever the tag
# stripping below misses. The reader lays the chapter out in columns from the
# outside.
# ════════════════════════════════════════════════════════════════════
_EPUB_CSP = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
             "font-src 'self' data:")
_EPUB_INDEX_CACHE: dict = {}      # path -> (mtime, index)


def _epub_index(epub_path: str) -> dict:
    """Reading order + chapter titles of a book. Cached per file."""
    mtime = os.path.getmtime(epub_path)
    hit = _EPUB_INDEX_CACHE.get(epub_path)
    if hit and hit[0] == mtime:
        return hit[1]
    from xml.etree import ElementTree as _ET
    with _zipfile.ZipFile(epub_path) as zf:
        container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
        ns_c = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
        opf_node = _ET.fromstring(container).find(".//c:rootfile", ns_c)
        if opf_node is None:
            raise RuntimeError("OPF not declared in container.xml")
        opf_path = opf_node.attrib["full-path"]
        opf_dir = os.path.dirname(opf_path).replace("\\", "/")
        opf_root = _ET.fromstring(zf.read(opf_path).decode("utf-8", "replace"))
        ns_opf = {"opf": "http://www.idpf.org/2007/opf"}

        def _in_zip(base_dir, href):
            href = _uq(href.split("#", 1)[0])
            path = (base_dir + "/" + href) if base_dir else href
            return os.path.normpath(path).replace("\\", "/").lstrip("/")

        items = {}
        for item in opf_root.findall(".//opf:manifest/opf:item", ns_opf):
            items[item.attrib.get("id")] = item.attrib
        chapters = []
        for ref in opf_root.findall(".//opf:spine/opf:itemref", ns_opf):
            it = items.get(ref.attrib.get("idref"))
            if it and it.get("href"):
                chapters.append(_in_zip(opf_dir, it["href"]))
        names = set(zf.namelist())
        chapters = [c for c in chapters if c in names]

        # titles: EPUB 3 nav document, then the EPUB 2 NCX
        titles = {}

        def _clean(t):
            return html.unescape(_re.sub(r"<[^>]+>", "", t or "")).strip()[:200]
        for it in items.values():
            href = it.get("href") or ""
            try:
                if "nav" in (it.get("properties") or "").split():
                    nav_path = _in_zip(opf_dir, href)
                    nav = zf.read(nav_path).decode("utf-8", "replace")
                    nav_dir = os.path.dirname(nav_path)
                    for m in _re.finditer(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                                          nav, _re.DOTALL | _re.IGNORECASE):
                        titles.setdefault(_in_zip(nav_dir, m.group(1)), _clean(m.group(2)))
                elif it.get("media-type") == "application/x-dtbncx+xml":
                    ncx_path = _in_zip(opf_dir, href)
                    ncx = zf.read(ncx_path).decode("utf-8", "replace")
                    ncx_dir = os.path.dirname(ncx_path)
                    for m in _re.finditer(
                            r"<navLabel>\s*<text>(.*?)</text>\s*</navLabel>\s*"
                            r"<content\b[^>]*\bsrc=[\"']([^\"']+)[\"']",
                            ncx, _re.DOTALL | _re.IGNORECASE):
                        titles.setdefault(_in_zip(ncx_dir, m.group(2)), _clean(m.group(1)))
            except (KeyError, ValueError):
                continue
        # a book without a usable table of contents: the first heading of each chapter
        if chapters and sum(1 for c in chapters if titles.get(c)) * 2 < len(chapters) \
                and len(chapters) <= 3000:
            for c in chapters:
                if titles.get(c):
                    continue
                try:
                    head = zf.read(c)[:6000].decode("utf-8", "replace")
                except KeyError:
                    continue
                m = (_re.search(r"<h[1-3][^>]*>(.*?)</h[1-3]>", head, _re.DOTALL | _re.IGNORECASE)
                     or _re.search(r"<title[^>]*>(.*?)</title>", head, _re.DOTALL | _re.IGNORECASE))
                if m and _clean(m.group(1)):
                    titles[c] = _clean(m.group(1))
        book_title = None
        t = opf_root.find(".//{http://purl.org/dc/elements/1.1/}title")
        if t is not None and (t.text or "").strip():
            book_title = t.text.strip()[:200]
    index = {"title": book_title, "chapters": chapters,
             "titles": [titles.get(c) or "" for c in chapters]}
    if len(_EPUB_INDEX_CACHE) > 32:
        _EPUB_INDEX_CACHE.clear()
    _EPUB_INDEX_CACHE[epub_path] = (mtime, index)
    return index


def _epub_chapter_body(zf, msg_id: int, chapter_path: str) -> str:
    """The <body> of one chapter, made safe to show: references rewritten to
    /epub/{id}/asset/..., style sheets, scripts and event handlers removed."""
    raw_html = zf.read(chapter_path).decode("utf-8", errors="replace")
    m = _re.search(r"<body[^>]*>(.*?)</body>", raw_html, _re.DOTALL | _re.IGNORECASE)
    body_html = m.group(1) if m else raw_html
    chapter_dir = os.path.dirname(chapter_path).replace("\\", "/")

    def fix_ref(match):
        src = _uq(match.group(1))
        if src.startswith(("http://", "https://", "data:", "/")):
            return match.group(0)
        abs_in_zip = os.path.normpath(os.path.join(chapter_dir, src)).replace("\\", "/")
        return match.group(0).replace(match.group(1), f"/epub/{msg_id}/asset/{_q(abs_in_zip)}")

    body_html = _re.sub(r"<img[^>]*\bsrc=[\"\']([^\"\']+)[\"\']", fix_ref, body_html)
    body_html = _re.sub(r"<image[^>]*\b(?:xlink:)?href=[\"\']([^\"\']+)[\"\']", fix_ref, body_html)
    body_html = _re.sub(r"<link[^>]*\brel=[\"\']stylesheet[\"\'][^>]*/?>", "",
                        body_html, flags=_re.IGNORECASE)
    body_html = _re.sub(r"<(script|style|iframe|object|embed)\b[^>]*>.*?</\1>", "",
                        body_html, flags=_re.DOTALL | _re.IGNORECASE)
    body_html = _re.sub(r"<(script|iframe|object|embed|base|meta)\b[^>]*/?>", "",
                        body_html, flags=_re.IGNORECASE)
    body_html = _re.sub(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|\'[^\']*\'|[^\s>]+)", "",
                        body_html, flags=_re.IGNORECASE)
    # links inside a book point at other files of the book: they would leave
    # the reader. Keep the text, drop the jump.
    body_html = _re.sub(r"(<a\b[^>]*?)\shref=(\"[^\"]*\"|\'[^\']*\')", r"\1", body_html,
                        flags=_re.IGNORECASE)
    # an EPUB cover is often <svg width="100%" height="100%">: let the reader size it
    body_html = _re.sub(r"(<svg[^>]*?)\s(?:width|height)=[\"\'][^\"\']*[\"\']",
                        r"\1", body_html, count=2, flags=_re.IGNORECASE)
    body_html = _re.sub(r"(<image[^>]*?)\s(?:width|height)=[\"\'][^\"\']*[\"\']",
                        r"\1", body_html, count=2, flags=_re.IGNORECASE)
    return body_html


_EPUB_CHAPTER_CSS = (
    "html,body{margin:0;padding:0}"
    "body{font-family:'PingFang SC','Microsoft YaHei','Noto Sans CJK SC','Hiragino Sans GB',"
    "Georgia,system-ui,sans-serif;line-height:1.9;word-break:break-word}"
    "h1,h2,h3,h4{font-weight:600;margin:1.2em 0 .6em;line-height:1.4}"
    "h1{font-size:1.5em}h2{font-size:1.3em}h3{font-size:1.12em}"
    "p{margin:.9em 0;text-indent:2em}"
    "img{max-width:100%;height:auto;display:block;margin:1.2em auto;border-radius:4px}"
    "svg{max-width:100%;height:auto;display:block;margin:0 auto 1.5em}"
    "svg image{width:100%;height:auto}"
    "blockquote{margin:1.2em 0;padding:.3em 1.2em;border-left:4px solid rgba(120,140,170,.5);"
    "opacity:.85}"
    "table{max-width:100%}pre{white-space:pre-wrap}"
    "sup,sub{font-size:.65em;line-height:0}"
)


def _epub_row_sql():
    return "SELECT local_path, file_name FROM messages WHERE id=$1"


@app.get("/epub/{msg_id}/toc")
async def epub_toc(msg_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(_epub_row_sql(), msg_id)
    epub_path = _epub_locate(msg_id, row)
    if not epub_path:
        return JSONResponse({"error": "not cached"}, status_code=404)
    try:
        idx = await asyncio.to_thread(_epub_index, epub_path)
    except Exception as e:
        return JSONResponse({"error": f"cannot read this book: {e}"}, status_code=422)
    return JSONResponse({"title": idx["title"] or row["file_name"] or f"book-{msg_id}",
                         "chapters": idx["titles"]})


@app.get("/epub/{msg_id}/ch/{n}", response_class=HTMLResponse)
async def epub_chapter(msg_id: int, n: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(_epub_row_sql(), msg_id)
    epub_path = _epub_locate(msg_id, row)
    if not epub_path:
        return HTMLResponse("not cached", status_code=404)
    try:
        idx = await asyncio.to_thread(_epub_index, epub_path)
        if not 0 <= n < len(idx["chapters"]):
            return HTMLResponse("no such chapter", status_code=404)

        def _read():
            with _zipfile.ZipFile(epub_path) as zf:
                return _epub_chapter_body(zf, msg_id, idx["chapters"][n])
        body = await asyncio.to_thread(_read)
    except Exception as e:
        return HTMLResponse(f"<p>cannot show this chapter: {html.escape(str(e))}</p>",
                            status_code=500, headers={"Content-Security-Policy": _EPUB_CSP})
    await _touch_local(msg_id)
    return HTMLResponse(
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_EPUB_CHAPTER_CSS}</style></head><body>{body}</body></html>",
        headers={"Content-Security-Policy": _EPUB_CSP,
                 "X-Content-Type-Options": "nosniff",
                 "Cache-Control": "private, max-age=3600"})


# What both readers need to read aloud: the browser's own voices (Edge offers
# Microsoft's natural ones), a voice chosen for the text's language, the text
# cut into short pieces (some voices stop on long passages), and a toast.
_SPEECH_JS = r"""
const synth = window.speechSynthesis;
function toast(text, ms){
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; document.body.appendChild(t); }
  t.textContent = text; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => { t.style.display = "none"; }, ms || 4000);
}
function voiceFor(text){
  const cjk = (text.match(/[一-鿿]/g) || []).length, kana = (text.match(/[぀-ヿ]/g) || []).length,
        hangul = (text.match(/[가-힯]/g) || []).length;
  const lang = kana > 10 ? "ja" : hangul > 10 ? "ko" : cjk > text.length * 0.15 ? "zh" : (navigator.language || "en").slice(0, 2);
  const vs = synth.getVoices().filter(v => v.lang && v.lang.toLowerCase().startsWith(lang));
  const score = v => (/natural|neural/i.test(v.name) ? 8 : 0) + (/online/i.test(v.name) ? 4 : 0) + (/microsoft|google/i.test(v.name) ? 2 : 0)
    + (lang === "zh" && /zh[-_]cn|hans/i.test(v.lang) ? 1 : 0);
  return {voice: vs.sort((a, b) => score(b) - score(a))[0] || null, lang: lang === "zh" ? "zh-CN" : lang};
}
function chunks(text){
  // sentences, packed into pieces of about 200 characters: some voices stop on long passages
  const out = []; let buf = "";
  (text.match(/[^.!?。！？；;\n]+[.!?。！？；;\n]*/g) || [text]).forEach(s => {
    if (buf && (buf + s).length > 200) { out.push(buf); buf = ""; }
    buf += s;
    while (buf.length > 260) { out.push(buf.slice(0, 240)); buf = buf.slice(240); }
  });
  if (buf.trim()) out.push(buf);
  return out.map(x => x.trim()).filter(Boolean);
}
"""

_EPUB_READER_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box}
html,body{margin:0;height:100%;overflow:hidden;font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif}
body{display:flex;flex-direction:column;background:var(--bg);color:var(--fg);
  --bg:#ffffff;--fg:#1f2937;--bar:#f8fafc;--line:#e2e8f0;--mut:#64748b;--acc:#2563eb}
body[data-theme=sepia]{--bg:#f6efdc;--fg:#433422;--bar:#efe6cf;--line:#dccfae;--mut:#8a7a5c;--acc:#a0522d}
body[data-theme=dark]{--bg:#15181d;--fg:#c9d1d9;--bar:#1c2128;--line:#30363d;--mut:#8b949e;--acc:#58a6ff}
.bar{display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--bar);
  border-bottom:1px solid var(--line);flex:none;min-height:50px}
.bar.bottom{border-bottom:none;border-top:1px solid var(--line)}
.ttl{flex:1;min-width:0;font-weight:600;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
button,.btn{font:inherit;font-size:14px;color:var(--fg);background:transparent;border:1px solid var(--line);
  border-radius:8px;padding:7px 12px;cursor:pointer;text-decoration:none;white-space:nowrap;line-height:1.2}
button:hover,.btn:hover{border-color:var(--acc);color:var(--acc)}
button:disabled{opacity:.35;cursor:default;border-color:var(--line);color:var(--fg)}
button.big{font-size:15px;font-weight:600;padding:9px 18px;background:var(--acc);color:#fff;border-color:var(--acc)}
button.big:hover{color:#fff;filter:brightness(1.1)}
button.big:disabled{background:transparent;color:var(--fg);border-color:var(--line)}
.stage{flex:1;position:relative;min-height:0}
iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:var(--bg)}
.zone{position:absolute;top:0;bottom:0;width:16%;z-index:2;cursor:pointer;display:flex;align-items:center;
  font-size:44px;color:var(--mut);opacity:0;transition:opacity .15s;user-select:none}
.zone:hover{opacity:.55}
.zone.l{left:0;justify-content:flex-start;padding-left:10px}
.zone.r{right:0;justify-content:flex-end;padding-right:10px}
body[data-mode=scroll] .zone{display:none}
.pos{flex:1;min-width:0;text-align:center;font-size:13px;color:var(--mut)}
.pos b{color:var(--fg);font-weight:600}
.pos .chn{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
input[type=range]{width:100%;accent-color:var(--acc);margin:2px 0 0}
.drawer{position:absolute;top:0;bottom:0;left:0;width:min(360px,86vw);background:var(--bar);
  border-right:1px solid var(--line);z-index:5;transform:translateX(-102%);transition:transform .18s;
  display:flex;flex-direction:column;box-shadow:4px 0 18px rgba(0,0,0,.12)}
.drawer.open{transform:none}
.drawer h3{margin:0;padding:14px 16px;font-size:15px;border-bottom:1px solid var(--line);display:flex;
  justify-content:space-between;align-items:center}
.drawer .list{overflow:auto;flex:1;padding:6px 0}
.drawer a{display:block;padding:9px 16px;font-size:14px;color:var(--fg);text-decoration:none;cursor:pointer;
  border-left:3px solid transparent}
.drawer a:hover{background:rgba(127,127,127,.12)}
.drawer a.cur{border-left-color:var(--acc);color:var(--acc);font-weight:600}
.shade{position:absolute;inset:0;z-index:4;background:rgba(0,0,0,.25);display:none}
.shade.open{display:block}
.msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--mut);
  z-index:1;pointer-events:none;font-size:15px}
.toast{position:fixed;left:50%;bottom:76px;transform:translateX(-50%);background:rgba(15,23,42,.9);color:#fff;
  font-size:13px;padding:8px 14px;border-radius:8px;z-index:20;max-width:86vw;text-align:center;pointer-events:none}
button.on{background:var(--acc,#2563eb);color:#fff;border-color:var(--acc,#2563eb)}
.tip{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);background:rgba(15,23,42,.88);color:#fff;
  padding:9px 16px;border-radius:20px;font-size:13px;z-index:6;pointer-events:none;transition:opacity .6s}
@media (max-width:640px){.hide-s{display:none}.bar{gap:5px;padding:6px 8px}button,.btn{padding:7px 9px}}
</style></head>
<body data-theme="light" data-mode="paged">
<div class="bar">
  <a class="btn" id="back" href="/library" title="back to the library">&larr;<span class="hide-s"> Library</span></a>
  <button id="tocBtn" title="table of contents">&#9776;<span class="hide-s"> Contents</span></button>
  <div class="ttl" id="ttl">__TITLE__</div>
  <button id="readBtn" title="read aloud from here; the page follows the voice (R)">&#128266;<span class="hide-s"> Read aloud</span></button>
  <button id="twoBtn" class="hide-s" title="two pages side by side, like an open book (2)">2 pages</button>
  <button id="fsDown" title="smaller text">A&minus;</button>
  <button id="fsUp" title="larger text">A+</button>
  <button id="themeBtn" title="light / sepia / dark">&#9680;<span class="hide-s"> Theme</span></button>
  <button id="modeBtn" title="turn pages or scroll"><span id="modeLbl">Scroll</span></button>
  <a class="btn hide-s" href="/media/__ID__" download title="download the file">&#11015;</a>
</div>
<div class="stage" id="stage">
  <div class="msg" id="msg">opening the book…</div>
  <iframe id="bk" sandbox="allow-same-origin" title="book"></iframe>
  <div class="zone l" id="zl" title="previous page">&lsaquo;</div>
  <div class="zone r" id="zr" title="next page">&rsaquo;</div>
  <div class="shade" id="shade"></div>
  <div class="drawer" id="drawer">
    <h3><span>Contents</span><button id="tocClose">&times;</button></h3>
    <div class="list" id="tocList"></div>
  </div>
  <div class="tip" id="tip">&larr; &rarr; or Space to turn pages &middot; click the edges &middot; swipe on a phone</div>
</div>
<div class="bar bottom">
  <button class="big" id="prev">&lsaquo; Prev</button>
  <div class="pos">
    <span class="chn" id="chn"></span>
    <span id="pg"></span>
    <input type="range" id="slider" min="0" max="0" value="0" title="jump to a chapter">
  </div>
  <button class="big" id="next">Next &rsaquo;</button>
</div>
<script>
(function(){
const ID = __ID__, KEY = "tgarr.read." + ID;
const $ = id => document.getElementById(id);
const fr = $("bk");
let titles = [], ch = 0, page = 0, pages = 1, W = 0, doc = null, pendingFrac = 0, busy = false;
let st = {}; try { st = JSON.parse(localStorage.getItem("tgarr.reader") || "{}"); } catch (e) {}
let fs = st.fs || 18, theme = st.theme || "light", mode = st.mode || "paged", two = !!st.two;
const calm = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
// two pages only where there is room for them
const spread = () => two && mode === "paged" && W >= 900;
// turning a page: the text slides sideways, the new page coming in as the old one leaves
function slide(){
  const x = page * W;
  if (calm) { doc.body.scrollLeft = x; return; }
  try { doc.body.scrollTo({left: x, behavior: "smooth"}); } catch (e) { doc.body.scrollLeft = x; }
}
const THEMES = {light: ["#ffffff", "#1f2937"], sepia: ["#f6efdc", "#433422"], dark: ["#15181d", "#c9d1d9"]};
function saveSt(){ try { localStorage.setItem("tgarr.reader", JSON.stringify({fs, theme, mode, two})); } catch (e) {} }
function frac(){
  if (mode === "paged") return pages > 1 ? page / (pages - 1) : 0;
  const se = doc && doc.scrollingElement; if (!se) return 0;
  const max = se.scrollHeight - se.clientHeight; return max > 0 ? se.scrollTop / max : 0;
}
function savePos(){ try { localStorage.setItem(KEY, JSON.stringify({ch, frac: frac()})); } catch (e) {} }
function css(){
  const c = THEMES[theme];
  let s = "html{background:" + c[0] + "}body{background:" + c[0] + ";color:" + c[1] + ";font-size:" + fs + "px}" +
          ".tg-reading{background:rgba(250,204,21," + (theme === "dark" ? ".22" : ".32") + ");border-radius:3px}";
  if (mode === "paged") {
    // one screen = one column with a margin P either side; or two columns
    // with the same 2P between them as between screens, so a screen is
    // always exactly W wide whichever it is
    const col = Math.min(W - 44, 860);
    const P = spread() ? Math.max(24, Math.round(W * 0.035)) : Math.max(22, Math.round((W - col) / 2));
    const cw = spread() ? Math.floor((W - 4 * P) / 2) : W - 2 * P;
    s += "html{height:100%;overflow:hidden}" +
         "body{margin:0;height:100vh;box-sizing:border-box;padding:26px " + P + "px;column-width:" + cw +
         "px;column-gap:" + (2 * P) + "px;column-fill:auto;overflow:hidden}" +
         "img,svg{max-height:calc(100vh - 90px);width:auto;max-width:100%;break-inside:avoid}" +
         "h1,h2,h3,h4{break-after:avoid}";
  } else {
    s += "body{max-width:820px;margin:0 auto;padding:26px 22px 80px}";
  }
  return s;
}
function layout(keep){
  if (!doc || !doc.body) return;
  W = fr.clientWidth;
  let el = doc.getElementById("tg-reader");
  if (!el) { el = doc.createElement("style"); el.id = "tg-reader"; doc.head.appendChild(el); }
  el.textContent = css();
  document.body.dataset.theme = theme; document.body.dataset.mode = mode;
  $("modeLbl").textContent = mode === "paged" ? "Scroll" : "Pages";
  $("twoBtn").classList.toggle("on", two); $("twoBtn").disabled = mode !== "paged";
  if (mode === "paged") {
    pages = Math.max(1, Math.round(doc.body.scrollWidth / W));
    page = Math.min(pages - 1, Math.max(0, Math.round(keep * (pages - 1))));
    doc.body.scrollLeft = page * W;
  } else {
    const se = doc.scrollingElement, max = se.scrollHeight - se.clientHeight;
    se.scrollTop = keep * Math.max(0, max);
  }
  ui();
}
function ui(){
  const n = titles.length || 1;
  $("chn").textContent = titles[ch] || ("Section " + (ch + 1));
  let within = frac();
  if (mode === "paged") $("pg").innerHTML = "page <b>" + (page + 1) + "</b> of " + pages + " &middot; chapter " + (ch + 1) + " of " + n +
      " &middot; <b>" + Math.round((ch + (pages > 1 ? page / pages : 0)) / n * 100) + "%</b>";
  else $("pg").innerHTML = "chapter " + (ch + 1) + " of " + n + " &middot; <b>" + Math.round((ch + within) / n * 100) + "%</b>";
  $("slider").max = n - 1; $("slider").value = ch;
  const first = ch === 0 && (mode === "paged" ? page === 0 : within <= 0.001);
  const last = ch >= n - 1 && (mode === "paged" ? page >= pages - 1 : within >= 0.999);
  $("prev").disabled = first; $("next").disabled = last;
  document.querySelectorAll("#tocList a").forEach(a => a.classList.toggle("cur", +a.dataset.i === ch));
  savePos();
}
function load(n, f){
  n = Math.max(0, Math.min((titles.length || 1) - 1, n));
  ch = n; pendingFrac = f || 0; busy = true;
  $("msg").style.display = "flex"; $("msg").textContent = "loading…";
  // replace, not assign: setting src made every chapter a step in the tab's
  // history, so "Library" (one step back) only went back one chapter
  const url = "/epub/" + ID + "/ch/" + n;
  try { fr.contentWindow.location.replace(url); } catch (e) { fr.src = url; }
}
function go(d){
  if (busy || !doc) return;
  hideTip(); userMoved();
  if (mode === "paged") {
    if (page + d >= 0 && page + d < pages) { page += d; slide(); ui(); return; }
  } else {
    const se = doc.scrollingElement, max = se.scrollHeight - se.clientHeight, step = se.clientHeight * 0.9;
    if ((d > 0 && se.scrollTop < max - 2) || (d < 0 && se.scrollTop > 2)) { se.scrollTop += d * step; setTimeout(ui, 60); return; }
  }
  if (d > 0 && ch < titles.length - 1) load(ch + 1, 0);
  else if (d < 0 && ch > 0) load(ch - 1, 1);
}
function onKey(e){
  if (e.target && /INPUT|TEXTAREA/.test(e.target.tagName) && e.target.type !== "range") return;
  const k = e.key;
  if (k === "ArrowRight" || k === "PageDown" || (k === " " && !e.shiftKey) || k === "ArrowDown" && mode === "paged") { e.preventDefault(); go(1); }
  else if (k === "ArrowLeft" || k === "PageUp" || (k === " " && e.shiftKey) || k === "ArrowUp" && mode === "paged") { e.preventDefault(); go(-1); }
  else if (k === "Home") { e.preventDefault(); userMoved(); load(0, 0); }
  else if (k === "End") { e.preventDefault(); userMoved(); load(titles.length - 1, 1); }
  else if (k === "Escape") toc(false);
  else if ((k === "r" || k === "R") && !e.ctrlKey && !e.metaKey) $("readBtn").click();
  else if (k === "2") $("twoBtn").click();
}
let wheelAt = 0;
function onWheel(e){
  if (mode !== "paged") { setTimeout(ui, 80); return; }
  e.preventDefault();
  const now = Date.now(); if (now - wheelAt < 260) return;
  if (Math.abs(e.deltaY) < 8 && Math.abs(e.deltaX) < 8) return;
  wheelAt = now; go((Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX) > 0 ? 1 : -1);
}
let tx = null, ty = null;
function onTouchStart(e){ const t = e.changedTouches[0]; tx = t.clientX; ty = t.clientY; }
function onTouchEnd(e){
  if (tx === null) return; const t = e.changedTouches[0], dx = t.clientX - tx, dy = t.clientY - ty; tx = null;
  if (mode === "paged" && Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.5) go(dx < 0 ? 1 : -1);
  else if (mode !== "paged") setTimeout(ui, 120);
}
function wire(target){
  try {
    target.addEventListener("keydown", onKey);
    target.addEventListener("wheel", onWheel, {passive: false});
    target.addEventListener("touchstart", onTouchStart, {passive: true});
    target.addEventListener("touchend", onTouchEnd, {passive: true});
  } catch (e) {}
}
fr.addEventListener("load", function(){
  try { doc = fr.contentDocument; } catch (e) { doc = null; }
  if (!doc || !doc.body) return;
  busy = false; $("msg").style.display = "none";
  wire(doc);
  doc.addEventListener("scroll", () => { if (mode !== "paged") ui(); }, {passive: true});
  layout(pendingFrac);
  if (reading) {
    // the voice asked for this chapter: start at its top. A person did: read what is in view.
    if (readerLoad) { readerLoad = false; blocks = collect(); if (blocks.length) readBlock(0); else readFromView(); }
    else readFromView();
  }
  readerLoad = false;
  // pictures arriving change the page count: lay out again, stay where we are
  let t = null;
  Array.from(doc.images || []).forEach(im => { if (!im.complete) im.addEventListener("load", () => {
    clearTimeout(t); t = setTimeout(() => layout(frac()), 120); }); });
});
wire(window);
window.addEventListener("resize", () => { if (doc) layout(frac()); });
$("prev").onclick = () => go(-1); $("next").onclick = () => go(1);
$("zl").onclick = () => go(-1); $("zr").onclick = () => go(1);
$("fsUp").onclick = () => { fs = Math.min(30, fs + 2); saveSt(); layout(frac()); };
$("fsDown").onclick = () => { fs = Math.max(12, fs - 2); saveSt(); layout(frac()); };
$("themeBtn").onclick = () => { theme = theme === "light" ? "sepia" : theme === "sepia" ? "dark" : "light"; saveSt(); layout(frac()); };
$("modeBtn").onclick = () => { const f = frac(); mode = mode === "paged" ? "scroll" : "paged"; saveSt(); layout(f); };
$("twoBtn").onclick = () => { const f = frac(); two = !two; saveSt(); layout(f); };
$("slider").addEventListener("change", e => { userMoved(); load(+e.target.value, 0); });
// ---- read aloud: paragraph by paragraph, the page following the voice ----
__SPEECH_JS__
let reading = false, readRun = 0, blocks = [], readerLoad = false, moveT = null;
const BLOCKS = "p,h1,h2,h3,h4,h5,h6,li,blockquote,dd,dt,td,th,pre,figcaption";
function collect(){
  if (!doc || !doc.body) return [];
  const all = Array.from(doc.body.querySelectorAll(BLOCKS)).filter(el =>
    (el.textContent || "").trim() && !Array.from(el.querySelectorAll(BLOCKS)).some(k => (k.textContent || "").trim()));
  return all.length ? all : ((doc.body.textContent || "").trim() ? [doc.body] : []);
}
// the pages a paragraph starts and ends on: one that runs over a page break
// has a piece in each column
function pagesOf(el){
  // Measured against the first thing in the chapter, not against how far the
  // text is scrolled: while a page is sliding the two do not add up, and a
  // paragraph on this page was taken for one on the next.
  const first = doc.body.firstElementChild || el, ref = first.getClientRects()[0], rs = el.getClientRects();
  if (!ref || !rs.length) return [page, page];
  // columns start a whole screen apart (half a screen with two pages)
  const at = x => Math.max(0, Math.min(pages - 1, Math.floor((x - ref.left + W * 0.2) / W)));
  return [at(rs[0].left), at(rs[rs.length - 1].left)];
}
function turnTo(p){ if (p !== page) { page = p; slide(); ui(); } }
function firstInView(){
  for (let i = 0; i < blocks.length; i++) {
    // on this page or after it - a paragraph that began on the page before
    // and ends on this one counts
    if (mode === "paged" ? pagesOf(blocks[i])[1] >= page : blocks[i].getBoundingClientRect().bottom > 40) return i;
  }
  return blocks.length;
}
function unmark(){ if (doc) doc.querySelectorAll(".tg-reading").forEach(x => x.classList.remove("tg-reading")); }
function stopRead(why){
  reading = false; readRun++; clearTimeout(moveT); if (synth) synth.cancel(); unmark();
  $("readBtn").classList.remove("on"); if (why) toast(why, 6000);
}
function readBlock(i){
  if (!reading) return;
  const run = ++readRun; synth.cancel(); unmark();
  if (i >= blocks.length) {
    if (ch >= titles.length - 1) { stopRead("the end of the book"); return; }
    readerLoad = true; load(ch + 1, 0); return;          // goes on when the chapter has arrived
  }
  const el = blocks[i], text = (el.textContent || "").replace(/\s+/g, " ").trim();
  if (el !== doc.body) el.classList.add("tg-reading");
  // show the paragraph - unless part of it is already on screen (do not pull
  // the page back to where it began)
  const span = mode === "paged" && el !== doc.body ? pagesOf(el) : null;
  if (span) { if (page < span[0] || page > span[1]) turnTo(span[0]); }
  else if (el !== doc.body) el.scrollIntoView({block: "center", behavior: "smooth"});
  const parts = chunks(text), v = voiceFor(text);
  if (!parts.length) { readBlock(i + 1); return; }
  parts.forEach((part, k) => {
    const u = new SpeechSynthesisUtterance(part);
    if (v.voice) u.voice = v.voice; u.lang = v.voice ? v.voice.lang : v.lang;
    // a paragraph that runs over the page: turn when the voice gets that far
    if (span && span[1] > span[0]) u.onstart = () => {
      if (!reading || run !== readRun) return;
      const want = span[0] + Math.min(span[1] - span[0], Math.floor(k / parts.length * (span[1] - span[0] + 1)));
      if (want > page) turnTo(want);
    };
    if (k === parts.length - 1) u.onend = () => { if (reading && run === readRun) readBlock(i + 1); };
    u.onerror = e => { if (e.error !== "canceled" && e.error !== "interrupted" && run === readRun) stopRead("the browser could not read this aloud"); };
    synth.speak(u);
  });
}
function readFromView(){
  if (!reading || busy || !doc) return;        // a chapter on its way: its arrival starts the reading
  blocks = collect();
  if (!blocks.length) { if (ch < titles.length - 1) { readerLoad = true; load(ch + 1, 0); } else stopRead("nothing to read here"); return; }
  // nothing readable on this page or after it (a picture, a blank end): readBlock
  // then goes on to the next chapter - never back to an earlier paragraph
  readBlock(firstInView());
}
// a person turned the page or jumped: read on from what is now in view
function userMoved(){ if (!reading) return; readRun++; synth.cancel(); clearTimeout(moveT); moveT = setTimeout(readFromView, 450); }
$("readBtn").onclick = () => {
  if (!synth) { toast("this browser cannot read aloud"); return; }
  if (reading) { stopRead(); return; }
  reading = true; $("readBtn").classList.add("on"); readFromView();
};
if (!synth) $("readBtn").style.display = "none";
else { synth.getVoices(); window.addEventListener("beforeunload", () => synth.cancel()); }
function toc(open){ $("drawer").classList.toggle("open", open); $("shade").classList.toggle("open", open);
  if (open) { const c = document.querySelector("#tocList a.cur"); if (c) c.scrollIntoView({block: "center"}); } }
$("tocBtn").onclick = () => toc(!$("drawer").classList.contains("open"));
$("tocClose").onclick = () => toc(false); $("shade").onclick = () => toc(false);
function hideTip(){ const t = $("tip"); if (t) { t.style.opacity = 0; setTimeout(() => t.remove(), 700); } }
setTimeout(hideTip, 7000);
// "back": players open in a new tab, where there is no page to go back to.
// Go back if there is one; otherwise close the tab (the list is still open
// behind it); if the browser will not close it, go to the list.
// A link followed inside the book still adds a step, so count the steps
// from where the reader opened and go back over all of them at once.
let startLen = history.length;
try {
  const k = "tgarr.start." + ID, s = +sessionStorage.getItem(k);
  const nav = (performance.getEntriesByType("navigation")[0] || {}).type;
  if (nav === "reload" && s && s <= history.length) startLen = s;
  else sessionStorage.setItem(k, startLen);
} catch (e) {}
function tgBack(fallback){
  if (history.length > 1 && document.referrer && document.referrer.indexOf(location.host) > 0) {
    history.go(-(1 + Math.max(0, history.length - startLen))); return;
  }
  window.close();
  setTimeout(function(){ location.href = fallback; }, 200);
}
$("back").onclick = e => { e.preventDefault(); tgBack("/library"); };
document.body.dataset.theme = theme;
fetch("/epub/" + ID + "/toc").then(r => r.json()).then(j => {
  if (j.error) { $("msg").textContent = "✗ " + j.error; return; }
  titles = j.chapters || [];
  if (j.title) { $("ttl").textContent = j.title; document.title = j.title; }
  const list = $("tocList"), fragm = document.createDocumentFragment();
  let listed = 0;
  titles.forEach((t, i) => { if (!t) return; listed++;
    const a = document.createElement("a"); a.textContent = t; a.dataset.i = i;
    a.onclick = () => { toc(false); userMoved(); load(i, 0); }; fragm.appendChild(a); });
  if (!listed) titles.forEach((t, i) => { const a = document.createElement("a"); a.textContent = "Section " + (i + 1);
    a.dataset.i = i; a.onclick = () => { toc(false); load(i, 0); }; fragm.appendChild(a); });
  list.appendChild(fragm);
  let pos = {}; try { pos = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
  // /epub/123#40 opens chapter 40
  const h = parseInt((location.hash || "").slice(1), 10);
  if (h >= 1 && h <= titles.length) pos = {ch: h - 1, frac: 0};
  load(pos.ch || 0, pos.frac || 0);
}).catch(() => { $("msg").textContent = "✗ could not open this book"; });
})();
</script>
<script src="/mc/ping.js" defer></script>
</body></html>"""


@app.get("/epub/{msg_id}", response_class=HTMLResponse)
async def epub_render(msg_id: int):
    """The e-book reader (see the block comment above)."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(_epub_row_sql(), msg_id)
    if not _epub_locate(msg_id, row):
        return HTMLResponse(
            f'<p>This book is not downloaded yet. <a href="/watch/{msg_id}">Open it</a>.</p>',
            status_code=404)
    title = _re.sub(r"\.epub$", "", row["file_name"] or f"book-{msg_id}", flags=_re.IGNORECASE)
    return HTMLResponse(_EPUB_READER_HTML.replace("__SPEECH_JS__", _SPEECH_JS).replace("__ID__", str(int(msg_id)))
                        .replace("__TITLE__", html.escape(title)))


# ════════════════════════════════════════════════════════════════════
# PDF reader
#
# /pdf/{id}                 the reader (same controls as the e-book reader)
# /pdf/{id}/info            page count and shape
# /pdf/{id}/page/{n}        one page as a picture (?w=width&clean=1)
#
# Pages are drawn on the server (poppler) and sent as pictures, so the reader
# is the same on every browser and phone, and so a page can be cleaned on the
# way: many PDFs passed around on Telegram carry a channel's address printed
# across every page. When the watermark is part of a scanned page there is
# nothing to delete from the file - but it is always pale and usually tinted,
# and the text is always dark. "Hide watermark" redraws the page in grey,
# turns pale and tinted pixels white, and darkens what is left so thin
# strokes survive. The file itself is never changed; "Original" opens it
# untouched in the browser's own viewer (text selection, search, printing).
# ════════════════════════════════════════════════════════════════════
_PDF_INFO_CACHE: dict = {}            # path -> (mtime, info)
_PDF_PAGE_CACHE: "collections.OrderedDict" = None
_PDF_PAGE_CACHE_MAX = 48
_PDF_RENDER_SEM = None
_PDF_WIDTHS = (800, 1000, 1200, 1400, 1600, 1800, 2000, 2400, 2800, 3200)


def _pdf_source(row):
    """The PDF to show for a library item: the file itself, or the PDF an
    e-book in another format was converted to."""
    if not row or not _is_local(row["local_path"]):
        return None
    src = os.path.join("/downloads", row["local_path"])
    if (row["local_path"] or "").lower().endswith(".pdf"):
        return src if os.path.exists(src) else None
    conv = src + ".pdf"
    return conv if os.path.exists(conv) else None


def _pdf_info(path: str) -> dict:
    import subprocess
    mtime = os.path.getmtime(path)
    hit = _PDF_INFO_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    out = subprocess.run(["pdfinfo", path], capture_output=True, timeout=30).stdout.decode(
        "utf-8", "replace")
    m = _re.search(r"^Pages:\s+(\d+)", out, _re.MULTILINE)
    if not m:
        raise RuntimeError("not a readable PDF")
    sz = _re.search(r"^Page size:\s+([\d.]+) x ([\d.]+)", out, _re.MULTILINE)
    ratio = (float(sz.group(2)) / float(sz.group(1))) if sz and float(sz.group(1)) else 1.414
    rot = _re.search(r"^Page rot:\s+(\d+)", out, _re.MULTILINE)
    if rot and int(rot.group(1)) in (90, 270):
        ratio = 1 / ratio
    info = {"pages": int(m.group(1)), "ratio": round(ratio, 4)}
    if len(_PDF_INFO_CACHE) > 64:
        _PDF_INFO_CACHE.clear()
    _PDF_INFO_CACHE[path] = (mtime, info)
    return info


def _pdf_clean(im):
    """Pale and tinted pixels to white, the rest to strong grey (see above)."""
    from PIL import ImageChops
    r, g, b = im.split()
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    tinted = ImageChops.multiply(
        ImageChops.subtract(mx, mn).point(lambda v: 255 if v > 30 else 0),
        mx.point(lambda v: 255 if v > 150 else 0))
    grey = ImageChops.lighter(mn, tinted)
    return grey.point([255 if v >= 205 else int(255 * (v / 205) ** 1.5) for v in range(256)])


def _pdf_render(path: str, n: int, width: int, clean: bool):
    """One page as a picture: (bytes, media type).

    As it is: JPEG. Cleaned: the page is drawn without any lossy step
    (PNG from poppler), filtered, sharpened a little so that strokes the
    filter thinned get their edge back, and sent as a grey PNG - text on
    white compresses well losslessly and keeps crisp edges, where JPEG
    leaves a haze around every stroke."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, "p")

        def _nice():
            try:
                os.nice(10)
            except OSError:
                pass
        fmt = ["-png"] if clean else ["-jpeg", "-jpegopt", "quality=93"]
        r = subprocess.run(
            ["pdftoppm", "-f", str(n), "-l", str(n), "-singlefile",
             "-scale-to-x", str(width), "-scale-to-y", "-1", *fmt, path, base],
            capture_output=True, timeout=60, preexec_fn=_nice)
        out = base + (".png" if clean else ".jpg")
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("could not draw this page")
        if not clean:
            with open(out, "rb") as f:
                return f.read(), "image/jpeg"
        import io as _io
        from PIL import Image, ImageFilter
        buf = _io.BytesIO()
        with Image.open(out) as im:
            page = _pdf_clean(im.convert("RGB"))
            page = page.filter(ImageFilter.UnsharpMask(radius=1.2, percent=90, threshold=2))
            page.save(buf, "PNG", optimize=False, compress_level=6)
        return buf.getvalue(), "image/png"


_CJK_CH = "\u2e80-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef"
_PDF_JOIN_CJK_RX = re.compile(f"([{_CJK_CH}])[ \\t]*\\n[ \\t]*(?=[{_CJK_CH}])")
_PDF_HYPHEN_RX = re.compile(r"([A-Za-z])-\n([a-z])")


def _pdf_text_clean(raw: str) -> str:
    """A page's text as it should be spoken: lines of one paragraph joined
    (Chinese without a space, a word hyphenated across lines made whole),
    paragraphs kept apart, page-break marks and stray spaces removed."""
    t = (raw or "").replace("\f", "\n").replace("\r", "")
    t = _PDF_HYPHEN_RX.sub(r"\1\2", t)
    t = _PDF_JOIN_CJK_RX.sub(r"\1", t)
    paras = [re.sub(r"[ \t]*\n[ \t]*", " ", p).strip() for p in re.split(r"\n[ \t]*\n+", t)]
    return "\n".join(re.sub(r"[ \t]{2,}", " ", p) for p in paras if p)[:20000]


def _pdf_text(path: str, n: int) -> str:
    import subprocess
    r = subprocess.run(["pdftotext", "-f", str(n), "-l", str(n), "-enc", "UTF-8", path, "-"],
                       capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("could not read this page's text")
    return _pdf_text_clean(r.stdout.decode("utf-8", "replace"))


@app.get("/pdf/{msg_id}/text/{n}")
async def pdf_text(msg_id: int, n: int):
    """The words on one page (for reading aloud). Empty for a scanned page:
    that is a picture of text, and there are no words in the file."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(_epub_row_sql(), msg_id)
    path = _pdf_source(row)
    if not path:
        return JSONResponse({"error": "not downloaded yet"}, status_code=404)
    try:
        text = await asyncio.to_thread(_pdf_text, path, max(1, int(n)))
    except Exception as e:
        return JSONResponse({"error": str(e), "text": ""}, status_code=422)
    return JSONResponse({"page": n, "text": text},
                        headers={"Cache-Control": "private, max-age=3600"})


@app.get("/pdf/{msg_id}/info")
async def pdf_info(msg_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(_epub_row_sql(), msg_id)
    path = _pdf_source(row)
    if not path:
        return JSONResponse({"error": "not downloaded yet"}, status_code=404)
    try:
        info = await asyncio.to_thread(_pdf_info, path)
    except Exception as e:
        return JSONResponse({"error": f"cannot read this PDF: {e}"}, status_code=422)
    return JSONResponse(info)


@app.get("/pdf/{msg_id}/page/{n}")
async def pdf_page(msg_id: int, n: int, w: int = 1200, clean: int = 0):
    global _PDF_PAGE_CACHE, _PDF_RENDER_SEM
    import collections
    if _PDF_PAGE_CACHE is None:
        _PDF_PAGE_CACHE = collections.OrderedDict()
        _PDF_RENDER_SEM = asyncio.Semaphore(2)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(_epub_row_sql(), msg_id)
    path = _pdf_source(row)
    if not path:
        return Response("not downloaded yet", status_code=404)
    # the smallest size that is not smaller than what the screen will show:
    # a picture the browser has to enlarge is a blurred picture
    _want = max(400, min(int(w), _PDF_WIDTHS[-1]))
    width = next(x for x in _PDF_WIDTHS if x >= _want)
    try:
        info = await asyncio.to_thread(_pdf_info, path)
        if not 1 <= n <= info["pages"]:
            return Response("no such page", status_code=404)
        key = (path, os.path.getmtime(path), n, width, bool(clean))
        data = _PDF_PAGE_CACHE.get(key)
        if data is None:
            async with _PDF_RENDER_SEM:
                data = _PDF_PAGE_CACHE.get(key)
                if data is None:
                    data = await asyncio.to_thread(_pdf_render, path, n, width, bool(clean))
            _PDF_PAGE_CACHE[key] = data
            while len(_PDF_PAGE_CACHE) > _PDF_PAGE_CACHE_MAX:
                _PDF_PAGE_CACHE.popitem(last=False)
        else:
            _PDF_PAGE_CACHE.move_to_end(key)
    except Exception as e:
        return Response(f"cannot draw this page: {e}", status_code=500)
    if n == 1:
        await _touch_local(msg_id)
    return Response(content=data[0], media_type=data[1],
                    headers={"Cache-Control": "private, max-age=86400"})


_PDF_READER_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box}
html,body{margin:0;height:100%;overflow:hidden;font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif}
body{display:flex;flex-direction:column;background:var(--bg);color:var(--fg);
  --bg:#eef1f5;--fg:#1f2937;--bar:#f8fafc;--line:#e2e8f0;--mut:#64748b;--acc:#2563eb}
body[data-theme=dark]{--bg:#0f1115;--fg:#c9d1d9;--bar:#1c2128;--line:#30363d;--mut:#8b949e;--acc:#58a6ff}
.bar{display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--bar);
  border-bottom:1px solid var(--line);flex:none;min-height:50px}
.bar.bottom{border-bottom:none;border-top:1px solid var(--line)}
.ttl{flex:1;min-width:0;font-weight:600;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
button,.btn{font:inherit;font-size:14px;color:var(--fg);background:transparent;border:1px solid var(--line);
  border-radius:8px;padding:7px 12px;cursor:pointer;text-decoration:none;white-space:nowrap;line-height:1.2}
button:hover,.btn:hover{border-color:var(--acc);color:var(--acc)}
button:disabled{opacity:.35;cursor:default}
button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
button.big{font-size:15px;font-weight:600;padding:9px 18px;background:var(--acc);color:#fff;border-color:var(--acc)}
button.big:hover{color:#fff;filter:brightness(1.1)}
button.big:disabled{background:transparent;color:var(--fg);border-color:var(--line)}
.stage{flex:1;overflow:auto;min-height:0;padding:14px 0 30px;-webkit-overflow-scrolling:touch}
.pg{margin:0 auto 14px;background:#fff;box-shadow:0 2px 10px rgba(0,0,0,.14);position:relative}
.pg img{display:block;width:100%;height:100%}
body[data-theme=sepia]{--bg:#e9dfc6;--fg:#433422;--bar:#efe6cf;--line:#dccfae;--mut:#8a7a5c;--acc:#a0522d}
body[data-theme=sepia] .pg{background:#f6efdc}
body[data-theme=sepia] .pg img{mix-blend-mode:multiply}
body[data-theme=dark] .pg{background:#1b1d22}
body[data-theme=dark] .pg img{filter:invert(.92) hue-rotate(180deg)}
.pg .no{position:absolute;right:8px;bottom:6px;font-size:11px;color:#94a3b8;pointer-events:none}
/* one page at a time: each page is a slide filling the stage; turning the
   page moves two slides sideways together */
.stage.paged{overflow:hidden;padding:0;position:relative;touch-action:pan-y pinch-zoom}
.slide{position:absolute;inset:0;overflow:auto;display:flex;padding:8px;will-change:transform;
  -webkit-overflow-scrolling:touch}
.slide .pair{margin:auto;display:flex;gap:10px;flex:none;align-items:flex-start}
.slide .pg{margin:0;flex:none}
.toast{position:fixed;left:50%;bottom:76px;transform:translateX(-50%);background:rgba(15,23,42,.9);color:#fff;
  font-size:13px;padding:8px 14px;border-radius:8px;z-index:20;max-width:86vw;text-align:center;pointer-events:none}
.slide.mv{transition:transform .28s cubic-bezier(.3,.1,.3,1)}
@media (prefers-reduced-motion:reduce){.slide.mv{transition:none}}
.pos{flex:1;min-width:0;text-align:center;font-size:13px;color:var(--mut)}
.pos b{color:var(--fg)}
.pos input[type=number]{width:64px;font:inherit;text-align:center;border:1px solid var(--line);border-radius:6px;
  padding:3px;background:transparent;color:var(--fg)}
input[type=range]{width:100%;accent-color:var(--acc);margin:4px 0 0}
.msg{padding:60px 20px;text-align:center;color:var(--mut)}
@media (max-width:640px){.hide-s{display:none}.bar{gap:5px;padding:6px 8px}button,.btn{padding:7px 9px}}
</style></head>
<body data-theme="light">
<div class="bar">
  <a class="btn" id="back" href="/library" title="back to the library">&larr;<span class="hide-s"> Library</span></a>
  <div class="ttl">__TITLE__</div>
  <button id="wmBtn" title="on: pages are redrawn in black and white without pale watermarks. off: the pages as they are, in colour. The file is never changed.">Hide watermark</button>
  <button id="readBtn" title="read this book aloud from the current page, turning pages as it goes (R)">&#128266;<span class="hide-s"> Read aloud</span></button>
  <button id="modeBtn" title="one page at a time (pages slide sideways), or one long scroll">Pages</button>
  <button id="fitBtn" title="how a page fills the window: the whole page, its width, or its height">Fit page</button>
  <button id="twoBtn" class="hide-s" title="two pages side by side, like an open book (2)">2 pages</button>
  <button id="fsBtn" class="hide-s" title="full screen (F)">&#9974;</button>
  <button id="zOut" title="smaller (-)">A&minus;</button>
  <button id="zIn" title="larger (+)">A+</button>
  <button id="themeBtn" title="light / sepia / dark">&#9680;<span class="hide-s"> Theme</span></button>
  <a class="btn hide-s" href="/media/__ID__" target="_blank" title="the untouched file in the browser's own viewer: select text, search, print">Original</a>
  <a class="btn hide-s" href="/media/__ID__" download title="download the file">&#11015;</a>
</div>
<div class="stage" id="stage"><div class="msg" id="msg">opening…</div></div>
<div class="bar bottom">
  <button class="big" id="prev">&lsaquo; Prev</button>
  <div class="pos">
    page <input type="number" id="pno" min="1" value="1"> of <b id="ptotal">…</b>
    <input type="range" id="slider" min="1" max="1" value="1">
  </div>
  <button class="big" id="next">Next &rsaquo;</button>
</div>
<script>
(function(){
const ID = __ID__, KEY = "tgarr.pdf." + ID;
const $ = id => document.getElementById(id);
const stage = $("stage");
let pages = 0, ratio = 1.414, cur = 1, els = [];
let st = {}; try { st = JSON.parse(localStorage.getItem("tgarr.pdfreader") || "{}"); } catch (e) {}
let book = {}; try { book = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
let zoom = st.zoom || 1, theme = st.theme || "light";
// "paged": one page at a time, turned sideways like a book (the default).
// "scroll": every page in one long column.
let mode = st.mode === "scroll" ? "scroll" : "paged";
let fit = ["page", "width", "height"].includes(st.fit) ? st.fit : "page";   // paged mode only
let two = !!st.two;                                                           // two pages side by side
let clean = book.clean !== undefined ? book.clean : (st.clean !== undefined ? !!st.clean : true);
const saveSt = () => { try { localStorage.setItem("tgarr.pdfreader", JSON.stringify({zoom, theme, clean, mode, fit, two})); } catch (e) {} };
const saveBook = () => { try { localStorage.setItem(KEY, JSON.stringify({page: cur, clean})); } catch (e) {} };
function pageW(){ return Math.round(Math.min(stage.clientWidth - 16, 980) * zoom); }
function reqW(){ return Math.min(3200, Math.round(pageW() * Math.min(3, window.devicePixelRatio || 1))); }
function src(n){ return "/pdf/" + ID + "/page/" + n + "?w=" + reqW() + (clean ? "&clean=1" : ""); }
function size(){
  const w = pageW();
  els.forEach(el => { el.style.width = w + "px"; el.style.height = Math.round(w * (el._ratio || ratio)) + "px"; });
}
const io = new IntersectionObserver(es => es.forEach(e => {
  if (!e.isIntersecting) return;
  const el = e.target, img = el.firstChild, want = src(+el.dataset.n);
  if (img.dataset.src !== want) { img.dataset.src = want; img.src = want; }
}), {root: stage, rootMargin: "1200px 0px"});
function refresh(){ els.forEach(el => { io.unobserve(el); io.observe(el); }); }
function build(){
  const f = document.createDocumentFragment();
  for (let n = 1; n <= pages; n++) {
    const el = document.createElement("div"); el.className = "pg"; el.dataset.n = n;
    const img = document.createElement("img"); img.alt = "page " + n; img.decoding = "async";
    img.onload = () => { if (img.naturalWidth) { const r = img.naturalHeight / img.naturalWidth;
      if (Math.abs(r - (el._ratio || ratio)) > 0.01) { el._ratio = r; el.style.height = Math.round(pageW() * r) + "px"; } } };
    const no = document.createElement("span"); no.className = "no"; no.textContent = n;
    el.appendChild(img); el.appendChild(no); f.appendChild(el); els.push(el);
  }
  stage.innerHTML = ""; stage.appendChild(f); size(); refresh();
}
// ---- one page at a time ----
let slide = null, moving = false; const ratios = {};
const calm = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
// two pages only where there is room for them
const spread = () => two && mode === "paged" && stage.clientWidth >= 760;
const step = () => spread() ? 2 : 1;
const left = n => spread() ? n - ((n - 1) % 2) : n;       // the page a spread starts on
function fitW(r){
  // at zoom 1: the whole page ("page"), as wide as the window ("width"), or as tall ("height")
  const W = (stage.clientWidth - 16 - (spread() ? 10 : 0)) / (spread() ? 2 : 1), H = stage.clientHeight - 16;
  const w = fit === "width" ? Math.min(W, 2400) : fit === "height" ? H / r : Math.min(W, H / r, 1600);
  return Math.round(Math.max(120, w) * zoom);
}
function pageSrc(n, w){
  const px = Math.min(3200, Math.ceil(w * Math.min(3, window.devicePixelRatio || 1) / 100) * 100);
  return "/pdf/" + ID + "/page/" + n + "?w=" + px + (clean ? "&clean=1" : "");
}
function mkSlide(first){
  const s = document.createElement("div"); s.className = "slide"; s.dataset.n = first;
  const pair = document.createElement("div"); pair.className = "pair";
  pair.appendChild(mkPage(first));
  if (spread() && first + 1 <= pages) pair.appendChild(mkPage(first + 1));
  s.appendChild(pair);
  return s;
}
function mkPage(n){
  const el = document.createElement("div"); el.className = "pg";
  const fit = r => { const w = fitW(r); el.style.width = w + "px"; el.style.height = Math.round(w * r) + "px"; return w; };
  const r0 = ratios[n] || ratio, w = fit(r0);
  const img = document.createElement("img"); img.alt = "page " + n; img.decoding = "async";
  img.onload = () => { if (!img.naturalWidth) return; const r = img.naturalHeight / img.naturalWidth;
    if (Math.abs(r - r0) > 0.01) { ratios[n] = r; fit(r); } };
  img.src = pageSrc(n, w);
  const no = document.createElement("span"); no.className = "no"; no.textContent = n;
  el.appendChild(img); el.appendChild(no);
  return el;
}
function settle(){
  // a turn still in flight: finish it at once, so that quick presses never queue up
  stage.querySelectorAll(".slide").forEach(x => { if (x !== slide) x.remove(); });
  if (slide) { slide.classList.remove("mv"); slide.style.transform = ""; }
  moving = false;
}
function show(n, dir){
  n = left(Math.max(1, Math.min(pages, n | 0)));
  if (moving) settle();
  const old = slide, s = mkSlide(n);
  slide = s; cur = n; ui(); saveBook(); turned();
  [n + step(), n + step() + 1, n - 1, n - 2].forEach(k => { if (k >= 1 && k <= pages) { const i = new Image(); i.src = pageSrc(k, fitW(ratios[k] || ratio)); } });
  if (!old || !dir || calm) { stage.querySelectorAll(".slide,.msg").forEach(x => x.remove()); stage.appendChild(s); return; }
  // dir +1: the new page comes in from the right and pushes the old one out
  // to the left; -1 the other way round
  moving = true;
  s.style.transform = "translateX(" + (dir * 100) + "%)";
  stage.appendChild(s); s.getBoundingClientRect();
  s.classList.add("mv"); old.classList.add("mv");
  s.style.transform = "translateX(0)"; old.style.transform = "translateX(" + (-dir * 100) + "%)";
  const done = () => { if (slide === s && moving) settle(); };
  s.addEventListener("transitionend", done, {once: true}); setTimeout(done, 450);
}
function setMode(m){
  mode = m === "scroll" ? "scroll" : "paged";
  stage.classList.toggle("paged", mode === "paged");
  stage.innerHTML = ""; els = []; slide = null; moving = false;
  if (mode === "paged") show(cur, 0); else { build(); go(cur); }
  ui();
}
function go(n, smooth){
  n = Math.max(1, Math.min(pages, n | 0));
  if (mode === "paged") { n = left(n); if (n !== cur || !slide) show(n, smooth ? Math.sign(n - cur) : 0); return; }
  if (!els[n - 1]) return;
  stage.scrollTo({top: els[n - 1].offsetTop - 8, behavior: smooth ? "smooth" : "auto"});
}
function track(){
  if (mode !== "scroll" || !els.length) return;
  const mid = stage.scrollTop + stage.clientHeight * 0.35; let lo = 0, hi = els.length - 1, k = 0;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (els[m].offsetTop <= mid) { k = m; lo = m + 1; } else hi = m - 1; }
  if (k + 1 !== cur) { cur = k + 1; ui(); saveBook(); }
}
function ui(){
  if (document.activeElement !== $("pno")) $("pno").value = cur;
  $("slider").value = cur; $("prev").disabled = cur <= 1; $("next").disabled = cur + step() > pages;
  $("fitBtn").textContent = "Fit " + fit; $("fitBtn").disabled = mode !== "paged";
  $("twoBtn").classList.toggle("on", two); $("twoBtn").disabled = mode !== "paged";
  $("readBtn").classList.toggle("on", reading);
  $("wmBtn").classList.toggle("on", clean); document.body.dataset.theme = theme;
  $("modeBtn").textContent = mode === "paged" ? "Pages" : "Scroll";
}
let tt = null; stage.addEventListener("scroll", () => { clearTimeout(tt); tt = setTimeout(track, 60); }, {passive: true});
$("prev").onclick = () => go(cur - step(), true); $("next").onclick = () => go(cur + step(), true);
$("pno").addEventListener("change", e => go(+e.target.value));
$("slider").addEventListener("input", e => { $("pno").value = e.target.value; });
$("slider").addEventListener("change", e => go(+e.target.value));
function redraw(){ const keep = cur; if (mode === "paged") show(keep, 0); else { size(); refresh(); go(keep); } }
function rezoom(z){ zoom = Math.max(0.5, Math.min(2.5, z)); saveSt(); redraw(); }
$("modeBtn").onclick = () => { setMode(mode === "paged" ? "scroll" : "paged"); saveSt(); };
$("fitBtn").onclick = () => { fit = fit === "page" ? "width" : fit === "width" ? "height" : "page"; saveSt(); redraw(); ui(); };
$("twoBtn").onclick = () => { two = !two; saveSt(); redraw(); ui(); };
const fullscreen = () => { if (document.fullscreenElement) document.exitFullscreen(); else if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen(); };
$("fsBtn").onclick = fullscreen;
// ---- read aloud ----
__SPEECH_JS__
let reading = false, readN = 0, readRun = 0, blanks = 0, byReader = false;
function stopRead(why){
  reading = false; readRun++; if (synth) synth.cancel(); ui(); if (why) toast(why, 6000);
}
async function readFrom(n){
  const run = ++readRun; readN = n; if (synth) synth.cancel();
  let j = {}; try { j = await fetch("/pdf/" + ID + "/text/" + n).then(r => r.json()); } catch (e) {}
  if (!reading || run !== readRun) return;
  const text = (j.text || "").trim();
  const nextPage = () => {
    if (!reading || run !== readRun) return;
    if (n >= pages) { stopRead("the end of the book"); return; }
    byReader = true; go(n + 1, true); byReader = false; readFrom(n + 1);
  };
  if (!text) {
    if (++blanks >= 3) { stopRead("no words to read on these pages - a scanned book holds pictures of text"); return; }
    nextPage(); return;
  }
  blanks = 0;
  const parts = chunks(text), v = voiceFor(text);
  parts.forEach((part, i) => {
    const u = new SpeechSynthesisUtterance(part);
    if (v.voice) u.voice = v.voice; u.lang = v.voice ? v.voice.lang : v.lang;
    if (i === parts.length - 1) u.onend = nextPage;
    u.onerror = e => { if (e.error !== "canceled" && e.error !== "interrupted" && run === readRun) stopRead("the browser could not read this aloud"); };
    synth.speak(u);
  });
}
function turned(){
  // the reader turned the page itself: nothing to do. A person did: read on from there.
  if (reading && !byReader) readFrom(cur);
}
$("readBtn").onclick = () => {
  if (!synth) { toast("this browser cannot read aloud"); return; }
  if (reading) { stopRead(); return; }
  reading = true; blanks = 0; ui(); readFrom(cur);
};
if (!synth) $("readBtn").style.display = "none";
else { synth.getVoices(); window.addEventListener("beforeunload", () => synth.cancel()); }
// a swipe turns the page (one finger, clearly sideways)
let sx = null, sy = 0;
stage.addEventListener("touchstart", e => { sx = e.touches.length === 1 ? e.touches[0].clientX : null; sy = e.touches[0].clientY; }, {passive: true});
stage.addEventListener("touchend", e => {
  if (mode !== "paged" || sx === null) return;
  const t = e.changedTouches[0], dx = t.clientX - sx, dy = t.clientY - sy; sx = null;
  if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy) * 1.5) go(cur + (dx < 0 ? step() : -step()), true);
}, {passive: true});
// the wheel scrolls inside a page that is taller than the window; at its
// edge (or when it fits) it turns the page
let wheelAt = 0;
stage.addEventListener("wheel", e => {
  if (mode !== "paged" || !slide || e.ctrlKey) return;
  const d = Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
  const top = slide.scrollTop <= 0, bottom = slide.scrollTop + slide.clientHeight >= slide.scrollHeight - 1;
  if (!((d > 0 && bottom) || (d < 0 && top))) return;
  e.preventDefault();
  if (Math.abs(d) > 6 && Date.now() - wheelAt > 420) { wheelAt = Date.now(); go(cur + (d > 0 ? step() : -step()), true); }
}, {passive: false});
$("zIn").onclick = () => rezoom(zoom * 1.2); $("zOut").onclick = () => rezoom(zoom / 1.2);
$("themeBtn").onclick = () => { theme = theme === "light" ? "sepia" : theme === "sepia" ? "dark" : "light"; saveSt(); ui(); };
$("wmBtn").onclick = () => { clean = !clean; saveSt(); saveBook(); ui(); if (mode === "paged") show(cur, 0); else refresh(); };
window.addEventListener("resize", redraw);
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT" && e.target.type === "number") return;
  const k = e.key;
  if (k === "ArrowRight" || k === "PageDown" || (k === " " && !e.shiftKey && mode === "paged")) { e.preventDefault(); go(cur + step(), true); }
  else if (k === "ArrowLeft" || k === "PageUp" || (k === " " && e.shiftKey && mode === "paged")) { e.preventDefault(); go(cur - step(), true); }
  else if (k === "f" || k === "F") fullscreen();
  else if (k === "r" || k === "R") $("readBtn").click();
  else if (k === "2") $("twoBtn").click();
  else if (k === "Home") go(1); else if (k === "End") go(pages);
  else if (k === "+" || k === "=") rezoom(zoom * 1.2); else if (k === "-") rezoom(zoom / 1.2);
});
// "back": players open in a new tab, where there is no page to go back to.
// Go back if there is one; otherwise close the tab (the list is still open
// behind it); if the browser will not close it, go to the list.
function tgBack(fallback){
  if (history.length > 1 && document.referrer && document.referrer.indexOf(location.host) > 0) { history.back(); return; }
  window.close();
  setTimeout(function(){ location.href = fallback; }, 200);
}
$("back").onclick = e => { e.preventDefault(); tgBack("/library"); };
ui();
fetch("/pdf/" + ID + "/info").then(r => r.json()).then(j => {
  if (j.error) { $("msg").textContent = "✗ " + j.error; return; }
  pages = j.pages; ratio = j.ratio || 1.414;
  $("ptotal").textContent = pages; $("slider").max = pages; $("pno").max = pages;
  const h = parseInt((location.hash || "").slice(1), 10);
  cur = (h >= 1 && h <= pages) ? h : Math.min(pages, book.page || 1);
  setMode(mode);
}).catch(() => { $("msg").textContent = "✗ could not open this PDF"; });
})();
</script>
<script src="/mc/ping.js" defer></script>
</body></html>"""


@app.get("/pdf/{msg_id}", response_class=HTMLResponse)
async def pdf_reader(msg_id: int):
    """The PDF reader (see the block comment above)."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(_epub_row_sql(), msg_id)
    if not _pdf_source(row):
        return HTMLResponse(
            f'<p>This file is not downloaded yet. <a href="/watch/{msg_id}">Open it</a>.</p>',
            status_code=404)
    title = _re.sub(r"\.[A-Za-z0-9]{2,5}$", "", row["file_name"] or f"file-{msg_id}")
    return HTMLResponse(_PDF_READER_HTML.replace("__SPEECH_JS__", _SPEECH_JS).replace("__ID__", str(int(msg_id)))
                        .replace("__TITLE__", html.escape(title)))


@app.get("/epub/{msg_id}/cover")
async def epub_cover(msg_id: int):
    """Extract + serve the cover image from an .epub. Tries:
       1. manifest item with properties='cover-image' (EPUB3)
       2. manifest item id='cover' or referenced by <meta name='cover'> (EPUB2)
       3. first image inside titlepage chapter
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT local_path FROM messages WHERE id=$1", msg_id)
    epub_path = _epub_locate(msg_id, row)
    if not epub_path:
        return Response("not cached", status_code=404)
    try:
        with _zipfile.ZipFile(epub_path) as zf:
            from xml.etree import ElementTree as _ET
            container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
            ns_c = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            opf_path = _ET.fromstring(container).find(".//c:rootfile", ns_c).attrib["full-path"]
            opf_dir = os.path.dirname(opf_path).replace("\\", "/")
            opf_root = _ET.fromstring(zf.read(opf_path).decode("utf-8", "replace"))
            ns_opf = {"opf": "http://www.idpf.org/2007/opf"}
            manifest = {item.attrib["id"]: item
                        for item in opf_root.findall(".//opf:manifest/opf:item", ns_opf)}

            cover_href = None
            # 1. EPUB3 — properties="cover-image"
            for item in manifest.values():
                if "cover-image" in (item.attrib.get("properties") or ""):
                    cover_href = item.attrib.get("href")
                    break
            # 2. EPUB2 — <meta name="cover" content="id">
            if not cover_href:
                for meta in opf_root.findall(".//opf:metadata/opf:meta", ns_opf):
                    if meta.attrib.get("name") == "cover":
                        cid = meta.attrib.get("content")
                        if cid in manifest:
                            cover_href = manifest[cid].attrib.get("href")
                            break
            # 3. manifest id="cover"
            if not cover_href and "cover" in manifest:
                cover_href = manifest["cover"].attrib.get("href")
            # 4. first image in titlepage / first spine chapter
            if not cover_href:
                spine_first = opf_root.find(".//opf:spine/opf:itemref", ns_opf)
                if spine_first is not None:
                    idref = spine_first.attrib.get("idref")
                    chapter_item = manifest.get(idref)
                    if chapter_item is not None:
                        chapter_href = _uq(chapter_item.attrib["href"])
                        chapter_path = (opf_dir + "/" + chapter_href).lstrip("/") if opf_dir else chapter_href
                        try:
                            chap = zf.read(chapter_path).decode("utf-8", "replace")
                            m = _re.search(r"(?:xlink:)?href=[\"\']([^\"\']+\.(?:jpg|jpeg|png|webp|gif))[\"\']",
                                          chap, _re.IGNORECASE)
                            if not m:
                                m = _re.search(r"<img[^>]*\bsrc=[\"\']([^\"\']+)[\"\']",
                                              chap, _re.IGNORECASE)
                            if m:
                                ref = _uq(m.group(1))
                                cover_href = os.path.normpath(
                                    os.path.join(os.path.dirname(chapter_path), ref)).replace("\\", "/")
                        except Exception:
                            pass
            if not cover_href:
                return Response("no cover", status_code=404)

            cover_path = cover_href
            # If from manifest, resolve relative to opf_dir
            if "/" not in cover_path or not cover_path.startswith(opf_dir or ""):
                if opf_dir and not cover_path.startswith(opf_dir):
                    cover_path = (opf_dir + "/" + cover_href).lstrip("/")
            cover_path = _re.sub(r"/+", "/", cover_path)
            try:
                data = zf.read(cover_path)
            except KeyError:
                # try common locations
                for guess in [cover_href, "OEBPS/" + cover_href, "OPS/" + cover_href]:
                    try:
                        data = zf.read(guess)
                        break
                    except KeyError:
                        continue
                else:
                    return Response("cover file missing", status_code=404)

            ext = cover_path.rsplit(".", 1)[-1].lower() if "." in cover_path else "jpg"
            return Response(
                content=data,
                media_type=_EPUB_MIME.get(ext, "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        return Response(f"cover error: {e}", status_code=500)


@app.get("/epub/{msg_id}/asset/{asset_path:path}")
async def epub_asset(msg_id: int, asset_path: str):
    """Serve embedded asset (image / css / etc) from inside the .epub zip."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT local_path FROM messages WHERE id=$1", msg_id)
    epub_path = _epub_locate(msg_id, row)
    if not epub_path:
        return Response("not cached", status_code=404)
    try:
        with _zipfile.ZipFile(epub_path) as zf:
            data = zf.read(_uq(asset_path))
    except (KeyError, _zipfile.BadZipFile):
        return Response("asset not found", status_code=404)
    ext = asset_path.rsplit(".", 1)[-1].lower() if "." in asset_path else ""
    return Response(
        content=data,
        media_type=_EPUB_MIME.get(ext, "application/octet-stream"),
        headers={"Cache-Control": "public, max-age=3600",
                 "Content-Security-Policy": "sandbox; " + _EPUB_CSP,
                 "X-Content-Type-Options": "nosniff"})


async def _tg_flood() -> Optional[dict]:
    """Active Telegram wait published by the crawler, if any."""
    try:
        async with db_pool.acquire() as conn:
            v = await conn.fetchval("SELECT value FROM config WHERE key='tg_flood'")
        if not v:
            return None
        until_s, _, what = v.partition("|")
        left = int(float(until_s) - time.time())
        if left <= 0:
            return None
        return {"seconds_left": left, "what": what}
    except Exception:
        return None


@app.get("/api/media-status/{msg_id}")
async def media_status(msg_id: int):
    """Lightweight status for /watch page polling.
    Returns {status, bytes, total, file_dc, my_dc, cross_dc}."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT local_path, file_size, file_name, media_type, mime_type, file_dc
               FROM messages WHERE id=$1""", msg_id)
    if not row:
        return JSONResponse({"status": "missing"}, status_code=404)
    lp = row["local_path"]
    total = row["file_size"] or 0
    file_dc = row["file_dc"] if (row["file_dc"] or 0) > 0 else None
    my_dc = await _get_my_dc()
    if not _signed_in() and not _is_local(lp):
        # nobody to fetch it: say so instead of waiting for a download that cannot start
        return JSONResponse({"status": "signedout"})
    if lp != "__failed__":
        await _want_fetch(msg_id)
    cross_dc = bool(file_dc and my_dc and file_dc != my_dc)
    if lp == "__failed__":
        return JSONResponse({"status": "failed", "file_dc": file_dc,
                            "my_dc": my_dc, "cross_dc": cross_dc})
    if not lp or lp == "__user_queued__":
        return JSONResponse({"status": "queued", "bytes": 0, "total": total,
                            "file_dc": file_dc, "my_dc": my_dc, "cross_dc": cross_dc,
                            "tg_wait": await _tg_flood()})
    path = os.path.join("/downloads", lp)
    bytes_ = os.path.getsize(path) if os.path.exists(path) else 0
    if total and bytes_ < total:
        # Not growing any more: the fetch died half-way (the crawler was
        # restarted). The person is asking for it right now - fetch it again.
        import time as _time
        try:
            idle = _time.time() - os.path.getmtime(path)
        except OSError:
            idle = 1e9
        async with db_pool.acquire() as conn:
            _by_hand = await conn.fetchval(
                "SELECT fetch_wanted_at = 'epoch' FROM messages WHERE id = $1", msg_id)
        if idle > 45 and not _by_hand:      # paused by hand stays paused
            await _revive_fetch(msg_id, lp)
            return JSONResponse({"status": "queued", "bytes": bytes_, "total": total,
                                 "file_dc": file_dc, "my_dc": my_dc, "cross_dc": cross_dc,
                                 "tg_wait": await _tg_flood()})
    if total and bytes_ >= total:
        return JSONResponse({"status": "ready", "bytes": bytes_, "total": total,
                            "mime": row["mime_type"], "media_type": row["media_type"],
                            "file_name": row["file_name"],
                            "file_dc": file_dc, "my_dc": my_dc, "cross_dc": cross_dc})
    async with db_pool.acquire() as conn:
        _paused = await conn.fetchval(
            "SELECT fetch_wanted_at = 'epoch' FROM messages WHERE id = $1", msg_id)
    return JSONResponse({"status": "downloading", "bytes": bytes_, "total": total,
                        "paused": bool(_paused),
                        "mime": row["mime_type"], "media_type": row["media_type"],
                        "file_name": row["file_name"],
                        "file_dc": file_dc, "my_dc": my_dc, "cross_dc": cross_dc})


# ── Video player ─────────────────────────────────────────────────────
# The browser's own <video> controls stay (they are what people know, and
# they work on every device); around them: a view that fills the window,
# favourite, speed, 10-second skips, picture-in-picture, "continue where I
# stopped", and - while the file is still arriving from Telegram - how much
# of it is here, because seeking past that point has to wait.
_WATCH_PLAYER_CSS = """
.vshell { width:100%; display:flex; flex-direction:column; align-items:center; }
.vstage { display:flex; align-items:center; justify-content:center; overflow:hidden; background:#000; border-radius:8px;
  width:1200px; max-width:96vw; height:calc(100vh - 150px); }
.vshell video { width:100%; height:100%; max-width:none; max-height:none; background:#000; object-fit:contain; transition:transform .2s; }
body.wide { height:100vh; overflow:hidden; }
body.wide main { padding:0; align-items:stretch; min-height:0; }
body.wide .vshell { height:100%; }
body.wide .vstage { flex:1; min-height:0; width:100%; max-width:100%; height:auto; border-radius:0; }
.vbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; justify-content:center; padding:10px 12px; width:100%; box-sizing:border-box; }
.vbar button, .vbar select, .vbar a, .bar button { font:inherit; font-size:13px; color:#e2e8f0; background:#1e293b;
  border:1px solid #334155; border-radius:8px; padding:7px 12px; cursor:pointer; text-decoration:none; line-height:1.2; }
.vbar button:hover, .vbar a:hover, .vbar select:hover, .bar button:hover { border-color:#5eb6e5; color:#fff; }
.vbar button.on, .bar button.on { background:#2563eb; border-color:#2563eb; color:#fff; }
.bar button.fav.on { background:transparent; border-color:#fb7185; color:#fb7185; }
.vbar .arr { font-size:12px; color:#94a3b8; display:flex; align-items:center; gap:8px; }
.vbar .arr i { display:block; width:120px; height:5px; background:#1e293b; border-radius:3px; overflow:hidden; }
.vbar .arr i b { display:block; height:100%; background:#5eb6e5; width:0; transition:width .4s; }
.vtoast { position:fixed; left:50%; top:70px; transform:translateX(-50%); background:rgba(15,23,42,.92); border:1px solid #334155;
  color:#e2e8f0; padding:9px 16px; border-radius:20px; font-size:13px; z-index:9; transition:opacity .5s; }
.vtoast a { color:#5eb6e5; cursor:pointer; margin-left:10px; }
@media (max-width:640px){ .vbar { gap:5px; padding:8px 6px } .vbar button, .vbar select, .vbar a { padding:7px 9px } }
"""

_WATCH_PLAYER_JS = r"""
// "back": players open in a new tab, where there is no page to go back to.
// Go back if there is one; otherwise close the tab (the list is still open
// behind it); if the browser will not close it, go to the list.
function tgBack(fallback){
  if (history.length > 1 && document.referrer && document.referrer.indexOf(location.host) > 0) { history.back(); return; }
  window.close();
  setTimeout(function(){ location.href = fallback; }, 200);
}
function tgFavBtn(){
  const bk = document.getElementById("backBtn");
  if (bk) bk.onclick = e => { e.preventDefault(); tgBack(MEDIA_TYPE === "video" ? "/videos" : MEDIA_TYPE === "audio" ? "/music" : "/library"); };
  const b = document.getElementById("favBtn"); if (!b) return;
  b.onclick = () => fetch("/api/fav/" + MSG_ID, {method: "POST"}).then(r => r.json()).then(j => {
    if (j.fav === undefined) return; b.classList.toggle("on", j.fav); b.title = j.fav ? "in your favourites" : "add to favourites"; });
}
function tgVideo(main){
  let prefs = {}; try { prefs = JSON.parse(localStorage.getItem("tgarr.video") || "{}"); } catch (e) {}
  if (prefs.wide === undefined) prefs.wide = true;
  const save = () => { try { localStorage.setItem("tgarr.video", JSON.stringify(prefs)); } catch (e) {} };
  const KEY = "tgarr.v." + MSG_ID;
  main.innerHTML =
    '<div class="vshell"><div class="vstage" id="vStage"><video id="v" controls autoplay playsinline src="/media/' + MSG_ID + '"></video></div>' +
    '<div class="vbar">' +
    '<button id="vBack" title="back 10 seconds (left arrow)">&#8630; 10s</button>' +
    '<button id="vFwd" title="forward 10 seconds (right arrow)">10s &#8631;</button>' +
    '<select id="vSpeed" title="playback speed (&lt; and &gt;)">' +
      [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2].map(x => '<option value="' + x + '">' + x + '&times;</option>').join("") + '</select>' +
    '<button id="vRot" title="turn the picture a quarter turn (r) - for phone videos lying on their side">&#8635; Rotate</button>' +
    '<button id="vMir" title="mirror the picture left-right (h) - for selfie videos">&#8646; Mirror</button>' +
    '<button id="vFill" title="fill the frame, cropping the edges (c)">Fill</button>' +
    '<button id="vWide" title="fill the window (w)">Full width</button>' +
    '<button id="vPip" title="keep playing in a small floating window">Picture-in-picture</button>' +
    '<button id="vFull" title="full screen (f)">Full screen</button>' +
    '<span class="arr" id="vArr" style="display:none"><i><b id="vArrBar"></b></i><span id="vArrTxt"></span></span>' +
    '</div></div>';
  const v = document.getElementById("v"), $ = id => document.getElementById(id);
  const toast = (htmlText, ms) => { const t = document.createElement("div"); t.className = "vtoast"; t.innerHTML = htmlText;
    document.body.appendChild(t); setTimeout(() => { t.style.opacity = 0; setTimeout(() => t.remove(), 600); }, ms || 5000); return t; };
  const fmt = s => { s = Math.floor(s || 0); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0"); };
  // picture: quarter turns, mirror, fill. Phone videos are often sideways or
  // mirrored; this is per video, and remembered for it.
  let pic = {}; try { pic = JSON.parse(localStorage.getItem(KEY + ".pic") || "{}"); } catch (e) {}
  const stage = $("vStage");
  function picture(){
    const rot = ((pic.rot || 0) % 360 + 360) % 360, side = rot === 90 || rot === 270;
    // a turned picture must fit a frame of the other shape: give the video
    // the frame's swapped size, then turn it
    v.style.width = side ? stage.clientHeight + "px" : "100%";
    v.style.height = side ? stage.clientWidth + "px" : "100%";
    v.style.flex = "none";
    v.style.transform = "rotate(" + rot + "deg)" + (pic.mir ? " scaleX(-1)" : "");
    v.style.objectFit = pic.fill ? "cover" : "contain";
    $("vRot").classList.toggle("on", rot !== 0); $("vMir").classList.toggle("on", !!pic.mir); $("vFill").classList.toggle("on", !!pic.fill);
    try { if (rot || pic.mir || pic.fill) localStorage.setItem(KEY + ".pic", JSON.stringify(pic)); else localStorage.removeItem(KEY + ".pic"); } catch (e) {}
  }
  $("vRot").onclick = () => { pic.rot = ((pic.rot || 0) + 90) % 360; picture(); };
  $("vMir").onclick = () => { pic.mir = !pic.mir; picture(); };
  $("vFill").onclick = () => { pic.fill = !pic.fill; picture(); };
  window.addEventListener("resize", picture);
  // wide
  const wide = on => { prefs.wide = on; save(); document.body.classList.toggle("wide", on); $("vWide").classList.toggle("on", on); picture(); };
  wide(prefs.wide); $("vWide").onclick = () => wide(!prefs.wide);
  // speed + volume
  const speed = x => { x = Math.max(0.5, Math.min(2, x)); v.playbackRate = x; v.defaultPlaybackRate = x; prefs.speed = x; save(); $("vSpeed").value = String(x); };
  $("vSpeed").onchange = e => speed(+e.target.value);
  if (prefs.vol !== undefined) v.volume = prefs.vol;
  v.addEventListener("volumechange", () => { prefs.vol = v.volume; save(); });
  v.addEventListener("loadedmetadata", () => {
    speed(prefs.speed || 1);
    // continue where I stopped
    let pos = 0; try { pos = +localStorage.getItem(KEY) || 0; } catch (e) {}
    if (pos > 30 && (!v.duration || pos < v.duration - 30)) {
      v.currentTime = pos;
      const t = toast("continued from " + fmt(pos) + ' <a id="vRestart">start over</a>', 8000);
      t.querySelector("#vRestart").onclick = () => { v.currentTime = 0; t.remove(); };
    }
  });
  let lastSave = 0;
  v.addEventListener("timeupdate", () => { const now = Date.now(); if (now - lastSave < 4000) return; lastSave = now;
    try { if (v.duration && v.currentTime > v.duration - 20) localStorage.removeItem(KEY); else localStorage.setItem(KEY, String(Math.floor(v.currentTime))); } catch (e) {} });
  // skips, pip, full screen
  const skip = d => { v.currentTime = Math.max(0, Math.min(v.duration || 1e9, v.currentTime + d)); };
  $("vBack").onclick = () => skip(-10); $("vFwd").onclick = () => skip(10);
  const full = () => { if (document.fullscreenElement) document.exitFullscreen(); else (v.requestFullscreen || v.webkitRequestFullscreen || v.webkitEnterFullscreen).call(v); };
  $("vFull").onclick = full;
  if (document.pictureInPictureEnabled) $("vPip").onclick = () => { if (document.pictureInPictureElement) document.exitPictureInPicture(); else v.requestPictureInPicture().catch(() => {}); };
  else $("vPip").style.display = "none";
  document.addEventListener("keydown", e => {
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
    const k = e.key;
    if (k === "ArrowLeft") { e.preventDefault(); skip(-10); }
    else if (k === "ArrowRight") { e.preventDefault(); skip(10); }
    else if (k === " " || k === "k") { if (e.target !== v) { e.preventDefault(); v.paused ? v.play() : v.pause(); } }
    else if (k === "f") full();
    else if (k === "w") wide(!prefs.wide);
    else if (k === "r") $("vRot").click();
    else if (k === "h") $("vMir").click();
    else if (k === "c") $("vFill").click();
    else if (k === "m") v.muted = !v.muted;
    else if (k === ">" || k === ".") speed((prefs.speed || 1) + 0.25);
    else if (k === "<" || k === ",") speed((prefs.speed || 1) - 0.25);
    else if (k === "ArrowUp") { e.preventDefault(); v.volume = Math.min(1, v.volume + 0.1); }
    else if (k === "ArrowDown") { e.preventDefault(); v.volume = Math.max(0, v.volume - 0.1); }
  });
  // how much of the file has arrived
  (async function arrival(){
    try {
      const j = await (await fetch("/api/media-status/" + MSG_ID)).json();
      if (j.status === "downloading" && j.total) {
        const pct = Math.min(100, j.bytes / j.total * 100);
        $("vArr").style.display = "flex"; $("vArrBar").style.width = pct + "%";
        $("vArrTxt").textContent = j.paused
          ? "paused at " + pct.toFixed(0) + "% — resume it on the Downloads page, or reopen this film"
          : "still arriving: " + pct.toFixed(0) + "% here — skipping further ahead will wait";
      } else if (j.status === "ready") { $("vArr").style.display = "none"; return; }
    } catch (e) {}
    setTimeout(arrival, 3000);
  })();
}
"""


@app.get("/watch/{msg_id}", response_class=HTMLResponse)
async def watch_page(msg_id: int):
    """Loading-aware media viewer. Polls /api/media-status until enough bytes
    are written to start playback, then renders <video>/<audio>/<iframe>."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT m.local_path, m.file_name, m.file_size, m.media_type,
                      m.mime_type, c.title AS ch_title,
                      EXISTS (SELECT 1 FROM favorites f WHERE f.msg_id = m.id) AS fav
               FROM messages m LEFT JOIN channels c ON c.id=m.channel_id
               WHERE m.id=$1""", msg_id)
    if not row:
        return HTMLResponse("<h1>404 — message not indexed</h1>", status_code=404)
    title = row["file_name"] or f"msg-{msg_id}"
    mt = row["media_type"] or "document"
    await _want_fetch(msg_id, opened=True)

    # Trigger queueing immediately so the user doesn\'t wait for first poll.
    if not row["local_path"]:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET local_path = $$__user_queued__$$ "
                "WHERE id = $1 AND local_path IS NULL",
                msg_id)

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} — tgarr</title>
<style>
body {{ background:#0f172a; color:#e2e8f0; font-family:system-ui,sans-serif;
       margin:0; padding:0; min-height:100vh; display:flex; flex-direction:column; }}
.bar {{ background:#1e293b; padding:12px 20px; display:flex; align-items:center;
       gap:12px; border-bottom:1px solid #334155; }}
.bar .title {{ font-weight:600; flex:1; overflow:hidden; text-overflow:ellipsis;
              white-space:nowrap; }}
.bar a {{ color:#94a3b8; text-decoration:none; font-size:13px; }}
.bar a:hover {{ color:#e2e8f0; }}
main {{ flex:1; display:flex; align-items:center; justify-content:center;
       padding:30px; }}
.loader {{ text-align:center; max-width:480px; }}
.loader .ico {{ font-size:72px; opacity:0.4; }}
.loader .status {{ font-size:18px; margin:18px 0 10px; }}
.loader .hint {{ color:#94a3b8; font-size:13px; margin-top:8px; }}
.progress {{ width:100%; height:6px; background:#1e293b; border-radius:3px;
            overflow:hidden; margin-top:14px; }}
.progress > div {{ height:100%; background:#5eb6e5; width:0%; transition:width 0.4s; }}
video, audio, iframe {{ max-width:96vw; max-height:80vh; }}
video, iframe {{ width:1200px; max-width:96vw; }}
audio {{ width:600px; max-width:96vw; }}
{_WATCH_PLAYER_CSS}
</style></head><body>
<div class="bar">
  <a href="#" id="backBtn">← back</a>
  <div class="title">{html.escape(title)}</div>
  <button class="fav{' on' if row["fav"] else ''}" id="favBtn" title="{'in your favourites' if row["fav"] else 'add to favourites'}">♥ Favourite</button>
  <a href="/media/{msg_id}" download>⬇ download</a>
</div>
<main id="main">
  <div class="loader">
    <div class="ico">⏳</div>
    <div class="status" id="status">requesting from Telegram CDN…</div>
    <div class="progress"><div id="progBar"></div></div>
    <div class="hint" id="hint">on-demand download — first chunk usually starts within seconds</div>
    <div class="hint" id="dchint" style="margin-top:6px"></div>
  </div>
</main>
<script>
const MSG_ID = {msg_id};
const MEDIA_TYPE = {_js_str(mt)};
const MEDIA_FILE_NAME = {_js_str(row["file_name"] or "")};
{_WATCH_PLAYER_JS}
tgFavBtn();
let started = false;
function fmtBytes(b) {{
  if (!b) return "0 B";
  const u = ["B","KB","MB","GB"]; let i = 0; let v = b;
  while (v >= 1024 && i < u.length-1) {{ v /= 1024; i++; }}
  return v.toFixed(v < 10 ? 1 : 0) + " " + u[i];
}}
function renderPlayer() {{
  if (started) return; started = true;
  const main = document.getElementById("main");
  if (MEDIA_TYPE === "video") {{
    tgVideo(main);
    return;
  }}
  if (MEDIA_TYPE === "audio") {{
    main.innerHTML = `<audio controls autoplay src="/media/${{MSG_ID}}"></audio>`;
    return;
  }}
  // document — pick by extension
  const ext = (MEDIA_FILE_NAME || "").toLowerCase().split(".").pop();
  if (ext === "pdf") {{
    location.replace("/pdf/" + MSG_ID);
    return;
  }}
  if (ext === "epub") {{
    // Read in the built-in reader: instant, and no conversion to run.
    location.replace("/epub/" + MSG_ID);
    return;
  }}
  if (["mobi","azw","azw3","djvu","fb2","lit","cbr","cbz"].includes(ext)) {{
    // Calibre-converted PDF — poll status, swap to iframe on ready.
    const loader = document.createElement("div");
    loader.className = "loader";
    loader.innerHTML =
      '<div class="ico">📚</div>' +
      '<div class="status" id="ebookStatus">checking…</div>' +
      '<div class="progress"><div id="ebookBar"></div></div>' +
      '<div class="hint">first open of a non-PDF ebook converts to PDF via calibre. ' +
      'small books (<10MB): ~20s. medium: 1–2 min. big books (50MB+) queue automatically — leave the page, conversion continues in background, come back later (a few minutes to hours for very large books). ' +
      'subsequent opens are instant.</div>' +
      '<div class="hint" id="ebookElapsed" style="margin-top:6px"></div>';
    main.innerHTML = "";
    main.appendChild(loader);
    const startedAt = Date.now();
    let lastSize = 0;
    async function tick() {{
      try {{
        const r = await fetch(`/api/ebook-status/${{MSG_ID}}`, {{cache: "no-store"}});
        const j = await r.json();
        const elapsed = ((Date.now()-startedAt)/1000).toFixed(0);
        document.getElementById("ebookElapsed").textContent = `elapsed: ${{elapsed}}s`;
        if (j.state === "ready") {{
          location.replace("/pdf/" + MSG_ID);
          return;
        }}
        if (j.state === "failed") {{
          document.getElementById("ebookStatus").textContent =
            "✗ conversion failed: " + (j.reason || "calibre error");
          return;
        }}
        if (j.state === "no_source") {{
          document.getElementById("ebookStatus").textContent = "✗ source not cached";
          return;
        }}

        let msg;
        if (j.state === "downloading") {{
          msg = "downloading…";
        }} else if (j.state === "queued") {{
          msg = `queued — position ${{j.position || 1}} in line`;
          if (j.eta_s) msg += ` · est wait ~${{Math.ceil(j.eta_s/60)}} min`;
        }} else {{
          msg = "converting…";
        }}
        if (j.src_size) msg += ` (source ${{fmtBytes(j.src_size)}})`;
        document.getElementById("ebookStatus").textContent = msg;
        if (j.out_size && j.out_size !== lastSize) {{
          lastSize = j.out_size;
        }}
        // Indeterminate-style progress: pulse the bar based on elapsed
        const pct = Math.min(95, parseInt(elapsed) * 2);
        document.getElementById("ebookBar").style.width = pct + "%";
        setTimeout(tick, 2000);
      }} catch (e) {{
        document.getElementById("ebookStatus").textContent = "✗ status check failed";
        setTimeout(tick, 5000);
      }}
    }}
    tick();
    return;
    return;
  }}
  // fallback: try iframe but offer download as escape hatch
  main.innerHTML = `<iframe src="/media/${{MSG_ID}}" style="height:85vh;width:1200px;max-width:96vw;border:0;background:#fff"></iframe>
    <div style="margin-top:8px"><a href="/media/${{MSG_ID}}" download style="color:#5eb6e5;font-size:13px">⬇ download raw</a></div>`;
}}
async function poll() {{
  try {{
    const r = await fetch(`/api/media-status/${{MSG_ID}}`);
    const j = await r.json();
    if (j.file_dc) {{
      const dh = document.getElementById("dchint");
      if (dh && !dh.dataset.set) {{
        dh.dataset.set = "1";
        const sameDc = !j.cross_dc;
        dh.innerHTML = sameDc
          ? `<span style="color:#10b981">● file DC ${{j.file_dc}} = your DC ${{j.my_dc}} — fast path</span>`
          : `<span style="color:#f59e0b">● file DC ${{j.file_dc}} ≠ your DC ${{j.my_dc}} — cross-DC, may hit FloodWait</span>`;
      }}
    }}
    if (j.status === "signedout") {{
      document.getElementById("status").textContent = "This file is not saved on this machine";
      document.getElementById("hint").innerHTML =
        'Fetching it needs a Telegram account. <a href="/login" style="color:#5eb6e5">Sign in</a> to play it.';
      return;
    }}
    if (j.status === "failed") {{
      document.getElementById("status").textContent = "✗ could not fetch this file";
      document.getElementById("hint").textContent =
        "The source message may have been deleted or the channel is no longer reachable.";
      return;
    }}
    if (j.status === "ready") {{
      document.getElementById("status").textContent = "✓ ready — opening player…";
      renderPlayer();
      return;
    }}
    if (j.status === "downloading") {{
      const pct = j.total > 0 ? (j.bytes / j.total * 100) : 0;
      document.getElementById("status").textContent =
        `downloading ${{fmtBytes(j.bytes)}}${{j.total ? " / " + fmtBytes(j.total) : ""}} (${{pct.toFixed(0)}}%)`;
      document.getElementById("progBar").style.width = pct + "%";
      // Start the player as soon as ~2MB or 10% is on disk — progressive
      // playback can then keep up with the download. Only for video and
      // audio: a book or a PDF is useless until it is whole.
      if ((MEDIA_TYPE === "video" || MEDIA_TYPE === "audio") &&
          (j.bytes > 2_000_000 || (j.total && pct > 10))) {{
        renderPlayer();
        return;
      }}
    }} else if (j.status === "queued") {{
      if (j.tg_wait && j.tg_wait.seconds_left > 0) {{
        const mins = Math.max(1, Math.round(j.tg_wait.seconds_left / 60));
        document.getElementById("status").textContent =
          "⏸ Telegram asked us to slow down — starts by itself in about " + mins + " min";
        document.getElementById("hint").textContent =
          "Your request stays queued. Nothing is wrong with the file or your account; " +
          "tgarr is waiting instead of pushing, to keep your Telegram account in good standing.";
      }} else {{
        document.getElementById("status").textContent = "queued — waiting for crawler…";
      }}
    }}
  }} catch(e) {{
    document.getElementById("status").textContent = "network error — retrying…";
  }}
  setTimeout(poll, 1500);
}}
poll();
</script>
<script src="/mc/ping.js" defer></script>
</body></html>"""
    return HTMLResponse(body)


# ════════════════════════════════════════════════════════════════════
# Login (Telegram QR + SMS)
# ════════════════════════════════════════════════════════════════════
LOGIN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>tgarr — sign in to Telegram</title>
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
    window._qrPng = r.qr_png_base64;
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
    let s;
    try {
      s = await fetch('/api/login/qr/status').then(r => r.json());
    } catch (e) { continue; }
    // Login codes expire after ~30s; show whichever one is current.
    if (s.qr_png_base64 && s.qr_png_base64 !== window._qrPng) {
      window._qrPng = s.qr_png_base64;
      document.getElementById('qrimg').src = 'data:image/png;base64,' + s.qr_png_base64;
      if (s.message) setStatus('qr', s.message, '');
    }
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


APP_SETUP_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>tgarr - first-time setup</title>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f1f5f9;color:#0f172a;
  display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px;box-sizing:border-box}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;max-width:560px;width:100%;padding:30px 32px;
  box-shadow:0 10px 30px rgba(15,23,42,.08)}
h1{margin:0 0 6px;font-size:24px}h1 span{color:#0ea5e9}
p{color:#475569;line-height:1.55;margin:8px 0}
ol{color:#334155;line-height:1.7;padding-left:20px;margin:14px 0 18px}
label{display:block;font-size:13px;font-weight:600;color:#334155;margin:14px 0 5px}
input{width:100%;box-sizing:border-box;padding:11px 12px;font:inherit;font-size:15px;border:1px solid #cbd5e1;
  border-radius:8px;font-family:ui-monospace,Consolas,monospace}
input:focus{outline:2px solid #0ea5e9;border-color:#0ea5e9}
button{margin-top:20px;width:100%;padding:12px;font:inherit;font-size:15px;font-weight:600;color:#fff;
  background:#0ea5e9;border:0;border-radius:8px;cursor:pointer}
button:disabled{opacity:.6;cursor:default}
#err{color:#b91c1c;font-size:14px;margin-top:12px;min-height:20px}
small{color:#64748b;font-size:12.5px;line-height:1.5;display:block;margin-top:16px}
a{color:#0284c7}
</style></head><body><div class="card">
<h1>tg<span>arr</span> - one step before signing in</h1>
<p>Telegram asks every program that connects to it for an <b>app id</b> and <b>app hash</b>.
They are free, belong to your own account, and take a minute to get:</p>
<ol>
<li>Open <a href="https://my.telegram.org/apps" target="_blank" rel="noopener">my.telegram.org/apps</a>
and sign in with your phone number (the code arrives in Telegram itself).</li>
<li>Fill in "Create new application" - any title and short name will do - and press Create.</li>
<li>Copy <b>App api_id</b> and <b>App api_hash</b> into the two boxes below.</li>
</ol>
<form id="f" autocomplete="off">
<label for="i">App api_id</label><input id="i" name="api_id" inputmode="numeric" placeholder="1234567" required/>
<label for="h">App api_hash</label><input id="h" name="api_hash" placeholder="32 letters and digits" required/>
<button id="b" type="submit">Save and continue to sign-in</button>
<div id="err"></div>
</form>
<small>The two values stay on this machine, beside the Telegram session. Setting TG_API_ID and
TG_API_HASH in the .env file does the same thing and takes precedence.</small>
</div>
<script>
document.getElementById("f").addEventListener("submit", async function (e) {
  e.preventDefault();
  const b = document.getElementById("b"), err = document.getElementById("err");
  b.disabled = true; err.textContent = "";
  try {
    const r = await fetch("/api/login/app", {method: "POST", body: new FormData(e.target)}).then(r => r.json());
    if (r.status === "ok") { location.href = "/login"; return; }
    err.textContent = r.message || "That did not work.";
  } catch (x) { err.textContent = "Could not reach tgarr: " + x; }
  b.disabled = false;
});
</script></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page(preview: Optional[int] = None):
    # .revoked marker overrides "already signed in" — session file is stale,
    # account was logged out upstream, must re-auth before doing anything.
    if login.session_exists() and not preview and not _is_revoked():
        return RedirectResponse("/")
    if not login.app_configured():
        return HTMLResponse(APP_SETUP_HTML)
    return HTMLResponse(LOGIN_HTML)


@app.post("/api/login/app")
async def api_login_app(api_id: str = Form(""), api_hash: str = Form("")):
    return login.save_app(api_id, api_hash)


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


PRIVATE_KEEP_DAYS = 365


async def hold_private_channel_data(uid) -> dict:
    """Signing out: keep the account's private chats instead of deleting them.
    They are marked as held for that account and disappear from every page
    and address until the same account signs in again - then they are simply
    there, and only what is new has to be fetched. Another account signing in
    here does not see them. Nothing held longer than the keeping period
    (config 'private_keep_days', a year unless changed) survives."""
    async with db_pool.acquire() as conn:
        r = await conn.execute(
            """UPDATE channels SET held_at = NOW(), account_user_id = COALESCE(account_user_id, $1)
               WHERE username IS NULL AND held_at IS NULL
                 AND ($1::bigint IS NULL OR COALESCE(account_user_id, $1) = $1)""", uid)
    n = int(r.split()[-1]) if r.startswith("UPDATE") else 0
    _PUBLIC_MSG.clear()
    logging.getLogger("tgarr").info("[sign-out] %d private chats kept for the account", n)
    return {"kept": n}


async def _purge_private_channels(target_ids: list) -> dict:
    """Really remove private chats (their messages, releases, downloads)."""
    if not target_ids:
        return {"channels": 0}
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            rel_target_q = """
                SELECT r.id FROM releases r
                JOIN messages m ON m.id = r.primary_msg_id
                WHERE m.channel_id = ANY($1::int[])"""
            await conn.execute(
                f"DELETE FROM downloads WHERE release_id IN ({rel_target_q})", target_ids)
            await conn.execute(
                f"UPDATE monitored_movies SET grabbed_release_id = NULL "
                f"WHERE grabbed_release_id IN ({rel_target_q})", target_ids)
            await conn.execute(
                """DELETE FROM releases WHERE primary_msg_id IN (
                       SELECT id FROM messages WHERE channel_id = ANY($1::int[]))""", target_ids)
            await conn.execute("DELETE FROM channels WHERE id = ANY($1::int[])", target_ids)
    return {"channels": len(target_ids)}


async def _private_keep_days() -> int:
    async with db_pool.acquire() as conn:
        v = await conn.fetchval("SELECT value FROM config WHERE key = 'private_keep_days'")
    try:
        return max(1, min(int(v), 3650)) if v else PRIVATE_KEEP_DAYS
    except ValueError:
        return PRIVATE_KEEP_DAYS


async def _private_keep_worker():
    """Every half hour: the account signed in gets its held chats back; what
    another account left here longer ago than the keeping period is removed."""
    await asyncio.sleep(120)
    while True:
        try:
            uid = _me_id()
            days = await _private_keep_days()
            async with db_pool.acquire() as conn:
                if uid is not None:
                    back = await conn.execute(
                        """UPDATE channels SET held_at = NULL
                           WHERE username IS NULL AND held_at IS NOT NULL
                             AND COALESCE(account_user_id, $1) = $1""", uid)
                    if back.split()[-1] != "0":
                        _PUBLIC_MSG.clear()
                        logging.getLogger("tgarr").info("[sign-in] private chats shown again: %s", back)
                old = [r["id"] for r in await conn.fetch(
                    """SELECT id FROM channels
                       WHERE username IS NULL AND held_at < NOW() - make_interval(days => $1)""", days)]
            if old:
                await _purge_private_channels(old)
                logging.getLogger("tgarr").info(
                    "[private] %d chats held longer than %d days removed", len(old), days)
        except Exception as e:
            logging.getLogger("tgarr").warning("[private] keep worker: %s", e)
        await asyncio.sleep(1800)


@app.get("/api/settings/private_keep_days")
async def get_private_keep_days():
    return {"days": await _private_keep_days()}


@app.post("/api/settings/private_keep_days")
async def set_private_keep_days(request: Request):
    try:
        days = max(1, min(int((await request.json()).get("days")), 3650))
    except Exception:
        return JSONResponse({"status": "error", "message": "a number of days, 1 to 3650"}, status_code=400)
    async with db_pool.acquire() as conn:
        await conn.execute("""INSERT INTO config (key, value) VALUES ('private_keep_days', $1)
                              ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()""", str(days))
    return {"status": "ok", "days": days}


@app.post("/api/login/logout")
async def api_login_logout():
    # Keep, do not delete: the account's private chats stay on this machine,
    # hidden, until it signs in again (or the keeping period runs out).
    uid = _me_id()
    try:
        wipe = await hold_private_channel_data(uid)
    except Exception as e:
        logging.getLogger("tgarr").exception("[logout] keeping private chats failed: %s", e)
        wipe = {"error": str(e)}
    return {**login.logout(), "wiped": wipe}


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


@app.post("/api/downloads/{dl_id}/pause")
async def api_download_pause(dl_id: int):
    """Flip row to 'paused'. Crawler's cancel-watcher will abort in-flight
    download if currently 'downloading'. .temp file is left on disk so
    /resume can pick up where we left off (Pyrogram redownloads from 0
    but GREATEST keeps UI monotonic until new download passes the mark)."""
    async with db_pool.acquire() as conn:
        n = await conn.execute(
            "UPDATE downloads SET status='paused', speed_kbps=0 "
            "WHERE id=$1 AND status IN ('pending','downloading')",
            dl_id)
    return {"status": "ok", "affected": n}


@app.post("/api/downloads/{dl_id}/resume")
async def api_download_resume(dl_id: int):
    """Flip 'paused' or 'failed' back to 'pending' so the dispatcher
    picks it up again. Doesn't reset bytes_done (UI stays monotonic;
    new attempt's progress will eventually pass the old high-water)."""
    async with db_pool.acquire() as conn:
        n = await conn.execute(
            "UPDATE downloads SET status='pending', error_message=NULL "
            "WHERE id=$1 AND status IN ('paused','failed')",
            dl_id)
    return {"status": "ok", "affected": n}


@app.post("/api/downloads/{dl_id}/delete")
async def api_download_delete(dl_id: int):
    """Cancel/forget a download. For active rows: flip to 'cancelled' so
    the crawler's watcher aborts + cleans .temp. For terminal rows:
    drop the DB row entirely so it disappears from history."""
    async with db_pool.acquire() as conn:
        cur = await conn.fetchrow(
            "SELECT status FROM downloads WHERE id=$1", dl_id)
        if not cur:
            return JSONResponse({"status": "missing"}, status_code=404)
        if cur["status"] in ("pending", "downloading", "paused"):
            await conn.execute(
                "UPDATE downloads SET status='cancelled' WHERE id=$1", dl_id)
        else:
            await conn.execute("DELETE FROM downloads WHERE id=$1", dl_id)
    return {"status": "ok", "prev": cur["status"]}


@app.post("/api/internal/arr-scan")
async def api_internal_arr_scan(payload: dict):
    """Crawler → api → Sonarr/Radarr push notification.

    Crawler doesn't carry SONARR/RADARR creds; api does. After a download
    finishes, crawler POSTs {"path": "...", "kind": "tv"|"movie"} here.
    This calls DownloadedEpisodesScan / DownloadedMoviesScan so the file
    is imported even when Sonarr's queue tracker dropped the downloadId
    (which happens on any tgarr-api downtime — the failure mode that
    orphaned bite-China episodes 2-8 today).
    """
    path = payload.get("path")
    kind = payload.get("kind", "tv")
    if not path:
        return JSONResponse({"status": "error", "message": "path required"},
                          status_code=400)
    if kind == "movie":
        url = os.environ.get("RADARR_URL", "")
        key = os.environ.get("RADARR_API_KEY", "")
        cmd = "DownloadedMoviesScan"
    else:
        url = os.environ.get("SONARR_URL", "")
        key = os.environ.get("SONARR_API_KEY", "")
        cmd = "DownloadedEpisodesScan"
    if not (url and key):
        return JSONResponse({"status": "skipped", "reason": "no key"},
                          status_code=200)
    # Map crawler-side path (/downloads/tgarr/...) to host-side path that
    # Sonarr sees. Sonarr's remotePathMapping translates from host -> its
    # localPath. We feed it host paths.
    sonarr_path = path.replace("/downloads/tgarr/",
                               "/DATA/Downloads/tgarr/tgarr/", 1)
    body = _json.dumps({"name": cmd, "path": sonarr_path,
                        "importMode": "move"}).encode()
    req = urllib.request.Request(
        f"{url}/api/v3/command", data=body,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
        method="POST")
    try:
        result = await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=10).read())
        return {"status": "ok", "cmd": cmd, "path": sonarr_path}
    except Exception as e:
        return JSONResponse(
            {"status": "error", "message": str(e)[:200]}, status_code=500)


@app.post("/api/grab/{guid}")
async def api_grab(guid: str):
    """User clicks Grab in tgarr UI → queue download directly. Avoids the
    Newznab `t=get` path that returns an .nzb file (only useful to Sonarr).
    """
    # Grab fetches via MTProto in the crawler — no account = no fetch path.
    # Block at the API to avoid queuing rows that would sit forever.
    if not login.session_exists() or _is_revoked():
        return JSONResponse(
            {"status": "error", "message": "not signed in — visit /login to authenticate"},
            status_code=401)
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


# Which copy of a work to show first when there are several.
#   quality (default): the higher resolution wins; among equal resolutions the
#                      copy on the account's own Telegram data center wins.
#   dc:                a copy on the account's own data center wins outright,
#                      then resolution.
# "Best" is per user: a file on DC5 is the fast path for a DC5 account and a
# cross-DC fetch for everyone else, so this is decided here, on each install,
# never centrally. Nothing is hidden for good - every version stays reachable.
async def _version_pref() -> str:
    async with db_pool.acquire() as conn:
        v = await conn.fetchval("SELECT value FROM config WHERE key='version_preference'")
    pref = v if v in ("quality", "dc") else "quality"
    # While Telegram is refusing cross-DC work, another DC's copy cannot be
    # fetched at all: lead with what can.
    fl = await _tg_flood()
    if fl and ("cross-DC" in (fl.get("what") or "") or "export" in (fl.get("what") or "").lower()):
        return "dc"
    return pref


_QUALITY_RANK_SQL = ("(CASE r.quality WHEN '2160p' THEN 5 WHEN '1440p' THEN 4 WHEN '1080p' THEN 3 "
                     "WHEN '720p' THEN 2 WHEN '576p' THEN 1 WHEN '480p' THEN 1 ELSE 0 END)")
# Identity of a work, for grouping its versions: normalized title + year for a
# movie, title + season + episode for an episode. NULL = not groupable.
_WORK_KEY_SQL = (
    "(CASE WHEN r.category = 'movie' AND COALESCE(NULLIF(r.canonical_title, ''), NULLIF(r.movie_title, '')) IS NOT NULL "
    "      THEN 'm:' || lower(regexp_replace(COALESCE(NULLIF(r.canonical_title, ''), r.movie_title), "
    "                         '[[:space:][:punct:]]+', '', 'g')) || ':' || COALESCE(r.movie_year::text, '') "
    "           || COALESCE(':part' || r.part::text, '') "
    "      WHEN r.category = 'tv' AND r.season IS NOT NULL "
    "           AND COALESCE(NULLIF(r.canonical_title, ''), NULLIF(r.series_title, '')) IS NOT NULL "
    "      THEN 't:' || lower(regexp_replace(COALESCE(NULLIF(r.canonical_title, ''), r.series_title), "
    "                         '[[:space:][:punct:]]+', '', 'g')) || ':s' || r.season::text "
    "           || 'e' || COALESCE(r.episode::text, 'x') "
    " END)")


@app.post("/api/settings/version_preference")
async def settings_version_preference(value: str = Form("quality")):
    val = "dc" if value == "dc" else "quality"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO config (key, value) VALUES ('version_preference', $1)
               ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=NOW()""", val)
    return RedirectResponse("/settings", status_code=302)


@app.post("/api/settings/nsfw_enabled")
async def settings_nsfw_enabled(value: str = Form("false")):
    val = "true" if value == "true" else "false"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO config (key, value) VALUES ('nsfw_enabled', $1)
               ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=NOW()""", val)
        if val == "true":
            # fetch the adult part of the Discover list now, not in 12 hours
            await conn.execute(
                """INSERT INTO config (key, value) VALUES ('registry_pull_force', $1)
                   ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=NOW()""",
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
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
        # Reset keyless / negative results so the next worker pass retries with the new key
        await conn.execute("""
            UPDATE releases SET metadata_source=NULL, poster_url=NULL,
                   canonical_title=NULL, overview=NULL, metadata_lookup_at=NULL
            WHERE metadata_source IN ('miss', 'wikipedia', 'wikidata', 'unmatched')
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
.user { padding:10px 12px; border-top:1px solid var(--border); font-size:14px; color:var(--muted); }
.user .name { color:var(--fg); font-weight:600; font-size:15px; }
.ub-wrap { position:relative; }
.ub { display:flex; align-items:center; gap:10px; width:100%; padding:8px 10px; border:0; background:none;
      border-radius:10px; font:inherit; color:inherit; text-align:left; cursor:pointer; }
button.ub:hover { background:rgba(127,127,127,.1); }
div.ub { cursor:default; }
.ub-av { width:38px; height:38px; border-radius:50%; flex:none; object-fit:cover; display:flex; align-items:center;
         justify-content:center; color:#fff; font-weight:700; font-size:16px; background:var(--accent); }
.ub-av.none { background:rgba(127,127,127,.25); color:var(--muted); }
.ub-txt { flex:1; min-width:0; line-height:1.25; }
.ub-txt b { display:block; color:var(--fg); font-size:14px; font-weight:600; white-space:nowrap; overflow:hidden;
            text-overflow:ellipsis; }
.ub-txt small { display:block; font-size:12px; color:var(--muted); white-space:nowrap; overflow:hidden;
                text-overflow:ellipsis; }
.ub-ch { color:var(--muted); font-size:18px; line-height:1; padding:0 2px; }
.ub-menu { position:absolute; left:0; right:0; bottom:calc(100% + 6px); background:var(--surface);
           border:1px solid var(--border); border-radius:10px; box-shadow:0 10px 30px rgba(15,23,42,.16);
           padding:6px; z-index:60; }
.ub-menu a, .ub-menu button { display:block; width:100%; box-sizing:border-box; padding:9px 12px; border:0;
           background:none; border-radius:7px; font:inherit; font-size:14px; color:var(--fg); text-align:left;
           text-decoration:none; cursor:pointer; }
.ub-menu a:hover, .ub-menu button:hover { background:rgba(127,127,127,.1); }
.ub-menu button { color:#dc2626; border-top:1px solid var(--border); border-radius:0 0 7px 7px; margin-top:4px; }
.user a.ub-lk { display:block; text-align:center; margin:2px 10px 4px; padding:7px 12px; font-size:13px; color:var(--muted);
                border:1px solid var(--border); border-radius:8px; }
.user .ub-menu a { color:var(--fg); font-weight:400; }
.user a.ub-in { color:#fff; }
.ub-in { display:block; margin:6px 10px 4px; padding:8px 12px; border-radius:8px; background:var(--accent);
         color:#fff; text-align:center; text-decoration:none; font-weight:600; font-size:13px; }
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
.dl-progress { margin-top:8px; }
.dl-progress-bar { background:#e2e8f0; border-radius:4px; height:10px; overflow:hidden; }
.dl-progress-fill { height:100%; background:linear-gradient(90deg, var(--accent), #5eb6e5); transition:width 1s linear; }
.dl-progress-meta { font-size:12px; color:var(--muted); margin-top:3px; font-family:ui-monospace,monospace; }

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
.poster-card .poster { height:200px; background:#f1f5f9; position:relative; overflow:hidden; }
.poster-card .poster .fallback { display:flex; width:100%; height:100%; align-items:center; justify-content:center; font-size:56px; color:#cbd5e1; background:#f1f5f9; }
.poster-card .poster .poster-img { display:block; width:100%; height:100%; object-fit:cover; background:#f1f5f9; }
.poster-card .poster .badge { position:absolute; top:8px; right:8px; padding:3px 10px; border-radius:11px; background:rgba(15,23,42,0.78); color:#fff; font-size:11px; font-weight:700; letter-spacing:0.5px; text-transform:uppercase; backdrop-filter:blur(4px); }
.poster-card .info { padding:10px 12px 4px; min-height:84px; flex:1; }
.poster-card .info .title { font-weight:700; font-size:15px; line-height:1.3; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; min-height:39px; color:var(--fg); }
.poster-card .info .sub { font-size:13px; color:var(--muted); margin-top:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.poster-card .pills { padding:0 12px 8px; display:flex; gap:4px; flex-wrap:wrap; min-height:24px; }
.poster-card .grab-row { padding:8px 14px 12px; border-top:1px solid var(--border); margin-top:auto; background:var(--surface2); }
.poster-card .grab-row .btn { width:100%; text-align:center; padding:8px; font-size:13px; }
/* the media card: one shape for films, episodes and plain videos */
.mc { background:var(--surface); border:1px solid var(--border); border-radius:10px; overflow:hidden; display:flex; flex-direction:column; transition:transform .12s, box-shadow .12s, border-color .12s; min-width:0; }
.mc:hover { transform:translateY(-3px); box-shadow:0 10px 24px rgba(15,23,42,.12); border-color:var(--accent); }
.mc-img { position:relative; display:block; aspect-ratio:2/3; background:#e9eef4; overflow:hidden; }
.mc.wide .mc-img { aspect-ratio:16/10; }
.mc-img img { position:relative; z-index:1; width:100%; height:100%; object-fit:cover; display:block; }
.mc-img .bq, .mc-img .bs, .mc-img .bp, .mc-img .pl { z-index:2; }
.mc-img .fb { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:46px; color:#b8c4d2; }
.mc-img .bq { position:absolute; top:8px; right:8px; padding:3px 8px; border-radius:6px; background:rgba(15,23,42,.8); color:#fff; font-size:10.5px; font-weight:700; letter-spacing:.4px; text-transform:uppercase; }
.mc-img .bs { position:absolute; top:8px; left:8px; padding:3px 8px; border-radius:6px; background:rgba(5,150,105,.92); color:#fff; font-size:10.5px; font-weight:700; }
.mc-img .bv { position:absolute; bottom:8px; right:8px; padding:3px 8px; border-radius:6px; background:rgba(37,99,235,.92); color:#fff; font-size:10.5px; font-weight:700; text-decoration:none; }
.mc-img .bp { position:absolute; bottom:8px; left:8px; padding:3px 8px; border-radius:6px; background:rgba(15,23,42,.8); color:#fff; font-size:10.5px; font-weight:700; }
.mc-img .pl { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; background:rgba(15,23,42,.28); opacity:0; transition:opacity .15s; }
.mc-img .pl span { width:54px; height:54px; border-radius:50%; background:rgba(255,255,255,.94); color:#0f172a; display:flex; align-items:center; justify-content:center; font-size:22px; padding-left:4px; }
.mc:hover .mc-img .pl { opacity:1; }
.mc-over { position:relative; height:0; }
.mc-over .bv { position:absolute; z-index:3; right:8px; bottom:8px; padding:3px 8px; border-radius:6px; background:rgba(37,99,235,.94); color:#fff; font-size:10.5px; font-weight:700; text-decoration:none; }
.mc-over .bv:hover { background:#1d4ed8; }
.mc-body { padding:10px 12px 8px; flex:1; min-width:0; }
.mc-title { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; font-weight:700; font-size:14.5px; line-height:1.3; min-height:38px; color:var(--fg); text-decoration:none; word-break:break-word; }
.mc-title:hover { color:var(--accent); }
.mc-sub { font-size:12.5px; color:var(--muted); margin-top:5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-height:17px; }
.mc-meta { font-size:12px; color:var(--muted); margin-top:4px; display:flex; gap:8px; align-items:center; min-height:20px; white-space:nowrap; overflow:hidden; }
.mc-meta .dc { font-weight:600; font-size:11px; padding:1px 6px; border-radius:8px; background:rgba(245,158,11,.16); color:#b45309; }
.mc-meta .dc.same { background:rgba(16,185,129,.16); color:#047857; }
.mc-act { display:flex; gap:6px; padding:8px 10px 10px; border-top:1px solid var(--border); background:var(--surface2); }
.mc-act .b { flex:1 1 0; height:34px; display:inline-flex; align-items:center; justify-content:center; gap:5px; border-radius:8px; border:1px solid var(--border); background:var(--surface); color:var(--fg); font:inherit; font-size:13px; font-weight:600; cursor:pointer; text-decoration:none; white-space:nowrap; padding:0 8px; min-width:0; }
.mc-act .b:hover { border-color:var(--accent); color:var(--accent); }
.mc-act .b.pri { background:var(--accent); border-color:var(--accent); color:#fff; }
.mc-act .b.pri:hover { filter:brightness(1.08); color:#fff; }
.mc-act .b.sq { flex:0 0 34px; padding:0; }
.mc-act .b.on { color:#e11d48; }
.mc-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(176px,1fr)); gap:16px; }
.mc-grid.wide { grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); }
.view-toggle { display:inline-flex; border:1px solid var(--border); border-radius:6px; overflow:hidden; }
.view-toggle a { padding:6px 14px; color:var(--muted); text-decoration:none; font-size:13px; font-weight:600; }
.view-toggle a.active { background:var(--accent); color:#fff; }

/* ── Image gallery ─────────────────── */
.gallery { columns:5 240px; column-gap:14px; }
.gallery .item { break-inside:avoid; margin-bottom:14px; position:relative; border-radius:8px; overflow:hidden; cursor:zoom-in; box-shadow:var(--shadow); }
.gallery .item img { width:100%; display:block; background:#f1f5f9; transition:transform 0.15s; }
.gallery .item:hover img { transform:scale(1.02); }
/* a tile whose picture has not arrived: visibly loading, not blank */
.gallery .item { min-height:170px; background:linear-gradient(100deg,#e3e9f0 30%,#f4f7fa 50%,#e3e9f0 70%); background-size:200% 100%; animation:tgShim 1.3s linear infinite; }
.gallery .item::before { content:'🖼'; position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:36px; opacity:.4; }
.gallery .item img { opacity:0; }
.gallery .item.loaded { min-height:0; background:none; animation:none; }
.gallery .item.loaded::before { display:none; }
.gallery .item.loaded img { opacity:1; transition:opacity .25s, transform .15s; }
.gallery .item.nopic { animation:none; background:#eef1f5; cursor:default; }
.gallery .item.nopic::before { content:'picture not available'; font-size:12.5px; opacity:.8; color:#64748b; }
.gallery .item.nopic img { display:none; }
@keyframes tgShim { to { background-position:-200% 0; } }
.gallery .item .meta { position:absolute; left:0; right:0; bottom:0; padding:18px 12px 8px; background:linear-gradient(transparent, rgba(0,0,0,0.78)); color:#fff; font-size:12px; opacity:0; transition:opacity 0.15s; }
.gallery .item:hover .meta { opacity:1; }
.gallery .item .meta .ch { font-weight:700; }
.gallery .item .meta .cap { margin-top:4px; line-height:1.35; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.gallery .item .dup-badge { position:absolute; top:8px; right:8px; padding:3px 10px; border-radius:11px; background:rgba(34,158,217,0.92); color:#fff; font-size:11px; font-weight:800; letter-spacing:0.5px; backdrop-filter:blur(4px); }

/* ── Lightbox + slideshow ─────────────────── */
.lightbox { position:fixed; inset:0; background:rgba(15,23,42,0.96); display:none; align-items:center; justify-content:center; z-index:9999; padding:20px; overflow:hidden; }
.lightbox.open { display:flex; }
.lightbox img { z-index:1; max-width:96vw; max-height:90vh; object-fit:contain; border-radius:6px; box-shadow:0 16px 50px rgba(0,0,0,0.5); will-change:transform,opacity,filter; image-rendering:auto; transition:filter .35s ease; }
.lightbox img.soft { filter:blur(9px) brightness(.92); }
.lightbox .lb-meta { position:absolute; left:32px; right:32px; bottom:80px; color:#fff; font-size:15px; line-height:1.55; max-width:660px; pointer-events:none; transition:opacity 0.3s; }
.lightbox .lb-meta .ch { color:var(--accent-hi); font-weight:700; font-size:13px; text-transform:uppercase; letter-spacing:1px; margin-bottom:4px; }
.lightbox .lb-close { z-index:10; position:absolute; top:18px; right:24px; color:#fff; font-size:36px; cursor:pointer; opacity:0.6; line-height:1; padding:6px 12px; background:none; border:none; }
.lightbox .lb-close:hover { opacity:1; }
.lightbox .lb-prev, .lightbox .lb-next { z-index:10; position:absolute; top:50%; margin-top:-28px; width:56px; height:56px; padding:0; border-radius:50%; background:rgba(255,255,255,0.10); color:#fff; border:none; cursor:pointer; display:flex; align-items:center; justify-content:center; opacity:0.75; transition:opacity .15s, background .15s; }
.lightbox .lb-prev svg, .lightbox .lb-next svg { width:26px; height:26px; display:block; }
.lightbox .lb-prev { left:24px; } .lightbox .lb-next { right:24px; }
.lightbox .lb-prev:hover, .lightbox .lb-next:hover { opacity:1; background:rgba(255,255,255,0.20); }
.lightbox .lb-controls { z-index:10; position:absolute; bottom:24px; left:50%; transform:translateX(-50%); display:flex; align-items:center; gap:14px; padding:8px 16px; background:rgba(255,255,255,0.08); border-radius:24px; backdrop-filter:blur(8px); }
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
.book-card .book-ico { font-size:48px; flex-shrink:0; line-height:1; width:60px; height:80px; display:flex; align-items:center; justify-content:center; }
.book-card .book-cover { width:60px; height:80px; object-fit:cover; border-radius:3px; flex-shrink:0; box-shadow:0 1px 4px rgba(0,0,0,0.15); }
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
    ("/videos",    "Videos",    "M8 5v14l11-7z"),
    ("/music",     "Music",     "M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"),
    ("/library",   "Library",   "M18 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 18H6V4h7v6l2-1 2 1V4h1v16z"),
    ("/downloads", "Downloads", "M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"),
    # /system page still accessible by URL, just hidden from nav (admin only).
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


def _fmt_count(n) -> str:
    """12 -> 12, 8700 -> 8.7K, 12521500 -> 12.5M. ("12521.5K" is not a number
    anyone reads.)"""
    if n is None:
        return "–"
    n = int(n)
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(n) >= div:
            v = n / div
            return (f"{v:.0f}" if v >= 100 else f"{v:.1f}".rstrip("0").rstrip(".")) + suf
    return str(n)


def _n_things(n, word: str) -> str:
    """ "1 film", "3.1K films" """
    return f"{_fmt_count(n)} {word[:-1] if n == 1 and word.endswith('s') else word}"


def _ago(dt) -> str:
    """A moment as people say it: "3 min ago", "2 h ago", "5 d ago", then a date."""
    if not dt:
        return ""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    sec = (now - dt).total_seconds()
    if sec < 0:
        return dt.strftime("%Y-%m-%d")
    if sec < 90:
        return "just now"
    if sec < 3600:
        return f"{int(sec // 60)} min ago"
    if sec < 86400:
        return f"{int(sec // 3600)} h ago"
    if sec < 86400 * 30:
        return f"{int(sec // 86400)} d ago"
    return dt.strftime("%Y-%m-%d")


def _avatar(title: str, size: int = 40) -> str:
    t = (title or "?").strip() or "?"
    return (f'<div style="width:{size}px;height:{size}px;border-radius:50%;flex:none;'
            f'background:{_color_for(t)};color:#fff;display:flex;align-items:center;'
            f'justify-content:center;font-weight:700;font-size:{int(size * 0.42)}px">'
            f'{html.escape(t[0].upper())}</div>')


def _chip_link(label: str, href: str, on: bool, title: str = "") -> str:
    style = ("background:rgba(94,182,229,0.15);color:var(--accent-hi);"
             "border-color:var(--accent);" if on else "")
    return (f'<a class="btn ghost" href="{href}" title="{html.escape(title, quote=True)}" '
            f'style="{style}padding:5px 12px;font-size:12px">{label}</a>')


_LIST_CSS = """<style>
.ls{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.ls-row{display:flex;align-items:center;gap:14px;padding:12px 16px;border-bottom:1px solid var(--border)}
.ls-row:last-child{border-bottom:none}
.ls-row:hover{background:rgba(127,127,127,.05)}
.ls-main{flex:1 1 0;min-width:0}
.ls-title{font-weight:600;font-size:15px;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ls-title a{color:inherit;text-decoration:none}.ls-title a:hover{color:var(--accent)}
.ls-sub{font-size:13px;color:var(--muted);margin-top:3px;display:flex;gap:6px 12px;flex-wrap:wrap;align-items:center}
.ls-sub a{color:var(--muted);text-decoration:none}.ls-sub a:hover{color:var(--accent)}
.ls-num{flex:none;width:86px;text-align:right;font-variant-numeric:tabular-nums}
.ls-num b{display:block;font-size:15px;color:var(--fg);font-weight:600}
.ls-num span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.ls-act{flex:none;display:flex;gap:8px;justify-content:flex-end;min-width:118px}
.tag{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px;
  background:rgba(127,127,127,.12);color:var(--muted);white-space:nowrap}
.tag.ok{background:rgba(16,185,129,.14);color:#047857}.tag.warn{background:rgba(245,158,11,.16);color:#b45309}
.tag.bad{background:rgba(239,68,68,.13);color:#b91c1c}.tag.acc{background:rgba(94,182,229,.16);color:var(--accent-hi)}
.abtn{display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box;min-width:112px;height:34px;
  padding:0 14px;font:inherit;font-size:13px;font-weight:500;border:1px solid var(--border);background:var(--surface);
  color:var(--fg);border-radius:8px;cursor:pointer;text-decoration:none;white-space:nowrap}
.abtn:hover{border-color:var(--accent);color:var(--accent)}
.abtn.pri{background:var(--accent);border-color:var(--accent);color:#fff}.abtn.pri:hover{filter:brightness(1.08);color:#fff}
.abtn:disabled{opacity:.55;cursor:default}
.bar-tools{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.bar-tools .sep{width:1px;height:22px;background:var(--border);margin:0 6px}
.bar-tools .lbl{font-size:12px;color:var(--muted);margin-right:2px}
.ls-head{display:flex;align-items:center;gap:14px;padding:9px 16px;border-bottom:1px solid var(--border);
  background:rgba(127,127,127,.06);font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted);user-select:none}
.ls-head a{color:inherit;text-decoration:none;cursor:pointer;white-space:nowrap}
.ls-head a:hover,.ls-head a.on{color:var(--accent)}
.ls-head i{font-style:normal;font-size:9px;margin-left:4px;display:inline-block;width:8px}
.ls-head .ls-num{font-variant-numeric:normal}
.sort-chips{display:none;gap:6px;flex-wrap:wrap;align-items:center}
@media (max-width:820px){.ls-num{display:none}.ls-act{min-width:0}.abtn{min-width:0;padding:0 10px}
  .ls-head{display:none}.sort-chips{display:inline-flex}}
</style>"""


def _ls_head(cols, sort: str, order: str, href) -> str:
    """The clickable column header of a .ls list.

    cols: (key, label, css class, first direction). key None = not sortable.
    Clicking the column that is already sorted turns it round; clicking
    another sorts by it in the direction that is useful first (biggest /
    newest first for numbers and dates, A-Z for names)."""
    out = ['<div class="ls-head"><span style="width:40px;flex:none"></span>']
    for key, label, css, first in cols:
        if not key:
            out.append(f'<span class="{css}">{label}</span>')
            continue
        on = key == sort
        nxt = ("asc" if order == "desc" else "desc") if on else first
        arrow = ("▲" if order == "asc" else "▼") if on else ""
        out.append(f'<a class="{css}{" on" if on else ""}" href="{href(key, nxt)}" '
                   f'title="sort by {label}{" (click again to reverse)" if on else ""}">'
                   f'{label}<i>{arrow}</i></a>')
    return "".join(out) + "</div>"


def _ls_sort(view: list, keyfn, order: str, name) -> list:
    """Sort by keyfn in the given direction; rows without a value always go
    last, whichever way round; equal rows stay in name order."""
    view = sorted(view, key=name)
    have = [r for r in view if keyfn(r) not in (None, "")]
    rest = [r for r in view if keyfn(r) in (None, "")]
    have.sort(key=keyfn, reverse=(order == "desc"))
    return have + rest


def _color_for(s: str) -> str:
    """Deterministic accent color for an avatar."""
    palette = ["#229ED9", "#7c3aed", "#06b6d4", "#10b981", "#f59e0b",
               "#ec4899", "#ef4444", "#8b5cf6", "#14b8a6", "#f97316"]
    return palette[sum(ord(c) for c in (s or "?")) % len(palette)]


def _user_block() -> str:
    """The account at the foot of the side menu: picture, name, a small menu."""
    if not _signed_in():
        return ('<div class="ub"><div class="ub-av none">?</div>'
                '<div class="ub-txt"><b>Not signed in</b><small>browsing what is already indexed</small></div></div>'
                '<a class="ub-in" href="/login">Sign in to Telegram</a>'
                + ('<a class="ub-lk" href="/lock">🔒 Lock</a>' if _ACCESS["pin"] else ''))
    user = login.ensure_user_info() or {}
    uname = user.get("username")
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or uname or "Telegram account"
    try:
        av_v = int(os.path.getmtime(login.AVATAR_FILE))
        av = f'<img class="ub-av" src="/api/me/avatar?v={av_v}" alt="">'
    except OSError:
        av = (f'<div class="ub-av" style="background:{_color_for(name)}">'
              f'{html.escape(name[0].upper())}</div>')
    return (
        '<div class="ub-wrap">'
        '<div class="ub-menu" id="tgUserMenu" hidden>'
        '<a href="/settings">Settings</a>'
        '<a href="/system">System status</a>'
        '<a href="/mediacenter/manage">Media Center &amp; PIN</a>'
        + (f'<a href="https://t.me/{html.escape(uname)}" target="_blank" rel="noopener">My Telegram profile ↗</a>'
           if uname else '')
        + ('<a href="/lock">🔒 Lock (back to the Media Center)</a>' if _ACCESS["pin"] else '')
        + '<button type="button" onclick="tgSignOut()">Sign out</button></div>'
        '<button type="button" class="ub" onclick="tgUserMenu(event)" title="account">'
        f'{av}<span class="ub-txt"><b>{html.escape(name)}</b>'
        f'<small>{("@" + html.escape(uname)) if uname else "signed in"}</small></span>'
        '<span class="ub-ch">⋯</span></button></div>'
        '<script>'
        'function tgUserMenu(e){e.stopPropagation();var m=document.getElementById("tgUserMenu");m.hidden=!m.hidden}'
        'document.addEventListener("click",function(){var m=document.getElementById("tgUserMenu");if(m)m.hidden=true});'
        'function tgSignOut(){if(!confirm("Sign out of Telegram on this tgarr?\\n\\nYour private chats stay on this machine, hidden, and come back when you sign in again. Public channels and saved files stay.")){return}'
        'fetch("/api/login/logout",{method:"POST"}).then(function(){location.href="/"})}'
        '</script>')


# Phones / narrow windows: the 260px sidebar used to eat two thirds of the
# screen. Below 820px it becomes a compact top bar with a horizontally
# scrolling icon+label nav, and paddings / the search box shrink to fit.
MOBILE_CSS = """
@media (max-width: 820px) {
  html, body { height:auto; }
  body { flex-direction:column; font-size:15px; }
  aside.nav { width:100%; min-width:0; flex-direction:row; align-items:center;
              border-right:none; border-bottom:1px solid var(--border);
              position:sticky; top:0; z-index:50; }
  .brand { padding:8px 12px; border-bottom:none; flex-shrink:0; }
  .brand .mark { width:26px; height:26px; }
  .brand .logo { font-size:24px; letter-spacing:-1px; }
  .brand .logo-row { gap:6px; }
  .brand .ver { display:none; }
  .navlinks { display:flex; flex:1; min-width:0; padding:0; overflow-x:auto; overflow-y:hidden;
              -webkit-overflow-scrolling:touch; scrollbar-width:none; }
  .navlinks::-webkit-scrollbar { display:none; }
  .navlink { flex-direction:column; gap:2px; padding:8px 11px; font-size:11px;
             border-left:none; border-bottom:3px solid transparent; white-space:nowrap; }
  .navlink.active { border-left-color:transparent; border-bottom-color:var(--accent); }
  .navlink svg { width:20px; height:20px; }
  aside.nav .user { flex:none; padding:4px 8px; border-top:none; }
  aside.nav .user .ub-txt, aside.nav .user .ub-ch, aside.nav .user div.ub { display:none; }
  aside.nav .user .ub { padding:4px; width:auto; }
  aside.nav .user .ub-av { width:32px; height:32px; font-size:14px; }
  aside.nav .user .ub-menu { top:calc(100% + 8px); bottom:auto; left:auto; right:0; width:220px; }
  aside.nav .user .ub-in { margin:0; padding:7px 10px; font-size:12px; white-space:nowrap; }
  main.main { min-height:0; }
  .topbar { height:auto; padding:10px 14px; gap:10px; flex-wrap:wrap; }
  .topbar h1 { font-size:20px; }
  .topbar form { margin-left:0 !important; flex:1 1 100%; }
  .topbar input.search { width:100%; padding:8px 12px; font-size:15px; }
  .content { padding:16px 12px; overflow-y:visible; }
  .stats { grid-template-columns:repeat(2, minmax(0, 1fr)); gap:10px; margin-bottom:20px; }
  table { display:block; overflow-x:auto; max-width:100%; }
  img, video { max-width:100%; }
}
"""


def _layout(title: str, active: str, body_html: str, *, page_title: Optional[str] = None,
            top_actions_html: Optional[str] = None) -> str:
    locked = _LOCKED.get()
    nav = "".join(
        f'<a href="{path}" class="navlink{" active" if path == active else ""}">'
        f'{_icon(svg)}<span>{name}</span></a>'
        # a locked browser is shown the kids' pages and nothing else
        for path, name, svg in ([("/mediacenter", "Media Center", "M12 2l2.9 6.3 6.9.8-5.1 4.7 1.4 6.8L12 17.2 5.9 20.6l1.4-6.8L2.2 9.1l6.9-.8z")]
                                if locked else NAV_ITEMS)
    )
    if top_actions_html is None:
        cfg = {
            "/gallery":   ("/gallery",  "Search photos…"),
            "/videos":    ("/videos",   "Search videos…"),
            "/discover":  ("/discover", "Search discovered channels…"),
            "/music":     ("/music",    "Search music…"),
            "/library":   ("/library",  "Search ebooks…"),
            "/channels":  ("/channels", "Search channels…"),
            "/downloads": ("/downloads","Search downloads…"),
            "/releases":  ("/releases", "Search releases…"),
        }
        action, placeholder = cfg.get(active, ("/search", "Search…"))
        top_actions_html = (
            f'<form action="{action}" method="GET" style="margin-left:auto">'
            f'<input type="text" name="q" class="search" placeholder="{placeholder}" />'
            f'</form>'
        )
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>{html.escape(title)} · tgarr</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
<style>{CSS}{MOBILE_CSS}</style>
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
  <div class="user">{'<a class="ub-in" href="/unlock">🔒 Enter PIN</a>' if locked else _user_block()}</div>
</aside>
<main class="main">
  <header class="topbar">
    <h1>{html.escape(page_title or title)}</h1>
    {top_actions_html}
  </header>
  <div class="content">{body_html}</div>
</main>
<script>
// On a wide screen the list scrolls inside .content, not the window, so the
// browser does not put it back after a reload: any button that reloads the
// page (pause, remove, refresh…) sent the reader to the top. Remember the
// place per page and restore it - on a reload only, never on a fresh visit.
(function () {{
  var box = document.querySelector('.content');
  if (!box) return;
  var key = 'tgScroll:' + location.pathname + location.search;
  try {{
    var nav = (performance.getEntriesByType('navigation')[0] || {{}}).type;
    var saved = sessionStorage.getItem(key);
    if (nav === 'reload' && saved) box.scrollTop = parseInt(saved, 10) || 0;
    sessionStorage.removeItem(key);
    window.addEventListener('pagehide', function () {{
      if (box.scrollTop > 0) sessionStorage.setItem(key, String(box.scrollTop));
    }});
  }} catch (e) {{}}
}})();
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
<script>
// Thumbnails are fetched on demand. /api/thumb answers a not-ready image with
// a 1x1 placeholder; keep asking (with backoff) until the real one arrives.
// A failed thumbnail answers 410, which fires 'error' and shows the fallback.
(() => {{
  const MAX = 8;
  document.addEventListener('load', ev => {{
    const img = ev.target;
    if (!(img instanceof HTMLImageElement)) return;
    if (img.classList.contains('lazy-thumb')) return;   // gallery has its own hook
    const base = (img.getAttribute('src') || '').split('?')[0];
    if (!base.startsWith('/api/thumb/')) return;
    if (img.naturalWidth > 1) {{ img.style.opacity = ''; return; }}
    img._tgTry = (img._tgTry || 0) + 1;
    img.style.opacity = '0';
    if (img._tgTry > MAX) {{ img.dispatchEvent(new Event('error')); return; }}
    setTimeout(() => {{ img.src = base + '?r=' + Date.now(); }}, 1500 + img._tgTry * 1000);
  }}, true);
}})();
</script>
</body></html>"""


# ════════════════════════════════════════════════════════════════════
# Page: Dashboard
# ════════════════════════════════════════════════════════════════════
@app.get("/api/stats/history")
async def stats_history(metric: str, days: int = 7):
    """Time-series for one metric. Daily resolution. Returns [{at, value}]."""
    if metric not in ("channels", "messages", "releases", "videos", "photos",
                      "audio", "books", "completed"):
        return JSONResponse({"error": "unknown metric"}, status_code=400)
    days = max(1, min(days, 365))
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT date_trunc('day', snapshot_at) AS day, max(value) AS value "
            "FROM stats_history "
            "WHERE metric = $1 AND snapshot_at >= NOW() - ($2 || ' days')::INTERVAL "
            "GROUP BY day ORDER BY day", metric, str(days))
    return {"metric": metric, "days": days,
            "points": [{"at": r["day"].isoformat(), "value": r["value"]} for r in rows]}


@app.get("/api/stats/distribution")
async def stats_distribution():
    """Current snapshot of media_type distribution for pie chart."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT COALESCE(media_type, 'other') AS k, count(*) AS v "
            "FROM messages GROUP BY media_type ORDER BY count(*) DESC")
    return {"slices": [{"label": r["k"], "value": r["v"]} for r in rows]}


# Expected workers — used to detect "should be running but isn't" cases.
# expected_max_sec = max wall-clock between heartbeats during normal operation.
# Health = healthy if age < 0.5×expected, idle if < 2×expected, else stale.
_EXPECTED_WORKERS = {
    "subscription_poller":        30 * 60,    # 30min poll cycle
    "federation_validator":       3600 * 2,   # 20min batch + 1h sleep = ~80min between hb
    "deep_backfill_worker":       300,        # 10s/page, heartbeats per page = frequent
    "on_demand_media_downloader": 86400,      # only on user click, ok to be stale
    "contribute_resources_worker": 30 * 60,   # 30min interval
    "decay_eviction_worker":      6 * 3600,   # 6h
    "dialog_gc_worker":           6 * 3600,   # 6h
}


def _worker_health(name: str, last_seen, expected_max: int) -> str:
    """healthy | idle | stale | never"""
    if not last_seen:
        return "never"
    import datetime as _dt
    age = (_dt.datetime.now(last_seen.tzinfo) - last_seen).total_seconds()
    if age < expected_max * 0.5:
        return "healthy"
    if age < expected_max * 2:
        return "idle"
    return "stale"


@app.get("/api/workers")
async def api_workers():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT worker, last_seen, last_action, iter_count, "
            "error_count, last_error, last_error_at, quiet_for_s "
            "FROM worker_status ORDER BY last_seen DESC")
        _rec_mode = (await conn.fetchval(
            "SELECT value FROM config WHERE key='dialog_reconcile_mode'") or "off").lower()
    # A job that is switched off, or that an older version ran and this one
    # no longer has, is not a broken job: show it grey, not red.
    off = set()
    if _rec_mode not in ("report", "leave"):
        off.add("dialog_reconcile_worker")
    import datetime as _dt
    for r in rows:
        if (r["worker"] not in _EXPECTED_WORKERS and r["last_seen"]
                and (_dt.datetime.now(r["last_seen"].tzinfo) - r["last_seen"]).days >= 7):
            off.add(r["worker"])
    out = []
    seen = set()
    for r in rows:
        seen.add(r["worker"])
        expected_max = _EXPECTED_WORKERS.get(r["worker"], 3600)
        # the job said it would be quiet this long: silence within that
        # (plus a margin) is what it promised, not a fault
        if r["quiet_for_s"]:
            expected_max = max(expected_max, int(r["quiet_for_s"] * 1.25) + 120)
        out.append({
            "worker": r["worker"],
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "last_action": r["last_action"],
            "iter_count": r["iter_count"],
            "error_count": r["error_count"],
            "last_error": r["last_error"],
            "last_error_at": r["last_error_at"].isoformat() if r["last_error_at"] else None,
            "health": ("off" if r["worker"] in off
                       else _worker_health(r["worker"], r["last_seen"], expected_max)),
            "expected_max_sec": expected_max,
        })
    # Add expected-but-never-seen workers
    for w, exp in _EXPECTED_WORKERS.items():
        if w not in seen:
            out.append({
                "worker": w, "last_seen": None, "last_action": None,
                "iter_count": 0, "error_count": 0,
                "last_error": None, "last_error_at": None,
                "health": "never", "expected_max_sec": exp,
            })
    stale_or_never = [w for w in out if w["health"] in ("stale", "never")]
    return {"workers": out,
            "all_healthy": len(stale_or_never) == 0,
            "stale_or_never": [w["worker"] for w in stale_or_never]}


@app.get("/api/monitored")
async def api_monitored_list():
    """List monitored series + movies + recent auto-grab log."""
    async with db_pool.acquire() as conn:
        series = await conn.fetch(
            "SELECT ms.id, ms.series_title, ms.status, ms.monitored_from_season, "
            "qp.name AS profile, ms.added_at "
            "FROM monitored_series ms LEFT JOIN quality_profile qp ON qp.id=ms.quality_profile_id "
            "ORDER BY ms.added_at DESC")
        movies = await conn.fetch(
            "SELECT mm.id, mm.title, mm.year, mm.status, mm.grabbed_release_id, "
            "qp.name AS profile, mm.added_at "
            "FROM monitored_movies mm LEFT JOIN quality_profile qp ON qp.id=mm.quality_profile_id "
            "ORDER BY mm.added_at DESC")
        log_rows = await conn.fetch(
            "SELECT release_id, monitored_kind, monitored_id, action, reason, decided_at "
            "FROM auto_grab_log ORDER BY decided_at DESC LIMIT 50")
    return {
        "series": [dict(r) | {"added_at": r["added_at"].isoformat()} for r in series],
        "movies": [dict(r) | {"added_at": r["added_at"].isoformat()} for r in movies],
        "recent_log": [dict(r) | {"decided_at": r["decided_at"].isoformat()} for r in log_rows],
    }


@app.post("/api/monitored/series")
async def api_monitor_series(series_title: str = Form(...),
                              profile_id: int = Form(1)):
    """Add a TV series to auto-monitor list."""
    async with db_pool.acquire() as conn:
        row_id = await conn.fetchval(
            "INSERT INTO monitored_series (series_title, quality_profile_id) "
            "VALUES ($1, $2) RETURNING id",
            series_title.strip(), profile_id)
    return {"status": "ok", "id": row_id, "monitored": series_title}


@app.post("/api/monitored/movie")
async def api_monitor_movie(title: str = Form(...),
                             year: int = Form(None),
                             profile_id: int = Form(1)):
    async with db_pool.acquire() as conn:
        row_id = await conn.fetchval(
            "INSERT INTO monitored_movies (title, year, quality_profile_id) "
            "VALUES ($1, $2, $3) RETURNING id",
            title.strip(), year, profile_id)
    return {"status": "ok", "id": row_id, "monitored": title, "year": year}


@app.delete("/api/monitored/series/{id}")
async def api_unmonitor_series(id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM monitored_series WHERE id=$1", id)
    return {"status": "ok"}


@app.delete("/api/monitored/movie/{id}")
async def api_unmonitor_movie(id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM monitored_movies WHERE id=$1", id)
    return {"status": "ok"}


@app.get("/api/channels/quality")
async def api_channels_quality():
    """Per-channel quality scoreboard: media-ratio, release-style ratio, size median,
    composite 0-5 star quality. Used by dashboard to dismiss low-value subs +
    by federation v2 to prioritize task assignment to high-quality channels."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
              c.id, c.username, c.title, c.audience, c.content_category,
              c.members_count, c.subscribed,
              (SELECT count(*) FROM messages m WHERE m.channel_id=c.id) AS msgs_local,
              (SELECT count(*) FROM messages m WHERE m.channel_id=c.id
                 AND m.media_type IS NOT NULL) AS msgs_media,
              (SELECT count(*) FROM messages m WHERE m.channel_id=c.id
                 AND m.media_type='video') AS msgs_video,
              (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY m.file_size)
                 FROM messages m WHERE m.channel_id=c.id AND m.file_size > 0) AS size_median,
              (SELECT count(*) FROM releases r
                 JOIN messages m ON m.id = r.primary_msg_id
                 WHERE m.channel_id = c.id) AS release_count
            FROM channels c
            WHERE c.subscribed = TRUE
            ORDER BY msgs_local DESC NULLS LAST
        """)
    out = []
    for r in rows:
        msgs = r["msgs_local"] or 0
        media = r["msgs_media"] or 0
        rel = r["release_count"] or 0
        median_b = int(r["size_median"] or 0)
        media_ratio = (media / msgs) if msgs else 0.0
        release_ratio = (rel / media) if media else 0.0
        # Size signal: median > 50MB = movie/show territory
        size_score = min(1.0, median_b / 50_000_000)
        # Composite 0-5 star
        stars = round(
            (media_ratio * 0.30 + release_ratio * 0.50 + size_score * 0.20) * 5, 1)
        out.append({
            "username": r["username"], "title": r["title"],
            "audience": r["audience"], "category": r["content_category"],
            "members": r["members_count"],
            "msgs_local": msgs, "msgs_media": media, "msgs_video": r["msgs_video"] or 0,
            "release_count": rel,
            "media_ratio": round(media_ratio, 2),
            "release_ratio": round(release_ratio, 2),
            "size_median_mb": round(median_b / 1024 / 1024, 1),
            "quality_stars": stars,
        })
    return {"channels": out, "count": len(out)}


@app.get("/api/deep-backfill-progress")
async def api_deep_backfill_progress():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT username, title, deep_backfilled, deep_oldest_tg_id,
                   is_joined, subscribed,
                   deep_last_run_at, deep_total_pulled,
                   remote_msgs, remote_photos, remote_videos,
                   remote_audio, remote_documents,
                   (SELECT count(*) FROM messages m WHERE m.channel_id = channels.id) AS msgs_local,
                   (SELECT count(*) FROM messages m WHERE m.channel_id = channels.id AND m.media_type='photo') AS local_photos,
                   (SELECT count(*) FROM messages m WHERE m.channel_id = channels.id AND m.media_type='video') AS local_videos,
                   (SELECT count(*) FROM messages m WHERE m.channel_id = channels.id AND m.media_type='audio') AS local_audio,
                   (SELECT count(*) FROM messages m WHERE m.channel_id = channels.id AND m.media_type='document') AS local_documents
            FROM channels
            -- Working set only: actively followed (subscribed/joined) or still
            -- queued for crawl (enabled & not yet done). Released channels
            -- (done + unfollowed) and purged ones drop off the list — the
            -- moat is the contributed resource data, not the channel row.
            WHERE purged_at IS NULL
              AND (subscribed = TRUE OR is_joined = TRUE
                   OR (COALESCE(enabled, TRUE) = TRUE
                       AND COALESCE(deep_backfilled, FALSE) = FALSE))
            ORDER BY deep_backfilled ASC, COALESCE(remote_msgs, 0) DESC
        """)
        released_count = await conn.fetchval("""
            SELECT count(*) FROM channels
            WHERE purged_at IS NULL
              AND COALESCE(deep_backfilled, FALSE) = TRUE
              AND COALESCE(subscribed, FALSE) = FALSE
              AND COALESCE(is_joined, FALSE) = FALSE
        """)
        purged_count = await conn.fetchval(
            "SELECT count(*) FROM channels WHERE purged_at IS NOT NULL")
    def pct(local, remote):
        if not remote or remote <= 0:
            return None
        return round(100.0 * (local or 0) / remote, 1)
    return {"channels": [
        {"username": r["username"], "title": r["title"],
         "done": r["deep_backfilled"],
         "mode": "JOIN" if r["is_joined"] else ("SUB" if r["subscribed"] else "—"),
         "oldest_id": r["deep_oldest_tg_id"],
         "last_run_at": r["deep_last_run_at"].isoformat() if r["deep_last_run_at"] else None,
         "pulled_deep": r["deep_total_pulled"],
         "msgs_local": r["msgs_local"],
         "remote_msgs": r["remote_msgs"],
         "pct_msgs": pct(r["msgs_local"], r["remote_msgs"]),
         "local_media": (r["local_photos"] or 0) + (r["local_videos"] or 0)
                        + (r["local_audio"] or 0) + (r["local_documents"] or 0),
         "remote_media": (r["remote_photos"] or 0) + (r["remote_videos"] or 0)
                         + (r["remote_audio"] or 0) + (r["remote_documents"] or 0),
         "pct_media": pct((r["local_photos"] or 0) + (r["local_videos"] or 0)
                          + (r["local_audio"] or 0) + (r["local_documents"] or 0),
                          (r["remote_photos"] or 0) + (r["remote_videos"] or 0)
                          + (r["remote_audio"] or 0) + (r["remote_documents"] or 0)),
         "local_photos": r["local_photos"], "remote_photos": r["remote_photos"],
         "pct_photos": pct(r["local_photos"], r["remote_photos"]),
         "local_videos": r["local_videos"], "remote_videos": r["remote_videos"],
         "pct_videos": pct(r["local_videos"], r["remote_videos"]),
         "local_audio": r["local_audio"], "remote_audio": r["remote_audio"],
         "pct_audio": pct(r["local_audio"], r["remote_audio"]),
         "local_documents": r["local_documents"], "remote_documents": r["remote_documents"],
         "pct_documents": pct(r["local_documents"], r["remote_documents"])}
        for r in rows],
        "released_count": released_count or 0,
        "purged_count": purged_count or 0}


@app.get("/system", response_class=HTMLResponse)
async def page_system():
    if not login.session_exists():
        return RedirectResponse("/login")
    # What the account's daily Telegram allowances went on today.
    import json as _json
    async with db_pool.acquire() as conn:
        _cfg = {r["key"]: r["value"] for r in await conn.fetch(
            "SELECT key, value FROM config WHERE key LIKE 'mtproto_daily%' OR key = 'mtproto_caps'")}
    try:
        _caps = _json.loads(_cfg.get("mtproto_caps") or "{}")
    except ValueError:
        _caps = {}
    _today = time.strftime("%Y-%m-%d", time.gmtime())
    _names = {"resolveUsername": ("Looking a channel up by @username",
                                  "needed the first time a channel is read; Telegram tolerates a few hundred a day"),
              "join_chat": ("Joining a channel", "only when a file cannot be read without joining"),
              "exportAuthorization": ("Opening another data center",
                                      "once per data center a file lives on, then reused")}
    _who = {"federation_validator": "checking channels for tgarr.me", "_thumb_get_messages": "thumbnails and previews",
            "_channel_gone_ask": "checking whether a channel still exists",
            "_mtproto_get_messages_with_auto_join": "opening a film, a song or a book",
            "subscription_poller": "reading followed channels", "seed_validator": "checking channels for tgarr.me"}
    _rows = []
    for _m, (_label, _hint) in _names.items():
        _cap = (_caps.get("caps") or {}).get(_m)
        _v = _cfg.get(f"mtproto_daily:{_m}") or ""
        _used = int(_v.partition(":")[2] or 0) if _v.startswith(_today) else 0
        _by = {}
        _vb = _cfg.get(f"mtproto_daily_by:{_m}") or ""
        if _vb.startswith(_today):
            try:
                _by = _json.loads(_vb.partition("|")[2])
            except ValueError:
                _by = {}
        _parts = []
        for _k, _n in sorted(_by.items(), key=lambda x: -x[1]):
            _bg = _k.startswith("bg:")
            _name = _who.get(_k[3:] if _bg else _k, (_k[3:] if _bg else _k))
            _parts.append(f'{html.escape(_name)} <b>{_n}</b>' + (' <span class="pill muted">background</span>' if _bg else ''))
        _pct = min(100, _used * 100 // _cap) if _cap else 0
        _col = "#ef4444" if _pct >= 100 else "#f59e0b" if _pct >= 75 else "#10b981"
        _rows.append(
            f'<tr><td><b>{_label}</b><div style="font-size:12px;color:var(--muted)">{_hint}</div></td>'
            f'<td style="white-space:nowrap"><b>{_used}</b> of {_cap if _cap else "–"}'
            f'<div style="height:6px;width:120px;background:var(--border);border-radius:3px;margin-top:5px">'
            f'<div style="height:6px;width:{_pct}%;background:{_col};border-radius:3px"></div></div></td>'
            f'<td style="font-size:13px">{" · ".join(_parts) or "<span style=color:var(--muted)>nothing yet today</span>"}</td></tr>')
    _share = int(float(_caps.get("bg_share", 0.4)) * 100)
    allowances_html = (
        '<section style="margin-bottom:28px">'
        '<h3 style="font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:0 0 8px">'
        'Telegram allowances today (resets at 00:00 UTC)</h3>'
        '<table><thead><tr><th>what</th><th>used</th><th>who used it</th></tr></thead>'
        f'<tbody>{"".join(_rows)}</tbody></table>'
        f'<div style="font-size:12.5px;color:var(--muted);margin-top:8px">tgarr keeps well inside what Telegram '
        f'tolerates so that your account is never slowed down. Background work may use at most {_share}% of an '
        'allowance; the rest is kept for what you open yourself.</div></section>')
    body = """
<h2 class="section">System</h2>
__ALLOWANCES__
<section style="margin-bottom:28px">
  <h3 style="color:#64748b;font-size:13px;text-transform:uppercase;letter-spacing:0.05em">Workers</h3>
  <div id="workersTable">loading...</div>
</section>
<section>
  <h3 style="color:#64748b;font-size:13px;text-transform:uppercase;letter-spacing:0.05em">Deep backfill progress</h3>
  <div id="deepBackfillTable">loading...</div>
</section>
<script>
function fmtAgo(iso) {
  if (!iso) return "never";
  const sec = (Date.now() - new Date(iso).getTime()) / 1000;
  if (sec < 60) return Math.floor(sec) + "s ago";
  if (sec < 3600) return Math.floor(sec/60) + "m ago";
  if (sec < 86400) return Math.floor(sec/3600) + "h ago";
  return Math.floor(sec/86400) + "d ago";
}
function healthDot(h) {
  const c = {healthy:"#22c55e",idle:"#eab308",stale:"#ef4444",never:"#94a3b8",off:"#cbd5e1"}[h] || "#94a3b8";
  return `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${c};margin-right:6px"></span>`;
}
async function loadWorkers() {
  const r = await fetch("/api/workers");
  const j = await r.json();
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  // The error counter is lifetime-cumulative. Only an error from the last 24h
  // is shown as a problem; older ones are greyed out with their age.
  const rows = (j.workers || []).map(w => {
    const recent = w.last_error_at &&
      (Date.now() - new Date(w.last_error_at).getTime()) < 86400000;
    const errColor = recent ? "#ef4444" : "#94a3b8";
    const errText = !w.last_error ? "" :
      (recent ? esc(w.last_error) : `${esc(w.last_error)} · ${fmtAgo(w.last_error_at)}`);
    return `
    <tr><td>${healthDot(w.health)}<b>${esc(w.worker)}</b></td>
        <td>${fmtAgo(w.last_seen)}</td>
        <td style="color:#64748b;font-size:13px">${esc(w.last_action) || "—"}</td>
        <td style="text-align:right;color:#64748b">${w.iter_count.toLocaleString()}</td>
        <td style="text-align:right;color:${w.error_count ? errColor : "#64748b"}"
            title="lifetime total">${w.error_count}</td>
        <td style="color:${errColor};font-size:12px">${errText}</td></tr>`; });
  document.getElementById("workersTable").innerHTML = `<table style="width:100%;border-collapse:collapse">
    <thead><tr style="text-align:left;color:#64748b;font-size:12px">
    <th>Worker</th><th>Last seen</th><th>Last action</th>
    <th style="text-align:right">Iters</th><th style="text-align:right">Errors</th>
    <th>Last error</th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
}
async function loadDeepBackfill() {
  const r = await fetch("/api/deep-backfill-progress");
  const j = await r.json();
  const fmtPct = (v) => v == null ? "—" : (v >= 100 ? "100%" : v + "%");
  const barColor = (v) => v == null ? "#cbd5e1" : (v >= 95 ? "#16a34a" : v >= 50 ? "#3b82f6" : v >= 10 ? "#f59e0b" : "#ef4444");
  const bar = (pct) => {
    if (pct == null) return '<span style="color:#94a3b8">—</span>';
    const w = Math.max(2, Math.min(100, pct));
    return `<div style="background:#e2e8f0;border-radius:3px;width:80px;height:8px;display:inline-block;overflow:hidden;vertical-align:middle"><div style="width:${w}%;height:100%;background:${barColor(pct)}"></div></div> <span style="font-size:11px;color:#475569">${fmtPct(pct)}</span>`;
  };
  const modeBadge = (m) => {
    const s = m==="JOIN" ? "background:#fef3c7;color:#92400e"
            : m==="SUB"  ? "background:#dcfce7;color:#166534"
            : "background:#f1f5f9;color:#94a3b8";
    const t = m==="JOIN" ? "加入+爬 (账号成员, 占 join 槽)"
            : m==="SUB"  ? "订阅+爬 (公开轮询, 不 join)" : "未订阅/未加入";
    return `<span title="${t}" style="${s};padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600">${m}</span>`;
  };
  const rows = (j.channels || []).map(c => {
    const totalCell = c.remote_media
      ? `${(c.local_media||0).toLocaleString()} / ${c.remote_media.toLocaleString()}`
      : (c.local_media||0).toLocaleString();
    return `
    <tr><td>${c.done?"✅":"🔄"} <b>@${c.username || "—"}</b></td>
        <td>${modeBadge(c.mode)}</td>
        <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(c.title || "").substring(0, 40)}</td>
        <td style="text-align:right;font-size:13px">${totalCell}</td>
        <td>${bar(c.pct_media)}</td>
        <td style="text-align:right;font-size:11px;color:#475569">P ${fmtPct(c.pct_photos)}</td>
        <td style="text-align:right;font-size:11px;color:#475569">V ${fmtPct(c.pct_videos)}</td>
        <td style="text-align:right;font-size:11px;color:#475569">A ${fmtPct(c.pct_audio)}</td>
        <td style="text-align:right;font-size:11px;color:#475569">D ${fmtPct(c.pct_documents)}</td>
        <td style="text-align:right;color:#64748b;font-size:12px">${fmtAgo(c.last_run_at)}</td></tr>`;
  });
  document.getElementById("deepBackfillTable").innerHTML = `<table style="width:100%;border-collapse:collapse">
    <thead><tr style="text-align:left;color:#64748b;font-size:12px">
    <th>Status</th><th>Mode</th><th>Title</th>
    <th style="text-align:right">Media (local/remote)</th>
    <th>Progress</th>
    <th style="text-align:right">Photos</th>
    <th style="text-align:right">Videos</th>
    <th style="text-align:right">Audio</th>
    <th style="text-align:right">Docs</th>
    <th style="text-align:right">Last run</th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
}
loadWorkers(); loadDeepBackfill();
setInterval(loadWorkers, 30000);
setInterval(loadDeepBackfill, 60000);
</script>
<style>
#workersTable table th, #workersTable table td,
#deepBackfillTable table th, #deepBackfillTable table td {
  padding: 8px 10px; border-bottom: 1px solid #e2e8f0;
}
</style>
"""
    body = body.replace("__ALLOWANCES__", allowances_html)
    return HTMLResponse(_layout("System", "/system", body))


@app.get("/health")
async def health():
    """Liveness/readiness: one trivial DB round-trip, no table scans."""
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as e:
        return JSONResponse({"status": "db_down", "error": type(e).__name__},
                            status_code=503)
    return {"status": "ok", "tgarr": TGARR_VERSION,
            "authed": login.session_exists(), "tg_wait": await _tg_flood()}


_DASH_CACHE: dict = {"at": 0.0, "v": None}


@app.get("/")
async def root(request: Request, accept: Optional[str] = Header(None)):
    async with db_pool.acquire() as conn:
        stats = await conn.fetchrow(
            """SELECT (SELECT count(*) FROM channels)                AS channels,
                      (SELECT count(*) FROM messages)                AS messages,
                      (SELECT count(*) FROM releases)                AS releases,
                      (SELECT count(*) FROM messages
                         WHERE media_type='video')                   AS videos,
                      (SELECT count(*) FROM messages
                         WHERE media_type='photo')                   AS photos,
                      (SELECT count(*) FROM messages
                         WHERE media_type='audio')                   AS audio,
                      (SELECT count(*) FROM messages
                         WHERE media_type='document'
                           AND file_name ~* '\.(pdf|epub|mobi|azw3?|djvu|fb2|cbr|cbz|lit|txt)$') AS books,
                      (SELECT count(*) FROM downloads
                         WHERE status='pending')                     AS pending,
                      (SELECT count(*) FROM downloads
                         WHERE status='downloading')                 AS downloading,
                      (SELECT count(*) FROM downloads
                         WHERE status='completed')                   AS completed""")

    # authed = can perform write actions (grab/refresh/crawl). browsable =
    # something cached to render. We split them so a logged-out user still
    # sees the public-channel cache rather than being bounced to /login —
    # private rows have already been wiped via _mark_revoked or /logout.
    revoked = _is_revoked()
    authed = login.session_exists() and not revoked
    browsable = stats["channels"] > 0

    wants_html = accept and "text/html" in accept
    if not wants_html:
        return {"tgarr": TGARR_VERSION, "authed": authed,
                "revoked": revoked, "browsable": browsable, **dict(stats)}

    # Only bounce to /login when there is nothing to show AND nobody is signed
    # in. Signed in with an empty index is the first minute of every new
    # install (and the permanent state of an account that follows no
    # channels): /login sends a signed-in visitor back here, so bouncing them
    # was an endless redirect - the first thing a new user saw.
    if not browsable and not authed:
        return RedirectResponse("/login")

    async with db_pool.acquire() as conn:
        recent = await conn.fetch(
            """SELECT d.id, d.status, d.local_path, d.requested_at, d.finished_at,
                      d.error_message, r.name, r.size_bytes
               FROM downloads d
               JOIN releases r ON r.id = d.release_id
               ORDER BY d.requested_at DESC LIMIT 10""")

    st = dict(stats)
    import datetime as _dt
    # The numbers below cost a few scans of the index. They change slowly:
    # work them out at most once a minute, not on every visit.
    if time.time() - _DASH_CACHE["at"] > 60:
        async with db_pool.acquire() as conn:
            lib = await conn.fetchrow(
                """SELECT (SELECT count(*) FROM releases WHERE category = 'movie') AS films,
                          (SELECT count(*) FROM releases WHERE category = 'tv')    AS episodes,
                          (SELECT count(*) FROM releases
                            WHERE category IN ('movie', 'tv')
                              AND created_at > NOW() - INTERVAL '24 hours')        AS rel_new""")
            new_today = {r["media_type"]: r["n"] for r in await conn.fetch(
                """SELECT media_type, count(*) AS n FROM messages
                   WHERE indexed_at > NOW() - INTERVAL '24 hours' GROUP BY 1""")}
            per_day = await conn.fetch(
                """SELECT date_trunc('day', indexed_at)::date AS d, count(*) AS n FROM messages
                   WHERE indexed_at > NOW() - INTERVAL '14 days' GROUP BY 1 ORDER BY 1""")
            latest = await conn.fetch(
                """SELECT r.primary_msg_id, r.poster_url, r.category, r.season, r.episode,
                          r.quality, r.movie_year,
                          COALESCE(NULLIF(r.canonical_title, ''), r.movie_title, r.series_title, r.name) AS t
                   FROM releases r
                   JOIN messages m ON m.id = r.primary_msg_id
                   JOIN channels c ON c.id = m.channel_id
                   WHERE r.poster_url IS NOT NULL AND r.category IN ('movie', 'tv')
                     AND COALESCE(c.audience, 'sfw') = 'sfw'""" + _pub() + """
                   ORDER BY r.created_at DESC, r.id DESC LIMIT 60""")
            _seen, _uniq = set(), []
            for _r in latest:                       # one poster per title
                _k = ((_r["t"] or "").lower(), _r["movie_year"], _r["season"], _r["episode"])
                if _k in _seen:
                    continue
                _seen.add(_k)
                _uniq.append(_r)
                if len(_uniq) == 12:
                    break
            latest = _uniq
            try:
                hb = await conn.fetchval("SELECT max(last_seen) FROM worker_status")
            except Exception:
                hb = None
            try:
                shared = await conn.fetchrow(
                    """SELECT max(contributed_at) AS last,
                              count(*) FILTER (WHERE contributed_at > NOW() - INTERVAL '24 hours') AS d1
                       FROM messages WHERE contributed_at IS NOT NULL""")
            except Exception:
                shared = None
        _DASH_CACHE.update(at=time.time(), v=(lib, new_today, per_day, latest, hb, shared))
    lib, new_today, per_day, latest, hb, shared = _DASH_CACHE["v"]
    fetches = await _player_fetches()
    usage = await _cache_usage()
    flood = await _tg_flood()
    user = login.ensure_user_info() or {}
    free_gb = _disk_free_gb("/downloads")

    # ── status ──
    def _status(dot, title, text, href=None):
        col = {"ok": "#10b981", "warn": "#f59e0b", "bad": "#ef4444", "off": "#94a3b8"}[dot]
        inner = (f'<div class="st-dot" style="background:{col}"></div><div class="st-txt">'
                 f'<div class="st-t">{title}</div><div class="st-s">{text}</div></div>')
        return (f'<a class="st-card" href="{href}">{inner}</a>' if href
                else f'<div class="st-card">{inner}</div>')
    if not authed:
        tg = _status("bad", "Telegram", "not signed in — sign in to index and play", "/login")
    elif flood:
        tg = _status("warn", "Telegram",
                     f'asked us to slow down — resumes in about {max(1, flood["seconds_left"] // 60)} min')
    else:
        who = user.get("username") or user.get("first_name") or ""
        tg = _status("ok", "Telegram", f'signed in{(" as @" + html.escape(str(who))) if who else ""}', "/settings")
    if hb:
        age = (_dt.datetime.now(_dt.timezone.utc) - hb).total_seconds()
        cr = (_status("ok", "Indexer", f"running · last activity {_ago(hb)}", "/system") if age < 900
              else _status("bad", "Indexer", f"no activity since {_ago(hb)}", "/system"))
    else:
        cr = _status("off", "Indexer", "has not reported yet", "/system")
    if shared and shared["last"]:
        sh = _status("ok" if shared["d1"] else "off", "Shared with tgarr.me",
                     f'{_fmt_count(shared["d1"])} files today · last {_ago(shared["last"])}')
    else:
        sh = _status("off", "Shared with tgarr.me", "nothing shared yet")
    used = sum(r["bytes"] for r in usage)
    try:
        import shutil as _sh
        _du = _sh.disk_usage("/downloads")
        _sto_lvl = _disk_level(_du.free, _du.total)
    except OSError:
        _sto_lvl = "off"
    sto = _status(_sto_lvl, "Storage",
                  f'{_fmt_size(used) or "0 MB"} saved · {free_gb:.0f} GB free', "/downloads")
    status_html = f'<div class="st-row">{tg}{cr}{sh}{sto}</div>'
    status_html = await _update_banner() + status_html
    if not browsable:
        status_html = (
            '<div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:12px;'
            'padding:16px 18px;margin-bottom:16px;line-height:1.6">'
            '<b>You are signed in. Nothing is indexed yet.</b><br>'
            'tgarr is reading the channels your Telegram account follows; they appear here '
            'within a few minutes, and their files over the next hours. If the account follows '
            'no channels with files, pick some on <a href="/discover">Discover</a>.'
            '</div>' + status_html)

    # ── library ──
    def _tile(href, icon, n, label, plus=0):
        return (f'<a class="tile" href="{href}"><div class="ti">{icon}</div>'
                f'<div class="tn">{n:,}</div><div class="tl">{label}</div>'
                + (f'<div class="tp">+{_fmt_count(plus)} today</div>' if plus else '<div class="tp">&nbsp;</div>')
                + '</a>')
    tiles = (
        _tile("/releases?cat=movie", "🎬", lib["films"], "films", 0)
        + _tile("/releases?cat=tv", "📺", lib["episodes"], "episodes", 0)
        + _tile("/videos", "▶", st["videos"], "videos", new_today.get("video", 0))
        + _tile("/music", "🎵", st["audio"], "songs", new_today.get("audio", 0))
        + _tile("/library", "📚", st["books"], "books", 0)
        + _tile("/gallery", "🖼", st["photos"], "photos", new_today.get("photo", 0))
        + _tile("/channels", "📡", st["channels"], "channels", 0))

    # ── now ──
    now_bits = []
    arriving = [f for f in fetches if f["state"] in ("queued", "fetching")]
    if arriving:
        now_bits.append(f'<a class="now-pill" href="/downloads">⬇ {len(arriving)} arriving for a player</a>')
    if st["pending"] or st["downloading"]:
        now_bits.append(f'<a class="now-pill" href="/downloads?tab=grabs">⬇ {st["pending"] + st["downloading"]} '
                        f'grab(s) in the queue</a>')
    if lib["rel_new"]:
        now_bits.append(f'<a class="now-pill" href="/releases">✨ {lib["rel_new"]:,} films and episodes '
                        f'recognised in the last day</a>')
    now_html = f'<div class="now-row">{"".join(now_bits)}</div>' if now_bits else ""

    # ── recently added ──
    if latest:
        cards = []
        for r in latest:
            sub = (f'S{r["season"]:02d}' + (f'E{r["episode"]:02d}' if r["episode"] is not None else "")
                   if r["category"] == "tv" and r["season"] is not None
                   else (str(r["movie_year"]) if r["movie_year"] else ""))
            cards.append(
                f'<a class="pc" href="/watch/{r["primary_msg_id"]}" title="{html.escape(r["t"] or "", quote=True)}">'
                f'<div class="pi"><img src="{html.escape(r["poster_url"], quote=True)}" loading="lazy" alt="" '
                f'onerror="this.remove()"/>'
                + (f'<span class="pq">{html.escape(r["quality"])}</span>' if r["quality"] else '')
                + f'</div><div class="pt">{html.escape(r["t"] or "")}</div>'
                f'<div class="ps">{html.escape(sub)}</div></a>')
        latest_html = (
            '<h2 class="section">Recently added <span class="extra"><a href="/releases">all releases →</a></span></h2>'
            f'<div class="strip">{"".join(cards)}</div>')
    else:
        latest_html = ""

    # ── added per day (drawn here: no chart library, nothing loaded from elsewhere) ──
    days = {r["d"]: r["n"] for r in per_day}
    today = _dt.datetime.now(_dt.timezone.utc).date()
    series = [(today - _dt.timedelta(days=i), days.get(today - _dt.timedelta(days=i), 0))
              for i in range(13, -1, -1)]
    peak = max((n for _, n in series), default=0) or 1
    W, H, PAD = 560, 150, 22
    bw = (W - 8) / len(series)
    bars = []
    for i, (d, n) in enumerate(series):
        h = (H - PAD - 8) * n / peak
        x = 4 + i * bw
        bars.append(
            f'<g><rect x="{x + bw * 0.16:.1f}" y="{H - PAD - h:.1f}" width="{bw * 0.68:.1f}" '
            f'height="{max(h, 1.5):.1f}" rx="3" fill="{"var(--accent)" if d == today else "rgba(94,182,229,.55)"}">'
            f'<title>{d.strftime("%b %d")}: {n:,} added</title></rect>'
            + (f'<text x="{x + bw / 2:.1f}" y="{H - 6}" text-anchor="middle" font-size="10" '
               f'fill="var(--muted)">{d.strftime("%m-%d")}</text>' if i % 2 == 1 else '')
            + '</g>')
    total14 = sum(n for _, n in series)
    growth_html = (
        '<div class="panel"><div class="ph"><span>Added per day</span>'
        f'<span class="pn">{total14:,} in two weeks · peak {peak:,}</span></div>'
        f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;display:block">'
        f'<line x1="0" y1="{H - PAD}" x2="{W}" y2="{H - PAD}" stroke="var(--border)"/>'
        f'{"".join(bars)}</svg></div>')
    mix = [("photos", st["photos"], "#5eb6e5"), ("videos", st["videos"], "#94d2bd"),
           ("songs", st["audio"], "#f4a261"), ("books", st["books"], "#e9c46a")]
    other = max(0, st["messages"] - sum(v for _, v, _ in mix))
    if other:
        mix.append(("other files", other, "#cbd5e1"))
    tot = sum(v for _, v, _ in mix) or 1
    mix_html = (
        '<div class="panel"><div class="ph"><span>What is indexed</span>'
        f'<span class="pn">{st["messages"]:,} items</span></div>'
        '<div class="mixbar">'
        + "".join(f'<div style="width:{v * 100 / tot:.2f}%;background:{c}" title="{k}: {v:,}"></div>'
                  for k, v, c in mix if v)
        + '</div><div class="mixleg">'
        + "".join(f'<div><i style="background:{c}"></i>{k}<b>{v:,}</b>'
                  f'<span>{v * 100 / tot:.0f}%</span></div>' for k, v, c in mix if v)
        + '</div></div>')

    # ── grabs + wiring ──
    if recent:
        items = []
        for d in recent[:6]:
            icon = ({"pending": "⏳", "downloading": "⬇", "completed": "✓",
                     "failed": "✗", "cancelled": "—"}).get(d["status"], "?")
            cls = ({"pending": "warn", "downloading": "acc", "completed": "ok",
                    "failed": "bad"}).get(d["status"], "")
            items.append(
                f'<div class="ls-row"><div style="width:26px;text-align:center">{icon}</div>'
                f'<div class="ls-main"><div class="ls-title">{html.escape(d["name"])}</div>'
                f'<div class="ls-sub"><span class="tag {cls}">{d["status"]}</span>'
                f'<span>{_fmt_size(d["size_bytes"])}</span><span>{_ago(d["requested_at"])}</span></div></div></div>')
        grabs_html = (
            '<h2 class="section">Recent grabs <span class="extra"><a href="/downloads?tab=grabs">all →</a></span></h2>'
            f'<div class="ls">{"".join(items)}</div>')
    else:
        grabs_html = ""

    base_url = html.escape(_base_url(request)) or "http://&lt;host&gt;:8765"

    def _wire(icon, name, where, value):
        return (f'<div class="ls-row"><div style="width:26px;text-align:center">{icon}</div>'
                f'<div class="ls-main"><div class="ls-title">{name}</div><div class="ls-sub">{where}</div></div>'
                f'<code class="wv">{value}</code>'
                f'<button class="abtn" style="min-width:74px" onclick="tgCopy(this)" '
                f'data-v="{value}">Copy</button></div>')
    wiring_html = (
        '<h2 class="section">Connect Sonarr / Radarr</h2><div class="ls">'
        + _wire("🔌", "Newznab indexer", "Settings → Indexers → + → Newznab → URL", f"{base_url}/newznab/")
        + _wire("⬇", "SABnzbd download client",
                "Settings → Download Clients → + → SABnzbd → same host and port, URL Base", "/sabnzbd")
        + '</div><script>function tgCopy(b){const v=b.dataset.v;const done=()=>{const o=b.textContent;'
          'b.textContent="Copied";setTimeout(()=>b.textContent=o,1400)};'
          'if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(v).then(done);'
          'else{const t=document.createElement("textarea");t.value=v;document.body.appendChild(t);t.select();'
          'try{document.execCommand("copy");done()}catch(e){}t.remove()}}</script>')

    dash_css = """<style>
.st-row{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:18px}
.st-card{display:flex;gap:12px;align-items:center;background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:12px 14px;text-decoration:none;color:inherit;min-width:0}
a.st-card:hover{border-color:var(--accent)}
.st-dot{width:10px;height:10px;border-radius:50%;flex:none;box-shadow:0 0 0 4px rgba(127,127,127,.10)}
.st-txt{min-width:0}.st-t{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600}
.st-s{font-size:13.5px;color:var(--fg);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tiles{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:12px;margin-bottom:14px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 14px 10px;
  text-decoration:none;color:inherit;transition:border-color .12s,transform .12s;min-width:0}
.tile:hover{border-color:var(--accent);transform:translateY(-2px)}
.tile .ti{font-size:20px;line-height:1}.tile .tn{font-size:24px;font-weight:700;color:var(--accent-hi);margin-top:8px;
  font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .tl{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600;margin-top:2px}
.tile .tp{font-size:11.5px;color:#059669;margin-top:6px;height:15px}
.now-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.now-pill{font-size:13px;padding:6px 12px;border-radius:16px;background:rgba(94,182,229,.13);color:var(--accent-hi);
  text-decoration:none;border:1px solid rgba(94,182,229,.35)}
.now-pill:hover{background:rgba(94,182,229,.22)}
.strip{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:10px}
.pc{text-decoration:none;color:inherit;min-width:0}
.pc .pi{position:relative;aspect-ratio:2/3;border-radius:8px;overflow:hidden;background:var(--surface2,#e2e8f0);
  border:1px solid var(--border)}
.pc .pi img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .2s}
.pc:hover .pi img{transform:scale(1.05)}
.pc .pq{position:absolute;right:4px;top:4px;font-size:9.5px;font-weight:700;background:rgba(15,23,42,.78);color:#fff;
  padding:2px 5px;border-radius:5px}
.pc .pt{font-size:12.5px;font-weight:600;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pc .ps{font-size:11px;color:var(--muted)}
.panels{display:grid;grid-template-columns:3fr 2fr;gap:14px;margin-top:22px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.panel .ph{display:flex;justify-content:space-between;align-items:baseline;font-size:11px;text-transform:uppercase;
  letter-spacing:.07em;color:var(--muted);font-weight:600;margin-bottom:10px}
.panel .pn{text-transform:none;letter-spacing:0;font-weight:400;font-size:12px}
.mixbar{display:flex;height:16px;border-radius:8px;overflow:hidden;background:var(--border);margin:14px 0 16px}
.mixleg div{display:flex;align-items:center;gap:8px;font-size:13.5px;padding:4px 0}
.mixleg i{width:10px;height:10px;border-radius:3px;flex:none}.mixleg b{margin-left:auto;font-variant-numeric:tabular-nums}
.mixleg span{width:38px;text-align:right;color:var(--muted);font-size:12px}
.wv{font-size:12.5px;background:rgba(127,127,127,.10);padding:5px 9px;border-radius:6px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;max-width:46%}
@media (max-width:1100px){.tiles{grid-template-columns:repeat(4,minmax(0,1fr))}.strip{grid-template-columns:repeat(6,minmax(0,1fr))}
  .st-row{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:700px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.strip{grid-template-columns:repeat(3,minmax(0,1fr))}
  .st-row,.panels{grid-template-columns:1fr}.wv{display:none}}
</style>"""

    body = (f'{_LIST_CSS}{dash_css}{status_html}'
            f'<div class="tiles">{tiles}</div>{now_html}'
            f'{latest_html}'
            f'<div class="panels">{growth_html}{mix_html}</div>'
            f'{grabs_html}{wiring_html}')
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
                """UPDATE channels SET subscribed=TRUE, unfollowed_at=NULL, purged_at=NULL,
                   last_polled_at=NULL, subscribe_error=NULL
                   WHERE id=$1""", existing)
            return {"status": "ok", "message": f"already known — re-queued @{u}",
                    "channel_id": existing}
        # Synthesize a placeholder tg_chat_id below the legal range to avoid
        # collision; poller fixes it on first scan.
        placeholder = -abs(hash(u)) // 1000 - 1_000_000_000_000
        new_id = await conn.fetchval(
            """INSERT INTO channels (tg_chat_id, username, title,
                                     subscribed, backfilled, handle_known)
               VALUES ($1, $2, $3, TRUE, FALSE, FALSE)
               ON CONFLICT (tg_chat_id) DO NOTHING
               RETURNING id""",
            placeholder, u, f"@{u} (resolving…)")
    return {"status": "queued", "message": f"@{u} queued for first scan",
            "channel_id": new_id}


@app.post("/api/channel/unsubscribe/{tg_chat_id}")
async def api_channel_unsubscribe(tg_chat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE channels SET subscribed=FALSE, unfollowed_at=NOW()
               WHERE tg_chat_id=$1 AND subscribed=TRUE""", tg_chat_id)
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
async def page_channels(q: Optional[str] = None, show: str = "all", sort: str = "films",
                        order: str = "", offset: int = 0, limit: int = 50,
                        min_members: Optional[int] = None, eligible: int = 0,
                        audience: str = ""):
    if not login.session_exists():
        return RedirectResponse("/login")
    if eligible:                       # old links
        show = "shared"
    nsfw_on = await _is_nsfw_enabled()
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
        # One pass over messages for every channel's numbers (the page used to
        # run three sub-queries per channel).
        rows = await conn.fetch(
            """WITH agg AS (
                 SELECT channel_id, count(*) AS msgs, count(file_name) AS media,
                        count(*) FILTER (WHERE media_type = 'video') AS videos,
                        count(*) FILTER (WHERE media_type = 'audio') AS songs,
                        count(*) FILTER (WHERE media_type = 'photo') AS photos,
                        count(*) FILTER (WHERE media_type = 'document') AS docs,
                        max(posted_at) AS last_post
                 FROM messages GROUP BY channel_id),
               rel AS (
                 SELECT m.channel_id, count(*) AS films FROM releases r
                 JOIN messages m ON m.id = r.primary_msg_id
                 WHERE r.category IN ('movie', 'tv') GROUP BY 1)
               SELECT c.tg_chat_id, c.username, c.title, c.backfilled, c.members_count,
                      c.audience, c.subscribed, c.is_joined, c.dead_at, c.subscribe_error,
                      c.purged_at, c.unfollowed_at, c.handle_known, c.web_preview,
                      COALESCE(a.msgs, 0) AS msgs, COALESCE(a.media, 0) AS media,
                      COALESCE(a.videos, 0) AS videos, COALESCE(a.songs, 0) AS songs,
                      COALESCE(a.photos, 0) AS photos, COALESCE(a.docs, 0) AS docs,
                      a.last_post, COALESCE(rel.films, 0) AS films
               FROM channels c
               LEFT JOIN agg a ON a.channel_id = c.id
               LEFT JOIN rel ON rel.channel_id = c.id
               WHERE COALESCE(c.audience, 'sfw') <> 'blocked_csam'""")
    rows = [r for r in rows if nsfw_on or (r["audience"] or "sfw") != "nsfw"]
    # Does this install clean a channel out of its index once nobody follows
    # it? (TGARR_INDEX_PURGE on the crawler; off unless switched on.) What
    # "Unfollow" means depends on it, and the page has to say which.
    async with db_pool.acquire() as conn:
        purge_on = (await conn.fetchval(
            "SELECT value FROM config WHERE key = 'index_purge'")) == "on"

    def _shared(r):
        return (r["members_count"] or 0) >= CONTRIB_MIN_MEMBERS and r["media"] >= CONTRIB_MIN_MEDIA
    # A channel the user unfollowed has left the list as far as they are
    # concerned, even while its index is still being cleared: it shows only
    # under "being cleared" (where it can be followed again in time).
    def _uf(r):
        return bool(r["unfollowed_at"]) and not r["subscribed"] and not r["is_joined"]
    kinds = {
        "all": lambda r: not _uf(r),
        "films": lambda r: r["films"] > 0 and not _uf(r),
        "joined": lambda r: r["is_joined"],
        "subscribed": lambda r: r["subscribed"] and not r["is_joined"],
        "shared": lambda r: _shared(r) and not _uf(r),
        "loose": lambda r: not r["is_joined"] and not r["subscribed"] and not r["dead_at"] and not _uf(r),
        "empty": lambda r: r["media"] == 0 and not _uf(r),
        "dead": lambda r: r["dead_at"] is not None and not _uf(r),
        "clearing": _uf,
    }
    if show not in kinds:
        show = "all"
    counts = {k: sum(1 for r in rows if f(r)) for k, f in kinds.items()}
    view = [r for r in rows if kinds[show](r)]
    if q:
        words = [w.lower() for w in q.split()]
        view = [r for r in view
                if all(w in ((r["title"] or "") + " " + (r["username"] or "")).lower() for w in words)]
    def _name(r):
        return (r["title"] or r["username"] or "~").lower()
    sorts = {
        "films": lambda r: (r["films"], r["media"]),
        "media": lambda r: (r["media"], r["films"]),
        "members": lambda r: r["members_count"],
        "recent": lambda r: r["last_post"],
        "name": _name,
    }
    if sort not in sorts:
        sort = "films"
    first_dir = {"name": "asc"}
    if order not in ("asc", "desc"):
        order = first_dir.get(sort, "desc")
    view = _ls_sort(view, sorts[sort], order, _name)
    page_size = max(10, min(limit, 200))
    page_offset = max(0, offset)
    page = view[page_offset:page_offset + page_size]

    def _cqs(**over):
        cur = {"q": q, "show": show if show != "all" else None,
               "sort": sort if sort != "films" else None,
               "order": order if order != first_dir.get(sort, "desc") else None,
               "limit": limit if limit != 50 else None}
        cur.update(over)
        bits = [f"{k}={_q(str(v))}" for k, v in cur.items() if v not in (None, "", 0)]
        return "?" + "&".join(bits) if bits else ""

    def _n(k):
        return f' <span style="opacity:.6">{counts[k]:,}</span>'
    chips = (
        _chip_link("all" + _n("all"), "/channels" + _cqs(show=None, offset=None), show == "all")
        + _chip_link("🎬 with films" + _n("films"), "/channels" + _cqs(show="films", offset=None),
                     show == "films", "channels that carry recognised films or episodes")
        + _chip_link("joined" + _n("joined"), "/channels" + _cqs(show="joined", offset=None),
                     show == "joined", "channels your Telegram account is a member of")
        + _chip_link("followed" + _n("subscribed"), "/channels" + _cqs(show="subscribed", offset=None),
                     show == "subscribed", "public channels tgarr reads without joining")
        + _chip_link("shared" + _n("shared"), "/channels" + _cqs(show="shared", offset=None),
                     show == "shared",
                     f"{CONTRIB_MIN_MEMBERS}+ members and {CONTRIB_MIN_MEDIA}+ files: listed on tgarr.me "
                     "for other people to find. Private chats and small groups never are.")
        + (_chip_link("not followed" + _n("loose"), "/channels" + _cqs(show="loose", offset=None),
                      show == "loose",
                      "channels this install knows but no longer reads"
                      + (": their files are being cleared from this index" if purge_on else ""))
           if counts["loose"] else "")
        + _chip_link("no files" + _n("empty"), "/channels" + _cqs(show="empty", offset=None), show == "empty")
        + (_chip_link("gone" + _n("dead"), "/channels" + _cqs(show="dead", offset=None),
                      show == "dead", "channels Telegram no longer serves") if counts["dead"] else "")
        + (_chip_link("being cleared" + _n("clearing"), "/channels" + _cqs(show="clearing", offset=None),
                      show == "clearing",
                      "channels you unfollowed: their index is being cleared and they then disappear. "
                      "Follow one again here to keep it.") if counts["clearing"] else "")
        # on a phone there is no room for the column header: the same sorts as chips
        + '<span class="sort-chips"><span class="sep"></span><span class="lbl">Sort:</span>'
        + _chip_link("films", "/channels" + _cqs(sort=None, order=None, offset=None), sort == "films")
        + _chip_link("files", "/channels" + _cqs(sort="media", order=None, offset=None), sort == "media")
        + _chip_link("members", "/channels" + _cqs(sort="members", order=None, offset=None), sort == "members")
        + _chip_link("latest post", "/channels" + _cqs(sort="recent", order=None, offset=None), sort == "recent")
        + _chip_link("name", "/channels" + _cqs(sort="name", order=None, offset=None), sort == "name")
        + '</span>')
    head = _ls_head(
        (("name", "Channel", "ls-main", "asc"), ("films", "Films", "ls-num", "desc"),
         ("media", "Files", "ls-num", "desc"), ("members", "Members", "ls-num", "desc"),
         ("recent", "Latest post", "ls-num lp", "desc"), (None, "", "ls-act", "")),
        sort, order, lambda k, d: "/channels" + _cqs(sort=k if k != "films" else None,
                                                     order=d if d != first_dir.get(k, "desc") else None,
                                                     offset=None))
    search = (
        '<form method="GET" action="/channels" style="display:flex;gap:8px;margin:0 0 12px">'
        + "".join(f'<input type="hidden" name="{k}" value="{html.escape(str(v), quote=True)}"/>'
                  for k, v in (("show", show if show != "all" else None),
                               ("sort", sort if sort != "films" else None),
                               ("order", order if order != first_dir.get(sort, "desc") else None)) if v)
        + f'<input type="text" name="q" value="{html.escape(q or "", quote=True)}" '
          'placeholder="find a channel by name or @username…" style="flex:1;max-width:420px"/>'
          '<button class="btn" type="submit">Search</button>'
        + (f'<a class="btn ghost" href="/channels{_cqs(q=None, offset=None)}">clear</a>' if q else '')
        + '</form>')

    def _row(r):
        title = r["title"] or (("@" + r["username"]) if r["username"] else "(untitled)")
        handle = (f'<a href="https://t.me/{html.escape(r["username"])}" target="_blank" rel="noopener" '
                  f'title="open in Telegram">@{html.escape(r["username"])}</a>'
                  if r["username"] else '<span>private chat</span>')
        tags = []
        if r["dead_at"]:
            tags.append('<span class="tag bad" title="Telegram no longer serves this channel">gone</span>')
        elif r["is_joined"]:
            tags.append('<span class="tag ok" title="your account is a member">joined</span>')
        elif r["subscribed"] and not r["handle_known"]:
            tags.append('<span class="tag acc" title="read from tgarr.me and Telegram\'s public web page; '
                        'your account is not used until you open one of its files">'
                        + ('followed · web' if r["web_preview"] else 'followed · listed') + '</span>')
        elif r["subscribed"]:
            tags.append('<span class="tag acc" title="read without joining">followed</span>')
        elif r["unfollowed_at"]:
            tags.append('<span class="tag warn" title="you unfollowed it: its films and files are being '
                        'cleared from this index and it will leave this list. Follow it again to keep it.">'
                        'unfollowed · being cleared</span>')
        elif r["purged_at"] and not purge_on:
            tags.append('<span class="tag" title="unfollowed and cleared; only files you saved or marked '
                        'as favourite are still here">not followed · cleared</span>')
        elif purge_on and r["purged_at"]:
            tags.append('<span class="tag" title="not followed any more; its files have been cleared '
                        'from this index. tgarr.me still lists it: follow it again to get them back.">'
                        'not followed · cleared</span>')
        elif purge_on:
            tags.append('<span class="tag warn" title="not followed any more: its films and files are '
                        'being cleared from this index (tgarr.me keeps the record). Follow it again '
                        'to keep them.">not followed · being cleared</span>')
        else:
            tags.append('<span class="tag" title="not read any more; what was indexed stays">'
                        'not followed</span>')
        if _shared(r):
            tags.append('<span class="tag" title="listed on tgarr.me">shared</span>')
        if (r["audience"] or "sfw") == "nsfw":
            tags.append('<span class="tag warn">18+</span>')
        if not r["backfilled"] and not r["dead_at"]:
            tags.append('<span class="tag warn" title="still reading its history">reading…</span>')
        if r["subscribe_error"] and not r["dead_at"]:
            tags.append(f'<span class="tag bad" title="{html.escape(r["subscribe_error"], quote=True)}">'
                        'cannot read</span>')
        has = []
        for n, ico, word, href in (
                (r["films"], "🎬", "films", None),
                (r["videos"], "▶", "videos", None),
                (r["songs"], "🎵", "songs", None),
                (r["docs"], "📄", "files", None),
                (r["photos"], "🖼", "photos",
                 f'/gallery?channel={_q(r["username"])}' if r["username"] else None)):
            if n:
                txt = f'{ico} {_n_things(n, word)}'
                has.append(f'<a href="{href}">{txt}</a>' if href else f'<span>{txt}</span>')
        act = ""
        _ids = (f'data-id="{r["tg_chat_id"]}" '
                f'data-uname="{html.escape(r["username"] or "", quote=True)}"')
        if r["subscribed"] and not r["is_joined"]:
            act = (f'<button class="abtn" {_ids} onclick="tgUnfollow(this)" '
                   f'title="stop reading this channel">Unfollow</button>')
        elif not r["is_joined"] and not r["dead_at"] and r["username"]:
            act = (f'<button class="abtn pri" {_ids} onclick="tgFollow(this)" '
                   f'title="read this public channel again, without joining it">+ Follow</button>')
        return (
            f'<div class="ls-row">{_avatar(title)}'
            f'<div class="ls-main"><div class="ls-title">{html.escape(title)} '
            f'{" ".join(tags)}</div>'
            f'<div class="ls-sub">{handle}{"".join(has) or "<span>no files indexed</span>"}</div></div>'
            f'<div class="ls-num"><b>{_fmt_count(r["films"]) if r["films"] else "–"}</b></div>'
            f'<div class="ls-num"><b>{_fmt_count(r["media"]) if r["media"] else "–"}</b></div>'
            f'<div class="ls-num"><b>{_fmt_count(r["members_count"])}</b></div>'
            f'<div class="ls-num lp"><b style="font-size:13px;font-weight:500">'
            f'{_ago(r["last_post"]) or "–"}</b></div>'
            f'<div class="ls-act">{act}</div></div>')

    if not page:
        listing = ('<div class="empty-state"><div class="icon">📡</div><div>'
                   + ("No channel matches." if (q or show != "all") else
                      "No channels yet. Join channels in Telegram, or follow some from Discover.")
                   + '</div></div>')
    else:
        listing = (f'<div class="ls">{head}{"".join(_row(r) for r in page)}</div>'
                   + _page_nav("/channels", _cqs, page_offset, page_size, len(page), len(view)))
    body = (
        f'{_LIST_CSS}<style>.ls-num{{width:76px}}.ls-num.lp{{width:104px}}</style>'
        f'<h2 class="section">Channels <span class="count">{len(view):,}'
        + (f' of {len(rows):,}' if len(view) != len(rows) else '') + '</span>'
        '<span class="extra" style="color:var(--muted);font-size:12px;font-weight:400;text-transform:none;'
        'letter-spacing:0;margin-left:10px"><a href="/discover">find more on Discover →</a></span></h2>'
        f'{search}<div class="bar-tools">{chips}</div>{listing}'
        # The button changes where it is. Reloading the page for one click
        # threw the reader back to the top of a long list every time.
        '<script>var tgUnfollowAsk=' + _js_str(
            "Stop reading this channel and take it off this list?\n\n"
            "Its films and files are cleared from this index within a few hours. Files you saved to "
            "this machine and your favourites are kept. You can find the channel again on Discover.")
        + r""";
function tgChGone(b, note) {
  var row = b.closest(".ls-row");
  b.disabled = true; b.textContent = note;
  if (row) { row.style.transition = "opacity .5s"; row.style.opacity = "0.35";
             setTimeout(function () { row.remove(); }, 900); }
}
function tgChSet(b, following) {
  // unfollowed: it has left the list. Followed again from "being cleared": it has left that view.
  if (!following) { tgChGone(b, "Removed"); return; }
  if (location.search.indexOf("show=clearing") >= 0) { tgChGone(b, "Following"); return; }
  b.disabled = false;
  if (following) {
    b.className = "abtn"; b.textContent = "Unfollow"; b.title = "stop reading this channel";
    b.onclick = function () { tgUnfollow(b); };
  } else if (b.dataset.uname) {
    b.className = "abtn pri"; b.textContent = "+ Follow";
    b.title = "read this public channel again, without joining it";
    b.onclick = function () { tgFollow(b); };
  } else { b.remove(); }
}
function tgChFail(b, label, why) {
  b.disabled = false; b.textContent = "Failed - retry"; b.title = why || "the request did not go through";
  setTimeout(function () { if (b.textContent === "Failed - retry") b.textContent = label; }, 4000);
}
function tgUnfollow(b) {
  if (!confirm(tgUnfollowAsk)) return;
  b.disabled = true; b.textContent = "…";
  fetch("/api/channel/unsubscribe/" + b.dataset.id, {method: "POST"})
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); tgChSet(b, false); })
    .catch(function (e) { tgChFail(b, "Unfollow", e.message); });
}
function tgFollow(b) {
  b.disabled = true; b.textContent = "…";
  fetch("/api/channel/subscribe", {method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: "username=" + encodeURIComponent(b.dataset.uname)})
    .then(function (r) { return r.json().then(function (j) {
      if (!r.ok || j.status === "error") throw new Error(j.message || ("HTTP " + r.status));
      tgChSet(b, true); }); })
    .catch(function (e) { tgChFail(b, "+ Follow", e.message); });
}
</script>""")
    return HTMLResponse(_layout("Channels", "/channels", body))


# ════════════════════════════════════════════════════════════════════
# Page: Discover (registry-pulled channels awaiting subscription)
# ════════════════════════════════════════════════════════════════════
def _profile_pills(r) -> str:
    """Small pills saying what a discovered channel carries."""
    out = []
    for col, label in (("release_files", "🎬 {n} releases"),
                       ("book_files", "📚 {n} books"),
                       ("audio_files", "🎵 {n} tracks")):
        n = r[col] or 0
        if n > 0:
            out.append(f'<span class="pill accent">{label.format(n=f"{n:,}")}</span>')
    return "".join(out)


@app.get("/discover", response_class=HTMLResponse)
async def page_discover(audience: str = "sfw", kind: str = "", q: Optional[str] = None,
                        sort: str = "best", order: str = "", mine: int = 0, offset: int = 0,
                        limit: int = 50):
    async with db_pool.acquire() as conn:
        n_channels = await conn.fetchval("SELECT count(*) FROM channels")
        if not login.session_exists() and not n_channels:
            return RedirectResponse("/login")
        # Telegram sometimes returns a differently-cased username
        # (eBookRoom vs ebookroom): match case-insensitively.
        known = {r["username"].lower(): (r["subscribed"] or r["is_joined"])
                 for r in await conn.fetch(
                     "SELECT username, subscribed, is_joined FROM channels "
                     "WHERE username IS NOT NULL")}
        nsfw_on = await _is_nsfw_enabled()
        if audience == "nsfw" and not nsfw_on:
            audience = "sfw"
        where = ["dismissed = FALSE"]
        if audience in ("sfw", "nsfw"):
            where.append(f"audience = '{audience}'")
        kind = kind if kind in ("video", "books", "music") else ""
        kind_col = {"video": "release_files", "books": "book_files",
                    "music": "audio_files"}.get(kind)
        if kind_col:
            where.append(f"COALESCE({kind_col}, 0) > 0")
        rows = await conn.fetch(f"""
            SELECT username, title, members_count, media_count, audience,
                   language, category, distinct_contributors, verified,
                   last_pulled_at, release_files, video_files, book_files, audio_files,
                   last_file_at
            FROM discovered
            WHERE {' AND '.join(where)}""")
        total = await conn.fetchval("SELECT count(*) FROM discovered WHERE dismissed = FALSE")
        last_pull = await conn.fetchval("SELECT max(last_pulled_at) FROM discovered")

    have = [r for r in rows if known.get(r["username"].lower())]
    view = rows if mine else [r for r in rows if not known.get(r["username"].lower())]
    if q:
        words = [w.lower() for w in q.split()]
        view = [r for r in view
                if all(w in ((r["title"] or "") + " " + r["username"]).lower() for w in words)]
    lead = kind_col or "release_files"
    def _dname(r):
        return (r["title"] or r["username"]).lower()
    sorts = {
        # what the channel carries first - the reason most people install
        # tgarr - then how many installs vouch for it
        "best": lambda r: (r[lead] or 0, int(bool(r["verified"])),
                           r["distinct_contributors"] or 0, r["members_count"] or 0),
        "lead": lambda r: r[lead] or None,
        "seen": lambda r: (r["distinct_contributors"] or 0, r[lead] or 0),
        "members": lambda r: r["members_count"],
        "files": lambda r: r["media_count"] or None,
        "recent": lambda r: r["last_file_at"],
        "name": _dname,
    }
    if sort not in sorts:
        sort = "best"
    dfirst = {"name": "asc"}
    if order not in ("asc", "desc"):
        order = dfirst.get(sort, "desc")
    view = _ls_sort(view, sorts[sort], order, _dname)
    page_size = max(10, min(limit, 200))
    page_offset = max(0, offset)
    page = view[page_offset:page_offset + page_size]

    def _dq(**over):
        cur = {"kind": kind or None, "audience": audience if audience != "sfw" else None,
               "q": q, "sort": sort if sort != "best" else None,
               "order": order if order != dfirst.get(sort, "desc") else None,
               "mine": 1 if mine else None,
               "limit": limit if limit != 50 else None}
        cur.update(over)
        bits = [f"{k}={_q(str(v))}" for k, v in cur.items() if v not in (None, "", 0)]
        return "?" + "&".join(bits) if bits else ""

    chips = (
        _chip_link("All", "/discover" + _dq(kind=None, offset=None), not kind)
        + _chip_link("🎬 Movies &amp; TV", "/discover" + _dq(kind="video", offset=None), kind == "video")
        + _chip_link("📚 Books", "/discover" + _dq(kind="books", offset=None), kind == "books")
        + _chip_link("🎵 Music", "/discover" + _dq(kind="music", offset=None), kind == "music")
        + ('<span class="sep"></span>'
           + _chip_link("SFW", "/discover" + _dq(audience=None, offset=None), audience == "sfw")
           + _chip_link("18+", "/discover" + _dq(audience="nsfw", offset=None), audience == "nsfw")
           if nsfw_on else '')
        + '<span class="sep"></span>'
        + _chip_link("best match", "/discover" + _dq(sort=None, order=None, offset=None), sort == "best",
                     "most of what you are looking at, then how many installs vouch for it")
        + '<span class="sort-chips"><span class="lbl">Sort:</span>'
        + _chip_link("most seen", "/discover" + _dq(sort="seen", order=None, offset=None), sort == "seen",
                     "indexed by the most tgarr installs")
        + _chip_link("members", "/discover" + _dq(sort="members", order=None, offset=None), sort == "members")
        + _chip_link("files", "/discover" + _dq(sort="files", order=None, offset=None), sort == "files")
        + _chip_link("latest file", "/discover" + _dq(sort="recent", order=None, offset=None), sort == "recent")
        + '</span>'
        + '<span class="sep"></span>'
        + _chip_link(f'include the {len(have):,} I already have', "/discover" + _dq(mine=None if mine else 1, offset=None),
                     bool(mine)))
    search = (
        '<form method="GET" action="/discover" style="display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px;align-items:center">'
        + "".join(f'<input type="hidden" name="{k}" value="{html.escape(str(v), quote=True)}"/>'
                  for k, v in (("kind", kind), ("audience", audience if audience != "sfw" else None),
                               ("sort", sort if sort != "best" else None),
                               ("order", order if order != dfirst.get(sort, "desc") else None),
                               ("mine", 1 if mine else None)) if v)
        + f'<input type="text" name="q" value="{html.escape(q or "", quote=True)}" '
          'placeholder="find a channel by name or @username…" style="flex:1;min-width:150px;max-width:420px"/>'
          '<button class="btn" type="submit">Search</button>'
        + (f'<a class="btn ghost" href="/discover{_dq(q=None, offset=None)}">clear</a>' if q else '')
        + '<span style="flex:1"></span>'
          '<span id="tgPullToast" style="font-size:13px;color:var(--muted);display:none"></span>'
        + ('<button type="button" class="abtn" id="tgPullBtn" onclick="tgRegistryPull(this)" '
           'title="ask tgarr.me for its current list now">↻ Refresh list</button>' if _signed_in() else '')
        + '</form>')

    _in = _signed_in()

    def _card(r):
        u = r["username"]
        mine_ = known.get(u.lower())
        btn = ('<a class="abtn" href="/login" title="following a channel needs a Telegram account">'
               'Sign in to follow</a>' if not _in else
               '<button class="abtn" disabled>✓ Following</button>' if mine_ else
               f'<button class="abtn pri" data-uname="{html.escape(u, quote=True)}" '
               f'onclick="tgDiscoverSubscribe(this)" '
               f'title="tgarr reads this public channel without your account joining it">+ Follow</button>')
        title = r["title"] or ("@" + u)
        has = []
        for n, ico, word in ((r["release_files"], "🎬", "films"), (r["book_files"], "📚", "books"),
                             (r["audio_files"], "🎵", "songs")):
            if n:
                has.append(f'<span style="color:var(--fg)">{ico} {_n_things(n, word)}</span>')
        if r["media_count"]:
            has.append(f'<span>{_n_things(r["media_count"], "files")}</span>')
        seen = r["distinct_contributors"] or 1
        return (
            f'<div class="ls-row">{_avatar(title)}'
            f'<div class="ls-main"><div class="ls-title">{html.escape(title)} '
            + ('<span class="tag ok" title="tgarr.me checked that this channel exists and is public">✓ verified</span> '
               if r["verified"] else '')
            + ('<span class="tag warn">18+</span>' if r["audience"] == "nsfw" else '')
            + f'</div><div class="ls-sub"><a href="https://t.me/{html.escape(u)}" target="_blank" rel="noopener" '
            f'title="open in Telegram">@{html.escape(u)}</a>{"".join(has)}</div></div>'
            f'<div class="ls-num"><b>{_fmt_count(r[lead]) if r[lead] else "–"}</b></div>'
            f'<div class="ls-num"><b>{_fmt_count(r["media_count"]) if r["media_count"] else "–"}</b></div>'
            f'<div class="ls-num"><b>{_fmt_count(r["members_count"])}</b></div>'
            f'<div class="ls-num" title="tgarr installs that index this channel"><b>{seen:,}</b></div>'
            f'<div class="ls-num lp" title="when the newest file that tgarr installs have reported for this '
            f'channel was posted"><b style="font-size:13px;font-weight:500">'
            f'{_ago(r["last_file_at"]) or "–"}</b></div>'
            f'<div class="ls-act">{btn}</div></div>')

    if not rows and not total:
        listing = ('<div class="empty-state"><div class="icon">🔭</div>'
                   '<div>The list from tgarr.me has not arrived yet.</div>'
                   '<div style="margin-top:8px;font-size:13px">It is fetched every 12 hours; '
                   '“Refresh list” asks for it now.</div></div>')
    elif not page:
        listing = ('<div class="empty-state"><div class="icon">🔭</div><div>'
                   + ("No channel matches." if (q or kind) else
                      "This part of the list has not arrived yet." if not rows else
                      "You already follow everything on the list.") + '</div>'
                   + ('<div style="margin-top:8px;font-size:13px">“Refresh list” asks tgarr.me for it now.</div>'
                      if not rows and not (q or kind) else '') + '</div>')
    else:
        dhead = _ls_head(
            (("name", "Channel", "ls-main", "asc"),
             ("lead", {"book_files": "Books", "audio_files": "Songs"}.get(lead, "Films"), "ls-num", "desc"),
             ("files", "Files", "ls-num", "desc"), ("members", "Members", "ls-num", "desc"),
             ("seen", "Installs", "ls-num", "desc"), ("recent", "Latest file", "ls-num lp", "desc"),
             (None, "", "ls-act", "")),
            sort, order, lambda k, d: "/discover" + _dq(sort=k, order=d if d != dfirst.get(k, "desc") else None,
                                                        offset=None))
        listing = (f'<div class="ls">{dhead}{"".join(_card(r) for r in page)}</div>'
                   + _page_nav("/discover", _dq, page_offset, page_size, len(page), len(view)))

    body = (
        f'{_LIST_CSS}<style>.ls-num{{width:76px}}.ls-num.lp{{width:104px}}</style>'
        f'<h2 class="section">Discover <span class="count">{len(view):,}</span>'
        '<span class="extra" style="color:var(--muted);font-size:12px;font-weight:400;text-transform:none;'
        f'letter-spacing:0;margin-left:10px">public channels other tgarr installs index · '
        f'{total or 0:,} on tgarr.me'
        + (f' · list from {_ago(last_pull)}' if last_pull else '') + '</span></h2>'
        f'{search}<div class="bar-tools">{chips}</div>{listing}'
        '<script>'
        '(() => {'
        '  window.tgRegistryPull = async function(btn) {'
        '    const toast = document.getElementById("tgPullToast");'
        '    btn.disabled = true; btn.textContent = "Refreshing…";'
        '    toast.style.display = "inline";'
        '    toast.textContent = "asking tgarr.me (up to a minute)…";'
        '    toast.style.color = "var(--muted)";'
        '    try {'
        '      const r = await fetch("/api/registry/pull-now", {method:"POST"});'
        '      const j = await r.json();'
        '      if (j.status === "ok") {'
        '        toast.textContent = "✓ " + j.affected + " channel(s) updated";'
        '        toast.style.color = "var(--ok)";'
        '        if (j.affected > 0) setTimeout(() => location.reload(), 1200);'
        '      } else if (j.status === "queued") {'
        '        toast.textContent = "queued — it will run shortly";'
        '        toast.style.color = "var(--warn)";'
        '      } else {'
        '        toast.textContent = "✗ " + (j.message || "error");'
        '        toast.style.color = "var(--err)";'
        '      }'
        '    } catch (e) {'
        '      toast.textContent = "✗ network error";'
        '      toast.style.color = "var(--err)";'
        '    } finally {'
        '      btn.disabled = false; btn.textContent = "↻ Refresh list";'
        '    }'
        '  };'
        '  window.tgDiscoverSubscribe = async function(btn) {'
        '    const u = btn.dataset.uname;'
        '    btn.disabled = true; btn.textContent = "…";'
        '    try {'
        '      const r = await fetch("/api/channel/subscribe", {method:"POST", '
        '          headers:{"Content-Type":"application/x-www-form-urlencoded"}, '
        '          body: "username=" + encodeURIComponent(u)});'
        '      const j = await r.json();'
        '      if (j.status === "queued" || j.status === "ok") {'
        '        btn.textContent = "✓ Following"; btn.classList.remove("pri");'
        '      } else { btn.disabled = false; btn.textContent = "✗ " + (j.message || "error"); }'
        '    } catch(e) { btn.disabled = false; btn.textContent = "✗ network"; }'
        '  };'
        '})();'
        '</script>'
    )
    return HTMLResponse(_layout("Discover", "/discover", body))


@app.post("/api/registry/pull-now")
async def api_registry_pull_now():
    """Force the next registry_puller tick to fire immediately and block until
    it consumes the sentinel (up to ~70s — crawler polls config every 60s).
    Returns JSON with how many discovered rows changed so the UI can show a
    real result toast instead of silent redirect.
    """
    import asyncio
    async with db_pool.acquire() as conn:
        before_max = await conn.fetchval(
            "SELECT MAX(last_pulled_at) FROM discovered")
        await conn.execute(
            """INSERT INTO config (key, value) VALUES ('registry_pull_force', $1)
               ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=NOW()""",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    # Poll for sentinel deletion (crawler consumed it = pull completed).
    for _ in range(140):  # 140 * 0.5s = 70s
        await asyncio.sleep(0.5)
        async with db_pool.acquire() as conn:
            sentinel = await conn.fetchval(
                "SELECT value FROM config WHERE key='registry_pull_force'")
        if sentinel is None:
            async with db_pool.acquire() as conn:
                if before_max:
                    affected = await conn.fetchval(
                        "SELECT COUNT(*) FROM discovered WHERE last_pulled_at > $1",
                        before_max)
                else:
                    affected = await conn.fetchval(
                        "SELECT COUNT(*) FROM discovered WHERE last_pulled_at IS NOT NULL")
            return JSONResponse({"status": "ok", "affected": int(affected or 0)})

    return JSONResponse(
        {"status": "queued",
         "message": "crawler didn\'t pick up sentinel within 70s; pull will run in background"},
        status_code=202)


# ════════════════════════════════════════════════════════════════════
# Page: Gallery (Telegram-shared photos)
# ════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════
# Usenet - a second source, for people who have a Usenet account
#
# Off until an account is entered in Settings and the server answers. Then:
#   * a slow scanner reads newsgroup headers and keeps one small card per
#     named video / audio / e-book / photo release (usenet.py);
#   * /newznab/usenet/api offers those cards to Sonarr and Radarr;
#   * a grab is answered with the release's NZB, built at that moment by
#     reading its stretch of headers again. tgarr does not download from
#     Usenet: the *arr hands the NZB to its own downloader (SABnzbd, NZBGet).
# In the *arr this is a second indexer entry, because an indexer is tied to
# one download client: "tgarr" goes to tgarr itself (Telegram), "tgarr
# Usenet" to the Usenet downloader.
# ════════════════════════════════════════════════════════════════════
_USENET = {"at": 0.0, "cfg": {}, "ok": None, "msg": "not tried yet", "checked": 0.0}
_USENET_KEYS = ("usenet_enabled", "usenet_host", "usenet_port", "usenet_ssl", "usenet_user", "usenet_pass",
                "usenet_per_day")
USENET_GROUPS = ("alt.binaries.teevee", "alt.binaries.hdtv.x264", "alt.binaries.x264", "alt.binaries.multimedia",
                 "alt.binaries.tv", "alt.binaries.movies", "alt.binaries.sounds.lossless", "alt.binaries.ebook",
                 "alt.binaries.e-book.technical")
USENET_BLOCK = 200_000               # headers read in one go
USENET_SETTLE = 30_000               # a release is taken as finished this many articles behind the reader
USENET_START = "2009-01-01"          # where a group is first read from: old posts carry readable names


async def _usenet_cfg(fresh: bool = False) -> dict:
    now = time.monotonic()
    if (fresh or now - _USENET["at"] > 5) and db_pool is not None:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM config WHERE key = ANY($1::text[])", list(_USENET_KEYS))
        d = {r["key"][7:]: r["value"] for r in rows}
        _USENET.update(at=now, cfg={
            "enabled": d.get("enabled") == "1", "host": (d.get("host") or "").strip(),
            "port": int(d.get("port") or 563), "ssl": d.get("ssl", "1") != "0",
            "user": d.get("user") or "", "pass": d.get("pass") or "",
            "per_day": max(0, int(d.get("per_day") or 2_000_000))})
    return _USENET["cfg"]


def _usenet_connect(cfg: dict):
    return usenet.NNTP(cfg["host"], cfg["port"], cfg["user"] or None, cfg["pass"] or None, use_ssl=cfg["ssl"])


def _usenet_try(cfg: dict) -> str:
    """'' when the server lets us in and answers, otherwise why not (never the account's name)."""
    try:
        nn = _usenet_connect(cfg)
    except usenet.NNTPError as e:
        return str(e)
    except Exception as e:
        return f"could not connect: {type(e).__name__}"
    try:
        nn.group("alt.binaries.teevee")
        return ""
    except Exception as e:
        return f"connected, but the server did not open a group: {e}"
    finally:
        nn.quit()


async def _usenet_ready(recheck: bool = False) -> bool:
    """Switched on, an account entered, and the server answered when last asked
    (asked again at most every ten minutes)."""
    cfg = await _usenet_cfg()
    if not (cfg.get("enabled") and cfg.get("host")):
        return False
    if recheck or _USENET["ok"] is None or time.monotonic() - _USENET["checked"] > 600:
        msg = await asyncio.to_thread(_usenet_try, cfg)
        _USENET.update(ok=not msg, msg=msg or "working", checked=time.monotonic())
    return bool(_USENET["ok"])


def _usenet_scan_block(cfg: dict, grp: str, start, asm, count: int):
    """Read `count` headers of a group from article `start` (None: from
    USENET_START), feed them to the group's assembler, and return the cards of
    the releases that are finished. Runs in a thread."""
    import calendar
    import random
    nn = _usenet_connect(cfg)
    try:
        _n, first, last = nn.group(grp)
        if start is None or start < first:
            start = usenet.article_at(nn, first, last, calendar.timegm(time.strptime(USENET_START, "%Y-%m-%d")))
        b0, lo, end = nn.bytes_in, start, min(last, start + count - 1)
        while lo <= end:
            for row in nn.over(lo, min(end, lo + 19999)):
                asm.add(*row)
            lo += 20000
        cursor, cards = end + 1, []
        for k, r in list(asm.rel.items()):
            if r["art_hi"] >= cursor - USENET_SETTLE and cursor <= last:
                continue                              # may still be arriving
            del asm.rel[k]
            c = usenet.card(r)
            if not c:
                continue
            ids = [m for f in r["files"].values() for m, _s in f["parts"].values()]
            sample = random.sample(ids, min(4, len(ids)))
            c["alive"] = round(sum(1 for m in sample if nn.stat(m)) / max(1, len(sample)), 2)
            cards.append(c)
        resume = min([r["art_lo"] for r in asm.rel.values()] + [cursor])
        return cards, cursor, max(resume, cursor - USENET_BLOCK), end - start + 1, nn.bytes_in - b0, first, last
    finally:
        nn.quit()


async def _usenet_scan_worker():
    """Reads a block of one group, then the next group, staying under the
    day's allowance of headers (their download is the cost: about 350 bytes
    each). Only while Usenet is switched on and the server answers."""
    await asyncio.sleep(90)
    logu = logging.getLogger("tgarr.usenet")
    asms, cursors, turn = {}, {}, 0
    while True:
        try:
            if not await _usenet_ready():
                await asyncio.sleep(300)
                continue
            cfg = await _usenet_cfg()
            day = time.strftime("%Y-%m-%d", time.gmtime())
            async with db_pool.acquire() as conn:
                used = await conn.fetchval("SELECT value FROM config WHERE key = 'usenet_today'") or ""
                rows = await conn.fetch("SELECT grp, next_article, enabled FROM usenet_progress")
            used_n = int(used.split(":")[1]) if used.startswith(day + ":") else 0
            room = cfg["per_day"] - used_n
            if room < 20000:
                await asyncio.sleep(900)
                continue
            known = {r["grp"]: r for r in rows}
            groups = [g for g in USENET_GROUPS if g not in known or known[g]["enabled"]]
            if not groups:
                await asyncio.sleep(900)
                continue
            grp = groups[turn % len(groups)]
            turn += 1
            start = cursors.get(grp, known[grp]["next_article"] if grp in known else None)
            asm = asms.setdefault(grp, usenet.Assembler(grp))
            try:
                cards, cursor, resume, n, nbytes, first, last = await asyncio.to_thread(
                    _usenet_scan_block, cfg, grp, start, asm, min(USENET_BLOCK, room))
            except Exception as e:
                logu.warning("[usenet] %s: %s", grp, str(e)[:120])
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO usenet_progress (grp, note) VALUES ($1, $2)
                           ON CONFLICT (grp) DO UPDATE SET note = $2, updated_at = NOW()""", grp, str(e)[:200])
                await asyncio.sleep(120)
                continue
            cursors[grp] = cursor
            async with db_pool.acquire() as conn:
                await _usenet_store(conn, cards)
                await conn.execute(
                    """INSERT INTO usenet_progress (grp, first_article, last_article, next_article, headers, cards, bytes, note)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, NULL)
                       ON CONFLICT (grp) DO UPDATE SET first_article = $2, last_article = $3, next_article = $4,
                           headers = usenet_progress.headers + $5, cards = usenet_progress.cards + $6,
                           bytes = usenet_progress.bytes + $7, note = NULL, updated_at = NOW()""",
                    grp, first, last, resume, n, len(cards), nbytes)
                await conn.execute(
                    """INSERT INTO config (key, value) VALUES ('usenet_today', $1)
                       ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()""", f"{day}:{used_n + n}")
            logu.info("[usenet] %s: %s headers, %s cards, %.0f MB", grp, f"{n:,}", len(cards), nbytes / 1e6)
            await asyncio.sleep(20 if cursor <= last else 600)
        except Exception as e:
            logu.warning("[usenet] scanner: %s: %s", type(e).__name__, str(e)[:160])
            await asyncio.sleep(300)


_USENET_COLS = ("id", "name", "kind", "size", "grp", "poster", "posted_from", "posted_to", "files", "files_expected",
                "segments", "segments_expected", "complete", "first_message_id", "key", "art_lo", "art_hi", "alive")


async def _usenet_store(conn, cards: list) -> None:
    for c in cards:
        row = dict(c)
        row["grp"] = row.pop("group", row.get("grp"))
        await conn.execute(
            f"""INSERT INTO usenet_cards ({", ".join(_USENET_COLS)})
                VALUES ({", ".join(f"${i + 1}" for i in range(len(_USENET_COLS)))})
                ON CONFLICT (id) DO UPDATE SET size = EXCLUDED.size, files = EXCLUDED.files,
                    segments = EXCLUDED.segments, segments_expected = EXCLUDED.segments_expected,
                    complete = EXCLUDED.complete, art_lo = LEAST(usenet_cards.art_lo, EXCLUDED.art_lo),
                    art_hi = GREATEST(usenet_cards.art_hi, EXCLUDED.art_hi), alive = EXCLUDED.alive,
                    posted_to = EXCLUDED.posted_to""",
            *[bool(row.get(k)) if k == "complete" else row.get(k) for k in _USENET_COLS])


def _usenet_nzb(cfg: dict, c) -> str:
    """The NZB of one card, from the headers as they are on the server now. Runs in a thread."""
    nn = _usenet_connect(cfg)
    try:
        nn.group(c["grp"])
        asm = usenet.Assembler(c["grp"])
        lo, hi = c["art_lo"] - 2000, c["art_hi"] + 2000
        while lo <= hi:
            for r in nn.over(lo, min(hi, lo + 19999)):
                if usenet.poster_hash(r[2]) == c["poster"]:
                    p = usenet.parse_subject(r[1])
                    if p and usenet.release_key(p) == c["key"]:
                        asm.add(*r)
            lo += 20000
        rels = asm.releases()
        return usenet.build_nzb(rels[0]) if rels else ""
    finally:
        nn.quit()


_USENET_EP_RX = re.compile(r"S(\d{1,2})E(\d{1,3})", re.I)


def _usenet_item_xml(c, base_url: str, apikey: str) -> str:
    link = html.escape(f"{base_url}/newznab/usenet/api?t=get&id=un-{c['id']}&apikey={apikey}", quote=True)
    name, hd = c["name"], bool(re.search(r"\b(720|1080|2160)[pi]\b", c["name"], re.I))
    ep = _USENET_EP_RX.search(name)
    if c["kind"] == "video":
        cat = ("5040" if hd else "5030") if ep or re.search(r"\b\d{4}[. ]\d{2}[. ]\d{2}\b", name) else ("2040" if hd else "2030")
    else:
        cat = {"audio": "3010", "ebook": "7020"}.get(c["kind"], "8000")
    when = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(c["posted_from"] or 0))
    attrs = f'      <newznab:attr name="category" value="{cat}"/>\n      <newznab:attr name="size" value="{c["size"] or 0}"/>\n'
    if ep and cat.startswith("5"):
        attrs += (f'      <newznab:attr name="season" value="{int(ep.group(1))}"/>\n'
                  f'      <newznab:attr name="episode" value="{int(ep.group(2))}"/>\n')
    attrs += f'      <newznab:attr name="group" value="{html.escape(c["grp"])}"/>\n'
    return (f"    <item>\n      <title>{html.escape(name)}</title>\n"
            f"      <guid isPermaLink=\"false\">un-{c['id']}</guid>\n      <link>{link}</link>\n"
            f"      <pubDate>{when}</pubDate>\n      <category>{cat}</category>\n      <size>{c['size'] or 0}</size>\n"
            f"      <description>Usenet, {html.escape(c['grp'])}</description>\n"
            f"      <enclosure url=\"{link}\" length=\"{c['size'] or 0}\" type=\"application/x-nzb\"/>\n{attrs}    </item>")


@app.get("/newznab/usenet/api", response_class=PlainTextResponse)
@app.get("/newznab/usenet", response_class=PlainTextResponse)
async def newznab_usenet(request: Request, t: str = Query("search"), q: Optional[str] = Query(None),
                         season: Optional[int] = None, ep: Optional[int] = None, cat: Optional[str] = None,
                         limit: int = 100, offset: int = 0, apikey: Optional[str] = "tgarr",
                         id: Optional[str] = None):
    base_url = _base_url(request)
    if t == "caps":
        return Response(CAPS_XML, media_type="application/xml")
    if t not in ("search", "tvsearch", "movie", "get"):
        return _newznab_error(202, f"No such function ({t})")
    # Nothing is offered unless Usenet is switched on and the server answers:
    # a result that cannot be turned into an NZB is worse than no result.
    if not await _usenet_ready():
        if t == "get":
            return _newznab_error(300, "Usenet is switched off or the news server is not answering", 503)
        return Response(_feed_envelope("", "usenet (off)", base_url), media_type="application/xml")
    if t == "get":
        if not id or not re.fullmatch(r"un-[0-9a-f]{20}", id):
            return _newznab_error(300, "No such item", 404)
        async with db_pool.acquire() as conn:
            c = await conn.fetchrow("SELECT * FROM usenet_cards WHERE id = $1", id[3:])
        if not c:
            return _newznab_error(300, "No such item", 404)
        try:
            text = await asyncio.to_thread(_usenet_nzb, await _usenet_cfg(), c)
        except Exception as e:
            _USENET.update(ok=None)                          # look at the server again before the next answer
            return _newznab_error(300, f"The news server did not give the headers: {str(e)[:80]}", 503)
        if not text:
            return _newznab_error(300, "This release is no longer on the news server", 404)
        safe = "".join(ch if ord(ch) < 128 and ch not in '"\\' else "_" for ch in c["name"])[:150]
        return Response(text, media_type="application/x-nzb",
                        headers={"Content-Disposition": f'attachment; filename="{safe}.nzb"'})
    where, params = ["alive >= 0.5"], []
    for w in re.split(r"[\s.]+", q or "")[:8]:
        if w:
            params.append("%" + w.replace("%", "").replace("_", r"\_") + "%")
            where.append(f"name ILIKE ${len(params)}")
    if t == "tvsearch":
        where.append(r"kind = 'video' AND name ~* 'S\d{1,2}E\d{1,3}|\d{4}[. ]\d{2}[. ]\d{2}'")
        if season is not None and ep is not None:
            params.append(rf"S0*{int(season)}E0*{int(ep)}(?!\d)")
            where.append(f"name ~* ${len(params)}")
        elif season is not None:
            params.append(rf"S0*{int(season)}(E\d|\M)")
            where.append(f"name ~* ${len(params)}")
    elif t == "movie":
        where.append(r"kind = 'video' AND name !~* 'S\d{1,2}E\d{1,3}'")
    else:
        where.append("kind IN ('video', 'audio', 'ebook')")
    lim = max(1, min(int(limit), 200))
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT *, count(*) OVER() AS _total FROM usenet_cards WHERE {' AND '.join(where)}
                ORDER BY complete DESC, posted_from DESC NULLS LAST LIMIT {lim} OFFSET {max(0, int(offset))}""", *params)
    items = "\n".join(_usenet_item_xml(r, base_url, apikey or "tgarr") for r in rows)
    return Response(_feed_envelope(items, f"usenet t={t} q={q or ''}", base_url, offset=max(0, int(offset)),
                                   total=rows[0]["_total"] if rows else 0), media_type="application/xml")


@app.post("/api/usenet/settings")
async def api_usenet_settings(request: Request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    vals = {"usenet_enabled": "1" if b.get("enabled") else "0", "usenet_host": str(b.get("host") or "").strip()[:200],
            "usenet_port": str(int(b.get("port") or 563)), "usenet_ssl": "1" if b.get("ssl", True) else "0",
            "usenet_user": str(b.get("user") or "")[:200],
            "usenet_per_day": str(max(0, min(int(b.get("per_day") or 2_000_000), 200_000_000)))}
    if b.get("password"):                                  # left empty = keep the one already kept
        vals["usenet_pass"] = str(b["password"])[:300]
    async with db_pool.acquire() as conn:
        for k, v in vals.items():
            await conn.execute("""INSERT INTO config (key, value) VALUES ($1, $2)
                                  ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""", k, v)
    await _usenet_cfg(fresh=True)
    _USENET.update(ok=None)
    ready = await _usenet_ready(recheck=True)
    return {"status": "ok", "working": ready, "message": _USENET["msg"] if vals["usenet_enabled"] == "1" else "switched off"}


@app.post("/api/usenet/enabled")
async def api_usenet_enabled(request: Request):
    """The on/off switch by itself: the account stays as it is."""
    try:
        on = bool((await request.json()).get("on"))
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    async with db_pool.acquire() as conn:
        await conn.execute("""INSERT INTO config (key, value) VALUES ('usenet_enabled', $1)
                              ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()""", "1" if on else "0")
    await _usenet_cfg(fresh=True)
    _USENET.update(ok=None)
    return {"status": "ok", "on": on, "working": await _usenet_ready(recheck=True) if on else False}


@app.get("/settings/usenet", response_class=HTMLResponse)
async def page_settings_usenet(request: Request):
    cfg = await _usenet_cfg(fresh=True)
    ready = await _usenet_ready() if cfg.get("enabled") and cfg.get("host") else False
    state = ('<span class="pill ok">working</span>' if ready else
             '<span class="pill muted">switched off</span>' if not cfg.get("enabled") else
             f'<span class="pill bad">not working</span> <span style="color:var(--muted)">{html.escape(_USENET["msg"])}</span>')
    base = _base_url(request)
    body = f"""<style>.un-box{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:22px}}
.un-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:12px 0}}
.un-grid label{{display:flex;flex-direction:column;gap:5px;font-size:13px;color:var(--muted)}}
.un-sw{{display:inline-flex;align-items:center;gap:10px;cursor:pointer;user-select:none}}
.un-sw input{{position:absolute;opacity:0;width:0;height:0}}
.un-sw span{{width:50px;height:28px;border-radius:999px;background:#cbd5e1;position:relative;transition:background .15s}}
.un-sw span::after{{content:"";position:absolute;left:3px;top:3px;width:22px;height:22px;border-radius:50%;background:#fff;
  box-shadow:0 1px 3px rgba(0,0,0,.3);transition:left .15s}}
.un-sw input:checked + span{{background:#16a34a}}.un-sw input:checked + span::after{{left:25px}}
.un-sw input:focus-visible + span{{outline:2px solid var(--accent);outline-offset:2px}}
.un-grid input{{padding:9px 12px;border:1px solid var(--border);border-radius:8px;font:inherit;background:var(--surface);color:var(--fg)}}
</style>
<div class="un-box"><div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:8px">
<h3 style="margin:0;flex:1">Usenet {state}</h3>
<label class="un-sw" title="switch Usenet on or off"><input type="checkbox" id="unOn" {"checked" if cfg.get("enabled") else ""} onchange="unToggle(this)"/><span></span><b>{"On" if cfg.get("enabled") else "Off"}</b></label></div>
<p style="color:var(--muted);margin:0 0 4px">With a Usenet account, tgarr reads newsgroup headers a little at a time, keeps a small
card for every named film, episode, album and book it finds, and offers them to Sonarr and Radarr beside what it finds on Telegram.
tgarr does not download from Usenet itself: Sonarr hands the NZB to your Usenet downloader (SABnzbd, NZBGet).</p>
<div class="un-grid">
<label>News server<input id="unHost" value="{html.escape(cfg.get("host") or "", quote=True)}" placeholder="news.example.com"/></label>
<label>Port<input id="unPort" type="number" value="{cfg.get("port") or 563}"/></label>
<label>User name<input id="unUser" value="{html.escape(cfg.get("user") or "", quote=True)}" autocomplete="off"/></label>
<label>Password<input id="unPass" type="password" placeholder="{"kept - leave empty to keep it" if cfg.get("pass") else ""}" autocomplete="new-password"/></label>
<label>Data it may use per day, in MB<input id="unDay" type="number" step="100" min="0" value="{round(cfg.get("per_day", 2000000) * 350 / 1e6)}"/></label>
</div>
<label style="display:flex;gap:8px;align-items:center;margin:6px 0"><input type="checkbox" id="unSsl" {"checked" if cfg.get("ssl", True) else ""}/> use TLS (port 563)</label>
<div style="height:8px"></div>
<button class="btn" onclick="unSave()">Save and test</button> <span id="unMsg" style="color:var(--muted);margin-left:10px"></span></div>
<h2 class="section">In Sonarr and Radarr</h2>
<div class="un-box"><p style="margin:0 0 8px">Add a second indexer (Newznab), beside the existing "tgarr" one, and give it your Usenet downloader:</p>
<p style="margin:0;line-height:1.9">URL <code>{html.escape(base)}/newznab/usenet</code> · API path <code>/api</code> · API key: the same as for "tgarr"<br/>
Download client: <b>SABnzbd</b> (or NZBGet) - not "tgarr". An indexer goes to one download client, which is why Usenet needs its own entry.</p></div>
<script>
function unToggle(el){{
  // the switch works at once, without touching the account fields
  fetch("/api/usenet/enabled", {{method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{on: el.checked}})}}).then(r => r.json()).then(() => location.reload())
    .catch(() => {{ el.checked = !el.checked; }});
}}
function unSave(){{
  const m = document.getElementById("unMsg"); m.textContent = "testing…";
  fetch("/api/usenet/settings", {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{
    enabled: document.getElementById("unOn").checked, host: document.getElementById("unHost").value,
    port: +document.getElementById("unPort").value, ssl: document.getElementById("unSsl").checked,
    user: document.getElementById("unUser").value, password: document.getElementById("unPass").value,
    per_day: Math.round(+document.getElementById("unDay").value * 1e6 / 350)}})}}).then(r => r.json())
    .then(j => {{ m.textContent = j.working ? "saved - the server answers" : "saved - " + (j.message || "not working"); setTimeout(() => location.reload(), 1200); }})
    .catch(() => {{ m.textContent = "could not save"; }});
}}
</script>"""
    return HTMLResponse(_layout("Usenet", "/settings", body, page_title="Usenet", top_actions_html=""))


# ════════════════════════════════════════════════════════════════════
# Kids' pages, the PIN, the kids' list
# ════════════════════════════════════════════════════════════════════
_UNLOCK_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>tgarr - PIN</title>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f1f5f9;color:#0f172a;
  display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px;box-sizing:border-box}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;max-width:360px;width:100%;padding:28px;text-align:center;
  box-shadow:0 10px 30px rgba(15,23,42,.08)}
h1{margin:0 0 6px;font-size:22px}p{color:#64748b;font-size:14px;line-height:1.5;margin:6px 0 18px}
input[type=password]{width:100%;box-sizing:border-box;padding:14px;font-size:26px;letter-spacing:8px;text-align:center;
  border:1px solid #cbd5e1;border-radius:10px}
input[type=password]:focus{outline:2px solid #0ea5e9;border-color:#0ea5e9}
label{display:flex;gap:8px;align-items:center;justify-content:center;font-size:13px;color:#475569;margin:14px 0 4px}
button{margin-top:12px;width:100%;padding:12px;font:inherit;font-size:15px;font-weight:600;color:#fff;background:#0ea5e9;
  border:0;border-radius:10px;cursor:pointer}button:disabled{opacity:.6}
#err{color:#b91c1c;font-size:14px;min-height:20px;margin-top:10px}a{color:#0284c7;font-size:13px}
</style></head><body><div class="card">
<h1>Enter the PIN</h1><p>Enter the PIN to open all of tgarr.</p>
<form id="f" autocomplete="off">
<input type="password" id="pin" inputmode="numeric" pattern="[0-9]*" maxlength="12" autofocus aria-label="PIN"/>
<label><input type="checkbox" id="rem"/> keep this browser unlocked for 30 days</label>
<button id="b" type="submit">Unlock</button><div id="err"></div></form>
<a href="/mediacenter">&larr; back to the Media Center</a></div>
<script>
document.getElementById("f").addEventListener("submit", async function (e) {
  e.preventDefault();
  const b = document.getElementById("b"), err = document.getElementById("err");
  b.disabled = true; err.textContent = "";
  try {
    const r = await fetch("/api/unlock", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({pin: document.getElementById("pin").value, remember: document.getElementById("rem").checked})});
    const j = await r.json();
    if (j.status === "ok") { location.href = "/"; return; }
    err.textContent = j.message || "That is not the PIN.";
  } catch (x) { err.textContent = "Could not reach tgarr."; }
  document.getElementById("pin").value = ""; b.disabled = false; document.getElementById("pin").focus();
});
</script></body></html>"""


@app.get("/unlock", response_class=HTMLResponse)
async def page_unlock(request: Request):
    if not await _is_locked(request):
        return RedirectResponse("/")
    return HTMLResponse(_UNLOCK_HTML)


def _unlock_cookie(resp, secret: str, remember: bool):
    seconds = access.REMEMBER_SECONDS if remember else access.UNLOCK_SECONDS
    resp.set_cookie(access.COOKIE, access.make_token(secret, seconds), max_age=seconds,
                    httponly=True, samesite="lax", path="/")
    return resp


@app.post("/api/unlock")
async def api_unlock(request: Request):
    cfg = await _access_cfg(fresh=True)
    if not cfg["pin"]:
        return {"status": "ok"}
    who = request.client.host if request.client else "?"
    wait = _GUESSES.wait(who)
    if wait:
        return JSONResponse({"status": "wait", "message": f"Too many wrong tries. Try again in {max(1, wait // 60)} min."},
                            status_code=429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not access.check_pin(str(body.get("pin") or ""), cfg["pin"]):
        earned = _GUESSES.wrong(who)
        return JSONResponse({"status": "wrong", "message": "That is not the PIN." + (
            f" Too many wrong tries - wait {earned // 60} min." if earned else "")}, status_code=403)
    _GUESSES.right(who)
    return _unlock_cookie(JSONResponse({"status": "ok"}), cfg["secret"], bool(body.get("remember")))


@app.get("/lock")
async def page_lock():
    """Lock this browser again: forget that the PIN was entered here, and go
    to the Media Center. A plain address, so that it can be a link anywhere."""
    resp = RedirectResponse("/mediacenter", status_code=303)
    resp.delete_cookie(access.COOKIE, path="/")
    return resp


@app.post("/api/lock")
async def api_lock():
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(access.COOKIE, path="/")
    return resp


_MC_PING_JS = """(function(){
  // tells the server "still here" while the page is in front or something is playing;
  // the server counts the time and says when it is time to stop
  function busy(){
    if (document.visibilityState === "visible") return true;
    var m = document.querySelectorAll("video,audio");
    for (var i = 0; i < m.length; i++) if (!m[i].paused && !m[i].ended) return true;
    return false;
  }
  function ping(){
    if (!busy()) return;
    fetch("/api/mc/ping", {credentials: "same-origin"}).then(function(r){ return r.json(); })
      .then(function(j){ if (j && j.ok === false && location.pathname !== "/mediacenter/rest") location.href = "/mediacenter/rest"; })
      .catch(function(){});
  }
  ping(); setInterval(ping, __EVERY__000); document.addEventListener("visibilitychange", ping);
})();""".replace("__EVERY__", str(screentime.PING_SECONDS))


@app.get("/mc/ping.js")
async def mc_ping_js():
    return Response(_MC_PING_JS, media_type="application/javascript", headers={"Cache-Control": "public, max-age=300"})


@app.get("/api/mc/ping")
async def api_mc_ping(request: Request):
    """"Still here" from a page. A browser that has entered the PIN (or an
    install with no PIN) is not counted and never told to stop."""
    if not await _is_locked(request):
        return {"ok": True, "free": True}
    rules = await _mc_rules()
    who, now = _mc_who(request), time.time()
    await _mc_device(who, rules)
    v = _DEVICES.ping(who, rules, now)
    d = _DEVICES.by[who]
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""INSERT INTO mc_usage (day, who, seconds) VALUES ($1, $2, $3)
                                  ON CONFLICT (day, who) DO UPDATE SET seconds = GREATEST(mc_usage.seconds, $3)""",
                               d["day"], who, d["used"])
    except Exception:
        pass
    return v


_MC_REST_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/><title>tgarr Media Center</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;box-sizing:border-box;
  background:#fff8ef;color:#2b2140;font-family:ui-rounded,"SF Pro Rounded","Nunito","Segoe UI","PingFang SC",system-ui,sans-serif;text-align:center}
@media (prefers-color-scheme:dark){body{background:#191624;color:#f3eefc}}
.big{font-size:96px;line-height:1}h1{font-size:34px;margin:18px 0 8px}p{font-size:19px;opacity:.75;margin:6px 0}
a{display:inline-block;margin-top:34px;font-size:13px;color:inherit;opacity:.55}
</style></head><body><div><div class="big">__ICON__</div><h1>__TITLE__</h1><p>__TEXT__</p>
<a href="/unlock">🔒 Enter PIN</a></div>
<script>
// look again every half minute: when it is time, go back in
setInterval(function(){ fetch("/api/mc/ping").then(function(r){ return r.json(); })
  .then(function(j){ if (j && j.ok) location.href = "/mediacenter"; }).catch(function(){}); }, 30000);
</script></body></html>"""


@app.get("/mediacenter/rest", response_class=HTMLResponse)
async def page_mc_rest(request: Request):
    rules = await _mc_rules()
    who = _mc_who(request)
    await _mc_device(who, rules)
    v = _DEVICES.check(who, rules, time.time())
    if v["ok"] or not await _is_locked(request):
        return RedirectResponse("/mediacenter", status_code=303)
    icon, title, text = {
        "hours": ("🌙", "The Media Center is closed now", f"It opens again at {v['until']}."),
        "day": ("🌟", "That is all for today", "Well done. See you tomorrow!"),
        "rest": ("🤸", "Time for a break", f"Stretch, drink some water, look far away. Back at {v['until']}."),
    }.get(v["why"], ("🌙", "The Media Center is closed now", ""))
    return HTMLResponse(_MC_REST_HTML.replace("__ICON__", icon).replace("__TITLE__", title).replace("__TEXT__", text))


@app.post("/api/kids/rules")
async def api_kids_rules(request: Request):
    try:
        rules = screentime.clean(await request.json())
    except Exception:
        return JSONResponse({"status": "error", "message": "bad request"}, status_code=400)
    async with db_pool.acquire() as conn:
        await conn.execute("""INSERT INTO config (key, value) VALUES ('mc_rules', $1)
                              ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()""", _json.dumps(rules))
    await _mc_rules(fresh=True)
    return {"status": "ok", "rules": rules}


@app.post("/api/kids/pin")
async def api_kids_pin(request: Request):
    """Set, change or remove the PIN. A locked browser never reaches this (the
    guard refuses it); changing or removing an existing PIN needs the old one."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    new, cur = str(body.get("pin") or ""), str(body.get("current") or "")
    cfg = await _access_cfg(fresh=True)
    if cfg["pin"] and not access.check_pin(cur, cfg["pin"]):
        return JSONResponse({"status": "error", "message": "The current PIN is not right."}, status_code=403)
    async with db_pool.acquire() as conn:
        if not new:
            await conn.execute("DELETE FROM config WHERE key = 'access_pin'")
            await _access_cfg(fresh=True)
            return {"status": "ok", "pin": False}
        if not access.valid_pin(new):
            return JSONResponse({"status": "error", "message": "A PIN is 4 to 12 digits."}, status_code=400)
        secret = cfg["secret"] or access.new_secret()
        for k, v in (("access_pin", access.hash_pin(new)), ("access_secret", secret)):
            await conn.execute(
                """INSERT INTO config (key, value) VALUES ($1, $2)
                   ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""", k, v)
    await _access_cfg(fresh=True)
    # the browser that set the PIN stays unlocked
    return _unlock_cookie(JSONResponse({"status": "ok", "pin": True}), secret, False)


@app.post("/api/kids/allow")
async def api_kids_allow(request: Request):
    try:
        body = await request.json()
        kind, ref, on = str(body.get("kind")), int(body.get("ref")), bool(body.get("on"))
    except Exception:
        return JSONResponse({"status": "error", "message": "bad request"}, status_code=400)
    if kind not in ("channel", "message"):
        return JSONResponse({"status": "error", "message": "bad kind"}, status_code=400)
    async with db_pool.acquire() as conn:
        if on:
            await conn.execute(
                "INSERT INTO kids_allow (kind, ref) VALUES ($1, $2) ON CONFLICT DO NOTHING", kind, ref)
        else:
            await conn.execute("DELETE FROM kids_allow WHERE kind = $1 AND ref = $2", kind, ref)
    _KID_OK.clear()
    return {"status": "ok", "on": on}


_MC_KINDS = {
    # key: (label, icon, colour, SQL)
    "video": ("Watch", "🎬", "#f97066", "m.media_type = 'video'"),
    "audio": ("Listen", "🎧", "#12b5a6", "m.media_type = 'audio'"),
    "book": ("Read", "📖", "#f5a524", r"m.file_name ~* '\.(pdf|epub|mobi|azw3?|djvu|fb2|cbr|cbz)$'"),
    "photo": ("Photos", "🖼️", "#8b6cf6", "m.media_type = 'photo'"),
}
_MC_JUNK_RX = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hdr\d*|dv|x26[45]|h\.?26[45]|hevc|avc|web-?dl|webrip|bluray|blu-ray|bdrip|"
    r"hdtv|dvdrip|remux|aac\d?(\.\d)?|ddp?\d?(\.\d)?|atmos|dts(-hd)?|flac|mp3|10bit|8bit|repack|proper|"
    r"高码率?|高清|超清|蓝光|国语|粤语|中字|双语|内封|简繁|英字)\b", re.I)


def _friendly_name(file_name, caption) -> str:
    """A title a person would say aloud, from a file name: no extension, no
    dots and underscores, no technical tags, no '@channel' advert."""
    t = (file_name or "").strip()
    if not t:
        t = (caption or "").strip().split("\n")[0]
    t = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", t)
    t = re.sub(r"[@#][\w.]+", " ", t)                      # @channel, #tag
    t = re.sub(r"[\[【(（][^\]】)）]{0,40}[\]】)）]", " ", t)    # [group], 【广告】, (2019) goes too - the year is noise here
    t = t.replace("_", " ").replace(".", " ")
    # what streaming sites glue onto a title: "…-免费在线观看-酷播", "… 高清免费在线观看 XX影视网"
    t = re.sub(r"[\s\-–—|]*(?:高清|超清)?(?:免费)?在线观看.*$", " ", t)
    t = re.sub(r"\S*(?:影视网|电影网|资源网|影院|酷播)\S*", " ", t)
    t = re.sub(r"(?:HD)?(?:中字|国语|粤语|双语)?(?:高清|超清|正片)+", " ", t, flags=re.I)
    t = _MC_JUNK_RX.sub(" ", t)
    t = re.sub(r"\s*-\s*[A-Za-z0-9]{2,12}\s*$", " ", t)    # -GROUP at the end
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—_·|")
    return (t or (file_name or "untitled"))[:80]


_MEDIACENTER_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<title>tgarr Media Center</title>
<link rel="icon" href="/favicon.svg"/>
<style>
:root{--bg:#fff8ef;--card:#ffffff;--ink:#2b2140;--soft:#7a7191;--line:#f0e4d3;--accent:__COLOR__;
  --shadow:0 6px 18px rgba(64,40,10,.10);--r:22px}
@media (prefers-color-scheme:dark){:root{--bg:#191624;--card:#262234;--ink:#f3eefc;--soft:#a79fbd;--line:#342f45;
  --shadow:0 6px 18px rgba(0,0,0,.35)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--ink);
  font-family:ui-rounded,"SF Pro Rounded","Nunito","Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif}
a{color:inherit;text-decoration:none}
.top{display:flex;align-items:center;gap:14px;padding:18px clamp(16px,4vw,44px) 6px}
.logo{display:flex;align-items:center;gap:10px;flex:1;min-width:0}
/* tgarr's own mark and wordmark, as in the side menu of the main pages */
.logo .mark{width:34px;height:34px;flex:none;color:#229ED9}
.logo .wm{font-size:34px;font-weight:900;letter-spacing:-1.8px;line-height:.9;color:#229ED9}
.logo .wm b{font-weight:900;color:var(--ink)}
.logo .mc{font-size:21px;font-weight:700;color:var(--soft);padding-left:12px;margin-left:4px;border-left:2px solid var(--line);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pill{font-size:13px;color:var(--soft);padding:9px 14px;border-radius:999px;border:1.5px solid var(--line);
  background:var(--card);white-space:nowrap}
.pill:hover{border-color:var(--accent);color:var(--ink)}
.cats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:clamp(10px,2vw,20px);
  padding:14px clamp(16px,4vw,44px) 4px}
.cat{background:var(--card);border-radius:var(--r);padding:clamp(14px,2.4vw,24px) 10px;text-align:center;
  box-shadow:var(--shadow);border:3px solid transparent;transition:transform .15s, border-color .15s}
.cat:hover{transform:translateY(-3px)}
.cat .ic{font-size:clamp(34px,6vw,56px);line-height:1.1;display:block}
.cat b{display:block;font-size:clamp(15px,2.2vw,21px);margin-top:6px}
.cat small{color:var(--soft);font-size:13px}
.cat.on{border-color:var(--c);background:color-mix(in srgb, var(--c) 12%, var(--card))}
.find{padding:16px clamp(16px,4vw,44px) 0;display:flex;gap:10px}
.find input{flex:1;max-width:520px;font:inherit;font-size:18px;padding:13px 20px;border-radius:999px;
  border:2px solid var(--line);background:var(--card);color:var(--ink);outline:none}
.find input:focus{border-color:var(--accent)}
.find button{font:inherit;font-size:17px;font-weight:700;padding:0 24px;border-radius:999px;border:0;
  background:var(--accent);color:#fff;cursor:pointer}
h2{font-size:21px;margin:26px clamp(16px,4vw,44px) 12px;display:flex;align-items:center;gap:8px}
.row{display:flex;gap:14px;overflow-x:auto;padding:2px clamp(16px,4vw,44px) 10px;scrollbar-width:none}
.row::-webkit-scrollbar{display:none}
.row .card{flex:0 0 clamp(170px,22vw,240px)}
.grid{display:grid;gap:clamp(12px,2vw,22px);padding:0 clamp(16px,4vw,44px);
  grid-template-columns:repeat(auto-fill,minmax(clamp(150px,20vw,230px),1fr))}
.grid.book{grid-template-columns:repeat(auto-fill,minmax(clamp(120px,15vw,170px),1fr))}
.grid.photo{grid-template-columns:repeat(auto-fill,minmax(clamp(110px,14vw,170px),1fr));gap:8px}
.card{background:var(--card);border-radius:var(--r);overflow:hidden;box-shadow:var(--shadow);display:flex;
  flex-direction:column;transition:transform .15s, box-shadow .15s;position:relative}
.card:hover,.card:focus-visible{transform:translateY(-4px) scale(1.015);box-shadow:0 12px 26px rgba(64,40,10,.18);outline:none}
.pic{aspect-ratio:16/10;background:linear-gradient(135deg,color-mix(in srgb,var(--c) 55%,#fff),var(--c));
  display:flex;align-items:center;justify-content:center;font-size:54px;position:relative;overflow:hidden}
.pic img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.book .pic{aspect-ratio:3/4}.photo .pic{aspect-ratio:1/1}.audio .pic{aspect-ratio:1/1}
.play{position:absolute;right:10px;bottom:10px;width:44px;height:44px;border-radius:50%;background:rgba(255,255,255,.94);
  display:flex;align-items:center;justify-content:center;font-size:20px;color:var(--c);box-shadow:0 3px 10px rgba(0,0,0,.25)}
.saved{position:absolute;left:10px;top:10px;background:rgba(255,255,255,.94);color:#157f3d;font-size:11.5px;font-weight:700;
  padding:3px 9px;border-radius:999px}
.name{padding:11px 14px 14px;font-size:15.5px;font-weight:700;line-height:1.3;word-break:break-word}
.name span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.photo .name{display:none}
.empty{margin:30px clamp(16px,4vw,44px);padding:46px 20px;text-align:center;background:var(--card);border-radius:var(--r);
  color:var(--soft);font-size:17px;box-shadow:var(--shadow)}
.empty i{display:block;font-size:54px;font-style:normal;margin-bottom:10px}
.more{display:flex;gap:12px;justify-content:center;padding:26px 16px 50px}
.more a{font-size:17px;font-weight:700;padding:13px 28px;border-radius:999px;background:var(--card);box-shadow:var(--shadow)}
.more a.go{background:var(--accent);color:#fff}
.lb{position:fixed;inset:0;background:rgba(10,8,18,.93);display:none;align-items:center;justify-content:center;z-index:9}
.lb.on{display:flex}.lb img{max-width:96vw;max-height:92vh;border-radius:12px}
.lb button{position:fixed;top:14px;right:16px;width:50px;height:50px;border-radius:50%;border:0;font-size:24px;cursor:pointer}
.lb .nv{top:50%;transform:translateY(-50%)}.lb .pv{left:14px;right:auto}.lb .nx{right:14px}
@media (max-width:560px){.cats{grid-template-columns:repeat(4,1fr);gap:8px}.cat small{display:none}.logo .wm{font-size:27px}.logo .mark{width:27px;height:27px}.logo .mc{font-size:16px;padding-left:9px}}
</style></head>
<body>
<div class="top">
  <a class="logo" href="/mediacenter" aria-label="tgarr Media Center">
    <svg class="mark" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
    <span class="wm">tg<b>arr</b></span><span class="mc">Media Center</span></a>
  __TOPRIGHT__
</div>
<nav class="cats">__CATS__</nav>
<form class="find" method="GET" action="/mediacenter">
  <input type="hidden" name="kind" value="__KIND__"/>
  <input type="search" name="q" value="__Q__" placeholder="What are you looking for?" aria-label="search"/>
  <button type="submit">Find</button>
</form>
<div id="recent"></div>
<h2>__HEADING__</h2>
__GRID__
__MORE__
<div class="lb" id="lb"><button id="lbX" aria-label="close">✕</button>
  <button class="nv pv" id="lbP" aria-label="previous">‹</button><img id="lbI" alt=""/>
  <button class="nv nx" id="lbN" aria-label="next">›</button></div>
<script>
(function(){
  var KEY = "tgarr.mc.recent";
  function load(){ try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return []; } }
  // remember what was opened in this browser, to offer it again
  document.querySelectorAll("a.card[data-id]").forEach(function(a){
    a.addEventListener("click", function(){
      if (a.dataset.kind === "photo") return;
      var r = load().filter(function(x){ return x.id !== a.dataset.id; });
      r.unshift({id: a.dataset.id, kind: a.dataset.kind, name: a.dataset.name, href: a.getAttribute("href"),
                 pic: a.dataset.pic || "", c: a.dataset.c, icon: a.dataset.icon});
      try { localStorage.setItem(KEY, JSON.stringify(r.slice(0, 12))); } catch (e) {}
    });
  });
  var rec = load();
  if (rec.length && !"__Q__" && !__OFFSET__) {
    var h = '<h2>⭐ Carry on</h2><div class="row">';
    rec.forEach(function(x){
      var name = document.createElement("div"); name.textContent = x.name;
      h += '<a class="card ' + x.kind + '" style="--c:' + x.c + '" href="' + x.href + '"><div class="pic">' + (x.icon || "") +
           (x.pic ? '<img loading="lazy" src="' + x.pic + '" alt="" onerror="this.remove()"/>' : "") +
           '<span class="play">▶</span></div><div class="name"><span>' + name.innerHTML + '</span></div></a>';
    });
    document.getElementById("recent").innerHTML = h + "</div>";
  }
  // photos open large, with next and previous
  var ph = Array.prototype.slice.call(document.querySelectorAll("a.card.photo")), at = 0;
  var lb = document.getElementById("lb"), im = document.getElementById("lbI");
  function show(i){ at = (i + ph.length) % ph.length; im.src = ph[at].getAttribute("href"); lb.classList.add("on"); }
  ph.forEach(function(a, i){ a.addEventListener("click", function(e){ e.preventDefault(); show(i); }); });
  document.getElementById("lbX").onclick = function(){ lb.classList.remove("on"); im.src = ""; };
  document.getElementById("lbP").onclick = function(){ show(at - 1); };
  document.getElementById("lbN").onclick = function(){ show(at + 1); };
  lb.addEventListener("click", function(e){ if (e.target === lb) document.getElementById("lbX").onclick(); });
  document.addEventListener("keydown", function(e){
    if (!lb.classList.contains("on")) return;
    if (e.key === "Escape") document.getElementById("lbX").onclick();
    else if (e.key === "ArrowRight") show(at + 1); else if (e.key === "ArrowLeft") show(at - 1);
  });
})();
</script>
<script src="/mc/ping.js" defer></script>
</body></html>"""


@app.get("/kids")
async def page_kids_old():
    return RedirectResponse("/mediacenter", status_code=308)


@app.get("/mediacenter", response_class=HTMLResponse)
async def page_mediacenter(request: Request, kind: str = "video", q: Optional[str] = None, offset: int = 0):
    """What a locked browser gets: the items on the Media Center list, as a
    page of its own - large pictures, four big choices, nothing to configure."""
    kind = kind if kind in _MC_KINDS else "video"
    offset, per = max(0, int(offset)), 48
    label, icon, colour, cond = _MC_KINDS[kind]
    where, params = [_KID_SQL, cond], []
    for w in (q or "").split()[:6]:
        params.append(f"%{w}%")
        where.append(f"COALESCE(m.file_name, m.caption, '') ILIKE ${len(params)}")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT m.id, m.file_name, m.caption,
                   (m.local_path IS NOT NULL AND left(m.local_path, 2) <> '__') AS saved
            FROM messages m JOIN channels c ON c.id = m.channel_id
            WHERE {' AND '.join(where)}
            ORDER BY saved DESC, m.posted_at DESC NULLS LAST LIMIT {per + 1} OFFSET {offset}""", *params)
        counts = {k: await conn.fetchval(f"""
            SELECT count(*) FROM messages m JOIN channels c ON c.id = m.channel_id
            WHERE {_KID_SQL} AND {v[3]}""") for k, v in _MC_KINDS.items()}
    more, rows = len(rows) > per, rows[:per]
    locked = _LOCKED.get()
    cats = "".join(
        f'<a class="cat{" on" if k == kind else ""}" style="--c:{v[2]}" href="/mediacenter?kind={k}">'
        f'<span class="ic">{v[1]}</span><b>{v[0]}</b><small>{counts[k]:,}</small></a>'
        for k, v in _MC_KINDS.items())
    cards = []
    for r in rows:
        name = _friendly_name(r["file_name"], r["caption"])
        if kind == "book":
            pic, href, ic = f"/ebook-cover/{r['id']}", f"/watch/{r['id']}", "📖"
        elif kind == "audio":
            pic, href, ic = "", f"/watch/{r['id']}", "🎵"
        elif kind == "photo":
            pic, href, ic = f"/api/thumb/{r['id']}", f"/api/photo-preview/{r['id']}", ""
        else:
            pic, href, ic = f"/api/thumb/{r['id']}", f"/watch/{r['id']}", "🎬"
        img = f'<img loading="lazy" src="{pic}" alt="" onerror="this.remove()"/>' if pic else ""
        cards.append(
            f'<a class="card {kind}" style="--c:{colour}" href="{href}" data-id="{r["id"]}" data-kind="{kind}" '
            f'data-name="{html.escape(name, quote=True)}" data-pic="{pic}" data-c="{colour}" data-icon="{ic}">'
            f'<div class="pic">{ic}{img}'
            + ('<span class="saved">ready</span>' if r["saved"] and kind != "photo" else "")
            + ('' if kind == "photo" else '<span class="play">▶</span>')
            + f'</div><div class="name"><span>{html.escape(name)}</span></div></a>')
    if cards:
        grid = f'<div class="grid {kind}">{"".join(cards)}</div>'
    elif q:
        grid = '<div class="empty"><i>🔎</i>Nothing found. Try another word?</div>'
    else:
        grid = ('<div class="empty"><i>🌱</i>Nothing here yet.<br/>Things show up here once they are added to the Media Center.</div>')
    qs = f"kind={kind}" + (f"&q={_q(q)}" if q else "")
    nav = ""
    if offset or more:
        nav = ('<div class="more">'
               + (f'<a href="/mediacenter?{qs}&offset={max(0, offset - per)}">← Back</a>' if offset else "")
               + (f'<a class="go" href="/mediacenter?{qs}&offset={offset + per}">More →</a>' if more else "")
               + "</div>")
    top_right = ('<a class="pill" href="/unlock" title="open all of tgarr">🔒 Enter PIN</a>' if locked else
             ('<a class="pill" href="/lock" title="lock this browser again">🔒 Lock</a>' if _ACCESS["pin"] else '')
             + '<a class="pill" href="/mediacenter/manage">⚙ Choose what is shown</a>'
             '<a class="pill" href="/">← Back to tgarr</a>')
    heading = f'{icon} {html.escape(label)}' + (f' · “{html.escape(q)}”' if q else "")
    page = (_MEDIACENTER_HTML.replace("__COLOR__", colour).replace("__TOPRIGHT__", top_right).replace("__CATS__", cats)
            .replace("__KIND__", kind).replace("__Q__", html.escape(q or "", quote=True))
            .replace("__OFFSET__", str(offset)).replace("__HEADING__", heading)
            .replace("__GRID__", grid).replace("__MORE__", nav))
    return HTMLResponse(page)


_KIDS_CSS = """<style>
.km-row{display:flex;align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid var(--border)}
.km-row:last-child{border-bottom:none}.km-row span.n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.km-box{background:var(--surface);border:1px solid var(--border);border-radius:12px;margin-bottom:22px;overflow:hidden}
.km-pad{padding:16px 18px}.km-row small{color:var(--muted)}
.km-chip.on{background:var(--accent);color:#fff;border-color:var(--accent)}
/* channels and single files side by side where there is room */
.km-two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:0 26px;align-items:start}
.km-two > div{min-width:0}
.km-list{height:min(62vh,640px);overflow:auto}
.km-two h2.section{margin-top:8px}
/* the note under each heading takes two lines at most, so the two lists start level */
.km-two h2.section + p{min-height:2.9em}
@media (max-width:1100px){.km-two{grid-template-columns:minmax(0,1fr)}.km-two h2.section + p{min-height:0}.km-list{height:auto;max-height:520px}}
.km-in{padding:9px 12px;border:1px solid var(--border);border-radius:8px;font:inherit;background:var(--surface);color:var(--fg)}
</style>"""


@app.get("/mediacenter/manage", response_class=HTMLResponse)
async def page_kids_manage():
    cfg = await _access_cfg(fresh=True)
    rules = await _mc_rules(fresh=True)
    today = screentime.local(time.time(), rules.get("tz", 0))[0]
    async with db_pool.acquire() as conn:
        usage = await conn.fetch(
            """SELECT who, sum(seconds) FILTER (WHERE day = $1) AS today, sum(seconds) AS week
               FROM mc_usage WHERE day > $1 - 7 GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 12""", today)
        opened = await conn.fetch(
            """SELECT l.at, l.who, m.file_name, m.caption FROM mc_log l JOIN messages m ON m.id = l.msg_id
               ORDER BY l.at DESC LIMIT 30""")
        chans = await conn.fetch("""
            SELECT c.id, c.title, c.username, COALESCE(c.audience, 'sfw') AS audience,
                   (SELECT count(*) FROM messages m WHERE m.channel_id = c.id) AS n,
                   EXISTS (SELECT 1 FROM kids_allow k WHERE k.kind = 'channel' AND k.ref = c.id) AS allowed
            FROM channels c
            WHERE EXISTS (SELECT 1 FROM messages m WHERE m.channel_id = c.id)
            ORDER BY allowed DESC, n DESC LIMIT 400""")
        # The files already saved on this machine (opened in a player, or
        # grabbed), newest use first - plus anything ticked earlier that has
        # since been cleared from disk, so that it can still be unticked.
        items = await conn.fetch("""
            SELECT m.id, m.file_name, m.media_type, m.file_size, c.title,
                   (m.local_path IS NOT NULL AND left(m.local_path, 2) <> '__') AS saved,
                   EXISTS (SELECT 1 FROM kids_allow k WHERE k.kind = 'message' AND k.ref = m.id) AS allowed,
                   EXISTS (SELECT 1 FROM kids_allow k WHERE k.kind = 'channel' AND k.ref = m.channel_id) AS by_channel
            FROM messages m JOIN channels c ON c.id = m.channel_id
            WHERE COALESCE(c.audience, 'sfw') = 'sfw'
              AND ((m.local_path IS NOT NULL AND left(m.local_path, 2) <> '__'
                    AND split_part(m.local_path, '/', 1) IN ('video', 'audio', 'library'))
                   OR m.id IN (SELECT ref FROM kids_allow WHERE kind = 'message'))
            ORDER BY COALESCE(m.local_accessed_at, m.indexed_at) DESC NULLS LAST, m.id DESC
            LIMIT 2000""")

    def box(kind, ref, on, dis=""):
        return (f'<input type="checkbox" {"checked" if on else ""} {dis} '
                f'onchange="kmAllow(\'{kind}\',{ref},this)"/>')
    pin_box = (
        '<div class="km-box"><div class="km-pad"><h3 style="margin:0 0 6px">PIN</h3>'
        + ('<p style="color:var(--muted);margin:0 0 12px">A PIN is set. Any browser that has not entered it sees only '
           'the Media Center with what is ticked below. This browser is unlocked.</p>' if cfg["pin"] else
           '<p style="color:var(--muted);margin:0 0 12px"><b style="color:#b45309">No PIN is set:</b> anybody who opens '
           'this address sees everything you see. Set a PIN and other browsers get only the Media Center.</p>')
        + '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        + ('<input class="km-in" id="kmCur" type="password" inputmode="numeric" maxlength="12" placeholder="current PIN" style="width:140px"/>'
           if cfg["pin"] else "")
        + '<input class="km-in" id="kmNew" type="password" inputmode="numeric" maxlength="12" '
          f'placeholder="{"new PIN" if cfg["pin"] else "PIN, 4 to 12 digits"}" style="width:170px"/>'
        + f'<button class="btn" onclick="kmPin(false)">{"Change PIN" if cfg["pin"] else "Set PIN"}</button>'
        + ('<button class="btn ghost" onclick="kmPin(true)">Remove PIN</button>'
           '<button class="btn ghost" onclick="kmLock()">Lock this browser now</button>' if cfg["pin"] else "")
        + '<span id="kmMsg" style="color:var(--muted);font-size:13px"></span></div></div></div>')
    chan_rows = "".join(
        f'<label class="km-row">{box("channel", c["id"], c["allowed"], "disabled" if c["audience"] != "sfw" else "")}'
        f'<span class="n">{html.escape(c["title"] or c["username"] or "chat")}'
        + (f' <small>@{html.escape(c["username"])}</small>' if c["username"] else ' <small>🔒 private</small>')
        + (' <small>· 18+, never shown there</small>' if c["audience"] != "sfw" else "")
        + f'</span><small>{c["n"]:,} items</small></label>' for c in chans)
    ico = {"video": "🎬", "audio": "🎵", "photo": "🖼"}

    def kind_of(r):
        if r["media_type"] in ("video", "audio"):
            return r["media_type"]
        return "book"
    item_rows = "".join(
        f'<label class="km-row" data-k="{kind_of(r)}">'
        + (box("message", r["id"], True, "disabled") if r["by_channel"] and not r["allowed"]
           else box("message", r["id"], r["allowed"]))
        + f'<span class="n">{ico.get(r["media_type"], "📄")} '
          f'{html.escape(r["file_name"] or "item " + str(r["id"]))} <small>{html.escape(r["title"] or "")}</small>'
        + (' <small>· shown already: its channel is ticked</small>' if r["by_channel"] and not r["allowed"] else "")
        + ("" if r["saved"] else ' <small>· no longer saved here</small>')
        + f'</span><small>{_fmt_size(r["file_size"]) or ""}</small></label>' for r in items)
    n_kind = {k: sum(1 for r in items if kind_of(r) == k) for k in ("video", "audio", "book")}
    chips = "".join(
        f'<button type="button" class="btn ghost km-chip{" on" if k == "all" else ""}" data-k="{k}" onclick="kmShow(this)">{lbl} '
        f'<span style="opacity:.6">{n:,}</span></button>'
        for k, lbl, n in (("all", "All", len(items)), ("video", "🎬 Videos", n_kind["video"]),
                          ("audio", "🎵 Music", n_kind["audio"]), ("book", "📄 Books", n_kind["book"])))
    def mins(x):
        x = int(x or 0)
        return f"{x // 3600} h {x % 3600 // 60:02d} min" if x >= 3600 else f"{x // 60} min"
    time_box = (
        '<div class="km-box"><div class="km-pad">'
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:6px"><h3 style="margin:0;flex:1">Time</h3>'
        f'<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="mcOn" {"checked" if rules["on"] else ""}/> <b>use time rules</b></label></div>'
        '<p style="color:var(--muted);margin:0 0 12px">For browsers that have not entered the PIN. Counted per device, '
        'only while a page is in front or something is playing.</p>'
        '<div style="display:grid;grid-template-columns:auto repeat(3,minmax(90px,150px));gap:8px 12px;align-items:center;max-width:620px">'
        '<span></span><small>open from</small><small>until</small><small>minutes a day (0 = no limit)</small>'
        + "".join(
            f'<b>{lbl}</b><input class="km-in" type="time" id="mc_{k}_from" value="{rules[k]["from"]}"/>'
            f'<input class="km-in" type="time" id="mc_{k}_to" value="{rules[k]["to"]}"/>'
            f'<input class="km-in" type="number" min="0" max="1440" id="mc_{k}_limit" value="{rules[k]["limit"]}"/>'
            for k, lbl in (("wd", "Monday to Friday"), ("we", "Saturday, Sunday")))
        + '</div><div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px">'
        f'<span>A break of <input class="km-in" type="number" min="0" max="240" id="mcRest" value="{rules["rest"]}" style="width:74px"/> minutes '
        f'after <input class="km-in" type="number" min="0" max="600" id="mcSession" value="{rules["session"]}" style="width:74px"/> minutes in one go</span>'
        '<button class="btn" onclick="mcSave()">Save</button><span id="mcMsg" style="color:var(--muted);font-size:13px"></span></div>'
        '</div></div>')
    used_box = ""
    if usage or opened:
        used_box = (
            '<h2 class="section">Use</h2><p style="color:var(--muted);margin:-6px 0 12px">Kept on this machine only: time per device, '
            'and what was opened in the Media Center.</p><div class="km-box">'
            + "".join(f'<div class="km-row"><span class="n">📱 {html.escape(u["who"])}</span>'
                      f'<small>today {mins(u["today"])} · last 7 days {mins(u["week"])}</small></div>' for u in usage)
            + "".join(f'<div class="km-row"><span class="n">{html.escape(_friendly_name(o["file_name"], o["caption"]))}</span>'
                      f'<small>{_fmt_time(o["at"])} · {html.escape(o["who"])}</small></div>' for o in opened)
            + '</div>')
    body = (
        _KIDS_CSS + pin_box + time_box
        + '<div class="km-two"><div>'
        + '<h2 class="section">Whole channels in the Media Center</h2>'
          '<p style="color:var(--muted);margin:-6px 0 12px">Everything in a ticked channel is shown in the Media Center, now and as it grows.</p>'
        + f'<div class="km-box km-list">{chan_rows or "<div class=km-pad>no channels yet</div>"}</div>'
        + '</div><div>'
        + '<h2 class="section">Single files</h2>'
          '<p style="color:var(--muted);margin:-6px 0 12px">The files already saved on this machine. Tick the ones to show in the Media Center.</p>'
        + (f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">{chips}</div>'
           f'<div class="km-box km-list" id="kmItems">{item_rows}</div>' if items else
           '<div class="km-box"><div class="km-pad" style="color:var(--muted)">Nothing is saved yet. Files you open in a player '
           'are kept and appear here.</div></div>')
        + '</div></div>'
        + used_box
        + '<p><a class="btn ghost" href="/mediacenter">Open the Media Center &rarr;</a></p>'
        + """<script>
function kmAllow(kind, ref, el){
  fetch("/api/kids/allow", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({kind: kind, ref: ref, on: el.checked})}).then(r => r.json())
    .then(j => { if (j.status !== "ok") el.checked = !el.checked; }).catch(() => { el.checked = !el.checked; });
}
function kmPin(remove){
  const cur = document.getElementById("kmCur"), nw = document.getElementById("kmNew"), msg = document.getElementById("kmMsg");
  if (remove && !confirm("Remove the PIN? Anybody who opens this address will see everything again.")) return;
  fetch("/api/kids/pin", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({pin: remove ? "" : nw.value, current: cur ? cur.value : ""})}).then(r => r.json())
    .then(j => { if (j.status === "ok") location.reload(); else msg.textContent = j.message || "that did not work"; });
}
function mcSave(){
  const g = id => document.getElementById(id), msg = g("mcMsg");
  const part = k => ({from: g("mc_" + k + "_from").value, to: g("mc_" + k + "_to").value, limit: +g("mc_" + k + "_limit").value});
  fetch("/api/kids/rules", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
    on: g("mcOn").checked, wd: part("wd"), we: part("we"), session: +g("mcSession").value, rest: +g("mcRest").value,
    tz: -new Date().getTimezoneOffset()})}).then(r => r.json())
    .then(j => { msg.textContent = j.status === "ok" ? "saved" : (j.message || "that did not work"); });
}
function kmShow(b){
  document.querySelectorAll(".km-chip").forEach(x => x.classList.toggle("on", x === b));
  document.querySelectorAll("#kmItems .km-row").forEach(r => { r.style.display = (b.dataset.k === "all" || r.dataset.k === b.dataset.k) ? "" : "none"; });
}
function kmLock(){ fetch("/api/lock", {method: "POST"}).then(() => { location.href = "/mediacenter"; }); }
</script>""")
    return HTMLResponse(_layout("Media Center", "/settings", body, page_title="Media Center & PIN", top_actions_html=""))


# A private chat: no public @name and small (see the crawler's
# PRIVATE_CHAT_SQL). A group of thousands that merely has no public name is
# not "my private chats".
_PRIVATE_CHAT = "(c.username IS NULL AND COALESCE(c.members_count, 0) < 200)"


@app.get("/gallery", response_class=HTMLResponse)
async def page_gallery(channel: Optional[str] = None, limit: int = 240,
                       q: Optional[str] = None, dc: Optional[str] = None,
                       ch: Optional[int] = None, private: int = 0,
                       offset: int = 0, sort: str = "new"):
    if not login.session_exists():
        # Skip redirect if we have data — crawler may be authed in-memory
        async with db_pool.acquire() as conn:
            n = await conn.fetchval("SELECT count(*) FROM channels")
        if not n:
            return RedirectResponse("/login")

    where = ["(m.media_type = 'photo'"
             " OR (m.media_type IS NULL AND m.file_name IS NULL"
             "     AND COALESCE(m.mime_type,'') = ''"
             "     AND m.file_unique_id IS NOT NULL))",
             "COALESCE(m.thumb_path, '') <> '__failed__'",
             "COALESCE(m.thumb_path, '') <> '__deleted__'"]
    params = []
    if channel:
        params.append(channel)
        where.append(f"c.username = ${len(params)}")
    if ch:
        # any chat, by its own id: a private chat has no @username to go by
        params.append(int(ch))
        where.append(f"c.id = ${len(params)}")
    _in = _signed_in()
    if not _in:
        private = 0                       # no account, no private chats
        where.append("c.username IS NOT NULL")
    if private:
        where.append(_PRIVATE_CHAT)
    if q:
        for w in q.split():
            params.append(f"%{w}%")
            where.append(f"(m.caption ILIKE ${len(params)} OR m.file_name ILIKE ${len(params)})")
    # Gate NSFW unless user has explicitly opted in via /settings.
    _nsfw_on = await _is_nsfw_enabled()
    if not _nsfw_on:
        where.append("COALESCE(c.audience, 'sfw') <> 'nsfw'")
    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)
    # CSAM hard-block — overrides every setting, always.
    where.append("COALESCE(c.audience, 'sfw') <> 'blocked_csam'")

    # Every DC by default. Restricting the gallery to the account's own DC used
    # to be the default because each cross-DC image cost a fresh media session
    # and authorization; with cached per-DC sessions that cost is gone, and the
    # restriction hid most of the library (about 9 in 10 photos on a typical
    # install). ?dc=mine is the opt-in "my DC only" filter.
    my_dc = await _get_my_dc()
    same_dc_only = (dc == "mine") and bool(my_dc)
    _dc_q = "&dc=mine" if same_dc_only else ""
    if same_dc_only:
        params.append(my_dc)
        where.append(f"m.file_dc = ${len(params)}")

    async with db_pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT count(*) FROM messages WHERE thumb_path IS NOT NULL "
            "AND thumb_path NOT LIKE '\\_\\_%'")
        unique_total = await conn.fetchval(
            "SELECT count(DISTINCT thumb_md5) FROM messages "
            "WHERE thumb_path IS NOT NULL AND thumb_path NOT LIKE '\\_\\_%' "
            "AND thumb_md5 IS NOT NULL")
        pending = await conn.fetchval(
            """SELECT count(*) FROM messages
               WHERE thumb_path IS NULL AND (media_type='photo' OR
                     (media_type IS NULL AND file_name IS NULL
                      AND COALESCE(mime_type,'')='' AND file_unique_id IS NOT NULL))""")
        # Every chat that has photos - private chats and groups included -
        # for the channel picker. (This used to be the twelve public channels
        # with the most photos, shown as unexplained chips; a chat without a
        # @username could not be chosen at all.)
        all_channels = await conn.fetch(
            f"""SELECT c.id, c.username, c.title, c.is_joined, c.subscribed, count(*) AS n,
                      {_PRIVATE_CHAT} AS private
               FROM messages m JOIN channels c ON c.id = m.channel_id
               WHERE m.media_type = 'photo'
                 AND COALESCE(c.audience, 'sfw') <> 'blocked_csam'
                 {"" if _nsfw_on else "AND COALESCE(c.audience, 'sfw') <> 'nsfw'"}
                 {"" if _in else "AND FALSE"}
               GROUP BY c.id
               ORDER BY count(*) DESC""")
        matched = await conn.fetchval(f"""
            SELECT count(DISTINCT COALESCE(m.thumb_md5, 'id-' || m.id::text))
            FROM messages m JOIN channels c ON c.id = m.channel_id
            WHERE {' AND '.join(where)}""", *params)
        # the number beside "My private chats" is the number of pictures that
        # choice shows: the same pictures counted the same way (one per image,
        # not one per post), not a sum of posts
        priv_n = 0 if not _in else await conn.fetchval(f"""
            SELECT count(DISTINCT COALESCE(m.thumb_md5, 'id-' || m.id::text))
            FROM messages m JOIN channels c ON c.id = m.channel_id
            WHERE {' AND '.join(where[:3])} AND {_PRIVATE_CHAT}
              AND COALESCE(c.audience, 'sfw') <> 'blocked_csam'
              {"" if _nsfw_on else "AND COALESCE(c.audience, 'sfw') <> 'nsfw'"}""")
        # DISTINCT ON thumb_md5 → one row per unique image. Rows without md5
        # (legacy thumbs not yet hashed) fall through with NULL bucket; that
        # group will be deduped to one row, which is OK for now and the hash
        # backfill task fills them in.
        rows = await conn.fetch(f"""
            WITH dup_agg AS (
              SELECT thumb_md5, count(*) AS dup_count
              FROM messages
              WHERE thumb_md5 IS NOT NULL
              GROUP BY thumb_md5
            )
            SELECT * FROM (
              SELECT DISTINCT ON (COALESCE(m.thumb_md5, 'id-' || m.id::text))
                     m.id, m.thumb_path, m.thumb_md5, m.caption, m.posted_at,
                     c.title AS ch_title, c.username AS ch_user,
                     COALESCE(d.dup_count, 0) AS dup_count
              FROM messages m
              JOIN channels c ON c.id = m.channel_id
              LEFT JOIN dup_agg d ON d.thumb_md5 = m.thumb_md5
              WHERE {' AND '.join(where)}
              ORDER BY COALESCE(m.thumb_md5, 'id-' || m.id::text), m.posted_at DESC NULLS LAST
            ) sub
            ORDER BY posted_at {"ASC NULLS LAST" if sort == "old" else "DESC NULLS LAST"}
            LIMIT {page_size} OFFSET {page_offset}
        """, *params)

    # ── which photos: all / my private chats / one chat ──────────────
    def _gqs(**over):
        cur = {"ch": ch, "channel": channel, "private": 1 if private else None, "q": q,
               "dc": "mine" if same_dc_only else None,
               "sort": sort if sort == "old" else None,
               "limit": limit if limit != 240 else None}
        cur.update(over)
        bits = [f"{k}={_q(str(v))}" for k, v in cur.items() if v not in (None, "", 0)]
        return "?" + "&".join(bits) if bits else ""

    picked = None
    for r in all_channels:
        if (ch and r["id"] == ch) or (channel and r["username"] == channel):
            picked = r
            break
    priv = [r for r in all_channels if r["private"]]

    def _name(r):
        return r["title"] or (("@" + r["username"]) if r["username"] else "private chat")
    opts = "".join(
        f'<a class="gp-opt" href="/gallery{_gqs(ch=r["id"], channel=None, private=None, offset=None)}" '
        f'data-k="{html.escape((_name(r) + " " + (r["username"] or "")).lower(), quote=True)}">'
        f'{_avatar(_name(r), 30)}<span class="gp-n">{html.escape(_name(r))}'
        + (f'<small>@{html.escape(r["username"])}</small>' if r["username"]
           else '<small>🔒 private</small>' if r["private"]
           else '<small>group · no public name</small>')
        + f'</span><span class="gp-c">{r["n"]:,}</span></a>'
        for r in all_channels)
    picker = (
        '<div class="gp" id="gp">'
        f'<button type="button" class="btn ghost gp-btn{" on" if picked else ""}" id="gpBtn">'
        + (f'📡 {html.escape(_name(picked)[:28])} <span style="opacity:.6">{picked["n"]:,}</span>'
           if picked else f'📡 Choose a channel… <span style="opacity:.6">{len(all_channels):,}</span>')
        + ' ▾</button>'
        '<div class="gp-pop" id="gpPop">'
        '<input type="text" id="gpQ" placeholder="type to find a channel or chat…" autocomplete="off"/>'
        f'<div class="gp-list" id="gpList">{opts}</div></div></div>')
    ch_chips_html = (
        '<style>'
        '.gp{position:relative}'
        '.gp-btn{padding:5px 12px;font-size:12px}'
        '.gp-btn.on{background:rgba(94,182,229,0.15);color:var(--accent-hi);border-color:var(--accent)}'
        '.gp-pop{display:none;position:absolute;z-index:50;top:calc(100% + 6px);left:0;width:min(380px,86vw);'
        'background:var(--surface);border:1px solid var(--border);border-radius:10px;'
        'box-shadow:0 14px 40px rgba(15,23,42,.18);padding:10px}'
        '.gp.open .gp-pop{display:block}'
        '.gp-pop input{width:100%;box-sizing:border-box;margin-bottom:8px}'
        '.gp-list{max-height:52vh;overflow:auto}'
        '.gp-opt{display:flex;align-items:center;gap:10px;padding:7px 8px;border-radius:8px;'
        'text-decoration:none;color:var(--fg)}'
        '.gp-opt:hover{background:rgba(127,127,127,.10)}'
        '.gp-n{flex:1;min-width:0;font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;'
        'text-overflow:ellipsis}'
        '.gp-n small{display:block;font-weight:400;font-size:11.5px;color:var(--muted)}'
        '.gp-c{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}'
        '</style>'
        '<div class="bar-tools" style="margin-bottom:16px">'
        + _chip_link("All photos", "/gallery" + _gqs(ch=None, channel=None, private=None, offset=None),
                     not (picked or private))
        + (_chip_link(f'🔒 My private chats <span style="opacity:.6">{priv_n:,}</span>',
                      "/gallery" + _gqs(ch=None, channel=None, private=1, offset=None), bool(private),
                      "photos from chats and groups that have no public @username - yours alone")
           if priv else "")
        + picker
        + '<span class="sep"></span><span class="lbl">Sort:</span>'
        + _chip_link("newest", "/gallery" + _gqs(sort=None, offset=None), sort != "old")
        + _chip_link("oldest", "/gallery" + _gqs(sort="old", offset=None), sort == "old")
        + (('<span class="sep"></span>'
            + _chip_link(f"only my data center (DC {my_dc})",
                         "/gallery" + _gqs(dc=None if same_dc_only else "mine", offset=None),
                         same_dc_only,
                         "photos stored on your own Telegram data center load fastest; "
                         "the others work too")) if my_dc else "")
        + '</div>'
        '<script>(function(){const gp=document.getElementById("gp"),q=document.getElementById("gpQ");'
        'document.getElementById("gpBtn").onclick=e=>{e.stopPropagation();gp.classList.toggle("open");'
        'if(gp.classList.contains("open"))q.focus()};'
        'document.addEventListener("click",e=>{if(!gp.contains(e.target))gp.classList.remove("open")});'
        'q.addEventListener("input",()=>{const v=q.value.trim().toLowerCase();'
        'document.querySelectorAll("#gpList .gp-opt").forEach(o=>{o.style.display=!v||o.dataset.k.includes(v)?"flex":"none"})});'
        'q.addEventListener("keydown",e=>{if(e.key==="Escape")gp.classList.remove("open");'
        'if(e.key==="Enter"){const f=[...document.querySelectorAll("#gpList .gp-opt")].find(o=>o.style.display!=="none");'
        'if(f)location.href=f.href}})})()</script>')
    _where_txt = (f'in {html.escape(_name(picked))}' if picked else
                  'in my private chats' if private else '')

    if not rows:
        body = (
            f'{_LIST_CSS}<h2 class="section">Gallery <span class="count">0</span></h2>'
            f'{ch_chips_html}'
            '<div class="empty-state">'
            '<div class="icon">🖼</div>'
            + ('<div>No photo matches.</div>' if (picked or private or q or same_dc_only or page_offset) else
               '<div>No photos yet.</div>'
               f'<div style="margin-top:8px;font-size:13px">{pending:,} photos are indexed; their '
               'thumbnails are fetched as pages are opened.</div>')
            + '</div>'
        )
        return HTMLResponse(_layout("Gallery", "/gallery", body))

    items = "".join(
        f'<figure class="item" data-id="{r["id"]}" '
        f'data-src="/api/thumb/{r["id"]}" '
        f'data-cap="{html.escape((r["caption"] or "")[:300])}" '
        f'data-ch="{html.escape(r["ch_title"] or r["ch_user"] or "")}">'
        f'<img src="/api/thumb/{r["id"]}" loading="lazy" data-mid="{r["id"]}" class="lazy-thumb" />'
        + (f'<div class="dup-badge" title="shared across {r["dup_count"]} channels">×{r["dup_count"]}</div>'
           if r.get("dup_count") and r["dup_count"] > 1 else '')
        + f'<figcaption class="meta">'
        f'<div class="ch">{html.escape(r["ch_title"] or "")}</div>'
        f'<div class="cap">{html.escape((r["caption"] or "")[:200])}</div>'
        f'</figcaption>'
        f'</figure>'
        for r in rows
    )

    body = (
        f'{_LIST_CSS}<h2 class="section">Gallery <span class="count">{matched:,}</span>'
        '<span class="extra" style="color:var(--muted);font-size:12px;font-weight:400;text-transform:none;'
        f'letter-spacing:0;margin-left:10px">photos {_where_txt}'
        + (f' matching “{html.escape(q)}”' if q else '')
        + ' · the same picture posted in several places is shown once · click one for the slideshow</span></h2>'
        f'{ch_chips_html}'
        f'<div class="gallery">{items}</div>'
        + _page_nav("/gallery", _gqs, page_offset, page_size, len(rows), matched or 0) +
        '<div class="lightbox">'
        '  <div class="lb-progress"><div class="lb-progress-bar" id="lbProgress"></div></div>'
        '  <button class="lb-close" id="lbClose">×</button>'
        '  <button class="lb-prev" id="lbPrev" title="previous"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></button>'
        '  <button class="lb-next" id="lbNext" title="next"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5l7 7-7 7"/></svg></button>'
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
        '  /* A thumbnail that has not arrived yet is a 1x1 placeholder from the server.'
        '     Until the real picture is there the tile shows a moving shimmer; it is'
        '     asked for again a few times, then the tile says there is no picture. */'
        '  function hookLazy(img) {'
        '    if (img._tgHook) return; img._tgHook = true; img._tgTry = 0;'
        '    const fig = img.closest(".item");'
        '    const check = () => {'
        '      if (img.naturalWidth > 1) { fig.classList.remove("nopic"); fig.classList.add("loaded"); return; }'
        '      if (img._tgTry < 6) {'
        '        img._tgTry++;'
        '        const base = img.src.split("?")[0];'
        '        setTimeout(() => { img.src = base + "?r=" + Date.now(); }, 2500 + img._tgTry * 1500);'
        '      } else { fig.classList.add("nopic"); }'
        '    };'
        '    img.addEventListener("load", check);'
        '    img.addEventListener("error", () => fig.classList.add("nopic"));'
        '    if (img.complete && img.naturalWidth) check();'
        '  }'
        '  document.querySelectorAll("img.lazy-thumb").forEach(hookLazy);'
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
        '  /* The large image of a slide.'
        '     The grid thumbnail (12 KB) is shown at once, softened, and the full image'
        '     replaces it when it has arrived AND been decoded - the swap is a de-blur,'
        '     not a jump. Full images are fetched with fetch() so the answer can be'
        '     told apart: 200 = here it is, 202 = still being fetched (ask again),'
        '     anything else = it will never come (do not wait 30 s on it). What has'
        '     been fetched is kept, so going back - or reaching a slide that was'
        '     prefetched - shows the full image immediately with no thumbnail flash. */'
        '  const sharp = new Map();'
        '  const pending = new Map();'
        '  const sleep = ms => new Promise(r => setTimeout(r, ms));'
        '  function loadSharp(id) {'
        '    if (sharp.has(id)) return Promise.resolve(sharp.get(id));'
        '    if (pending.has(id)) return pending.get(id);'
        '    const job = (async () => {'
        '      const t0 = Date.now();'
        '      for (let t = 0; t < 10 && Date.now() - t0 < 30000; t++) {'
        '        let r;'
        '        try { r = await fetch("/api/photo-preview/" + id + (t ? "?r=" + t : "")); }'
        '        catch (e) { await sleep(1500); continue; }'
        '        if (r.status === 200) {'
        '          const url = URL.createObjectURL(await r.blob());'
        '          const im = new Image(); im.src = url;'
        '          try { await im.decode(); } catch (e) {}'
        '          if (im.naturalWidth > 1) {'
        '            const v = {url: url, w: im.naturalWidth, h: im.naturalHeight};'
        '            sharp.set(id, v);'
        '            if (sharp.size > 60) { const k = sharp.keys().next().value; const o = sharp.get(k);'
        '              if (o && o.url) URL.revokeObjectURL(o.url); sharp.delete(k); }'
        '            return v;'
        '          }'
        '          URL.revokeObjectURL(url);'
        '        } else if (r.status !== 202) {'
        '          sharp.set(id, null);'
        '          return null;'
        '        }'
        '        await sleep(900 + t * 600);'
        '      }'
        '      return null;'
        '    })();'
        '    pending.set(id, job);'
        '    job.finally(() => pending.delete(id));'
        '    return job;'
        '  }'
        '  /* Size the picture to the window without blowing a small one up into a'
        '     smear: at most 2.5x its own pixels. */'
        '  function fit(w, h, cap) {'
        '    const k = Math.min(window.innerWidth * 0.96 / w, window.innerHeight * 0.9 / h, cap || 99);'
        '    img.style.width = Math.round(w * k) + "px";'
        '    img.style.height = Math.round(h * k) + "px";'
        '  }'
        '  function armAdvance(myIdx, delay) {'
        '    if (cur !== myIdx || !playing) return;'
        '    clearTimeout(timer);'
        '    timer = setTimeout(() => show(cur + 1), delay);'
        '    prog.style.transition = "none"; prog.style.width = "0";'
        '    void prog.offsetHeight;'
        '    prog.style.transition = "width " + delay + "ms linear";'
        '    prog.style.width = "100%";'
        '  }'
        '  function prefetch(myIdx) {'
        '    for (let d = 1; d <= 2; d++) { const n = photos[(myIdx + d) % photos.length]; if (n) loadSharp(n.id); }'
        '  }'
        '  function show(idx) {'
        '    if (!photos.length) return;'
        '    idx = ((idx % photos.length) + photos.length) % photos.length;'
        '    cur = idx;'
        '    const p = photos[idx], myIdx = idx;'
        '    clearTimeout(timer);'
        '    prog.style.transition = "none"; prog.style.width = "0";'
        '    FX.forEach(c => img.classList.remove(c));'
        '    void img.offsetHeight;'
        '    let fx; do { fx = Math.floor(Math.random() * FX.length); } while (fx === lastFx && FX.length > 1);'
        '    lastFx = fx;'
        '    chEl.textContent = p.ch || "";'
        '    capEl.textContent = p.cap || "";'
        '    counter.textContent = (idx + 1) + " / " + photos.length;'
        '    const have = sharp.get(p.id);'
        '    if (have) {'
        '      img.classList.remove("soft");'
        '      fit(have.w, have.h, 2.5);'
        '      img.src = have.url;'
        '      img.classList.add(FX[fx]);'
        '      armAdvance(myIdx, DELAY);'
        '      prefetch(myIdx);'
        '      return;'
        '    }'
        '    /* thumbnail first, softened */'
        '    img.classList.add("soft");'
        '    const th = p.el.querySelector("img");'
        '    if (th && th.naturalWidth > 1) fit(th.naturalWidth, th.naturalHeight); else { img.style.width = ""; img.style.height = ""; }'
        '    img.src = p.src;'
        '    img.classList.add(FX[fx]);'
        '    loadSharp(p.id).then(v => {'
        '      if (cur !== myIdx) return;'
        '      if (v) {'
        '        fit(v.w, v.h, 2.5);'
        '        img.src = v.url;'
        '        img.classList.remove("soft");'
        '        armAdvance(myIdx, DELAY);'
        '        prefetch(myIdx);'
        '      } else {'
        '        /* no full image for this one: show what there is, move on sooner */'
        '        img.classList.remove("soft");'
        '        if (th && th.naturalWidth > 1) fit(th.naturalWidth, th.naturalHeight, 2.5);'
        '        armAdvance(myIdx, 2200);'
        '      }'
        '    });'
        '  }'
        '  window.addEventListener("resize", () => {'
        '    if (!lb.classList.contains("open") || cur < 0) return;'
        '    const v = sharp.get(photos[cur].id);'
        '    if (v) fit(v.w, v.h, 2.5);'
        '  });'
        '  function updateProg() {'
        '    /* Progress bar is now driven by armAdvance() in show(). */'
        '    prog.style.transition = "none"; prog.style.width = "0";'
        '  }'
        '  function play() { playing = true; playBtn.textContent = "⏸"; clearTimeout(timer); show(cur); }'
        '  function pause() { playing = false; playBtn.textContent = "▶"; clearTimeout(timer); prog.style.transition = "none"; prog.style.width = "0"; }'
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


# ── Music player ─────────────────────────────────────────────────────
# The bar at the bottom plays; "Now playing" opens a panel with the cover,
# the lyrics (timed when lrclib.net has them, highlighted as the song goes)
# and a spectrum. The spectrum can be switched off: it routes the sound
# through Web Audio, which a few browsers / Bluetooth setups dislike, so with
# it off playback is the plain <audio> element.
_MUSIC_PLAYER_HTML = r"""
<style>
.music-list .act { white-space:nowrap; text-align:right; }
.mbtn { display:inline-block; border:1px solid transparent; background:transparent; color:var(--muted);
  font-size:16px; line-height:1; padding:6px 8px; border-radius:8px; cursor:pointer; text-decoration:none; }
.mbtn:hover { border-color:var(--border); color:var(--accent); }
.mbtn.fav.on { color:#e11d48; }
.player-bar { gap:12px; padding:10px 18px; }
.player-bar .np { cursor:pointer; min-width:0; flex:0 1 300px; }
.player-bar .np-icon { overflow:hidden; flex:none; }
.player-bar .np-icon img { width:100%; height:100%; object-fit:cover; display:block; }
.player-bar .ctl { display:flex; gap:6px; align-items:center; flex:none; }
.player-bar .ctl button, .player-bar .ctl a { padding:8px 11px; font-size:16px; line-height:1; }
.player-bar #pp { font-size:20px; padding:9px 15px; }
.player-bar #mini { width:110px; height:38px; flex:none; }
.player-bar .seek { flex:1; min-width:80px; display:flex; align-items:center; gap:8px; font-size:12px; color:var(--muted); }
.player-bar .seek input { flex:1; accent-color:var(--accent); }
.player-bar audio { display:none; }
.np-panel { position:fixed; left:260px; right:0; bottom:66px; top:0; z-index:99; display:none; color:#e5e7eb;
  background:linear-gradient(160deg,#0b1220 0%,#111a2e 55%,#1b1033 100%); overflow:hidden; }
.np-panel.open { display:flex; flex-direction:column; }
.np-panel .bgimg { position:absolute; inset:-40px; background-size:cover; background-position:center;
  filter:blur(46px) brightness(.38); opacity:.9; }
.np-panel .top { position:relative; display:flex; justify-content:flex-end; gap:8px; padding:14px 18px; }
.np-panel .top button { background:rgba(255,255,255,.08); color:#e5e7eb; border:1px solid rgba(255,255,255,.16);
  border-radius:18px; padding:7px 14px; font-size:13px; cursor:pointer; }
.np-panel .top button.on { background:rgba(96,165,250,.28); border-color:#60a5fa; }
.np-panel .mid { position:relative; flex:1; min-height:0; display:flex; gap:44px; padding:6px 48px 0; align-items:center; }
.np-panel .art { flex:none; width:min(300px,28vw); text-align:center; }
.np-panel .art .cv { width:100%; aspect-ratio:1; border-radius:14px; background:rgba(255,255,255,.07);
  display:flex; align-items:center; justify-content:center; font-size:84px; overflow:hidden;
  box-shadow:0 18px 50px rgba(0,0,0,.5); }
.np-panel .art .cv img { width:100%; height:100%; object-fit:cover; }
.np-panel .art .t { font-size:20px; font-weight:700; margin-top:18px; color:#fff; }
.np-panel .art .a { font-size:14px; color:#9ca3af; margin-top:6px; }
.np-panel .lyr { flex:1; min-width:0; height:100%; overflow-y:auto; scrollbar-width:none; padding:38vh 0;
  -webkit-mask-image:linear-gradient(transparent,#000 18%,#000 82%,transparent);
  mask-image:linear-gradient(transparent,#000 18%,#000 82%,transparent); }
.np-panel .lyr::-webkit-scrollbar { display:none; }
.np-panel .lyr div { font-size:19px; line-height:1.5; padding:9px 0; color:#6b7280; transition:color .25s,transform .25s;
  transform-origin:left center; cursor:pointer; }
.np-panel .lyr div.on { color:#fff; font-weight:700; transform:scale(1.08); }
.np-panel .lyr.plain { padding:20px 0; }
.np-panel .lyr.plain div { cursor:default; color:#cbd5e1; font-size:16px; padding:3px 0; }
.np-panel .lyr .none { color:#6b7280; font-size:15px; cursor:default; }
.np-panel canvas#viz { position:relative; width:100%; height:120px; flex:none; display:block; }
@media (max-width:900px){ .np-panel{ left:0 } .np-panel .mid{ flex-direction:column; gap:14px; padding:0 20px }
  .np-panel .art{ width:150px } .np-panel .art .t{ font-size:16px; margin-top:10px } .np-panel .lyr{ width:100%; padding:20vh 0 }
  .player-bar #mini, .player-bar .seek span { display:none } .player-bar .np { flex:1 1 120px } }
/* phone: the side menu is a top bar, so the player spans the whole width
   and wraps onto two rows; the list keeps title, length and the two buttons */
@media (max-width:820px){
  .player-bar { left:0; flex-wrap:wrap; padding:8px 10px; gap:6px 8px; }
  .player-bar .np { min-width:0; flex:1 1 100%; }
  .player-bar .np-icon { width:34px; height:34px; font-size:17px; }
  .player-bar .np-text { min-width:0; flex:1; }
  .player-bar .np-t, .player-bar .np-a { max-width:none; }
  .player-bar .seek { flex:1 1 100%; order:5; }
  .player-bar .ctl button, .player-bar .ctl a { padding:7px 9px; font-size:15px; }
  .player-bar #npBtn { font-size:12px; }
  .music-list th:nth-child(4), .music-list td:nth-child(4),
  .music-list th:nth-child(5), .music-list td:nth-child(5) { display:none; }
  .music-list td, .music-list th { padding-left:8px; padding-right:8px; }
  .music-list { margin-bottom:130px; }
}
</style>
<div class="np-panel" id="npPanel">
  <div class="bgimg" id="npBg"></div>
  <div class="top">
    <button id="vizBtn" title="moving spectrum on / off">Spectrum</button>
    <button id="lyrBtn" title="lyrics on / off">Lyrics</button>
    <button id="npClose">Close ✕</button>
  </div>
  <div class="mid">
    <div class="art"><div class="cv" id="npCover">🎵</div><div class="t" id="npT2"></div><div class="a" id="npA2"></div></div>
    <div class="lyr" id="lyr"></div>
  </div>
  <canvas id="viz"></canvas>
</div>
<div class="player-bar">
  <div class="np" id="npOpen" title="open the now-playing view">
    <div class="np-icon" id="npIcon">🎵</div>
    <div class="np-text"><div class="np-t" id="npT">— select a track —</div><div class="np-a" id="npA"></div></div>
  </div>
  <div class="ctl">
    <button class="ghost" id="prevBtn" title="previous (p)">⏮</button>
    <button id="pp" title="play / pause (space)">▶</button>
    <button class="ghost" id="nextBtn" title="next (n)">⏭</button>
  </div>
  <div class="seek"><span id="tCur">0:00</span><input type="range" id="seek" min="0" max="1000" value="0"><span id="tDur">0:00</span></div>
  <canvas id="mini"></canvas>
  <div class="ctl">
    <button class="ghost" id="shuffleBtn" title="shuffle">🔀</button>
    <button class="ghost" id="favBtn" title="favourite (f)">♥</button>
    <a class="btn ghost" id="dlBtn" title="download this file" download>⬇</a>
    <button class="ghost" id="npBtn" title="cover, lyrics and spectrum (l)">♪ Now playing</button>
  </div>
  <audio id="audio" preload="metadata"></audio>
</div>
<script>
(() => {
  const $ = id => document.getElementById(id);
  const rows = [...document.querySelectorAll(".music-list tbody tr")];
  const audio = $("audio");
  let cur = -1, shuffled = false, lines = null, lineEls = [], lineIdx = -1;
  let prefs = {}; try { prefs = JSON.parse(localStorage.getItem("tgarr.music") || "{}"); } catch (e) {}
  if (prefs.viz === undefined) prefs.viz = true;
  if (prefs.lyr === undefined) prefs.lyr = true;
  const save = () => { try { localStorage.setItem("tgarr.music", JSON.stringify(prefs)); } catch (e) {} };
  const fmt = s => { s = Math.max(0, Math.floor(s || 0)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

  // ── spectrum ──
  let ctx = null, an = null, data = null, raf = 0;
  function vizStart(){
    if (!prefs.viz) return;
    try {
      if (!ctx) {
        ctx = new (window.AudioContext || window.webkitAudioContext)();
        an = ctx.createAnalyser(); an.fftSize = 256; an.smoothingTimeConstant = 0.82;
        ctx.createMediaElementSource(audio).connect(an); an.connect(ctx.destination);
        data = new Uint8Array(an.frequencyBinCount);
      }
      if (ctx.state === "suspended") ctx.resume();
      if (!raf) raf = requestAnimationFrame(draw);
    } catch (e) { prefs.viz = false; vizUi(); }
  }
  function bars(cv, n, big){
    const w = cv.clientWidth, h = cv.clientHeight; if (!w || !h) return;
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
    const bw = w / n, used = Math.floor(data.length * 0.72);
    for (let i = 0; i < n; i++) {
      let v = 0; const a = Math.floor(i * used / n), b = Math.max(a + 1, Math.floor((i + 1) * used / n));
      for (let j = a; j < b; j++) v = Math.max(v, data[j]);
      const bh = Math.max(2, (v / 255) * h * (big ? 0.95 : 0.9));
      if (big) { const gr = g.createLinearGradient(0, h - bh, 0, h); gr.addColorStop(0, "rgba(167,139,250,.95)"); gr.addColorStop(1, "rgba(56,189,248,.35)"); g.fillStyle = gr; }
      else g.fillStyle = "rgba(56,160,220,.85)";
      g.fillRect(i * bw + bw * 0.18, h - bh, bw * 0.64, bh);
    }
  }
  function draw(){
    raf = 0;
    if (!prefs.viz || !an || audio.paused) { clear(); return; }
    an.getByteFrequencyData(data);
    bars($("mini"), 18, false);
    if ($("npPanel").classList.contains("open")) bars($("viz"), 72, true);
    raf = requestAnimationFrame(draw);
  }
  function clear(){ for (const id of ["mini", "viz"]) { const c = $(id); const g = c.getContext("2d"); g.clearRect(0, 0, c.width, c.height); } }
  function vizUi(){ $("vizBtn").classList.toggle("on", !!prefs.viz); $("viz").style.display = prefs.viz ? "block" : "none";
    $("mini").style.visibility = prefs.viz ? "visible" : "hidden"; }
  $("vizBtn").onclick = () => { prefs.viz = !prefs.viz; save(); vizUi();
    if (prefs.viz && !audio.paused) vizStart();
    else if (!prefs.viz && ctx) { clear(); } };

  // ── lyrics ──
  function lyrUi(){ $("lyrBtn").classList.toggle("on", !!prefs.lyr); $("lyr").style.visibility = prefs.lyr ? "visible" : "hidden"; }
  $("lyrBtn").onclick = () => { prefs.lyr = !prefs.lyr; save(); lyrUi(); if (prefs.lyr && cur >= 0 && lines === null) loadLyrics(rows[cur].dataset.id); };
  function loadLyrics(id){
    const box = $("lyr"); lines = null; lineEls = []; lineIdx = -1;
    box.className = "lyr"; box.innerHTML = '<div class="none">looking for lyrics…</div>';
    if (!prefs.lyr) return;
    fetch("/api/lyrics/" + id).then(r => r.json()).then(j => {
      if (cur < 0 || rows[cur].dataset.id !== String(id)) return;
      if (!j.found) { lines = []; box.innerHTML = '<div class="none">No lyrics found for this track.</div>'; return; }
      if (j.synced) {
        lines = j.lines;
        box.innerHTML = lines.map((l, i) => '<div data-i="' + i + '">' + (esc(l[1]) || "♪") + "</div>").join("");
        lineEls = [...box.children];
        lineEls.forEach(el => el.onclick = () => { audio.currentTime = lines[+el.dataset.i][0]; });
      } else {
        lines = []; box.className = "lyr plain";
        box.innerHTML = j.text.split("\n").map(t => "<div>" + (esc(t) || "&nbsp;") + "</div>").join("");
      }
    }).catch(() => { lines = []; box.innerHTML = '<div class="none">Could not load lyrics.</div>'; });
  }
  function lyrTick(){
    if (!lines || !lines.length || !lineEls.length) return;
    const t = audio.currentTime + 0.15; let lo = 0, hi = lines.length - 1, k = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (lines[m][0] <= t) { k = m; lo = m + 1; } else hi = m - 1; }
    if (k === lineIdx) return;
    if (lineIdx >= 0 && lineEls[lineIdx]) lineEls[lineIdx].classList.remove("on");
    lineIdx = k;
    if (k >= 0) { lineEls[k].classList.add("on");
      if ($("npPanel").classList.contains("open")) lineEls[k].scrollIntoView({block: "center", behavior: "smooth"}); }
  }

  // ── playing ──
  function setCover(url){
    const img = url ? '<img src="' + esc(url) + '" alt="" onerror="this.replaceWith(document.createTextNode(\'🎵\'))">' : "🎵";
    $("npIcon").innerHTML = img; $("npCover").innerHTML = img;
    $("npBg").style.backgroundImage = url ? 'url("' + url.replace(/"/g, "%22") + '")' : "none";
  }
  function favUi(){ const on = cur >= 0 && rows[cur].dataset.fav === "1"; $("favBtn").style.color = on ? "#e11d48" : ""; }
  window.tgMusicPlay = function(row){
    cur = rows.indexOf(row);
    rows.forEach(r => r.classList.remove("playing")); row.classList.add("playing");
    const d = row.dataset;
    audio.src = "/media/" + d.id;
    $("npT").textContent = $("npT2").textContent = d.title;
    $("npA").textContent = $("npA2").textContent = d.artist;
    $("dlBtn").href = "/media/" + d.id; $("dlBtn").setAttribute("download", d.file || "");
    setCover(d.cover); favUi(); loadLyrics(d.id);
    if ("mediaSession" in navigator) { try { navigator.mediaSession.metadata = new MediaMetadata({title: d.title, artist: d.artist,
      artwork: d.cover ? [{src: d.cover}] : []}); } catch (e) {} }
    vizStart();
    audio.play().catch(() => {});
  };
  window.tgMusicNext = function(){ if (!rows.length) return;
    tgMusicPlay(rows[shuffled ? Math.floor(Math.random() * rows.length) : (cur + 1) % rows.length]); };
  function prev(){ if (!rows.length) return; if (audio.currentTime > 4) { audio.currentTime = 0; return; }
    tgMusicPlay(rows[(cur - 1 + rows.length) % rows.length]); }
  function toggle(){ if (cur < 0) { if (rows.length) tgMusicPlay(rows[0]); return; } audio.paused ? audio.play() : audio.pause(); }
  window.tgFav = function(row){
    fetch("/api/fav/" + row.dataset.id, {method: "POST"}).then(r => r.json()).then(j => {
      if (j.fav === undefined) return;
      row.dataset.fav = j.fav ? "1" : "0";
      const b = row.querySelector(".mbtn.fav"); if (b) b.classList.toggle("on", j.fav);
      favUi();
    });
  };
  $("pp").onclick = toggle; $("nextBtn").onclick = tgMusicNext; $("prevBtn").onclick = prev;
  $("favBtn").onclick = () => { if (cur >= 0) tgFav(rows[cur]); };
  $("shuffleBtn").onclick = e => { shuffled = !shuffled; const b = e.currentTarget;
    b.style.background = shuffled ? "var(--accent)" : ""; b.style.color = shuffled ? "#fff" : ""; };
  const panel = on => { $("npPanel").classList.toggle("open", on); $("npBtn").classList.toggle("on", on);
    if (on) { lineIdx = -1; lyrTick(); if (!audio.paused) vizStart(); } };
  $("npBtn").onclick = () => panel(!$("npPanel").classList.contains("open"));
  $("npOpen").onclick = () => panel(true); $("npClose").onclick = () => panel(false);
  audio.addEventListener("play", () => { $("pp").textContent = "⏸"; vizStart(); });
  audio.addEventListener("pause", () => { $("pp").textContent = "▶"; });
  audio.addEventListener("ended", tgMusicNext);
  audio.addEventListener("error", () => { if (cur >= 0) $("npA").textContent = "could not play this file — try the next one"; });
  audio.addEventListener("timeupdate", () => {
    $("tCur").textContent = fmt(audio.currentTime); $("tDur").textContent = fmt(audio.duration);
    if (audio.duration && !seeking) $("seek").value = Math.round(audio.currentTime / audio.duration * 1000);
    lyrTick();
  });
  let seeking = false;
  $("seek").addEventListener("input", () => { seeking = true; });
  $("seek").addEventListener("change", e => { seeking = false; if (audio.duration) audio.currentTime = e.target.value / 1000 * audio.duration; });
  document.addEventListener("keydown", e => {
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) && e.target.type !== "range") return;
    if (e.key === "n") tgMusicNext();
    else if (e.key === "p") prev();
    else if (e.key === "f" && cur >= 0) tgFav(rows[cur]);
    else if (e.key === "l") panel(!$("npPanel").classList.contains("open"));
    else if (e.key === "Escape") panel(false);
    else if (e.key === " ") { e.preventDefault(); toggle(); }
  });
  vizUi(); lyrUi();
  // /music#now opens the now-playing view on the first track
  if (location.hash === "#now" && rows.length) { tgMusicPlay(rows[0]); panel(true); }
})();
</script>
"""


@app.get("/music", response_class=HTMLResponse)
async def page_music(limit: int = 100, q: Optional[str] = None,
                     lang: Optional[str] = None, sort: str = "quality",
                     offset: int = 0, local: int = 0, fav: int = 0):
    async with db_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM channels")
    if not login.session_exists() and not n:
        return RedirectResponse("/login")

    _music_my_dc = await _get_my_dc()
    # Composite quality: MB-canonical hit + has cover + duration > 1min
    sort_clause = {
        "quality": (
            "(CASE WHEN m.audio_canonical_title IS NOT NULL THEN 1.5 ELSE 1.0 END "
            " * CASE WHEN m.audio_cover_url IS NOT NULL THEN 1.2 ELSE 1.0 END "
            " * CASE WHEN m.audio_duration_sec >= 60 THEN 1.0 "
            "        WHEN m.audio_duration_sec >= 20 THEN 0.5 ELSE 0.2 END "
            # Actual audio quality: lossless containers first, then the
            # average bitrate implied by size / duration (kbit/s).
            " * CASE WHEN m.file_name ~* '\.(flac|ape|wav|alac|aiff?|dsf|wv)$' "
            "             OR m.mime_type ~* '(flac|wav|x-ape|aiff)' THEN 1.25 "
            "        WHEN COALESCE(m.audio_duration_sec, 0) < 20 "
            "             OR COALESCE(m.file_size, 0) = 0 THEN 1.0 "
            "        WHEN m.file_size * 8.0 / 1000 / m.audio_duration_sec >= 256 THEN 1.15 "
            "        WHEN m.file_size * 8.0 / 1000 / m.audio_duration_sec >= 160 THEN 1.0 "
            "        WHEN m.file_size * 8.0 / 1000 / m.audio_duration_sec >= 96  THEN 0.85 "
            "        ELSE 0.7 END "
            ") DESC NULLS LAST, m.posted_at DESC NULLS LAST"
        ),
        "recent":   "m.posted_at DESC NULLS LAST",
        "longest":  "m.audio_duration_sec DESC NULLS LAST",
        "size":     "m.file_size DESC NULLS LAST",
    }.get(sort, "m.posted_at DESC NULLS LAST")

    where_parts = ["m.media_type = 'audio'" + _pub_m()]
    params = []
    if q:
        params.append(f"%{q}%")
        where_parts.append(
            f"(m.audio_title ILIKE ${len(params)} OR "
            f"m.audio_performer ILIKE ${len(params)} OR "
            f"m.file_name ILIKE ${len(params)})")
    if lang:
        params.append(lang)
        where_parts.append(f"m.detected_lang = ${len(params)}")
    if local:
        where_parts.append(_LOCAL_SQL)
    if fav:
        where_parts.append("EXISTS (SELECT 1 FROM favorites f WHERE f.msg_id = m.id)")
    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)

    async with db_pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM messages m WHERE {' AND '.join(where_parts)}", *params)
        pending_n = await conn.fetchval(
            "SELECT count(*) FROM messages WHERE media_type='audio' AND local_path IS NULL")
        lang_dist = await conn.fetch(
            "SELECT COALESCE(detected_lang,'unknown') AS lang, count(*) AS n "
            "FROM messages WHERE media_type='audio' "
            "GROUP BY detected_lang ORDER BY count(*) DESC")
        rows = await conn.fetch(f"""
            SELECT m.id, m.audio_title, m.audio_performer, m.audio_duration_sec,
                   m.file_name, m.file_size, m.posted_at, m.file_dc,
                   m.audio_canonical_title, m.audio_album, m.audio_year,
                   m.audio_cover_url, m.local_path, m.detected_lang,
                   EXISTS (SELECT 1 FROM favorites f WHERE f.msg_id = m.id) AS fav,
                   c.title AS ch_title, c.username AS ch_user
            FROM messages m
            JOIN channels c ON c.id = m.channel_id
            WHERE {' AND '.join(where_parts)}
            ORDER BY {sort_clause}, m.id
            LIMIT {page_size} OFFSET {page_offset}
        """, *params)

    def _music_qs(**overrides):
        defaults = {"q": q, "lang": lang, "sort": sort if sort != "quality" else None,
                    "local": 1 if local else None, "fav": 1 if fav else None,
                    "limit": limit if limit != 100 else None}
        merged = {**defaults, **overrides}
        bits = []
        for k, v in merged.items():
            if v not in (None, "", 0):
                bits.append(f"{k}={html.escape(str(v))}")
        return "?" + "&".join(bits) if bits else ""

    def _music_lang_chip(code, count):
        label = _LANG_LABELS.get(code, f"❓ {code}")
        active = lang == code
        bg = "background:rgba(94,182,229,0.15);color:var(--accent-hi);border-color:var(--accent);" if active else ""
        href = f"/music{_music_qs(lang=None if active else code)}"
        return (f'<a class="btn ghost" href="{href}" '
                f'style="{bg}padding:5px 12px;font-size:12px">'
                f'{label} <span style="opacity:0.6">{count:,}</span></a>')

    lang_chips_html = (
        '<a class="btn ghost" href="/music" style="' +
        ('background:rgba(94,182,229,0.15);color:var(--accent-hi);border-color:var(--accent);' if not lang else '') +
        'padding:5px 12px;font-size:12px">all</a>' +
        "".join(_music_lang_chip(r["lang"], r["n"]) for r in lang_dist if r["lang"] != 'unknown' and r["n"] > 0)
    )

    def _music_sort_chip(val, label):
        active = sort == val
        bg = "background:rgba(94,182,229,0.15);color:var(--accent-hi);border-color:var(--accent);" if active else ""
        href = f"/music{_music_qs(sort=val if val != 'quality' else None)}"
        return f'<a class="btn ghost" href="{href}" style="{bg}padding:5px 10px;font-size:12px">{label}</a>'

    sort_chips_html = (
        _music_sort_chip("quality", "★ Best") +
        _music_sort_chip("recent",  "Recent") +
        _music_sort_chip("longest", "Longest") +
        _music_sort_chip("size",    "Largest") +
        '<span style="margin-left:10px"></span>' + _saved_chip("/music", _music_qs, local) +
        f'<a class="btn ghost" href="/music{_music_qs(fav=None if fav else 1)}" '
        f'style="{"background:rgba(244,63,94,0.12);color:#e11d48;border-color:#fb7185;" if fav else ""}'
        f'padding:5px 12px;font-size:12px" title="only my favourites">♥ favourites</a>')

    toolbar = (
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center">'
        + lang_chips_html + '</div>'
        '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:18px;align-items:center">'
        '<span style="color:var(--muted);font-size:12px;margin-right:4px">Sort:</span>'
        + sort_chips_html + '</div>'
    )

    if not rows:
        body = (
            f'<h2 class="section">Music <span class="count">0</span></h2>'
            f'{toolbar}'
            '<div class="empty-state">'
            '<div class="icon">🎵</div>'
            '<div>No audio matches these filters.</div>'
            f'<div style="margin-top:8px;font-size:13px">{pending_n:,} audio indexed — click to fetch.</div>'
            '</div>'
        )
        return HTMLResponse(_layout("Music", "/music", body))

    def _row(r):
        title = r["audio_canonical_title"] or r["audio_title"] or r["file_name"] or "(untitled)"
        artist = r["audio_performer"] or ""
        album = r["audio_album"] or r["ch_title"] or ""
        sub = artist + ((" · " + album) if album else "") + \
            ((" · " + str(r["audio_year"])) if r.get("audio_year") else "")
        return (
            f'<tr data-id="{r["id"]}" data-title="{html.escape(title, quote=True)}" '
            f'data-artist="{html.escape(artist, quote=True)}" '
            f'data-cover="{html.escape(r["audio_cover_url"] or "", quote=True)}" '
            f'data-fav="{1 if r["fav"] else 0}" '
            f'data-file="{html.escape(r["file_name"] or "", quote=True)}" onclick="tgMusicPlay(this)">'
            f'<td class="play-cell"><div class="play-ico">▶</div></td>'
            f'<td class="title-cell">'
            f'<div class="t">{html.escape(title)} {_local_pill(r["local_path"])}</div>'
            f'<div class="a">{_dc_badge(r.get("file_dc"), _music_my_dc)} {html.escape(sub)}</div>'
            f'</td>'
            f'<td class="num">{_fmt_dur(r["audio_duration_sec"])}</td>'
            f'<td class="num">{_fmt_size(r["file_size"])}</td>'
            f'<td class="num">{_fmt_time(r["posted_at"])[:10]}</td>'
            f'<td class="act">'
            f'<button class="mbtn fav{" on" if r["fav"] else ""}" title="favourite" '
            f'onclick="event.stopPropagation();tgFav(this.closest(\'tr\'))">♥</button>'
            f'<a class="mbtn" title="download this file" href="/media/{r["id"]}" '
            f'download="{html.escape(r["file_name"] or "", quote=True)}" '
            f'onclick="event.stopPropagation()">⬇</a>'
            f'</td>'
            f'</tr>')

    rows_html = "".join(_row(r) for r in rows)

    body = (
        f'<h2 class="section">Music <span class="count">{len(rows)} of {total:,}</span>'
        + (f' · <span style="color:var(--muted);font-size:13px">{pending_n:,} indexed (click to fetch)</span>'
           if pending_n else '')
        + '</h2>'
        f'{toolbar}'
        '<table class="music-list">'
        '<thead><tr><th style="width:40px"></th><th>title</th><th>duration</th><th>size</th>'
        '<th>posted</th><th style="width:84px"></th></tr></thead>'
        f'<tbody>{rows_html}</tbody></table>'
        + _page_nav("/music", _music_qs, page_offset, page_size, len(rows), total)
        + _MUSIC_PLAYER_HTML
    )
    return HTMLResponse(_layout("Music", "/music", body))


# ════════════════════════════════════════════════════════════════════
# Page: Library (ebooks)
# ════════════════════════════════════════════════════════════════════
_LANG_LABELS = {
    'en': '🇬🇧 EN', 'zh': '🇨🇳 中文', 'ja': '🇯🇵 日本語',
    'ko': '🇰🇷 한국어', 'ar': '🇸🇦 العربية', 'fa': '🇮🇷 فارسی',
    'ru': '🇷🇺 Русский', 'hi': '🇮🇳 हिन्दी', 'he': '🇮🇱 עברית',
    'th': '🇹🇭 ไทย',
}


@app.get("/library", response_class=HTMLResponse)
async def page_library(limit: int = 100, q: Optional[str] = None,
                       lang: Optional[str] = None, sort: str = "quality",
                       fmt: Optional[str] = None, offset: int = 0, local: int = 0):
    async with db_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM channels")
    if not login.session_exists() and not n:
        return RedirectResponse("/login")

    _lib_my_dc = await _get_my_dc()
    # Composite book quality score:
    # - format: epub=1.0 > mobi=0.9 > azw3=0.9 > azw=0.8 > pdf=0.6 > others=0.5
    # - file_size sweet spot 100KB-50MB
    # - has cached file = +0.2 (cover extracted, ready to read)
    sort_clause = {
        "quality": (
            "(CASE WHEN m.file_name ~* '\\.epub$' THEN 1.0 "
            "      WHEN m.file_name ~* '\\.mobi$' THEN 0.9 "
            "      WHEN m.file_name ~* '\\.azw3?$' THEN 0.85 "
            "      WHEN m.file_name ~* '\\.pdf$' THEN 0.6 "
            "      ELSE 0.5 END "
            " * CASE WHEN m.file_size BETWEEN 100000 AND 50000000 THEN 1.0 "
            "        WHEN m.file_size BETWEEN 50000 AND 200000000 THEN 0.7 "
            "        ELSE 0.4 END "
            " * CASE WHEN m.local_path IS NOT NULL AND m.local_path NOT LIKE '\\_\\_%' THEN 1.2 ELSE 1.0 END "
            ") DESC NULLS LAST, m.posted_at DESC NULLS LAST"
        ),
        "recent": "m.posted_at DESC NULLS LAST",
        "size":   "m.file_size DESC NULLS LAST",
        "title":  "m.file_name ASC NULLS LAST",
    }.get(sort, "m.posted_at DESC NULLS LAST")

    where_parts = ["m.media_type = 'document'" + _pub_m(),
                   "m.file_name ~* '\\.(pdf|epub|mobi|azw3?|djvu|fb2|cbr|cbz|lit|txt)$'"]
    params = []
    if q:
        params.append(f"%{q}%")
        where_parts.append(f"m.file_name ILIKE ${len(params)}")
    if lang:
        params.append(lang)
        where_parts.append(f"m.detected_lang = ${len(params)}")
    if fmt:
        params.append(f".{fmt.lower()}$")
        where_parts.append(f"m.file_name ~* ${len(params)}")
    if local:
        where_parts.append(_LOCAL_SQL)
    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)

    async with db_pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM messages m WHERE {' AND '.join(where_parts)}", *params)
        pending_n = await conn.fetchval(
            r"""SELECT count(*) FROM messages
               WHERE media_type='document' AND local_path IS NULL
                 AND file_name ~* '\.(pdf|epub|mobi|azw3?|djvu|fb2|cbr|cbz|lit|txt)$'""")
        # Language distribution chips
        lang_dist = await conn.fetch(
            r"""SELECT COALESCE(detected_lang,'unknown') AS lang, count(*) AS n
               FROM messages
               WHERE media_type='document'
                 AND file_name ~* '\.(pdf|epub|mobi|azw3?|djvu|fb2|cbr|cbz|lit|txt)$'
               GROUP BY detected_lang ORDER BY count(*) DESC""")
        rows = await conn.fetch(f"""
            SELECT m.id, m.file_name, m.file_size, m.mime_type, m.posted_at,
                   m.local_path, m.file_dc, m.detected_lang,
                   c.title AS ch_title, c.username AS ch_user
            FROM messages m
            JOIN channels c ON c.id = m.channel_id
            WHERE {' AND '.join(where_parts)}
            ORDER BY {sort_clause}, m.id
            LIMIT {page_size} OFFSET {page_offset}
        """, *params)

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
            f'<div style="margin-top:8px;font-size:13px">{pending_n:,} ebook indexed — click to fetch.</div>'
            '</div>'
        )
        return HTMLResponse(_layout("Library", "/library", body))

    def _qs(**overrides):
        defaults = {"q": q, "lang": lang, "sort": sort if sort != "quality" else None, "fmt": fmt,
                    "local": 1 if local else None,
                    "limit": limit if limit != 100 else None}
        merged = {**defaults, **overrides}
        bits = []
        for k, v in merged.items():
            if v not in (None, "", 0):
                bits.append(f"{k}={html.escape(str(v))}")
        return "?" + "&".join(bits) if bits else ""

    def _lang_chip(code, count):
        label = _LANG_LABELS.get(code, f"❓ {code}")
        active = lang == code
        bg = "rgba(94,182,229,0.15);color:var(--accent-hi);border-color:var(--accent);" if active else ""
        href = f"/library{_qs(lang=None if active else code)}"
        return (f'<a class="btn ghost" href="{href}" '
                f'style="{bg}padding:5px 12px;font-size:12px">'
                f'{label} <span style="opacity:0.6">{count:,}</span></a>')

    lang_chips = (
        '<a class="btn ghost" href="/library" style="' +
        ('background:rgba(94,182,229,0.15);color:var(--accent-hi);border-color:var(--accent);' if not lang else '') +
        'padding:5px 12px;font-size:12px">all</a>' +
        "".join(_lang_chip(r["lang"], r["n"]) for r in lang_dist if r["lang"] != 'unknown' and r["n"] > 0)
    )

    def _sort_chip(val, label):
        active = sort == val
        bg = "background:rgba(94,182,229,0.15);color:var(--accent-hi);border-color:var(--accent);" if active else ""
        href = f"/library{_qs(sort=val if val != 'quality' else None)}"
        return f'<a class="btn ghost" href="{href}" style="{bg}padding:5px 10px;font-size:12px">{label}</a>'

    sort_chips = (
        _sort_chip("quality", "★ Best") +
        _sort_chip("recent",  "Recent") +
        _sort_chip("size",    "Largest") +
        _sort_chip("title",   "Title") +
        '<span style="margin-left:10px"></span>' + _saved_chip("/library", _qs, local))

    toolbar = (
        '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center">' +
        lang_chips + '</div>'
        '<div style="display:flex;gap:6px;margin-bottom:18px;align-items:center">'
        '<span style="color:var(--muted);font-size:12px;margin-right:4px">Sort:</span>' +
        sort_chips + '</div>'
    )

    if not rows:
        body = (
            f'<h2 class="section">Library <span class="count">0</span></h2>'
            f'{toolbar}'
            '<div class="empty-state">'
            '<div class="icon">📚</div>'
            '<div>No ebooks match these filters.</div>'
            f'<div style="margin-top:8px;font-size:13px">{pending_n:,} ebook indexed — click to fetch.</div>'
            '</div>'
        )
        return HTMLResponse(_layout("Library", "/library", body))

    def _lang_badge(code):
        if not code or code == 'unknown':
            return ''
        label = _LANG_LABELS.get(code, code.upper())
        return f'<span class="pill muted" style="font-size:11px">{label}</span>'

    items = "".join(
        f'<div class="book-card">'
        + (f'<img class="book-cover" src="/ebook-cover/{r["id"]}" alt="" '
           f'onerror="this.outerHTML=\'<div class=&quot;book-ico&quot;>{_ico(_ext(r["file_name"]))}</div>\'" />'
           if r["local_path"] and not r["local_path"].startswith("__") and _ext(r["file_name"]) in ("EPUB","MOBI","AZW","AZW3")
           else f'<div class="book-ico">{_ico(_ext(r["file_name"]))}</div>')
        + f'<div class="book-body">'
        f'<div class="book-title">{html.escape(r["file_name"] or "(untitled)")}</div>'
        f'<div class="book-meta">'
        f'{_dc_badge(r.get("file_dc"), _lib_my_dc)}'
        f'{_lang_badge(r.get("detected_lang"))}'
        f'{_local_pill(r["local_path"])}'
        f'<span class="pill accent">{_ext(r["file_name"])}</span>'
        f'<span class="pill muted">{_fmt_size(r["file_size"])}</span>'
        f'<span>· {html.escape(r["ch_title"] or "")}</span>'
        f'</div>'
        f'</div>'
        f'<div class="book-actions">'
        f'<a class="btn" href="/watch/{r["id"]}">Open</a>'
        f'<a class="btn ghost" href="/media/{r["id"]}" download="{html.escape(r["file_name"] or "")}">Download</a>'
        f'</div>'
        f'</div>'
        for r in rows
    )

    body = (
        f'<h2 class="section">Library <span class="count">{len(rows)} of {total:,}</span>'
        + (f' · <span style="color:var(--muted);font-size:13px">{pending_n:,} indexed (click to fetch)</span>'
           if pending_n else '')
        + '</h2>'
        f'{toolbar}'
        f'<div class="book-grid">{items}</div>'
        + _page_nav("/library", _qs, page_offset, page_size, len(rows), total)
    )
    return HTMLResponse(_layout("Library", "/library", body))


# ════════════════════════════════════════════════════════════════════
# Page: Videos (all video messages — incl. those that didn't parse)
# ════════════════════════════════════════════════════════════════════
@app.get("/videos", response_class=HTMLResponse)
async def page_videos(q: Optional[str] = None, limit: int = 120,
                      offset: int = 0, aud: str = "sfw",
                      min_mb: int = 50, local: int = 0, fav: int = 0, sort: str = "new"):
    if not login.session_exists():
        async with db_pool.acquire() as conn:
            n = await conn.fetchval("SELECT count(*) FROM channels")
        if not n:
            return RedirectResponse("/login")

    where = ["m.media_type = 'video'" + _pub_m(),
             "COALESCE(c.audience, 'sfw') <> 'blocked_csam'"]
    params = []
    if q:
        for w in q.split():
            params.append(f"%{w}%")
            where.append(f"(m.file_name ILIKE ${len(params)} OR m.caption ILIKE ${len(params)})")
    if aud == "sfw":
        where.append("COALESCE(c.audience, 'sfw') = 'sfw'")
    elif aud == "nsfw":
        where.append("c.audience = 'nsfw'")
    if min_mb and min_mb > 0:
        where.append(f"COALESCE(m.file_size,0) >= {min_mb * 1024 * 1024}")
    if local:
        where.append(_LOCAL_SQL)
    if fav:
        where.append("EXISTS (SELECT 1 FROM favorites f WHERE f.msg_id = m.id)")

    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)
    where_sql = " AND ".join(where)
    _vid_my_dc = await _get_my_dc()
    async with db_pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM messages m "
            f"JOIN channels c ON c.id = m.channel_id "
            f"WHERE {where_sql}", *params)
        rows = await conn.fetch(
            f"SELECT m.id, m.file_name, m.file_size, m.caption, m.posted_at, "
            f"m.thumb_path, m.local_path, m.file_dc, "
            f"EXISTS (SELECT 1 FROM favorites f WHERE f.msg_id = m.id) AS fav, "
            f"c.title AS ch_title, c.username AS ch_user, c.audience, "
            f"r.id AS release_id, r.canonical_title, r.quality, r.movie_title, r.series_title, "
            f"r.part AS rel_part, r.movie_year, m.duration_sec "
            f"FROM messages m "
            f"JOIN channels c ON c.id = m.channel_id "
            f"LEFT JOIN releases r ON r.primary_msg_id = m.id "
            f"WHERE {where_sql} "
            f"ORDER BY {'m.file_size DESC NULLS LAST, m.id DESC' if sort == 'size' else 'm.posted_at DESC NULLS LAST'} "
            f"LIMIT {page_size} OFFSET {page_offset}",
            *params)

    def _qs(**overrides):
        defaults = {"q": q,
                    "aud": aud if aud != "sfw" else None,
                    "min_mb": min_mb if min_mb != 50 else None,
                    "local": 1 if local else None, "fav": 1 if fav else None,
                    "sort": sort if sort != "new" else None,
                    "limit": limit if limit != 120 else None}
        merged = {**defaults, **overrides}
        bits = []
        for k, v in merged.items():
            if v not in (None, "", "sfw") and not (v == 0 and k != "min_mb"):
                bits.append(f"{k}={html.escape(str(v))}")
        return "?" + "&".join(bits) if bits else ""

    _nsfw_on = await _is_nsfw_enabled()

    def _vc(label, href, on, tip=""):
        return _chip_link(label, "/videos" + href, on, tip)
    toolbar = (
        '<div class="bar-tools" style="margin-bottom:18px">'
        + ((_vc("SFW", _qs(aud=None, offset=None), aud == "sfw")
            + _vc("18+", _qs(aud="nsfw", offset=None), aud == "nsfw")
            + _vc("both", _qs(aud="any", offset=None), aud == "any")
            + '<span class="sep"></span>') if _nsfw_on else '')
        + '<span class="lbl">Size:</span>'
        + _vc("any", _qs(min_mb=0, offset=None), min_mb <= 0)
        + _vc("50 MB+", _qs(min_mb=None, offset=None), min_mb == 50, "leaves out clips")
        + _vc("300 MB+", _qs(min_mb=300, offset=None), min_mb == 300, "film-sized")
        + '<span class="sep"></span><span class="lbl">Sort:</span>'
        + _vc("newest", _qs(sort=None, offset=None), sort != "size")
        + _vc("largest", _qs(sort="size", offset=None), sort == "size")
        + '<span class="sep"></span>'
        + _saved_chip("/videos", _qs, local)
        + f'<a class="btn ghost" href="/videos{_qs(fav=None if fav else 1, offset=None)}" '
          f'style="{"background:rgba(244,63,94,0.12);color:#e11d48;border-color:#fb7185;" if fav else ""}'
          f'padding:5px 12px;font-size:12px" title="only my favourites">♥ favourites</a>'
        + '</div>' + _FAV_CARD_JS + _THUMB_RETRY_JS)

    def _card(r):
        title = (r["canonical_title"] or r["movie_title"] or r["series_title"]
                 or r["file_name"] or (r["caption"] or "")[:80] or f"video {r['id']}")
        if r["rel_part"]:
            title += f" — part {r['rel_part']}"
        ch = r["ch_title"] or r["ch_user"] or ""
        date = _fmt_time(r["posted_at"])[:10] if r["posted_at"] else ""
        return _media_card(
            msg_id=r["id"], title=title, sub=" · ".join(x for x in (ch, date) if x),
            img=f'/api/thumb/{r["id"]}', size=r["file_size"], file_dc=r.get("file_dc"),
            my_dc=_vid_my_dc, quality=r.get("quality"), saved=_is_local(r["local_path"]),
            fav=r["fav"], wide=True, duration=r["duration_sec"])

    start_idx = page_offset + 1 if rows else 0
    end_idx = page_offset + len(rows)
    if rows:
        grid_html = '<div class="mc-grid wide">' + ''.join(_card(r) for r in rows) + '</div>'
    else:
        grid_html = ('<div class="empty-state"><div class="icon">🎬</div>'
                     '<div>No video matches.</div></div>')
    _note = []
    if q:
        _note.append(f'matching “{html.escape(q)}”')
    body = (
        f'{_LIST_CSS}'
        f'<h2 class="section">Videos <span class="count">{total:,}</span>'
        f'<span class="extra" style="color:var(--muted, #64748b);font-size:12px;font-weight:400;'
        f'text-transform:none;letter-spacing:0;margin-left:10px">{" · ".join(_note)}</span></h2>'
        f'{toolbar}{grid_html}'
        + _page_nav("/videos", _qs, page_offset, page_size, len(rows), total or 0))
    return HTMLResponse(_layout("Videos", "/videos", body))


# ════════════════════════════════════════════════════════════════════
# Page: Releases
# ════════════════════════════════════════════════════════════════════
def _audience_badge(audience) -> str:
    """SFW/NSFW indicator. blocked_csam was already filtered out earlier."""
    if not audience or audience == "sfw":
        return '<span class="pill ok" style="font-size:11px" title="safe for work">SFW</span>'
    if audience == "nsfw":
        return '<span class="pill warn" style="font-size:11px" title="adult content">NSFW</span>'
    return ""


def _dc_badge(file_dc, my_dc) -> str:
    """Tiny DC indicator: 🟢 same DC (fast), 🟡 cross-DC (may hit FloodWait),
    ⚪ unknown (file_dc not yet recorded — backfills on first click)."""
    if not file_dc:
        return '<span class="pill muted" title="DC unknown — backfills on first click" style="font-size:11px">⚪</span>'
    if not my_dc or file_dc == my_dc:
        return f'<span class="pill ok" title="same DC {file_dc} — fast" style="font-size:11px">🟢 DC{file_dc}</span>'
    return f'<span class="pill warn" title="cross-DC ({file_dc} vs your {my_dc}) — may hit FloodWait" style="font-size:11px">🟡 DC{file_dc}</span>'


def _part_pill(r) -> str:
    """A film posted in pieces: say which piece this card is."""
    try:
        n = r["part"] if "part" in r.keys() else None
    except Exception:
        return ""
    return f'<span class="pill muted" title="this film was posted in several parts">part {int(n)}</span>' if n else ""


def _versions_pill(r) -> str:
    try:
        n = r["versions"] if "versions" in r.keys() else 1
        wk = r["wk"] if "wk" in r.keys() else None
    except Exception:
        return ""
    if not wk or not n or n < 2:
        return ""
    return (f'<a class="pill accent" href="/releases?work={_q(wk)}" '
            f'title="all versions of this title" style="text-decoration:none">{n} versions</a>')


def _rk(r, k, default=None):
    """Field of a row that may or may not carry it (pages select different columns)."""
    try:
        return r[k] if k in r.keys() else default
    except Exception:
        return default


def _dc_mark(file_dc, my_dc) -> str:
    if not file_dc:
        return ""
    same = bool(my_dc and file_dc == my_dc)
    tip = ("on your Telegram data center: the fast path" if same else
           "on another Telegram data center: it works, a little slower to start")
    return f'<span class="dc{" same" if same else ""}" title="{tip}">DC{file_dc}</span>'


def _fmt_dur_short(sec) -> str:
    if not sec:
        return ""
    sec = int(sec)
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60} min"


def _media_card(*, msg_id, title, img, sub="", size=None, file_dc=None, my_dc=None,
                quality=None, saved=False, versions=0, work=None, part=None,
                guid=None, fav=None, wide=False, duration=None, fallback="🎬") -> str:
    """One card for a film, an episode or a plain video."""
    watch = f"/watch/{msg_id}"
    img_html = (f'<img src="{html.escape(img, quote=True)}" loading="lazy" alt="" '
                f'onerror="this.remove()"/>' if img else "")
    badges = ""
    if quality:
        badges += f'<span class="bq">{html.escape(quality)}</span>'
    if saved:
        badges += '<span class="bs" title="already downloaded: opens instantly">● saved</span>'
    if part:
        badges += f'<span class="bp" title="this film was posted in several parts">part {int(part)}</span>'
    more = ""
    if work and versions and versions > 1:
        more = (f'<a class="bv" href="/releases?work={_q(work)}" '
                f'title="other copies of this title: other resolutions or uploads">{versions} versions</a>')
    meta = [x for x in (_fmt_size(size), _fmt_dur_short(duration)) if x]
    acts = f'<a class="b pri" href="{watch}">▶ Play</a>'
    if not _signed_in():
        fav = None                        # favourites and grabs belong to the account
    if guid and _signed_in():
        acts += (f'<button class="b grab-btn" data-guid="{guid}" onclick="tgGrab(this)" '
                 f'title="save it to the download folder (the one Sonarr/Radarr read)">⬇ Grab</button>')
    elif _signed_in() or saved:
        # signed out, only a file that is already on this machine can be handed over
        acts += (f'<a class="b sq" href="/media/{msg_id}" download title="download to this device">⬇</a>')
    if fav is not None:
        acts += (f'<button class="b sq{" on" if fav else ""}" onclick="tgFavCard(this,{msg_id})" '
                 f'title="{"in your favourites" if fav else "add to favourites"}">♥</button>')
    return (
        f'<div class="mc{" wide" if wide else ""}">'
        f'<a class="mc-img" href="{watch}"><span class="fb">{fallback}</span>{img_html}'
        f'<span class="pl"><span>▶</span></span>{badges}</a>'
        f'{("<div class=mc-over>" + more + "</div>") if more else ""}'
        f'<div class="mc-body"><a class="mc-title" href="{watch}" title="{html.escape(title, quote=True)}">'
        f'{html.escape(title)}</a>'
        f'<div class="mc-sub">{html.escape(sub)}</div>'
        f'<div class="mc-meta">{" · ".join(meta)}{_dc_mark(file_dc, my_dc)}</div></div>'
        f'<div class="mc-act">{acts}</div></div>')


# Cards whose picture is a thumbnail still being fetched: the server answers
# with a 1x1 placeholder at first. Shimmer until the real one is there, ask
# again a few times, then leave the plain icon.
_THUMB_RETRY_JS = (
    '<style>.mc-img{background:linear-gradient(100deg,#e3e9f0 30%,#f4f7fa 50%,#e3e9f0 70%);'
    'background-size:200% 100%;animation:tgShim 1.3s linear infinite}'
    '.mc-img.done{animation:none;background:#e9eef4}'
    '@keyframes tgShim{to{background-position:-200% 0}}</style>'
    '<script>document.addEventListener("DOMContentLoaded",function(){'
    'document.querySelectorAll(".mc-img img").forEach(function(img){var box=img.closest(".mc-img"),n=0;'
    'function chk(){if(img.naturalWidth>1){box.classList.add("done");return}'
    'if(img.src.indexOf("/api/thumb/")<0||n>=6){img.style.display="none";box.classList.add("done");return}'
    'n++;var b=img.src.split("?")[0];setTimeout(function(){img.src=b+"?r="+Date.now()},2500+n*1500)}'
    'img.addEventListener("load",chk);img.addEventListener("error",function(){box.classList.add("done")});'
    'if(img.complete&&img.naturalWidth)chk()})})</script>')

_FAV_CARD_JS = ('<script>function tgFavCard(b,id){fetch("/api/fav/"+id,{method:"POST"}).then(r=>r.json())'
                '.then(j=>{if(j.fav===undefined)return;b.classList.toggle("on",j.fav);'
                'b.title=j.fav?"in your favourites":"add to favourites"})}</script>')


def _release_card(r, my_dc=None) -> str:
    """A film or an episode."""
    title = _rk(r, "canonical_title") or (r["name"] or "").replace(".", " ")
    bits = []
    if r["category"] == "tv" and r["season"] is not None:
        bits.append(f'S{r["season"]:02}' + (f'E{r["episode"]:02}' if r["episode"] is not None else ""))
    if _rk(r, "movie_year"):
        bits.append(str(r["movie_year"]))
    if not bits and r["posted_at"]:
        bits.append("posted " + _fmt_time(r["posted_at"])[:10])
    pmid = r["primary_msg_id"]
    return _media_card(
        msg_id=pmid, title=title, sub=" · ".join(bits),
        img=r["poster_url"] or (f"/api/thumb/{pmid}" if pmid else ""),
        size=r["size_bytes"], file_dc=_rk(r, "file_dc"), my_dc=my_dc,
        quality=r["quality"], saved=_is_local(_rk(r, "local_path")),
        versions=_rk(r, "versions") or 0, work=_rk(r, "wk"), part=_rk(r, "part"),
        guid=r["guid"], wide=not r["poster_url"])


@app.get("/releases", response_class=HTMLResponse)
async def page_releases(q: Optional[str] = None, cat: Optional[str] = None,
                        view: str = "grid", limit: int = 120,
                        offset: int = 0,
                        min_mb: int = 100, aud: str = "sfw",
                        work: Optional[str] = None, sort: str = "best"):
    if not _signed_in():
        # readable without the account, as long as there is something to read
        async with db_pool.acquire() as conn:
            if not await conn.fetchval("SELECT 1 FROM channels LIMIT 1"):
                return RedirectResponse("/login")
    _sorts = {"best": "composite_score DESC NULLS LAST, posted_at DESC NULLS LAST",
              "new": "posted_at DESC NULLS LAST, id DESC",
              "size": "size_bytes DESC NULLS LAST, id DESC",
              "name": "lower(COALESCE(NULLIF(canonical_title, ''), name)), id"}
    if sort not in _sorts:
        sort = "best"
    where = ["1=1"]
    if not _signed_in():
        where.append("c.username IS NOT NULL")
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
    # CSAM is always hidden. SFW/NSFW chip filter — default sfw.
    where.append("COALESCE(c.audience, 'sfw') <> 'blocked_csam'")
    if aud == "sfw":
        where.append("COALESCE(c.audience, 'sfw') = 'sfw'")
    elif aud == "nsfw":
        where.append("c.audience = 'nsfw'")
    # aud == "any" or anything else → no audience filter beyond csam
    def _qual(w):
        # audience refs already use c. prefix — pass through
        if "c.audience" in w or "c.username" in w:
            return w
        return (w.replace("name ILIKE", "r.name ILIKE")
                 .replace("category =", "r.category =")
                 .replace("COALESCE(size_bytes", "COALESCE(r.size_bytes"))
    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)
    # Composite quality score: parse confidence × tmdb verification × quality tier
    # × grab popularity × recency. Show "best version of any content" first.
    # Several files can be the same work (a 1080p and a 2160p, or the same
    # film uploaded by different people to different data centers). Show one
    # card per work - the copy that is best FOR THIS ACCOUNT - with the number
    # of versions; ?work=<key> lists them all. Nothing is removed.
    _release_my_dc = await _get_my_dc()
    _pref = await _version_pref()
    _count_params = list(params)        # the count query takes the text filters only
    params.append(_release_my_dc or -1)
    _same_dc = f"(CASE WHEN m.file_dc = ${len(params)} THEN 1 ELSE 0 END)"
    _order = (f"same_dc DESC, qr DESC" if _pref == "dc" else f"qr DESC, same_dc DESC")
    _order += ", size_bytes DESC NULLS LAST, id"
    _work_filter = ""
    if work:
        params.append(work[:300])
        _work_filter = f"WHERE wk = ${len(params)}"
    sql = (f"WITH base AS (SELECT r.id, r.guid, r.name, r.category, r.season, r.episode, r.quality, "
          f"r.size_bytes, r.posted_at, r.parse_score, r.poster_url, r.canonical_title, "
          f"r.primary_msg_id, r.grab_count, m.file_dc, c.audience, r.part, m.local_path, "
          f"r.movie_year, "
          f"{_WORK_KEY_SQL} AS wk, {_QUALITY_RANK_SQL} AS qr, {_same_dc} AS same_dc, "
          f"  (r.parse_score "
          f"   * (CASE r.metadata_source WHEN 'tmdb' THEN 1.3 WHEN 'wikidata' THEN 1.2 WHEN 'wikipedia' THEN 1.1 WHEN 'miss' THEN 0.7 ELSE 1.0 END) "
          f"   * (CASE r.quality WHEN '2160p' THEN 1.4 WHEN '1080p' THEN 1.2 WHEN '720p' THEN 1.0 ELSE 0.8 END) "
          f"   * (1 + LEAST(LOG(r.grab_count + 1) / 3.0, 1.0)) "
          f"   * (1.0 / (1 + EXTRACT(EPOCH FROM (NOW() - COALESCE(r.posted_at, NOW())))/86400 * 0.005)) "
          f"  ) AS composite_score "
          f"FROM releases r LEFT JOIN messages m ON m.id = r.primary_msg_id "
          f"LEFT JOIN channels c ON c.id = m.channel_id "
          f"WHERE {' AND '.join([_qual(w) for w in where])}), "
          f"ranked AS (SELECT *, "
          f"  row_number() OVER (PARTITION BY COALESCE(wk, 'id:' || id::text) ORDER BY {_order}) AS rn, "
          f"  count(*) OVER (PARTITION BY COALESCE(wk, 'id:' || id::text)) AS versions "
          f"  FROM base {_work_filter}) "
          + (f"SELECT * FROM ranked ORDER BY {_order} "
             if work else
             f"SELECT * FROM ranked WHERE rn = 1 "
             f"ORDER BY {_sorts[sort]} ")
          + f"LIMIT {page_size} OFFSET {page_offset}")
    count_sql = (f"WITH base AS (SELECT r.id, {_WORK_KEY_SQL} AS wk FROM releases r "
                 f"LEFT JOIN messages m ON m.id = r.primary_msg_id "
                 f"LEFT JOIN channels c ON c.id = m.channel_id "
                 f"WHERE {' AND '.join([_qual(w) for w in where])}) "
                 + (f"SELECT count(*) FROM base WHERE wk = ${len(_count_params) + 1}" if work else
                    f"SELECT count(DISTINCT COALESCE(wk, 'id:' || id::text)) FROM base"))
    if work:
        _count_params.append(work[:300])
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        total_matched = await conn.fetchval(count_sql, *_count_params)

    def _qs(**overrides) -> str:
        defaults = {"q": q, "cat": cat, "view": view if view != "grid" else None,
                    "aud": aud if aud != "sfw" else None,
                    "sort": sort if sort != "best" else None,
                    "min_mb": min_mb if min_mb != 100 else None,
                    "limit": limit if limit != 120 else None}
        merged = {**defaults, **overrides}
        bits = []
        for k, v in merged.items():
            if v not in (None, "", "sfw") and not (v == 0 and k != "min_mb"):
                bits.append(f"{k}={html.escape(str(v))}")
        return "?" + "&".join(bits) if bits else ""

    def _chip(val: str, label: str) -> str:
        active = (cat or "") == val
        href = f"/releases{_qs(cat=val if val else None)}"
        style = ('background:rgba(94,182,229,0.15);color:var(--accent-hi);'
                'border-color:var(--accent);') if active else ''
        return (f'<a class="btn ghost" href="{href}" '
               f'style="{style}padding:5px 12px;font-size:12px">{label}</a>')

    def _aud_chip(val: str, label: str) -> str:
        active = aud == val or (val == "sfw" and not aud)
        href = f"/releases{_qs(aud=val if val != 'sfw' else None)}"
        bg = "rgba(245,158,11,0.18)" if val == "nsfw" and active else "rgba(94,182,229,0.15)"
        fg = "var(--warn)" if val == "nsfw" else "var(--accent-hi)"
        style = (f'background:{bg};color:{fg};border-color:{fg};') if active else ''
        return (f'<a class="btn ghost" href="{href}" '
               f'style="{style}padding:5px 12px;font-size:12px">{label}</a>')

    _nsfw_on = await _is_nsfw_enabled()
    grid_active = "active" if view != "list" else ""
    list_active = "active" if view == "list" else ""
    toolbar = (
        f'<div style="display:flex;gap:10px;margin-bottom:22px;align-items:center;flex-wrap:wrap">'
        f'{_chip("","all")}{_chip("movie","🎬 films")}{_chip("tv","📺 episodes")}'
        + (f'<span style="width:1px;height:22px;background:var(--border);margin:0 4px"></span>'
           f'{_aud_chip("sfw","SFW")}{_aud_chip("nsfw","18+")}{_aud_chip("any","both")}'
           if _nsfw_on else '')
        + f'<span style="width:1px;height:22px;background:var(--border);margin:0 4px"></span>'
        f'<span style="font-size:12px;color:var(--muted)">Sort:</span>'
        + "".join(_chip_link(lbl, "/releases" + _qs(sort=None if k == "best" else k, offset=None), sort == k, tip)
                  for k, lbl, tip in (("best", "best", "known titles and good copies first"),
                                      ("new", "newest", "most recently posted"),
                                      ("size", "largest", ""), ("name", "name", "")))
        + f'<div style="margin-left:auto" class="view-toggle">'
        f'<a class="{grid_active}" href="/releases{_qs(view=None)}">▦ Grid</a>'
        f'<a class="{list_active}" href="/releases{_qs(view="list")}">☰ List</a>'
        f'</div></div>'
    )

    if not rows:
        body_html = ('<div class="empty-state"><div class="icon">🎬</div>'
                    '<div>No releases match this filter.</div></div>')
    elif view == "list":
        def _row_thumb(url, pmid=None):
            if not url and pmid:
                url = f'/api/thumb/{pmid}'
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
            f'<td style="width:60px;padding:8px 12px">{_row_thumb(r["poster_url"], r["primary_msg_id"])}</td>'
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
                    f'{"".join(_release_card(r, _release_my_dc) for r in rows)}'
                    f'</div>')
        body_html = body_html.replace('<div class="poster-grid">', '<div class="mc-grid">', 1)

    # Build pagination nav
    start_idx = page_offset + 1 if rows else 0
    end_idx = page_offset + len(rows)
    filter_desc_parts = [f"aud={aud}"]
    if min_mb > 0:
        filter_desc_parts.append(f"≥{min_mb}MB")
    if cat:
        filter_desc_parts.append(f"cat={cat}")
    if q:
        filter_desc_parts.append(f'q="{html.escape(q)}"')
    filter_desc = " · ".join(filter_desc_parts)
    prev_offset = max(0, page_offset - page_size)
    next_offset = page_offset + page_size
    nav_links = []
    if page_offset > 0:
        nav_links.append(f'<a class="btn ghost" href="/releases{_qs(offset=prev_offset if prev_offset else None)}">← prev</a>')
    if next_offset < (total_matched or 0):
        nav_links.append(f'<a class="btn ghost" href="/releases{_qs(offset=next_offset)}">next →</a>')
    pagination_html = (
        f'<div style="display:flex;justify-content:space-between;align-items:center;'
        f'margin-top:18px;padding:12px 0;border-top:1px solid var(--border, #e2e8f0)">'
        f'<span style="color:var(--muted, #64748b);font-size:13px">'
        f'showing {start_idx}–{end_idx} of {total_matched:,}</span>'
        f'<div style="display:flex;gap:8px">{"".join(nav_links)}</div>'
        f'</div>')
    _what = ("Versions of this title" if work else
             {"movie": "Films", "tv": "Episodes"}.get(cat or "", "Films and episodes"))
    _note = []
    if q:
        _note.append(f'matching “{html.escape(q)}”')
    if min_mb > 0 and not work:
        _note.append(f'{min_mb} MB and larger · <a href="/releases{_qs(min_mb=0, offset=None)}">show smaller too</a>')
    if work:
        _note.append('<a href="/releases">← all releases</a>')
    body = (f'{_THUMB_RETRY_JS}<h2 class="section">{_what} '
           f'<span class="count">{total_matched:,}</span>'
           f'<span class="extra" style="color:var(--muted, #64748b);font-size:12px;font-weight:400;'
           f'text-transform:none;letter-spacing:0;margin-left:10px">{" · ".join(_note)}</span></h2>'
           f'{toolbar}{body_html}{pagination_html}')
    actions = (f'<form action="/releases" method="GET" style="margin-left:auto">'
              f'<input type="text" name="q" class="search" placeholder="filter…" '
              f'value="{html.escape(q) if q else ""}" /></form>')
    return HTMLResponse(_layout("Releases", "/releases", body, top_actions_html=actions))


# ════════════════════════════════════════════════════════════════════
# Page: Downloads (queue + history)
# ════════════════════════════════════════════════════════════════════
@app.get("/downloads", response_class=HTMLResponse)
async def page_downloads(tab: str = "players", q: Optional[str] = None,
                         kind: Optional[str] = None, sort: str = "recent",
                         fav: int = 0, offset: int = 0, limit: int = 50):
    # signed out: what is already saved from public channels can still be
    # opened; nothing can be fetched, paused or removed
    ro = not _signed_in()
    tab = "grabs" if (tab == "grabs" and not ro) else "players"
    page_size = max(10, min(limit, 200))
    page_offset = max(0, offset)
    _my_dc = await _get_my_dc()
    async with db_pool.acquire() as conn:
        active = await conn.fetch(
            """SELECT d.id, d.status, d.requested_at, r.name, r.size_bytes,
                      d.bytes_done, d.speed_kbps, d.last_progress_at,
                      m.file_dc
               FROM downloads d
               JOIN releases r ON r.id = d.release_id
               LEFT JOIN messages m ON m.id = r.primary_msg_id
               WHERE d.status IN ('pending','downloading','paused')
               ORDER BY d.requested_at""")
        done = await conn.fetch(
            """SELECT d.id, d.status, d.local_path, d.requested_at, d.finished_at,
                      d.error_message, r.name, r.size_bytes,
                      m.file_dc
               FROM downloads d
               JOIN releases r ON r.id = d.release_id
               LEFT JOIN messages m ON m.id = r.primary_msg_id
               WHERE d.status IN ('completed','failed','cancelled')
               ORDER BY d.finished_at DESC NULLS LAST LIMIT 100""")

    fetches = [] if ro else await _player_fetches()
    saved = (await _player_saved(q, kind, sort, fav, page_size, page_offset)
             if tab == "players" else None)

    def _dqs(**over):
        cur = {"tab": None if tab == "players" else tab, "q": q, "kind": kind,
               "sort": sort if sort != "recent" else None, "fav": 1 if fav else None,
               "limit": limit if limit != 50 else None}
        cur.update(over)
        bits = [f"{k}={_q(str(v))}" for k, v in cur.items() if v not in (None, "", 0)]
        return "?" + "&".join(bits) if bits else ""

    def _render_saved():
        if saved is None:
            return ""
        kinds = saved["kinds"]
        all_n = sum(kinds.values())

        def chip(label, href, on, extra=""):
            style = ("background:rgba(94,182,229,0.15);color:var(--accent-hi);"
                     "border-color:var(--accent);" if on else "")
            return (f'<a class="btn ghost" href="/downloads{href}" '
                    f'style="{style}padding:5px 12px;font-size:12px">{label}{extra}</a>')

        def cnt(n):
            return f' <span style="opacity:.6">{n:,}</span>'
        chips = (
            chip("all", _dqs(kind=None, offset=None), not kind, cnt(all_n))
            + chip("🎬 films", _dqs(kind="video", offset=None), kind == "video", cnt(kinds.get("video", 0)))
            + chip("🎵 songs", _dqs(kind="audio", offset=None), kind == "audio", cnt(kinds.get("audio", 0)))
            + chip("📄 books", _dqs(kind="book", offset=None), kind == "book", cnt(kinds.get("library", 0)))
            + '<span style="margin-left:8px"></span>'
            + chip("♥ favourites", _dqs(fav=None if fav else 1, offset=None), bool(fav))
            + '<span style="margin-left:14px;color:var(--muted);font-size:12px">Sort:</span>'
            + chip("last opened", _dqs(sort=None, offset=None), sort == "recent")
            + chip("oldest", _dqs(sort="oldest", offset=None), sort == "oldest")
            + chip("largest", _dqs(sort="size", offset=None), sort == "size")
            + chip("name", _dqs(sort="name", offset=None), sort == "name"))
        search = (
            '<form method="GET" action="/downloads" style="display:flex;gap:8px;margin:0 0 12px">'
            + "".join(f'<input type="hidden" name="{k}" value="{html.escape(str(v), quote=True)}"/>'
                      for k, v in (("kind", kind), ("sort", sort if sort != "recent" else None),
                                   ("fav", 1 if fav else None)) if v)
            + f'<input type="text" name="q" value="{html.escape(q or "", quote=True)}" '
              'placeholder="find a saved file by name…" style="flex:1;max-width:420px"/>'
              '<button class="btn" type="submit">Search</button>'
            + (f'<a class="btn ghost" href="/downloads{_dqs(q=None, offset=None)}">clear</a>' if q else '')
            + '</form>')
        head = (f'<h2 class="section">Saved from players <span class="count">{saved["total"]:,}</span>'
                f'<span class="extra" style="color:var(--muted);font-size:12px;font-weight:400;'
                f'text-transform:none;letter-spacing:0;margin-left:10px">'
                f'{_fmt_size(saved["bytes"]) or "0 MB"}'
                + ('' if ro else ' · <a href="/settings#cache">clean-up rules</a>') + '</span></h2>'
                f'{search}'
                f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;align-items:center">{chips}</div>')
        if not saved["rows"]:
            return head + ('<div class="empty-state"><div class="icon">💾</div><div>'
                           + ("Nothing saved matches." if (q or kind or fav or page_offset) else
                              "Films, songs and books you open are kept here so they open "
                              "instantly next time.") + '</div></div>')
        ico = {"video": "🎬", "audio": "🎵"}
        trs = []
        for r, have in zip(saved["rows"], saved["have"]):
            total = r["file_size"] or 0
            if have is None:
                state = '<span class="pill bad" title="the file is no longer on disk">missing</span>'
            elif total and have < total:
                state = (f'<span class="pill warn" title="not all of it has arrived">'
                         f'{have * 100 // total}%</span>')
            else:
                state = ""
            trs.append(
                f'<tr data-id="{r["id"]}">'
                f'<td class="ck">{"" if ro else "<input type=checkbox class=sv />"}</td>'
                f'<td class="ic">{ico.get(r["media_type"], "📄")}</td>'
                f'<td class="nm"><a href="/watch/{r["id"]}">'
                f'{html.escape(r["file_name"] or r["local_path"].rsplit("/", 1)[-1])}</a> '
                f'{"<span style=color:#e11d48>♥</span>" if r["fav"] else ""} {state}</td>'
                f'<td class="num">{_fmt_size(total)}</td>'
                f'<td class="num">{_fmt_time(r["used"]) if r["used"] else ""}</td>'
                f'<td class="ac"><div class="dl-ctrls">'
                f'<a class="dl-btn sq" href="/media/{r["id"]}" download '
                f'title="download to this device">⬇</a>'
                + ('' if ro else
                   f'<button class="dl-btn bad sq" onclick="fetchRemove({r["id"]})" '
                   f'title="remove from the server">✗</button>') + '</div></td></tr>')
        table = (
            '<style>.sv-list{width:100%;border-collapse:collapse;background:var(--surface);'
            'border:1px solid var(--border);border-radius:10px;overflow:hidden}'
            '.sv-list th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);'
            'text-align:left;padding:9px 10px;border-bottom:1px solid var(--border)}'
            '.sv-list td{padding:7px 10px;border-bottom:1px solid var(--border);font-size:14px;vertical-align:middle}'
            '.sv-list tr:last-child td{border-bottom:none}.sv-list tr:hover td{background:rgba(127,127,127,.06)}'
            '.sv-list .ck{width:30px}.sv-list .ic{width:30px}.sv-list .num{white-space:nowrap;color:var(--muted);'
            'font-size:13px}.sv-list .ac{white-space:nowrap;text-align:right}'
            '.sv-list .nm a{color:var(--fg);text-decoration:none;word-break:break-all}'
            '.sv-list .nm a:hover{color:var(--accent)}</style>'
            + ('' if ro else
               '<div style="display:flex;gap:10px;align-items:center;margin-bottom:8px;font-size:13px">'
               '<label style="cursor:pointer"><input type="checkbox" id="svAll"/> select this page</label>'
               '<button class="dl-btn bad" id="svRm" disabled onclick="svRemove()" style="min-width:150px">✗ Remove selected</button>'
               '<span id="svN" style="color:var(--muted)"></span></div>')
            + '<table class="sv-list"><thead><tr><th></th><th></th><th>name</th><th>size</th>'
            '<th>last opened</th><th></th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table>'
            + _page_nav("/downloads", _dqs, page_offset, page_size, len(saved["rows"]), saved["total"])
            + ('' if ro else '<script>(function(){const all=document.getElementById("svAll"),rm=document.getElementById("svRm"),'
              'n=document.getElementById("svN");const boxes=()=>[...document.querySelectorAll(".sv")];'
              'function upd(){const c=boxes().filter(b=>b.checked).length;rm.disabled=!c;n.textContent=c?c+" selected":""}'
              'all.onchange=()=>{boxes().forEach(b=>b.checked=all.checked);upd()};'
              'boxes().forEach(b=>b.onchange=upd);'
              'window.svRemove=function(){const ids=boxes().filter(b=>b.checked).map(b=>+b.closest("tr").dataset.id);'
              'if(!ids.length||!confirm("Remove "+ids.length+" file(s) from the server? They stay in the index '
              'and can be fetched again."))return;'
              'fetch("/api/fetch/remove",{method:"POST",headers:{"Content-Type":"application/json"},'
              'body:JSON.stringify({ids:ids})}).then(()=>location.reload())}})()</script>'))
        return head + table

    _fetch_js = ('<script>function fetchCancel(id){fetch("/api/fetch/"+id+"/cancel",{method:"POST"})'
                 '.then(()=>location.reload())}'
                 'function fetchAct(id,a){fetch("/api/fetch/"+id+"/"+a,{method:"POST"})'
                 '.then(()=>setTimeout(()=>location.reload(),600))}'
                 'function fetchRemove(id){if(confirm("Remove this file from the server? '
                 'It stays in the index and can be fetched again."))fetchCancel(id)}</script>')

    def _render_fetches():
        if not fetches:
            return ""
        items = []
        for it in fetches:
            r, have, total = it["r"], it["have"], it["r"]["file_size"] or 0
            pct = round(have * 100 / total, 1) if total else 0
            label, cls, icon = {
                "queued": ("queued", "warn", "⏳"),
                "fetching": ("arriving", "accent", "⬇"),
                "stopped": ("stopped — nobody is watching", "muted", "⏸"),
                "paused": ("paused", "muted", "⏸"),
            }[it["state"]]
            if it.get("pinned") and it["state"] in ("queued", "fetching"):
                label += " · will finish on its own"
            if it["state"] in ("queued", "fetching"):
                act = (f'<button class="dl-btn" onclick="fetchAct({r["id"]},\'pause\')" '
                       f'title="stop for now and keep what has arrived">⏸ Pause</button>')
            else:
                act = (f'<button class="dl-btn ok" onclick="fetchAct({r["id"]},\'resume\')" '
                       f'title="fetch the rest, even with no player open">▶ Resume</button>')
            dc = _dc_badge(r["file_dc"], _my_dc) if r["file_dc"] else ""
            items.append(
                f'<div class="dl-item"><div class="icon">{icon}</div><div class="info">'
                f'<div class="name"><a href="/watch/{r["id"]}" style="color:inherit;text-decoration:none">'
                f'{html.escape(r["file_name"] or "(no name)")}</a></div>'
                f'<div class="meta"><span class="pill {cls}">{label}</span>{dc}'
                f'<span>· {_fmt_size(total)}</span></div>'
                f'<div class="dl-progress"><div class="dl-progress-bar"><div class="dl-progress-fill" '
                f'style="width:{pct}%"></div></div>'
                f'<div class="dl-progress-meta">{pct:.1f}% · {_fmt_size(have) or "0"} / {_fmt_size(total)}</div></div>'
                f'</div><div class="right"><div class="dl-ctrls">{act}'
                f'<button class="dl-btn bad" onclick="fetchCancel({r["id"]})" '
                f'title="stop, and remove what has arrived">✗ Cancel</button></div></div></div>')
        return (
            f'<h2 class="section">Opened in a player <span class="count">{len(fetches)}</span></h2>'
            '<div style="color:var(--muted);font-size:13px;margin:-6px 0 12px">'
            'Opening a film fetches it from Telegram. It keeps arriving while its player is open, '
            'stops when it is closed, and continues from the same point when it is opened again. '
            '<b>Resume</b> here fetches the rest with no player open; <b>Pause</b> stops it and keeps '
            'what has arrived.</div>'
            f'<div class="dl-list">{"".join(items)}</div>')

    def _render(rows, show_finished):
        if not rows:
            return ('<div class="empty-state"><div class="icon">⬇</div>'
                   '<div>nothing here yet</div></div>')
        items = []
        for d in rows:
            icon = ({"pending": "⏳", "downloading": "⬇", "completed": "✓",
                    "failed": "✗", "cancelled": "—", "paused": "⏸"}
                   ).get(d["status"], "?")
            pill_cls = ({"pending": "warn", "downloading": "accent",
                        "completed": "ok", "failed": "bad",
                        "cancelled": "muted", "paused": "muted"}
                       ).get(d["status"], "muted")
            err_html = ""
            error_msg = d["error_message"] if "error_message" in d.keys() else None
            if error_msg:
                err_html = f'<div class="err">{html.escape(error_msg)}</div>'
            local = ""
            if show_finished and "local_path" in d.keys() and d["local_path"]:
                local = " · " + html.escape(d["local_path"].rsplit("/", 1)[-1])
            finished = d["finished_at"] if "finished_at" in d.keys() else None
            # Progress bar + speed + ETA for in-flight downloads.
            progress_html = ""
            if d["status"] == "downloading":
                size_b = d["size_bytes"] or 0
                done_b = (d["bytes_done"] if "bytes_done" in d.keys() else 0) or 0
                kbps = (d["speed_kbps"] if "speed_kbps" in d.keys() else 0) or 0
                pct = round(done_b * 100 / size_b, 1) if size_b else 0
                left_b = max(0, size_b - done_b)
                eta_sec = int(left_b / max(kbps * 1024, 1)) if kbps else 0
                eta_str = (f"{eta_sec//3600}h{(eta_sec%3600)//60:02d}m"
                           if eta_sec >= 3600 else
                           f"{eta_sec//60}m{eta_sec%60:02d}s"
                           if eta_sec >= 60 else f"{eta_sec}s")
                speed_str = (f"{kbps/1024:.1f} MB/s" if kbps >= 1024
                             else f"{kbps} KB/s")
                progress_html = (
                    f'<div class="dl-progress">'
                    f'<div class="dl-progress-bar"><div class="dl-progress-fill" '
                    f'style="width:{pct}%"></div></div>'
                    f'<div class="dl-progress-meta">'
                    f'{pct:.1f}% · {_fmt_size(done_b)} / {_fmt_size(size_b)} '
                    f'· {speed_str} · ETA {eta_str}'
                    f'</div></div>'
                )
            # DC badge: same-DC = fast direct download, cross-DC = slow
            # + burns auth.ExportAuthorization quota. User can predict speed.
            dc_html = ""
            file_dc = d["file_dc"] if "file_dc" in d.keys() else None
            if file_dc:
                if _my_dc and file_dc == _my_dc:
                    dc_html = f'<span class="pill ok" title="same DC — fast">DC{file_dc} ⚡</span>'
                else:
                    dc_html = f'<span class="pill warn" title="cross-DC — slower">DC{file_dc} ✗</span>'
            # Per-row controls. Buttons depend on status:
            #   pending/downloading → Pause + Delete
            #   paused/failed       → Resume + Delete
            #   completed/cancelled → Delete (purges history row)
            btns = []
            st = d["status"]
            if st in ("pending", "downloading"):
                btns.append(f'<button class="dl-btn" '
                           f'onclick="dlAct({d["id"]},\'pause\')">⏸ Pause</button>')
            if st in ("paused", "failed"):
                btns.append(f'<button class="dl-btn ok" '
                           f'onclick="dlAct({d["id"]},\'resume\')">▶ Resume</button>')
            btns.append(f'<button class="dl-btn bad" '
                       f'onclick="dlAct({d["id"]},\'delete\')">✗</button>')
            ctrls = f'<div class="dl-ctrls">{" ".join(btns)}</div>'
            items.append(
                f'<div class="dl-item">'
                f'<div class="icon">{icon}</div>'
                f'<div class="info">'
                f'<div class="name">{html.escape(d["name"])}</div>'
                f'<div class="meta">'
                f'<span class="pill {pill_cls}">{d["status"]}</span>'
                f'{dc_html}'
                f'<span>· {_fmt_size(d["size_bytes"])}</span>'
                f'<span>· {_fmt_time(d["requested_at"])}{local}</span>'
                f'</div>{progress_html}{err_html}'
                f'</div>'
                f'<div class="right">{ctrls}'
                f'<div class="dl-time">{_fmt_time(finished) if show_finished else ""}</div>'
                f'</div>'
                f'</div>'
            )
        return f'<div class="dl-list">{"".join(items)}</div>'

    # Auto-refresh while any download is in flight, so progress bar updates
    # without manual reload. Stops refreshing when queue empties.
    refresh_js = ('<script>setTimeout(()=>location.reload(),5000)</script>'
                  if ((tab == "grabs" and active) or
                      (tab == "players" and any(f["state"] != "stopped" for f in fetches)
                       and not page_offset and not q)) else '')
    ctrl_js = '''<script>
function dlAct(id, action){
  if(action==='delete' && !confirm('Delete this download?')) return;
  fetch('/api/downloads/'+id+'/'+action,{method:'POST'})
    .then(r=>r.json())
    .then(_=>location.reload())
    .catch(e=>alert('failed: '+e));
}
</script>
'''
    # one look for every action button on this page: same height, same width
    # for the worded ones, square for the icon-only ones in the table
    dl_css = """<style>
.dl-item .right{display:flex;flex-direction:column;align-items:flex-end;justify-content:center;gap:4px}
.dl-ctrls{display:flex;gap:8px;justify-content:flex-end;align-items:center;flex-wrap:nowrap}
.dl-btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;box-sizing:border-box;
  min-width:104px;height:34px;padding:0 14px;font:inherit;font-size:13px;font-weight:500;line-height:1;
  border:1px solid var(--border);background:var(--surface);color:var(--fg);border-radius:8px;
  cursor:pointer;text-decoration:none;white-space:nowrap;transition:background .12s,border-color .12s,color .12s}
.dl-btn:hover{border-color:var(--accent);color:var(--accent)}
.dl-btn:disabled{opacity:.4;cursor:default;border-color:var(--border);color:var(--fg)}
.dl-btn.ok{color:#047857;border-color:#a7f3d0;background:#ecfdf5}
.dl-btn.ok:hover{background:#d1fae5;border-color:#34d399;color:#047857}
.dl-btn.bad{color:#b91c1c;border-color:#fecaca;background:#fef2f2}
.dl-btn.bad:hover{background:#fee2e2;border-color:#f87171;color:#b91c1c}
.dl-btn.sq{min-width:34px;width:34px;padding:0}
.dl-time{font-size:11px;color:#888;text-align:right}
@media (max-width:640px){.dl-btn{min-width:0;padding:0 10px}.dl-ctrls{flex-wrap:wrap}}
</style>"""

    def _tab(key, label, n):
        on = tab == key
        style = ("border-bottom:2px solid var(--accent);color:var(--accent-hi);font-weight:600;"
                 if on else "border-bottom:2px solid transparent;color:var(--muted);")
        href = "/downloads" if key == "players" else "/downloads?tab=grabs"
        return (f'<a href="{href}" style="{style}padding:8px 4px;margin-right:22px;'
                f'text-decoration:none;font-size:15px">{label}'
                + (f' <span style="font-size:12px;opacity:.7">{n}</span>' if n else '') + '</a>')
    tabs = ('<div style="display:flex;border-bottom:1px solid var(--border);margin-bottom:20px">'
            + _tab("players", "Opened in players", len(fetches))
            + _tab("grabs", "Grabs (Sonarr · Radarr · Grab button)", len(active)) + '</div>')
    if ro:
        tabs = ('<div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;'
                'padding:12px 16px;margin-bottom:20px;color:var(--muted);font-size:14px">'
                'Not signed in: files already saved from public channels can be opened and downloaded. '
                '<a href="/login">Sign in to Telegram</a> to fetch more or to remove any.</div>')
    if tab == "players":
        body = f'{dl_css}{tabs}{_fetch_js}{_render_fetches()}{_render_saved()}{refresh_js}'
    else:
        body = (f'{dl_css}{tabs}'
                f'<h2 class="section">Queue <span class="count">{len(active)}</span></h2>'
                f'{_render(active, show_finished=False)}'
                f'<h2 class="section">History <span class="count">last 100</span></h2>'
                f'{_render(done, show_finished=True)}'
                f'{ctrl_js}'
                f'{refresh_js}')
    return HTMLResponse(_layout("Downloads", "/downloads", body))


# ════════════════════════════════════════════════════════════════════
# Page: Search
# ════════════════════════════════════════════════════════════════════
@app.get("/search", response_class=HTMLResponse)
async def page_search(q: Optional[str] = None,
                      scope: Optional[str] = None):
    if not _signed_in():
        async with db_pool.acquire() as conn:
            if not await conn.fetchval("SELECT 1 FROM channels LIMIT 1"):
                return RedirectResponse("/login")
    scope = scope if scope in ("releases", "messages", "channels") else "releases"
    nsfw_on = await _is_nsfw_enabled()
    # what may be shown: never the hard-blocked, adult only when switched on
    gate = ("COALESCE(c.audience, 'sfw') <> 'blocked_csam'"
            + ("" if nsfw_on else " AND COALESCE(c.audience, 'sfw') <> 'nsfw'") + _pub())
    results_html, elapsed_ms, found = "", 0, 0
    if q:
        t0 = time.time()
        my_dc = await _get_my_dc()
        if scope == "messages":
            res = await meili_search("messages", q, limit=150)
            ids = [h["id"] for h in res.get("hits", [])]
            rows = []
            if ids:
                async with db_pool.acquire() as conn:
                    rows = await conn.fetch(f"""
                        SELECT m.id, m.file_name, m.media_type, m.file_size, m.caption,
                               m.posted_at, m.local_path, m.file_dc,
                               c.username AS ch_user, c.title AS ch_title
                        FROM messages m JOIN channels c ON c.id = m.channel_id
                        WHERE m.id = ANY($1::bigint[]) AND {gate}
                        ORDER BY array_position($1::bigint[], m.id)""", ids)
            found = len(rows)
            ico = {"video": "🎬", "audio": "🎵", "photo": "🖼", "document": "📄"}
            items = []
            for r in rows:
                name = r["file_name"] or (r["caption"] or "")[:90] or f'{r["media_type"] or "file"} {r["id"]}'
                href = (f'/gallery?q={_q(q)}' if r["media_type"] == "photo" else f'/watch/{r["id"]}')
                items.append(
                    f'<div class="ls-row"><div style="width:30px;text-align:center;font-size:20px">'
                    f'{ico.get(r["media_type"], "📄")}</div>'
                    f'<div class="ls-main"><div class="ls-title"><a href="{href}">{html.escape(name)}</a> '
                    + ('<span class="tag ok">saved</span>' if _is_local(r["local_path"]) else '')
                    + f'</div><div class="ls-sub"><span>{html.escape(r["ch_title"] or r["ch_user"] or "")}</span>'
                    f'<span>{_fmt_size(r["file_size"])}</span>'
                    f'<span>{_fmt_time(r["posted_at"])[:10] if r["posted_at"] else ""}</span></div></div>'
                    f'<div class="ls-act"><a class="abtn" href="{href}">Open</a></div></div>')
            results_html = f'<div class="ls">{"".join(items)}</div>' if items else ""
        elif scope == "channels":
            res = await meili_search("channels", q, limit=80)
            unames = [h.get("username") for h in res.get("hits", []) if h.get("username")]
            rows = []
            if unames:
                async with db_pool.acquire() as conn:
                    rows = await conn.fetch(f"""
                        SELECT c.username, c.title, c.members_count, c.subscribed, c.is_joined,
                               c.audience, c.dead_at
                        FROM channels c WHERE c.username = ANY($1::text[]) AND {gate}
                        ORDER BY array_position($1::text[], c.username)""", unames)
            found = len(rows)
            items = []
            for r in rows:
                title = r["title"] or ("@" + r["username"])
                tag = ('<span class="tag bad">gone</span>' if r["dead_at"] else
                       '<span class="tag ok">joined</span>' if r["is_joined"] else
                       '<span class="tag acc">followed</span>' if r["subscribed"] else '')
                items.append(
                    f'<div class="ls-row">{_avatar(title)}'
                    f'<div class="ls-main"><div class="ls-title">{html.escape(title)} {tag}</div>'
                    f'<div class="ls-sub"><a href="https://t.me/{html.escape(r["username"])}" target="_blank" '
                    f'rel="noopener">@{html.escape(r["username"])}</a></div></div>'
                    f'<div class="ls-num"><b>{_fmt_count(r["members_count"])}</b><span>members</span></div>'
                    f'<div class="ls-act"><a class="abtn" href="/channels?q={_q(r["username"])}">Details</a></div></div>')
            results_html = f'<div class="ls">{"".join(items)}</div>' if items else ""
        else:
            res = await meili_search("releases", q, limit=150)
            ids = [h["id"] for h in res.get("hits", [])]
            rows = []
            if ids:
                async with db_pool.acquire() as conn:
                    rows = await conn.fetch(f"""
                        SELECT r.id, r.guid, r.name, r.category, r.season, r.episode, r.quality,
                               r.size_bytes, r.posted_at, r.poster_url, r.canonical_title,
                               r.primary_msg_id, r.movie_year, r.part, m.file_dc, m.local_path,
                               {_WORK_KEY_SQL} AS wk
                        FROM releases r
                        JOIN messages m ON m.id = r.primary_msg_id
                        JOIN channels c ON c.id = m.channel_id
                        WHERE r.id = ANY($1::bigint[]) AND {gate}
                        ORDER BY array_position($1::bigint[], r.id)""", ids)
            # one card per title: the best-ranked copy leads, the rest are one click away
            groups, order = {}, []
            for r in rows:
                k = r["wk"] or f"id:{r['id']}"
                if k not in groups:
                    groups[k] = []
                    order.append(k)
                groups[k].append(r)
            found = len(order)
            cards = []
            for k in order:
                lead = dict(groups[k][0])
                lead["versions"] = len(groups[k])
                cards.append(_release_card(lead, my_dc))
            results_html = f'<div class="mc-grid">{"".join(cards)}</div>' if cards else ""
        elapsed_ms = int((time.time() - t0) * 1000)
        if not results_html:
            results_html = ('<div class="empty-state"><div class="icon">🔍</div>'
                            f'<div>Nothing found for <strong>{html.escape(q)}</strong></div>'
                            '<div style="margin-top:8px;font-size:13px">Try fewer words, another spelling, '
                            'or one of the other tabs.</div></div>')
    else:
        results_html = ('<div class="empty-state"><div class="icon">🔍</div>'
                        '<div>Search everything tgarr has indexed.</div>'
                        '<div style="margin-top:8px;font-size:13px">Films and episodes by title · '
                        'any file by its name · channels by name or @username.</div></div>')

    def tab(name, label):
        on = scope == name
        style = ("border-bottom:2px solid var(--accent);color:var(--accent-hi);font-weight:600;"
                 if on else "border-bottom:2px solid transparent;color:var(--muted);")
        return (f'<a href="/search?q={_q(q or "")}&scope={name}" style="{style}padding:8px 4px;'
                f'margin-right:22px;text-decoration:none;font-size:15px">{label}'
                + (f' <span style="font-size:12px;opacity:.7">{found:,}</span>' if on and q else '') + '</a>')
    body = (
        f'{_LIST_CSS}{_THUMB_RETRY_JS}'
        '<form action="/search" method="GET" style="margin-bottom:16px;display:flex;gap:10px">'
        f'<input type="text" name="q" value="{html.escape(q or "", quote=True)}" '
        'placeholder="a film, a file name, a channel…" '
        'style="flex:1;font-size:16px;padding:12px 16px" autofocus />'
        f'<input type="hidden" name="scope" value="{scope}">'
        '<button type="submit" style="padding:0 22px">Search</button></form>'
        '<div style="display:flex;align-items:flex-end;border-bottom:1px solid var(--border);margin-bottom:20px">'
        + tab("releases", "Films &amp; episodes") + tab("messages", "All files") + tab("channels", "Channels")
        + (f'<span style="margin-left:auto;font-size:11px;color:var(--muted);padding-bottom:10px">'
           f'{elapsed_ms} ms</span>' if q else '')
        + f'</div>{results_html}')
    return HTMLResponse(_layout("Search", "/search", body, top_actions_html=""))


# ════════════════════════════════════════════════════════════════════
# Page: Settings
# ════════════════════════════════════════════════════════════════════
# ── the machine tgarr runs on, as far as a container can see it ───────
def _disk_level(free: float, total: float) -> str:
    """ok / warn / bad for a disk. Both an absolute floor and a share: 10 GB
    free is plenty on a small disk's scale and nothing next to a 4K film."""
    if not total:
        return "off"
    gb, share = free / 1024 ** 3, free / total
    if gb < 5 or share < 0.03:
        return "bad"
    if gb < 20 or share < 0.10:
        return "warn"
    return "ok"


def _host_info() -> dict:
    """Disk, memory, load and uptime of the host. A bind-mounted folder
    reports the host's disk, and /proc shows the host's memory and load, so
    these are the real machine's numbers - not the container's."""
    import platform
    import shutil
    info = {"disks": [], "mem": None, "load": None, "cores": os.cpu_count() or 0,
            "uptime_s": None, "kernel": platform.release(), "arch": platform.machine()}
    seen = {}
    for path, label in (("/downloads", "Downloads folder"), ("/app/session", "tgarr data")):
        try:
            dev = os.stat(path).st_dev
            u = shutil.disk_usage(path)
        except OSError:
            continue
        if dev in seen:
            seen[dev]["label"] += " + " + label.lower()
            continue
        d = {"label": label, "total": u.total, "free": u.free, "used": u.total - u.free,
             "level": _disk_level(u.free, u.total)}
        seen[dev] = d
        info["disks"].append(d)
    try:
        kv = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                kv[k] = int(v.split()[0]) * 1024
        total, avail = kv.get("MemTotal", 0), kv.get("MemAvailable", 0)
        if total:
            share = avail / total
            info["mem"] = {"total": total, "avail": avail,
                           "level": "bad" if share < 0.05 else "warn" if share < 0.12 else "ok"}
    except (OSError, ValueError, IndexError):
        pass
    try:
        l1, l5, l15 = os.getloadavg()
        per = l5 / max(1, info["cores"])
        info["load"] = {"l1": l1, "l5": l5, "l15": l15,
                        "level": "bad" if per > 3 else "warn" if per > 1.5 else "ok"}
    except (OSError, AttributeError):
        pass
    try:
        with open("/proc/uptime") as f:
            info["uptime_s"] = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return info


def _fmt_uptime(sec) -> str:
    if not sec:
        return "–"
    d, h = int(sec // 86400), int(sec % 86400 // 3600)
    return f"{d} d {h} h" if d else f"{h} h {int(sec % 3600 // 60)} min"


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    if not _signed_in():
        return RedirectResponse("/login")     # settings belong to the account
    authed = login.session_exists()
    user = login.ensure_user_info() or {}
    async with db_pool.acquire() as conn:
        tmdb_raw = await conn.fetchval(
            "SELECT value FROM config WHERE key='tmdb_api_key'")
        meta_stats = await conn.fetchrow("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE metadata_source='tmdb') AS tmdb,
                   count(*) FILTER (WHERE metadata_source IN ('wikidata', 'wikipedia')) AS wiki,
                   count(*) FILTER (WHERE metadata_source='miss') AS missed,
                   count(*) FILTER (WHERE metadata_source IS NULL) AS pending
            FROM releases
        """)
    tmdb_masked = (tmdb_raw[:6] + "…" + tmdb_raw[-4:]) if tmdb_raw else ""
    nsfw_on = await _is_nsfw_enabled()
    async with db_pool.acquire() as conn:
        version_pref = await conn.fetchval(
            "SELECT value FROM config WHERE key='version_preference'") or "quality"
    my_dc_disp = (await _get_my_dc()) or "?"
    cache_ttl, cache_gb = await _cache_settings()
    _usage = await _cache_usage()
    _kind = {"video": "videos", "audio": "songs", "library": "books"}
    cache_total = _fmt_size(sum(r["bytes"] for r in _usage)) or "0 MB"
    cache_detail = " · ".join(
        f'{r["n"]:,} {_kind.get(r["kind"], r["kind"])} ({_fmt_size(r["bytes"]) or "0"})'
        for r in _usage) or "nothing downloaded yet"
    _full = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    _uname = user.get("username")
    _shown = _full or (("@" + _uname) if _uname else "Telegram account")
    try:                                   # the account's own picture, when it has one
        _av = (f'<img class="acct-av" src="/api/me/avatar?v={int(os.path.getmtime(login.AVATAR_FILE))}" alt="">')
    except OSError:
        _av = (f'<div class="acct-av" style="background:{_color_for(_shown)}">'
               f'{html.escape(_shown.lstrip("@")[:1].upper())}</div>')
    account_html = (
        f'<div class="acct">{_av}<div class="acct-txt">'
        f'<div class="acct-name">{html.escape(_shown)}</div>'
        f'<div class="acct-sub">'
        + (f'@{html.escape(_uname)} · ' if _uname and _full else '')
        + f'Telegram data center {my_dc_disp} <span class="pill ok">signed in</span></div>'
        f'<div class="acct-meta">API ID <code>{html.escape(str(login.API_ID or "-"))}</code> · '
        f'session at <code>_data/session/tgarr.session</code></div></div>'
        '<div class="acct-act"><button class="ghost" onclick="if(confirm(\'Sign out of Telegram on this tgarr?'
        '\\n\\nYour private chats stay on this machine, hidden, and come back when you sign in again. Public channels and saved files stay.\'))'
        'fetch(\'/api/login/logout\',{method:\'POST\'}).then(()=>location.href=\'/\')">Sign out</button></div></div>')

    # ── this machine ──
    host = await asyncio.to_thread(_host_info)
    async with db_pool.acquire() as conn:
        db_bytes = await conn.fetchval("SELECT pg_database_size(current_database())")
        idx_counts = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM channels) AS ch, (SELECT count(*) FROM messages) AS msgs")
    _lvl_col = {"ok": "#10b981", "warn": "#f59e0b", "bad": "#ef4444", "off": "#94a3b8"}

    def _bar(share: float, level: str) -> str:
        return (f'<div class="sys-bar"><i style="width:{max(2, min(100, share * 100)):.0f}%;'
                f'background:{_lvl_col[level]}"></i></div>')

    def _gb(b) -> str:
        g = (b or 0) / 1024 ** 3
        return f"{g:.0f} GB" if g >= 10 else f"{g:.1f} GB"
    tiles, alerts = [], []
    for d in host["disks"]:
        used_share = d["used"] / d["total"] if d["total"] else 0
        tiles.append(
            f'<div class="sys-t {d["level"]}"><div class="sys-l">💽 {html.escape(d["label"])}</div>'
            f'<div class="sys-n">{_gb(d["free"])} <small>free</small></div>'
            f'{_bar(used_share, d["level"])}'
            f'<div class="sys-s">{_gb(d["used"])} used of {_gb(d["total"])} · {used_share * 100:.0f}% full</div></div>')
        if d["level"] in ("warn", "bad"):
            alerts.append((d["level"],
                           f'<b>{html.escape(d["label"])}: {_gb(d["free"])} free.</b> '
                           + ("New downloads and plays will start failing. " if d["level"] == "bad" else
                              "Space is getting short. ")
                           + 'Free some under <a href="#files">Downloaded files</a> below, or set an automatic clean-up there.'))
    if host["mem"]:
        m = host["mem"]
        tiles.append(
            f'<div class="sys-t {m["level"]}"><div class="sys-l">🧠 Memory</div>'
            f'<div class="sys-n">{_gb(m["avail"])} <small>available</small></div>'
            f'{_bar(1 - m["avail"] / m["total"], m["level"])}'
            f'<div class="sys-s">of {_gb(m["total"])} in this machine</div></div>')
        if m["level"] == "bad":
            alerts.append(("bad", f'<b>Memory: only {_gb(m["avail"])} available.</b> '
                                  'Converting e-books and playing several files at once may fail.'))
    if host["load"]:
        ld = host["load"]
        tiles.append(
            f'<div class="sys-t {ld["level"]}"><div class="sys-l">⚙ Processor</div>'
            f'<div class="sys-n">{host["cores"]} <small>cores</small></div>'
            f'<div class="sys-s">load {ld["l1"]:.2f} · {ld["l5"]:.2f} · {ld["l15"]:.2f}'
            f'<br>(1, 5 and 15 minutes)</div></div>')
    tiles.append(
        f'<div class="sys-t"><div class="sys-l">🗂 Index</div>'
        f'<div class="sys-n">{_fmt_size(db_bytes) or "0"}</div>'
        f'<div class="sys-s">{idx_counts["ch"]:,} channels · {_fmt_count(idx_counts["msgs"])} posts<br>'
        f'downloaded files: {html.escape(cache_total)}</div></div>')
    tiles.append(
        f'<div class="sys-t"><div class="sys-l">🖥 System</div>'
        f'<div class="sys-n">{_fmt_uptime(host["uptime_s"])} <small>up</small></div>'
        f'<div class="sys-s">Linux {html.escape(host["kernel"])}<br>{html.escape(host["arch"])} · '
        f'tgarr {TGARR_VERSION}</div></div>')
    alerts.sort(key=lambda a: a[0] != "bad")
    machine_html = (
        "".join(f'<div class="sys-alert {lvl}">{txt}</div>' for lvl, txt in alerts)
        + f'<div class="sys">{"".join(tiles)}</div>')

    body = f"""
<style>
.settings h2.section {{ margin-top:32px; }}
.settings .dl-list {{ border-radius:12px; }}
.settings .dl-item {{ grid-template-columns:44px 1fr auto; gap:16px; padding:18px 20px; align-items:start; }}
.settings .dl-item .icon {{ width:44px; height:44px; border-radius:12px; background:rgba(127,127,127,.09);
  display:flex; align-items:center; justify-content:center; font-size:20px; }}
.settings .dl-item .info .name {{ font-size:15.5px; line-height:1.5; }}
.settings .dl-item .info .meta {{ font-size:13.5px; }}
.settings code {{ font-size:12.5px; padding:1px 6px; border-radius:5px; background:rgba(127,127,127,.12); }}
.acct {{ display:flex; align-items:center; gap:22px; padding:22px 24px; background:var(--surface);
  border:1px solid var(--border); border-radius:14px; box-shadow:var(--shadow); flex-wrap:wrap; }}
.acct-av {{ width:92px; height:92px; border-radius:50%; object-fit:cover; flex:none; display:flex;
  align-items:center; justify-content:center; color:#fff; font-size:38px; font-weight:700;
  box-shadow:0 0 0 4px var(--surface), 0 0 0 5px var(--border); }}
.acct-txt {{ flex:1; min-width:220px; }}
.acct-name {{ font-size:22px; font-weight:700; color:var(--fg); line-height:1.25; }}
.acct-sub {{ margin-top:4px; font-size:14px; color:var(--muted); display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
.acct-meta {{ margin-top:10px; font-size:13px; color:var(--muted); }}
.acct-act {{ flex:none; }}
.sys {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(165px, 1fr)); gap:12px; }}
.sys-t {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:14px 16px;
  box-shadow:var(--shadow); }}
.sys-t.warn {{ border-color:#fcd34d; background:#fffbeb; }}
.sys-t.bad {{ border-color:#fca5a5; background:#fef2f2; }}
.sys-l {{ font-size:12px; font-weight:600; letter-spacing:.05em; text-transform:uppercase; color:var(--muted); }}
.sys-n {{ font-size:24px; font-weight:700; color:var(--fg); margin:6px 0 8px; line-height:1.15; }}
.sys-n small {{ font-size:13px; font-weight:500; color:var(--muted); }}
.sys-t.warn .sys-n {{ color:#b45309; }} .sys-t.bad .sys-n {{ color:#b91c1c; }}
.sys-s {{ font-size:12.5px; color:var(--muted); line-height:1.5; }}
.sys-bar {{ height:6px; border-radius:3px; background:rgba(127,127,127,.18); overflow:hidden; margin-bottom:8px; }}
.sys-bar i {{ display:block; height:100%; border-radius:3px; }}
.sys-alert {{ padding:12px 16px; border-radius:10px; margin-bottom:12px; font-size:14px; line-height:1.55; }}
.sys-alert.warn {{ background:#fffbeb; border:1px solid #fde68a; color:#92400e; }}
.sys-alert.bad {{ background:#fef2f2; border:1px solid #fecaca; color:#991b1b; }}
.sys-alert a {{ color:inherit; font-weight:600; }}
@media (max-width:820px) {{ .acct {{ padding:16px; gap:14px; }} .acct-av {{ width:68px; height:68px; font-size:28px; }}
  .acct-name {{ font-size:19px; }} .settings .dl-item {{ grid-template-columns:36px 1fr; }}
  .settings .dl-item .icon {{ width:36px; height:36px; font-size:17px; }} .settings .dl-item .right {{ grid-column:1 / -1; }} }}
.settings .dl-item .meta {{ display:block; line-height:1.65; }}
.settings .dl-item .meta .pill {{ vertical-align:middle; }}
.settings input[type=number] {{ font:inherit; font-size:14px; padding:6px 8px; border:1px solid var(--border); border-radius:8px; background:var(--surface); color:var(--fg); text-align:center; }}
.settings details.adv {{ margin-top:26px; }}
.settings details.adv summary {{ cursor:pointer; font-size:13px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.07em; }}
.settings details.adv summary:hover {{ color:var(--accent); }}
</style>
<script>function tgCopy(b){{const v=b.dataset.v;const done=()=>{{const o=b.textContent;b.textContent="Copied";setTimeout(()=>b.textContent=o,1400)}};if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(v).then(done);else{{const t=document.createElement("textarea");t.value=v;document.body.appendChild(t);t.select();try{{document.execCommand("copy");done()}}catch(e){{}}t.remove()}}}}</script>
<div class="settings">
{account_html}

<h2 class="section">This machine</h2>
{machine_html}

<h2 class="section">Library</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">🎯</div>
    <div class="info">
      <div class="name">When a title has several versions, show first — <span class="pill ok">{'my data center' if version_pref == 'dc' else 'best quality'}</span></div>
      <div class="meta">
        The same film is often posted more than once: different resolutions, or
        the same file uploaded to different Telegram data centers. tgarr shows
        one card per title and keeps every version one click away.
        Your account is on <strong>DC {my_dc_disp}</strong>; a copy on that data
        center is your fastest path.
      </div>
      <form method="POST" action="/api/settings/version_preference" style="margin-top:12px;font-size:14px;line-height:1.7">
        <label style="display:block;cursor:pointer">
          <input type="radio" name="value" value="quality" {'checked' if version_pref != 'dc' else ''} onchange="this.form.submit()" />
          <strong>Best quality first</strong> — higher resolution wins; between equal resolutions, the copy on my data center wins.
        </label>
        <label style="display:block;cursor:pointer">
          <input type="radio" name="value" value="dc" {'checked' if version_pref == 'dc' else ''} onchange="this.form.submit()" />
          <strong>My data center first</strong> — a copy on my data center wins, then resolution.
        </label>
        <div style="color:var(--muted);font-size:12px;margin-top:6px">
          While Telegram is rate-limiting cross-data-center access, tgarr leads with your data center regardless of this choice.
        </div>
      </form>
    </div>
  </div>
  <div class="dl-item" id="cache">
    <div class="icon" id="files">💾</div>
    <div class="info">
      <div class="name">Downloaded files — <span class="pill ok">{cache_total}</span></div>
      <div class="meta">
        Opening a film, a song or a book downloads it here so it plays, and
        opens instantly next time. Saved items carry a
        <span class="pill ok" style="font-size:11px">● saved</span> mark in the lists.
        Now on disk: {cache_detail}.<br/>
        tgarr tidies up by itself: a file nobody opened for a while is removed
        (it stays in the index; opening it again fetches it again). Files
        grabbed through Sonarr/Radarr are never touched.
      </div>
      <form method="POST" action="/api/settings/cache" style="margin-top:12px;font-size:14px;line-height:2.2">
        Remove a file not opened for
        <input type="number" name="ttl_days" value="{cache_ttl}" min="0" style="width:70px" /> days,
        and keep the total under
        <input type="number" name="max_gb" value="{cache_gb}" min="0" style="width:70px" /> GB
        <span style="color:var(--muted);font-size:12px">(0 = no limit)</span>
        <button class="btn" type="submit" style="margin-left:8px">Save</button>
      </form>
      <form method="POST" action="/api/settings/cache_clear" style="margin-top:6px"
            onsubmit="return confirm('Remove every downloaded film, song and book from this server? They stay in the index and can be fetched again.')">
        <button class="ghost" type="submit">Remove all downloaded files now</button>
      </form>
    </div>
  </div>
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

<h2 class="section">Privacy</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">🔒</div>
    <div class="info">
      <div class="name">Private chats after signing out</div>
      <div class="meta">Signing out hides your private chats and keeps them on this machine, so that signing in again brings them
      straight back. Another Telegram account signing in here never sees them. Kept for
      <input id="pkDays" type="number" min="1" max="3650" style="width:80px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--fg)"/>
      days without a sign-in, then removed. <span id="pkMsg" style="color:var(--muted)"></span></div>
    </div>
    <div class="right"><button class="btn" onclick="pkSave()">Save</button></div>
  </div>
</div>
<script>
fetch("/api/settings/private_keep_days").then(r => r.json()).then(j => {{ document.getElementById("pkDays").value = j.days; }});
function pkSave(){{ fetch("/api/settings/private_keep_days", {{method: "POST", headers: {{"Content-Type": "application/json"}},
  body: JSON.stringify({{days: +document.getElementById("pkDays").value}})}}).then(r => r.json())
  .then(j => {{ document.getElementById("pkMsg").textContent = j.status === "ok" ? "saved" : (j.message || "not saved"); }}); }}
</script>
<h2 class="section">Other sources</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">📰</div>
    <div class="info">
      <div class="name">Usenet</div>
      <div class="meta">With a Usenet account, tgarr also indexes newsgroups and offers what it finds to Sonarr and Radarr.</div>
    </div>
    <div class="right"><a class="btn" href="/settings/usenet">Set up</a></div>
  </div>
</div>
<h2 class="section">Metadata sources</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">🔑</div>
    <div class="info">
      <div class="name">TMDB API key <span class="pill {('ok' if tmdb_raw else 'muted')}">{('configured' if tmdb_raw else 'not set — using Wikidata only')}</span></div>
      <div class="meta">Posters &amp; canonical titles from <a href="https://www.themoviedb.org/settings/api" target="_blank">themoviedb.org</a> (free, personal use). Without a key, titles are still recognised through Wikidata (free, no key), with fewer posters.</div>
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
        <span class="pill ok">Wikidata {meta_stats['wiki']:,}</span>
        <span class="pill warn">Not found {meta_stats['missed']:,}</span>
        <span class="pill muted">Pending {meta_stats['pending']:,}</span>
        / total {meta_stats['total']:,}
      </div>
    </div>
  </div>
</div>

<h2 class="section">Connect Sonarr / Radarr</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">🔌</div>
    <div class="info"><div class="name">Newznab indexer</div>
      <div class="meta">Settings → Indexers → + → Newznab → URL</div></div>
    <div class="right" style="display:flex;gap:8px;align-items:center"><code>{html.escape(_base_url(request))}/newznab/</code>
      <button class="ghost" onclick="tgCopy(this)" data-v="{html.escape(_base_url(request))}/newznab/">Copy</button></div>
  </div>
  <div class="dl-item">
    <div class="icon">⬇</div>
    <div class="info"><div class="name">SABnzbd download client</div>
      <div class="meta">Settings → Download Clients → + → SABnzbd → same host and port, URL Base</div></div>
    <div class="right" style="display:flex;gap:8px;align-items:center"><code>/sabnzbd</code>
      <button class="ghost" onclick="tgCopy(this)" data-v="/sabnzbd">Copy</button></div>
  </div>
</div>

<details class="adv"><summary>Advanced — values set when the containers were started</summary>
<div class="dl-list" style="margin-top:12px">
  <div class="dl-item">
    <div class="icon">⚙</div>
    <div class="info"><div class="name">Parser score threshold</div>
      <div class="meta">A file whose name scores below this is not treated as a film or an episode · TG_PARSE_SCORE_MIN</div></div>
    <div class="right"><code>{os.environ.get("TG_PARSE_SCORE_MIN","0.30")}</code></div>
  </div>
  <div class="dl-item">
    <div class="icon">📁</div>
    <div class="info"><div class="name">Download root</div>
      <div class="meta">Where grabbed files are written · TG_DOWNLOAD_ROOT</div></div>
    <div class="right"><code>{os.environ.get("TG_DOWNLOAD_ROOT","/downloads/tgarr")}</code></div>
  </div>
  <div class="dl-item">
    <div class="icon">🔢</div>
    <div class="info"><div class="name">Backfill limit per channel</div>
      <div class="meta">How far back a channel is read the first time · TG_BACKFILL_LIMIT</div></div>
    <div class="right"><code>{os.environ.get("TG_BACKFILL_LIMIT","5000")}</code></div>
  </div>
</div>
<div style="font-size:12.5px;color:var(--muted);margin:10px 4px">Change them in <code>docker-compose.yml</code> / <code>.env</code> and restart. Background jobs and their state: <a href="/system">System</a>.</div>
</details>

<h2 class="section">About</h2>
<div class="dl-list">
  <div class="dl-item">
    <div class="icon">ℹ</div>
    <div class="info"><div class="name">tgarr v{TGARR_VERSION}</div>
      <div class="meta">Self-hosted Telegram-as-source bridge · MIT license</div></div>
    <div class="right"><a class="btn ghost" href="https://tgarr.me" target="_blank">tgarr.me ↗</a></div>
  </div>
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
    _release_my_dc = await _get_my_dc()
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
    <tv-search       available="yes" supportedParams="q,season,ep"/>
    <movie-search    available="yes" supportedParams="q"/>
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
    <category id="8000" name="Other">
      <subcat id="8010" name="Other/Misc"/>
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
    return "8010"   # not classified as movie/tv → Other/Misc (0 is not a category)


def _cats_to_filter(cat: Optional[str]):
    """Map a Newznab `cat=` list to (allow_movie, allow_tv, allow_other).
    None = caller did not restrict categories."""
    if not cat:
        return None
    movie = tv = other = False
    for c in cat.split(","):
        c = c.strip()
        if not c.isdigit():
            continue
        n = int(c)
        if 2000 <= n < 3000:
            movie = True
        elif 5000 <= n < 6000:
            tv = True
        elif n >= 7000 or n == 0:
            other = True
    if not (movie or tv or other):
        return None
    return movie, tv, other


# Series alias map: rewrite non-English release names to the canonical
# English title Sonarr/Radarr knows. Universal across languages — table is
# populated from Sonarr/Radarr alternateTitles + manual fallbacks.
#
# `_series_aliases_cache` is loaded from DB on first use + refreshed every
# 60s. Lookup is substring match against release.name; first hit wins.
_series_aliases_cache: list = []  # list of (alias_text, canonical_title, season, kind)
_series_aliases_loaded_at: float = 0.0


def _alias_ok(alias: str) -> bool:
    """Is this alias specific enough to be matched against release names?

    Sonarr/Radarr alternateTitles contain junk ("1", "2", "U", "T2", "Ov").
    Matched as plain substrings they renamed almost every release in the feed
    (anything containing a "2" became "Swapped"). Reject: empty, digits-only,
    and short aliases — under 4 chars when ASCII-only, under 2 otherwise.
    """
    a = (alias or "").strip()
    if not a or a.isdigit():
        return False
    if a.isascii():
        return len(re.sub(r"[^A-Za-z0-9]", "", a)) >= 4
    return len(a) >= 2


def _alias_in_name(alias: str, raw: str) -> bool:
    """True if `alias` occurs in `raw` as a whole token run — not glued to a
    neighbouring letter/digit/CJK character on either side."""
    i = raw.find(alias)
    while i != -1:
        before = raw[i - 1] if i > 0 else ""
        j = i + len(alias)
        after = raw[j] if j < len(raw) else ""
        if not (before.isalnum() or after.isalnum()):
            return True
        i = raw.find(alias, i + 1)
    return False


def _alias_names_release(alias: str, raw: str) -> bool:
    """Does this alias name this release? A whole-token match - and, for a
    very short non-English alias, only at the start of the name: two-character
    titles are common (主角, 家业, 三体) but so are two-character words
    (生命 is 'Life' the series and also just 'life'), and '宇宙的奇迹' must
    not be offered as 'Cosmos'."""
    if not _alias_in_name(alias, raw):
        return False
    if alias.isascii() or len(alias) > 2:
        return True
    head = raw.lstrip("#[【(（ \t")
    if not head.startswith(alias):
        return False
    after = head[len(alias):len(alias) + 1]
    return not after.isalnum()


async def _refresh_series_aliases():
    """Reload alias map from DB. Called lazily — once per minute is plenty
    for newznab feed queries."""
    global _series_aliases_cache, _series_aliases_loaded_at
    if time.time() - _series_aliases_loaded_at < 60:
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT alias_text, canonical_title, season, kind FROM series_aliases "
            "ORDER BY length(alias_text) DESC")  # longest first → specific beats general
    _series_aliases_cache = [
        (r["alias_text"], r["canonical_title"], r["season"], r["kind"] or "tv")
        for r in rows if _alias_ok(r["alias_text"])]
    _series_aliases_loaded_at = time.time()


_OFFERED_SE_RX = re.compile(r"\.S(\d{1,3})E\d{1,4}")


def _offered_season(r) -> int:
    """The season a release is offered to an *arr under: its own; else the one
    in the name it is given on the way out (an alias can name a season, and a
    release without one goes out as season 1); else 1."""
    if r["season"] is not None:
        return int(r["season"])
    m = _OFFERED_SE_RX.search(_rewrite_release_title_sync(r))
    return int(m.group(1)) if m else 1


def _rewrite_release_title_sync(r, my_dc=None) -> str:
    """Outbound rewrite (release → newznab feed). Substring match against
    cached alias map. DC tag included as release-group suffix so Sonarr sees
    multi-DC variants as distinct candidates + can rank by quality/size."""
    raw = r["name"] or ""
    file_dc = r["file_dc"] if hasattr(r, "__getitem__") and "file_dc" in (r.keys() if hasattr(r, "keys") else []) else None
    dc_tag = f"-dc{file_dc}" if file_dc else ""
    for alias_text, canonical, season, kind in _series_aliases_cache:
        if alias_text in raw and _alias_names_release(alias_text, raw):
            if kind == "movie" and r["category"] == "tv":
                continue   # a movie's alias must not rename a TV episode
            if kind != "movie" and r["category"] == "movie":
                continue
            # Only state what the release actually told us. A missing quality
            # is left out (the *arr parses it as Unknown) rather than being
            # advertised as 1080p.
            qual = f".{r['quality']}" if r["quality"] else ""
            canon = re.sub(r"\s+", ".", canonical.strip())
            if kind == "movie" or r["category"] == "movie":
                yr = r["movie_year"] if "movie_year" in r.keys() else None
                yr_s = f".{int(yr)}" if yr else ""
                return f"{canon}{yr_s}{qual}.WEBRip-tgarr{dc_tag}"
            if r["episode"] is None:
                # An alias hit with no parsed episode is not a single episode;
                # don't claim E01. Expose it under its own name instead.
                break
            seas = season if season is not None else (r["season"] or 1)
            return (f"{canon}.S{int(seas):02d}E{int(r['episode']):02d}"
                    f"{qual}.WEBRip-tgarr{dc_tag}")
    # No alias match — append DC tag to raw name so multi-DC duplicates differ
    if dc_tag and file_dc:
        return f"{raw}.tgarr{dc_tag}"
    return raw


def _expand_inbound_query(q: str) -> list:
    """Inbound: Sonarr searches 'A Bite of China'. Return all alias variants
    so SQL ILIKE matches Chinese releases too. Always includes the original."""
    variants = {q}
    ql = " ".join(q.lower().split())
    # The canonical title must BE the query (optionally followed by a year).
    # Substring matching in either direction made short queries ("her", "up",
    # "it") pull in the aliases of every title containing those letters.
    ql_noyear = re.sub(r"\s+(19|20)\d{2}$", "", ql)
    for alias_text, canonical, _s, _k in _series_aliases_cache:
        cl = " ".join(canonical.lower().split())
        if cl == ql or cl == ql_noyear:
            variants.add(alias_text)
    return list(variants)


ALIAS_LOOKUPS_PER_RUN = 40


def _names_to_aliases(names, canonical: str) -> list:
    """The names worth matching release names against: specific enough
    (_alias_ok), not the canonical title itself, not absurdly long."""
    canon = (canonical or "").strip().casefold()
    out = []
    for n in sorted(names):
        n = (n or "").strip()
        if n and n.casefold() != canon and len(n) <= 80 and _alias_ok(n):
            out.append(n)
    return out


async def _alias_names_from_metadata(series: list) -> int:
    """Other-language names for the series Sonarr tracks, by TVDB number.
    Each series is asked about once (again after two weeks if nothing was
    found, or sooner after a failed request). A name never replaces an alias
    that came from Sonarr, Radarr or was entered by hand."""
    # newest first: the series just added is the one being searched for now
    known = [(int(s["tvdbId"]), s.get("title") or "")
             for s in sorted(series, key=lambda s: s.get("id") or 0, reverse=True)
             if s.get("tvdbId") and s.get("title")]
    if not known:
        return 0
    async with db_pool.acquire() as conn:
        done = {r["tvdb_id"] for r in await conn.fetch(
            """SELECT tvdb_id FROM series_alias_lookups
               WHERE names > 0 OR looked_at > NOW() - INTERVAL '14 days'""")}
        key = await conn.fetchval("SELECT value FROM config WHERE key='tmdb_api_key'")
    added = 0
    for tvdb_id, canonical in [k for k in known if k[0] not in done][:ALIAS_LOOKUPS_PER_RUN]:
        try:
            names, source = await md.series_names(tvdb_id, key or None)
        except Exception as e:
            logging.getLogger("tgarr.alias").info("names for tvdb %s: %s - later", tvdb_id, str(e)[:80])
            await asyncio.sleep(2)
            continue
        aliases = _names_to_aliases(names, canonical)
        async with db_pool.acquire() as conn:
            for a in aliases:
                r = await conn.execute(
                    """INSERT INTO series_aliases (alias_text, canonical_title, season, kind, source, external_id)
                       VALUES ($1, $2, NULL, 'tv', $3, $4) ON CONFLICT (alias_text) DO NOTHING""",
                    a, canonical, source, tvdb_id)
                added += r.endswith("1")
            await conn.execute(
                """INSERT INTO series_alias_lookups (tvdb_id, names, source) VALUES ($1, $2, $3)
                   ON CONFLICT (tvdb_id) DO UPDATE SET looked_at = NOW(), names = $2, source = $3""",
                tvdb_id, len(aliases), source)
        await asyncio.sleep(0.4)
    return added


async def _arr_alias_sync_worker():
    """Pull Sonarr+Radarr series + alternateTitles, UPSERT into series_aliases.
    Runs hourly. Endpoints + keys from env. Failures non-fatal — manual
    aliases (source='manual') always remain in DB.
    """
    await asyncio.sleep(60)  # let startup settle
    SONARR_URL = os.environ.get("SONARR_URL", "http://host.docker.internal:8989")
    SONARR_KEY = os.environ.get("SONARR_API_KEY", "")
    RADARR_URL = os.environ.get("RADARR_URL", "http://host.docker.internal:7878")
    RADARR_KEY = os.environ.get("RADARR_API_KEY", "")
    while True:
        try:
            inserted = 0
            for app_name, url, key, endpoint, kind in (
                ("sonarr", SONARR_URL, SONARR_KEY, "series", "tv"),
                ("radarr", RADARR_URL, RADARR_KEY, "movie", "movie"),
            ):
                if not key:
                    continue
                def _fetch():
                    req = urllib.request.Request(
                        f"{url}/api/v3/{endpoint}",
                        headers={"X-Api-Key": key})
                    try:
                        with urllib.request.urlopen(req, timeout=30) as r:
                            return _json.loads(r.read())
                    except Exception as e:
                        logging.getLogger("tgarr.alias").warning(
                            "%s fetch failed: %s", app_name, e)
                        return []
                items = await asyncio.to_thread(_fetch)
                if kind == "tv" and items:
                    try:
                        n_meta = await _alias_names_from_metadata(items)
                        if n_meta:
                            logging.getLogger("tgarr.alias").info(
                                "%d other-language series names added", n_meta)
                            global _series_aliases_loaded_at
                            _series_aliases_loaded_at = 0.0      # use them at once
                    except Exception as e:
                        logging.getLogger("tgarr.alias").warning("series names: %s", e)
                aliases = []
                for it in items:
                    canonical = it.get("title") or ""
                    ext_id = it.get("tvdbId") or it.get("tmdbId") or 0
                    if not canonical:
                        continue
                    # Sonarr/Radarr expose alternateTitles → list of {title, ...}
                    for alt in it.get("alternateTitles", []):
                        alias_text = (alt.get("title") or "").strip()
                        if not alias_text or alias_text == canonical:
                            continue
                        season = alt.get("seasonNumber")
                        if season is not None and season < 0:
                            season = None
                        if _alias_ok(alias_text):
                            aliases.append((alias_text, canonical, season, kind, app_name, ext_id))
                if aliases:
                    async with db_pool.acquire() as conn:
                        await conn.executemany(
                            """INSERT INTO series_aliases
                                 (alias_text, canonical_title, season, kind, source, external_id)
                               VALUES ($1,$2,$3,$4,$5,$6)
                               ON CONFLICT (alias_text) DO UPDATE SET
                                 canonical_title=EXCLUDED.canonical_title,
                                 season=EXCLUDED.season,
                                 source=CASE
                                   WHEN series_aliases.source='manual' THEN 'manual'
                                   ELSE EXCLUDED.source END,
                                 external_id=EXCLUDED.external_id""",
                            aliases)
                    inserted += len(aliases)
            if inserted:
                logging.getLogger("tgarr.alias").info(
                    "synced %d aliases from arr APIs", inserted)
        except Exception as e:
            logging.getLogger("tgarr.alias").warning("sync loop error: %s", e)
        await asyncio.sleep(3600)


def _item_xml(r, base_url: str, apikey: str, my_dc=None) -> str:
    guid = str(r["guid"])
    link = f"{base_url}/newznab/api?t=get&id={guid}&apikey={apikey}"
    # XML 1.0 requires & to be escaped as &amp; even inside element body —
    # Sonarr's strict XDocument parser bails out on raw &id= otherwise.
    link_text = html.escape(link)
    link_attr = html.escape(link, quote=True)
    pubdate = (r["posted_at"].strftime("%a, %d %b %Y %H:%M:%S +0000")
               if r["posted_at"]
               else time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(0)))
    size = r["size_bytes"] or 0
    cat_id = _category_id(r)
    title = html.escape(_rewrite_release_title_sync(r, my_dc=my_dc))
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


def _newznab_error(code: int, desc: str, status: int = 200) -> Response:
    """Newznab-style error document (the *arrs expect XML, not plain text)."""
    return Response(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<error code="{code}" description="{html.escape(desc, quote=True)}"/>',
        media_type="application/xml", status_code=status)


def _feed_envelope(items_xml: str, title: str, base_url: str,
                   offset: int = 0, total: int = 0) -> str:
    return (
        f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        f"<rss version=\"2.0\" xmlns:newznab=\"http://www.newznab.com/DTD/2010/feeds/attributes/\">\n"
        f"  <channel>\n"
        f"    <title>tgarr</title>\n"
        f"    <description>{html.escape(title)}</description>\n"
        f"    <link>{base_url}/newznab/api</link>\n"
        f"    <newznab:response offset=\"{offset}\" total=\"{total}\"/>\n"
        f"{items_xml}\n"
        f"  </channel>\n"
        f"</rss>"
    )


@app.get("/newznab/api", response_class=PlainTextResponse)
@app.get("/newznab", response_class=PlainTextResponse)
async def newznab_api(
    request: Request,
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
    base_url = _base_url(request)
    if t == "caps":
        return Response(CAPS_XML, media_type="application/xml")
    if t not in ("search", "tvsearch", "movie", "get"):
        return _newznab_error(202, f"No such function ({t})")

    if t == "get":
        return await _handle_grab(id)

    # Refresh alias map (cached 60s) — used for both inbound query expansion
    # and outbound release-name rewrite via _item_xml→_rewrite_release_title_sync.
    await _refresh_series_aliases()

    # If Sonarr/Radarr passed tvdbid/tmdbid (the canonical way they query),
    # resolve to canonical_title via series_aliases.external_id then synthesize q.
    # IDs are no longer advertised in caps (so the *arrs send q=<title>), but
    # still honoured when a client sends one. An ID we cannot resolve must
    # return an EMPTY feed — falling through used to return the newest
    # releases of the whole category as if they matched.
    if not q and (tvdbid or tmdbid or imdbid):
        canonical = None
        ext_id = tvdbid or tmdbid
        if ext_id and str(ext_id).isdigit():
            kind = "tv" if tvdbid else "movie"
            async with db_pool.acquire() as conn:
                canonical = await conn.fetchval(
                    "SELECT canonical_title FROM series_aliases "
                    "WHERE external_id = $1 AND kind = $2 LIMIT 1",
                    int(ext_id), kind)
        if not canonical:
            return Response(
                _feed_envelope("", f"search t={t} (unresolved id)", base_url),
                media_type="application/xml")
        q = canonical

    where_parts = ["1=1"]
    params = []

    if q:
        # Inbound query expansion: Sonarr asks "A Bite of China" → also match
        # any release containing aliased non-English title like "舌尖上的中国".
        # Each variant becomes a separate OR group; words within a variant are AND.
        variants = _expand_inbound_query(q)
        or_groups = []
        for variant in variants:
            and_parts = []
            for word in variant.split():
                params.append(f"%{word}%")
                and_parts.append(f"r.name ILIKE ${len(params)}")
            if and_parts:
                or_groups.append("(" + " AND ".join(and_parts) + ")")
        if or_groups:
            where_parts.append("(" + " OR ".join(or_groups) + ")")

    if t == "tvsearch":
        where_parts.append("r.category = 'tv'")
        if season is not None:
            # A release with no season of its own ("第 12 集", "EP12" - most
            # Chinese series) is offered as season 1, or as the season its
            # alias names; it must be findable under that season too. Sonarr
            # always asks with a season, so "r.season = N" alone hid them all.
            params.append(season)
            where_parts.append(f"(r.season = ${len(params)} OR r.season IS NULL)")
        if ep is not None:
            params.append(ep)
            where_parts.append(f"r.episode = ${len(params)}")
    elif t == "movie":
        where_parts.append("r.category = 'movie'")
    else:
        # Generic search / RSS sync. Honour the categories the client asked
        # for. With no cat= at all, serve movies + TV only: unclassified
        # releases are opt-in (cat=8000) so they don't flood an *arr's feed.
        cf = _cats_to_filter(cat) or (True, True, False)
        allowed = [c for c, on in zip(("movie", "tv"), cf[:2]) if on]
        conds = []
        if allowed:
            conds.append("r.category IN (" + ",".join(f"'{c}'" for c in allowed) + ")")
        if cf[2]:
            conds.append("(r.category IS NULL OR r.category NOT IN ('movie','tv'))")
        where_parts.append("(" + " OR ".join(conds) + ")")

    # Adult content never goes to an *arr unless the operator opts in
    # (config key newznab_nsfw=true). Two layers: the channel's audience, and
    # the release name itself for adult posts inside otherwise mixed channels.
    async with db_pool.acquire() as conn:
        _nz_nsfw = (await conn.fetchval(
            "SELECT value FROM config WHERE key='newznab_nsfw'") or "").lower() == "true"
    if not _nz_nsfw:
        where_parts.append("COALESCE(c.audience, 'sfw') NOT IN ('nsfw', 'blocked_csam')")
        params.append(ADULT_RX)
        where_parts.append(f"r.name !~* ${len(params)}")

    limit_val = max(1, min(limit, 200))
    _release_my_dc = await _get_my_dc()
    # JOIN messages to get file_dc; same-DC releases ranked higher so Sonarr's
    # release picker prefers fast downloads. Same content uploaded to multiple
    # channels/DCs naturally surfaces as multiple candidates (we don't dedup
    # by file_unique_id — each is a valid alternative source).
    sql = (f"SELECT r.id, r.guid, r.name, r.category, r.season, r.episode, "
          f"r.quality, r.source, r.codec, r.size_bytes, r.posted_at, "
          f"r.movie_year, m.file_dc, count(*) OVER() AS _total "
          f"FROM releases r LEFT JOIN messages m ON m.id = r.primary_msg_id "
          f"LEFT JOIN channels c ON c.id = m.channel_id "
          f"WHERE {' AND '.join(where_parts)} "
          f"ORDER BY (CASE WHEN m.file_dc = ${len(params)+1} THEN 0 ELSE 1 END), "
          f"  r.posted_at DESC NULLS LAST "
          f"LIMIT {limit_val} OFFSET {max(0, offset)}")
    params.append(_release_my_dc or -1)
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    total = rows[0]["_total"] if rows else 0
    if t == "tvsearch" and season is not None:
        rows = [r for r in rows if _offered_season(r) == season]
        total = max(0, offset) + len(rows)
    items = "\n".join(_item_xml(r, base_url, apikey or "tgarr",
                                 my_dc=_release_my_dc) for r in rows)
    return Response(
        _feed_envelope(items, f"search t={t} q={q or ''}", base_url,
                       offset=max(0, offset), total=total),
        media_type="application/xml",
    )


async def _handle_grab(guid: Optional[str]) -> Response:
    if not guid:
        return _newznab_error(200, "Missing parameter (id)", 400)
    if not _GUID_RX.fullmatch(guid):
        return _newznab_error(300, "No such item", 404)
    async with db_pool.acquire() as conn:
        rel = await conn.fetchrow(
            "SELECT id, name FROM releases WHERE guid = $1", guid)
        if not rel:
            return _newznab_error(300, "No such item", 404)
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
    # HTTP headers are latin-1 only; sanitize the filename (release name can
    # contain CJK/Cyrillic/Arabic). Use ASCII-safe fallback + RFC 5987
    # `filename*` for the original UTF-8 name.
    raw_name = rel["name"]
    safe_name = "".join(c if ord(c) < 128 else "_" for c in raw_name)[:120]
    quoted_utf8 = urllib.parse.quote(raw_name, safe="")
    return Response(
        stub,
        media_type="application/x-nzb",
        headers={"Content-Disposition":
                 f'attachment; filename="{safe_name}.nzb"; '
                 f"filename*=UTF-8''{quoted_utf8}.nzb"},
    )


# ════════════════════════════════════════════════════════════════════
# SABnzbd download-client emulation
# ════════════════════════════════════════════════════════════════════
@app.get("/sabnzbd/api")
@app.get("/api")
@app.post("/sabnzbd/api")  # Sonarr posts multipart for mode=addfile
@app.post("/api")
async def sab_api(
    request: Request,
    mode: str = Query("queue"),
    name: Optional[str] = None,
    value: Optional[str] = None,
    cat: Optional[str] = None,
    nzbname: Optional[str] = None,
    apikey: Optional[str] = None,
    output: str = Query("json"),
):
    # Sonarr mode=addfile sends the NZB content as multipart; we don't actually
    # need to parse the NZB body — the release guid is in the nzb's <subject>
    # but more reliably we encode it into the filename when generating the stub.
    # For our purposes, extract the filename and grab via its guid.
    if mode == "addfile" and request.method == "POST":
        try:
            form = await request.form()
            file = form.get("nzbfile") or form.get("name")
            # Read NZB body — our stub embeds `tgarr-<guid>` in <segment>.
            # That's how we recover the original release identity even when
            # Sonarr renamed the file via alias rewrite.
            nzb_bytes = b""
            if file and hasattr(file, "read"):
                nzb_bytes = await file.read()
            elif isinstance(file, str):
                nzb_bytes = file.encode()
            m = re.search(rb"tgarr-([0-9a-f-]{36})", nzb_bytes)
            if m:
                guid = m.group(1).decode()
                fname = getattr(file, "filename", None) or ""
                if fname.endswith(".nzb"):
                    fname = fname[:-4]
                return await _sab_addurl(guid, fname, cat)
            return JSONResponse({"status": False,
                                 "error": "no tgarr-guid found in NZB body"})
        except Exception as e:
            return JSONResponse({"status": False, "error": str(e)[:200]})
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
            """SELECT d.id, d.status, r.name, r.size_bytes, r.guid,
                      d.bytes_done, d.speed_kbps, d.last_progress_at,
                      COALESCE(d.sab_cat,
                               CASE r.category WHEN 'movie' THEN 'movies' ELSE 'tv' END) AS sab_cat
               FROM downloads d JOIN releases r ON r.id = d.release_id
               WHERE d.status IN ('pending','downloading')
               ORDER BY d.requested_at""")
    slots = []
    total_kbps = 0
    total_mbleft = 0.0
    for i, r in enumerate(rows):
        size_b = r["size_bytes"] or 0
        done_b = r["bytes_done"] or 0
        left_b = max(0, size_b - done_b)
        kbps = r["speed_kbps"] or 0
        pct = round(done_b * 100 / size_b, 1) if size_b else 0
        # ETA = left_b / (kbps * 1024). Format as H:MM:SS.
        eta_sec = int(left_b / max(kbps * 1024, 1)) if kbps else 0
        eta_str = (f"{eta_sec//3600}:{(eta_sec%3600)//60:02d}:{eta_sec%60:02d}"
                   if eta_sec < 86400*7 else "0:00:00")
        slots.append({
            "nzo_id": f"tgarr-{r['guid']}",
            "filename": r["name"],
            "cat": r["sab_cat"],
            "status": "Downloading" if r["status"] == "downloading" else "Queued",
            "mb": f"{size_b/1048576:.1f}",
            "mbleft": f"{left_b/1048576:.1f}",
            "percentage": str(int(pct)),
            "size": f"{size_b/1048576:.1f} MB",
            "sizeleft": f"{left_b/1048576:.1f} MB",
            "timeleft": eta_str,
            "priority": "Normal",
            "script": "None",
            "msgid": "",
            "index": i,
        })
        if r["status"] == "downloading":
            total_kbps += kbps
            total_mbleft += left_b / 1048576
    total_eta = (f"{int(total_mbleft*1024/max(total_kbps,1))//3600}:00:00"
                 if total_kbps else "0:00:00")
    return {"queue": {
        "status": "Idle" if not slots else "Downloading",
        "version": "3.7.2", "paused": False,
        "noofslots_total": len(slots), "noofslots": len(slots),
        "slots": slots, "speedlimit": "", "speedlimit_abs": "",
        "speed": f"{total_kbps/1024:.1f} M" if total_kbps > 1024 else f"{total_kbps:.0f} K",
        "kbpersec": str(total_kbps),
        "size": f"{total_mbleft:.1f} MB", "sizeleft": f"{total_mbleft:.1f} MB",
        "mb": f"{total_mbleft:.0f}", "mbleft": f"{total_mbleft:.0f}",
        "diskspace1": "1000", "diskspace2": "1000",
        "diskspace1_norm": "1 T", "diskspace2_norm": "1 T",
        "diskspacetotal1": "2000", "diskspacetotal2": "2000",
        "limit": "0", "have_warnings": "0", "pause_int": "0", "loadavg": "",
    }}


async def _sab_history():
    async with db_pool.acquire() as conn:
        # One row per release. Completed wins over Failed regardless of which
        # is newer — once a release has been successfully downloaded, that's
        # the authoritative state for CDH. Otherwise newer failure wins.
        rows = await conn.fetch(
            """SELECT * FROM (
                 SELECT DISTINCT ON (d.release_id)
                        d.id, d.status, d.local_path, d.finished_at,
                        r.name, r.size_bytes, r.guid, d.error_message,
                        COALESCE(d.sab_cat,
                                 CASE r.category WHEN 'movie' THEN 'movies' ELSE 'tv' END) AS sab_cat
                 FROM downloads d JOIN releases r ON r.id = d.release_id
                 WHERE d.status IN ('completed','failed')
                 ORDER BY d.release_id,
                          CASE d.status WHEN 'completed' THEN 0 ELSE 1 END,
                          d.id DESC NULLS LAST
               ) h
               -- newest first: the old LIMIT ran over release_id order, so
               -- once there were 200+ rows new completions never appeared and
               -- were never imported.
               ORDER BY h.finished_at DESC NULLS LAST, h.id DESC
               LIMIT 200""")
    slots = []
    for r in rows:
        size_mb = (r["size_bytes"] or 0) / 1048576
        # SAB API: storage is the COMPLETED DIRECTORY containing files (not the
        # file itself). Sonarr's CDH scans this dir to find media to import.
        if r["local_path"]:
            storage = os.path.dirname(r["local_path"]) or r["local_path"]
        else:
            storage = "/downloads/tgarr"
        slots.append({
            "nzo_id": f"tgarr-{r['guid']}",
            "name": r["name"], "category": r["sab_cat"],
            "storage": storage,
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
    elif re.fullmatch(r"[0-9a-f-]{36}", url or ""):
        # Caller passed bare guid (from addfile NZB-body parse path).
        guid = url
    if not guid or not _GUID_RX.fullmatch(guid):
        return {"status": False, "error": "no guid in url"}
    sab_cat = (cat or "").strip()[:40] or None
    async with db_pool.acquire() as conn:
        rel = await conn.fetchrow("SELECT id FROM releases WHERE guid = $1", guid)
        if not rel:
            return {"status": False, "error": "release not found"}
        await conn.execute(
            """INSERT INTO downloads (release_id, status, sab_cat)
               SELECT $1, 'pending', $2
               WHERE NOT EXISTS (
                 SELECT 1 FROM downloads
                 WHERE release_id = $1
                   AND status IN ('pending','downloading','completed')
               )""", rel["id"], sab_cat)
    return {"status": True, "nzo_ids": [f"tgarr-{guid}"]}


async def _sab_delete(nzo_id: str):
    if not nzo_id.startswith("tgarr-") or not _GUID_RX.fullmatch(nzo_id[6:]):
        return {"status": False, "error": "bad nzo_id"}
    guid = nzo_id[6:]
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE downloads SET status='cancelled'
               WHERE release_id = (SELECT id FROM releases WHERE guid = $1)
                 AND status IN ('pending','downloading')""", guid)
    return {"status": True}
