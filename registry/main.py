"""registry.tgarr.me — central federation service.

POST /api/v1/contribute — instances POST their eligible channels here.
GET  /api/v1/registry   — instances pull canonical curated channel list.
GET  /api/v1/stats      — aggregated stats (channels, contributors, etc).
GET  /                  — small status page.

Privacy:
- Client sends an `instance_uuid` (random hex). We SHA-256 it before storage
  so the raw UUID never lands on disk. UUIDs rotate weekly on client side.
- No user identity, no chat-history content. Only public @usernames + channel
  meta (members_count, audience, language) the user has already joined.

Defense in depth:
- Server-side CSAM keyword block on incoming username + title (matches the
  client's blocklist; protects against tampered/old clients).
- Per-IP-hash rate limit so a single attacker can't carpet-bomb fake channels.
- distinct_contributors >= 3 → channel auto-verified (community confirmation).
"""
import hashlib
import logging
import os
import re
import time
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tgarr-registry")

DB_DSN = os.environ["DB_DSN"]
VERSION = "0.3.6"
USERNAME_RX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
# Same as client — defense in depth
CSAM_RX = re.compile(
    r"\b(loli|lolicon|shota|shotacon|child\s*porn|kid\s*porn|"
    r"pre[\s_-]*teen|under[\s_-]*age|\bcp\d+|\bcp_)\b", re.IGNORECASE)

app = FastAPI(title="tgarr-registry", version=VERSION)
db_pool: Optional[asyncpg.Pool] = None


@app.on_event("startup")
async def _startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=8)
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        async with db_pool.acquire() as conn:
            await conn.execute(f.read())
    await _seed_honeypots()
    log.info("registry up — schema applied + honeypots seeded")


@app.on_event("shutdown")
async def _shutdown():
    if db_pool:
        await db_pool.close()


# Honeypot seed names — plausible-looking but DO NOT actually exist on Telegram.
# Rotated periodically. If a contribute payload mentions one, the submitter
# clearly didn't get the name from their own joined channels.
HONEYPOT_USERNAMES = [
    "PrismHDArchive", "AzraqMediaVault", "MeridianFilmDrop",
    "ZenithTVHub", "OakwoodSeriesHQ", "ClarionPicturesHD",
    "VertexCinemaCache", "QuartzShowsArchive",
]


async def _seed_honeypots():
    async with db_pool.acquire() as conn:
        for u in HONEYPOT_USERNAMES:
            await conn.execute(
                """INSERT INTO registry_channels
                     (username, title, members_count, media_count, audience,
                      is_honeypot, verified, distinct_contributors)
                   VALUES ($1, $2, $3, $4, 'sfw', TRUE, TRUE, 5)
                   ON CONFLICT (username) DO NOTHING""",
                u, u.replace("HD", " HD").replace("TV", " TV"),
                50000 + abs(hash(u)) % 500000,  # 50K-550K members
                200 + abs(hash(u)) % 800)        # 200-1000 media


async def _bump_suspicion(actor_key: str, delta: int, reason: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO registry_suspicion (actor_key, score, reasons)
               VALUES ($1, $2, $3)
               ON CONFLICT (actor_key) DO UPDATE SET
                 score = registry_suspicion.score + $2,
                 reasons = CASE
                   WHEN registry_suspicion.reasons IS NULL THEN $3
                   ELSE registry_suspicion.reasons || ',' || $3
                 END,
                 last_flagged_at = NOW()""",
            actor_key, delta, reason)


async def _suspicion_score(ip_hash_v: str, instance_hash_v: str = "") -> int:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT score FROM registry_suspicion
               WHERE actor_key = $1 OR actor_key = $2""",
            f"ip:{ip_hash_v}", f"inst:{instance_hash_v}")
    return max((r["score"] for r in rows), default=0)


def _ip_hash(request: Request) -> str:
    """Hash the originating IP (X-Forwarded-For first hop from CF/nginx)."""
    ip = (request.headers.get("CF-Connecting-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or (request.client.host if request.client else ""))
    return hashlib.sha256((ip or "").encode()).hexdigest()[:32]


def _instance_hash(uuid_str: str) -> str:
    return hashlib.sha256((uuid_str or "").encode()).hexdigest()[:32]


# ════════════════════════════════════════════════════════════════════
# POST /api/v1/contribute
# ════════════════════════════════════════════════════════════════════
@app.post("/api/v1/contribute")
async def contribute(request: Request):
    body = await request.json()
    instance_uuid = (body.get("instance_uuid") or "").strip()
    if not instance_uuid:
        raise HTTPException(400, "missing instance_uuid")
    inst_hash = _instance_hash(instance_uuid)
    ip_hash = _ip_hash(request)

    # Per-IP rate limit: max 60 submissions per hour (1/min effective)
    async with db_pool.acquire() as conn:
        recent = await conn.fetchval(
            """SELECT count(*) FROM registry_contributions
               WHERE remote_ip_hash = $1
                 AND submitted_at > NOW() - INTERVAL '1 hour'""", ip_hash)
        if recent > 60:
            raise HTTPException(429, "rate limit — try later")

    channels = body.get("channels") or []
    if not isinstance(channels, list):
        raise HTTPException(400, "channels must be array")
    if len(channels) > 500:
        raise HTTPException(413, "too many channels in one submission (max 500)")

    accepted = rejected = 0
    csam_flagged = honeypot_hits = 0
    async with db_pool.acquire() as conn:
        for c in channels:
            username = (c.get("username") or "").strip().lstrip("@")
            title = (c.get("title") or "")[:300]
            if not USERNAME_RX.match(username):
                rejected += 1
                continue
            # Honeypot trip — legitimate clients never see these as joined
            # channels. Submitting one means the client got the name from
            # scraping our own /api/v1/registry, not from real Telegram.
            if username in HONEYPOT_USERNAMES:
                honeypot_hits += 1
                rejected += 1
                continue
            # Server-side CSAM block (defense in depth)
            if CSAM_RX.search(username) or CSAM_RX.search(title):
                csam_flagged += 1
                rejected += 1
                # Pre-block the slot so future submissions are rejected fast
                await conn.execute(
                    """INSERT INTO registry_channels
                         (username, title, audience, blocked, block_reason)
                       VALUES ($1, $2, 'blocked_csam', TRUE, 'csam-keyword')
                       ON CONFLICT (username) DO UPDATE SET
                         blocked = TRUE, audience = 'blocked_csam',
                         block_reason = 'csam-keyword'""",
                    username, title)
                continue

            audience = c.get("audience") or "sfw"
            if audience not in ("sfw", "nsfw"):
                audience = "sfw"

            members = c.get("members_count")
            media = c.get("media_count")
            lang = (c.get("language") or "")[:8] or None
            cat = (c.get("category") or "")[:24] or None

            # Upsert channel + bump contribution_count
            await conn.execute(
                """INSERT INTO registry_channels
                     (username, title, members_count, media_count, audience,
                      language, category)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT (username) DO UPDATE SET
                     title = COALESCE(EXCLUDED.title, registry_channels.title),
                     members_count = GREATEST(
                       COALESCE(registry_channels.members_count, 0),
                       COALESCE(EXCLUDED.members_count, 0)),
                     media_count = GREATEST(
                       COALESCE(registry_channels.media_count, 0),
                       COALESCE(EXCLUDED.media_count, 0)),
                     audience = CASE
                       WHEN registry_channels.audience = 'blocked_csam' THEN 'blocked_csam'
                       ELSE EXCLUDED.audience END,
                     language = COALESCE(EXCLUDED.language, registry_channels.language),
                     category = COALESCE(EXCLUDED.category, registry_channels.category),
                     last_seen_at = NOW(),
                     contribution_count = registry_channels.contribution_count + 1""",
                username, title, members, media, audience, lang, cat)

            # Distinct-contributor accounting
            inserted = await conn.fetchval(
                """INSERT INTO registry_contributor_seen (instance_hash, username)
                   VALUES ($1, $2)
                   ON CONFLICT (instance_hash, username) DO UPDATE SET last_at = NOW()
                   RETURNING (xmax = 0)""",
                inst_hash, username)
            if inserted:
                await conn.execute(
                    """UPDATE registry_channels
                       SET distinct_contributors = distinct_contributors + 1,
                           verified = (distinct_contributors + 1) >= 3
                       WHERE username = $1""",
                    username)
            accepted += 1

        await conn.execute(
            """INSERT INTO registry_contributions
                 (instance_hash, tgarr_version, channels_accepted,
                  channels_rejected, remote_ip_hash)
               VALUES ($1, $2, $3, $4, $5)""",
            inst_hash, body.get("tgarr_version", ""), accepted, rejected, ip_hash)

    if csam_flagged:
        log.warning("CSAM-flagged %s names from instance %s — blocked",
                   csam_flagged, inst_hash[:8])
        await _bump_suspicion(f"inst:{inst_hash}", 50,
                            f"csam-attempt-x{csam_flagged}")
        await _bump_suspicion(f"ip:{ip_hash}", 50, "csam-attempt")
    if honeypot_hits:
        log.warning("HONEYPOT-trip %s names from instance %s ip %s — scraper signal",
                   honeypot_hits, inst_hash[:8], ip_hash[:8])
        await _bump_suspicion(f"inst:{inst_hash}", 20 * honeypot_hits,
                            f"honeypot-trip-x{honeypot_hits}")
        await _bump_suspicion(f"ip:{ip_hash}", 20 * honeypot_hits,
                            f"honeypot-trip")

    # Honest deception: return the SAME shape regardless. Bad actor can't
    # tell their submission was caught — they just see normal-looking counts.
    return {"status": "ok", "accepted": accepted, "rejected": rejected,
            "csam_flagged": csam_flagged}


# ════════════════════════════════════════════════════════════════════
# GET /api/v1/registry
# ════════════════════════════════════════════════════════════════════
# Per-IP-hash pull rate limit (defense in depth — Cloudflare absorbs 99%
# of legit traffic but origin still gets hit on cache misses). Free tier
# gets a much lower budget than paid; API key passes mark the request as
# paid and skip the free quota.
PULL_LIMIT_FREE_PER_DAY = 24
PULL_LIMIT_PAID_PER_DAY = 1000  # standard tier; plus tier currently same in code


@app.get("/api/v1/registry")
async def get_registry(
    request: Request,
    audience: str = Query("sfw"),
    min_contributors: int = Query(1),
    only_verified: int = Query(0),
    limit: int = Query(5000),
    since: Optional[str] = None,
    api_key: Optional[str] = Query(None),
):
    ip_hash = _ip_hash(request)
    # TODO v0.4: validate api_key against an auth table linked to Stripe.
    # Today a non-empty api_key just unlocks the paid-tier cap.
    daily_cap = PULL_LIMIT_PAID_PER_DAY if api_key else PULL_LIMIT_FREE_PER_DAY
    async with db_pool.acquire() as conn:
        # Count this IP's pulls in the last 24h. Real query against pulls
        # table, not contributions.
        today_pulls = await conn.fetchval(
            """SELECT count(*) FROM registry_pulls
               WHERE ip_hash = $1
                 AND pulled_at > NOW() - INTERVAL '24 hours'""", ip_hash)
        if today_pulls and today_pulls >= daily_cap:
            raise HTTPException(
                429, f"daily registry-pull limit exceeded ({daily_cap} for "
                     f"{'paid' if api_key else 'free'} tier — see "
                     f"https://tgarr.me/docs/PRICING.md)")
        # Record this pull now (before serving) so concurrent pulls count.
        await conn.execute(
            "INSERT INTO registry_pulls (ip_hash, api_key_set) VALUES ($1, $2)",
            ip_hash, bool(api_key))
        # Cheap prune: 1% chance per request to drop >7d-old rows.
        import random as _r
        if _r.random() < 0.01:
            await conn.execute(
                "DELETE FROM registry_pulls WHERE pulled_at < NOW() - INTERVAL '7 days'")

    # Response degradation based on suspicion score
    susp = await _suspicion_score(ip_hash)
    if susp >= 60:
        # Tarpit: slow drip + only honeypots
        await asyncio.sleep(5)
        await _bump_suspicion(f"ip:{ip_hash}", 5, "tarpitted")
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT username, title, members_count, media_count, audience, "
                "language, category, distinct_contributors, verified, last_seen_at "
                "FROM registry_channels WHERE is_honeypot=TRUE")
        # Identical JSON shape so attacker can't tell
        def _row_json(r):
            d = dict(r)
            if d.get("last_seen_at"):
                d["last_seen_at"] = d["last_seen_at"].isoformat()
            return d
        return JSONResponse({"version": VERSION,
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "count": len(rows),
                "channels": [_row_json(r) for r in rows]},
                headers={"Cache-Control": "no-store"})

    # Compute honeypot ratio for suspicious-but-not-tarpit range.
    honeypot_ratio = 0.0
    if susp >= 30:
        honeypot_ratio = 0.9
    elif susp >= 10:
        honeypot_ratio = 0.3

    where = ["blocked = FALSE", "audience <> 'blocked_csam'"]
    if audience in ("sfw", "nsfw"):
        where.append(f"audience = '{audience}'")
    if only_verified:
        where.append("verified = TRUE")
    if min_contributors > 1:
        where.append(f"distinct_contributors >= {min_contributors}")
    if since:
        where.append(f"last_seen_at >= '{since}'")
    limit = max(1, min(limit, 20000))
    base_cols = ("username, title, members_count, media_count, audience, "
                "language, category, distinct_contributors, verified, "
                "last_seen_at")
    async with db_pool.acquire() as conn:
        if honeypot_ratio > 0:
            # Suspicious tier: poison the well. Compute split, fetch separately,
            # interleave randomly so attacker can't sort honeypots out trivially.
            n_honey = max(1, int(limit * honeypot_ratio))
            n_real = max(0, limit - n_honey)
            where_real = where + ["is_honeypot = FALSE"]
            where_honey = where + ["is_honeypot = TRUE"]
            real = await conn.fetch(
                f"SELECT {base_cols} FROM registry_channels "
                f"WHERE {' AND '.join(where_real)} "
                f"ORDER BY distinct_contributors DESC, "
                f"COALESCE(members_count,0) DESC LIMIT {n_real}")
            honey = await conn.fetch(
                f"SELECT {base_cols} FROM registry_channels "
                f"WHERE {' AND '.join(where_honey)} "
                f"ORDER BY random() LIMIT {n_honey}")
            import random as _r
            rows = list(real) + list(honey)
            _r.shuffle(rows)
        else:
            # Normal tier: real channels only.
            where_real = where + ["is_honeypot = FALSE"]
            rows = await conn.fetch(
                f"SELECT {base_cols} FROM registry_channels "
                f"WHERE {' AND '.join(where_real)} "
                f"ORDER BY distinct_contributors DESC, "
                f"COALESCE(members_count,0) DESC LIMIT {limit}")
    def _row_json(r):
        d = dict(r)
        if d.get("last_seen_at"):
            d["last_seen_at"] = d["last_seen_at"].isoformat()
        return d
    body = {"version": VERSION,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(rows),
            "channels": [_row_json(r) for r in rows]}
    # Cloudflare CDN absorbs the load for 100K users. 15 min fresh +
    # 1 hour stale-while-revalidate means origin sees ~96 fills/day per
    # distinct (audience, only_verified, min_contributors) tuple.
    return JSONResponse(body, headers={
        "Cache-Control": "public, max-age=900, stale-while-revalidate=3600",
        "Vary": "Accept-Encoding",
    })


# ════════════════════════════════════════════════════════════════════
# GET /api/v1/stats
# ════════════════════════════════════════════════════════════════════
@app.get("/api/v1/stats")
async def stats():
    async with db_pool.acquire() as conn:
        s = await conn.fetchrow("""
            SELECT count(*) AS total_channels,
                   count(*) FILTER (WHERE audience='sfw' AND NOT blocked) AS sfw,
                   count(*) FILTER (WHERE audience='nsfw' AND NOT blocked) AS nsfw,
                   count(*) FILTER (WHERE blocked) AS blocked,
                   count(*) FILTER (WHERE verified AND NOT blocked) AS verified,
                   (SELECT count(DISTINCT instance_hash) FROM registry_contributions)
                                                                AS contributors,
                   (SELECT count(*) FROM registry_contributions) AS total_submissions
            FROM registry_channels""")
    return dict(s)


# ════════════════════════════════════════════════════════════════════
# GET / — status page
# ════════════════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def status_page():
    async with db_pool.acquire() as conn:
        s = await conn.fetchrow("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE verified AND NOT blocked) AS verified,
                   count(*) FILTER (WHERE audience='sfw' AND NOT blocked) AS sfw,
                   count(*) FILTER (WHERE audience='nsfw' AND NOT blocked) AS nsfw,
                   count(*) FILTER (WHERE blocked) AS blocked,
                   (SELECT count(DISTINCT instance_hash) FROM registry_contributions)
                                                                AS contributors,
                   (SELECT count(*) FROM registry_contributions) AS submissions
            FROM registry_channels""")
        top = await conn.fetch("""
            SELECT username, title, members_count, audience, distinct_contributors
            FROM registry_channels
            WHERE NOT blocked AND audience='sfw'
            ORDER BY distinct_contributors DESC, COALESCE(members_count,0) DESC
            LIMIT 25""")
    rows = "".join(
        f"<tr><td>@{r['username']}</td><td>{(r['title'] or '')[:60]}</td>"
        f"<td>{r['members_count'] or '-':,}</td>"
        f"<td>{r['distinct_contributors']}</td></tr>"
        for r in top
    )
    return f"""<!doctype html>
<html><head><title>registry.tgarr.me</title>
<style>
body {{ font:16px/1.5 -apple-system,system-ui,sans-serif; background:#f5f7fa; color:#1e293b; padding:32px; max-width:1000px; margin:auto; }}
h1 {{ color:#229ED9; font-size:32px; }}
.stat {{ display:inline-block; padding:12px 20px; background:#fff; border:1px solid #e2e8f0; border-radius:6px; margin:4px 6px 4px 0; }}
.stat .n {{ font-size:24px; font-weight:700; color:#229ED9; }}
.stat .l {{ font-size:12px; color:#64748b; text-transform:uppercase; }}
table {{ width:100%; background:#fff; border:1px solid #e2e8f0; border-radius:6px; border-collapse:collapse; margin-top:16px; }}
th,td {{ padding:10px 14px; text-align:left; border-bottom:1px solid #e2e8f0; font-size:14px; }}
th {{ background:#f8fafc; color:#64748b; font-size:11px; text-transform:uppercase; letter-spacing:1px; }}
code {{ background:#f1f5f9; padding:2px 6px; border-radius:3px; }}
</style></head><body>
<h1>registry · <span style="color:#1e293b">tgarr.me</span></h1>
<p style="color:#64748b">Federation registry for self-hosted tgarr instances.
Each instance POSTs eligible channels here; clients pull a deduped, contributor-verified list back.</p>

<div style="margin:18px 0">
  <div class="stat"><div class="n">{s['total']:,}</div><div class="l">total channels</div></div>
  <div class="stat"><div class="n">{s['verified']:,}</div><div class="l">verified (≥3 contributors)</div></div>
  <div class="stat"><div class="n">{s['sfw']:,}</div><div class="l">sfw</div></div>
  <div class="stat"><div class="n">{s['nsfw']:,}</div><div class="l">nsfw</div></div>
  <div class="stat"><div class="n">{s['blocked']:,}</div><div class="l">csam-blocked</div></div>
  <div class="stat"><div class="n">{s['contributors']:,}</div><div class="l">instances contributing</div></div>
  <div class="stat"><div class="n">{s['submissions']:,}</div><div class="l">total submissions</div></div>
</div>

<h2>API</h2>
<ul>
  <li><code>POST /api/v1/contribute</code> — clients submit channels they have joined</li>
  <li><code>GET /api/v1/registry?audience=sfw&only_verified=1</code> — pull curated list</li>
  <li><code>GET /api/v1/stats</code> — JSON stats</li>
</ul>

<h2>Top contributed channels</h2>
<table><thead><tr><th>username</th><th>title</th><th>members</th><th>distinct contributors</th></tr></thead>
<tbody>{rows or '<tr><td colspan="4" style="text-align:center;color:#94a3b8">no contributions yet</td></tr>'}</tbody></table>
</body></html>"""
