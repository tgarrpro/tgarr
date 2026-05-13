"""Ingest YAML channel candidates into registry_candidates table.

Reads:
  /tmp/registry-builtin.yaml   — Claude-recalled (private moat data)
  /tmp/tgstat-imported.yaml    — tgstat.com scrape
  channels/registry.yaml        — hand-curated initial seed

Inserts into seed_candidates table on registry's Postgres. ON CONFLICT
keeps the existing row's validation_status — we don't reset progress just
because we re-run the loader.

Run from registry container or any container that can reach the db:
  docker compose exec registry python /app/tools/load_seed_candidates.py

Or from host with psql access. The loader is idempotent.
"""
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
import yaml

DB_DSN = os.environ.get("DB_DSN", "postgresql://tgarr:tgarr@db:5432/tgarr")

SEED_FILES = [
    ("/tmp/registry-builtin.yaml", "claude-knowledge"),
    ("/tmp/tgstat-imported.yaml", "tgstat-scrape"),
    ("/seed/registry-builtin.yaml", "claude-knowledge"),
    ("/seed/tgstat-imported.yaml", "tgstat-scrape"),
    ("/seed/registry.yaml", "curated"),
]


def _normalize_one(d: dict, default_source: str) -> dict | None:
    u = (d.get("username") or "").strip().lstrip("@")
    if not u:
        return None
    return {
        "username": u,
        "title": (d.get("display_name") or d.get("title") or "")[:300] or None,
        "category": d.get("category"),
        "region": d.get("region"),
        "language": d.get("language"),
        "audience_hint": d.get("audience") or "sfw",
        "tags": d.get("tags") or [],
        "source": d.get("source") or default_source,
    }


async def load_file(conn, path: Path, default_source: str) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    with path.open() as f:
        try:
            doc = yaml.safe_load(f)
        except yaml.YAMLError as e:
            print(f"[skip] {path}: YAML parse error: {e}", file=sys.stderr)
            return 0
    channels = []
    if isinstance(doc, dict):
        channels = doc.get("channels") or []
    elif isinstance(doc, list):
        channels = doc
    inserted = 0
    for c in channels:
        if not isinstance(c, dict):
            continue
        n = _normalize_one(c, default_source)
        if not n:
            continue
        try:
            new = await conn.fetchval(
                """INSERT INTO seed_candidates
                     (username, title, category, region, language, audience_hint,
                      tags, source, validation_status)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending')
                   ON CONFLICT (username) DO UPDATE SET
                     title = COALESCE(EXCLUDED.title, seed_candidates.title)
                   RETURNING (xmax = 0)""",
                n["username"], n["title"], n["category"], n["region"],
                n["language"], n["audience_hint"], n["tags"], n["source"])
            if new:
                inserted += 1
        except Exception as e:
            print(f"[err] {n['username']}: {e}", file=sys.stderr)
    return inserted


async def main():
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    grand = 0
    async with pool.acquire() as conn:
        for path_str, src in SEED_FILES:
            p = Path(path_str)
            n = await load_file(conn, p, src)
            print(f"  {path_str:50s} → {n} new ({src})")
            grand += n
        total = await conn.fetchval("SELECT count(*) FROM seed_candidates")
        pending = await conn.fetchval(
            "SELECT count(*) FROM seed_candidates WHERE validation_status='pending'")
        print(f"\ntotal seed_candidates: {total}  ({pending} pending validation)")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
