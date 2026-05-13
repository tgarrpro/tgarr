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
import logging
import os
import re

import asyncpg
from pyrogram import Client
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

    # Identify the media kind explicitly so /gallery can find photos cleanly.
    if msg.video:
        media, media_type = msg.video, "video"
    elif msg.document:
        media, media_type = msg.document, "document"
    elif msg.audio:
        media, media_type = msg.audio, "audio"
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
        new_msg_id = await conn.fetchval(
            """INSERT INTO messages
                 (channel_id, tg_message_id, tg_chat_id,
                  file_unique_id, file_name, caption, file_size, mime_type,
                  media_type, posted_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
               ON CONFLICT (channel_id, tg_message_id) DO NOTHING
               RETURNING id""",
            ch_id, msg_id, chat_id, file_unique_id, file_name,
            caption[:8000], file_size, mime_type, media_type, msg.date,
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
    log.info("[backfill] start chat_id=%s title=%s limit=%s",
             chat_id, title, BACKFILL_LIMIT)
    count = 0
    media_count = 0
    async for msg in app.get_chat_history(chat_id, limit=BACKFILL_LIMIT):
        try:
            inserted = await ingest_message(msg)
            count += 1
            if inserted and (msg.video or msg.document or msg.audio):
                media_count += 1
            if count % 200 == 0:
                log.info("[backfill] chat_id=%s progress=%s", chat_id, count)
        except Exception as e:
            log.warning("[backfill] err chat_id=%s msg_id=%s: %s", chat_id, msg.id, e)

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

    async for dialog in app.get_dialogs():
        ctype = dialog.chat.type.name
        if ctype in ("PRIVATE", "BOT"):
            continue
        chat_id = dialog.chat.id
        title = dialog.chat.title or ""
        if chat_id in backfilled:
            log.info("[backfill] skip already-done chat_id=%s title=%s", chat_id, title)
            continue
        try:
            await backfill_channel(chat_id, title)
        except Exception as e:
            log.error("[backfill] failed chat_id=%s: %s", chat_id, e)


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
                row = await conn.fetchrow(
                    """SELECT m.id, m.tg_chat_id, m.tg_message_id, m.file_unique_id
                       FROM messages m
                       WHERE m.thumb_path IS NULL
                         AND (m.media_type = 'photo'
                              OR (m.media_type IS NULL
                                  AND m.file_name IS NULL
                                  AND COALESCE(m.mime_type,'') = ''
                                  AND m.file_unique_id IS NOT NULL))
                       ORDER BY m.posted_at DESC NULLS LAST
                       LIMIT 1""")
            if not row:
                await asyncio.sleep(60)
                continue

            safe_uid = SAFE_NAME.sub("_", row["file_unique_id"] or str(row["id"]))[:80]
            fname = f"{safe_uid}.jpg"
            target = os.path.join(THUMBS_ROOT, fname)
            try:
                msg = await app.get_messages(row["tg_chat_id"], row["tg_message_id"])
                if not msg or not msg.photo:
                    raise RuntimeError("no photo on message")
                await app.download_media(msg.photo, file_name=target)
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


async def main() -> None:
    await init_db()
    await wait_for_session()
    await app.start()
    me = await app.get_me()
    log.info("connected as @%s (id=%s)", me.username or "-", me.id)

    asyncio.create_task(download_worker())
    asyncio.create_task(thumb_downloader())
    asyncio.create_task(thumb_hash_backfill())

    log.info("starting backfill (limit %s/channel)...", BACKFILL_LIMIT)
    await backfill_all()
    log.info("backfill complete, switching to live listen mode")
    await asyncio.Event().wait()


if __name__ == "__main__":
    app.run(main())
