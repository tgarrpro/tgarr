"""tgarr crawler — Pyrogram MTProto channel/group/supergroup listener + back-filler.

v0.1.0 MVP:
- On startup, back-fill all historical messages from every joined channel/group
  (skipping channels we've already back-filled in a prior run).
- Listen for new incoming messages continuously.
- Every message that has media metadata (file_name + size) is indexed.

Persistence: channels.backfilled flag in Postgres marks completion so restarts
do not re-fetch entire histories.
"""
import asyncio
import logging
import os

import asyncpg
from pyrogram import Client
from pyrogram.types import Message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("tgarr")
# quiet pyrogram noise — keep our [tgarr] logs prominent
logging.getLogger("pyrogram").setLevel(logging.WARNING)

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
DB_DSN = os.environ["DB_DSN"]
BACKFILL_LIMIT = int(os.environ.get("TG_BACKFILL_LIMIT", "5000"))  # per channel cap

app = Client(
    name="tgarr",
    api_id=API_ID,
    api_hash=API_HASH,
    workdir="/app/session",
)

db_pool: asyncpg.Pool = None


async def init_db() -> None:
    global db_pool
    db_pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=4)
    log.info("postgres pool ready")


async def ingest_message(msg: Message) -> bool:
    """Persist one message to Postgres. Returns True if it was a new row."""
    chat_type = msg.chat.type.name
    if chat_type in ("PRIVATE", "BOT"):
        return False

    chat_id = msg.chat.id
    msg_id = msg.id

    file_name = None
    file_size = None
    mime_type = None
    file_unique_id = None

    media = msg.video or msg.document or msg.audio or msg.photo
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
        result = await conn.execute(
            """INSERT INTO messages
                 (channel_id, tg_message_id, tg_chat_id,
                  file_unique_id, file_name, caption, file_size, mime_type, posted_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (channel_id, tg_message_id) DO NOTHING""",
            ch_id,
            msg_id,
            chat_id,
            file_unique_id,
            file_name,
            caption[:8000],
            file_size,
            mime_type,
            msg.date,
        )
        return "INSERT 0 1" in result


@app.on_message()
async def on_new_message(client: Client, msg: Message) -> None:
    """Live handler — every new incoming message from any chat we're in."""
    inserted = await ingest_message(msg)
    if inserted and msg.chat.type.name != "PRIVATE":
        media = msg.video or msg.document or msg.audio or msg.photo
        file_info = (getattr(media, "file_name", "") or "") if media else ""
        caption = (msg.caption or msg.text or "")[:80]
        log.info("[live] ch=%s msg=%s file=%s text=%s",
                 msg.chat.id, msg.id, file_info, caption)


async def backfill_channel(chat_id: int, title: str) -> int:
    """Fetch historical messages for a channel up to BACKFILL_LIMIT."""
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
            "UPDATE channels SET backfilled = TRUE WHERE tg_chat_id = $1",
            chat_id,
        )
    log.info("[backfill] done chat_id=%s scanned=%s media_rows=%s",
             chat_id, count, media_count)
    return count


async def backfill_all() -> None:
    """Walk every joined channel/group and back-fill if not already done."""
    async with db_pool.acquire() as conn:
        backfilled = {
            r["tg_chat_id"]
            for r in await conn.fetch("SELECT tg_chat_id FROM channels WHERE backfilled")
        }

    async for dialog in app.get_dialogs():
        ctype = dialog.chat.type.name
        if ctype in ("PRIVATE", "BOT"):
            log.info("[backfill] skip %s chat_id=%s title=%s",
                     ctype.lower(), dialog.chat.id, dialog.chat.title or "(dm)")
            continue
        chat_id = dialog.chat.id
        title = dialog.chat.title or ""
        if chat_id in backfilled:
            log.info("[backfill] skip already done chat_id=%s title=%s", chat_id, title)
            continue
        try:
            await backfill_channel(chat_id, title)
        except Exception as e:
            log.error("[backfill] failed chat_id=%s: %s", chat_id, e)


async def main() -> None:
    await init_db()
    await app.start()
    me = await app.get_me()
    log.info("connected as @%s (id=%s)", me.username or "-", me.id)
    log.info("starting backfill of all joined dialogs (limit %s each)...", BACKFILL_LIMIT)
    await backfill_all()
    log.info("backfill complete, switching to live listen mode")
    await asyncio.Event().wait()


if __name__ == "__main__":
    app.run(main())
