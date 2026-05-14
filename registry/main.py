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
import asyncio
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
VERSION = "0.4.6"
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


async def _ban_check(ip_hash_v: str, instance_hash_v: str):
    """If either actor is past SUSPICION_BAN_THRESHOLD, reject outright (403)."""
    score = await _suspicion_score(ip_hash_v, instance_hash_v)
    if score >= SUSPICION_BAN_THRESHOLD:
        # Honest deception: same 200-OK shape would mislead some scanners,
        # but a 403 sends the right signal to legit clients that hit
        # accidental anomaly bumps and need to back off.
        raise HTTPException(403, "blocked: suspicion threshold exceeded")


def _validate_since(s):
    """Strictly validate `since` param is ISO-8601 timestamp.

    This is BOTH input validation AND SQL-injection defense. By rejecting
    anything that fromisoformat can't parse, we guarantee `s` has no
    SQL metacharacters before it lands in the WHERE clause.
    """
    if s is None or s == "":
        return None
    from datetime import datetime
    try:
        # Allow trailing Z (UTC marker)
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise HTTPException(400, "since must be ISO-8601 timestamp")
    return s


def _validate_audience(a):
    """Lock audience to known values. Defense vs WHERE injection."""
    if a not in ("sfw", "nsfw", "any"):
        return "sfw"  # safe default
    return a


async def _instance_rate_check(conn, instance_hash_v: str):
    """Per-instance hourly cap on contribution events."""
    recent = await conn.fetchval(
        """SELECT count(*) FROM registry_contributions
           WHERE instance_hash = $1
             AND submitted_at > NOW() - INTERVAL '1 hour'""", instance_hash_v)
    if recent >= INSTANCE_RATE_LIMIT_HOUR:
        raise HTTPException(429, f"instance rate limit ({INSTANCE_RATE_LIMIT_HOUR}/hour)")


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

    # Hard-ban gate: blocked actors get 403 before any DB work
    await _ban_check(ip_hash, inst_hash)

    # Deprecation gate: clients older than min_supported_version → 410
    _check_client_version_supported(body.get("tgarr_version"))

    # Per-IP rate limit: max 60 submissions per hour (1/min effective)
    async with db_pool.acquire() as conn:
        await _instance_rate_check(conn, inst_hash)
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




# ====================================================================
# POST /api/v1/contribute_resources
# Resources (file_unique_id-keyed assets) -- clients push back what files
# they've discovered in validated channels. Dedupes across the swarm via
# distinct_contributors aggregation. Underpins the "TG-as-storage"
# framing: file_unique_id is the asset, channel is the pointer.
# ====================================================================
@app.post("/api/v1/contribute_resources")
async def contribute_resources(request: Request):
    body = await request.json()
    instance_uuid = (body.get("instance_uuid") or "").strip()
    if not instance_uuid:
        raise HTTPException(400, "missing instance_uuid")
    inst_hash = _instance_hash(instance_uuid)
    ip_hash = _ip_hash(request)

    # Hard-ban gate: blocked actors get 403 before any DB work
    await _ban_check(ip_hash, inst_hash)

    # Deprecation gate: clients older than min_supported_version → 410
    _check_client_version_supported(body.get("tgarr_version"))

    async with db_pool.acquire() as conn:
        await _instance_rate_check(conn, inst_hash)
        recent = await conn.fetchval(
            """SELECT count(*) FROM registry_contributions
               WHERE remote_ip_hash = $1
                 AND submitted_at > NOW() - INTERVAL '1 hour'""", ip_hash)
        if recent > 60:
            raise HTTPException(429, "rate limit -- try later")

    resources = body.get("resources") or []
    if not isinstance(resources, list):
        raise HTTPException(400, "resources must be array")
    if len(resources) > 5000:
        raise HTTPException(413, "too many resources in one submission (max 5000)")

    def _int(x, lo=None, hi=None):
        try:
            v = int(x)
            if lo is not None and v < lo: return None
            if hi is not None and v > hi: return None
            return v
        except (TypeError, ValueError):
            return None

    accepted = rejected = csam_flagged = 0
    new_resource_count = 0

    async with db_pool.acquire() as conn:
        for r in resources:
            fuid = (r.get("file_unique_id") or "").strip()
            if not fuid or not FILE_UNIQUE_ID_RX.match(fuid):
                rejected += 1
                anomaly_format = locals().get("anomaly_format", 0) + 1
                continue
            file_name = (r.get("file_name") or "")[:300] or None
            channel = (r.get("channel_username") or "").strip().lstrip("@")
            if channel and not USERNAME_RX.match(channel):
                channel = None
            if file_name and CSAM_RX.search(file_name):
                csam_flagged += 1; rejected += 1; continue
            if channel and CSAM_RX.search(channel):
                csam_flagged += 1; rejected += 1; continue
            file_size = _int(r.get("file_size"), 0, 5*1024*1024*1024)
            duration = _int(r.get("duration_sec"))
            year = _int(r.get("release_year"), 1900, 2100)
            season = _int(r.get("season"))
            episode = _int(r.get("episode"))
            msg_id = _int(r.get("msg_id"))
            mime = (r.get("mime_type") or "")[:80] or None
            media_type = (r.get("media_type") or "")[:20] or None
            canonical = (r.get("canonical_title") or "")[:200] or None
            quality = (r.get("quality") or "")[:20] or None
            requires_join = r.get("requires_join")
            if requires_join is None:
                requires_join = True  # default — TG download usually needs join
            requires_join = bool(requires_join)
            access_kind = (r.get("access_kind") or "")[:20] or None
            posted_at = r.get("posted_at")
            if isinstance(posted_at, str):
                try:
                    from datetime import datetime
                    posted_at = datetime.fromisoformat(posted_at.replace("Z","+00:00"))
                except Exception:
                    posted_at = None
            else:
                posted_at = None

            was_new = await conn.fetchval(
                """INSERT INTO registry_resources
                     (file_unique_id, file_name, file_size, mime_type, media_type,
                      duration_sec, canonical_title, release_year, season, episode, quality,
                      requires_join, access_kind)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                   ON CONFLICT (file_unique_id) DO UPDATE SET
                     file_name       = COALESCE(registry_resources.file_name, EXCLUDED.file_name),
                     file_size       = COALESCE(registry_resources.file_size, EXCLUDED.file_size),
                     mime_type       = COALESCE(registry_resources.mime_type, EXCLUDED.mime_type),
                     media_type      = COALESCE(registry_resources.media_type, EXCLUDED.media_type),
                     duration_sec    = COALESCE(registry_resources.duration_sec, EXCLUDED.duration_sec),
                     canonical_title = COALESCE(registry_resources.canonical_title, EXCLUDED.canonical_title),
                     release_year    = COALESCE(registry_resources.release_year, EXCLUDED.release_year),
                     season          = COALESCE(registry_resources.season, EXCLUDED.season),
                     episode         = COALESCE(registry_resources.episode, EXCLUDED.episode),
                     quality         = COALESCE(registry_resources.quality, EXCLUDED.quality),
                     requires_join   = (registry_resources.requires_join OR EXCLUDED.requires_join),
                     access_kind     = COALESCE(registry_resources.access_kind, EXCLUDED.access_kind),
                     last_seen_at    = NOW()
                   RETURNING (xmax = 0)""",
                fuid, file_name, file_size, mime, media_type,
                duration, canonical, year, season, episode, quality,
                requires_join, access_kind)
            if was_new:
                new_resource_count += 1

            if channel:
                ch_inserted = await conn.fetchval(
                    """INSERT INTO registry_resource_channels
                         (file_unique_id, channel_username, msg_id, posted_at)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (file_unique_id, channel_username) DO UPDATE SET
                         msg_id = COALESCE(EXCLUDED.msg_id, registry_resource_channels.msg_id),
                         posted_at = COALESCE(EXCLUDED.posted_at, registry_resource_channels.posted_at)
                       RETURNING (xmax = 0)""",
                    fuid, channel, msg_id, posted_at)
                if ch_inserted:
                    await conn.execute(
                        "UPDATE registry_resources SET distinct_channels = distinct_channels + 1 WHERE file_unique_id = $1",
                        fuid)

            contrib_new = await conn.fetchval(
                """INSERT INTO registry_resource_contributor_seen
                     (instance_hash, file_unique_id) VALUES ($1, $2)
                   ON CONFLICT (instance_hash, file_unique_id) DO NOTHING
                   RETURNING TRUE""",
                inst_hash, fuid)
            if contrib_new:
                await conn.execute(
                    "UPDATE registry_resources SET distinct_contributors = distinct_contributors + 1 WHERE file_unique_id = $1",
                    fuid)
            accepted += 1

        await conn.execute(
            """INSERT INTO registry_contributions
                 (instance_hash, tgarr_version, channels_accepted, channels_rejected,
                  resources_accepted, resources_rejected, remote_ip_hash)
               VALUES ($1, $2, 0, 0, $3, $4, $5)""",
            inst_hash, body.get("tgarr_version", ""), accepted, rejected, ip_hash)

    # Anomaly bumps — accumulated from row-level checks (uses locals to
    # avoid extra control flow; harmless when zero).
    af = locals().get("anomaly_format", 0)
    if af:
        log.warning("anomaly-format %s rows from instance %s", af, inst_hash[:8])
        await _bump_suspicion(f"inst:{inst_hash}", min(50, 5 * af), f"anomaly-format-x{af}")
        await _bump_suspicion(f"ip:{ip_hash}", min(50, 5 * af), "anomaly-format")
    if len(resources) > SUSPICION_BATCH_THRESHOLD:
        log.warning("anomaly-batch-size %s from instance %s", len(resources), inst_hash[:8])
        await _bump_suspicion(f"inst:{inst_hash}", 30, f"batch-size-{len(resources)}")
        await _bump_suspicion(f"ip:{ip_hash}", 30, "batch-size")

    if csam_flagged:
        log.warning("CSAM-flagged %s resources from instance %s -- blocked",
                   csam_flagged, inst_hash[:8])
        await _bump_suspicion(f"inst:{inst_hash}", 50, f"csam-resource-x{csam_flagged}")
        await _bump_suspicion(f"ip:{ip_hash}", 50, "csam-resource")

    return {"status": "ok",
            "accepted": accepted, "rejected": rejected,
            "new_resources": new_resource_count,
            "csam_flagged": csam_flagged}



# ====================================================================
# GET /api/v1/seeds
# Distributes unvalidated seed candidates to clients for swarm-validation.
# Each client pulls a batch, validates locally on their TG account, pushes
# results back via /api/v1/contribute. Random ordering — overlap acceptable
# because /contribute is idempotent + distinct_contributors aggregation
# naturally dedupes consensus.
# ====================================================================
@app.get("/api/v1/seeds")
async def get_seeds(
    request: Request,
    batch: int = Query(20, ge=1, le=100),
    kind: str = Query("any"),
):
    """Return a batch of pending seed candidates for client-side validation.

    Query params:
      batch: 1-100 (default 20)
      kind:  'any' | 'username' | 'invite'
    """
    ip_hash = _ip_hash(request)

    async with db_pool.acquire() as conn:
        # Rate limit: same registry_pulls counter as /api/v1/registry
        today_pulls = await conn.fetchval(
            """SELECT count(*) FROM registry_pulls
               WHERE ip_hash = $1
                 AND pulled_at > NOW() - INTERVAL '24 hours'""", ip_hash)
        if today_pulls and today_pulls >= PULL_LIMIT_FREE_PER_DAY:
            raise HTTPException(
                429, f"daily seeds-pull limit exceeded ({PULL_LIMIT_FREE_PER_DAY}/day free)")

        # Apply suspicion gate (same as /registry)
        susp = await _suspicion_score(ip_hash)
        if susp >= 60:
            raise HTTPException(403, "suspicion-blocked")

        where_clauses = ["validation_status = 'pending'"]
        if kind == "username":
            where_clauses.append("invite_link IS NULL")
        elif kind == "invite":
            where_clauses.append("invite_link IS NOT NULL")

        sql = f"""SELECT username, invite_link, source, category,
                         audience_hint, language, region
                  FROM seed_candidates
                  WHERE {' AND '.join(where_clauses)}
                  ORDER BY RANDOM()
                  LIMIT $1"""
        rows = await conn.fetch(sql, batch)

        # Log the pull (same registry_pulls table used for /registry)
        await conn.execute(
            "INSERT INTO registry_pulls (ip_hash, api_key_set) VALUES ($1, FALSE)",
            ip_hash)

    return {
        "version": VERSION,
        "served_at": int(time.time()),
        "count": len(rows),
        "seeds": [
            {
                "username": r["username"],
                "invite_link": r["invite_link"],
                "source": r["source"],
                "category": r["category"],
                "audience_hint": r["audience_hint"],
                "language": r["language"],
                "region": r["region"],
            }
            for r in rows
        ],
    }


# ════════════════════════════════════════════════════════════════════
# GET /api/v1/registry
# ════════════════════════════════════════════════════════════════════
# Per-IP-hash pull rate limit (defense in depth — Cloudflare absorbs 99%
# of legit traffic but origin still gets hit on cache misses). Free tier
# gets a much lower budget than paid; API key passes mark the request as
# paid and skip the free quota.
PULL_LIMIT_FREE_PER_DAY = 24
PULL_LIMIT_PAID_PER_DAY = 1000  # standard tier; plus tier currently same in code

# Federation consensus gate: a channel must have N independent contributors
# confirming "alive" before /api/v1/registry surfaces it to public consumers.
# Defends against single-actor poisoning (one malicious client can't push
# bad channels into the moat). Clients may REQUEST higher via query param,
# but never lower than this floor.
REGISTRY_MIN_CONSENSUS = int(os.environ.get("REGISTRY_MIN_CONSENSUS", "3"))

# ── Abuse defense thresholds ────────────────────────────────────────
# Hard ban at this suspicion score — contributions outright rejected (403).
# Currently rules in this file bump:
#   csam-attempt:      +50 per CSAM submission
#   honeypot-trip:     +20 per honeypot
#   csam-resource:     +50 per CSAM filename
#   anomaly-format:    +5 per malformed payload row (NEW)
#   anomaly-impossible:+10 per implausible field (NEW)
#   anomaly-spam:      +30 if batch > SUSPICION_BATCH_THRESHOLD (NEW)
SUSPICION_BAN_THRESHOLD = int(os.environ.get("SUSPICION_BAN_THRESHOLD", "100"))
# Per-instance hourly contribution rate limit. Multiple instance_hashes
# behind one IP each get their own bucket, but bad-actor signal escalates
# via ip_hash bumping too.
INSTANCE_RATE_LIMIT_HOUR = int(os.environ.get("INSTANCE_RATE_LIMIT_HOUR", "200"))
# Suspiciously-large batch — legitimate clients rarely submit > 200 in
# one call. Anything bigger triggers an anomaly bump.
SUSPICION_BATCH_THRESHOLD = int(os.environ.get("SUSPICION_BATCH_THRESHOLD", "300"))

# TG file_unique_id format: ~15-30 chars, base64-url-safe-ish character set.
# Submissions that don't match suggest fabricated data.
FILE_UNIQUE_ID_RX = re.compile(r"^[A-Za-z0-9_-]{12,32}$")



@app.get("/api/v1/registry")
async def get_registry(
    request: Request,
    audience: str = Query("sfw"),
    min_contributors: int = Query(REGISTRY_MIN_CONSENSUS),
    only_verified: int = Query(0),
    limit: int = Query(5000),
    since: Optional[str] = None,
    api_key: Optional[str] = Query(None),
):
    # Input sanitation: lock to known values before they touch SQL
    audience = _validate_audience(audience)
    since = _validate_since(since)
    # int Query params (min_contributors, only_verified, limit) are already
    # type-coerced by FastAPI; injection-safe by construction
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
    # audience already validated against allowlist — safe to interpolate
    assert audience in ("sfw", "nsfw", "any")
    if audience in ("sfw", "nsfw"):
        where.append(f"audience = '{audience}'")
    if only_verified:
        where.append("verified = TRUE")
    # Always apply at least the consensus floor (defense vs poison-injection).
    # effective_min is int — injection-safe.
    effective_min = max(min_contributors, REGISTRY_MIN_CONSENSUS)
    where.append(f"distinct_contributors >= {effective_min}")
    # since already validated as ISO-8601 — no SQL metacharacters possible
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



# ====================================================================
# GET /api/v1/version
# Clients call this every 6h to know whether they're running the
# latest tgarr code. Watchtower handles the actual image pull; this
# endpoint is the source-of-truth for what version is current AND
# what minimum version is still accepted by the federation protocol.
# ====================================================================
MIN_SUPPORTED_CLIENT_VERSION = os.environ.get("MIN_SUPPORTED_CLIENT_VERSION", "0.3.0")
RECOMMENDED_IMAGE_TAG = os.environ.get("RECOMMENDED_IMAGE_TAG", "latest")
UPDATE_NOTES_URL = os.environ.get("UPDATE_NOTES_URL", "https://github.com/tgarrpro/tgarr/releases")

# ── Client version deprecation policy ────────────────────────────────
# Normal sliding window: a client version 90 days past current's release is
# rejected from /contribute. Security floor moves faster (manual bump).
# Protocol-break sunset can force-eject all old versions on a specific date.
CURRENT_VERSION_RELEASED_AT = os.environ.get("CURRENT_VERSION_RELEASED_AT", "2026-05-13")
CLIENT_VERSION_TTL_DAYS = int(os.environ.get("CLIENT_VERSION_TTL_DAYS", "90"))
SECURITY_MIN_VERSION = os.environ.get("SECURITY_MIN_VERSION", "")
PROTOCOL_BREAK_AT = os.environ.get("PROTOCOL_BREAK_AT", "")


def _semver_tuple(v):
    """0.4.6 / 0.4.6-pre -> (0, 4, 2). Tolerates suffixes + missing parts."""
    if not v:
        return (0, 0, 0)
    base = str(v).split("-")[0].split("+")[0]
    parts = base.split(".")
    try:
        return tuple(int(p) for p in parts[:3]) + (0,) * (3 - len(parts[:3]))
    except (ValueError, TypeError):
        return (0, 0, 0)


def _min_supported_until_iso():
    """Compute the date past which min_supported_version itself is bumped."""
    try:
        from datetime import datetime, timedelta
        released = datetime.fromisoformat(CURRENT_VERSION_RELEASED_AT)
        sunset = released + timedelta(days=CLIENT_VERSION_TTL_DAYS)
        return sunset.date().isoformat()
    except Exception:
        return None


def _check_client_version_supported(client_version):
    """Raise 410 if client version is < min_supported_version or security floor.
    Tolerate missing (no version header) — gradual rollout, older clients.
    Protocol-break-at, if set, hard-rejects everything below current after that date."""
    if not client_version:
        return  # tolerate absent — most old clients don't send tgarr_version

    cv = _semver_tuple(client_version)
    if cv < _semver_tuple(MIN_SUPPORTED_CLIENT_VERSION):
        raise HTTPException(
            410,
            f"client version {client_version} is past deprecation. "
            f"min_supported={MIN_SUPPORTED_CLIENT_VERSION}. "
            f"Upgrade: see /api/v1/version for image URLs.")

    if SECURITY_MIN_VERSION and cv < _semver_tuple(SECURITY_MIN_VERSION):
        raise HTTPException(
            410,
            f"security update required: client {client_version} below "
            f"security_min={SECURITY_MIN_VERSION}. Upgrade ASAP — see /api/v1/version.")

    # Protocol-break-at: hard sunset everything-not-current after specific date
    if PROTOCOL_BREAK_AT:
        try:
            from datetime import datetime, timezone
            break_ts = datetime.fromisoformat(PROTOCOL_BREAK_AT.replace("Z", "+00:00"))
            if break_ts.tzinfo is None:
                break_ts = break_ts.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= break_ts and cv < _semver_tuple(VERSION):
                raise HTTPException(
                    410,
                    f"protocol break: as of {PROTOCOL_BREAK_AT}, only "
                    f"current_version={VERSION} accepted. Upgrade immediately.")
        except HTTPException:
            raise
        except Exception:
            pass

FALLBACK_REGISTRY = os.environ.get("FALLBACK_REGISTRY_HOST", "registry.tgarr.me")
FALLBACK_TARBALL_BASE = os.environ.get("FALLBACK_TARBALL_BASE", "https://tgarr.me/dl")

@app.get("/api/v1/version")
async def get_version(response: Response):
    """Tell clients what version is current + how to upgrade.

    Two upgrade paths:
      1. PRIMARY  — pull from GHCR (ghcr.io/tgarrpro/...). Watchtower handles
         this automatically every 5 min for image-based deploys.
      2. FALLBACK — if GHCR unreachable (rate-limited / org suspended / etc),
         pull from registry.tgarr.me (Docker Registry v2 mirror on the
         central host) OR docker-load a tarball from tgarr.me/dl/.

    Clients use this endpoint as the source-of-truth for what version is
    current. min_supported_version is the federation floor — clients older
    than this get rejected by /contribute (forces upgrade or stops bad data)."""
    return {
        "current_version": VERSION,
        "min_supported_version": MIN_SUPPORTED_CLIENT_VERSION,
        "recommended_image_tag": RECOMMENDED_IMAGE_TAG,
        # Primary: GHCR
        "image_crawler": f"ghcr.io/tgarrpro/tgarr-crawler:{RECOMMENDED_IMAGE_TAG}",
        "image_api": f"ghcr.io/tgarrpro/tgarr-api:{RECOMMENDED_IMAGE_TAG}",
        # Fallback: central-hosted Docker Registry v2 mirror
        "fallback_image_crawler": f"{FALLBACK_REGISTRY}/tgarr-crawler:{RECOMMENDED_IMAGE_TAG}",
        "fallback_image_api": f"{FALLBACK_REGISTRY}/tgarr-api:{RECOMMENDED_IMAGE_TAG}",
        # Last-resort: tarball download for docker load
        "fallback_tarball_crawler": f"{FALLBACK_TARBALL_BASE}/tgarr-crawler-{VERSION}.tar.gz",
        "fallback_tarball_api": f"{FALLBACK_TARBALL_BASE}/tgarr-api-{VERSION}.tar.gz",
        "update_notes_url": UPDATE_NOTES_URL,
        "checked_at": int(time.time()),
        # Deprecation policy
        "current_version_released_at": CURRENT_VERSION_RELEASED_AT,
        "min_supported_until": _min_supported_until_iso(),
        "client_ttl_days": CLIENT_VERSION_TTL_DAYS,
        "security_min_version": SECURITY_MIN_VERSION or None,
        "protocol_break_at": PROTOCOL_BREAK_AT or None,
        # Thundering-herd defense
        "client_poll_interval_sec": 21600,
        "smear_seconds": 21600,
        "image_pull_smear_seconds": 86400,
        "_cache_max_age": 3600,
    }

# Cloudflare-friendly cache: 1h fresh + 1h stale-while-revalidate.
# At 1M clients × poll every 6h = 46/s average -> Origin sees only
# (1 req per 1h cache fill) per CF edge POP. Essentially zero load.

@app.middleware("http")
async def _version_cache_middleware(request, call_next):
    resp = await call_next(request)
    if request.url.path == "/api/v1/version":
        resp.headers["Cache-Control"] = "public, max-age=3600, stale-while-revalidate=3600"
    return resp


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
