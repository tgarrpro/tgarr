"""Read-only operator panel for the registry: /admin/

Off unless a config file exists (ADMIN_CONFIG, default /app/admin.json):

    {"secret": "<random, signs the session cookie>",
     "password_hash": "scrypt$<salt hex>$<hash hex>",        # password sign-in
     "google": {"client_id": "...", "client_secret": "..."}, # or Google sign-in
     "emails": ["someone@example.com"]}                       # who may sign in with Google

Make one with:  python admin.py init        (writes a new password to admin.json.password)

With Google configured, Google is the only way in; otherwise the password.
Nothing here writes to the registry's data. Every query is cached, so a
panel left open costs the database next to nothing. Contributors appear only
as the hashes the registry already stores - there is nothing else to show.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

CONFIG_PATH = os.environ.get("ADMIN_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.json"))
COOKIE = "tgarr_admin"
SESSION_HOURS = 72
PULL_CAP = 24

router = APIRouter()
_started = time.time()
_ctx: dict = {"pool": None, "version": "", "ip_hash": None}
_cfg_cache: dict = {"mtime": None, "cfg": None}
_fails: dict = {}          # ip hash -> [timestamps of failed sign-ins]
_cache: dict = {}          # key -> (expires, value)
_locks: dict = {}


def setup(app, get_pool, version: str, ip_hash) -> None:
    _ctx.update(pool=get_pool, version=version, ip_hash=ip_hash)
    app.include_router(router)
    # monitoring runs whether or not the panel is switched on: when it is
    # switched on later, the history is already there
    app.middleware("http")(_metrics_mw)


# ── config ────────────────────────────────────────────────────────────
def _cfg() -> Optional[dict]:
    try:
        m = os.stat(CONFIG_PATH).st_mtime
    except OSError:
        return None
    if _cfg_cache["mtime"] != m:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                c = json.load(f)
        except (OSError, ValueError):
            return None
        if not c.get("secret") or len(c["secret"]) < 32:
            return None
        if not c.get("password_hash") and not (c.get("google") or {}).get("client_id"):
            return None
        _cfg_cache.update(mtime=m, cfg=c)
    return _cfg_cache["cfg"]


def _need_cfg() -> dict:
    c = _cfg()
    if not c:
        raise HTTPException(404, "Not Found")
    return c


def _uses_google(c: dict) -> bool:
    g = c.get("google") or {}
    return bool(g.get("client_id") and g.get("client_secret") and c.get("emails"))


# ── passwords and cookies ─────────────────────────────────────────────
def hash_password(pw: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def _check_password(pw: str, stored: str) -> bool:
    try:
        kind, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        got = hash_password(pw, bytes.fromhex(salt)).split("$")[2]
        return hmac.compare_digest(got, want)
    except (ValueError, AttributeError):
        return False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(c: dict, payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new(c["secret"].encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + mac


def _unsign(c: dict, token: str) -> Optional[dict]:
    try:
        body, mac = token.split(".")
        want = hmac.new(c["secret"].encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, want):
            return None
        p = json.loads(_unb64(body))
        return p if p.get("exp", 0) > time.time() else None
    except (ValueError, TypeError):
        return None


def _user(request: Request) -> Optional[str]:
    c = _cfg()
    if not c:
        return None
    p = _unsign(c, request.cookies.get(COOKIE, ""))
    return p.get("u") if p and p.get("k") == "s" else None


def _auth(request: Request) -> str:
    _need_cfg()
    u = _user(request)
    if not u:
        raise HTTPException(401, "sign in")
    return u


def _headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'")
    return resp


def _set_session(resp, c: dict, who: str):
    resp.set_cookie(COOKIE, _sign(c, {"k": "s", "u": who, "exp": int(time.time()) + SESSION_HOURS * 3600}),
                    max_age=SESSION_HOURS * 3600, path="/admin", httponly=True, secure=True, samesite="lax")
    return resp


def _throttled(key: str) -> bool:
    now = time.time()
    lst = [t for t in _fails.get(key, []) if now - t < 900]
    _fails[key] = lst
    if len(_fails) > 5000:
        _fails.clear()
    return len(lst) >= 5


# ── sign-in pages ─────────────────────────────────────────────────────
_LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>tgarr.me · admin</title>
<style>body{font:15px/1.5 -apple-system,system-ui,sans-serif;background:#f4f6f9;color:#0f172a;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}
.box{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:28px;width:320px}
h1{font-size:18px;margin:0 0 4px}p{color:#64748b;margin:0 0 18px;font-size:13px}
input{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #cbd5e1;border-radius:8px;font:inherit}
button,a.b{display:block;width:100%;box-sizing:border-box;margin-top:12px;padding:10px;border:0;border-radius:8px;
background:#229ed9;color:#fff;font:inherit;font-weight:600;cursor:pointer;text-align:center;text-decoration:none}
.e{color:#b91c1c;font-size:13px;margin-top:10px}</style></head><body><div class="box">
<h1>tgarr.me registry</h1><p>Operator sign-in. Read-only.</p>__BODY____ERR__</div></body></html>"""


def _login_page(c: dict, err: str = "") -> HTMLResponse:
    if _uses_google(c):
        body = '<a class="b" href="/admin/auth/start">Sign in with Google</a>'
    else:
        body = ('<form method="post" action="/admin/login"><input type="password" name="password" '
                'placeholder="password" autofocus autocomplete="current-password">'
                '<button type="submit">Sign in</button></form>')
    e = f'<div class="e">{err}</div>' if err else ""
    return _headers(HTMLResponse(_LOGIN_HTML.replace("__BODY__", body).replace("__ERR__", e)))


@router.get("/admin")
async def admin_bare():
    _need_cfg()
    return RedirectResponse("/admin/", status_code=302)


@router.get("/admin/login")
async def login_form(request: Request):
    c = _need_cfg()
    if _user(request):
        return RedirectResponse("/admin/", status_code=302)
    return _login_page(c)


@router.post("/admin/login")
async def login_post(request: Request, password: str = Form("")):
    c = _need_cfg()
    if _uses_google(c) or not c.get("password_hash"):
        raise HTTPException(404, "Not Found")
    key = _ctx["ip_hash"](request)
    if _throttled(key):
        return _login_page(c, "Too many attempts. Try again in 15 minutes.")
    ok = await asyncio.to_thread(_check_password, password, c["password_hash"])
    if not ok:
        _fails.setdefault(key, []).append(time.time())
        await asyncio.sleep(1)
        return _login_page(c, "Wrong password.")
    return _set_session(_headers(RedirectResponse("/admin/", status_code=303)), c, "operator")


@router.post("/admin/logout")
async def logout():
    _need_cfg()
    r = _headers(RedirectResponse("/admin/login", status_code=303))
    r.delete_cookie(COOKIE, path="/admin")
    return r


# Google sign-in: authorisation-code flow with state, nonce and PKCE. The
# id_token is read from Google's token endpoint over TLS (not from the
# browser), which is the case where OpenID Connect lets a client rely on the
# TLS connection instead of checking the token's signature; issuer, audience,
# expiry, nonce and a verified e-mail on the allow-list are all checked.
def _redirect_uri(request: Request) -> str:
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.url.netloc
    return f"https://{host}/admin/auth/callback"


@router.get("/admin/auth/start")
async def google_start(request: Request):
    c = _need_cfg()
    if not _uses_google(c):
        raise HTTPException(404, "Not Found")
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    q = urllib.parse.urlencode({
        "client_id": c["google"]["client_id"], "redirect_uri": _redirect_uri(request),
        "response_type": "code", "scope": "openid email", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"})
    r = _headers(RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + q, status_code=302))
    r.set_cookie(COOKIE + "_o", _sign(c, {"k": "o", "s": state, "n": nonce, "v": verifier,
                                          "exp": int(time.time()) + 600}),
                 max_age=600, path="/admin", httponly=True, secure=True, samesite="lax")
    return r


def _google_exchange(c: dict, code: str, verifier: str, redirect_uri: str) -> dict:
    data = urllib.parse.urlencode({
        "code": code, "client_id": c["google"]["client_id"], "client_secret": c["google"]["client_secret"],
        "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        tok = json.loads(resp.read())
    return json.loads(_unb64(tok["id_token"].split(".")[1]))


@router.get("/admin/auth/callback")
async def google_callback(request: Request, code: str = "", state: str = ""):
    c = _need_cfg()
    if not _uses_google(c):
        raise HTTPException(404, "Not Found")
    o = _unsign(c, request.cookies.get(COOKIE + "_o", ""))
    if not o or o.get("k") != "o" or not code or not hmac.compare_digest(str(o.get("s")), state):
        return _login_page(c, "Sign-in expired. Try again.")
    try:
        claims = await asyncio.to_thread(_google_exchange, c, code, o["v"], _redirect_uri(request))
    except Exception:
        return _login_page(c, "Google sign-in failed. Try again.")
    email = str(claims.get("email", "")).lower()
    ok = (claims.get("iss") in ("https://accounts.google.com", "accounts.google.com")
          and claims.get("aud") == c["google"]["client_id"]
          and float(claims.get("exp", 0)) > time.time()
          and hmac.compare_digest(str(claims.get("nonce", "")), str(o.get("n")))
          and claims.get("email_verified") in (True, "true")
          and email in [e.lower() for e in c.get("emails", [])])
    if not ok:
        return _login_page(c, "That account may not use this panel.")
    r = _set_session(_headers(RedirectResponse("/admin/", status_code=303)), c, email)
    r.delete_cookie(COOKIE + "_o", path="/admin")
    return r


# ── data (read-only, cached) ──────────────────────────────────────────
async def _cached(key: str, ttl: int, fn):
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        val = await fn()
        _cache[key] = (time.time() + ttl, val)
        if len(_cache) > 300:
            for k in [k for k, v in _cache.items() if v[0] < time.time()]:
                _cache.pop(k, None)
        return val


async def _q(sql: str, *args, timeout_s: int = 20):
    """Rows as plain dicts, inside a read-only transaction with a time limit."""
    async with _ctx["pool"]().acquire() as conn:
        async with conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_s) * 1000}")
            rows = await conn.fetch(sql, *args)
    out = []
    for r in rows:
        d = {}
        for k, v in r.items():
            d[k] = v.isoformat() if hasattr(v, "isoformat") else (float(v) if type(v).__name__ == "Decimal" else v)
        out.append(d)
    return out


async def _overview():
    ch = (await _q("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE health_status = 'alive' AND NOT blocked) AS alive,
               count(*) FILTER (WHERE verified AND NOT blocked) AS verified,
               count(*) FILTER (WHERE blocked) AS blocked,
               count(*) FILTER (WHERE audience = 'nsfw' AND NOT blocked) AS nsfw,
               count(*) FILTER (WHERE NOT COALESCE(seeded, false)) AS contributed,
               count(*) FILTER (WHERE seeded) AS seeded,
               count(*) FILTER (WHERE COALESCE(release_files, 0) > 0) AS with_releases,
               count(*) FILTER (WHERE first_seen_at > NOW() - INTERVAL '24 hours') AS new_24h
        FROM registry_channels"""))[0]
    est = {r["relname"]: r["n"] for r in await _q("""
        SELECT c.relname, GREATEST(c.reltuples, 0)::bigint AS n
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND c.relname IN ('registry_resources', 'registry_http_resources',
                            'registry_resource_channels', 'seed_candidates')""")}
    inst = (await _q("""
        SELECT count(DISTINCT instance_hash) AS total,
               count(DISTINCT instance_hash) FILTER (WHERE submitted_at > NOW() - INTERVAL '24 hours') AS d1,
               count(DISTINCT instance_hash) FILTER (WHERE submitted_at > NOW() - INTERVAL '7 days') AS d7,
               count(DISTINCT instance_hash) FILTER (WHERE submitted_at > NOW() - INTERVAL '30 days') AS d30,
               count(*) FILTER (WHERE submitted_at > NOW() - INTERVAL '24 hours') AS subs_24h,
               COALESCE(sum(resources_accepted) FILTER (WHERE submitted_at > NOW() - INTERVAL '24 hours'), 0) AS res_24h,
               COALESCE(sum(channels_accepted) FILTER (WHERE submitted_at > NOW() - INTERVAL '24 hours'), 0) AS ch_24h,
               max(submitted_at) AS last_at
        FROM registry_contributions"""))[0]
    pulls = (await _q("""
        SELECT count(*) AS n, count(DISTINCT ip_hash) AS ips
        FROM registry_pulls WHERE pulled_at > NOW() - INTERVAL '24 hours'"""))[0]
    db = (await _q("SELECT pg_database_size(current_database()) AS bytes"))[0]
    tables = await _q("""
        SELECT c.relname AS name, GREATEST(c.reltuples, 0)::bigint AS rows, pg_total_relation_size(c.oid) AS bytes
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 12""")
    return {"channels": ch, "est": est, "instances": inst, "pulls": pulls, "db_bytes": db["bytes"],
            "tables": tables, "version": _ctx["version"], "uptime_s": int(time.time() - _started),
            "as_of": int(time.time())}


async def _seeds():
    return await _q("""SELECT COALESCE(validation_status, 'not checked') AS status, count(*) AS n
                       FROM seed_candidates GROUP BY 1 ORDER BY 2 DESC""", timeout_s=40)


async def _instances():
    rows = await _q("""
        SELECT instance_hash,
               (array_agg(tgarr_version ORDER BY submitted_at DESC))[1] AS version,
               min(submitted_at) AS first_at, max(submitted_at) AS last_at, count(*) AS submissions,
               COALESCE(sum(channels_accepted), 0) AS ch_ok, COALESCE(sum(channels_rejected), 0) AS ch_no,
               COALESCE(sum(resources_accepted), 0) AS res_ok, COALESCE(sum(resources_rejected), 0) AS res_no,
               COALESCE(sum(resources_accepted) FILTER (WHERE submitted_at > NOW() - INTERVAL '24 hours'), 0) AS res_24h,
               count(DISTINCT remote_ip_hash) AS ips
        FROM registry_contributions GROUP BY 1 ORDER BY max(submitted_at) DESC""")
    seen = {r["instance_hash"]: r["n"] for r in await _q(
        "SELECT instance_hash, count(*) AS n FROM registry_contributor_seen GROUP BY 1")}
    sus = {r["actor_key"][5:]: r["score"] for r in await _q(
        "SELECT actor_key, score FROM registry_suspicion WHERE actor_key LIKE 'inst:%'")}
    for r in rows:
        r["channels_seen"] = seen.get(r["instance_hash"], 0)
        r["suspicion"] = sus.get(r["instance_hash"], 0)
    return rows


async def _trends():
    days = await _q("""
        SELECT d::date AS day,
               COALESCE(c.subs, 0) AS subs, COALESCE(c.inst, 0) AS instances,
               COALESCE(c.ch, 0) AS channels, COALESCE(c.res, 0) AS resources, COALESCE(p.n, 0) AS pulls
        FROM generate_series((NOW() - INTERVAL '29 days')::date, NOW()::date, '1 day') d
        LEFT JOIN (SELECT submitted_at::date AS day, count(*) AS subs, count(DISTINCT instance_hash) AS inst,
                          sum(channels_accepted) AS ch, sum(resources_accepted) AS res
                   FROM registry_contributions WHERE submitted_at > NOW() - INTERVAL '31 days' GROUP BY 1) c
               ON c.day = d::date
        LEFT JOIN (SELECT pulled_at::date AS day, count(*) AS n FROM registry_pulls
                   WHERE pulled_at > NOW() - INTERVAL '31 days' GROUP BY 1) p ON p.day = d::date
        ORDER BY 1""")
    versions = await _q("""
        SELECT version, count(*) AS instances,
               count(*) FILTER (WHERE last_at > NOW() - INTERVAL '7 days') AS active_7d
        FROM (SELECT instance_hash, (array_agg(tgarr_version ORDER BY submitted_at DESC))[1] AS version,
                     max(submitted_at) AS last_at
              FROM registry_contributions WHERE submitted_at > NOW() - INTERVAL '30 days' GROUP BY 1) x
        GROUP BY 1 ORDER BY 3 DESC, 2 DESC""")
    return {"days": days, "versions": versions}


_CH_COLS = """username, title, members_count, audience, health_status, verified, blocked, block_reason,
              distinct_contributors, release_files, video_files, book_files, audio_files, last_seen_at"""


async def _channels(view: str, q: str):
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return await _q(f"""SELECT {_CH_COLS} FROM registry_channels
                            WHERE username ILIKE $1 OR title ILIKE $1
                            ORDER BY COALESCE(members_count, 0) DESC LIMIT 100""", like)
    if view == "blocked":
        return await _q(f"""SELECT {_CH_COLS} FROM registry_channels WHERE blocked
                            ORDER BY last_seen_at DESC NULLS LAST LIMIT 200""")
    if view == "recent":
        return await _q(f"""SELECT {_CH_COLS} FROM registry_channels
                            ORDER BY last_seen_at DESC LIMIT 100""")
    if view == "shared":
        return await _q(f"""SELECT {_CH_COLS} FROM registry_channels
                            ORDER BY distinct_contributors DESC LIMIT 100""")
    return await _q(f"""SELECT {_CH_COLS} FROM registry_channels
                        ORDER BY release_files DESC NULLS LAST LIMIT 100""")


async def _pulls():
    return await _q("""
        SELECT ip_hash, count(*) AS n, max(pulled_at) AS last_at, bool_or(api_key_set) AS api_key
        FROM registry_pulls WHERE pulled_at > NOW() - INTERVAL '24 hours'
        GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT 200""")


async def _suspicion():
    return await _q("""SELECT actor_key, score, reasons, first_flagged_at, last_flagged_at
                       FROM registry_suspicion ORDER BY score DESC, last_flagged_at DESC LIMIT 200""")


def _json(data) -> JSONResponse:
    return _headers(JSONResponse(data))


@router.get("/admin/api/overview")
async def api_overview(request: Request):
    _auth(request)
    return _json(await _cached("overview", 300, _overview))


@router.get("/admin/api/seeds")
async def api_seeds(request: Request):
    _auth(request)
    return _json(await _cached("seeds", 3600, _seeds))


@router.get("/admin/api/instances")
async def api_instances(request: Request):
    _auth(request)
    return _json(await _cached("instances", 120, _instances))


@router.get("/admin/api/trends")
async def api_trends(request: Request):
    _auth(request)
    return _json(await _cached("trends", 300, _trends))


@router.get("/admin/api/channels")
async def api_channels(request: Request, view: str = "top", q: str = ""):
    _auth(request)
    view = view if view in ("top", "blocked", "recent", "shared") else "top"
    q = q.strip().lstrip("@")[:60]
    if q and len(q) < 3:
        raise HTTPException(400, "search needs 3 characters or more")
    return _json(await _cached(f"ch:{view}:{q.lower()}", 120, lambda: _channels(view, q)))


@router.get("/admin/api/pulls")
async def api_pulls(request: Request):
    _auth(request)
    return _json({"cap": PULL_CAP, "rows": await _cached("pulls", 60, _pulls)})


@router.get("/admin/api/suspicion")
async def api_suspicion(request: Request):
    _auth(request)
    return _json(await _cached("suspicion", 120, _suspicion))


# ── server monitoring ─────────────────────────────────────────────────
# Counted in the registry itself: every request it answers, the bytes of
# every response, how many are in flight. Once a minute that, plus the
# machine's CPU, memory and disk (read from /proc: the host's figures, also
# from inside a container), becomes one row of registry_metrics.
# Bandwidth here is what the registry hands to the web server, i.e. before
# compression: exact for the binary dedup filter (the bulk of it), an upper
# bound for JSON.
_mx = {"req": 0, "out": 0, "in": 0, "inflight": 0, "peak": 0, "err": 0, "paths": {}}
_cpu_prev = [None]


def _path_key(method: str, path: str) -> str:
    if path.startswith("/api/v1/"):
        return method + " " + "/".join(path.split("/")[:4])[:60]
    if path.startswith("/admin"):
        return "admin"
    return "other"


async def _metrics_mw(request: Request, call_next):
    if not _mx.get("sampler"):
        # started with the first request: no start-up hook to depend on
        _mx["sampler"] = asyncio.create_task(_metrics_sampler())
    _mx["inflight"] += 1
    if _mx["inflight"] > _mx["peak"]:
        _mx["peak"] = _mx["inflight"]
    try:
        resp = await call_next(request)
    except Exception:
        _mx["err"] += 1
        raise
    finally:
        _mx["inflight"] -= 1
    try:
        n_out = int(resp.headers.get("content-length") or 0)
    except ValueError:
        n_out = 0
    try:
        n_in = int(request.headers.get("content-length") or 0)
    except ValueError:
        n_in = 0
    _mx["req"] += 1
    _mx["out"] += n_out
    _mx["in"] += n_in
    if resp.status_code >= 500:
        _mx["err"] += 1
    k = _path_key(request.method, request.url.path)
    if k != "admin":
        cell = _mx["paths"].setdefault(k, [0, 0])
        cell[0] += 1
        cell[1] += n_out
        if len(_mx["paths"]) > 60:                     # a scanner inventing paths
            _mx["paths"] = dict(sorted(_mx["paths"].items(), key=lambda x: -x[1][1])[:30])
    return resp


def _machine() -> dict:
    out = {"cores": os.cpu_count() or 1}
    try:
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:9]]
        idle, total = v[3] + v[4], sum(v)
        prev = _cpu_prev[0]
        _cpu_prev[0] = (idle, total)
        if prev and total > prev[1]:
            out["cpu_busy"] = round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 2)
    except (OSError, ValueError, IndexError):
        pass
    try:
        out["load1"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    try:
        kv = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, val = line.partition(":")
                kv[k] = int(val.split()[0]) * 1024
        out["mem_total"], out["mem_avail"] = kv.get("MemTotal"), kv.get("MemAvailable")
    except (OSError, ValueError, IndexError):
        pass
    try:
        import shutil
        du = shutil.disk_usage(os.path.dirname(os.path.abspath(__file__)))
        out["disk_free"], out["disk_total"] = du.free, du.total
    except OSError:
        pass
    return out


async def _metrics_sampler():
    await asyncio.sleep(5)
    _machine()                                         # first CPU reading: a baseline only
    while True:
        await asyncio.sleep(60)
        try:
            snap = {k: _mx[k] for k in ("req", "out", "in", "peak", "err")}
            paths = _mx["paths"]
            _mx.update(req=0, out=0, err=0, peak=_mx["inflight"], paths={})
            _mx["in"] = 0
            m = await asyncio.to_thread(_machine)
            pool = _ctx["pool"]()
            if pool is None:
                continue
            async with pool.acquire() as conn:
                db_conns = await conn.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
                await conn.execute(
                    """INSERT INTO registry_metrics
                         (ts, cpu_busy, load1, cores, mem_avail, mem_total, disk_free, disk_total,
                          requests, bytes_out, bytes_in, inflight_peak, errors, db_conns, by_path)
                       VALUES (date_trunc('minute', NOW()), $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb)
                       ON CONFLICT (ts) DO NOTHING""",
                    m.get("cpu_busy"), m.get("load1"), m.get("cores"), m.get("mem_avail"), m.get("mem_total"),
                    m.get("disk_free"), m.get("disk_total"), snap["req"], snap["out"], snap["in"],
                    snap["peak"], snap["err"], db_conns, json.dumps(paths))
                if secrets.randbelow(1440) == 0:       # about once a day
                    await conn.execute("DELETE FROM registry_metrics WHERE ts < NOW() - INTERVAL '400 days'")
        except Exception:
            pass                                       # monitoring must never hurt the registry


def forecast(per_install_bytes_day: float, peak_factor: float, installs: int) -> dict:
    """What `installs` installs would need, if each behaves like today's
    average one. peak_factor = busiest hour / average hour, from history."""
    day = per_install_bytes_day * installs
    avg_mbps = day * 8 / 86400 / 1e6
    return {"installs": installs, "gb_day": round(day / 1e9, 1), "tb_month": round(day * 30 / 1e12, 2),
            "mbps_avg": round(avg_mbps, 1), "mbps_peak": round(avg_mbps * max(1.0, peak_factor), 1)}


async def _server():
    now = await _q("""SELECT * FROM registry_metrics ORDER BY ts DESC LIMIT 1""")
    last5 = (await _q("""
        SELECT COALESCE(sum(requests), 0) AS req, COALESCE(sum(bytes_out), 0) AS bytes_out,
               COALESCE(max(inflight_peak), 0) AS inflight, COALESCE(avg(cpu_busy), 0) AS cpu,
               count(*) AS minutes
        FROM registry_metrics WHERE ts > NOW() - INTERVAL '5 minutes'"""))[0]
    day = await _q("""
        SELECT to_timestamp(floor(extract(epoch FROM ts) / 600) * 600) AS t,
               avg(cpu_busy) AS cpu, max(cpu_busy) AS cpu_max, sum(requests) AS req,
               sum(bytes_out) AS bytes_out, max(inflight_peak) AS inflight, sum(errors) AS errors,
               max(db_conns) AS db_conns, min(mem_avail) AS mem_avail
        FROM registry_metrics WHERE ts > NOW() - INTERVAL '24 hours' GROUP BY 1 ORDER BY 1""")
    month = await _q("""
        SELECT ts::date AS day, sum(bytes_out) AS bytes_out, sum(bytes_in) AS bytes_in, sum(requests) AS req,
               max(cpu_busy) AS cpu_max, avg(cpu_busy) AS cpu, max(inflight_peak) AS inflight,
               sum(errors) AS errors, count(*) AS minutes
        FROM registry_metrics WHERE ts > NOW() - INTERVAL '30 days' GROUP BY 1 ORDER BY 1""")
    hours = await _q("""
        SELECT date_trunc('hour', ts) AS h, sum(bytes_out) AS b
        FROM registry_metrics WHERE ts > NOW() - INTERVAL '7 days' GROUP BY 1""")
    paths = await _q("""
        SELECT k AS path, sum((v->>0)::bigint) AS req, sum((v->>1)::bigint) AS bytes_out
        FROM registry_metrics, jsonb_each(by_path) AS e(k, v)
        WHERE ts > NOW() - INTERVAL '24 hours' GROUP BY 1 ORDER BY 3 DESC LIMIT 12""")
    installs = (await _q("""
        SELECT count(DISTINCT instance_hash) AS n FROM registry_contributions
        WHERE submitted_at > NOW() - INTERVAL '24 hours'"""))[0]["n"]
    # per-install cost from the full days on record (a day with under 20 h of samples is not a day)
    full = [d for d in month if d["minutes"] >= 1200]
    per_install = None
    if full and installs:
        per_install = sum(d["bytes_out"] for d in full) / len(full) / installs
    hb = [h["b"] for h in hours if h["b"]]
    peak_factor = (max(hb) / (sum(hb) / len(hb))) if hb else 1.0
    return {"now": now[0] if now else None, "last5": last5, "day": day, "month": month, "paths": paths,
            "installs_24h": installs, "per_install_bytes_day": per_install,
            "peak_factor": round(peak_factor, 2), "full_days": len(full),
            "forecast": [forecast(per_install, peak_factor, n) for n in (100, 1000, 10000)] if per_install else []}


@router.get("/admin/api/server")
async def api_server(request: Request):
    _auth(request)
    return _json(await _cached("server", 55, _server))


# ── the page ──────────────────────────────────────────────────────────
_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>tgarr.me · admin</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--line:#e2e8f0;--fg:#0f172a;--mut:#64748b;--acc:#229ed9;--ok:#059669;--warn:#d97706;--bad:#dc2626}
*{box-sizing:border-box}body{font:14px/1.5 -apple-system,system-ui,"Segoe UI",sans-serif;background:var(--bg);color:var(--fg);margin:0}
header{background:var(--card);border-bottom:1px solid var(--line);padding:10px 20px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:5}
header b{font-size:16px}header .sp{flex:1}header small{color:var(--mut)}
header button{font:inherit;border:1px solid var(--line);background:var(--card);border-radius:8px;padding:5px 12px;cursor:pointer}
nav{display:flex;gap:4px;padding:10px 20px 0;overflow-x:auto}
nav a{padding:8px 14px;border-radius:8px 8px 0 0;color:var(--mut);text-decoration:none;white-space:nowrap;font-weight:600}
nav a.on{background:var(--card);color:var(--acc);border:1px solid var(--line);border-bottom-color:var(--card)}
main{padding:16px 20px 60px;max-width:1400px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card.warn{border-color:#fcd34d;background:#fffbeb}.card.bad{border-color:#fca5a5;background:#fef2f2}
.card .n{font-size:24px;font-weight:700;line-height:1.2}.card .l{color:var(--mut);font-size:12px}.card .s{font-size:12px;color:var(--mut);margin-top:2px}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:22px 0 8px}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);position:sticky;top:0;background:var(--card);cursor:default}
th.s{cursor:pointer}td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}td.w{white-space:normal;max-width:340px}
code{font:12px ui-monospace,Menlo,Consolas,monospace;color:#334155}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:600;background:#e2e8f0;color:#334155}
.tag.ok{background:#d1fae5;color:#065f46}.tag.warn{background:#fef3c7;color:#92400e}.tag.bad{background:#fee2e2;color:#991b1b}
.bar{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.bar input{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;min-width:200px}
.chip{padding:6px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card);cursor:pointer;font:inherit}
.chip.on{background:#e0f2fe;border-color:var(--acc);color:#0369a1}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.chart{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.chart .t{font-weight:600}.chart .v{color:var(--mut);font-size:12px;margin-bottom:6px}
svg{display:block;width:100%;height:110px}svg rect{fill:var(--acc)}svg rect:hover{fill:#0369a1}
.mut{color:var(--mut)}.err{color:var(--bad);padding:14px}
@media (max-width:700px){main,nav,header{padding-left:12px;padding-right:12px}}
</style></head><body>
<header><b>tgarr.me registry</b><small id="ver"></small><span class="sp"></span><small id="who">__WHO__</small>
<form method="post" action="/admin/logout" style="margin:0"><button>Sign out</button></form></header>
<nav id="nav"></nav><main id="main"><div class="mut">loading…</div></main>
<script>
const TABS=[["overview","Overview"],["server","Server & capacity"],["instances","Contributors"],["trends","Trends"],["channels","Channels"],["pulls","Pulls & limits"],["suspicion","Flagged"]];
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num=n=>n==null?"–":Number(n).toLocaleString("en-US");
const short=n=>{n=Number(n||0);return n>=1e9?(n/1e9).toFixed(1)+"B":n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e4?(n/1e3).toFixed(0)+"K":n.toLocaleString("en-US")};
const bytes=b=>{b=Number(b||0);const u=["B","KB","MB","GB","TB"];let i=0;while(b>=1024&&i<4){b/=1024;i++}return b.toFixed(i?1:0)+" "+u[i]};
const ago=t=>{if(!t)return"–";const s=(Date.now()-new Date(t))/1000;return s<90?"just now":s<5400?Math.round(s/60)+" min ago":s<129600?Math.round(s/3600)+" h ago":Math.round(s/86400)+" d ago"};
const day=t=>t?String(t).slice(0,10):"–";
async function get(p){const r=await fetch("/admin/api/"+p,{credentials:"same-origin"});if(r.status===401){location.href="/admin/login";throw 0}
  if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||("HTTP "+r.status));return r.json()}
const card=(n,l,s)=>`<div class="card"><div class="n">${n}</div><div class="l">${esc(l)}</div>${s?`<div class="s">${s}</div>`:""}</div>`;
function table(cols,rows,empty){ // cols: [label, fn(row)->html, "r"?, sortKey?]
  if(!rows.length)return `<div class="box"><div class="mut" style="padding:14px">${esc(empty||"nothing to show")}</div></div>`;
  return `<div class="box"><table><thead><tr>${cols.map((c,i)=>`<th class="${c[2]||""}${c[3]?" s":""}" data-i="${i}">${esc(c[0])}</th>`).join("")}</tr></thead><tbody>${
    rows.map(r=>`<tr>${cols.map(c=>`<td class="${c[2]||""}">${c[1](r)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`}
function sortable(el,cols,rows,render){el.querySelectorAll("th.s").forEach(th=>th.onclick=()=>{const k=cols[th.dataset.i][3];
  const dir=(el._k===k&&el._d===-1)?1:-1;el._k=k;el._d=dir;rows.sort((a,b)=>{const x=a[k],y=b[k];return (x==null?-1:y==null?1:x>y?1:x<y?-1:0)*dir});render()})}
function chart(title,days,key,noSum){const vals=days.map(d=>Number(d[key]||0));const max=Math.max(1,...vals);const w=100/vals.length;
  const total=vals.reduce((a,b)=>a+b,0);
  return `<div class="chart"><div class="t">${esc(title)}</div><div class="v">${noSum?"peak "+short(max):short(total)+" in 30 days"} · today ${short(vals[vals.length-1])}</div>
  <svg viewBox="0 0 100 40" preserveAspectRatio="none">${vals.map((v,i)=>`<rect x="${(i*w+0.15*w).toFixed(2)}" width="${(0.7*w).toFixed(2)}" y="${(40-38*v/max).toFixed(2)}" height="${(38*v/max).toFixed(2)}"><title>${day(days[i].day)}: ${num(v)}</title></rect>`).join("")}</svg></div>`}
const V={
 async server(){const s=await get("server");const n=s.now;
  if(!n){$("#main").innerHTML='<div class="mut">No samples yet. The registry writes one a minute; look again shortly.</div>';return}
  const mbps=b=>(b*8/60/1e6);const pct=x=>x==null?"–":x.toFixed(1)+"%";
  const lvl=(v,w,b)=>v>=b?"bad":v>=w?"warn":"";
  const tile=(n_,l,s_,c)=>`<div class="card ${c||""}"><div class="n">${n_}</div><div class="l">${esc(l)}</div>${s_?`<div class="s">${s_}</div>`:""}</div>`;
  const memUsed=n.mem_total?100*(1-n.mem_avail/n.mem_total):0, diskUsed=n.disk_total?100*(1-n.disk_free/n.disk_total):0;
  const m5=s.last5.minutes||1;
  let h=`<h2>Right now</h2><div class="cards">
   ${tile(pct(100-(n.cpu_busy||0)),"processor idle",`${n.cores} cores · load ${n.load1}`,lvl(n.cpu_busy||0,70,90))}
   ${tile(bytes(n.mem_avail),"memory available","of "+bytes(n.mem_total),lvl(memUsed,88,95))}
   ${tile(bytes(n.disk_free),"disk free","of "+bytes(n.disk_total),lvl(diskUsed,85,95))}
   ${tile((s.last5.bytes_out*8/(m5*60)/1e6).toFixed(2)+" Mbps","sent, last 5 minutes","before compression")}
   ${tile(num(Math.round(s.last5.req/m5)),"requests a minute","last 5 minutes")}
   ${tile(num(s.last5.inflight),"most at one moment","requests in flight, last 5 min")}
   ${tile(num(n.db_conns),"database connections")}
   ${tile(num(s.installs_24h),"installs sending data","last 24 hours")}</div>`;
  const d=s.day.map(r=>({day:r.t,mbps:mbps(r.bytes_out)/10,cpu:r.cpu||0,cpu_max:r.cpu_max||0,req:r.req||0,inflight:r.inflight||0}));
  const line=(title,rows,key,unit,digits)=>{const vals=rows.map(r=>Number(r[key]||0));const max=Math.max(1e-9,...vals);const w=100/Math.max(1,vals.length);
   return `<div class="chart"><div class="t">${esc(title)}</div><div class="v">peak ${max.toFixed(digits)}${unit} · now ${vals.length?vals[vals.length-1].toFixed(digits):0}${unit}</div>
   <svg viewBox="0 0 100 40" preserveAspectRatio="none">${vals.map((v,i)=>`<rect x="${(i*w).toFixed(2)}" width="${Math.max(0.2,w*0.85).toFixed(2)}" y="${(40-38*v/max).toFixed(2)}" height="${(38*v/max).toFixed(2)}"><title>${String(rows[i].day).slice(0,16).replace("T"," ")}: ${v.toFixed(digits)}${unit}</title></rect>`).join("")}</svg></div>`};
  h+=`<h2>Last 24 hours (10-minute steps)</h2><div class="grid2">${line("Bandwidth sent",d,"mbps"," Mbps",2)}${line("Processor busy (average)",d,"cpu","%",1)}${line("Requests",d,"req","",0)}${line("Most requests in flight",d,"inflight","",0)}</div>`;
  const mo=s.month.map(r=>({day:r.day,gb:r.bytes_out/1e9,req:r.req,cpu_max:r.cpu_max||0,inflight:r.inflight||0}));
  h+=`<h2>Last 30 days</h2><div class="grid2">${line("Sent per day",mo,"gb"," GB",2)}${line("Requests per day",mo,"req","",0)}${line("Processor busy (daily peak)",mo,"cpu_max","%",1)}${line("Most requests in flight (daily peak)",mo,"inflight","",0)}</div>`;
  h+=`<h2>Where the bandwidth goes (24 hours)</h2>`+table([["endpoint",r=>`<code>${esc(r.path)}</code>`],["requests",r=>num(r.req),"r"],["sent",r=>bytes(r.bytes_out),"r"],["per request",r=>bytes(r.bytes_out/Math.max(1,r.req)),"r"],
    ["share",r=>{const t=s.paths.reduce((a,b)=>a+Number(b.bytes_out),0)||1;return (100*r.bytes_out/t).toFixed(1)+"%"},"r"]],s.paths,"no traffic recorded yet");
  h+=`<h2>Capacity forecast</h2>`;
  if(s.forecast.length){h+=`<div class="bar"><span>Each install costs about <b>${bytes(s.per_install_bytes_day)}</b> a day (${s.installs_24h} installs, average of ${s.full_days} full day${s.full_days===1?"":"s"}). The busiest hour runs at ${s.peak_factor}× the average. If every install behaves like today's:</span></div>`+
    table([["installs",r=>num(r.installs),"r"],["per day",r=>r.gb_day+" GB","r"],["per month",r=>r.tb_month+" TB","r"],["average",r=>r.mbps_avg+" Mbps","r"],["busiest hour",r=>r.mbps_peak+" Mbps","r"]],s.forecast)}
  else h+=`<div class="mut">Needs one full day of samples and at least one install sending data. Samples so far cover ${s.month.reduce((a,b)=>a+Number(b.minutes),0)} minutes.</div>`;
  $("#main").innerHTML=h},
 async overview(){const o=await get("overview");$("#ver").textContent="registry "+o.version+" · up "+ago(Date.now()-o.uptime_s*1000).replace(" ago","");
  const c=o.channels,i=o.instances,e=o.est;
  let h=`<h2>Contributors</h2><div class="cards">${card(num(i.d1),"active in 24 h")}${card(num(i.d7),"active in 7 days")}${card(num(i.d30),"active in 30 days")}${card(num(i.total),"ever")}
   ${card(num(i.subs_24h),"submissions in 24 h","last "+ago(i.last_at))}${card(short(i.res_24h),"files accepted in 24 h")}${card(num(i.ch_24h),"channels accepted in 24 h")}${card(num(o.pulls.n),"list pulls in 24 h",num(o.pulls.ips)+" addresses")}</div>
   <h2>Channels</h2><div class="cards">${card(short(c.total),"known")}${card(short(c.alive),"alive, not blocked")}${card(short(c.verified),"verified")}${card(short(c.contributed),"first reported by an install")}${card(short(c.seeded),"from the seed lists")}
   ${card(short(c.with_releases),"with films / episodes")}${card(short(c.nsfw),"18+")}${card(num(c.blocked),"blocked")}${card(num(c.new_24h),"new in 24 h")}</div>
   <h2>Files (estimates)</h2><div class="cards">${card(short(e.registry_resources),"files from installs")}${card(short(e.registry_resource_channels),"file ↔ channel links")}${card(short(e.registry_http_resources),"files from web previews")}${card(short(e.seed_candidates),"candidate channels")}${card(bytes(o.db_bytes),"database size")}</div>
   <h2>Candidate channels by check result</h2><div id="seeds" class="mut">loading…</div>
   <h2>Largest tables</h2>${table([["table",r=>`<code>${esc(r.name)}</code>`],["rows (est.)",r=>num(r.rows),"r"],["size",r=>bytes(r.bytes),"r"]],o.tables)}`;
  $("#main").innerHTML=h;
  get("seeds").then(s=>{$("#seeds").outerHTML=`<div class="cards">${s.map(r=>card(short(r.n),r.status)).join("")}</div>`}).catch(e=>{$("#seeds").textContent=e.message});},
 async instances(){const rows=await get("instances");const cols=[
   ["install",r=>`<code title="${esc(r.instance_hash)}">${esc(r.instance_hash.slice(0,10))}</code>`],
   ["version",r=>esc(r.version||"?"),"","version"],["last sent",r=>ago(r.last_at),"","last_at"],["first seen",r=>day(r.first_at),"","first_at"],
   ["submissions",r=>num(r.submissions),"r","submissions"],["channels ok / refused",r=>num(r.ch_ok)+" / "+num(r.ch_no),"r","ch_ok"],
   ["files ok / refused",r=>short(r.res_ok)+" / "+short(r.res_no),"r","res_ok"],["files 24 h",r=>short(r.res_24h),"r","res_24h"],
   ["channels seen",r=>num(r.channels_seen),"r","channels_seen"],["addresses",r=>num(r.ips),"r","ips"],
   ["flag",r=>r.suspicion?`<span class="tag ${r.suspicion>=100?"bad":"warn"}">${r.suspicion}</span>`:"","r","suspicion"]];
  const wk=rows.filter(r=>Date.now()-new Date(r.last_at)<7*864e5).length;
  const draw=()=>{$("#main").innerHTML=`<div class="bar"><span>${rows.length} installs have ever sent data · ${wk} in the last 7 days. An install is known only by a hash of its id.</span></div>`+table(cols,rows);sortable($("#main"),cols,rows,draw)};draw()},
 async trends(){const t=await get("trends");const d=t.days;
  $("#main").innerHTML=`<div class="grid2">${chart("Installs sending data, per day",d,"instances",1)}${chart("Submissions per day",d,"subs")}${chart("Files accepted per day",d,"resources")}${chart("Channels accepted per day",d,"channels")}${chart("List pulls per day",d,"pulls")}</div>
   <h2>Versions (installs that sent data in 30 days)</h2>`+table([["version",r=>esc(r.version||"?")],["installs",r=>num(r.instances),"r"],["active in 7 days",r=>num(r.active_7d),"r"]],t.versions)},
 async channels(){const st=V._ch||(V._ch={view:"top",q:""});
  const rows=await get("channels?view="+st.view+"&q="+encodeURIComponent(st.q));
  const views=[["top","most films"],["shared","most installs"],["recent","recently seen"],["blocked","blocked"]];
  $("#main").innerHTML=`<div class="bar"><input id="q" placeholder="search name or @username" value="${esc(st.q)}">${views.map(v=>`<button class="chip ${!st.q&&st.view===v[0]?"on":""}" data-v="${v[0]}">${v[1]}</button>`).join("")}</div>`+
   table([["channel",r=>`<a href="https://t.me/${esc(r.username)}" target="_blank" rel="noopener noreferrer">@${esc(r.username)}</a>`],["title",r=>esc((r.title||"").slice(0,60)),"w"],
    ["members",r=>short(r.members_count),"r"],["installs",r=>num(r.distinct_contributors),"r"],["films",r=>num(r.release_files),"r"],["videos",r=>num(r.video_files),"r"],["books",r=>num(r.book_files),"r"],["audio",r=>num(r.audio_files),"r"],
    ["state",r=>(r.blocked?`<span class="tag bad" title="${esc(r.block_reason)}">blocked</span> `:"")+(r.verified?'<span class="tag ok">verified</span> ':"")+(r.audience==="nsfw"?'<span class="tag warn">18+</span> ':"")+`<span class="tag">${esc(r.health_status||"not checked")}</span>`],
    ["reason",r=>esc(r.block_reason||""),"w"],["last seen",r=>ago(r.last_seen_at)]],rows,st.q?"no channel matches":"nothing here");
  $("#q").onkeydown=e=>{if(e.key==="Enter"){st.q=e.target.value.trim();show("channels")}};
  document.querySelectorAll(".chip").forEach(b=>b.onclick=()=>{st.view=b.dataset.v;st.q="";show("channels")})},
 async pulls(){const p=await get("pulls");
  const hit=p.rows.filter(r=>r.n>=p.cap).length;
  $("#main").innerHTML=`<div class="bar"><span>${p.rows.length} addresses pulled the channel list in the last 24 h. The limit is ${p.cap} a day without a key; ${hit} reached it. Addresses are stored as hashes.</span></div>`+
   table([["address",r=>`<code>${esc(r.ip_hash.slice(0,12))}</code>`],["pulls in 24 h",r=>num(r.n)+(r.n>=p.cap?' <span class="tag warn">at limit</span>':""),"r"],["last",r=>ago(r.last_at)],["key",r=>r.api_key?'<span class="tag ok">yes</span>':""]],p.rows)},
 async suspicion(){const rows=await get("suspicion");
  $("#main").innerHTML=`<div class="bar"><span>Installs and addresses the registry has flagged. 100 or more is refused.</span></div>`+
   table([["who",r=>`<code>${esc(r.actor_key.slice(0,18))}</code>`],["score",r=>`<span class="tag ${r.score>=100?"bad":"warn"}">${r.score}</span>`,"r"],["why",r=>esc(r.reasons),"w"],["first",r=>day(r.first_flagged_at)],["last",r=>ago(r.last_flagged_at)]],rows,"nobody is flagged")}
};
async function show(t){if(!V[t])t="overview";location.hash=t;
  $("#nav").innerHTML=TABS.map(x=>`<a href="#${x[0]}" class="${x[0]===t?"on":""}">${x[1]}</a>`).join("");
  $("#main").innerHTML='<div class="mut">loading…</div>';
  try{await V[t]()}catch(e){if(e)$("#main").innerHTML=`<div class="err">${esc(e.message||e)}</div>`}}
window.onhashchange=()=>show(location.hash.slice(1));show(location.hash.slice(1)||"overview");
</script></body></html>"""


@router.get("/admin/", response_class=HTMLResponse)
async def admin_page(request: Request):
    _need_cfg()
    u = _user(request)
    if not u:
        return RedirectResponse("/admin/login", status_code=302)
    import html as _html
    return _headers(HTMLResponse(_PAGE.replace("__WHO__", _html.escape(u))))


# ── python admin.py init ──────────────────────────────────────────────
if __name__ == "__main__":
    if sys.argv[1:2] != ["init"]:
        sys.exit("usage: python admin.py init [path]")
    path = sys.argv[2] if len(sys.argv) > 2 else CONFIG_PATH
    if os.path.exists(path):
        sys.exit(f"{path} already exists - remove it first to start over")
    pw = secrets.token_urlsafe(18)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"secret": secrets.token_hex(32), "password_hash": hash_password(pw)}, f, indent=1)
    # The password goes to a file only its owner can read, never to the
    # terminal: terminals get logged, pasted and screen-shared.
    pw_path = path + ".password"
    fd = os.open(pw_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(pw + "\n")
    print(f"wrote {path}")
    print(f"the password is in {pw_path} - read it, keep it somewhere safe, delete the file")
