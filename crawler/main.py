"""tgarr crawler — Pyrogram MTProto channel/group listener + back-filler + download worker.

Responsibilities:
- On startup, back-fill historical messages from every joined channel/group/supergroup.
- Listen for new incoming messages continuously, indexing media metadata.
- Run filename parser on each new message → create release row if score ≥ threshold.
- Background download worker: poll `downloads` table, fetch media via MTProto,
  drop completed file into /downloads/tgarr/<release-name>/.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import uuid as uuidlib

import asyncpg
from pyrogram import Client
from pyrogram.handlers import RawUpdateHandler
from pyrogram.raw.types import UpdateChannel
from pyrogram.errors import (
    FloodWait, UsernameNotOccupied, UsernameInvalid,
    ChannelInvalid, ChannelPrivate,
)
from pyrogram.types import Message

from parser import parse_filename, to_release_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("tgarr")
logging.getLogger("pyrogram").setLevel(logging.WARNING)

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
DB_DSN = os.environ["DB_DSN"]
BACKFILL_LIMIT = int(os.environ.get("TG_BACKFILL_LIMIT", "5000"))
PARSE_SCORE_MIN = float(os.environ.get("TG_PARSE_SCORE_MIN", "0.30"))
DOWNLOAD_ROOT = os.environ.get("TG_DOWNLOAD_ROOT", "/downloads/tgarr")

app = Client(
    name="tgarr",
    api_id=API_ID,
    api_hash=API_HASH,
    workdir="/app/session",
)

db_pool: asyncpg.Pool = None

SAFE_NAME = re.compile(r"[^\w\-._]+")


async def init_db() -> None:
    global db_pool
    db_pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=4)
    log.info("postgres pool ready")


MIN_MOVIE_BYTES = 100 * 1024 * 1024   # 100 MB — anything smaller is sample/trailer/junk
MIN_TV_BYTES = 30 * 1024 * 1024       # 30 MB — short TV ep can be 50MB at 480p


async def maybe_create_release(conn, msg_row_id, file_name, file_size, posted_at):
    """Parse filename + insert release row if score is high enough."""
    parsed = parse_filename(file_name or "")
    score = parsed.get("score", 0.0)
    if score < PARSE_SCORE_MIN:
        return None
    rname = to_release_name(parsed, fallback=file_name or "")
    if not rname:
        return None
    type_ = parsed.get("type", "unknown")
    # Size sanity: filename-only parse calls trailers/samples "movie".
    # Real video files exceed these floors. Skip junk before it pollutes releases.
    if file_size:
        if type_ == "movie" and file_size < MIN_MOVIE_BYTES:
            return None
        if type_ == "tv" and file_size < MIN_TV_BYTES:
            return None
    await conn.execute(
        """INSERT INTO releases
             (name, category, series_title, season, episode,
              movie_title, movie_year, quality, source, codec,
              size_bytes, posted_at, primary_msg_id, parse_score)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
        rname,
        type_,
        parsed.get("title") if type_ == "tv" else None,
        parsed.get("season"),
        parsed.get("episode"),
        parsed.get("title") if type_ == "movie" else None,
        parsed.get("year") if type_ == "movie" else None,
        parsed.get("quality"),
        parsed.get("source"),
        parsed.get("codec"),
        file_size,
        posted_at,
        msg_row_id,
        score,
    )
    return rname


async def ingest_message(msg: Message) -> bool:
    """Persist one message + maybe create release. Returns True if new row."""
    chat_type = msg.chat.type.name
    if chat_type in ("PRIVATE", "BOT"):
        return False

    chat_id = msg.chat.id
    msg_id = msg.id

    file_name = None
    file_size = None
    mime_type = None
    file_unique_id = None
    media_type = None
    audio_title = None
    audio_performer = None
    audio_duration_sec = None

    # Identify the media kind explicitly so /gallery can find photos cleanly.
    if msg.video:
        media, media_type = msg.video, "video"
    elif msg.document:
        media, media_type = msg.document, "document"
    elif msg.audio:
        media, media_type = msg.audio, "audio"
        audio_title = getattr(msg.audio, "title", None)
        audio_performer = getattr(msg.audio, "performer", None)
        audio_duration_sec = getattr(msg.audio, "duration", None)
    elif msg.photo:
        media, media_type = msg.photo, "photo"
    else:
        media = None

    if media:
        file_name = getattr(media, "file_name", None)
        file_size = getattr(media, "file_size", None)
        mime_type = getattr(media, "mime_type", None)
        file_unique_id = getattr(media, "file_unique_id", None)

    caption = msg.caption or msg.text or ""

    async with db_pool.acquire() as conn:
        ch_id = await conn.fetchval(
            """INSERT INTO channels (tg_chat_id, username, title, category)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (tg_chat_id) DO UPDATE
                 SET username = EXCLUDED.username,
                     title = EXCLUDED.title
               RETURNING id""",
            chat_id,
            msg.chat.username,
            msg.chat.title or "",
            chat_type.lower(),
        )
        # NOISE FILTER: skip persisting messages with no media attachment.
        # text-only chatter has 0 resource value; discovery is handled via
        # federation registry, not via parsing message text for mentions.
        if media is None:
            return False
        new_msg_id = await conn.fetchval(
            """INSERT INTO messages
                 (channel_id, tg_message_id, tg_chat_id,
                  file_unique_id, file_name, caption, file_size, mime_type,
                  media_type, audio_title, audio_performer, audio_duration_sec,
                  posted_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (channel_id, tg_message_id) DO NOTHING
               RETURNING id""",
            ch_id, msg_id, chat_id, file_unique_id, file_name,
            caption[:8000], file_size, mime_type, media_type,
            audio_title, audio_performer, audio_duration_sec, msg.date,
        )

        if new_msg_id and file_name:
            try:
                await maybe_create_release(conn, new_msg_id, file_name, file_size, msg.date)
            except Exception as e:
                log.warning("[parse] failed msg_id=%s file=%s err=%s", new_msg_id, file_name, e)

        return bool(new_msg_id)


@app.on_message()
async def on_new_message(client: Client, msg: Message) -> None:
    inserted = await ingest_message(msg)
    if inserted and msg.chat.type.name != "PRIVATE":
        media = msg.video or msg.document or msg.audio or msg.photo
        file_info = (getattr(media, "file_name", "") or "") if media else ""
        caption = (msg.caption or msg.text or "")[:80]
        log.info("[live] ch=%s msg=%s file=%s text=%s",
                 msg.chat.id, msg.id, file_info, caption)


async def backfill_channel(chat_id: int, title: str) -> int:
    """Backfill recent history with sample-then-decide noise filter.

    After NOISE_SAMPLE_AT messages, if media density < NOISE_THRESHOLD_PCT,
    mark channel as noise + abort remaining backfill. ~96% TG quota savings
    on metaindex / chat channels.
    """
    NOISE_SAMPLE_AT = 200
    NOISE_THRESHOLD_PCT = 5
    log.info("[backfill] start chat_id=%s title=%s limit=%s",
             chat_id, title, BACKFILL_LIMIT)
    count = 0
    media_count = 0
    async for msg in app.get_chat_history(chat_id, limit=BACKFILL_LIMIT):
        has_media = bool(msg.video or msg.document or msg.audio or msg.photo)
        try:
            await ingest_message(msg)
            count += 1
            if has_media:
                media_count += 1
            if count % 200 == 0:
                log.info("[backfill] chat_id=%s progress=%s media=%s",
                         chat_id, count, media_count)
            if count == NOISE_SAMPLE_AT:
                pct = media_count * 100 // count
                if pct < NOISE_THRESHOLD_PCT:
                    log.info("[backfill] NOISE chat_id=%s title=%s "
                             "media=%d/%d (%d%% < %d%%) — disabling",
                             chat_id, title, media_count, count,
                             pct, NOISE_THRESHOLD_PCT)
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            """UPDATE channels SET enabled=FALSE,
                               category='noise', backfilled=TRUE
                               WHERE tg_chat_id=$1""", chat_id)
                    return count
        except Exception as e:
            log.warning("[backfill] err chat_id=%s msg_id=%s: %s",
                        chat_id, msg.id, e)

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE channels SET backfilled = TRUE WHERE tg_chat_id = $1", chat_id)
    log.info("[backfill] done chat_id=%s scanned=%s media_rows=%s",
             chat_id, count, media_count)
    return count


async def backfill_all() -> None:
    async with db_pool.acquire() as conn:
        backfilled = {
            r["tg_chat_id"]
            for r in await conn.fetch("SELECT tg_chat_id FROM channels WHERE backfilled")
        }

    current_dialog_ids: set[int] = set()
    async for dialog in app.get_dialogs():
        ctype = dialog.chat.type.name
        if ctype in ("PRIVATE", "BOT"):
            continue
        chat_id = dialog.chat.id
        current_dialog_ids.add(chat_id)
        title = dialog.chat.title or ""
        if chat_id in backfilled:
            log.info("[backfill] skip already-done chat_id=%s title=%s", chat_id, title)
            continue
        try:
            await backfill_channel(chat_id, title)
        except Exception as e:
            log.error("[backfill] failed chat_id=%s: %s", chat_id, e)

    # Reconcile dialog-sourced rows against user's current TG dialog list.
    # subscribed=TRUE rows are username-polled (don't need user membership),
    # so we leave them alone. Only dialog-sourced rows get disabled when the
    # user has left/archived them on their TG account. Noise-flagged channels
    # are never re-enabled even if still in dialog list.
    if current_dialog_ids:
        ids_list = list(current_dialog_ids)
        async with db_pool.acquire() as conn:
            stale = await conn.fetch(
                """UPDATE channels SET enabled = FALSE
                   WHERE subscribed = FALSE
                     AND enabled = TRUE
                     AND tg_chat_id <> ALL($1::bigint[])
                   RETURNING tg_chat_id, title""",
                ids_list)
            rejoined = await conn.fetch(
                """UPDATE channels SET enabled = TRUE
                   WHERE subscribed = FALSE
                     AND enabled = FALSE
                     AND COALESCE(category,'') <> 'noise'
                     AND tg_chat_id = ANY($1::bigint[])
                   RETURNING tg_chat_id, title""",
                ids_list)
        if stale:
            log.info("[reconcile] disabled %d stale dialog channels: %s",
                     len(stale), [(s["tg_chat_id"], (s["title"] or "")[:40]) for s in stale])
        if rejoined:
            log.info("[reconcile] re-enabled %d rejoined channels: %s",
                     len(rejoined), [(s["tg_chat_id"], (s["title"] or "")[:40]) for s in rejoined])


async def download_worker() -> None:
    """Background loop: pick up pending downloads + fetch media via MTProto."""
    log.info("[worker] download worker started; root=%s", DOWNLOAD_ROOT)
    os.makedirs(DOWNLOAD_ROOT, exist_ok=True)
    while True:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT d.id AS dl_id, d.release_id, r.name AS rel_name,
                              m.tg_chat_id, m.tg_message_id
                       FROM downloads d
                       JOIN releases r ON r.id = d.release_id
                       JOIN messages m ON m.id = r.primary_msg_id
                       WHERE d.status = 'pending'
                       ORDER BY d.requested_at
                       LIMIT 1""")
            if not row:
                await asyncio.sleep(5)
                continue

            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE downloads SET status='downloading' WHERE id=$1", row["dl_id"])

            safe_name = SAFE_NAME.sub("_", row["rel_name"])[:160]
            target_dir = os.path.join(DOWNLOAD_ROOT, safe_name)
            os.makedirs(target_dir, exist_ok=True)
            log.info("[worker] downloading id=%s release=%s → %s",
                     row["dl_id"], row["rel_name"], target_dir)

            try:
                msg = await app.get_messages(row["tg_chat_id"], row["tg_message_id"])
                if not msg or not (msg.video or msg.document or msg.audio):
                    raise RuntimeError("media missing on source message")
                local_path = await app.download_media(
                    msg, file_name=f"{target_dir}/")
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE downloads
                           SET status='completed', finished_at=NOW(), local_path=$1
                           WHERE id=$2""",
                        str(local_path), row["dl_id"])
                log.info("[worker] completed id=%s path=%s", row["dl_id"], local_path)
            except Exception as e:
                log.exception("[worker] failed id=%s: %s", row["dl_id"], e)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE downloads
                           SET status='failed', finished_at=NOW(), error_message=$1
                           WHERE id=$2""",
                        str(e)[:500], row["dl_id"])
        except Exception as e:
            log.exception("[worker] outer-loop error: %s", e)
            await asyncio.sleep(10)


SESSION_PATH = "/app/session/tgarr.session"
THUMBS_ROOT = "/downloads/thumbs"

REGISTRY_URL = os.environ.get("TGARR_REGISTRY_URL", "https://tgarr.me").rstrip("/")
CONTRIBUTE_ENABLED = os.environ.get("TGARR_CONTRIBUTE", "true").lower() == "true"
CONTRIBUTE_INTERVAL_SEC = int(os.environ.get("TGARR_CONTRIBUTE_INTERVAL_SEC", "21600"))  # 6h
INSTANCE_UUID_ROTATE_DAYS = 7

# Federation swarm validator (v0.4.10+): client pulls seed candidates from
# central, validates on this client's TG account, pushes back via /contribute.
# Each client validates a slice — quota scales linearly with # of clients.
# See reference_tgarr_federation_swarm_design.md.
FEDERATION_VALIDATOR_ENABLED = os.environ.get("TGARR_FEDERATION_VALIDATOR", "true").lower() == "true"
SEEDS_BATCH = int(os.environ.get("TGARR_SEEDS_BATCH", "20"))
SEEDS_INTERVAL_SEC = int(os.environ.get("TGARR_SEEDS_INTERVAL_SEC", "3600"))  # 1h
PER_SEED_DELAY_SEC = int(os.environ.get("TGARR_SEED_DELAY_SEC", "60"))  # 1 min/seed -> 1/min resolveUsername
AUDIO_ROOT = "/downloads/audio"
LIBRARY_ROOT = "/downloads/library"
VIDEO_ROOT = "/downloads/video"
MAX_AUDIO_BYTES = int(os.environ.get("TG_MAX_AUDIO_BYTES", str(150 * 1024 * 1024)))
MAX_BOOK_BYTES = int(os.environ.get("TG_MAX_BOOK_BYTES", str(80 * 1024 * 1024)))
MAX_AUDIO_COUNT = int(os.environ.get("TG_MAX_AUDIO_COUNT", "300"))
MAX_BOOK_COUNT = int(os.environ.get("TG_MAX_BOOK_COUNT", "300"))


def _session_authed() -> bool:
    """True only if the SQLite session row has a non-NULL user_id.

    The api container's qr_start() opens a Pyrogram connection which writes
    auth_key but leaves user_id NULL until login actually completes.
    Without this check, we would proceed to app.start() and Pyrogram would
    prompt for phone via stdin (EOFError in container).
    """
    import sqlite3
    if not (os.path.exists(SESSION_PATH) and os.path.getsize(SESSION_PATH) > 0):
        return False
    try:
        con = sqlite3.connect(f"file:{SESSION_PATH}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("SELECT user_id FROM sessions LIMIT 1").fetchone()
        finally:
            con.close()
        return bool(row and row[0])
    except Exception:
        return False


async def wait_for_session() -> None:
    """Block until the api container's login flow finishes signing in."""
    if _session_authed():
        return
    log.info("[startup] no Telegram session yet — open http://<host>:8765/login + scan QR")
    log.info("[startup] waiting for signed-in session at %s …", SESSION_PATH)
    while not _session_authed():
        await asyncio.sleep(5)
    await asyncio.sleep(2)  # let api flush
    log.info("[startup] session detected, continuing")


async def on_raw_update(client, update, users, chats) -> None:
    """Pyrogram RawUpdateHandler: catch UpdateChannel for instant
    new-channel detection. MTProto pushes this when the user joins a
    channel/supergroup, so we trigger backfill_channel within seconds
    instead of waiting for the 30s polling watcher.
    """
    if not isinstance(update, UpdateChannel):
        return
    raw_id = getattr(update, "channel_id", None)
    if not raw_id:
        return
    # Pyrogram peer-ID convention: -100<channel_id> for channels/supergroups
    chat_id = -1000000000000 - raw_id
    try:
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM channels WHERE tg_chat_id=$1", chat_id)
        if exists:
            return  # already known (could be a meta-update, not a join)
        chat_obj = chats.get(raw_id) if chats else None
        title = getattr(chat_obj, "title", "") or ""
        log.info("[live-join] new channel chat_id=%s title=%s", chat_id, title)
        asyncio.create_task(backfill_channel(chat_id, title))
    except Exception as e:
        log.exception("[live-join] error: %s", e)


async def new_dialog_watcher() -> None:
    """Detect newly-joined channels in near-real-time without crawler restart.

    Polls the user's top-N recent dialogs every NEW_DIALOG_INTERVAL seconds,
    compares against the channels table, and triggers backfill_channel for
    any new entries. New channel posts then start flowing via on_message
    immediately, while history backfills in the background.
    """
    NEW_DIALOG_INTERVAL = 30
    NEW_DIALOG_SCAN_LIMIT = 30
    log.info("[new-dialog] watcher started, interval=%ds limit=%d",
             NEW_DIALOG_INTERVAL, NEW_DIALOG_SCAN_LIMIT)
    await asyncio.sleep(45)  # give startup backfill_all a head start
    while True:
        try:
            async with db_pool.acquire() as conn:
                known = {r["tg_chat_id"] for r in await conn.fetch(
                    "SELECT tg_chat_id FROM channels")}
            new_dialogs = []
            async for dialog in app.get_dialogs(limit=NEW_DIALOG_SCAN_LIMIT):
                ctype = dialog.chat.type.name
                if ctype in ("PRIVATE", "BOT"):
                    continue
                if dialog.chat.id not in known:
                    new_dialogs.append(
                        (dialog.chat.id, dialog.chat.title or ""))
            for chat_id, title in new_dialogs:
                log.info("[new-dialog] discovered chat_id=%s title=%s",
                         chat_id, title)
                try:
                    await backfill_channel(chat_id, title)
                except Exception as e:
                    log.error("[new-dialog] backfill failed chat_id=%s: %s",
                              chat_id, e)
        except FloodWait as fw:
            wait = getattr(fw, "value", 60) + 5
            log.warning("[new-dialog] flood-wait %ds", wait)
            await asyncio.sleep(min(wait, 600))
            continue
        except Exception as e:
            log.exception("[new-dialog] outer: %s", e)
            await asyncio.sleep(120)
        await asyncio.sleep(NEW_DIALOG_INTERVAL)


async def thumb_downloader() -> None:
    """Background: fetch one photo at a time via MTProto + save to disk.

    Identifies eligible messages by media_type='photo' or — for legacy rows
    indexed before the column existed — by the (file_name NULL + mime_type NULL
    + file_unique_id NOT NULL) signature that Pyrogram photo objects produce.
    Marks failed lookups with thumb_path='__failed__' so they don't churn.
    """
    log.info("[thumbs] downloader started; root=%s", THUMBS_ROOT)
    os.makedirs(THUMBS_ROOT, exist_ok=True)
    while True:
        try:
            async with db_pool.acquire() as conn:
                # On-demand mode: only fetch when the UI flags
                # thumb_path='__user_queued__'. No proactive scan.
                row = await conn.fetchrow(
                    """SELECT m.id, m.tg_chat_id, m.tg_message_id, m.file_unique_id
                       FROM messages m
                       WHERE m.thumb_path = '__user_queued__'
                       ORDER BY m.id ASC
                       LIMIT 1""")
            if not row:
                await asyncio.sleep(60)
                continue

            safe_uid = SAFE_NAME.sub("_", row["file_unique_id"] or str(row["id"]))[:80]
            fname = f"{safe_uid}.jpg"
            target = os.path.join(THUMBS_ROOT, fname)
            try:
                msg = await app.get_messages(row["tg_chat_id"], row["tg_message_id"])
                if not msg:
                    raise RuntimeError("message gone")
                # Resolve the best available thumbnail source:
                #   photo message → full photo (largest PhotoSize)
                #   video message → embedded video thumbnail (last in thumbs list)
                #   document message → embedded document thumbnail
                thumb_media = None
                if msg.photo:
                    thumb_media = msg.photo
                elif msg.video and getattr(msg.video, "thumbs", None):
                    thumb_media = msg.video.thumbs[-1]
                elif msg.document and getattr(msg.document, "thumbs", None):
                    thumb_media = msg.document.thumbs[-1]
                if not thumb_media:
                    raise RuntimeError("no thumbnail available on message")
                await app.download_media(thumb_media, file_name=target)
                # MD5 of the downloaded bytes → dedup across channels
                with open(target, "rb") as f:
                    md5 = hashlib.md5(f.read()).hexdigest()
                async with db_pool.acquire() as conn:
                    existing = await conn.fetchval(
                        """SELECT thumb_path FROM messages
                           WHERE thumb_md5 = $1
                             AND thumb_path IS NOT NULL
                             AND thumb_path NOT LIKE '\\_\\_%' ESCAPE '\\'
                             AND id <> $2
                           LIMIT 1""", md5, row["id"])
                    if existing and existing != fname:
                        # Duplicate content — discard our copy, point at canonical
                        try:
                            os.remove(target)
                        except Exception:
                            pass
                        await conn.execute(
                            "UPDATE messages SET thumb_path=$1, thumb_md5=$2 WHERE id=$3",
                            existing, md5, row["id"])
                        log.info("[thumbs] dedup id=%s → %s", row["id"], existing)
                    else:
                        await conn.execute(
                            "UPDATE messages SET thumb_path=$1, thumb_md5=$2 WHERE id=$3",
                            fname, md5, row["id"])
            except FloodWait as fw:
                wait = getattr(fw, "value", 60) + 5
                log.warning("[thumbs] flood-wait %ds (id=%s) — pausing", wait, row["id"])
                await asyncio.sleep(min(wait, 3600))
                continue  # retry same row after cooldown; don't mark __failed__
            except Exception as e:
                log.warning("[thumbs] failed id=%s: %s", row["id"], e)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE messages SET thumb_path='__failed__' WHERE id=$1",
                        row["id"])
            await asyncio.sleep(0.8)
        except Exception as e:
            log.exception("[thumbs] outer loop: %s", e)
            await asyncio.sleep(10)


async def channel_meta_refresher() -> None:
    """Update members_count for each channel via get_chat. Once a day per channel.
    A friend group has ~5 members; a resource channel has 10K-100K+. This
    column drives the /channels filter chips so personal chats stay separated
    from public media channels.
    """
    log.info("[meta] channel meta refresher started")
    await asyncio.sleep(15)  # let connect settle first
    while True:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT tg_chat_id FROM channels
                       WHERE meta_updated_at IS NULL
                          OR meta_updated_at < NOW() - INTERVAL '7 days'
                       ORDER BY meta_updated_at NULLS FIRST
                       LIMIT 1""")
            if not row:
                await asyncio.sleep(900)
                continue
            try:
                chat = await app.get_chat(row["tg_chat_id"])
                members = getattr(chat, "members_count", None)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE channels SET members_count=$1, meta_updated_at=NOW()
                           WHERE tg_chat_id=$2""", members, row["tg_chat_id"])
                log.info("[meta] chat_id=%s members=%s", row["tg_chat_id"], members)
            except FloodWait as fw:
                wait = getattr(fw, "value", 60) + 5
                log.warning("[meta] flood-wait %ds", wait)
                await asyncio.sleep(min(wait, 600))
                continue
            except Exception as e:
                log.warning("[meta] failed chat_id=%s: %s", row["tg_chat_id"], e)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE channels SET meta_updated_at=NOW() WHERE tg_chat_id=$1",
                        row["tg_chat_id"])
            await asyncio.sleep(3)
        except Exception as e:
            log.exception("[meta] outer: %s", e)
            await asyncio.sleep(30)


async def thumb_hash_backfill() -> None:
    """One-off-ish: compute MD5 for thumbs saved before MD5 tracking landed.
    Walks rows with thumb_path set + thumb_md5 NULL, hashes file, dedupes."""
    log.info("[hash] backfill task started")
    while True:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT id, thumb_path FROM messages
                       WHERE thumb_path IS NOT NULL
                         AND thumb_path NOT LIKE '\\_\\_%' ESCAPE '\\'
                         AND thumb_md5 IS NULL
                       LIMIT 1""")
            if not row:
                await asyncio.sleep(120)
                continue
            path = os.path.join(THUMBS_ROOT, row["thumb_path"])
            if not os.path.exists(path):
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE messages SET thumb_path='__failed__' WHERE id=$1",
                        row["id"])
                continue
            with open(path, "rb") as f:
                md5 = hashlib.md5(f.read()).hexdigest()
            async with db_pool.acquire() as conn:
                existing = await conn.fetchval(
                    """SELECT thumb_path FROM messages
                       WHERE thumb_md5 = $1
                         AND thumb_path NOT LIKE '\\_\\_%' ESCAPE '\\'
                         AND id <> $2
                       ORDER BY id LIMIT 1""", md5, row["id"])
                if existing and existing != row["thumb_path"]:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    await conn.execute(
                        "UPDATE messages SET thumb_path=$1, thumb_md5=$2 WHERE id=$3",
                        existing, md5, row["id"])
                else:
                    await conn.execute(
                        "UPDATE messages SET thumb_md5=$1 WHERE id=$2",
                        md5, row["id"])
            await asyncio.sleep(0.05)
        except Exception as e:
            log.exception("[hash] error: %s", e)
            await asyncio.sleep(5)


async def local_media_downloader() -> None:
    """Background: cache recent audio + ebook documents to disk so /music and
    /library can serve them with Range support. Bounded by env limits."""
    log.info("[media-dl] audio=%s library=%s video=%s",
             AUDIO_ROOT, LIBRARY_ROOT, VIDEO_ROOT)
    os.makedirs(AUDIO_ROOT, exist_ok=True)
    os.makedirs(LIBRARY_ROOT, exist_ok=True)
    os.makedirs(VIDEO_ROOT, exist_ok=True)
    while True:
        try:
            async with db_pool.acquire() as conn:
                audio_n = await conn.fetchval(
                    """SELECT count(*) FROM messages
                       WHERE local_path IS NOT NULL
                         AND local_path NOT LIKE '\\_\\_%' ESCAPE '\\'
                         AND media_type='audio'""")
                book_n = await conn.fetchval(
                    """SELECT count(*) FROM messages
                       WHERE local_path IS NOT NULL
                         AND local_path NOT LIKE '\\_\\_%' ESCAPE '\\'
                         AND media_type='document'""")
                cond_parts = []
                if audio_n < MAX_AUDIO_COUNT:
                    cond_parts.append(
                        f"(m.media_type='audio' AND COALESCE(m.file_size,0) < {MAX_AUDIO_BYTES})")
                if book_n < MAX_BOOK_COUNT:
                    cond_parts.append(
                        f"(m.media_type='document' "
                        f"AND m.file_name ~* '\\.(pdf|epub|mobi|azw3?|djvu|fb2|cbr|cbz|lit|txt)$' "
                        f"AND COALESCE(m.file_size,0) < {MAX_BOOK_BYTES})")
                if not cond_parts:
                    await asyncio.sleep(180)
                    continue
                row = await conn.fetchrow(f"""
                    SELECT m.id, m.tg_chat_id, m.tg_message_id, m.media_type,
                           m.file_name, m.file_size
                    FROM messages m
                    WHERE m.local_path IS NULL
                      AND ({' OR '.join(cond_parts)})
                    ORDER BY m.posted_at DESC NULLS LAST
                    LIMIT 1
                """)
            if not row:
                # Short sleep when queue empty so user-clicks see worker wake
                # within ~5s rather than waiting up to 90s.
                await asyncio.sleep(5)
                continue

            mt = row["media_type"]
            root = (AUDIO_ROOT if mt == "audio"
                    else VIDEO_ROOT if mt == "video"
                    else LIBRARY_ROOT)
            base = (row["file_name"] or f"item-{row['id']}")
            safe = SAFE_NAME.sub("_", base)[:180]
            target = os.path.join(root, safe)
            try:
                msg = await app.get_messages(row["tg_chat_id"], row["tg_message_id"])
                if not msg:
                    raise RuntimeError("message gone")
                if mt == "audio":
                    media = msg.audio
                elif mt == "video":
                    media = msg.video
                else:
                    media = msg.document
                if not media:
                    raise RuntimeError("no media on message")
                # Stream the media in place at `target` so the API endpoint
                # can serve partial bytes as they\'re written (Pyrogram\'s
                # download_media uses a .temp file + rename, breaking partial
                # serving). Pre-UPDATE local_path before the stream begins.
                rel = os.path.relpath(target, "/downloads")
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE messages SET local_path=$1 WHERE id=$2",
                        rel, row["id"])
                log.info("[media-dl] %s id=%s streaming → %s",
                         row["media_type"], row["id"], rel)
                written = 0
                with open(target, "wb") as f:
                    async for chunk in app.stream_media(media):
                        f.write(chunk)
                        written += len(chunk)
                log.info("[media-dl] %s id=%s done → %s (%s bytes)",
                         row["media_type"], row["id"], rel, written)
            except FloodWait as fw:
                # Cross-DC auth.ExportAuthorization rate-limits us. Respect the
                # server-supplied wait verbatim; don't mark the row failed.
                wait = getattr(fw, "value", 60) + 5
                log.warning("[media-dl] flood-wait %ds (id=%s left unmarked, will retry)", wait, row["id"])
                await asyncio.sleep(min(wait, 1800))  # cap to 30 min, then loop
                continue
            except Exception as e:
                log.warning("[media-dl] failed id=%s: %s", row["id"], e)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE messages SET local_path='__failed__' WHERE id=$1",
                        row["id"])
            await asyncio.sleep(2)  # bigger files than thumbs — slower
        except Exception as e:
            log.exception("[media-dl] outer: %s", e)
            await asyncio.sleep(10)


SUBSCRIBE_POLL_INTERVAL = int(os.environ.get("TG_SUBSCRIBE_POLL_INTERVAL", "1800"))  # 30m
SUBSCRIBE_BACKFILL_LIMIT = int(os.environ.get("TG_SUBSCRIBE_BACKFILL_LIMIT", "1000"))

# Registry pull cadence — deliberately slow to keep tgarr.me load sane at scale.
# UUID-derived hour-of-day stagger means 100K instances spread evenly.
REGISTRY_PULL_INTERVAL_SEC = int(os.environ.get("TGARR_REGISTRY_PULL_INTERVAL", "43200"))  # 12h
REGISTRY_PULL_ENABLED = os.environ.get("TGARR_REGISTRY_PULL", "true").lower() == "true"


async def subscription_poller() -> None:
    """Poll public channels the user has subscribed to (without joining).

    Pyrogram's get_chat + get_chat_history both work on public channels by
    @username without a join, so we can index thousands of channels while
    keeping the user's TG account small (no mass-join ban risk).

    For each subscribed channel:
      1. Resolve @username → real tg_chat_id, update row (was placeholder)
      2. Backfill recent N messages via get_chat_history
      3. Re-poll every SUBSCRIBE_POLL_INTERVAL seconds (default 30m)

    on_message live updates don't fire for non-joined channels, so polling is
    the only way to catch new posts. 30m is a reasonable trade-off vs flood.
    """
    log.info("[subscribe] poller started, interval=%ds", SUBSCRIBE_POLL_INTERVAL)
    await asyncio.sleep(15)
    while True:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT id, tg_chat_id, username, title, backfilled
                       FROM channels
                       WHERE subscribed = TRUE
                         AND (last_polled_at IS NULL
                              OR last_polled_at < NOW() - $1 * INTERVAL '1 second')
                       ORDER BY last_polled_at NULLS FIRST
                       LIMIT 1""",
                    SUBSCRIBE_POLL_INTERVAL)
            if not row:
                await asyncio.sleep(60)
                continue

            uname = row["username"]
            log.info("[subscribe] poll @%s (backfilled=%s)", uname, row["backfilled"])
            try:
                chat = await app.get_chat(uname)
                real_chat_id = chat.id
                title = chat.title or uname
                members = getattr(chat, "members_count", None)
            except FloodWait as fw:
                log.warning("[subscribe] flood-wait %ds on @%s", fw.value, uname)
                await asyncio.sleep(min(fw.value + 2, 600))
                continue
            except Exception as e:
                log.warning("[subscribe] resolve fail @%s: %s", uname, e)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE channels SET subscribed=FALSE,
                           subscribe_error=$1, last_polled_at=NOW()
                           WHERE id=$2""", str(e)[:200], row["id"])
                continue

            # If we resolved a real chat_id different from placeholder, replace it
            if real_chat_id != row["tg_chat_id"]:
                async with db_pool.acquire() as conn:
                    existing = await conn.fetchval(
                        "SELECT id FROM channels WHERE tg_chat_id=$1", real_chat_id)
                    if existing and existing != row["id"]:
                        # Real chat already in table (joined or pre-subscribed),
                        # merge by deleting placeholder row
                        await conn.execute("DELETE FROM channels WHERE id=$1", row["id"])
                        await conn.execute(
                            """UPDATE channels SET subscribed=TRUE, username=$1,
                               last_polled_at=NOW() WHERE id=$2""",
                            uname, existing)
                        row = await conn.fetchrow(
                            "SELECT id, tg_chat_id, username, title, backfilled "
                            "FROM channels WHERE id=$1", existing)
                    else:
                        await conn.execute(
                            """UPDATE channels SET tg_chat_id=$1, title=$2,
                               members_count=$3, last_polled_at=NOW()
                               WHERE id=$4""",
                            real_chat_id, title, members, row["id"])
                        row = dict(row)
                        row["tg_chat_id"] = real_chat_id

            # Backfill on first poll, then incremental on later polls.
            limit = SUBSCRIBE_BACKFILL_LIMIT if not row["backfilled"] else 200
            count = media_count = 0
            try:
                async for msg in app.get_chat_history(real_chat_id, limit=limit):
                    try:
                        inserted = await ingest_message(msg)
                        count += 1
                        if inserted and (msg.video or msg.document or msg.audio):
                            media_count += 1
                    except Exception as e:
                        log.warning("[subscribe] ingest err: %s", e)
            except FloodWait as fw:
                log.warning("[subscribe] flood-wait %ds mid-backfill @%s",
                          fw.value, uname)
                await asyncio.sleep(min(fw.value + 2, 600))
                continue
            except Exception as e:
                log.warning("[subscribe] history err @%s: %s", uname, e)

            async with db_pool.acquire() as conn:
                await conn.execute(
                    """UPDATE channels SET backfilled=TRUE, last_polled_at=NOW(),
                       subscribe_error=NULL WHERE id=$1""", row["id"])
            log.info("[subscribe] @%s done: %d scanned, %d media",
                   uname, count, media_count)
            await asyncio.sleep(3)
        except Exception as e:
            log.exception("[subscribe] outer: %s", e)
            await asyncio.sleep(60)


SEED_VALIDATOR_ENABLED = os.environ.get("TGARR_SEED_VALIDATOR", "").lower() == "true"
SEED_VALIDATOR_INTERVAL = int(os.environ.get("TGARR_SEED_VALIDATOR_INTERVAL", "30"))  # 30s/candidate

# Same CSAM regex as registry server — defense in depth.
_CSAM_RX = re.compile(
    r"\b(loli|lolicon|shota|shotacon|child\s*porn|kid\s*porn|"
    r"pre[\s_-]*teen|under[\s_-]*age|\bcp\d+|\bcp_)\b", re.IGNORECASE)
_NSFW_RX = re.compile(
    r"(porn|xxx|nsfw|adult|18\+|hentai|erotic|nude|naked|onlyfan|"
    r"sexy|sex\b|色情|成人|18禁|裸|淫|эротик|порно|секс|اباحي|سكس)",
    re.IGNORECASE)


def _classify(title: str, username: str, hint: str | None) -> str:
    blob = (title or "") + " " + (username or "")
    if _CSAM_RX.search(blob):
        return "blocked_csam"
    if _NSFW_RX.search(blob) or hint == "nsfw":
        return "nsfw"
    return "sfw"


async def seed_validator() -> None:
    """Resolve seed_candidates one at a time via Pyrogram.

    Survey-mode pipeline: 6924 YAML candidates → registry_channels rows
    with seeded=true on success. Mark dead/banned/csam appropriately so we
    don't waste calls re-resolving them next pass.

    Only runs on the central tgarr.me instance — gated by TGARR_SEED_VALIDATOR=true
    in the .env. End-user instances never validate seeds; they only consume
    the registry the central operator validated.

    Conservative 30s/call pace to avoid Telegram FloodWait. 6924 × 30s ≈
    57 hours background work; designed to keep running across multiple days.
    """
    if not SEED_VALIDATOR_ENABLED:
        return
    log.info("[seed-validator] enabled, interval=%ds per candidate",
             SEED_VALIDATOR_INTERVAL)
    await asyncio.sleep(60)  # let backfill settle first

    while True:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT username, audience_hint, category, language, region
                       FROM seed_candidates
                       WHERE validation_status = 'pending' OR validation_status IS NULL
                       ORDER BY added_at
                       LIMIT 1""")
            if not row:
                log.info("[seed-validator] all candidates processed — sleeping 1h")
                await asyncio.sleep(3600)
                continue

            uname = row["username"]
            # Pre-check CSAM by name — never even resolve these.
            if _CSAM_RX.search(uname):
                log.warning("[seed-validator] CSAM-pattern skipped: @%s", uname)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE seed_candidates SET validation_status='csam',
                           validated_at=NOW() WHERE username=$1""", uname)
                    # Also pre-block in registry_channels
                    await conn.execute(
                        """INSERT INTO registry_channels
                             (username, audience, blocked, block_reason, seeded)
                           VALUES ($1, 'blocked_csam', TRUE, 'csam-keyword', TRUE)
                           ON CONFLICT (username) DO UPDATE SET
                             audience='blocked_csam', blocked=TRUE,
                             block_reason='csam-keyword'""", uname)
                continue

            try:
                chat = await app.get_chat(uname)
            except FloodWait as fw:
                log.warning("[seed-validator] FloodWait %ds on @%s — backing off",
                            fw.value, uname)
                await asyncio.sleep(min(fw.value + 10, 1800))
                continue
            except (UsernameNotOccupied, UsernameInvalid):
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE seed_candidates SET validation_status='dead',
                           validated_at=NOW() WHERE username=$1""", uname)
                continue
            except (ChannelInvalid, ChannelPrivate):
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE seed_candidates SET validation_status='forbidden',
                           validated_at=NOW() WHERE username=$1""", uname)
                continue
            except Exception as e:
                log.warning("[seed-validator] err @%s: %s", uname, e)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE seed_candidates SET validation_status='err',
                           validated_at=NOW() WHERE username=$1""", uname)
                continue

            title = chat.title or uname
            members = getattr(chat, "members_count", None)
            description = (getattr(chat, "description", None) or "")[:1000] or None
            audience = _classify(title, uname, row["audience_hint"])

            # Last message timestamp via 1-msg history pull
            last_msg = None
            try:
                async for m in app.get_chat_history(chat.id, limit=1):
                    last_msg = m.date
                    break
            except Exception:
                pass

            async with db_pool.acquire() as conn:
                if audience == "blocked_csam":
                    await conn.execute(
                        """INSERT INTO registry_channels
                             (username, title, audience, blocked, block_reason,
                              seeded, members_count, description, last_msg_at,
                              health_status, health_checked_at)
                           VALUES ($1,$2,'blocked_csam',TRUE,'csam-after-resolve',
                                   TRUE,$3,$4,$5,'banned',NOW())
                           ON CONFLICT (username) DO UPDATE SET
                             audience='blocked_csam', blocked=TRUE,
                             health_checked_at=NOW()""",
                        uname, title, members, description, last_msg)
                    status = "csam"
                else:
                    await conn.execute(
                        """INSERT INTO registry_channels
                             (username, title, members_count, audience, language,
                              category, description, last_msg_at, seeded,
                              health_status, health_checked_at)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,TRUE,'alive',NOW())
                           ON CONFLICT (username) DO UPDATE SET
                             title=EXCLUDED.title,
                             members_count=EXCLUDED.members_count,
                             audience=CASE WHEN registry_channels.audience='blocked_csam'
                                          THEN 'blocked_csam'
                                          ELSE EXCLUDED.audience END,
                             language=COALESCE(EXCLUDED.language, registry_channels.language),
                             category=COALESCE(EXCLUDED.category, registry_channels.category),
                             description=EXCLUDED.description,
                             last_msg_at=COALESCE(EXCLUDED.last_msg_at, registry_channels.last_msg_at),
                             seeded=TRUE,
                             health_status='alive',
                             health_checked_at=NOW()""",
                        uname, title, members, audience,
                        row["language"], row["category"], description, last_msg)
                    status = "alive"

                await conn.execute(
                    """UPDATE seed_candidates SET validation_status=$1,
                       validated_at=NOW() WHERE username=$2""", status, uname)

            log.info("[seed-validator] @%s → %s (%s members, %s)",
                     uname, status, members, audience)
            await asyncio.sleep(SEED_VALIDATOR_INTERVAL)
        except Exception as e:
            log.exception("[seed-validator] outer loop: %s", e)
            await asyncio.sleep(120)


async def registry_puller() -> None:
    """Pull curated channel list from registry.tgarr.me into local `discovered` table.

    Thundering-herd safety:
    - First-ever pull is deterministically offset by hash(instance_uuid) % 24h,
      so 100K instances spread evenly across the day instead of stampeding
      at boot.
    - Subsequent pulls are every 12h by default (rare, since the registry
      doesn't change fast). Each tier may tune via env.
    - Each pull sends `since=<last_pulled_at>` so the server returns only
      changes — most calls return small payloads.
    - HTTP Cache-Control on the server response means Cloudflare absorbs
      99% of legit traffic before it hits the origin.

    Discovered channels are NOT auto-subscribed. They land in the `discovered`
    table and the user picks which to actually subscribe to via /discover UI.
    """
    if not REGISTRY_PULL_ENABLED:
        log.info("[pull] TGARR_REGISTRY_PULL=false — disabled")
        return

    # Compute initial offset from instance UUID hash → even spread.
    async with db_pool.acquire() as conn:
        uuid_val = await conn.fetchval(
            "SELECT value FROM config WHERE key='instance_uuid'") or "bootstrap"
    initial_offset = (int(hashlib.sha256(uuid_val.encode()).hexdigest(), 16)
                      % REGISTRY_PULL_INTERVAL_SEC)
    log.info("[pull] registry puller — initial offset %ds (deterministic from UUID),"
             " then every %ds", initial_offset, REGISTRY_PULL_INTERVAL_SEC)
    # Wait initial offset but wake every 30s to honor /api/registry/pull-now
    # force-trigger (otherwise admin would need to wait up to 1h).
    initial_wait = min(initial_offset, 3600)
    waited = 0
    while waited < initial_wait:
        await asyncio.sleep(min(30, initial_wait - waited))
        waited += 30
        async with db_pool.acquire() as conn:
            if await conn.fetchval(
                "SELECT value FROM config WHERE key='registry_pull_force'"):
                log.info("[pull] force-trigger received during initial wait")
                break

    while True:
        try:
            # Check for manual force-pull trigger from /api/registry/pull-now
            async with db_pool.acquire() as conn:
                forced = await conn.fetchval(
                    "SELECT value FROM config WHERE key='registry_pull_force'")
                if forced:
                    # Clear so we don't loop on it
                    await conn.execute(
                        "DELETE FROM config WHERE key='registry_pull_force'")
                    log.info("[pull] force-trigger received, pulling now")
                last_pulled = await conn.fetchval(
                    "SELECT max(last_pulled_at) FROM discovered")
            params = {"audience": "sfw", "only_verified": "1", "limit": "5000"}
            if last_pulled:
                params["since"] = last_pulled.isoformat()
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url = REGISTRY_URL + "/api/v1/registry?" + qs

            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "tgarr/0.4.10 (+https://tgarr.me)",
                             "Accept": "application/json"},
                )
                resp = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(req, timeout=30).read())
                data = json.loads(resp.decode())
                channels = data.get("channels", [])
            except Exception as e:
                log.warning("[pull] registry GET failed: %s", e)
                channels = []

            inserted = updated = 0
            async with db_pool.acquire() as conn:
                for c in channels:
                    u = (c.get("username") or "").strip()
                    if not u:
                        continue
                    is_new = await conn.fetchval(
                        """INSERT INTO discovered
                             (username, title, members_count, media_count, audience,
                              language, category, distinct_contributors, verified)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                           ON CONFLICT (username) DO UPDATE SET
                             title = EXCLUDED.title,
                             members_count = EXCLUDED.members_count,
                             media_count = EXCLUDED.media_count,
                             audience = EXCLUDED.audience,
                             language = EXCLUDED.language,
                             category = EXCLUDED.category,
                             distinct_contributors = EXCLUDED.distinct_contributors,
                             verified = EXCLUDED.verified,
                             last_pulled_at = NOW()
                           RETURNING (xmax = 0)""",
                        u, c.get("title"), c.get("members_count"), c.get("media_count"),
                        c.get("audience"), c.get("language"), c.get("category"),
                        c.get("distinct_contributors", 0), c.get("verified", False))
                    if is_new:
                        inserted += 1
                    else:
                        updated += 1

            log.info("[pull] registry sync: %d new, %d updated (of %d returned)",
                     inserted, updated, len(channels))
            # Sleep but wake up every 60s to check for force-pull trigger
            for _ in range(REGISTRY_PULL_INTERVAL_SEC // 60):
                await asyncio.sleep(60)
                async with db_pool.acquire() as conn:
                    if await conn.fetchval(
                        "SELECT value FROM config WHERE key='registry_pull_force'"):
                        break
        except Exception as e:
            log.exception("[pull] outer: %s", e)
            await asyncio.sleep(600)


async def contribute_to_registry() -> None:
    """Federation: push our eligible channels to registry.tgarr.me periodically.

    Eligibility (same rule as /channels UI ✓ moat pill):
      members_count >= 500  AND  media_count >= 100  AND  audience IN (sfw, nsfw)
      AND audience <> blocked_csam

    Privacy:
    - Instance UUID is random hex, stored in `config` table, rotated weekly.
    - Server SHA-256-hashes it before storing; raw UUID never persisted on
      either side after each rotation.
    - No user identity, no message content — only public channel @usernames.

    Opt-out: TGARR_CONTRIBUTE=false on container.
    """
    if not CONTRIBUTE_ENABLED:
        log.info("[federation] TGARR_CONTRIBUTE=false — contribute task disabled")
        return
    log.info("[federation] contribute task started; endpoint=%s interval=%ds",
             REGISTRY_URL, CONTRIBUTE_INTERVAL_SEC)
    await asyncio.sleep(120)  # let backfill + metadata settle first
    while True:
        try:
            async with db_pool.acquire() as conn:
                # Rotate instance UUID weekly.
                row = await conn.fetchrow(
                    "SELECT value, updated_at FROM config WHERE key='instance_uuid'")
                rotate = True
                if row:
                    age = (await conn.fetchval(
                        "SELECT EXTRACT(EPOCH FROM (NOW() - $1))::int", row["updated_at"]))
                    rotate = age >= INSTANCE_UUID_ROTATE_DAYS * 86400
                if rotate:
                    uuid_val = uuidlib.uuid4().hex
                    await conn.execute(
                        """INSERT INTO config (key, value) VALUES ('instance_uuid', $1)
                           ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=NOW()""",
                        uuid_val)
                else:
                    uuid_val = row["value"]

                rows = await conn.fetch(
                    """SELECT c.username, c.title, c.members_count, c.audience,
                              (SELECT count(*) FROM messages m WHERE m.channel_id=c.id
                                 AND m.file_name IS NOT NULL) AS media_count
                       FROM channels c
                       WHERE c.username IS NOT NULL
                         AND c.members_count >= 500
                         AND COALESCE(c.audience, 'sfw') <> 'blocked_csam'
                         AND (SELECT count(*) FROM messages m WHERE m.channel_id=c.id
                              AND m.file_name IS NOT NULL) >= 100"""
                )

            if not rows:
                log.info("[federation] no eligible channels — sleeping %ds",
                         CONTRIBUTE_INTERVAL_SEC)
                await asyncio.sleep(CONTRIBUTE_INTERVAL_SEC)
                continue

            payload = {
                "instance_uuid": uuid_val,
                "tgarr_version": "0.4.10",
                "channels": [{
                    "username": r["username"],
                    "title": r["title"],
                    "members_count": r["members_count"],
                    "media_count": r["media_count"],
                    "audience": r["audience"] or "sfw",
                } for r in rows],
            }

            try:
                req = urllib.request.Request(
                    REGISTRY_URL + "/api/v1/contribute",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json",
                             "User-Agent": "tgarr/0.4.10 (+https://tgarr.me)"},
                    method="POST")
                resp = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(req, timeout=30).read())
                result = json.loads(resp.decode())
                log.info("[federation] contributed %s channels: %s",
                         len(rows), result)
            except Exception as e:
                log.warning("[federation] contribute call failed: %s", e)

            await asyncio.sleep(CONTRIBUTE_INTERVAL_SEC)
        except Exception as e:
            log.exception("[federation] outer loop: %s", e)
            await asyncio.sleep(600)



async def federation_validator() -> None:
    """Swarm-validator: pull seed candidates from central, validate locally,
    push back via /api/v1/contribute.

    This is how end-user instances participate in the federation. Each client
    validates a fresh random batch every interval using its own TG account
    quota. Central aggregates all clients' results via distinct_contributors
    consensus on registry_channels.

    Pattern per batch:
      1. GET REGISTRY_URL/api/v1/seeds?batch=N
      2. For each seed, app.get_chat(@username) or app.join_chat(invite_link)
      3. POST verified-alive channels to REGISTRY_URL/api/v1/contribute

    Rate-limited per-seed (default 60s) to keep resolveUsername quota safe.
    On FloodWait, abort current batch and wait full cooldown.

    Opt-out: TGARR_FEDERATION_VALIDATOR=false
    """
    if not FEDERATION_VALIDATOR_ENABLED:
        log.info("[fed-validator] TGARR_FEDERATION_VALIDATOR=false — disabled")
        return
    log.info("[fed-validator] enabled; batch=%d interval=%ds per_seed=%ds endpoint=%s",
             SEEDS_BATCH, SEEDS_INTERVAL_SEC, PER_SEED_DELAY_SEC, REGISTRY_URL)
    await asyncio.sleep(180)  # let backfill + metadata settle first

    while True:
        try:
            # ─── 1. Fetch a batch from central ──────────────────────────
            try:
                url = f"{REGISTRY_URL}/api/v1/seeds?batch={SEEDS_BATCH}"
                req = urllib.request.Request(url, headers={
                    "User-Agent": "tgarr/0.4.10 (+https://tgarr.me)"})
                resp = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(req, timeout=30).read())
                doc = json.loads(resp.decode())
                seeds = doc.get("seeds", []) or []
            except Exception as e:
                log.warning("[fed-validator] seed fetch failed: %s", e)
                await asyncio.sleep(SEEDS_INTERVAL_SEC)
                continue

            if not seeds:
                log.info("[fed-validator] no pending seeds; sleeping %ds",
                         SEEDS_INTERVAL_SEC)
                await asyncio.sleep(SEEDS_INTERVAL_SEC)
                continue

            log.info("[fed-validator] received %d seeds to validate", len(seeds))
            verified_alive = []
            flood_aborted = False

            # ─── 2. Validate each via TG ────────────────────────────────
            for seed in seeds:
                uname = seed.get("username") or ""
                invite = seed.get("invite_link")
                if not uname and not invite:
                    continue
                try:
                    if invite:
                        # join_chat → different rate-limit bucket
                        chat = await app.join_chat(invite)
                    else:
                        chat = await app.get_chat(uname)
                    members = getattr(chat, "members_count", None)
                    title = chat.title or (chat.username or uname)
                    real_username = chat.username or uname
                    verified_alive.append({
                        "username": real_username,
                        "title": title,
                        "members_count": members,
                        "audience": seed.get("audience_hint") or "sfw",
                        "language": seed.get("language"),
                        "category": seed.get("category"),
                    })
                    log.info("[fed-validator] alive @%s (%s members) %r",
                             real_username, members, title[:40] if title else "")
                except FloodWait as fw:
                    log.warning("[fed-validator] FloodWait %ds — aborting batch",
                                fw.value)
                    await asyncio.sleep(min(fw.value + 10, 1800))
                    flood_aborted = True
                    break
                except (UsernameNotOccupied, UsernameInvalid):
                    log.info("[fed-validator] dead @%s", uname)
                except (ChannelInvalid, ChannelPrivate):
                    log.info("[fed-validator] private/forbidden @%s", uname)
                except Exception as e:
                    log.warning("[fed-validator] err @%s: %s", uname, str(e)[:80])
                await asyncio.sleep(PER_SEED_DELAY_SEC)

            # ─── 3. Push verified-alive back to central ─────────────────
            if verified_alive:
                try:
                    async with db_pool.acquire() as conn:
                        row = await conn.fetchrow(
                            "SELECT value, updated_at FROM config WHERE key='instance_uuid'")
                        rotate = True
                        if row:
                            age = (await conn.fetchval(
                                "SELECT EXTRACT(EPOCH FROM (NOW() - $1))::int",
                                row["updated_at"]))
                            rotate = age >= INSTANCE_UUID_ROTATE_DAYS * 86400
                        if rotate:
                            uuid_val = uuidlib.uuid4().hex
                            await conn.execute(
                                """INSERT INTO config (key, value) VALUES ('instance_uuid', $1)
                                   ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=NOW()""",
                                uuid_val)
                        else:
                            uuid_val = row["value"]
                    payload = {
                        "instance_uuid": uuid_val,
                        "tgarr_version": "0.4.10",
                        "channels": verified_alive,
                    }
                    req = urllib.request.Request(
                        REGISTRY_URL + "/api/v1/contribute",
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json",
                                 "User-Agent": "tgarr/0.4.10 (+https://tgarr.me)"},
                        method="POST")
                    resp = await asyncio.to_thread(
                        lambda: urllib.request.urlopen(req, timeout=30).read())
                    result = json.loads(resp.decode())
                    log.info("[fed-validator] pushed %d alive: %s",
                             len(verified_alive), result)
                except Exception as e:
                    log.warning("[fed-validator] contribute-back failed: %s", e)

            # Honor flood abort by waiting full interval before next batch
            await asyncio.sleep(SEEDS_INTERVAL_SEC)
        except Exception as e:
            log.exception("[fed-validator] outer: %s", e)
            await asyncio.sleep(600)


async def main() -> None:
    await init_db()
    await wait_for_session()
    await app.start()
    app.add_handler(RawUpdateHandler(on_raw_update))
    me = await app.get_me()
    log.info("connected as @%s (id=%s)", me.username or "-", me.id)

    asyncio.create_task(download_worker())
    # On-demand mode 2026-05-14: thumb_downloader stays but only processes
    # rows where /api/thumb/{id} UI hit has set thumb_path='__user_queued__'.
    asyncio.create_task(thumb_downloader())
    # thumb_hash_backfill is proactive (scans all thumbs for md5 dedup), so it's
    # disabled to match the no-pre-download rule.
    # asyncio.create_task(thumb_hash_backfill())
    # local_media_downloader disabled 2026-05-14 — strict on-demand mode.
    # Binary content fetches only via download_worker (POST /api/grab/{guid}).
    # asyncio.create_task(local_media_downloader())
    asyncio.create_task(channel_meta_refresher())
    asyncio.create_task(new_dialog_watcher())
    asyncio.create_task(subscription_poller())
    asyncio.create_task(contribute_to_registry())
    asyncio.create_task(federation_validator())
    asyncio.create_task(registry_puller())
    asyncio.create_task(seed_validator())

    log.info("starting backfill (limit %s/channel)...", BACKFILL_LIMIT)
    await backfill_all()
    log.info("backfill complete, switching to live listen mode")
    await asyncio.Event().wait()


if __name__ == "__main__":
    app.run(main())
