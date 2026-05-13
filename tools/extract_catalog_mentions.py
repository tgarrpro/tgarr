"""Catalog message harvester — 3 layers.

Walks messages from joined catalog channels and extracts:
  (1) @<username> mentions    -> seed_candidates (source='tg-catalog-mention')
  (2) invite links            -> seed_candidates (invite_link populated)
  (3) IPTV endpoints / creds  -> iptv_sources

Tom's insight (2026-05-13): random-named insider channels and invite-only
channels are the real gold; public-search-friendly @usernames mostly funnel
to honeypots. Invite links use a different TG rate-limit bucket from
resolveUsername. IPTV stream URLs are the actual vendor backbone.

Idempotent — ON CONFLICT DO NOTHING on seed_candidates, UNIQUE on iptv_sources.

Run: docker compose exec crawler python /app/tools/extract_catalog_mentions.py
"""
import asyncio
import os
import re
import asyncpg

DB_DSN = os.environ['DB_DSN']

CATALOG_SOURCE = 'tg-catalog-candidate'

# Layer 1: bare @username mentions
MENTION_RX = re.compile(
    r'(?<![A-Za-z0-9_])'
    r'@([A-Za-z][A-Za-z0-9_]{4,31})\b'
)

# Layer 2: invite links — t.me/+xxxxx or t.me/joinchat/xxxxx
INVITE_RX = re.compile(
    r'(?:https?://)?t(?:elegram)?\.(?:me|dog)/(?:\+|joinchat/)([A-Za-z0-9_-]{16,})',
    re.IGNORECASE,
)

# Layer 3 — IPTV patterns
IPTV_PATTERNS = [
    # m3u/m3u8 playlist URL
    ('m3u_url', re.compile(r'https?://[A-Za-z0-9.\-]+(?::\d+)?/[^\s"<>]*\.m3u8?\b[^\s"<>]*', re.IGNORECASE)),
    # xtream-codes get.php with creds in query
    ('xtream',  re.compile(r'https?://[A-Za-z0-9.\-]+(?::\d+)?/(?:get\.php|player_api\.php)\?[^\s"<>]*username=[^\s&"<>]+[^\s"<>]*', re.IGNORECASE)),
    # stalker portal
    ('stalker', re.compile(r'https?://[A-Za-z0-9.\-]+(?::\d+)?/(?:stalker_portal|portal\.php|c/)[^\s"<>]*', re.IGNORECASE)),
    # ServerYserver:port credentials blob — common 'Host:x Port:y User:z Pass:w' patterns
    ('cred_blob', re.compile(
        r'(?:host|server|url)\s*[:=]\s*([A-Za-z0-9.\-]+(?::\d+)?)\s*[,;\n].{0,80}?'
        r'(?:user(?:name)?)\s*[:=]\s*(\S+)\s*[,;\n].{0,80}?'
        r'(?:pass(?:word)?)\s*[:=]\s*(\S+)',
        re.IGNORECASE | re.DOTALL)),
]

# Provider name hints — guess from context
PROVIDER_HINTS = re.compile(
    r'\b(aurora|king|eternal|express|crystal|premium|elite|gold|platinum|'
    r'magnum|tivimate|smarters|iptv\s?smarters|gse|tivone|tvnow|streamy)\b',
    re.IGNORECASE,
)


async def main():
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT c.id, c.username
            FROM channels c
            WHERE LOWER(c.username) IN (
                SELECT LOWER(sc.username) FROM seed_candidates sc
                WHERE sc.source = $1 AND sc.validation_status = 'alive'
            )
        ''', CATALOG_SOURCE)

        if not rows:
            print('no validated catalog channels yet — nothing to extract')
            await pool.close()
            return

        ch_ids = [r['id'] for r in rows]
        print(f'catalogs joined: {len(ch_ids)} -> {[r["username"] for r in rows]}')

        msgs = await conn.fetch('''
            SELECT m.id, m.caption, c.username AS chan
            FROM messages m JOIN channels c ON c.id=m.channel_id
            WHERE m.channel_id = ANY($1::bigint[])
              AND m.caption IS NOT NULL AND LENGTH(m.caption) > 0
        ''', ch_ids)
        print(f'scanning {len(msgs)} catalog messages…')

        mentions = set()       # username -> count
        invites = {}           # invite_hash -> full url
        iptv_rows = []

        for m in msgs:
            cap = m['caption']
            chan = m['chan']
            # Layer 1
            for hit in MENTION_RX.finditer(cap):
                mentions.add(hit.group(1).lower())
            # Layer 2
            for hit in INVITE_RX.finditer(cap):
                h = hit.group(1)
                full = f'https://t.me/+{h}' if not hit.group(0).lower().startswith(('http', 't')) else hit.group(0)
                invites[h] = full
            # Layer 3
            for kind, rx in IPTV_PATTERNS:
                for hit in rx.finditer(cap):
                    raw = hit.group(0)
                    prov = PROVIDER_HINTS.search(cap)
                    iptv_rows.append((kind, raw[:500], prov.group(1).lower() if prov else None, m['id'], chan))

        print(f'extracted: {len(mentions)} mentions / {len(invites)} invite links / {len(iptv_rows)} iptv hits')

        # INSERT layer 1+2 into seed_candidates
        if mentions:
            await conn.executemany('''
                INSERT INTO seed_candidates (username, source, audience_hint, validation_status)
                VALUES ($1, 'tg-catalog-mention', 'sfw', 'pending')
                ON CONFLICT (username) DO NOTHING
            ''', [(u,) for u in mentions])
            print(f'inserted up to {len(mentions)} new mention candidates')

        if invites:
            await conn.executemany('''
                INSERT INTO seed_candidates (username, source, audience_hint, validation_status, invite_link)
                VALUES ($1, 'tg-catalog-invite', 'sfw', 'pending', $2)
                ON CONFLICT (username) DO NOTHING
            ''', [(f'INVITE:{h}', url) for h, url in invites.items()])
            print(f'inserted up to {len(invites)} new invite candidates')

        # INSERT layer 3 into iptv_sources
        if iptv_rows:
            for kind, raw, prov, msg_id, chan in iptv_rows:
                await conn.execute('''
                    INSERT INTO iptv_sources (kind, raw_match, provider_hint, found_in_msg_id, channel_username)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (kind, raw_match) DO UPDATE SET
                      hit_count = iptv_sources.hit_count + 1,
                      last_seen_at = NOW()
                ''', kind, raw, prov, msg_id, chan)
            print(f'upserted {len(iptv_rows)} iptv_sources rows')

    await pool.close()


if __name__ == '__main__':
    asyncio.run(main())
