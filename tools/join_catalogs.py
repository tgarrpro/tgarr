"""Join all alive 'tg-catalog-candidate' channels.

Reads seed_candidates joined with registry_channels to find catalog channels
that the seed_validator has confirmed alive, then JOINs each via Pyrogram.
After this runs, the crawler's existing on_message + backfill_all will
naturally accumulate the catalog's posts into the messages table; then
tools/extract_catalog_mentions.py harvests @mentions back into seed_candidates.

⚠ Must be run with the crawler container STOPPED — Pyrogram session file
is shared and two writers will corrupt it. Workflow:

    docker compose stop crawler
    docker compose run --rm crawler python /app/tools/join_catalogs.py
    docker compose up -d crawler

Aborts early on FloodWait (no point burning the cooldown). Re-run after
cooldown clears.

Idempotent on already-joined channels (PEER_ID_INVALID / UserAlreadyParticipant
silently skipped).
"""
import os
import sys
import time

import asyncpg
import asyncio
from pyrogram import Client
from pyrogram.errors import FloodWait, UserAlreadyParticipant, ChannelInvalid, ChannelPrivate, UsernameNotOccupied, UsernameInvalid

API_ID = int(os.environ['TG_API_ID'])
API_HASH = os.environ['TG_API_HASH']
DB_DSN = os.environ['DB_DSN']
DELAY = int(os.environ.get('JOIN_DELAY', '45'))

CATALOG_SOURCE = 'tg-catalog-candidate'


async def get_targets() -> list[str]:
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT sc.username
            FROM seed_candidates sc
            LEFT JOIN registry_channels rc ON LOWER(rc.username) = LOWER(sc.username)
            LEFT JOIN channels c ON LOWER(c.username) = LOWER(sc.username)
            WHERE sc.source = $1
              AND sc.validation_status = 'alive'
              AND c.id IS NULL  -- skip already-joined
            ORDER BY rc.members_count DESC NULLS LAST
        ''', CATALOG_SOURCE)
    await pool.close()
    return [r['username'] for r in rows]


def main():
    targets = asyncio.run(get_targets())
    if not targets:
        print('no alive un-joined catalog channels — wait for validator to process them')
        sys.exit(0)
    print(f'targets: {len(targets)}')
    results = {'joined': 0, 'skip': 0, 'fail': 0, 'floodwait': 0}

    with Client('tgarr', api_id=API_ID, api_hash=API_HASH, workdir='/app/session') as app:
        for i, uname in enumerate(targets, 1):
            try:
                chat = app.join_chat(uname)
                print(f'[{i}/{len(targets)}] ✓ joined @{uname}  id={chat.id} title={chat.title!r}')
                results['joined'] += 1
            except UserAlreadyParticipant:
                print(f'[{i}/{len(targets)}] ⊙ already in @{uname}')
                results['skip'] += 1
            except FloodWait as fw:
                print(f'[{i}/{len(targets)}] ⏸ FloodWait {fw.value}s on @{uname} — aborting run (re-run after cooldown)')
                results['floodwait'] += 1
                break
            except (UsernameNotOccupied, UsernameInvalid, ChannelInvalid, ChannelPrivate) as e:
                print(f'[{i}/{len(targets)}] ✗ {type(e).__name__} on @{uname}')
                results['fail'] += 1
            except Exception as e:
                print(f'[{i}/{len(targets)}] ✗ {type(e).__name__}: {str(e)[:80]}')
                results['fail'] += 1
            if i < len(targets):
                time.sleep(DELAY)

    print()
    print('=== summary ===')
    for k, v in results.items():
        print(f'  {k}: {v}')


if __name__ == '__main__':
    main()
