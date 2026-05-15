"""tgarr crawler — Pyrogram MTProto channel/group listener + back-filler + download worker.

Responsibilities:
- On startup, back-fill historical messages from every joined channel/group/supergroup.
- Listen for new incoming messages continuously, indexing media metadata.
- Run filename parser on each new message → create release row if score ≥ threshold.
- Background download worker: poll `downloads` table, fetch media via MTProto,
  drop completed file into /downloads/tgarr/<release-name>/.
"""
import asyncio
import gzip
import hashlib
import json
import logging
import random
import struct
import time
import os
import re
import urllib.parse
import urllib.request
import uuid as uuidlib

import asyncpg
from pyrogram import Client
from pyrogram.file_id import FileId
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

# Set in main() after app.start() — the TG user id of the connected account.
# Workers filter channel access by this so an account switch leaves the prior
# account's channels untouched (not poll'd, not deep_backfill'd, etc.).
CURRENT_USER_ID: int = 0


# ════════════════════════════════════════════════════════════════════
# Global MTProto rate limiter + circuit breaker
#
# Two failure modes the bare Pyrogram API exposes us to:
#   1. FloodWait — TG punishes burst of calls with N-second wait. Critically,
#      retrying inside the window EXTENDS it. So when ANY worker sees a
#      FloodWait > 60s, ALL workers must pause uniformly.
#   2. Per-method ceilings — `resolveUsername` ~5/min, `get_chat` ~10/min,
#      `download_media` higher. Exceeding triggers FloodWait.
#
# This module gates every Pyrogram call through `_mtproto(...)`. It enforces
# per-method token buckets, observes FloodWait, and sets a global halt that
# all subsequent calls (across all workers) honor.
# ════════════════════════════════════════════════════════════════════
_MTPROTO_HALT_UNTIL: dict = {}  # method → epoch sec when that method's FloodWait clears
_MTPROTO_LOCK = asyncio.Lock()
_MTPROTO_BUDGETS = {
    # method → (max calls, window seconds). Tuned conservative to stay below
    # TG's actual ceilings even with concurrent workers.
    "resolveUsername":   (5, 60),    # 5/min — most flood-prone
    "get_chat":          (10, 60),
    "get_chat_history":  (30, 60),
    "get_messages":      (30, 60),
    "download_media":    (60, 60),
    "join_chat":         (3, 60),    # rare; TG hard-caps ~20/day
    "leave_chat":        (3, 60),
    "search_messages_count": (10, 60),
    "default":           (30, 60),   # unclassified calls
}
_MTPROTO_CALL_LOG: dict = {}  # method → list[float] of recent call timestamps


async def _mtproto(method_name: str, coro_factory):
    """Gate Pyrogram calls: per-method rate limit + per-method halt + FloodWait observation.
    Halt is per-method: TG flood-waits specific endpoints (e.g. resolveUsername)
    rather than the whole session, so other methods stay unblocked.
    """
    while True:
        # 1. Honor THIS method's halt (set by prior FloodWait on same method).
        now = time.time()
        halt_until = _MTPROTO_HALT_UNTIL.get(method_name, 0)
        if halt_until > now:
            remaining = halt_until - now
            log.info("[mtproto-rl] halt %s: %ds remaining → sleeping",
                     method_name, int(remaining))
            await asyncio.sleep(min(remaining + 1, 60))
            continue
        # 2. Per-method token bucket.
        limit, window = _MTPROTO_BUDGETS.get(method_name,
                                            _MTPROTO_BUDGETS["default"])
        async with _MTPROTO_LOCK:
            bucket = _MTPROTO_CALL_LOG.setdefault(method_name, [])
            now = time.time()
            bucket[:] = [t for t in bucket if now - t < window]
            if len(bucket) >= limit:
                wait_s = window - (now - bucket[0]) + 0.1
            else:
                wait_s = 0
                bucket.append(now)
        if wait_s > 0:
            log.debug("[mtproto-rl] %s at %d/%ds — sleep %.1fs",
                      method_name, limit, window, wait_s)
            await asyncio.sleep(wait_s)
            continue
        break
    # 3. Make call. FloodWait → halt JUST this method.
    try:
        return await coro_factory()
    except FloodWait as fw:
        fw_val = int(getattr(fw, "value", 60) or 60)
        until = time.time() + fw_val + 5
        async with _MTPROTO_LOCK:
            if until > _MTPROTO_HALT_UNTIL.get(method_name, 0):
                _MTPROTO_HALT_UNTIL[method_name] = until
        log.warning("[mtproto-rl] %s FloodWait %ds → halt this method only",
                    method_name, fw_val)
        raise


async def _mtproto_wait_clearance(method_name: str = "get_chat_history"):
    """Block until the given method's halt expires. Use before async generators
    which can't be wrapped call-by-call. Default to get_chat_history.
    """
    while True:
        now = time.time()
        halt_until = _MTPROTO_HALT_UNTIL.get(method_name, 0)
        if halt_until > now:
            remaining = halt_until - now
            log.info("[mtproto-rl] halt %s: %ds remaining → sleeping",
                     method_name, int(remaining))
            await asyncio.sleep(min(remaining + 1, 60))
            continue
        return


async def _mtproto_get_messages_with_auto_join(chat_id, msg_id):
    """get_messages with auto-join on CHANNEL_INVALID — for restricted channels
    where the session isn't yet a member. join_chat is itself rate-limited;
    if join also fails, propagates the original error to caller.
    """
    from pyrogram.errors import ChannelInvalid
    try:
        return await _mtproto("get_messages",
                               lambda: app.get_messages(chat_id, msg_id))
    except ChannelInvalid:
        # First try a refresh via get_chat_history (cheap, often fixes stale
        # access_hash). NOTE: get_chat_history is an async generator — can't
        # be wrapped in `await coro()`. Use clearance gate + iterate directly.
        try:
            await _mtproto_wait_clearance("get_chat_history")
            async for _ in app.get_chat_history(chat_id, limit=1):
                break
            return await _mtproto("get_messages",
                                   lambda: app.get_messages(chat_id, msg_id))
        except ChannelInvalid:
            pass
        # Last resort: join the channel. join_chat(chat_id) only works if we
        # have access_hash. For a new account that never resolved this peer,
        # we need username lookup. Try chat_id first (cheap), fallback to
        # username resolve (uses resolveUsername quota).
        log.info("[mtproto] auto-join %s after CHANNEL_INVALID", chat_id)
        try:
            await _mtproto("join_chat", lambda: app.join_chat(chat_id))
        except ChannelInvalid:
            # No access_hash cached — resolve via username then join.
            async with db_pool.acquire() as conn:
                uname = await conn.fetchval(
                    "SELECT username FROM channels WHERE tg_chat_id=$1", chat_id)
            if not uname:
                raise
            log.info("[mtproto] join by username @%s (no access_hash for %s)",
                     uname, chat_id)
            await _mtproto("join_chat", lambda: app.join_chat(uname))
        return await _mtproto("get_messages",
                               lambda: app.get_messages(chat_id, msg_id))

MEILI_URL = os.environ.get("MEILI_URL", "http://meili:7700")
MEILI_KEY = os.environ.get("MEILI_MASTER_KEY", "")
_meili_queue: "asyncio.Queue[tuple[str, dict]] | None" = None


def _meili_enqueue(index: str, doc: dict) -> None:
    """Best-effort push to local queue. Drains in meili_sync_worker."""
    if _meili_queue is None or not MEILI_KEY:
        return
    try:
        _meili_queue.put_nowait((index, doc))
    except asyncio.QueueFull:
        pass  # drop oldest batch — sync isn't critical-path


async def meili_sync_worker():
    """Drain _meili_queue every 5s in batches, POST to Meili.
    Best-effort: Meili down ≠ ingest stops. Drops on error after retry."""
    global _meili_queue
    _meili_queue = asyncio.Queue(maxsize=20000)
    log.info("[meili] sync worker started url=%s", MEILI_URL)
    while True:
        await asyncio.sleep(5)
        if _meili_queue.empty():
            continue
        # Drain up to 2000 per cycle, group by index
        buckets: dict[str, list[dict]] = {}
        while not _meili_queue.empty() and sum(len(v) for v in buckets.values()) < 2000:
            try:
                idx, doc = _meili_queue.get_nowait()
                buckets.setdefault(idx, []).append(doc)
            except asyncio.QueueEmpty:
                break
        for idx, docs in buckets.items():
            try:
                def _post():
                    req = urllib.request.Request(
                        f"{MEILI_URL}/indexes/{idx}/documents",
                        data=json.dumps(docs).encode(),
                        method="POST",
                        headers={"Authorization": f"Bearer {MEILI_KEY}",
                                 "Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        return r.read()
                await asyncio.to_thread(_post)
                log.debug("[meili] flushed %d to %s", len(docs), idx)
            except Exception as e:
                log.warning("[meili] flush %s failed: %s (dropping %d docs)",
                            idx, e, len(docs))

SAFE_NAME = re.compile(r"[^\w\-._]+")

# Time-sensitive content category detection. News-style channels have
# rapidly-decaying value — 20-year-old news is historical archive territory,
# not Sonarr/Radarr/Jellyfin replacement use case. Deep-backfill these only
# back to NEWS_BACKFILL_DAYS, not to message id 1.
# Decay model: per-category half-life. cutoff = 3 × half_life (V → ~12.5%).
# Add new categories here without touching DB schema.
_DECAY_HALF_LIFE_DAYS = {
    'archival': None,   # movies/TV/ebooks/music — never decay
    'mixed':    365,    # default — 2y window
    'news':     30,     # news → 90d cutoff
    'sports':   14,     # sports → 42d
    'chat':     7,      # group/discussion → 21d
}

# Title-based category hint (coarse). Adaptive page-level detection in
# deep_backfill_worker is the safety net for misclassified channels.
_CATEGORY_HINTS = [
    ('news',   re.compile(
        r"(\bnews\b|\bnewsroom\b|\bbreaking\b|\bheadlines\b|\bdaily\b|"
        r"新闻|头条|资讯|快讯|实时|"
        r"اخبار|новости|notici|"
        r"international tv|интернешнл|"
        r"broadcasting|live tv|news network|news agency)",
        re.IGNORECASE)),
    ('sports', re.compile(r"\b(sports?|football|soccer|nba|nhl|球|赛|подкаст)\b", re.IGNORECASE)),
    ('chat',   re.compile(r"\b(chat|discussion|group|聊天|交流|обсуждение)\b", re.IGNORECASE)),
]


# Multi-signal quality gate at ingest. Rejecting low-value messages here is
# cheaper than indexing then evicting them later.
_AD_KEYWORDS = re.compile(
    r"(\bcrypto\b|\bnft\b|\busdt\b|赌博|赌场|博彩|casino|gamble|gambling|"
    r"投资理财|做单|赚钱|月入|代理|代充|代购|刷单|"
    r"telegram[\s_]*bot|airdrop|"
    r"加[\s_]*我[\s_]*(微信|tg|telegram|wx)|私[\s_]*聊|"
    r"limited[\s_]*offer|click[\s_]*here|join[\s_]*now[\s_]*free|"
    r"earn[\s_]*money|earn[\s_]*cash|"
    r"vip[\s_]*会员|赚钱机会|高佣金)",
    re.IGNORECASE)

_MIN_SIZES = {
    # 5MB video proxy for ~3min @ 720p — sub-3min videos are usually
    # ads, teasers, news clips, social-media filler. Real movies/episodes
    # always cross this floor (movie min ~80-200MB; TV ep ~30-300MB).
    'video':    5_000_000,
    'audio':    100_000,      # < 100KB audio → voice note / clip
    'document': 10_000,       # < 10KB document → just text junk
    'photo':    5_000,        # < 5KB photo → ad logo / thumb only
}


def _quality_check(file_name: str | None, file_size: int | None,
                   media_type: str | None, caption: str | None) -> tuple[bool, str | None]:
    """Multi-signal quality gate. Returns (keep?, reject_reason_if_drop).

    Composite scoring: any single hard signal → reject. Cheap O(1) checks only.
    Captions are mostly NULL by now (we extract+discard at ingest), so file_name
    + size do most of the work.
    """
    blob = (file_name or "") + " " + (caption or "")
    if blob.strip() and _AD_KEYWORDS.search(blob):
        return False, "ad-keyword"
    floor = _MIN_SIZES.get(media_type)
    if floor and file_size and file_size < floor:
        return False, f"micro-{media_type}"
    return True, None


def _detect_content_category(title: str, username: str) -> str:
    """Returns category string. NULL/None = 'mixed' (default 2y cutoff)."""
    blob = (title or "") + " " + (username or "")
    for cat, rx in _CATEGORY_HINTS:
        if rx.search(blob):
            return cat
    return 'mixed'


def _cutoff_days(category: str) -> int | None:
    """3 × half-life. None = no cutoff (archival)."""
    hl = _DECAY_HALF_LIFE_DAYS.get(category or 'mixed')
    if hl is None:
        return None
    return 3 * hl


# === Auto-grab matcher (Sonarr/Radarr feature parity) ===
# Resolution rank for quality preference comparison
_RES_RANK = {'2160p': 4, '1080p': 3, '720p': 2, '480p': 1, '360p': 0}


def _detect_lang(text: str | None) -> str | None:
    """Cheap Unicode-range language detection. Returns ISO 639-1-like code.
    Used to populate messages.detected_lang for UI filters.
    """
    if not text or not text.strip():
        return None
    cjk = arabic = cyrillic = hangul = hiragana_kata = devanagari = hebrew = thai = latin = 0
    for c in text:
        cp = ord(c)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF: cjk += 1
        elif 0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF: hiragana_kata += 1
        elif 0xAC00 <= cp <= 0xD7AF: hangul += 1
        elif 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F: arabic += 1
        elif 0x0400 <= cp <= 0x04FF: cyrillic += 1
        elif 0x0900 <= cp <= 0x097F: devanagari += 1
        elif 0x0590 <= cp <= 0x05FF: hebrew += 1
        elif 0x0E00 <= cp <= 0x0E7F: thai += 1
        elif 0x0041 <= cp <= 0x007A: latin += 1
    if hangul > 2: return 'ko'
    if hiragana_kata > 2: return 'ja'  # hiragana before cjk — JP can have kanji
    if cjk > 3: return 'zh'
    if hebrew > 2: return 'he'
    if thai > 2: return 'th'
    if devanagari > 2: return 'hi'
    if cyrillic > 4: return 'ru'
    if arabic > 4:
        # Persian vs Arabic: chars 0x067E (پ), 0x0686 (چ), 0x0698 (ژ), 0x06AF (گ) are Persian-only
        if any(c in text for c in 'پچژگکیی'):
            return 'fa'
        return 'ar'
    if latin > 3: return 'en'
    return None


def _release_passes_profile(rel: dict, profile: dict) -> tuple[bool, str]:
    """Check if release matches quality profile rules."""
    size = rel.get('size_bytes') or 0
    if profile.get('min_size_bytes') and size < profile['min_size_bytes']:
        return False, f"below_min_size ({size} < {profile['min_size_bytes']})"
    if profile.get('max_size_bytes') and size > profile['max_size_bytes']:
        return False, f"above_max_size ({size} > {profile['max_size_bytes']})"
    if profile.get('preferred_resolutions'):
        q = rel.get('quality')
        if q and q not in profile['preferred_resolutions']:
            return False, f"resolution_not_preferred ({q})"
    return True, "ok"


async def maybe_auto_grab(conn, release_id: int, parsed: dict, file_size: int):
    """Decide if a freshly-parsed release should be auto-grabbed.

    Matches against monitored_series/monitored_movies; quality_profile filter;
    INSERTs into downloads if approved. Logs decision to auto_grab_log.
    """
    rel = {
        'id': release_id,
        'title': parsed.get('title') or '',
        'year': parsed.get('year'),
        'season': parsed.get('season'),
        'episode': parsed.get('episode'),
        'quality': parsed.get('quality'),
        'size_bytes': file_size,
        'type': parsed.get('type'),
    }
    if rel['type'] == 'tv' and rel['title']:
        mon = await conn.fetchrow(
            """SELECT ms.id, ms.quality_profile_id, qp.preferred_resolutions,
                      qp.preferred_codecs, qp.max_size_bytes, qp.min_size_bytes
               FROM monitored_series ms
               LEFT JOIN quality_profile qp ON qp.id = ms.quality_profile_id
               WHERE ms.status = 'active'
                 AND LOWER(ms.series_title) = LOWER($1)
               LIMIT 1""", rel['title'])
        if not mon:
            return
        ok, reason = _release_passes_profile(rel, dict(mon))
        if not ok:
            await conn.execute(
                "INSERT INTO auto_grab_log (release_id, monitored_kind, monitored_id, action, reason) "
                "VALUES ($1, 'series', $2, 'rejected', $3)",
                release_id, mon['id'], reason)
            return
        # Check we don't already have this episode grabbed
        already = await conn.fetchval(
            """SELECT 1 FROM downloads d JOIN releases r ON r.id = d.release_id
               WHERE r.category = 'tv'
                 AND LOWER(r.series_title) = LOWER($1)
                 AND r.season = $2 AND r.episode = $3
                 AND d.status IN ('pending','downloading','completed')
               LIMIT 1""", rel['title'], rel['season'], rel['episode'])
        if already:
            return
        await conn.execute(
            "INSERT INTO downloads (release_id, status, requested_at) VALUES ($1, 'pending', NOW())",
            release_id)
        await conn.execute(
            "INSERT INTO auto_grab_log (release_id, monitored_kind, monitored_id, action, reason) "
            "VALUES ($1, 'series', $2, 'grabbed', $3)",
            release_id, mon['id'], reason)
        log.info("[auto-grab] series @%s S%02dE%02d release_id=%s — queued",
                 rel['title'], rel['season'] or 0, rel['episode'] or 0, release_id)
    elif rel['type'] == 'movie' and rel['title']:
        mon = await conn.fetchrow(
            """SELECT mm.id, mm.quality_profile_id, mm.grabbed_release_id,
                      qp.preferred_resolutions, qp.preferred_codecs,
                      qp.max_size_bytes, qp.min_size_bytes
               FROM monitored_movies mm
               LEFT JOIN quality_profile qp ON qp.id = mm.quality_profile_id
               WHERE mm.status IN ('wanted','grabbed')
                 AND LOWER(mm.title) = LOWER($1)
                 AND ($2::int IS NULL OR mm.year = $2)
               LIMIT 1""", rel['title'], rel['year'])
        if not mon:
            return
        ok, reason = _release_passes_profile(rel, dict(mon))
        if not ok:
            await conn.execute(
                "INSERT INTO auto_grab_log (release_id, monitored_kind, monitored_id, action, reason) "
                "VALUES ($1, 'movie', $2, 'rejected', $3)",
                release_id, mon['id'], reason)
            return
        if mon['grabbed_release_id']:
            # Already grabbed something — compare quality, upgrade if better
            prev = await conn.fetchrow(
                "SELECT quality, size_bytes FROM releases WHERE id=$1", mon['grabbed_release_id'])
            if prev and _RES_RANK.get(rel['quality'], 0) <= _RES_RANK.get(prev['quality'], 0):
                await conn.execute(
                    "INSERT INTO auto_grab_log (release_id, monitored_kind, monitored_id, action, reason) "
                    "VALUES ($1, 'movie', $2, 'rejected', 'not_upgrade')",
                    release_id, mon['id'])
                return
            action = 'upgraded'
        else:
            action = 'grabbed'
        await conn.execute(
            "INSERT INTO downloads (release_id, status, requested_at) VALUES ($1, 'pending', NOW())",
            release_id)
        await conn.execute(
            "UPDATE monitored_movies SET status='grabbed', grabbed_release_id=$1 WHERE id=$2",
            release_id, mon['id'])
        await conn.execute(
            "INSERT INTO auto_grab_log (release_id, monitored_kind, monitored_id, action, reason) "
            "VALUES ($1, 'movie', $2, $3, 'profile_match')",
            release_id, mon['id'], action)
        log.info("[auto-grab] movie %s (%s) release_id=%s — %s",
                 rel['title'], rel['year'], release_id, action)

# Caption-mention discovery: extract new channel hints from message text
_MENTION_RX = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
_INVITE_RX = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/(?:joinchat/|\+)([A-Za-z0-9_-]{16,32})")
# Skip ones our own crawler / common bots / generic words that look like usernames
_MENTION_SKIP = {
    "tgarr", "tgarr_bot", "telegram", "telegrambot", "username",
    "channel", "channels", "admin", "support", "bot", "bots",
    "addlist", "addstickers", "share", "you", "your", "me", "myself",
}
CONTRIBUTE_MENTIONS_ENABLED = os.environ.get("TGARR_CONTRIBUTE_MENTIONS", "true").lower() == "true"


def _extract_mentions(text: str) -> set[str]:
    if not text:
        return set()
    out = set()
    for m in _MENTION_RX.finditer(text):
        u = m.group(1)
        if u.lower() in _MENTION_SKIP:
            continue
        out.add(u)
    return out


def _extract_invites(text: str) -> set[str]:
    if not text:
        return set()
    return set(_INVITE_RX.findall(text))


async def init_db() -> None:
    global db_pool
    db_pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=4)
    log.info("postgres pool ready")


async def _heartbeat(worker: str, action: str = "", error: str | None = None) -> None:
    """Write worker heartbeat into worker_status table.
    Call from inside each worker's main loop iteration.
    """
    try:
        async with db_pool.acquire() as conn:
            if error:
                await conn.execute(
                    "INSERT INTO worker_status "
                    "  (worker, last_seen, last_action, iter_count, error_count, "
                    "   last_error, last_error_at) "
                    "VALUES ($1, NOW(), $2, 1, 1, $3, NOW()) "
                    "ON CONFLICT (worker) DO UPDATE SET "
                    "  last_seen=NOW(), last_action=$2, "
                    "  iter_count=worker_status.iter_count+1, "
                    "  error_count=worker_status.error_count+1, "
                    "  last_error=$3, last_error_at=NOW()",
                    worker, action[:200], error[:500])
            else:
                await conn.execute(
                    "INSERT INTO worker_status "
                    "  (worker, last_seen, last_action, iter_count) "
                    "VALUES ($1, NOW(), $2, 1) "
                    "ON CONFLICT (worker) DO UPDATE SET "
                    "  last_seen=NOW(), last_action=$2, "
                    "  iter_count=worker_status.iter_count+1",
                    worker, action[:200])
    except Exception:
        pass  # never let heartbeat failures cascade


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
    rel_id = await conn.fetchval(
        """INSERT INTO releases
             (name, category, series_title, season, episode,
              movie_title, movie_year, quality, source, codec,
              size_bytes, posted_at, primary_msg_id, parse_score)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
           RETURNING id""",
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
    # Auto-grab pipeline: if this release matches monitored series/movie + quality
    # profile, enqueue download. Sonarr/Radarr feature parity. Best-effort,
    # never let failure block the parse.
    try:
        if rel_id:
            await maybe_auto_grab(conn, rel_id, parsed, file_size)
    except Exception as e:
        log.warning("[auto-grab] error rel_id=%s: %s", rel_id, e)
    if rel_id:
        _meili_enqueue("releases", {
            "id": rel_id,
            "name": rname,
            "canonical_title": rname,
            "series_title": parsed.get("title") if type_ == "tv" else None,
            "movie_title": parsed.get("title") if type_ == "movie" else None,
            "category": type_,
            "quality": parsed.get("quality") or "",
            "movie_year": parsed.get("year") or 0,
            "season": parsed.get("season") or 0,
            "episode": parsed.get("episode") or 0,
            "parse_score": float(score or 0),
            "grab_count": 0,
            "size_bytes": file_size or 0,
            "posted_at_ts": int(posted_at.timestamp()) if posted_at else 0,
            "audience": "sfw",
        })
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

    file_dc = None
    if media:
        file_name = getattr(media, "file_name", None)
        file_size = getattr(media, "file_size", None)
        mime_type = getattr(media, "mime_type", None)
        file_unique_id = getattr(media, "file_unique_id", None)
        file_dc = getattr(media, "dc_id", None)
        if not file_dc:
            try:
                file_dc = FileId.decode(media.file_id).dc_id
            except Exception:
                file_dc = None

    caption = msg.caption or msg.text or ""

    async with db_pool.acquire() as conn:
        # Never overwrite a populated username/title with empty — Pyrogram
        # sometimes returns msg.chat.username='' for forwarded/edited messages
        # even though the channel HAS a real public @username. Preserve the
        # known good value.
        ch_id = await conn.fetchval(
            """INSERT INTO channels (tg_chat_id, username, title, category, account_user_id)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (tg_chat_id) DO UPDATE
                 SET username = COALESCE(NULLIF(EXCLUDED.username, ''), channels.username),
                     title = COALESCE(NULLIF(EXCLUDED.title, ''), channels.title),
                     account_user_id = EXCLUDED.account_user_id
               RETURNING id""",
            chat_id,
            msg.chat.username,
            msg.chat.title or "",
            chat_type.lower(),
            CURRENT_USER_ID or None,
        )
        # === Caption-mention extraction (always; contribute gated separately) ===
        # Even text-only messages can contain @mention / invite links — those
        # are pure discovery signal independent of media payload.
        if caption:
            try:
                mentions = _extract_mentions(caption)
                invites = _extract_invites(caption)
                if mentions or invites:
                    aud_hint = await conn.fetchval(
                        "SELECT audience FROM channels WHERE id=$1", ch_id) or "sfw"
                    for u in mentions:
                        await conn.execute(
                            "INSERT INTO seed_candidates "
                            "  (username, source, audience_hint, validation_status) "
                            "VALUES ($1, 'caption-mention', $2, NULL) "
                            "ON CONFLICT (username) DO NOTHING",
                            u, aud_hint)
                    for inv in invites:
                        ikey = f"INVITE:{inv[:24]}"
                        iurl = (f"https://t.me/joinchat/{inv}"
                                if inv.startswith("joinchat") else f"https://t.me/+{inv}")
                        await conn.execute(
                            "INSERT INTO seed_candidates "
                            "  (username, invite_link, source, audience_hint, "
                            "   validation_status) "
                            "VALUES ($1, $2, 'caption-invite', $3, NULL) "
                            "ON CONFLICT (username) DO NOTHING",
                            ikey, iurl, aud_hint)
            except Exception:
                pass  # never let extraction break ingest

        # NOISE FILTER: skip persisting messages with no media attachment.
        # text-only chatter has 0 resource value (we already extracted any
        # @mention / invite link signals above for discovery).
        if media is None:
            return False
        # QUALITY GATE: ad-keyword / micro-size junk rejected before persist.
        keep, reason = _quality_check(file_name, file_size, media_type, caption)
        if not keep:
            return False
        # Raw caption discarded — we already extracted mention/invite signals
        # above into seed_candidates. Storing raw text wastes ~200B/msg with
        # near-zero re-extract value. Use the structured fields downstream.
        # Detect language from file_name (caption already null) for UI filters.
        detected_lang = _detect_lang(file_name) or _detect_lang(msg.chat.title or "")
        new_msg_id = await conn.fetchval(
            """INSERT INTO messages
                 (channel_id, tg_message_id, tg_chat_id,
                  file_unique_id, file_name, caption, file_size, mime_type,
                  media_type, audio_title, audio_performer, audio_duration_sec,
                  posted_at, file_dc, detected_lang)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
               ON CONFLICT (channel_id, tg_message_id) DO NOTHING
               RETURNING id""",
            ch_id, msg_id, chat_id, file_unique_id, file_name,
            None, file_size, mime_type, media_type,
            audio_title, audio_performer, audio_duration_sec, msg.date,
            file_dc, detected_lang,
        )

        if new_msg_id and file_name:
            try:
                await maybe_create_release(conn, new_msg_id, file_name, file_size, msg.date)
            except Exception as e:
                log.warning("[parse] failed msg_id=%s file=%s err=%s", new_msg_id, file_name, e)

        if new_msg_id:
            _meili_enqueue("messages", {
                "id": new_msg_id,
                "file_name": file_name,
                "audio_title": audio_title,
                "audio_performer": audio_performer,
                "audio_canonical_title": None,
                "detected_lang": detected_lang or "unknown",
                "media_type": media_type or "unknown",
                "channel_username": msg.chat.username,
                "audience": "sfw",
                "has_local": False,
                "file_size": file_size or 0,
                "posted_at_ts": int(msg.date.timestamp()) if msg.date else 0,
            })

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
    await _mtproto_wait_clearance()
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
    await _mtproto_wait_clearance()
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


_arr_alias_cache: list = []  # (alias_text, canonical, season, kind)
_arr_alias_loaded_at: float = 0.0


async def _refresh_arr_aliases():
    """Same series_aliases table the api side maintains via _arr_alias_sync_worker.
    Cache in crawler for filename rewrites; reload every 60s."""
    global _arr_alias_cache, _arr_alias_loaded_at
    if time.time() - _arr_alias_loaded_at < 60:
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT alias_text, canonical_title, season, kind FROM series_aliases "
            "ORDER BY length(alias_text) DESC")
    _arr_alias_cache = [(r["alias_text"], r["canonical_title"],
                         r["season"], r["kind"]) for r in rows]
    _arr_alias_loaded_at = time.time()


async def _rename_for_arr(local_path: str, row) -> str:
    """Rename a freshly downloaded file to a Sonarr/Radarr-parseable name.

    Sonarr's parser can't extract series/season/episode from CJK filenames
    like ``[唯电影]《舌尖上的中国第三季》第1集 器.mp4`` and refuses to import.
    We rewrite to ``A Bite of China.S03E01.1080p.WEBRip-tgarr.mp4`` using the
    same series_aliases table the newznab feed uses, so the on-disk name
    matches what Sonarr's CDH was told to expect.

    Returns the (possibly new) path. No-op if no alias matches.
    """
    if not local_path or not os.path.exists(local_path):
        return local_path
    await _refresh_arr_aliases()
    raw = row["rel_name"] or ""
    ext = os.path.splitext(local_path)[1] or ".mp4"
    ep = row["episode"] or 1
    seas_default = row["season"] or 1
    qual = row["quality"] or "1080p"
    new_name = None
    for alias_text, canonical, season, kind in _arr_alias_cache:
        if alias_text and alias_text in raw:
            if kind == "movie":
                yr = row["movie_year"] or ""
                yr_suffix = f".{yr}" if yr else ""
                new_name = f"{canonical}{yr_suffix}.{qual}.WEBRip-tgarr{ext}"
            else:
                seas = season if season is not None else seas_default
                new_name = f"{canonical}.S{int(seas):02d}E{int(ep):02d}.{qual}.WEBRip-tgarr{ext}"
            break
    if not new_name:
        return local_path
    # Filesystem-safe but keep spaces (Sonarr parses both, spaces look nicer)
    new_name = re.sub(r'[\\/:*?"<>|]', '_', new_name)[:200]
    new_path = os.path.join(os.path.dirname(local_path), new_name)
    if new_path == local_path:
        return local_path
    try:
        os.rename(local_path, new_path)
        log.info("[worker] renamed for arr id=%s %s → %s",
                 row["dl_id"], os.path.basename(local_path), new_name)
        return new_path
    except OSError as e:
        log.warning("[worker] rename-for-arr failed id=%s: %s", row["dl_id"], e)
        return local_path


async def _notify_arr(local_path: str, row) -> None:
    """Push Sonarr/Radarr to scan the output folder via tgarr-api internal
    endpoint (api owns the SONARR/RADARR creds; crawler doesn't).

    This makes import self-healing: even when Sonarr's queue tracker
    dropped the downloadId (which happens on tgarr-api downtime — the
    failure mode that orphaned bite-China E02-E08 today), the push-scan
    command makes Sonarr's own DiskScanService walk the folder + import.

    Fire-and-forget; failure here doesn't fail the download.
    """
    try:
        kind = "movie" if (row.get("movie_year") and not row.get("episode")) else "tv"
        body = json.dumps({"path": local_path, "kind": kind}).encode()
        req = urllib.request.Request(
            "http://tgarr-api:8765/api/internal/arr-scan",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST")
        await asyncio.to_thread(
            lambda: urllib.request.urlopen(req, timeout=10).read())
        log.info("[worker] notified arr scan id=%s kind=%s path=%s",
                 row["dl_id"], kind, local_path)
    except Exception as e:
        log.warning("[worker] arr-notify failed id=%s: %s",
                    row["dl_id"], e)


CURRENT_DC: int = 0  # populated in main() after app.start()
_SAME_DC_SEM = asyncio.Semaphore(5)   # 5 parallel same-DC downloads
_CROSS_DC_SEM = asyncio.Semaphore(1)  # 1 cross-DC at a time (auth.ExportAuthorization budget)


async def _claim_pending(same_dc: bool):
    """Atomically claim one pending download matching the DC predicate.
    Two-step: FOR UPDATE SKIP LOCKED to safely pick + flip status, then a
    second fetch of the full join. Race-free across dispatchers."""
    async with db_pool.acquire() as conn:
        if same_dc:
            pred = "(m.file_dc = $1 OR m.file_dc IS NULL)"
        else:
            pred = "(m.file_dc <> $1)"
        claimed = await conn.fetchval(f"""
            WITH claim AS (
              SELECT d.id
              FROM downloads d
              JOIN releases r ON r.id = d.release_id
              JOIN messages m ON m.id = r.primary_msg_id
              WHERE d.status = 'pending' AND {pred}
              ORDER BY d.requested_at
              FOR UPDATE OF d SKIP LOCKED
              LIMIT 1
            )
            UPDATE downloads SET status='downloading'
            WHERE id IN (SELECT id FROM claim)
            RETURNING id;
        """, CURRENT_DC)
        if not claimed:
            return None
        rec = await conn.fetchrow("""
            SELECT d.id AS dl_id, d.release_id, r.name AS rel_name,
                   r.season, r.episode, r.quality,
                   r.movie_year, r.category,
                   m.tg_chat_id, m.tg_message_id, m.media_type,
                   m.file_size, m.file_name, m.file_dc,
                   c.username AS channel_username
            FROM downloads d
            JOIN releases r ON r.id = d.release_id
            JOIN messages m ON m.id = r.primary_msg_id
            JOIN channels c ON c.id = m.channel_id
            WHERE d.id = $1;
        """, claimed)
        return dict(rec) if rec else None


_active_tasks: dict = {}  # dl_id -> asyncio.Task (live download workers only)


async def _cancel_watcher() -> None:
    """Poll DB every 5s for rows that the api marked 'paused' or 'cancelled'.
    If a matching task is currently in _active_tasks, cancel it so the
    in-flight download aborts. The worker's except CancelledError block
    then leaves the row status alone (the api already set it) and skips
    the 'failed' flip."""
    while True:
        try:
            await asyncio.sleep(5)
            if not _active_tasks:
                continue
            ids = list(_active_tasks.keys())
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, status FROM downloads "
                    "WHERE id = ANY($1::bigint[]) "
                    "AND status IN ('paused','cancelled')", ids)
            for r in rows:
                t = _active_tasks.get(r["id"])
                if t and not t.done():
                    log.info("[cancel-watch] aborting id=%s reason=%s",
                             r["id"], r["status"])
                    t.cancel()
        except Exception:
            log.exception("[cancel-watch] loop error")


async def _process_one_download(row_payload: dict) -> None:
    """The actual download + rename + chmod work for a single claimed row.
    Extracted from download_worker so the dispatcher can spawn multiple of
    these concurrently under a semaphore."""
    row = row_payload  # dict, not asyncpg Record — same key access pattern
    _active_tasks[row["dl_id"]] = asyncio.current_task()
    try:
        safe_name = SAFE_NAME.sub("_", row["rel_name"])[:160]
        target_dir = os.path.join(DOWNLOAD_ROOT, safe_name)
        os.makedirs(target_dir, exist_ok=True)
        same_dc = (row.get("file_dc") in (None, CURRENT_DC))
        log.info("[worker] downloading id=%s release=%s media=%s dc=%s(%s) → %s",
                 row["dl_id"], row["rel_name"], row["media_type"],
                 row.get("file_dc"), "same" if same_dc else "cross", target_dir)

        local_path = None
        # ── Path 1: HTTPS for video on public channels (no MTProto burn). ──
        if (row["media_type"] == "video" and row["channel_username"]):
            try:
                vurl = await _fetch_video_url_https(
                    row["channel_username"], row["tg_message_id"])
                if vurl:
                    fname = row["file_name"] or f"{safe_name}.mp4"
                    fname = SAFE_NAME.sub("_", fname)[:200]
                    if not fname.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
                        fname += ".mp4"
                    target_path = os.path.join(target_dir, fname)
                    written = await _download_video_https(
                        vurl, target_path, row["file_size"],
                        dl_id=row["dl_id"])
                    local_path = target_path
                    log.info("[worker] HTTPS video done id=%s bytes=%d",
                             row["dl_id"], written)
            except Exception as he:
                log.warning("[worker] HTTPS video failed id=%s: %s — falling back to MTProto",
                            row["dl_id"], he)

        # ── Path 2: MTProto fallback. User doesn't see this distinction.
        # Cross-DC ops are serialized by _CROSS_DC_SEM in the dispatcher.
        if local_path is None:
            if os.environ.get("TGARR_THUMB_ONLY", "false").lower() == "true":
                raise RuntimeError("THUMB_ONLY mode: MTProto download disabled")
            msg = await _mtproto_get_messages_with_auto_join(
                row["tg_chat_id"], row["tg_message_id"])
            if not msg or not (msg.video or msg.document or msg.audio):
                raise RuntimeError("media missing on source message")
            dl_id = row["dl_id"]
            progress_state = {"last_t": time.time(), "last_b": 0}
            async def _progress(current, total):
                now = time.time()
                dt = now - progress_state["last_t"]
                if dt < 5 and current < total:
                    return
                db = current - progress_state["last_b"]
                kbps = int(db / 1024 / max(dt, 0.001))
                progress_state["last_t"] = now
                progress_state["last_b"] = current
                try:
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            """UPDATE downloads
                               SET bytes_done = GREATEST(COALESCE(bytes_done,0), $1),
                                   speed_kbps=$2, last_progress_at=NOW()
                               WHERE id=$3""",
                            int(current), kbps, dl_id)
                except Exception:
                    pass
            local_path = await _mtproto("download_media",
                lambda: app.download_media(msg, file_name=f"{target_dir}/",
                                           progress=_progress))
        local_path = await _rename_for_arr(str(local_path), row)
        try:
            os.chmod(local_path, 0o666)
            os.chmod(target_dir, 0o777)
        except OSError as e:
            log.warning("[worker] chmod failed id=%s: %s", row["dl_id"], e)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE downloads
                   SET status='completed', finished_at=NOW(), local_path=$1
                   WHERE id=$2""",
                str(local_path), row["dl_id"])
        log.info("[worker] completed id=%s path=%s", row["dl_id"], local_path)
        # Push-notify Sonarr/Radarr to scan + import — self-heals when CDH
        # queue tracking was dropped (tgarr-api downtime / restart race).
        await _notify_arr(local_path, row)
    except asyncio.CancelledError:
        # User-initiated abort via /api/downloads/{id}/pause or /delete.
        # The api already flipped the row status; don't overwrite. For
        # 'cancelled' rows, sweep the partial folder so disk doesn't bloat.
        try:
            async with db_pool.acquire() as conn:
                final_status = await conn.fetchval(
                    "SELECT status FROM downloads WHERE id=$1", row["dl_id"])
            log.info("[worker] aborted id=%s final_status=%s",
                     row["dl_id"], final_status)
            if final_status == "cancelled":
                target_dir = os.path.join(
                    DOWNLOAD_ROOT,
                    SAFE_NAME.sub("_", row["rel_name"])[:160])
                if os.path.isdir(target_dir):
                    import shutil
                    shutil.rmtree(target_dir, ignore_errors=True)
                    log.info("[worker] swept cancelled folder %s", target_dir)
        except Exception:
            log.exception("[worker] cancel cleanup id=%s", row["dl_id"])
        raise  # propagate so the dispatcher's sem releases properly
    except Exception as e:
        log.exception("[worker] failed id=%s: %s", row["dl_id"], e)
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """UPDATE downloads
                       SET status='failed', finished_at=NOW(), error_message=$1
                       WHERE id=$2""",
                    str(e)[:500], row["dl_id"])
        except Exception:
            log.exception("[worker] failed to mark failed id=%s", row["dl_id"])
    finally:
        _active_tasks.pop(row["dl_id"], None)


async def _download_dispatcher(same_dc: bool, sem: asyncio.Semaphore) -> None:
    """One of two dispatcher loops. Acquires sem before claiming a row so
    the in-flight count never exceeds the pool size. Released by the
    spawned worker task on completion."""
    pool_name = "same-dc" if same_dc else "cross-dc"
    log.info("[worker] dispatcher started pool=%s cap=%s", pool_name, sem._value)
    while True:
        await sem.acquire()
        try:
            row = await _claim_pending(same_dc)
        except Exception:
            log.exception("[worker] claim error pool=%s", pool_name)
            sem.release()
            await asyncio.sleep(10)
            continue
        if not row:
            sem.release()
            await asyncio.sleep(5)
            continue
        async def _task(r=row):
            try:
                await _process_one_download(r)
            finally:
                sem.release()
        asyncio.create_task(_task())


async def download_worker() -> None:
    """Spawn two dispatcher loops: same-DC parallel (5), cross-DC serial (1).
    Kept as a single entry point so main() doesn't change much; this just
    fans out the two pools."""
    log.info("[worker] download worker root=%s my_dc=%s",
             DOWNLOAD_ROOT, CURRENT_DC)
    os.makedirs(DOWNLOAD_ROOT, exist_ok=True)
    await asyncio.gather(
        _download_dispatcher(True, _SAME_DC_SEM),
        _download_dispatcher(False, _CROSS_DC_SEM),
    )


SESSION_PATH = "/app/session/tgarr.session"
THUMBS_ROOT = "/downloads/thumbs"
PREVIEWS_ROOT = "/downloads/previews"

REGISTRY_URL = os.environ.get("TGARR_REGISTRY_URL", "https://tgarr.me").rstrip("/")
CONTRIBUTE_ENABLED = os.environ.get("TGARR_CONTRIBUTE", "true").lower() == "true"
CONTRIBUTE_INTERVAL_SEC = int(os.environ.get("TGARR_CONTRIBUTE_INTERVAL_SEC", "21600"))  # 6h


def _adaptive_sleep_seconds(pending: int) -> int:
    """5-tier backlog-aware sleep. Same curve for both contrib workers.
    Aggressive during catch-up, quiet at idle — central doesn't get spammed
    once steady-state is reached.
    """
    if pending > 5000: return 30      # burst (catch-up backlog)
    if pending > 100:  return 300     # 5 min — active ingest
    if pending > 0:    return 1800    # 30 min — trickle
    return 21600                       # 6h — heartbeat only


# ════════════════════════════════════════════════════════════════════
# Bloom-sketch handshake: client fetches central's "known-with-consensus"
# filter, locally tests each pending fuid, skips push for those already
# at consensus. Cuts redundant traffic ~99% in mature federations.
# ════════════════════════════════════════════════════════════════════
_BLOOM_BLOB: bytes = b""          # raw bit-array (m/8 bytes), header stripped
_BLOOM_M_BITS: int = 0
_BLOOM_K: int = 0
_BLOOM_ETAG: str = ""
_BLOOM_BUILT_AT: int = 0
_BLOOM_N: int = 0


async def _refresh_bloom_sketch() -> None:
    """Pull latest sketch from central. ETag-cached → 304 = no-op."""
    global _BLOOM_BLOB, _BLOOM_M_BITS, _BLOOM_K, _BLOOM_ETAG, _BLOOM_BUILT_AT, _BLOOM_N

    def _fetch():
        req = urllib.request.Request(
            f"{REGISTRY_URL}/api/v1/known_resources/sketch",
            headers={"If-None-Match": f'"{_BLOOM_ETAG}"' if _BLOOM_ETAG else "",
                     "Accept-Encoding": "gzip",
                     "User-Agent": "tgarr/bloom"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                etag = r.headers.get("etag", "").strip('"')
                raw = r.read()
                if r.headers.get("content-encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return r.status, etag, raw
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, _BLOOM_ETAG, b""
            return e.code, "", b""
        except Exception as e:
            log.warning("[bloom] fetch error: %s", e)
            return 0, "", b""

    status, etag, blob = await asyncio.to_thread(_fetch)
    if status == 304:
        return  # cached version still good
    if status != 200 or len(blob) < 20:
        log.warning("[bloom] fetch failed status=%s len=%d", status, len(blob))
        return
    # 20-byte header: magic(4s) + m_bits(I) + k(B) + reserved(3x) + n(I) + built_at(I)
    if blob[:4] != b"BLM1":
        log.warning("[bloom] bad magic: %r", blob[:4])
        return
    try:
        m_bits, k, n, built_at = struct.unpack("<IB3xII", blob[4:20])
    except struct.error as e:
        log.warning("[bloom] header parse: %s", e)
        return
    expected_size = 20 + m_bits // 8
    if len(blob) != expected_size:
        log.warning("[bloom] size mismatch: got %d expected %d", len(blob), expected_size)
        return
    _BLOOM_BLOB = blob[20:]
    _BLOOM_M_BITS = m_bits
    _BLOOM_K = k
    _BLOOM_ETAG = etag
    _BLOOM_BUILT_AT = built_at
    _BLOOM_N = n
    age = int(time.time() - built_at)
    log.info("[bloom] sketch updated: n=%d m=%d k=%d size=%dKB age=%ds",
             n, m_bits, k, len(blob)//1024, age)


def _bloom_contains(fuid: str) -> bool:
    """True if fuid is probably in central's consensus set.
    False if definitely absent (need to push) or sketch unavailable.
    Empty sketch → False everywhere → no skips → fail-safe behavior.
    """
    if not _BLOOM_BLOB or _BLOOM_M_BITS == 0 or not fuid:
        return False
    h = hashlib.sha256(fuid.encode()).digest()
    for i in range(_BLOOM_K):
        idx = int.from_bytes(h[i*4:(i+1)*4], 'little') % _BLOOM_M_BITS
        if not (_BLOOM_BLOB[idx >> 3] & (1 << (idx & 7))):
            return False
    return True
INSTANCE_UUID_ROTATE_DAYS = 7

# Federation swarm validator (v0.4.34+): client pulls seed candidates from
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
            await _mtproto_wait_clearance()
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


_TME_THUMB_RX = re.compile(
    r"https://cdn[0-9]+\.telesco\.pe/file/[A-Za-z0-9_\-]+\.jpg")

# Video src in t.me embed: <video src="https://cdn5.telesco.pe/file/xxx.mp4?token=...">
# The token is a signed URL good for plain HTTPS GET. Supports Range.
_TME_VIDEO_RX = re.compile(
    r'<video[^>]*\bsrc="(https://cdn[0-9]+\.telesco\.pe/file/[^"]+\.mp4\?token=[^"]+)"')


async def _fetch_video_url_https(username: str, tg_message_id: int) -> str | None:
    """Scrape t.me embed page → extract the signed video src URL.
    Returns the full URL (with token) or None if no video / private / scrape miss.
    """
    if not username:
        return None
    url = f"https://t.me/{username}/{tg_message_id}?embed=1&single"

    def _scrape():
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                html = r.read().decode("utf-8", errors="replace")
        except Exception:
            return None
        m = _TME_VIDEO_RX.search(html)
        return m.group(1) if m else None

    return await asyncio.to_thread(_scrape)


async def _download_video_https(video_url: str, target_path: str,
                                 expected_size: int | None = None,
                                 dl_id: int | None = None) -> int:
    """Stream-download a video URL to disk. Returns bytes written.
    Raises on HTTP error or size mismatch (when expected_size given).
    If dl_id given, updates downloads.bytes_done + speed_kbps every ~5s.
    """
    progress_state = {"last_t": time.time(), "last_b": 0, "written": 0}

    def _dl():
        req = urllib.request.Request(
            video_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=300) as r:
            with open(target_path, "wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)  # 1 MiB
                    if not chunk:
                        break
                    f.write(chunk)
                    progress_state["written"] += len(chunk)
        return progress_state["written"]

    async def _progress_pump():
        while True:
            await asyncio.sleep(5)
            now = time.time()
            cur = progress_state["written"]
            db = cur - progress_state["last_b"]
            dt = now - progress_state["last_t"]
            progress_state["last_b"] = cur
            progress_state["last_t"] = now
            if dl_id and db > 0:
                try:
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE downloads "
                            "SET bytes_done = GREATEST(COALESCE(bytes_done,0), $1), "
                            "    speed_kbps=$2, last_progress_at=NOW() WHERE id=$3",
                            int(cur), int(db / 1024 / max(dt, 0.001)), dl_id)
                except Exception:
                    pass

    pump = asyncio.create_task(_progress_pump()) if dl_id else None
    try:
        bytes_written = await asyncio.to_thread(_dl)
    finally:
        if pump:
            pump.cancel()
    if expected_size and bytes_written != expected_size:
        raise RuntimeError(
            f"size mismatch: got {bytes_written} expected {expected_size}")
    return bytes_written


_TME_PREVIEW_RX = re.compile(
    r"https://cdn[0-9]+\.telesco\.pe/file/[A-Za-z0-9_\-]+\.jpg")


async def _fetch_preview_https(username: str, tg_message_id: int) -> bytes | None:
    """Gallery slideshow preview (~76KB 450px JPEG). Same t.me embed scrape
    as thumb but takes the LAST cdn URL (background-image, bigger) instead
    of the FIRST (link_preview, smaller). HTTPS — no MTProto, no FloodWait.
    """
    if not username:
        return None
    url = f"https://t.me/{username}/{tg_message_id}?embed=1&single"

    def _scrape():
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                html = r.read().decode("utf-8", errors="replace")
        except Exception:
            return None
        if ("tgme_widget_message_photo_wrap" not in html
                and "tgme_widget_message_video_wrap" not in html):
            return None
        # ALL cdn jpg URLs in order; pick last = biggest available preview.
        urls = _TME_PREVIEW_RX.findall(html)
        if not urls:
            return None
        target = urls[-1] if len(urls) > 1 else urls[0]
        try:
            req2 = urllib.request.Request(
                target, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req2, timeout=12) as r2:
                return r2.read()
        except Exception:
            return None

    return await asyncio.to_thread(_scrape)


async def _fetch_thumb_https(username: str, tg_message_id: int) -> bytes | None:
    """Try public t.me embed → grab the small CDN thumb. No MTProto, no FloodWait.

    Returns the JPEG bytes (≤~15KB usually) or None if HTTPS path unavailable
    (private channel, deleted post, non-photo media, etc.). Falls back to
    MTProto caller-side on None.
    """
    if not username:
        return None
    url = f"https://t.me/{username}/{tg_message_id}?embed=1&single"

    def _scrape():
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                html = r.read().decode("utf-8", errors="replace")
        except Exception:
            return None
        # Skip if post has no photo/video media (deleted / text-only post →
        # embed only renders channel avatar, regex would falsely match it).
        if ("tgme_widget_message_photo_wrap" not in html
                and "tgme_widget_message_video_wrap" not in html):
            return None
        # First cdn URL in HTML = small thumb (<i src=>). background-image
        # is the larger 450px preview — skip it.
        m = _TME_THUMB_RX.search(html)
        if not m:
            return None
        try:
            req2 = urllib.request.Request(
                m.group(0), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req2, timeout=10) as r2:
                return r2.read()
        except Exception:
            return None

    return await asyncio.to_thread(_scrape)


_THUMB_HTTPS_SEM = asyncio.Semaphore(10)
_THUMB_MTPROTO_SEM = asyncio.Semaphore(1)
_THUMB_BATCH = 20


async def _thumb_process_one(row) -> None:
    """Fetch + write + md5-dedup one queued thumb row. Concurrency-safe via
    semaphores: HTTPS path runs up to 10-wide, MTProto fallback serializes.
    """
    safe_uid = SAFE_NAME.sub("_", row["file_unique_id"] or str(row["id"]))[:80]
    fname = f"{safe_uid}.jpg"
    target = os.path.join(THUMBS_ROOT, fname)
    try:
        # ── Path 1: HTTPS scrape via t.me embed (public channels only). ──
        # Avoids MTProto + cross-DC auth.ExportAuthorization FloodWait.
        https_bytes = None
        if row["channel_username"]:
            async with _THUMB_HTTPS_SEM:
                https_bytes = await _fetch_thumb_https(
                    row["channel_username"], row["tg_message_id"])
        if https_bytes:
            with open(target, "wb") as f:
                f.write(https_bytes)
        else:
            # THUMB-ONLY emergency mode: skip MTProto fallback entirely — these
            # rows just stay '__failed__' until the cross-DC FloodWait clears
            # and the mode flag is flipped off.
            if os.environ.get("TGARR_THUMB_ONLY", "false").lower() == "true":
                raise RuntimeError("THUMB_ONLY mode: HTTPS scrape miss + MTProto disabled")
            # ── Path 2: MTProto fallback through global rate limiter. ──
            async with _THUMB_MTPROTO_SEM:
                msg = await _mtproto_get_messages_with_auto_join(
                    row["tg_chat_id"], row["tg_message_id"])
                if not msg:
                    raise RuntimeError("message gone")
                thumb_media = None
                if msg.photo:
                    pthumbs = getattr(msg.photo, "thumbs", None) or []
                    thumb_media = pthumbs[-1] if pthumbs else msg.photo
                elif msg.video and getattr(msg.video, "thumbs", None):
                    thumb_media = msg.video.thumbs[-1]
                elif msg.document and getattr(msg.document, "thumbs", None):
                    thumb_media = msg.document.thumbs[-1]
                if not thumb_media:
                    raise RuntimeError("no thumbnail available on message")
                await _mtproto("download_media",
                    lambda: app.download_media(thumb_media, file_name=target))
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
        log.warning("[thumbs] flood-wait %ds (id=%s) — leaving in queue", wait, row["id"])
        await asyncio.sleep(min(wait, 3600))
        # Row stays as __user_queued__; will be retried on next batch.
    except Exception as e:
        log.warning("[thumbs] failed id=%s: %s", row["id"], e)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET thumb_path='__failed__' WHERE id=$1",
                row["id"])


_PREVIEW_SEM = asyncio.Semaphore(5)  # concurrent HTTPS fetches


async def _preview_process_one(row):
    if not row["channel_username"]:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET preview_path='__failed__' WHERE id=$1",
                row["id"])
        return
    try:
        async with _PREVIEW_SEM:
            blob = await _fetch_preview_https(
                row["channel_username"], row["tg_message_id"])
        if not blob:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE messages SET preview_path='__failed__' WHERE id=$1",
                    row["id"])
            return
        safe_uid = SAFE_NAME.sub(
            "_", row["file_unique_id"] or str(row["id"]))[:80]
        fname = f"{safe_uid}.jpg"
        target = os.path.join(PREVIEWS_ROOT, fname)
        with open(target, "wb") as f:
            f.write(blob)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET preview_path=$1 WHERE id=$2",
                fname, row["id"])
    except Exception as e:
        log.warning("[preview] failed id=%s: %s", row["id"], e)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET preview_path='__failed__' WHERE id=$1",
                row["id"])


async def preview_downloader() -> None:
    """450px+ JPEG preview fetcher — sem-capped 5 concurrent HTTPS scrapes.
    LIFO batch (newest queued first) so user's just-opened slideshow is
    prioritized over backlog. Same t.me embed source as thumb, takes the
    larger CDN URL.
    """
    log.info("[preview] dispatcher started; root=%s conc=5", PREVIEWS_ROOT)
    os.makedirs(PREVIEWS_ROOT, exist_ok=True)
    while True:
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT m.id, m.tg_message_id, m.file_unique_id,
                              c.username AS channel_username
                       FROM messages m JOIN channels c ON c.id = m.channel_id
                       WHERE m.preview_path = '__user_queued__'
                       ORDER BY m.id DESC LIMIT 20""")
            if not rows:
                await asyncio.sleep(30)
                continue
            await asyncio.gather(
                *[_preview_process_one(r) for r in rows],
                return_exceptions=True)
        except Exception:
            log.exception("[preview] outer loop")
            await asyncio.sleep(10)


async def thumb_downloader() -> None:
    """Concurrent thumb-fetch dispatcher.

    Pulls a batch of queued rows, fans out to _thumb_process_one in parallel,
    waits for the batch, repeats. HTTPS path is sem-capped at 10 concurrent;
    MTProto fallback at 1 (shared Pyrogram session).

    Eligible rows are flagged thumb_path='__user_queued__' by /api/thumb on
    UI hits (on-demand mode). No proactive scan.
    """
    log.info("[thumbs] dispatcher started; root=%s batch=%d https_conc=10 mtproto_conc=1",
             THUMBS_ROOT, _THUMB_BATCH)
    os.makedirs(THUMBS_ROOT, exist_ok=True)
    while True:
        try:
            async with db_pool.acquire() as conn:
                # LIFO: newest queue requests first. User just switched channel
                # → those rows have the biggest m.id (recently INSERTed by
                # /api/thumb's UPDATE). Drain them before old leftovers so the
                # 12s polling timeout in /api/thumb is more likely to win.
                rows = await conn.fetch(
                    """SELECT m.id, m.tg_chat_id, m.tg_message_id, m.file_unique_id,
                              c.username AS channel_username
                       FROM messages m JOIN channels c ON c.id = m.channel_id
                       WHERE m.thumb_path = '__user_queued__'
                       ORDER BY m.id DESC
                       LIMIT $1""", _THUMB_BATCH)
            if not rows:
                await asyncio.sleep(60)
                continue
            tasks = [asyncio.create_task(_thumb_process_one(r)) for r in rows]
            await asyncio.gather(*tasks, return_exceptions=True)
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
                       WHERE (account_user_id = $1 OR account_user_id IS NULL)
                         AND (meta_updated_at IS NULL
                              OR meta_updated_at < NOW() - INTERVAL '7 days')
                       ORDER BY meta_updated_at NULLS FIRST
                       LIMIT 1""", CURRENT_USER_ID)
            if not row:
                await asyncio.sleep(900)
                continue
            try:
                chat = await _mtproto("get_chat",
                    lambda: app.get_chat(row["tg_chat_id"]))
                members = getattr(chat, "members_count", None)
                category = _detect_content_category(chat.title or "", chat.username or "")
                from pyrogram import enums as _pf
                cid = row["tg_chat_id"]
                remote_msgs = await _mtproto("get_chat_history",
                    lambda: app.get_chat_history_count(cid))
                remote_photos = await _mtproto("search_messages_count",
                    lambda: app.search_messages_count(cid, filter=_pf.MessagesFilter.PHOTO))
                remote_videos = await _mtproto("search_messages_count",
                    lambda: app.search_messages_count(cid, filter=_pf.MessagesFilter.VIDEO))
                remote_audio = await _mtproto("search_messages_count",
                    lambda: app.search_messages_count(cid, filter=_pf.MessagesFilter.AUDIO))
                remote_docs = await _mtproto("search_messages_count",
                    lambda: app.search_messages_count(cid, filter=_pf.MessagesFilter.DOCUMENT))
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE channels SET members_count=$1,
                             remote_msgs=$2, remote_photos=$3, remote_videos=$4,
                             remote_audio=$5, remote_documents=$6,
                             remote_counts_refreshed_at=NOW(),
                             content_category=$7,
                             meta_updated_at=NOW()
                           WHERE tg_chat_id=$8""",
                        members, remote_msgs, remote_photos, remote_videos,
                        remote_audio, remote_docs, category, row["tg_chat_id"])
                log.info("[meta] chat_id=%s members=%s remote: msg=%s photo=%s "
                         "video=%s audio=%s doc=%s",
                         row["tg_chat_id"], members, remote_msgs, remote_photos,
                         remote_videos, remote_audio, remote_docs)
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
                msg = await _mtproto_get_messages_with_auto_join(
                    row["tg_chat_id"], row["tg_message_id"])
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
                stream_floodwait = None
                try:
                    with open(target, "wb") as f:
                        async for chunk in app.stream_media(media):
                            f.write(chunk)
                            written += len(chunk)
                except FloodWait as fw:
                    stream_floodwait = getattr(fw, "value", 60)
                    log.warning("[media-dl] FloodWait %ds id=%s mid-stream "
                                "(partial=%d bytes)",
                                stream_floodwait, row["id"], written)
                except Exception as se:
                    log.warning("[media-dl] stream error id=%s: %s",
                                row["id"], se)
                expected = row["file_size"] or 0
                if stream_floodwait or (expected and written < expected * 0.95):
                    log.warning("[media-dl] %s id=%s short/failed: "
                                "written=%d expected=%d — marking __failed__",
                                row["media_type"], row["id"], written, expected)
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE messages SET local_path = $$__failed__$$ "
                            "WHERE id=$1", row["id"])
                    if stream_floodwait:
                        await asyncio.sleep(min(stream_floodwait + 5, 1800))
                    continue
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
                         AND (account_user_id = $2 OR account_user_id IS NULL)
                         AND (last_polled_at IS NULL
                              OR last_polled_at < NOW() - $1 * INTERVAL '1 second')
                       ORDER BY last_polled_at NULLS FIRST
                       LIMIT 1""",
                    SUBSCRIBE_POLL_INTERVAL, CURRENT_USER_ID)
            if not row:
                await asyncio.sleep(60)
                continue

            uname = row["username"]
            await _heartbeat("subscription_poller", f"polling @{uname}")
            log.info("[subscribe] poll @%s (backfilled=%s)", uname, row["backfilled"])
            try:
                # resolveUsername is the most flood-prone TG call — _mtproto
                # rate-limits to 5/min globally + observes global halt.
                chat = await _mtproto("resolveUsername",
                    lambda: app.get_chat(uname))
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
                await _mtproto_wait_clearance()
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
    r"sexy|sex\b|色情|成人|18禁|裸|淫|эротик|порно|секс|اباحي|سكس|"
    # English slang
    r"milf|dilf|bdsm|fetish|kink|bondage|gangbang|blowjob|anal|deepthroat|"
    r"jav|jvid|escort|bbw|whore|slut|leaked[\s_-]*nude|"
    r"sex[\s_-]*video|porn[\s_-]*hub|pornhub|brazzers|xvideos|xnxx|r[-]?18|"
    # CJK slang
    r"swag|p站|av女优|av男优|av影片|av资源|成人影片|成人电影|成人视频|"
    r"反差|白虎|母狗|少妇|高潮|做爱|乱伦|约炮|包养|"
    r"萝莉|熟女|巨乳|自慰|口交|肛交|内射|颜射|中出|群p|调教|偷拍|露出|"
    r"黄色片|黄网|黄片|三级片|福利姬|大尺度|抠逼|操逼|大鸡巴|鸡巴|"
    r"91av|av天堂|h漫|h动画|h小说|"
    # JP/KR/AR/FA
    r"エロ|ハメ撮り|アダルト|야동|성인영상|سکس|إباحي|"
    # ES/PT
    r"desnuda|"
    # Emoji high-signal in TG channel names
    r"🔞|🍆🍑|💦💦)",
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
                await _heartbeat("seed_validator", "queue empty — sleeping 1h")
                log.info("[seed-validator] all candidates processed — sleeping 1h")
                await asyncio.sleep(3600)
                continue

            uname = row["username"]
            await _heartbeat("seed_validator", f"validating @{uname}")
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
                chat = await _mtproto("resolveUsername",
                    lambda: app.get_chat(uname))
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
                await _mtproto_wait_clearance()
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
                    headers={"User-Agent": "tgarr/0.4.34 (+https://tgarr.me)",
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
    # Anti-thundering-herd: random jitter on top of base settle delay so 1M
    # clients restarting simultaneously don't all push central in the same
    # 30s window. Base 120s + 0-600s random = first push ∈ [2min, 12min].
    initial_nap = 120 + random.uniform(0, 600)
    log.info("[federation] initial jitter sleep %.0fs", initial_nap)
    await asyncio.sleep(initial_nap)
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
                nap = _adaptive_sleep_seconds(0)
                log.info("[federation] no eligible channels — sleep %ds", nap)
                await asyncio.sleep(nap)
                continue

            # Collect pending caption-discovered seeds for upload
            pending_seeds = []
            if CONTRIBUTE_MENTIONS_ENABLED:
                async with db_pool.acquire() as conn:
                    pending_seeds = await conn.fetch(
                        "SELECT username, invite_link, source, audience_hint "
                        "FROM seed_candidates "
                        "WHERE source IN ('caption-mention', 'caption-invite') "
                        "  AND contributed_at IS NULL "
                        "ORDER BY added_at DESC "
                        "LIMIT 500")

            payload = {
                "instance_uuid": uuid_val,
                "tgarr_version": "0.4.52",
                "channels": [{
                    "username": r["username"],
                    "title": r["title"],
                    "members_count": r["members_count"],
                    "media_count": r["media_count"],
                    "audience": r["audience"] or "sfw",
                } for r in rows],
                "seed_mentions": [{
                    "username": s["username"],
                    "invite_link": s["invite_link"],
                    "source": s["source"],
                    "audience_hint": s["audience_hint"],
                } for s in pending_seeds],
            }

            try:
                body_raw = json.dumps(payload).encode()
                body_gz = gzip.compress(body_raw, compresslevel=5)
                req = urllib.request.Request(
                    REGISTRY_URL + "/api/v1/contribute",
                    data=body_gz,
                    headers={"Content-Type": "application/json",
                             "Content-Encoding": "gzip",
                             "Accept-Encoding": "gzip",
                             "User-Agent": "tgarr/0.4.62 (+https://tgarr.me)"},
                    method="POST")
                def _post():
                    with urllib.request.urlopen(req, timeout=30) as r:
                        raw = r.read()
                        if r.headers.get("content-encoding", "").lower() == "gzip":
                            raw = gzip.decompress(raw)
                        return raw
                resp = await asyncio.to_thread(_post)
                result = json.loads(resp.decode())
                log.info("[federation] contributed %s channels + %s seeds (%dKB→%dKB gzip): %s",
                         len(rows), len(pending_seeds), len(body_raw)//1024, len(body_gz)//1024, result)
                # Mark pushed seeds as contributed (only if server accepted)
                if pending_seeds and result.get("status") == "ok":
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE seed_candidates SET contributed_at=NOW() "
                            "WHERE username = ANY($1::text[])",
                            [s["username"] for s in pending_seeds])
            except Exception as e:
                log.warning("[federation] contribute call failed: %s", e)

            # adaptive: drive cadence by unpushed seed_candidates count.
            # Channel meta is idempotent UPSERT; the real "is there new info"
            # signal is fresh caption-mention seeds.
            async with db_pool.acquire() as conn:
                pending_seeds_n = await conn.fetchval(
                    """SELECT count(*) FROM seed_candidates
                       WHERE source IN ('caption-mention', 'caption-invite')
                         AND contributed_at IS NULL""")
            nap = _adaptive_sleep_seconds(pending_seeds_n or 0)
            log.info("[federation] pending_seeds=%d → sleep %ds",
                     pending_seeds_n or 0, nap)
            await asyncio.sleep(nap)
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
            await _heartbeat("federation_validator", f"fetching seed batch (size={SEEDS_BATCH})")
            # ─── 1. Fetch a batch from central ──────────────────────────
            try:
                url = f"{REGISTRY_URL}/api/v1/seeds?batch={SEEDS_BATCH}"
                req = urllib.request.Request(url, headers={
                    "User-Agent": "tgarr/0.4.34 (+https://tgarr.me)"})
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
                        chat = await _mtproto("join_chat",
                            lambda: app.join_chat(invite))
                    else:
                        chat = await _mtproto("resolveUsername",
                            lambda: app.get_chat(uname))
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
                        "tgarr_version": "0.4.34",
                        "channels": verified_alive,
                    }
                    req = urllib.request.Request(
                        REGISTRY_URL + "/api/v1/contribute",
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json",
                                 "User-Agent": "tgarr/0.4.34 (+https://tgarr.me)"},
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


async def dc_backfill_worker() -> None:
    """One-shot-ish background filler: walks messages that are referenced
    as releases.primary_msg_id but have NULL file_dc, fetches each via
    get_messages, extracts media.dc_id, UPDATEs. Slow rate to avoid
    FloodWait. Sleeps 1h when no work, exits silently on hard errors.
    """
    log.info("[dc-backfill] worker started, 1 row/sec")
    await asyncio.sleep(60)  # let startup settle
    while True:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT m.id, m.tg_chat_id, m.tg_message_id
                       FROM messages m
                       WHERE COALESCE(m.file_dc, 0) = 0
                         AND m.id IN (SELECT primary_msg_id FROM releases
                                      WHERE primary_msg_id IS NOT NULL)
                       LIMIT 1""")
            if not row:
                # No releases pending; check non-release media (lower priority)
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """SELECT id, tg_chat_id, tg_message_id
                           FROM messages
                           WHERE COALESCE(file_dc, 0) = 0
                             AND media_type IN ('audio','video','document','photo')
                           ORDER BY id DESC
                           LIMIT 1""")
            if not row:
                await asyncio.sleep(3600)
                continue
            try:
                msg = await _mtproto("get_messages",
                    lambda: app.get_messages(row["tg_chat_id"], row["tg_message_id"]))
            except FloodWait as fw:
                wait = getattr(fw, "value", 60) + 5
                log.warning("[dc-backfill] FloodWait %ds", wait)
                await asyncio.sleep(min(wait, 1800))
                continue
            except Exception as e:
                # Mark with sentinel so we don't retry forever
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE messages SET file_dc = 0 WHERE id=$1", row["id"])
                continue
            media = (msg and (msg.video or msg.audio or msg.document or msg.photo))
            dc = None
            if media:
                # Try attribute first (newer Pyrogram), then decode file_id (universal)
                dc = getattr(media, "dc_id", None)
                if not dc:
                    try:
                        dc = FileId.decode(media.file_id).dc_id
                    except Exception:
                        pass
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE messages SET file_dc=$1 WHERE id=$2",
                    dc or 0, row["id"])
                # Propagate within channel: all media in a TG channel is
                # almost always on the same DC. Saves 99% of get_messages
                # calls on the worker.
                if dc:
                    await conn.execute(
                        """UPDATE messages SET file_dc=$1
                           WHERE tg_chat_id=$2
                             AND COALESCE(file_dc, 0) <= 0""",
                        dc, row["tg_chat_id"])
            await asyncio.sleep(1)
        except Exception as e:
            log.exception("[dc-backfill] outer: %s", e)
            await asyncio.sleep(60)


async def on_demand_media_downloader() -> None:
    """Process messages marked local_path='__user_queued__' by API.

    Strictly user-driven: scans only for the queue marker, no proactive
    backlog work. Same stream_media() path as the disabled proactive
    worker, with FloodWait + short-download detection.
    """
    log.info("[on-demand-media] started — processes __user_queued__ marker")
    os.makedirs(AUDIO_ROOT, exist_ok=True)
    os.makedirs(LIBRARY_ROOT, exist_ok=True)
    os.makedirs(VIDEO_ROOT, exist_ok=True)
    while True:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT m.id, m.tg_chat_id, m.tg_message_id, m.media_type,
                           m.file_name, m.file_size
                    FROM messages m
                    WHERE m.local_path = '__user_queued__'
                    ORDER BY m.id DESC
                    LIMIT 1
                """)
            if not row:
                await asyncio.sleep(2)
                continue

            mt = row["media_type"] or "document"
            await _heartbeat("on_demand_media_downloader", f"streaming msg {row['id']} ({mt})")
            root = (AUDIO_ROOT if mt == "audio"
                    else VIDEO_ROOT if mt == "video"
                    else LIBRARY_ROOT)
            base = (row["file_name"] or f"item-{row['id']}")
            safe = SAFE_NAME.sub("_", base)[:180]
            target = os.path.join(root, safe)
            rel = os.path.relpath(target, "/downloads")

            try:
                msg = await _mtproto_get_messages_with_auto_join(
                    row["tg_chat_id"], row["tg_message_id"])
                if not msg:
                    raise RuntimeError("msg gone")
                media = (msg.audio if mt == "audio"
                         else msg.video if mt == "video"
                         else msg.document)
                if not media:
                    raise RuntimeError("no media obj")

                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE messages SET local_path=$1 WHERE id=$2",
                        rel, row["id"])
                log.info("[on-demand-media] %s id=%s → %s", mt, row["id"], rel)

                expected = row["file_size"] or 0
                written = 0
                with open(target, "wb") as f:
                    async for chunk in app.stream_media(msg):
                        f.write(chunk)
                        written += len(chunk)

                if expected and written < expected * 0.95:
                    log.warning("[on-demand-media] short download id=%s wrote=%s expected=%s",
                                row["id"], written, expected)
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE messages SET local_path='__failed__' WHERE id=$1",
                            row["id"])
                else:
                    log.info("[on-demand-media] complete id=%s wrote=%s", row["id"], written)
            except FloodWait as e:
                wait_s = getattr(e, "value", 30)
                log.warning("[on-demand-media] FloodWait %ss id=%s", wait_s, row["id"])
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE messages SET local_path='__user_queued__' WHERE id=$1",
                        row["id"])
                await asyncio.sleep(wait_s + 5)
            except Exception as e:
                log.exception("[on-demand-media] id=%s error: %s", row["id"], e)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE messages SET local_path='__failed__' WHERE id=$1",
                        row["id"])
        except Exception:
            log.exception("[on-demand-media] outer loop")
            await asyncio.sleep(10)


DEEP_BACKFILL_ENABLED = os.environ.get("TGARR_DEEP_BACKFILL", "true").lower() == "true"
DEEP_BACKFILL_PAGE_SIZE = int(os.environ.get("TGARR_DEEP_BACKFILL_PAGE_SIZE", "200"))
DEEP_BACKFILL_DELAY_SEC = int(os.environ.get("TGARR_DEEP_BACKFILL_DELAY_SEC", "10"))
# Channels with fewer than this many TOTAL media items (TG-reported) are
# skipped. Saves quota on pure-text/discussion channels.
DEEP_BACKFILL_MIN_MEDIA = int(os.environ.get("TGARR_DEEP_BACKFILL_MIN_MEDIA", "100"))


async def dialog_gc_worker() -> None:
    """Auto-unsubscribe subscribed channels that have produced no new
    messages in > TGARR_DIALOG_GC_DAYS (default 30) — they're effectively
    dead. Frees subscription_poller quota for active channels.

    No TG action needed since we never joined (subscribe = poll-only).
    Just set channels.subscribed=FALSE; poller stops touching them.
    """
    GC_DAYS = int(os.environ.get("TGARR_DIALOG_GC_DAYS", "30"))
    log.info("[dialog-gc] worker started — silent>%dd auto-unsubscribe, 6h cadence", GC_DAYS)
    await asyncio.sleep(1800)  # let initial backfill settle
    while True:
        try:
            async with db_pool.acquire() as conn:
                # last_post NULL → never had any message (likely failed-resolve / pending)
                # last_post too-old → silent → drop
                rows = await conn.fetch(f"""
                    SELECT c.id, c.username,
                           (SELECT max(posted_at) FROM messages m
                             WHERE m.channel_id = c.id) AS last_post
                    FROM channels c
                    WHERE c.subscribed = TRUE
                      AND c.last_polled_at < NOW() - INTERVAL '{GC_DAYS} days'
                """)
            dead = []
            for r in rows:
                lp = r["last_post"]
                if lp is None:
                    dead.append((r["id"], r["username"], None))
                else:
                    from datetime import datetime as _dt, timezone as _tz
                    age = (_dt.now(_tz.utc) - lp).days
                    if age > GC_DAYS:
                        dead.append((r["id"], r["username"], age))
            if dead:
                ids = [d[0] for d in dead]
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE channels SET subscribed=FALSE, "
                        "subscribe_error='auto-gc: silent >30d' "
                        "WHERE id = ANY($1::bigint[])", ids)
                for did, uname, age in dead:
                    log.info("[dialog-gc] auto-unsubscribed @%s (last msg %s days ago)",
                             uname, age if age is not None else "never")
            await _heartbeat("dialog_gc_worker",
                             f"swept {len(rows)} silent candidates, dropped {len(dead)}")
        except Exception:
            log.exception("[dialog-gc] outer")
        await asyncio.sleep(21600)  # 6h


async def decay_eviction_worker() -> None:
    """Periodic LRU-by-decay cleanup: drop messages whose value has decayed
    below pull cost. Same model as deep_backfill cutoff but operating on
    already-indexed rows. Frees disk + speeds up queries.

    Schedule: once per 6h. Per category, find rows past 3×half_life and DELETE.
    file_unique_id federation value is preserved (central already has it from
    contribute_resources push); eviction is purely local index hygiene.
    """
    await asyncio.sleep(600)  # let other workers settle
    log.info("[eviction] worker started — 6h cadence")
    while True:
        try:
            for category, half_life in _DECAY_HALF_LIFE_DAYS.items():
                if half_life is None:
                    continue
                cutoff_days = 3 * half_life
                async with db_pool.acquire() as conn:
                    # Don't delete rows that user has grabbed (local_path set)
                    # — those are local cache the user is using.
                    result = await conn.execute(
                        """DELETE FROM messages
                           WHERE channel_id IN (
                             SELECT id FROM channels WHERE content_category = $1
                           )
                           AND posted_at < NOW() - ($2 || ' days')::INTERVAL
                           AND COALESCE(local_path, '') = ''
                           AND id NOT IN (SELECT primary_msg_id FROM releases WHERE primary_msg_id IS NOT NULL)""",
                        category, str(cutoff_days))
                # asyncpg returns "DELETE N" — parse the number
                deleted = int(result.split()[-1]) if result.startswith("DELETE") else 0
                if deleted:
                    log.info("[eviction] category=%s cutoff=%dd deleted=%d",
                             category, cutoff_days, deleted)
                await _heartbeat("decay_eviction_worker",
                                 f"swept {category} >{cutoff_days}d (-{deleted})")
            # Multi-signal quality sweep — orthogonal to time decay
            async with db_pool.acquire() as conn:
                # 1) Ad-keyword file_names
                r1 = await conn.execute("""
                    DELETE FROM messages
                    WHERE file_name ~* $1
                      AND COALESCE(local_path, '') = ''
                      AND id NOT IN (SELECT primary_msg_id FROM releases WHERE primary_msg_id IS NOT NULL)
                """, r'(\bcrypto\b|\bnft\b|\busdt\b|赌博|博彩|casino|gamble|gambling|'
                     r'投资理财|做单|赚钱|月入|代理|代充|代购|刷单|'
                     r'telegram[\s_]*bot|airdrop|'
                     r'limited[\s_]*offer|click[\s_]*here|join[\s_]*now[\s_]*free|'
                     r'earn[\s_]*money|earn[\s_]*cash|vip[\s_]*会员)')
                ads_n = int(r1.split()[-1]) if r1.startswith("DELETE") else 0
                # 2) Micro-media: < 5MB video ≈ < 3min (ad/clip/news),
                # < 100KB audio / < 10KB doc / < 5KB photo = junk
                r2 = await conn.execute("""
                    DELETE FROM messages
                    WHERE (
                      (media_type='video'    AND file_size < 5000000) OR
                      (media_type='audio'    AND file_size < 100000)  OR
                      (media_type='document' AND file_size < 10000)   OR
                      (media_type='photo'    AND file_size < 5000)
                    )
                    AND COALESCE(local_path, '') = ''
                    AND id NOT IN (SELECT primary_msg_id FROM releases WHERE primary_msg_id IS NOT NULL)
                """)
                micro_n = int(r2.split()[-1]) if r2.startswith("DELETE") else 0
                # 3) Cross-channel hash spam — same thumb_md5 in 20+ channels
                r3 = await conn.execute("""
                    DELETE FROM messages
                    WHERE thumb_md5 IN (
                        SELECT thumb_md5 FROM messages
                        WHERE thumb_md5 IS NOT NULL
                        GROUP BY thumb_md5
                        HAVING count(DISTINCT channel_id) > 20
                    )
                    AND COALESCE(local_path, '') = ''
                    AND id NOT IN (SELECT primary_msg_id FROM releases WHERE primary_msg_id IS NOT NULL)
                """)
                spam_n = int(r3.split()[-1]) if r3.startswith("DELETE") else 0
            if ads_n + micro_n + spam_n:
                log.info("[eviction] quality sweep: ads=%d micro=%d cross-spam=%d",
                         ads_n, micro_n, spam_n)
                await _heartbeat("decay_eviction_worker",
                                 f"quality sweep: -{ads_n+micro_n+spam_n}")
        except Exception as e:
            log.exception("[eviction] outer loop: %s", e)
        await asyncio.sleep(21600)  # 6h


async def deep_backfill_worker() -> None:
    """Continuously pages OLDER messages past the first 5000-cap backfill.

    For each subscribed channel that's not yet deep_backfilled:
      1. Find oldest tg_message_id we have locally (or use deep_oldest_tg_id cursor)
      2. get_chat_history(offset_id=oldest, limit=DEEP_BACKFILL_PAGE_SIZE) →
         returns messages OLDER than offset
      3. Ingest each, update cursor to new oldest seen
      4. If 0 returned → bottom reached, mark deep_backfilled=TRUE
      5. Sleep DEEP_BACKFILL_DELAY_SEC between pages (paced for TG quota)

    Honors FloodWait with exponential backoff. Designed to run for days.
    """
    if not DEEP_BACKFILL_ENABLED:
        log.info("[deep-backfill] TGARR_DEEP_BACKFILL=false — disabled")
        return
    log.info("[deep-backfill] started: page_size=%d delay=%ds",
             DEEP_BACKFILL_PAGE_SIZE, DEEP_BACKFILL_DELAY_SEC)
    await asyncio.sleep(120)  # let initial backfill + subscribe settle first

    while True:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT c.id, c.tg_chat_id, c.username, c.title,
                           c.content_category,
                           COALESCE(c.deep_oldest_tg_id,
                                    (SELECT min(tg_message_id) FROM messages m
                                     WHERE m.channel_id = c.id)) AS cursor
                    FROM channels c
                    WHERE COALESCE(c.deep_backfilled, FALSE) = FALSE
                      AND c.tg_chat_id > -2000000000000  -- exclude placeholder ids
                      AND (c.account_user_id = $2 OR c.account_user_id IS NULL)
                      AND (
                        -- explicit tgarr subscription resolved via TG API
                        (c.subscribed = TRUE AND c.last_polled_at IS NOT NULL)
                        OR
                        -- channel Tom joined directly in TG (chat_id known real)
                        (c.enabled = TRUE AND c.subscribed = FALSE)
                      )
                      -- skip resource-poor channels (only if remote counts known)
                      AND NOT (
                        c.remote_counts_refreshed_at IS NOT NULL
                        AND COALESCE(c.remote_photos, 0)
                          + COALESCE(c.remote_videos, 0)
                          + COALESCE(c.remote_audio, 0)
                          + COALESCE(c.remote_documents, 0) < $1
                      )
                    -- prioritize channels with most remote media (biggest payoff first)
                    ORDER BY
                        COALESCE(c.remote_videos, 0) + COALESCE(c.remote_audio, 0)
                        + COALESCE(c.remote_documents, 0) DESC NULLS LAST,
                        COALESCE(c.deep_last_run_at, '1970-01-01'::timestamptz) ASC
                    LIMIT 1
                """, DEEP_BACKFILL_MIN_MEDIA, CURRENT_USER_ID)
            if not row:
                await _heartbeat("deep_backfill_worker", "all channels deep-backfilled")
                await asyncio.sleep(3600)  # all done, idle hourly
                continue

            uname = row["username"] or f"chat-{row['tg_chat_id']}"
            cursor = row["cursor"]
            await _heartbeat("deep_backfill_worker",
                             f"paging @{uname} older-than={cursor}")

            count_ingested = 0
            oldest_seen = cursor
            hit_cutoff = False
            # Decay-model cutoff for time-sensitive categories
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            cutoff_date = None
            cat = row["content_category"]
            if cat:
                hl = _DECAY_HALF_LIFE_DAYS.get(cat)
                if hl:
                    cutoff_date = _dt.now(_tz.utc) - _td(days=3 * hl)
            try:
                await _mtproto_wait_clearance()
                offset_kw = {"offset_id": cursor} if cursor else {}
                async for msg in app.get_chat_history(
                        row["tg_chat_id"],
                        limit=DEEP_BACKFILL_PAGE_SIZE,
                        **offset_kw):
                    # Coerce msg.date to UTC-aware if Pyrogram returned naive
                    msg_date = msg.date
                    if msg_date is not None and msg_date.tzinfo is None:
                        msg_date = msg_date.replace(tzinfo=_tz.utc)
                    # Time-decay cutoff
                    if cutoff_date and msg_date and msg_date < cutoff_date:
                        hit_cutoff = True
                        log.info("[deep-backfill] @%s decay cutoff hit at msg %s (date=%s, category=%s)",
                                 uname, msg.id, msg_date.date(), cat)
                        break
                    try:
                        await ingest_message(msg)
                        count_ingested += 1
                        if oldest_seen is None or msg.id < oldest_seen:
                            oldest_seen = msg.id
                    except Exception as e:
                        log.warning("[deep-backfill] ingest err msg %s: %s", msg.id, e)
            except FloodWait as fw:
                log.warning("[deep-backfill] FloodWait %ds on @%s — backing off",
                            fw.value, uname)
                await _heartbeat("deep_backfill_worker",
                                 f"FloodWait {fw.value}s on @{uname}",
                                 error=f"FloodWait {fw.value}s")
                await asyncio.sleep(fw.value + 5)
                continue
            except Exception as e:
                log.warning("[deep-backfill] @%s page error: %s", uname, e)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE channels SET deep_last_run_at=NOW() WHERE id=$1",
                        row["id"])
                await asyncio.sleep(30)
                continue

            log.info("[deep-backfill] @%s page done: ingested=%d oldest=%s",
                     uname, count_ingested, oldest_seen)

            # Done if hit decay cutoff OR page returned fewer than page_size (TG bottom)
            done = hit_cutoff or count_ingested < DEEP_BACKFILL_PAGE_SIZE
            async with db_pool.acquire() as conn:
                await conn.execute("""
                    UPDATE channels
                    SET deep_oldest_tg_id=$1,
                        deep_last_run_at=NOW(),
                        deep_backfilled=$2,
                        deep_total_pulled=COALESCE(deep_total_pulled,0)+$3
                    WHERE id=$4
                """, oldest_seen, done, count_ingested, row["id"])
            if done:
                log.info("[deep-backfill] @%s BOTTOM REACHED — marked done", uname)
                await _heartbeat("deep_backfill_worker",
                                 f"@{uname} deep-backfill complete")

            await asyncio.sleep(DEEP_BACKFILL_DELAY_SEC)
        except Exception:
            log.exception("[deep-backfill] outer loop")
            await asyncio.sleep(60)


CONTRIBUTE_RESOURCES_ENABLED = os.environ.get("TGARR_CONTRIBUTE_RESOURCES", "true").lower() == "true"
CONTRIBUTE_RESOURCES_BATCH = int(os.environ.get("TGARR_CONTRIBUTE_RESOURCES_BATCH", "5000"))
CONTRIBUTE_RESOURCES_INTERVAL_SEC = int(os.environ.get("TGARR_CONTRIBUTE_RESOURCES_INTERVAL_SEC", "10"))


async def contribute_resources_worker() -> None:
    """Push local file_unique_id-keyed resources to central via /api/v1/contribute_resources.

    This is the *core* federation value: each client tells central which TG
    assets (file_unique_id) it has observed. Central aggregates across clients
    → distinct_contributors per resource → swarm-verified resource registry.

    Eligibility: messages with file_unique_id NOT NULL AND contributed_at IS NULL.
    Batch: up to CONTRIBUTE_RESOURCES_BATCH per submission (server cap 5000).
    Frequency: every CONTRIBUTE_RESOURCES_INTERVAL_SEC (default 30m).
    """
    if not CONTRIBUTE_RESOURCES_ENABLED:
        log.info("[contrib-res] TGARR_CONTRIBUTE_RESOURCES=false — disabled")
        return
    log.info("[contrib-res] started; batch=%d interval=%ds",
             CONTRIBUTE_RESOURCES_BATCH, CONTRIBUTE_RESOURCES_INTERVAL_SEC)
    # Same anti-thundering-herd jitter — see contribute_to_registry rationale.
    initial_nap = 180 + random.uniform(0, 600)
    log.info("[contrib-res] initial jitter sleep %.0fs", initial_nap)
    await asyncio.sleep(initial_nap)

    while True:
        try:
            await _heartbeat("contribute_resources_worker", "scanning pending")
            async with db_pool.acquire() as conn:
                uuid_val = await conn.fetchval(
                    "SELECT value FROM config WHERE key='instance_uuid'")
                if not uuid_val:
                    log.info("[contrib-res] no instance_uuid yet, sleep 5min")
                    await asyncio.sleep(300)
                    continue
                rows = await conn.fetch(f"""
                    SELECT m.id, m.tg_message_id, m.file_unique_id, m.file_name,
                           m.file_size, m.mime_type, m.media_type,
                           m.audio_duration_sec, m.posted_at,
                           c.username AS channel_username
                    FROM messages m
                    JOIN channels c ON c.id = m.channel_id
                    WHERE m.file_unique_id IS NOT NULL
                      AND m.contributed_at IS NULL
                      AND COALESCE(c.audience, 'sfw') <> 'blocked_csam'
                    ORDER BY m.id DESC
                    LIMIT {CONTRIBUTE_RESOURCES_BATCH}
                """)
            if not rows:
                # adaptive: empty queue → long heartbeat sleep
                nap = _adaptive_sleep_seconds(0)
                await _heartbeat("contribute_resources_worker",
                                 f"queue empty; sleep {nap}s")
                await asyncio.sleep(nap)
                continue

            # ── Bloom handshake: skip fuids central already has at consensus. ──
            await _refresh_bloom_sketch()
            push_rows = []
            skipped_ids = []
            for r in rows:
                if r["file_unique_id"] and _bloom_contains(r["file_unique_id"]):
                    skipped_ids.append(r["id"])  # mark contributed without push
                else:
                    push_rows.append(r)
            if skipped_ids:
                # Skipped rows are already at consensus on central — record locally
                # so we don't keep retesting them every cycle. Saves DB scan time.
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE messages SET contributed_at=NOW() "
                        "WHERE id = ANY($1::bigint[])", skipped_ids)
                log.info("[contrib-res] bloom: skipped %d/%d (%d to push) — "
                         "central already has consensus on these",
                         len(skipped_ids), len(rows), len(push_rows))
            if not push_rows:
                # Whole batch was redundant — no HTTP call needed.
                async with db_pool.acquire() as conn:
                    pending_after = await conn.fetchval(
                        """SELECT count(*) FROM messages m
                           JOIN channels c ON c.id = m.channel_id
                           WHERE m.file_unique_id IS NOT NULL
                             AND m.contributed_at IS NULL
                             AND COALESCE(c.audience, 'sfw') <> 'blocked_csam'""")
                nap = _adaptive_sleep_seconds(pending_after or 0)
                log.info("[contrib-res] all-bloom-skipped; backlog=%d sleep %ds",
                         pending_after or 0, nap)
                await asyncio.sleep(nap)
                continue

            payload = {
                "instance_uuid": uuid_val,
                "tgarr_version": "0.4.62",
                "resources": [{
                    "file_unique_id": r["file_unique_id"],
                    "file_name": r["file_name"],
                    "channel_username": r["channel_username"],
                    "file_size": r["file_size"],
                    "mime_type": r["mime_type"],
                    "media_type": r["media_type"],
                    "duration_sec": r["audio_duration_sec"],
                    "msg_id": r["tg_message_id"],
                    "posted_at": r["posted_at"].isoformat() if r["posted_at"] else None,
                    "requires_join": True,
                } for r in push_rows],
            }

            try:
                body_raw = json.dumps(payload).encode()
                body_gz = gzip.compress(body_raw, compresslevel=5)
                req = urllib.request.Request(
                    REGISTRY_URL + "/api/v1/contribute_resources",
                    data=body_gz,
                    headers={"Content-Type": "application/json",
                             "Content-Encoding": "gzip",
                             "Accept-Encoding": "gzip",
                             "User-Agent": "tgarr/0.4.62 (+https://tgarr.me)"},
                    method="POST")
                def _post():
                    with urllib.request.urlopen(req, timeout=120) as r:
                        raw = r.read()
                        if r.headers.get("content-encoding", "").lower() == "gzip":
                            raw = gzip.decompress(raw)
                        return raw
                resp = await asyncio.to_thread(_post)
                result = json.loads(resp.decode())
                log.info("[contrib-res] pushed %d resources (%dKB→%dKB gzip): %s",
                         len(push_rows), len(body_raw)//1024, len(body_gz)//1024, result)
                if result.get("status") == "ok":
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE messages SET contributed_at=NOW() WHERE id = ANY($1::bigint[])",
                            [r["id"] for r in push_rows])
                    await _heartbeat("contribute_resources_worker",
                                     f"pushed {len(push_rows)}, skipped {len(skipped_ids)} via bloom")
            except Exception as e:
                log.warning("[contrib-res] POST failed: %s", e)
                await _heartbeat("contribute_resources_worker",
                                 f"POST failed", error=str(e)[:200])

            # adaptive: re-measure backlog after this cycle, pick interval
            async with db_pool.acquire() as conn:
                pending_after = await conn.fetchval(
                    """SELECT count(*) FROM messages m
                       JOIN channels c ON c.id = m.channel_id
                       WHERE m.file_unique_id IS NOT NULL
                         AND m.contributed_at IS NULL
                         AND COALESCE(c.audience, 'sfw') <> 'blocked_csam'""")
            nap = _adaptive_sleep_seconds(pending_after or 0)
            log.info("[contrib-res] backlog=%d → sleep %ds", pending_after or 0, nap)
            await asyncio.sleep(nap)
        except Exception:
            log.exception("[contrib-res] outer loop")
            await asyncio.sleep(600)


async def _session_change_watcher(boot_user_id: int):
    """Detect when /login QR-changes the underlying session to a different
    TG account. Crawler can't hot-swap Pyrogram client mid-flight; on change
    we self-exit and let docker's restart policy bring us back fresh.
    """
    import sqlite3
    await asyncio.sleep(60)  # initial settle
    while True:
        try:
            if os.path.exists(SESSION_PATH) and os.path.getsize(SESSION_PATH) > 0:
                con = sqlite3.connect(f"file:{SESSION_PATH}?mode=ro",
                                      uri=True, timeout=2)
                try:
                    row = con.execute(
                        "SELECT user_id FROM sessions LIMIT 1").fetchone()
                finally:
                    con.close()
                if row and row[0] and row[0] != boot_user_id:
                    log.warning(
                        "[session-watch] user_id changed %s → %s — self-exiting "
                        "for docker restart to pick up new account",
                        boot_user_id, row[0])
                    # Brief delay so log flushes
                    await asyncio.sleep(2)
                    os._exit(0)
        except Exception as e:
            log.debug("[session-watch] check err: %s", e)
        await asyncio.sleep(30)


async def main() -> None:
    global CURRENT_USER_ID, CURRENT_DC
    await init_db()
    await wait_for_session()
    await app.start()
    app.add_handler(RawUpdateHandler(on_raw_update))
    me = await app.get_me()
    CURRENT_USER_ID = me.id
    try:
        CURRENT_DC = await app.storage.dc_id()
    except Exception:
        CURRENT_DC = 0
    log.info("connected as @%s (id=%s) dc=%s",
             me.username or "-", me.id, CURRENT_DC)
    # Reset stale 'downloading' from previous crash/restart — worker is
    # single-task serial, so only one can really be in flight at a time.
    # Any leftover 'downloading' from a previous boot is dead state.
    async with db_pool.acquire() as conn:
        # Keep bytes_done so UI stays monotonic across crawler restart; new
        # Pyrogram download will overwrite via GREATEST once it surpasses
        # last-known. Only flip status + clear stale speed/timestamp.
        reset = await conn.execute(
            "UPDATE downloads SET status='pending', speed_kbps=0, "
            "last_progress_at=NULL WHERE status='downloading'")
        if reset and reset != "UPDATE 0":
            log.info("[worker] %s stale downloading → pending on boot "
                     "(bytes_done preserved)", reset)
    asyncio.create_task(_session_change_watcher(me.id))
    asyncio.create_task(_cancel_watcher())

    # THUMB-ONLY MODE: emergency lockdown when MTProto cross-DC FloodWait is
    # escalating. Runs only HTTPS-safe workers (thumb_downloader uses t.me
    # web scrape, no MTProto). Skips deep_backfill, fed-validator, channel
    # meta refresher, dialog watchers — anything that hits cross-DC auth.
    THUMB_ONLY_MODE = os.environ.get("TGARR_THUMB_ONLY", "false").lower() == "true"

    asyncio.create_task(meili_sync_worker())   # HTTPS to local Meili — safe
    asyncio.create_task(thumb_downloader())    # HTTPS-first, MTProto fallback (gated by THUMB_ONLY)
    asyncio.create_task(preview_downloader())  # HTTPS-only (450px slideshow preview)
    asyncio.create_task(download_worker())     # HTTPS-first for video, MTProto fallback (gated)

    if THUMB_ONLY_MODE:
        log.warning("[main] THUMB-ONLY MODE — skipping MTProto-heavy workers")
        log.info("[main] active: meili, thumb_downloader, download_worker (HTTPS-only paths), live listener")
        log.info("backfill skipped (THUMB-ONLY)")
        await asyncio.Event().wait()
        return

    asyncio.create_task(on_demand_media_downloader())
    asyncio.create_task(deep_backfill_worker())
    asyncio.create_task(contribute_resources_worker())
    asyncio.create_task(decay_eviction_worker())
    asyncio.create_task(dialog_gc_worker())
    # thumb_hash_backfill is proactive (scans all thumbs for md5 dedup), so it's
    # disabled to match the no-pre-download rule.
    # asyncio.create_task(thumb_hash_backfill())
    # local_media_downloader disabled 2026-05-14 — strict on-demand mode.
    # Binary content fetches only via download_worker (POST /api/grab/{guid}).
    # asyncio.create_task(local_media_downloader())
    asyncio.create_task(channel_meta_refresher())
    asyncio.create_task(new_dialog_watcher())
    asyncio.create_task(dc_backfill_worker())
    asyncio.create_task(subscription_poller())
    asyncio.create_task(contribute_to_registry())
    asyncio.create_task(federation_validator())
    asyncio.create_task(registry_puller())
    # seed_validator disabled on client: leftover from pre-federation split.

    log.info("starting backfill (limit %s/channel)...", BACKFILL_LIMIT)
    await backfill_all()
    log.info("backfill complete, switching to live listen mode")
    await asyncio.Event().wait()


if __name__ == "__main__":
    app.run(main())
