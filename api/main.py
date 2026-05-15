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
from fastapi import FastAPI, Form, Header, Query, Response, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

import login  # local module
import metadata as md  # local module

DB_DSN = os.environ["DB_DSN"]
TGARR_VERSION = "0.4.53"
ANY_API_KEY_ACCEPTED = True

app = FastAPI(title="tgarr", version=TGARR_VERSION)
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
    asyncio.create_task(_audio_metadata_worker())
    asyncio.create_task(_stats_snapshot_worker())
    asyncio.create_task(_ebook_queue_restore())


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
            ALTER TABLE messages ADD COLUMN IF NOT EXISTS file_dc INTEGER;
            CREATE TABLE IF NOT EXISTS worker_status (
              worker TEXT PRIMARY KEY,
              last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              last_action TEXT,
              iter_count BIGINT NOT NULL DEFAULT 0,
              error_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              last_error_at TIMESTAMPTZ
            );
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS deep_backfilled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS deep_oldest_tg_id BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS deep_last_run_at TIMESTAMPTZ;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS deep_total_pulled BIGINT NOT NULL DEFAULT 0;
            CREATE INDEX IF NOT EXISTS idx_channels_deep_backfilled ON channels (deep_backfilled);
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_msgs BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_photos BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_videos BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_audio BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_documents BIGINT;
            ALTER TABLE channels ADD COLUMN IF NOT EXISTS remote_counts_refreshed_at TIMESTAMPTZ;
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
from fastapi.responses import FileResponse, StreamingResponse
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


_TINY_TRANSPARENT_GIF = bytes.fromhex(
    "47494638396101000100800000ffffff00000021f90401000000002c00000000"
    "010001000002024401003b"
)


@app.get("/api/thumb/{msg_id}")
async def lazy_thumb(msg_id: int):
    """On-demand thumb materialization. Cached -> serve. Uncached -> mark
    thumb_path='__user_queued__' so crawler thumb_downloader picks it up,
    poll up to ~12s for the file to land, fallback to 202 + 1x1 GIF.
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
    for _ in range(24):
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
                return FileResponse(fpath, media_type="image/jpeg",
                                  headers={"Cache-Control": "public, max-age=604800"})
        if tp in ("__failed__", "__deleted__"):
            return Response(_TINY_TRANSPARENT_GIF, status_code=410,
                           media_type="image/gif")
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

    # Queue download if not already + wait for file to start appearing
    if not row["local_path"] or row["local_path"] == "__user_queued__":
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET local_path = $$__user_queued__$$ "
                "WHERE id = $1 AND (local_path IS NULL "
                "OR local_path = $$__user_queued__$$)",
                msg_id)
        for _ in range(180):  # wait up to 90s for download to start writing
            await asyncio.sleep(0.5)
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

    async def stream_range():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            stuck_iters = 0
            while remaining > 0:
                current = os.path.getsize(path)
                target_pos = f.tell()
                if current <= target_pos:
                    # Partial file caught up — wait for crawler to write more.
                    # Abort if download appears stalled.
                    stuck_iters += 1
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
        _EBOOK_CONVERT_SEMAPHORE = asyncio.Semaphore(2)
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
            import resource
            # RLIMIT_AS dropped: Chromium virt mem >> resident, container mem_limit is real bound
            resource.setrlimit(resource.RLIMIT_CPU, (1800, 1800))  # 30 min CPU cap
            # RLIMIT_FSIZE dropped: calibre temp files can be >200MB
        proc = await asyncio.create_subprocess_exec(
            "ebook-convert", src, tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env, preexec_fn=_set_limits)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
        except asyncio.TimeoutError:
            proc.kill()
            print(f'[ebook-convert] TIMEOUT src={src}', flush=True)
            return False
        if proc.returncode != 0:
            print(f'[ebook-convert] rc={proc.returncode} stderr={stderr.decode(errors="replace")[-500:]!r}', flush=True)
        if proc.returncode == 0 and os.path.exists(tmp):
            os.rename(tmp, pdf)
            _EBOOK_JOBS.pop(msg_id, None)
            try: os.unlink(src + ".queue")
            except OSError: pass
            return True
        if os.path.exists(tmp):
            os.unlink(tmp)
        _EBOOK_JOBS[msg_id] = {"state": "failed", "started_at": _EBOOK_JOBS.get(msg_id, {}).get("started_at", 0)}
        # Keep .queue marker so restart will retry
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
            "SELECT local_path, file_name FROM messages WHERE id=$1", msg_id)
    if not row or not row["local_path"] or row["local_path"].startswith("__"):
        return {"state": "no_source"}
    ext = (row["file_name"] or "").rsplit(".", 1)[-1].lower()
    src, pdf, _ = _ebook_paths(row["local_path"])
    if not os.path.exists(src):
        return {"state": "no_source"}
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
        return {"state": "failed", "reason": "conversion error — check api logs"}
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
            "SELECT local_path, file_name FROM messages WHERE id=$1", msg_id)
    if not row or not row["local_path"] or row["local_path"].startswith("__"):
        return Response("not cached — open via /watch to trigger download",
                        status_code=404)
    ext = (row["file_name"] or "").rsplit(".", 1)[-1].lower()
    src, pdf, _ = _ebook_paths(row["local_path"])
    if not os.path.exists(src):
        return Response("source file missing", status_code=410)
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


@app.get("/epub/{msg_id}", response_class=HTMLResponse)
async def epub_render(msg_id: int):
    """Server-side epub renderer. Concatenates spine chapters into one
    long HTML page, rewrites image refs, applies CJK-friendly typography.
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT local_path, file_name FROM messages WHERE id=$1", msg_id)
    epub_path = _epub_locate(msg_id, row)
    if not epub_path:
        return HTMLResponse("<h1>not cached</h1>", status_code=404)
    title = row["file_name"] or f"book-{msg_id}"
    try:
        with _zipfile.ZipFile(epub_path) as zf:
            # Find OPF via META-INF/container.xml
            container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
            from xml.etree import ElementTree as _ET
            ns_c = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            opf_node = _ET.fromstring(container).find(".//c:rootfile", ns_c)
            if opf_node is None:
                raise RuntimeError("OPF not declared in container.xml")
            opf_path = opf_node.attrib["full-path"]
            opf_dir = os.path.dirname(opf_path).replace("\\", "/")
            opf_root = _ET.fromstring(zf.read(opf_path).decode("utf-8", "replace"))
            ns_opf = {"opf": "http://www.idpf.org/2007/opf"}
            manifest = {item.attrib["id"]: item.attrib["href"]
                        for item in opf_root.findall(".//opf:manifest/opf:item", ns_opf)}
            spine_ids = [item.attrib["idref"]
                         for item in opf_root.findall(".//opf:spine/opf:itemref", ns_opf)]

            chapters_html = []
            for idref in spine_ids:
                href = manifest.get(idref)
                if not href:
                    continue
                href = _uq(href)
                chapter_path = (opf_dir + "/" + href).lstrip("/") if opf_dir else href
                chapter_path = _re.sub(r"/+", "/", chapter_path)
                try:
                    raw = zf.read(chapter_path)
                except KeyError:
                    continue
                raw_html = raw.decode("utf-8", errors="replace")
                # Extract <body>
                m = _re.search(r"<body[^>]*>(.*?)</body>", raw_html,
                              _re.DOTALL | _re.IGNORECASE)
                body_html = m.group(1) if m else raw_html

                # Rewrite img/css refs to /epub/{id}/asset/<absolute-in-zip>
                chapter_dir = os.path.dirname(chapter_path).replace("\\", "/")
                def fix_ref(match, attr):
                    src = _uq(match.group(1))
                    if src.startswith(("http://", "https://", "data:", "/")):
                        return match.group(0)
                    abs_in_zip = os.path.normpath(
                        os.path.join(chapter_dir, src)).replace("\\", "/")
                    new_url = f"/epub/{msg_id}/asset/{_q(abs_in_zip)}"
                    return match.group(0).replace(match.group(1), new_url)

                body_html = _re.sub(r"<img[^>]*\bsrc=[\"\']([^\"\']+)[\"\']",
                                   lambda mm: fix_ref(mm, "src"), body_html)
                # SVG covers use <image xlink:href="..."> or <image href="...">
                body_html = _re.sub(r"<image[^>]*\b(?:xlink:)?href=[\"\']([^\"\']+)[\"\']",
                                   lambda mm: fix_ref(mm, "href"), body_html)
                # Strip <link rel=stylesheet> — chapter CSS would leak into reader
                body_html = _re.sub(r"<link[^>]*\brel=[\"\']stylesheet[\"\'][^>]*/?>", "",
                                   body_html, flags=_re.IGNORECASE)
                # Strip <script> blocks (no JS in our reader)
                body_html = _re.sub(r"<script[^>]*>.*?</script>", "",
                                   body_html, flags=_re.DOTALL | _re.IGNORECASE)
                # Strip inline event handlers (onclick="...")
                body_html = _re.sub(r"\son[a-z]+\s*=\s*[\"\'][^\"\']*[\"\']", "",
                                   body_html, flags=_re.IGNORECASE)
                # Clamp SVG: epubs embed cover as <svg width="100%" height="100%">
                # which expands to whole viewport. Strip width/height so our CSS wins.
                body_html = _re.sub(r"(<svg[^>]*?)\s(?:width|height)=[\"\'][^\"\']*[\"\']",
                                   r"\1", body_html, count=2, flags=_re.IGNORECASE)
                body_html = _re.sub(r"(<image[^>]*?)\s(?:width|height)=[\"\'][^\"\']*[\"\']",
                                   r"\1", body_html, count=2, flags=_re.IGNORECASE)

                chapters_html.append(body_html)

        body = "<hr class='chap-sep'/>".join(chapters_html)
        css = (
            "html,body{margin:0;padding:0}"
            "body{background:#f5f7fa;font-family:'PingFang SC','Microsoft YaHei',"
            "'Noto Sans CJK SC','Hiragino Sans GB',system-ui,sans-serif}"
            ".bar{position:sticky;top:0;background:#fff;padding:12px 20px;"
            "border-bottom:1px solid #e2e8f0;z-index:10;display:flex;gap:14px;"
            "align-items:center;box-shadow:0 1px 4px rgba(0,0,0,0.04)}"
            ".bar a{color:#64748b;text-decoration:none;font-size:14px}"
            ".bar a:hover{color:#0f172a}"
            ".bar .title{font-weight:600;color:#0f172a;flex:1;overflow:hidden;"
            "text-overflow:ellipsis;white-space:nowrap}"
            ".book{max-width:760px;margin:30px auto;padding:50px 56px;"
            "background:#fff;border-radius:10px;box-shadow:0 4px 14px rgba(0,0,0,0.08);"
            "font-size:17px;line-height:1.9;color:#1f2937}"
            ".book h1,.book h2,.book h3,.book h4{color:#0f172a;font-weight:600;"
            "margin:1.8em 0 0.6em;line-height:1.4}"
            ".book h1{font-size:1.6em;border-bottom:2px solid #e2e8f0;padding-bottom:0.4em}"
            ".book h2{font-size:1.35em}"
            ".book h3{font-size:1.15em}"
            ".book p{margin:1em 0;text-indent:2em}"
            ".book img{max-width:100%;height:auto;display:block;margin:1.8em auto;"
            "border-radius:4px}"
            ".book svg{max-width:420px;width:100%;height:auto;display:block;margin:0 auto 2em}"
            ".book svg image{width:100%;height:auto}"
            ".book a{color:#3b82f6;text-decoration:none}"
            ".book a:hover{text-decoration:underline}"
            ".book blockquote{margin:1.4em 0;padding:0.4em 1.4em;border-left:4px solid #93c5fd;"
            "color:#475569;background:#f1f5f9;border-radius:0 6px 6px 0}"
            "hr.chap-sep{border:none;border-top:1px dashed #cbd5e1;margin:3em 0}"
            "@media (max-width:768px){.book{margin:14px;padding:24px}}"
        )
        return HTMLResponse(
            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)}</title>"
            f"<style>{css}</style></head><body>"
            f"<div class='bar'><a href='javascript:history.back()'>\u2190 back</a>"
            f"<div class='title'>{html.escape(title)}</div>"
            f"<a href='/media/{msg_id}' download>\u2b07 raw</a></div>"
            f"<div class='book'>{body}</div></body></html>"
        )
    except Exception as e:
        return HTMLResponse(
            f"<h1>EPUB render failed</h1><pre>{html.escape(str(e))}</pre>"
            f"<p><a href='/media/{msg_id}' download>Download raw .epub</a></p>",
            status_code=500)


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
        headers={"Cache-Control": "public, max-age=3600"})


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
    file_dc = row["file_dc"]
    my_dc = await _get_my_dc()
    cross_dc = bool(file_dc and my_dc and file_dc != my_dc)
    if lp == "__failed__":
        return JSONResponse({"status": "failed", "file_dc": file_dc,
                            "my_dc": my_dc, "cross_dc": cross_dc})
    if not lp or lp == "__user_queued__":
        return JSONResponse({"status": "queued", "bytes": 0, "total": total,
                            "file_dc": file_dc, "my_dc": my_dc, "cross_dc": cross_dc})
    path = os.path.join("/downloads", lp)
    bytes_ = os.path.getsize(path) if os.path.exists(path) else 0
    if total and bytes_ >= total:
        return JSONResponse({"status": "ready", "bytes": bytes_, "total": total,
                            "mime": row["mime_type"], "media_type": row["media_type"],
                            "file_name": row["file_name"],
                            "file_dc": file_dc, "my_dc": my_dc, "cross_dc": cross_dc})
    return JSONResponse({"status": "downloading", "bytes": bytes_, "total": total,
                        "mime": row["mime_type"], "media_type": row["media_type"],
                        "file_name": row["file_name"],
                        "file_dc": file_dc, "my_dc": my_dc, "cross_dc": cross_dc})


@app.get("/watch/{msg_id}", response_class=HTMLResponse)
async def watch_page(msg_id: int):
    """Loading-aware media viewer. Polls /api/media-status until enough bytes
    are written to start playback, then renders <video>/<audio>/<iframe>."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT m.local_path, m.file_name, m.file_size, m.media_type,
                      m.mime_type, c.title AS ch_title
               FROM messages m LEFT JOIN channels c ON c.id=m.channel_id
               WHERE m.id=$1""", msg_id)
    if not row:
        return HTMLResponse("<h1>404 — message not indexed</h1>", status_code=404)
    title = row["file_name"] or f"msg-{msg_id}"
    mt = row["media_type"] or "document"

    # Trigger queueing immediately so the user doesn\'t wait for first poll.
    if not row["local_path"]:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET local_path = $$__user_queued__$$ "
                "WHERE id = $1 AND local_path IS NULL",
                msg_id)

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(title)} — tgarr</title>
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
</style></head><body>
<div class="bar">
  <a href="javascript:history.back()">← back</a>
  <div class="title">{html.escape(title)}</div>
  <a href="/media/{msg_id}" download>⬇ download raw</a>
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
const MEDIA_TYPE = {repr(mt)};
const MEDIA_FILE_NAME = {repr(row["file_name"] or "")};
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
    main.innerHTML = `<video controls autoplay src="/media/${{MSG_ID}}"></video>`;
    return;
  }}
  if (MEDIA_TYPE === "audio") {{
    main.innerHTML = `<audio controls autoplay src="/media/${{MSG_ID}}"></audio>`;
    return;
  }}
  // document — pick by extension
  const ext = (MEDIA_FILE_NAME || "").toLowerCase().split(".").pop();
  if (ext === "pdf") {{
    main.innerHTML = `<iframe src="/media/${{MSG_ID}}" style="height:90vh;width:1200px;max-width:96vw;border:0;background:#fff"></iframe>`;
    return;
  }}
  if (["epub","mobi","azw","azw3","djvu","fb2","lit","cbr","cbz"].includes(ext)) {{
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
          main.innerHTML = `<iframe src="${{j.url}}" style="height:90vh;width:1200px;max-width:96vw;border:0;background:#fff"></iframe>`;
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
        if (j.state === "queued") {{
          msg = `queued — position ${{j.position}} in line`;
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
    if (j.status === "failed") {{
      document.getElementById("status").textContent = "✗ download failed";
      document.getElementById("hint").textContent = "Telegram channel may be invalid or banned";
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
      // playback can then keep up with the download.
      if (j.bytes > 2_000_000 || (j.total && pct > 10)) {{
        renderPlayer();
        return;
      }}
    }} else if (j.status === "queued") {{
      document.getElementById("status").textContent = "queued — waiting for crawler…";
    }}
  }} catch(e) {{
    document.getElementById("status").textContent = "network error — retrying…";
  }}
  setTimeout(poll, 1500);
}}
poll();
</script>
</body></html>"""
    return HTMLResponse(body)


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
.poster-card .poster { height:200px; background:#f1f5f9; position:relative; overflow:hidden; }
.poster-card .poster .fallback { display:flex; width:100%; height:100%; align-items:center; justify-content:center; font-size:56px; color:#cbd5e1; background:#f1f5f9; }
.poster-card .poster .poster-img { display:block; width:100%; height:100%; object-fit:cover; background:#f1f5f9; }
.poster-card .poster .badge { position:absolute; top:8px; right:8px; padding:3px 10px; border-radius:11px; background:rgba(15,23,42,0.78); color:#fff; font-size:11px; font-weight:700; letter-spacing:0.5px; text-transform:uppercase; backdrop-filter:blur(4px); }
.poster-card .info { padding:10px 12px 4px; min-height:84px; flex:1; }
.poster-card .info .title { font-weight:700; font-size:15px; line-height:1.3; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; min-height:39px; color:var(--fg); }
.poster-card .info .sub { font-size:13px; color:var(--muted); margin-top:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.poster-card .pills { padding:0 12px 8px; display:flex; gap:4px; flex-wrap:nowrap; overflow:hidden; min-height:24px; }
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
    ("/system",    "System",    "M19.43 12.98c.04-.32.07-.64.07-.98s-.03-.66-.07-.98l2.11-1.65c.19-.15.24-.42.12-.64l-2-3.46c-.12-.22-.39-.3-.61-.22l-2.49 1c-.52-.4-1.08-.73-1.69-.98l-.38-2.65A.488.488 0 0 0 14 2h-4c-.25 0-.46.18-.49.42l-.38 2.65c-.61.25-1.17.59-1.69.98l-2.49-1c-.23-.09-.49 0-.61.22l-2 3.46c-.13.22-.07.49.12.64l2.11 1.65c-.04.32-.07.65-.07.98s.03.66.07.98l-2.11 1.65c-.19.15-.24.42-.12.64l2 3.46c.12.22.39.3.61.22l2.49-1c.52.4 1.08.73 1.69.98l.38 2.65c.03.24.24.42.49.42h4c.25 0 .46-.18.49-.42l.38-2.65c.61-.25 1.17-.59 1.69-.98l2.49 1c.23.09.49 0 .61-.22l2-3.46c.12-.22.07-.49-.12-.64l-2.11-1.65zM12 15.5c-1.93 0-3.5-1.57-3.5-3.5s1.57-3.5 3.5-3.5 3.5 1.57 3.5 3.5-1.57 3.5-3.5 3.5z"),
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
            "error_count, last_error, last_error_at "
            "FROM worker_status ORDER BY last_seen DESC")
    out = []
    seen = set()
    for r in rows:
        seen.add(r["worker"])
        expected_max = _EXPECTED_WORKERS.get(r["worker"], 3600)
        out.append({
            "worker": r["worker"],
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "last_action": r["last_action"],
            "iter_count": r["iter_count"],
            "error_count": r["error_count"],
            "last_error": r["last_error"],
            "last_error_at": r["last_error_at"].isoformat() if r["last_error_at"] else None,
            "health": _worker_health(r["worker"], r["last_seen"], expected_max),
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


@app.get("/api/deep-backfill-progress")
async def api_deep_backfill_progress():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT username, title, deep_backfilled, deep_oldest_tg_id,
                   deep_last_run_at, deep_total_pulled,
                   remote_msgs, remote_photos, remote_videos,
                   remote_audio, remote_documents,
                   (SELECT count(*) FROM messages m WHERE m.channel_id = channels.id) AS msgs_local,
                   (SELECT count(*) FROM messages m WHERE m.channel_id = channels.id AND m.media_type='photo') AS local_photos,
                   (SELECT count(*) FROM messages m WHERE m.channel_id = channels.id AND m.media_type='video') AS local_videos,
                   (SELECT count(*) FROM messages m WHERE m.channel_id = channels.id AND m.media_type='audio') AS local_audio,
                   (SELECT count(*) FROM messages m WHERE m.channel_id = channels.id AND m.media_type='document') AS local_documents
            FROM channels
            WHERE subscribed = TRUE
            ORDER BY deep_backfilled ASC, COALESCE(remote_msgs, 0) DESC
        """)
    def pct(local, remote):
        if not remote or remote <= 0:
            return None
        return round(100.0 * (local or 0) / remote, 1)
    return {"channels": [
        {"username": r["username"], "title": r["title"],
         "done": r["deep_backfilled"],
         "oldest_id": r["deep_oldest_tg_id"],
         "last_run_at": r["deep_last_run_at"].isoformat() if r["deep_last_run_at"] else None,
         "pulled_deep": r["deep_total_pulled"],
         "msgs_local": r["msgs_local"],
         "remote_msgs": r["remote_msgs"],
         "pct_msgs": pct(r["msgs_local"], r["remote_msgs"]),
         "local_photos": r["local_photos"], "remote_photos": r["remote_photos"],
         "pct_photos": pct(r["local_photos"], r["remote_photos"]),
         "local_videos": r["local_videos"], "remote_videos": r["remote_videos"],
         "pct_videos": pct(r["local_videos"], r["remote_videos"]),
         "local_audio": r["local_audio"], "remote_audio": r["remote_audio"],
         "pct_audio": pct(r["local_audio"], r["remote_audio"]),
         "local_documents": r["local_documents"], "remote_documents": r["remote_documents"],
         "pct_documents": pct(r["local_documents"], r["remote_documents"])}
        for r in rows]}


@app.get("/system", response_class=HTMLResponse)
async def page_system():
    if not login.session_exists():
        return RedirectResponse("/login")
    body = """
<h2 class="section">System</h2>
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
  const c = {healthy:"#22c55e",idle:"#eab308",stale:"#ef4444",never:"#94a3b8"}[h] || "#94a3b8";
  return `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${c};margin-right:6px"></span>`;
}
async function loadWorkers() {
  const r = await fetch("/api/workers");
  const j = await r.json();
  const rows = (j.workers || []).map(w => `
    <tr><td>${healthDot(w.health)}<b>${w.worker}</b></td>
        <td>${fmtAgo(w.last_seen)}</td>
        <td style="color:#64748b;font-size:13px">${w.last_action || "—"}</td>
        <td style="text-align:right;color:#64748b">${w.iter_count.toLocaleString()}</td>
        <td style="text-align:right;color:${w.error_count?"#ef4444":"#64748b"}">${w.error_count}</td>
        <td style="color:#ef4444;font-size:12px">${w.last_error || ""}</td></tr>`);
  document.getElementById("workersTable").innerHTML = `<table style="width:100%;border-collapse:collapse">
    <thead><tr style="text-align:left;color:#64748b;font-size:12px">
    <th>Worker</th><th>Last seen</th><th>Last action</th>
    <th style="text-align:right">Iters</th><th style="text-align:right">Errors</th>
    <th>Last error</th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
}
async function loadDeepBackfill() {
  const r = await fetch("/api/deep-backfill-progress");
  const j = await r.json();
  const rows = (j.channels || []).map(c => `
    <tr><td>${c.done?"✅":"🔄"} <b>@${c.username || "—"}</b></td>
        <td>${(c.title || "").substring(0, 40)}</td>
        <td style="text-align:right">${c.msgs_local.toLocaleString()}</td>
        <td style="text-align:right;color:#64748b">+${(c.pulled_deep || 0).toLocaleString()}</td>
        <td style="text-align:right;color:#64748b;font-size:12px">${fmtAgo(c.last_run_at)}</td></tr>`);
  document.getElementById("deepBackfillTable").innerHTML = `<table style="width:100%;border-collapse:collapse">
    <thead><tr style="text-align:left;color:#64748b;font-size:12px">
    <th>Status</th><th>Title</th>
    <th style="text-align:right">Msgs local</th>
    <th style="text-align:right">Pulled deep</th>
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
    return HTMLResponse(_layout("System", "/system", body))


@app.get("/")
async def root(accept: Optional[str] = Header(None)):
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
    worker_banner = """
<div id="workerBanner" style="display:none;background:#fef3c7;border:1px solid #f59e0b;
  border-radius:8px;padding:10px 14px;margin:0 0 16px 0;color:#92400e;font-size:13px">
  <b>⚠ Workers need attention:</b> <span id="workerBannerList"></span>
  <a href="/system" style="margin-left:8px;color:#92400e;text-decoration:underline">view system →</a>
</div>
<script>
(async function checkWorkers() {
  try {
    const r = await fetch("/api/workers");
    const j = await r.json();
    if (!j.all_healthy && j.stale_or_never && j.stale_or_never.length) {
      document.getElementById("workerBannerList").textContent = j.stale_or_never.join(", ");
      document.getElementById("workerBanner").style.display = "block";
    } else {
      document.getElementById("workerBanner").style.display = "none";
    }
  } catch (e) {}
  setTimeout(checkWorkers, 30000);
})();
</script>
"""
    stats_html = "".join(
        f'<div class="stat-card"><div class="n">{v:,}</div><div class="l">{label}</div></div>'
        for v, label in [
            (s["channels"], "channels"),
            (s["messages"], "messages indexed"),
            (s["releases"], "parsed releases"),
            (s["videos"], "videos"),
            (s["photos"], "photos"),
            (s["audio"], "audio"),
            (s["books"], "books"),
            (s["pending"], "pending"),
            (s["downloading"], "downloading"),
            (s["completed"], "completed"),
        ]
    )
    charts_html = """
<section class="dashboard-charts">
  <div class="chart-card chart-line">
    <header>
      <h3>Growth</h3>
      <div class="chart-controls">
        <select id="growthMetric">
          <option value="messages" selected>messages</option>
          <option value="videos">videos</option>
          <option value="releases">releases</option>
          <option value="channels">channels</option>
          <option value="photos">photos</option>
          <option value="books">books</option>
        </select>
        <select id="growthDays">
          <option value="7" selected>7d</option>
          <option value="30">30d</option>
          <option value="90">90d</option>
        </select>
      </div>
    </header>
    <div class="chart-canvas-wrap"><canvas id="growthChart"></canvas></div>
  </div>
  <div class="chart-card chart-pie">
    <header><h3>Media mix</h3></header>
    <div class="chart-canvas-wrap"><canvas id="distChart"></canvas></div>
  </div>
</section>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<script>
(function() {
  let growth = null, dist = null;
  function fmtDate(iso) { return iso.slice(5, 10); }
  function color(i) {
    const c = ['#5eb6e5','#94d2bd','#e9c46a','#f4a261','#e76f51','#a8dadc','#bdb2ff'];
    return c[i % c.length];
  }
  async function loadGrowth() {
    const metric = document.getElementById('growthMetric').value;
    const days   = document.getElementById('growthDays').value;
    const r = await fetch(`/api/stats/history?metric=${metric}&days=${days}`);
    const j = await r.json();
    const labels = (j.points || []).map(p => fmtDate(p.at));
    const data   = (j.points || []).map(p => p.value);
    const ctx = document.getElementById('growthChart').getContext('2d');
    if (growth) growth.destroy();
    growth = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets: [{
        label: metric, data,
        fill: true, backgroundColor: 'rgba(59,130,246,0.10)',
        borderColor: '#3b82f6', borderWidth: 2, tension: 0.35,
        pointRadius: 3, pointHoverRadius: 5
      }]},
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { beginAtZero: false, ticks: { color: '#475569' }, grid: { color: '#e2e8f0' } },
          x: { ticks: { color: '#475569' }, grid: { color: '#f1f5f9' } } }
      }
    });
  }
  async function loadDist() {
    const r = await fetch('/api/stats/distribution');
    const j = await r.json();
    const labels = (j.slices || []).map(s => s.label);
    const data   = (j.slices || []).map(s => s.value);
    const ctx = document.getElementById('distChart').getContext('2d');
    if (dist) dist.destroy();
    dist = new Chart(ctx, {
      type: 'doughnut',
      data: { labels, datasets: [{ data,
        backgroundColor: labels.map((_, i) => color(i)),
        borderColor: '#ffffff', borderWidth: 2
      }]},
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'right',
          labels: { color: '#334155', boxWidth: 14, padding: 8, font: { size: 12 } } } }
      }
    });
  }
  document.getElementById('growthMetric').addEventListener('change', loadGrowth);
  document.getElementById('growthDays').addEventListener('change', loadGrowth);
  loadGrowth();
  loadDist();
})();
</script>
<style>
.dashboard-charts { display: grid; grid-template-columns: 2fr 1fr; gap: 16px;
  margin: 20px 0; }
.dashboard-charts .chart-card { background: var(--surface, #fff);
  border: 1px solid var(--border, #e2e8f0);
  border-radius: 8px; padding: 14px 16px; }
.dashboard-charts .chart-card header { display: flex; align-items: center;
  justify-content: space-between; margin-bottom: 8px; }
.dashboard-charts .chart-card h3 { margin: 0; font-size: 12px; font-weight: 600;
  color: #64748b; text-transform: uppercase; letter-spacing: 0.06em; }
.dashboard-charts .chart-controls select { background: #fff; color: #1e293b;
  border: 1px solid var(--border, #e2e8f0); padding: 4px 8px; border-radius: 4px;
  font-size: 12px; margin-left: 6px; }
.dashboard-charts .chart-canvas-wrap { position: relative; height: 240px; width: 100%; }
.dashboard-charts canvas { width: 100% !important; height: 100% !important; }
@media (max-width: 900px) {
  .dashboard-charts { grid-template-columns: 1fr; }
}
</style>
"""

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
{worker_banner}
<div class="stats">{stats_html}</div>{charts_html}

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
        # Auto-subscribed check — TG sometimes returns differently-cased username
        # (eBookRoom vs ebookroom) so match case-insensitively.
        known = {r["username"].lower(): r["subscribed"]
                 for r in await conn.fetch(
                     "SELECT username, subscribed FROM channels "
                     "WHERE username IS NOT NULL")}
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
        state = known.get(u.lower())
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
        '  <button type="button" class="ghost" id="tgPullBtn" style="font-size:13px" onclick="tgRegistryPull(this)">↻ Pull now</button>'
        '  <span id="tgPullToast" style="font-size:13px;color:var(--muted);align-self:center;display:none"></span>'
        '</div>'
        f'<div class="dl-list">{"".join(_card(r) for r in rows)}</div>'
        '<script>'
        '(() => {'
        '  window.tgRegistryPull = async function(btn) {'
        '    const toast = document.getElementById("tgPullToast");'
        '    btn.disabled = true; btn.textContent = "Pulling…";'
        '    toast.style.display = "inline";'
        '    toast.textContent = "waiting for crawler (up to 70s)…";'
        '    toast.style.color = "var(--muted)";'
        '    try {'
        '      const r = await fetch("/api/registry/pull-now", {method:"POST"});'
        '      const j = await r.json();'
        '      if (j.status === "ok") {'
        '        toast.textContent = `\u2713 pull complete \u2014 ${j.affected} channel(s) updated`;'
        '        toast.style.color = "var(--ok)";'
        '        if (j.affected > 0) setTimeout(() => location.reload(), 1200);'
        '      } else if (j.status === "queued") {'
        '        toast.textContent = "\u23F3 queued \u2014 crawler busy, will run soon";'
        '        toast.style.color = "var(--warn)";'
        '      } else {'
        '        toast.textContent = "\u2717 " + (j.message || "error");'
        '        toast.style.color = "var(--err)";'
        '      }'
        '    } catch (e) {'
        '      toast.textContent = "\u2717 network error";'
        '      toast.style.color = "var(--err)";'
        '    } finally {'
        '      btn.disabled = false; btn.textContent = "\u21BB Pull now";'
        '    }'
        '  };'
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
@app.get("/gallery", response_class=HTMLResponse)
async def page_gallery(channel: Optional[str] = None, limit: int = 240,
                       q: Optional[str] = None):
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
    if q:
        for w in q.split():
            params.append(f"%{w}%")
            where.append(f"(m.caption ILIKE ${len(params)} OR m.file_name ILIKE ${len(params)})")
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

    dup_saved = max(0, total - unique_total) if unique_total else 0
    body = (
        f'<h2 class="section">Gallery <span class="count">{len(rows)} unique · {unique_total:,} total dedup</span>'
        + (f' · <span style="color:var(--muted);font-size:13px">{dup_saved:,} dup'
           f'{f" · {pending:,} indexed (click to fetch)" if pending else ""}</span>'
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
        '  function hookLazy(img) {'
        '    if (img._tgHook) return; img._tgHook = true; img._tgTry = 0;'
        '    img.addEventListener("load", () => {'
        '      if (img.naturalWidth <= 1 && img._tgTry < 4) {'
        '        img._tgTry++;'
        '        const base = img.src.split("?")[0];'
        '        const delay = 3000 + img._tgTry * 2000;'
        '        setTimeout(() => { img.src = base + "?r=" + Date.now(); }, delay);'
        '      }'
        '    });'
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
async def page_music(limit: int = 200, q: Optional[str] = None):
    async with db_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM channels")
    if not login.session_exists() and not n:
        return RedirectResponse("/login")

    _music_my_dc = await _get_my_dc()
    async with db_pool.acquire() as conn:
        cached_n = await conn.fetchval(
            """SELECT count(*) FROM messages
               WHERE media_type='audio' AND local_path IS NOT NULL
                 AND local_path NOT LIKE '\\_\\_%' ESCAPE '\\'""")
        pending_n = await conn.fetchval(
            "SELECT count(*) FROM messages WHERE media_type='audio' AND local_path IS NULL")
        q_pat = f"%{q}%" if q else None
        rows = await conn.fetch(f"""
            SELECT m.id, m.audio_title, m.audio_performer, m.audio_duration_sec,
                   m.file_name, m.file_size, m.posted_at, m.file_dc,
                   m.audio_canonical_title, m.audio_album, m.audio_year,
                   m.audio_cover_url, m.local_path,
                   c.title AS ch_title, c.username AS ch_user
            FROM messages m
            JOIN channels c ON c.id = m.channel_id
            WHERE m.media_type = 'audio'
              AND ($1::text IS NULL OR
                   m.audio_title ILIKE $1 OR
                   m.audio_performer ILIKE $1 OR
                   m.file_name ILIKE $1)
            ORDER BY m.posted_at DESC NULLS LAST
            LIMIT {max(1, min(limit, 500))}
        """, q_pat)

    if not rows:
        body = (
            f'<h2 class="section">Music <span class="count">0 cached</span></h2>'
            '<div class="empty-state">'
            '<div class="icon">🎵</div>'
            '<div>No audio cached yet.</div>'
            f'<div style="margin-top:8px;font-size:13px">{pending_n:,} audio indexed — click to fetch (capped to recent 300 tracks).</div>'
            '</div>'
        )
        return HTMLResponse(_layout("Music", "/music", body))

    rows_html = "".join(
        f'<tr data-id="{r["id"]}" onclick="tgMusicPlay(this)">'
        f'<td class="play-cell"><div class="play-ico">▶</div></td>'
        f'<td class="title-cell">'
        f'<div class="t">{html.escape(r["audio_canonical_title"] or r["audio_title"] or r["file_name"] or "(untitled)")}</div>'
        f'<div class="a">{_dc_badge(r.get("file_dc"), _music_my_dc)} {html.escape((r["audio_performer"] or "") + ((" · " + (r["audio_album"] or r["ch_title"])) if (r["audio_album"] or r["ch_title"]) else ""))}{(" · " + str(r["audio_year"])) if r.get("audio_year") else ""}</div>'
        f'</td>'
        f'<td class="num">{_fmt_dur(r["audio_duration_sec"])}</td>'
        f'<td class="num">{_fmt_size(r["file_size"])}</td>'
        f'<td class="num">{_fmt_time(r["posted_at"])[:10]}</td>'
        f'</tr>'
        for r in rows
    )

    body = (
        f'<h2 class="section">Music <span class="count">{len(rows)} cached</span>'
        + (f' · <span style="color:var(--muted);font-size:13px">{pending_n:,} indexed (click to fetch)</span>'
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
async def page_library(limit: int = 200, q: Optional[str] = None):
    async with db_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM channels")
    if not login.session_exists() and not n:
        return RedirectResponse("/login")

    _lib_my_dc = await _get_my_dc()
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
                   m.local_path, m.file_dc,
                   c.title AS ch_title, c.username AS ch_user
            FROM messages m
            JOIN channels c ON c.id = m.channel_id
            WHERE m.media_type = 'document'
              AND m.file_name ~* '\\.(pdf|epub|mobi|azw3?|djvu|fb2|cbr|cbz|lit|txt)$'
              AND ($1::text IS NULL OR m.file_name ILIKE $1)
            ORDER BY m.posted_at DESC NULLS LAST
            LIMIT {max(1, min(limit, 500))}
        """, f"%{q}%" if q else None)

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
        f'<span class="pill accent">{_ext(r["file_name"])}</span>'
        f'<span class="pill muted">{_fmt_size(r["file_size"])}</span>'
        f'<span>· {html.escape(r["ch_title"] or "")}</span>'
        f'</div>'
        f'</div>'
        f'<div class="book-actions">'
        f'<a class="btn" href="/watch/{r["id"]}" target="_blank">Open</a>'
        f'<a class="btn ghost" href="/media/{r["id"]}" download="{html.escape(r["file_name"] or "")}">Download</a>'
        f'</div>'
        f'</div>'
        for r in rows
    )

    body = (
        f'<h2 class="section">Library <span class="count">{len(rows)} cached</span>'
        + (f' · <span style="color:var(--muted);font-size:13px">{pending_n:,} indexed (click to fetch)</span>'
           if pending_n else '')
        + '</h2>'
        f'<div class="book-grid">{items}</div>'
    )
    return HTMLResponse(_layout("Library", "/library", body))


# ════════════════════════════════════════════════════════════════════
# Page: Videos (all video messages — incl. those that didn't parse)
# ════════════════════════════════════════════════════════════════════
@app.get("/videos", response_class=HTMLResponse)
async def page_videos(q: Optional[str] = None, limit: int = 120,
                      offset: int = 0, aud: str = "sfw",
                      min_mb: int = 50):
    if not login.session_exists():
        async with db_pool.acquire() as conn:
            n = await conn.fetchval("SELECT count(*) FROM channels")
        if not n:
            return RedirectResponse("/login")

    where = ["m.media_type = 'video'",
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
            f"c.title AS ch_title, c.username AS ch_user, c.audience, "
            f"r.id AS release_id, r.canonical_title, r.quality "
            f"FROM messages m "
            f"JOIN channels c ON c.id = m.channel_id "
            f"LEFT JOIN releases r ON r.primary_msg_id = m.id "
            f"WHERE {where_sql} "
            f"ORDER BY m.posted_at DESC NULLS LAST "
            f"LIMIT {page_size} OFFSET {page_offset}",
            *params)

    def _qs(**overrides):
        defaults = {"q": q,
                    "aud": aud if aud != "sfw" else None,
                    "min_mb": min_mb if min_mb != 50 else None,
                    "limit": limit if limit != 120 else None}
        merged = {**defaults, **overrides}
        bits = []
        for k, v in merged.items():
            if v not in (None, "", 0, "sfw"):
                bits.append(f"{k}={html.escape(str(v))}")
        return "?" + "&".join(bits) if bits else ""

    def _aud_chip(val, label):
        active = aud == val or (val == "sfw" and not aud)
        href = f"/videos{_qs(aud=val if val != 'sfw' else None)}"
        bg = "rgba(245,158,11,0.18)" if val == "nsfw" and active else "rgba(94,182,229,0.15)"
        fg = "var(--warn)" if val == "nsfw" else "var(--accent-hi)"
        style = (f'background:{bg};color:{fg};border-color:{fg};') if active else ''
        return (f'<a class="btn ghost" href="{href}" '
                f'style="{style}padding:5px 12px;font-size:12px">{label}</a>')

    toolbar = (
        '<div style="display:flex;gap:10px;margin-bottom:22px;align-items:center;flex-wrap:wrap">'
        + _aud_chip("sfw", "SFW")
        + _aud_chip("nsfw", "NSFW")
        + _aud_chip("any", "all")
        + '</div>'
    )

    def _card(r):
        title = (r["canonical_title"] or r["file_name"]
                 or (r["caption"] or "")[:80] or f"video-{r['id']}")
        size = _fmt_size(r["file_size"]) if r["file_size"] else ""
        date = _fmt_time(r["posted_at"])[:10] if r["posted_at"] else ""
        ch = r["ch_title"] or r["ch_user"] or ""
        parsed_pill = ('<span class="pill ok" style="font-size:11px">parsed</span>'
                       if r["release_id"] else
                       '<span class="pill muted" style="font-size:11px">unparsed</span>')
        return (
            f'<div class="poster-card">'
            f'<div class="poster">'
            f'<img class="poster-img" src="/api/thumb/{r["id"]}" loading="lazy" alt="" '
            f'onerror="this.outerHTML=\'<div class=&quot;fallback&quot;>🎬</div>\'" />'
            f'</div>'
            f'<div class="info">'
            f'<div class="title">{html.escape(title)}</div>'
            f'<div class="sub">{html.escape(ch)} · {date}</div>'
            f'</div>'
            f'<div class="pills">'
            f'{_dc_badge(r.get("file_dc"), _vid_my_dc)}'
            f'{parsed_pill}'
            + (f'<span class="pill accent" style="font-size:11px">{html.escape(r["quality"])}</span>'
               if r.get("quality") else '')
            + f'<span class="pill muted" style="font-size:11px">{size}</span>'
            f'</div>'
            f'<div class="grab-row" style="margin-top:8px;display:flex;gap:6px">'
            f'<a class="btn" href="/watch/{r["id"]}" target="_blank">▶ watch</a>'
            f'<a class="btn ghost" href="/media/{r["id"]}" download>⬇</a>'
            f'</div>'
            f'</div>'
        )

    grid_html = ('<div class="poster-grid">'
                 + ''.join(_card(r) for r in rows)
                 + '</div>')
    start_idx = page_offset + 1 if rows else 0
    end_idx = page_offset + len(rows)
    filter_parts = [f"aud={aud}"]
    if min_mb > 0:
        filter_parts.append(f"≥{min_mb}MB")
    if q:
        filter_parts.append(f'q="{html.escape(q)}"')
    filter_desc = " · ".join(filter_parts)
    prev_offset = max(0, page_offset - page_size)
    next_offset = page_offset + page_size
    nav_links = []
    if page_offset > 0:
        nav_links.append(
            f'<a class="btn ghost" href="/videos{_qs(offset=prev_offset if prev_offset else None)}">← prev</a>')
    if next_offset < (total or 0):
        nav_links.append(
            f'<a class="btn ghost" href="/videos{_qs(offset=next_offset)}">next →</a>')
    pagination = (
        f'<div style="display:flex;justify-content:space-between;align-items:center;'
        f'margin-top:18px;padding:12px 0;border-top:1px solid var(--border, #e2e8f0)">'
        f'<span style="color:var(--muted, #64748b);font-size:13px">'
        f'showing {start_idx}–{end_idx} of {total:,} '
        f'<span style="opacity:0.7">({filter_desc})</span></span>'
        f'<div style="display:flex;gap:8px">{"".join(nav_links)}</div></div>')
    body = (
        f'<h2 class="section">Videos '
        f'<span class="count">{len(rows)} of {total:,}</span>'
        f'<span class="extra" style="color:var(--muted, #64748b);font-size:12px;'
        f'margin-left:10px">{filter_desc}</span></h2>'
        f'{toolbar}{grid_html}{pagination}')
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


def _release_card(r, my_dc=None) -> str:
    """Sonarr/Radarr-style poster card with 3-tier fallback +
    DC badge (so users can pick same-DC items without clicking)."""
    title_disp = r["canonical_title"] or r["name"].replace(".", " ")
    poster = r["poster_url"]
    pmid = r["primary_msg_id"]
    se = ""
    if r["season"] and r["episode"]:
        se = f"S{r['season']:02}E{r['episode']:02}"
    elif r["season"]:
        se = f"Season {r['season']}"
    posted = _fmt_time(r["posted_at"])[:10] if r["posted_at"] else ""
    sub_bits = [b for b in (se, posted) if b]
    cat_pill = ("accent" if r["category"] == "movie"
                else "ok" if r["category"] == "tv" else "muted")
    # Always render the emoji fallback in the DOM and let the <img> overlay
    # cover it. If the img 404s/410s the browser onerror hides the img and
    # the emoji shows through. CSS .poster .fallback is absolute-positioned;
    # .poster-img is z-index:2 stacked on top.
    if poster:
        img_src = html.escape(poster)
    elif pmid:
        img_src = f"/api/thumb/{pmid}"
    else:
        img_src = ""
    poster_style = ""
    if img_src:
        # onerror swaps the <img> for a fallback emoji div (no CSS :has() needed)
        poster_img = (f'<img class="poster-img" src="{img_src}" loading="lazy" alt="" '
                      f'onerror="this.outerHTML=\'&lt;div class=&quot;fallback&quot;&gt;\🎬&lt;/div&gt;\'" />')
        fallback = ""
    else:
        poster_img = ""
        fallback = '<div class="fallback">\U0001F3AC</div>'
    quality_badge = (f'<div class="badge">{html.escape(r["quality"])}</div>'
                     if r["quality"] else "")
    return (
        f'<div class="poster-card">'
        f'<div class="poster" {poster_style}>{fallback}{poster_img}{quality_badge}</div>'
        f'<div class="info">'
        f'<div class="title">{html.escape(title_disp)}</div>'
        f'<div class="sub">{" · ".join(html.escape(x) for x in sub_bits)}</div>'
        f'</div>'
        f'<div class="pills">'
        f'{_dc_badge(r.get("file_dc") if hasattr(r,"get") else r["file_dc"], my_dc)}'
        f'<span class="pill {cat_pill}">{r["category"]}</span>'
        f'<span class="pill muted">{_fmt_size(r["size_bytes"])}</span>'
        f'</div>'
        f'<div class="grab-row" style="display:flex;gap:6px">'
        f'<button class="btn play-btn" onclick="window.open(\'/watch/{r["primary_msg_id"]}\', \'_blank\')">▶ Play</button>'
        f'<button class="btn grab-btn" data-guid="{r["guid"]}" onclick="tgGrab(this)">⬇ Grab</button>'
        f'</div>'
        f'</div>'
    )


@app.get("/releases", response_class=HTMLResponse)
async def page_releases(q: Optional[str] = None, cat: Optional[str] = None,
                        view: str = "grid", limit: int = 120,
                        offset: int = 0,
                        min_mb: int = 100, aud: str = "sfw"):
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
    # CSAM is always hidden. SFW/NSFW chip filter — default sfw.
    where.append("COALESCE(c.audience, 'sfw') <> 'blocked_csam'")
    if aud == "sfw":
        where.append("COALESCE(c.audience, 'sfw') = 'sfw'")
    elif aud == "nsfw":
        where.append("c.audience = 'nsfw'")
    # aud == "any" or anything else → no audience filter beyond csam
    def _qual(w):
        # audience refs already use c. prefix — pass through
        if "c.audience" in w:
            return w
        return (w.replace("name ILIKE", "r.name ILIKE")
                 .replace("category =", "r.category =")
                 .replace("COALESCE(size_bytes", "COALESCE(r.size_bytes"))
    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)
    sql = (f"SELECT r.id, r.guid, r.name, r.category, r.season, r.episode, r.quality, "
          f"r.size_bytes, r.posted_at, r.parse_score, r.poster_url, r.canonical_title, "
          f"r.primary_msg_id, m.file_dc, c.audience "
          f"FROM releases r LEFT JOIN messages m ON m.id = r.primary_msg_id "
          f"LEFT JOIN channels c ON c.id = m.channel_id "
          f"WHERE {' AND '.join([_qual(w) for w in where])} "
          f"ORDER BY r.posted_at DESC NULLS LAST "
          f"LIMIT {page_size} OFFSET {page_offset}")
    count_sql = (f"SELECT count(*) FROM releases r "
                 f"LEFT JOIN messages m ON m.id = r.primary_msg_id "
                 f"LEFT JOIN channels c ON c.id = m.channel_id "
                 f"WHERE {' AND '.join([_qual(w) for w in where])}")
    _release_my_dc = await _get_my_dc()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        total_matched = await conn.fetchval(count_sql, *params)

    def _qs(**overrides) -> str:
        defaults = {"q": q, "cat": cat, "view": view,
                    "aud": aud if aud != "sfw" else None,
                    "min_mb": min_mb if min_mb != 100 else None,
                    "limit": limit if limit != 120 else None}
        merged = {**defaults, **overrides}
        bits = []
        for k, v in merged.items():
            if v not in (None, "", 0, "sfw"):
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

    grid_active = "active" if view != "list" else ""
    list_active = "active" if view == "list" else ""
    toolbar = (
        f'<div style="display:flex;gap:10px;margin-bottom:22px;align-items:center;flex-wrap:wrap">'
        f'{_chip("","all")}{_chip("movie","movies")}{_chip("tv","tv")}'
        f'<span style="color:var(--muted);font-size:12px;margin:0 4px">·</span>'
        f'{_aud_chip("sfw","SFW")}{_aud_chip("nsfw","NSFW")}{_aud_chip("any","all")}'
        f'<div style="margin-left:auto" class="view-toggle">'
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
        f'showing {start_idx}–{end_idx} of {total_matched:,} '
        f'<span style="opacity:0.7">({filter_desc})</span></span>'
        f'<div style="display:flex;gap:8px">{"".join(nav_links)}</div>'
        f'</div>')
    body = (f'<h2 class="section">Parsed releases '
           f'<span class="count">{len(rows)} of {total_matched:,}</span>'
           f'<span class="extra" style="color:var(--muted, #64748b);font-size:12px;'
           f'margin-left:10px">{filter_desc}</span></h2>'
           f'{toolbar}{body_html}{pagination_html}')
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
        sql = (f"SELECT r.id, r.guid, r.name, r.category, r.season, r.episode, r.quality, "
              f"r.size_bytes, r.posted_at, r.parse_score, r.poster_url, r.canonical_title, "
              f"r.primary_msg_id, m.file_dc "
              f"FROM releases r LEFT JOIN messages m ON m.id = r.primary_msg_id "
              f"WHERE {' AND '.join([w.replace('name ILIKE','r.name ILIKE').replace('category =','r.category =').replace('size_bytes','r.size_bytes') for w in where])} "
              f"ORDER BY r.posted_at DESC NULLS LAST LIMIT 120")
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
                f'{"".join(_release_card(r, _release_my_dc) for r in rows)}'
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
    _release_my_dc = await _get_my_dc()
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
