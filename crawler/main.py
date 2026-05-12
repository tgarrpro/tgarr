"""tgarr crawler — Pyrogram MTProto channel listener.

v0.1.0 MVP: connect to Telegram as user account, log all messages from joined
channels into Postgres. Foundation for the Newznab-compatible indexer that
Sonarr/Radarr will query.
"""
import asyncio
import logging
import os

import asyncpg
from pyrogram import Client, filters
from pyrogram.types import Message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("tgarr")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
DB_DSN = os.environ["DB_DSN"]

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


@app.on_message(filters.channel)
async def on_channel_message(client: Client, msg: Message) -> None:
    """Capture every message from joined channels into the index."""
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
            """INSERT INTO channels (tg_chat_id, username, title)
               VALUES ($1, $2, $3)
               ON CONFLICT (tg_chat_id) DO UPDATE
                 SET username = EXCLUDED.username,
                     title = EXCLUDED.title
               RETURNING id""",
            chat_id,
            msg.chat.username,
            msg.chat.title or "",
        )
        await conn.execute(
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

    log.info(
        "[ch=%s] msg=%s file=%s caption=%s",
        chat_id,
        msg_id,
        file_name,
        caption[:80],
    )


async def list_joined_channels() -> None:
    """Log every channel/supergroup the user account is subscribed to."""
    me = await app.get_me()
    log.info("connected as @%s (id=%s)", me.username or "-", me.id)
    count = 0
    async for dialog in app.get_dialogs():
        if dialog.chat.type.name in ("CHANNEL", "SUPERGROUP"):
            log.info(
                "  channel: %s | %s | id=%s",
                dialog.chat.username or "-",
                dialog.chat.title,
                dialog.chat.id,
            )
            count += 1
    log.info("total %d channels/supergroups subscribed", count)


async def main() -> None:
    await init_db()
    async with app:
        await list_joined_channels()
    log.info("crawler online, listening for new messages...")
    await app.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    app.run(main())
