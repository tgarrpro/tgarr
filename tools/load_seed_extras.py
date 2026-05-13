"""Load YAML candidates from any /seed/*.yaml into seed_candidates table.

Discovery-friendly: walks /seed dir, ingests every YAML it finds.
Each file's `channels:` list (or top-level list) is ingested with the source
field taken from each entry's `source:` key, defaulting to filename stem.
Idempotent — ON CONFLICT DO NOTHING.
"""
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
import yaml

DB_DSN = os.environ["DB_DSN"]
SEED_DIR = Path(os.environ.get("SEED_DIR", "/seed"))


async def main():
    pool = await asyncpg.create_pool(DB_DSN)
    grand = 0
    async with pool.acquire() as conn:
        for path in sorted(SEED_DIR.glob("*.yaml")):
            if path.stat().st_size == 0:
                continue
            with path.open() as f:
                try:
                    doc = yaml.safe_load(f)
                except yaml.YAMLError as e:
                    print(f"  {path.name}: yaml err {e}", file=sys.stderr)
                    continue
            chans = doc.get("channels", []) if isinstance(doc, dict) else (doc or [])
            n_file = 0
            for c in chans:
                if not isinstance(c, dict):
                    continue
                u = (c.get("username") or "").strip().lstrip("@")
                if not u or len(u) < 5:
                    continue
                source = c.get("source") or path.stem
                new = await conn.fetchval(
                    """INSERT INTO seed_candidates
                         (username, title, category, region, language,
                          audience_hint, tags, source, validation_status)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending')
                       ON CONFLICT (username) DO NOTHING
                       RETURNING 1""",
                    u,
                    (c.get("display_name") or c.get("title") or "")[:300] or None,
                    c.get("category"),
                    c.get("region"),
                    c.get("language"),
                    c.get("audience") or "sfw",
                    c.get("tags") or [],
                    source)
                if new:
                    n_file += 1
            print(f"  {path.name:40s} +{n_file} new (of {len(chans)} in file)")
            grand += n_file

        total = await conn.fetchval("SELECT count(*) FROM seed_candidates")
        pending = await conn.fetchval(
            "SELECT count(*) FROM seed_candidates WHERE validation_status='pending'")
        print(f"\n+{grand} new candidates this run")
        print(f"total: {total}   pending validation: {pending}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
