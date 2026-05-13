"""Backfill releases from already-indexed messages.

Run once after deploying the parser to populate the releases table from
messages indexed by an earlier (parser-less) crawler version.

    docker compose exec crawler python /app/tools/backfill_parse.py
"""
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, "/app")
from parser import parse_filename, to_release_name

PARSE_SCORE_MIN = float(os.environ.get("TG_PARSE_SCORE_MIN", "0.30"))


async def backfill():
    pool = await asyncpg.create_pool(os.environ["DB_DSN"])
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT m.id AS msg_id, m.file_name, m.file_size, m.posted_at
               FROM messages m
               LEFT JOIN releases r ON r.primary_msg_id = m.id
               WHERE m.file_name IS NOT NULL
                 AND r.id IS NULL
               ORDER BY m.posted_at DESC""")
    print(f"unparsed messages with filename: {len(rows)}")

    parsed = skipped_low = errors = 0
    for r in rows:
        p = parse_filename(r["file_name"])
        if p.get("score", 0) < PARSE_SCORE_MIN:
            skipped_low += 1
            continue
        name = to_release_name(p, fallback=r["file_name"])
        type_ = p.get("type", "unknown")
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO releases
                         (name, category, series_title, season, episode,
                          movie_title, movie_year, quality, source, codec,
                          size_bytes, posted_at, primary_msg_id, parse_score)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
                    name,
                    type_,
                    p.get("title") if type_ == "tv" else None,
                    p.get("season"),
                    p.get("episode"),
                    p.get("title") if type_ == "movie" else None,
                    p.get("year") if type_ == "movie" else None,
                    p.get("quality"),
                    p.get("source"),
                    p.get("codec"),
                    r["file_size"],
                    r["posted_at"],
                    r["msg_id"],
                    p.get("score", 0),
                )
            parsed += 1
        except Exception as e:
            errors += 1
            if errors < 5:
                print(f"  err msg_id={r['msg_id']} file={r['file_name']!r}: {e}")

    print(f"parsed: {parsed}, skipped (low score): {skipped_low}, errors: {errors}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(backfill())
