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
import sys
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
    AuthKeyUnregistered, SessionRevoked, SessionExpired,
    UserDeactivated, UserDeactivatedBan, Unauthorized,
)
from pyrogram.types import Message

from parser import (parse_filename, to_release_name, decide_release, is_video_file,
                    is_generic_name, part_number, title_from_caption, with_context)

# Pyrogram 2.0.106 hard-codes the pre-2024 channel-id range
# (MIN_CHANNEL_ID = -1002147483647). Telegram now hands out channel ids above
# 2^31, and for those every call that addresses the chat by id — get_messages,
# thumbnails, previews, downloads, backfill — died with "Peer id invalid".
# Widen the range to the current MTProto limit.
from pyrogram import utils as _pyro_utils
# (Only the original Pyrogram has this constant. Kurigram, the maintained fork
# tgarr uses now, handles the larger ids by itself and does not define it.)
if getattr(_pyro_utils, "MIN_CHANNEL_ID", None) is not None \
        and _pyro_utils.MIN_CHANNEL_ID > -1007852516352:
    _pyro_utils.MIN_CHANNEL_ID = -1007852516352

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
# Single source for the version this crawler reports to the registry.
TGARR_VERSION = "0.5.7"
TGARR_UA = f"tgarr/{TGARR_VERSION} (+https://tgarr.me)"

# ── adult (NSFW) classification ────────────────────────────────────────
# PostgreSQL regexes (used with ~*). Keep the three copies (crawler, api,
# registry) identical. A channel is NSFW when its title matches
# ADULT_TITLE_RX, or when >= 8% of its named files / captions (at least
# 20 of them, at least 5 hits) match ADULT_RX. On live data adult channels
# score 10-90% and ordinary ones 0-3%.
ADULT_RX = '(约炮|外围女|外围小|探花|口交|抽插|内射|无码|無碼|有码|啪啪|做爱|做愛|性爱|性愛|自慰|巨乳|爆乳|美乳|大奶|奶子|福利姬|淫|骚货|骚逼|骚穴|小骚|少妇|人妻|嫩穴|嫩逼|操逼|肉棒|后入|後入|颜射|潮吹|调教|援交|楼凤|包养|裸聊|裸舞|黄片|黄油|成人视频|成人影片|色情|情色|乱伦|亂倫|强奸|迷奸|偷拍|换妻|群交|约啪|反差婊|母狗|国产自拍|麻豆传媒|天美传媒|91大神|porn|xxx|onlyfans|only fans|hentai|brazzers|blowjob|creampie|milf|nsfw|18\\+|uncensored|порно|секс|эротик|хентай|سکس|سكس|پورن|بورن|ポルノ|無修正|야동)'
ADULT_TITLE_RX = '(约炮|外围女|外围小|探花|口交|抽插|内射|无码|無碼|有码|啪啪|做爱|做愛|性爱|性愛|自慰|巨乳|爆乳|美乳|大奶|奶子|福利姬|淫|骚货|骚逼|骚穴|小骚|少妇|人妻|嫩穴|嫩逼|操逼|肉棒|后入|後入|颜射|潮吹|调教|援交|楼凤|包养|裸聊|裸舞|黄片|黄油|成人视频|成人影片|色情|情色|乱伦|亂倫|强奸|迷奸|偷拍|换妻|群交|约啪|反差婊|母狗|国产自拍|麻豆传媒|天美传媒|91大神|porn|xxx|onlyfans|only fans|hentai|brazzers|blowjob|creampie|milf|nsfw|18\\+|uncensored|порно|секс|эротик|хентай|سکس|سكس|پورن|بورن|ポルノ|無修正|야동|反差|熟女|糖心|麻豆|海角|伦理片|91国产|(^|[^a-z])av([^a-z]|$)|adult|erotic)'
ADULT_MIN_FILES = 20
ADULT_MIN_HITS = 5
ADULT_MIN_RATIO = 0.08

app = Client(
    name="tgarr",
    api_id=API_ID,
    api_hash=API_HASH,
    workdir="/app/session",
)

db_pool: asyncpg.Pool = None

# ════════════════════════════════════════════════════════════════════
# Account-revoke auto-shutdown
#
# When the bound TG account is logged out from Telegram's device list,
# Pyrogram's read-only calls (get_chat_history) keep returning success
# briefly via residual auth_key — but the account considers the session
# dead. Every worker that touches TG must stop, the container must NOT
# auto-restart into the same dead state, and the UI must surface
# "re-login required" instead of silently failing.
#
# Mechanism:
#   1. /app/session/.revoked marker — written on detection, refused at boot
#   2. _mtproto() central wrapper traps AuthKeyUnregistered/SessionRevoked/
#      UserDeactivated/Unauthorized → mark + exit
#   3. _session_revoke_watcher() background coroutine pings get_me() so
#      read-only-only workloads still detect the revoke within ~60s
#   4. QR re-login (api container) deletes the marker on success
# ════════════════════════════════════════════════════════════════════
REVOKED_MARKER = "/app/session/.revoked"
_REVOKED_AUTH_ERRORS = (
    AuthKeyUnregistered, SessionRevoked, SessionExpired,
    UserDeactivated, UserDeactivatedBan, Unauthorized,
)
_REVOKED_FIRED = False


async def wipe_private_channel_data() -> dict:
    """Drop all channels with no public username (= private/invite-only) and
    everything that depends on them — messages, releases, downloads, and the
    auto_grab_log/Meili docs that follow. Returns counts for logging.

    Rationale: private channels are reachable only via MTProto from the
    account that joined them. Once that account is logged out / revoked,
    the local rows are dead pointers (no client can fetch them). Central
    federation already has the resources, so on next login the client can
    pull a fresh snapshot.

    Public channels (username IS NOT NULL) are kept — any new account can
    re-join and re-index them, and the cached browse data stays useful in
    read-only mode while logged out.
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            target_ids = [r["id"] for r in await conn.fetch(
                "SELECT id FROM channels WHERE username IS NULL")]
            if not target_ids:
                return {"channels": 0, "messages": 0, "releases": 0}
            msg_count = await conn.fetchval(
                "SELECT COUNT(*) FROM messages WHERE channel_id = ANY($1::int[])",
                target_ids)
            rel_target_q = """
                SELECT r.id FROM releases r
                JOIN messages m ON m.id = r.primary_msg_id
                WHERE m.channel_id = ANY($1::int[])"""
            # Order matters — bottom-up so FK constraints don't fire:
            # downloads → releases → monitored_movies-nullify → releases →
            # (channels cascade messages, auto_grab_log cascades from releases).
            await conn.execute(
                f"DELETE FROM downloads WHERE release_id IN ({rel_target_q})",
                target_ids)
            await conn.execute(
                f"UPDATE monitored_movies SET grabbed_release_id = NULL "
                f"WHERE grabbed_release_id IN ({rel_target_q})",
                target_ids)
            rel_count = int((await conn.execute(
                f"""DELETE FROM releases WHERE primary_msg_id IN (
                       SELECT id FROM messages
                       WHERE channel_id = ANY($1::int[]))""",
                target_ids)).split()[-1])
            await conn.execute(
                "DELETE FROM channels WHERE id = ANY($1::int[])", target_ids)
    log.warning("[wipe-private] removed %d channels + %d messages + %d releases",
                len(target_ids), msg_count, rel_count)
    # Meili will reconcile on next sync pass — its docs reference the now-gone
    # message/release ids, those become stale until meili_sync_worker prunes.
    return {"channels": len(target_ids), "messages": msg_count,
            "releases": rel_count}


async def _mark_revoked(reason: str) -> None:
    """Write .revoked marker, wipe private-channel rows (dead pointers without
    the account), and self-exit. Idempotent — first call wins. Entrypoint
    will refuse restart until QR re-login clears the marker."""
    global _REVOKED_FIRED
    if _REVOKED_FIRED:
        return
    _REVOKED_FIRED = True
    # Wipe BEFORE marker write — if wipe fails, marker still gets set so the
    # safety property (no MTProto with dead session) is preserved. If wipe
    # succeeds, dashboard drops to public-only view on next render.
    try:
        if db_pool is not None:
            counts = await wipe_private_channel_data()
            log.warning("[revoked] wiped private slice: %s", counts)
    except Exception as e:
        log.exception("[revoked] wipe failed (continuing): %s", e)
    try:
        with open(REVOKED_MARKER, "w") as f:
            f.write(f"{int(time.time())} {reason}\n")
    except Exception as e:
        log.error("[revoked] failed to write %s: %s", REVOKED_MARKER, e)
    log.warning("[revoked] TG account revoked (%s) — exiting. "
                "Re-login via /login QR to clear %s.", reason, REVOKED_MARKER)
    # Brief flush so the log line lands
    try:
        time.sleep(1)
    except Exception:
        pass
    os._exit(0)

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
    "download_thumb":    (120, 60),  # <=320px embedded thumbs, ~10 KB each
    "download_preview":  (60, 60),   # full-size photos for the lightbox
    "join_chat":         (3, 60),    # rare; TG hard-caps ~20/day
    "leave_chat":        (3, 60),
    "search_messages_count": (10, 60),
    "default":           (30, 60),   # unclassified calls
}
_MTPROTO_CALL_LOG: dict = {}  # method → list[float] of recent call timestamps

# Per-DAY ceilings for the two calls Telegram punishes hardest. The per-minute
# buckets above reset on every restart and say nothing about the daily total:
# join_chat is hard-capped around 20/day per account, username resolution
# around 150-300/day, and blowing through either costs hours-to-days of
# FloodWait on the user's own account. Counters are persisted in the config
# table so a restart (or a watchtower update) does not hand out a fresh budget.
_MTPROTO_DAILY_MAX = {
    "join_chat":       int(os.environ.get("TGARR_JOIN_DAILY_MAX", "12")),
    "resolveUsername": int(os.environ.get("TGARR_RESOLVE_DAILY_MAX", "200")),
    # Cross-DC authorizations (auth.ExportAuthorization). With cached media
    # sessions a healthy day needs a handful; this is the backstop.
    "exportAuthorization": int(os.environ.get("TGARR_EXPORT_AUTH_DAILY_MAX", "40")),
}
_MTPROTO_DAILY: dict = {}   # method → [utc_day:str, count:int]
# Who gets the allowance.
#
# Username lookups are the scarcest thing an account has (Telegram tolerates
# roughly 150-300 a day). The federation validator - work done for tgarr.me,
# not for the person running this install - was built to spend about 144 of
# the 200 a day, around the clock. What was left for the person's own
# thumbnails, films and follows ran out by the evening of an ordinary day,
# and a restart (which sent the validator straight into another batch) made
# it worse.
#
# So the allowance is split: BACKGROUND work may use at most this share of
# the day's allowance; work a person is waiting on may use all of it. And it
# is recorded who used how much, so that "where did it go" has an answer.
_MTPROTO_BG_SHARE = float(os.environ.get("TGARR_BACKGROUND_SHARE", "0.4"))
_MTPROTO_BG_HALT_UNTIL: dict = {}     # method → epoch: background callers only
_MTPROTO_DAILY_BY: dict = {}          # method → [utc_day, {caller: count}]


async def _profile_picture(me) -> None:
    """The account's own profile picture, for the side menu. Fetched once at
    start (one small file from the account's home data centre); removed if the
    account has none."""
    path = "/app/session/avatar.jpg"
    try:
        photo = getattr(me, "photo", None)
        if not photo:
            if os.path.exists(path):
                os.remove(path)
            return
        await asyncio.sleep(20)                 # let start-up traffic through first
        tmp = path + ".part"
        got = await app.download_media(photo.big_file_id, file_name=tmp)
        if got and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
            log.info("[profile] account picture saved")
    except Exception as e:
        log.info("[profile] account picture not fetched: %s", e)


def _iso_or_none(v):
    """An ISO-8601 time from the registry as a datetime; None for anything else."""
    if not isinstance(v, str) or not v:
        return None
    try:
        from datetime import datetime, timezone
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class DailyBudgetExhausted(RuntimeError):
    """Raised instead of making a call whose daily Telegram budget is spent."""


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _secs_to_utc_midnight() -> float:
    now = time.time()
    return 86400 - (now % 86400)


async def _daily_budget_take(method_name: str, user: bool = True, who: str = "") -> bool:
    """Consume one unit of today's allowance. False = none left FOR THIS
    CALLER: a background caller is refused once background work has used its
    share, while work a person is waiting on can still go ahead. Persisted,
    with a per-caller tally."""
    cap = _MTPROTO_DAILY_MAX.get(method_name)
    if not cap or cap <= 0:
        return True   # uncapped (0 or negative disables the ceiling)
    key = f"mtproto_daily:{method_name}"
    today = _utc_day()
    ent = _MTPROTO_DAILY.get(method_name)
    if ent is None:
        ent = [today, 0]
        by = [today, {}]
        try:
            if db_pool is not None:
                async with db_pool.acquire() as conn:
                    v = await conn.fetchval(
                        "SELECT value FROM config WHERE key=$1", key)
                    vb = await conn.fetchval(
                        "SELECT value FROM config WHERE key=$1", f"mtproto_daily_by:{method_name}")
                if v and ":" in v:
                    d, _, c = v.partition(":")
                    ent = [d, int(c or 0)]
                if vb and "|" in vb:
                    d, _, j = vb.partition("|")
                    by = [d, json.loads(j)]
        except Exception as e:
            log.debug("[mtproto-rl] daily budget load %s: %s", method_name, e)
        _MTPROTO_DAILY[method_name] = ent
        _MTPROTO_DAILY_BY[method_name] = by
    by = _MTPROTO_DAILY_BY.setdefault(method_name, [today, {}])
    if ent[0] != today:
        ent[0], ent[1] = today, 0
    if by[0] != today:
        by[0], by[1] = today, {}
    if ent[1] >= cap:
        return False
    if not user:
        bg_used = sum(n for k, n in by[1].items() if k.startswith("bg:"))
        if bg_used >= int(cap * _MTPROTO_BG_SHARE):
            return False
    ent[1] += 1
    tag = ("" if user else "bg:") + (who or "?")
    by[1][tag] = by[1].get(tag, 0) + 1
    try:
        if db_pool is not None:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO config (key, value) VALUES ($1, $2)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                    key, f"{ent[0]}:{ent[1]}")
                await conn.execute(
                    """INSERT INTO config (key, value) VALUES ($1, $2)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                    f"mtproto_daily_by:{method_name}", f"{by[0]}|{json.dumps(by[1])}")
    except Exception as e:
        log.debug("[mtproto-rl] daily budget save %s: %s", method_name, e)
    return True


async def _username_cached(username) -> bool:
    """Does the session already know this @username? Then looking it up costs
    Telegram nothing, and must not be charged to the day's allowance (Pyrogram
    keeps a username for eight hours)."""
    if not username or not isinstance(username, str):
        return False
    try:
        await app.storage.get_peer_by_username(username.lstrip("@").lower())
        return True
    except Exception:
        return False


async def _mtproto(method_name: str, coro_factory, wait: bool = True,
                   user: "bool | None" = None, username=None):
    """Gate Pyrogram calls: per-method rate limit + per-method halt + FloodWait observation.
    Halt is per-method: TG flood-waits specific endpoints (e.g. resolveUsername)
    rather than the whole session, so other methods stay unblocked.

    wait=False is for work a person is waiting on (a thumbnail, a preview, a
    film they opened): a method that is parked - by a flood wait or because
    its daily allowance is spent - raises DailyBudgetExhausted at once instead
    of sleeping. A background job can sit out twelve hours; the thumbnail
    dispatcher sleeping twelve hours inside one channel's lookup meant no
    thumbnail at all, for anything, until midnight.

    user: is a person waiting on this? Defaults to "yes if we may not wait".
    Background work only gets its share of a daily allowance (see
    _MTPROTO_BG_SHARE). username: for resolveUsername, the name being looked
    up - if the session still knows it the call is free and is not charged.
    """
    if user is None:
        # joining only ever happens because someone opened a file
        user = (not wait) or method_name == "join_chat"
    who = sys._getframe(1).f_code.co_name
    if method_name == "resolveUsername" and username and await _username_cached(username):
        method_name = "get_chat"          # answered from the session: no lookup happens
    while True:
        # 1. Honor THIS method's halt (set by prior FloodWait on same method).
        now = time.time()
        halt_until = _MTPROTO_HALT_UNTIL.get(method_name, 0)
        if not user:
            halt_until = max(halt_until, _MTPROTO_BG_HALT_UNTIL.get(method_name, 0))
        if halt_until > now:
            remaining = halt_until - now
            if not wait and remaining > 3:
                raise DailyBudgetExhausted(
                    f"{method_name} is paused for another {int(remaining)}s")
            log.info("[mtproto-rl] halt %s: %ds remaining → sleeping",
                     method_name, int(remaining))
            await asyncio.sleep(min(remaining + 1, 60))
            continue
        # 1b. Daily ceiling (persisted). join_chat fails fast — it sits on the
        # user-click download path, so say so instead of hanging until
        # midnight. Username resolution is background-only: park the method
        # until the UTC day rolls over, exactly like a FloodWait halt.
        if method_name in _MTPROTO_DAILY_MAX and not await _daily_budget_take(
                method_name, user=user, who=who):
            cap = _MTPROTO_DAILY_MAX[method_name]
            _spent = (_MTPROTO_DAILY.get(method_name) or ["", 0])[1] >= cap
            if not _spent:
                # only the background share is used up: park background
                # callers, leave the rest for the person
                _park = _secs_to_utc_midnight() + 5
                async with _MTPROTO_LOCK:
                    _MTPROTO_BG_HALT_UNTIL[method_name] = time.time() + _park
                log.info("[mtproto-rl] %s: background work has used its share of today's "
                         "allowance (%d of %d) — background paused %dh%02dm, the rest is kept "
                         "for you", method_name, int(cap * _MTPROTO_BG_SHARE), cap,
                         int(_park // 3600), int(_park % 3600 // 60))
                continue
            if method_name == "join_chat":
                log.warning("[mtproto-rl] join_chat daily budget (%d) spent — refusing", cap)
                raise DailyBudgetExhausted(
                    f"daily join budget ({cap}) reached — protecting the Telegram "
                    f"account; try again after 00:00 UTC")
            _park = _secs_to_utc_midnight() + 5
            async with _MTPROTO_LOCK:
                _MTPROTO_HALT_UNTIL[method_name] = max(
                    _MTPROTO_HALT_UNTIL.get(method_name, 0), time.time() + _park)
            log.warning("[mtproto-rl] %s daily budget (%d) spent — parked %dh%02dm",
                        method_name, cap, int(_park // 3600), int(_park % 3600 // 60))
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
    except _REVOKED_AUTH_ERRORS as e:
        # Account revoked / session killed / user deactivated. Terminal — no
        # retry can recover. Write marker + exit so docker doesn't loop us
        # back into the same dead state. UI prompts re-login via /login.
        await _mark_revoked(type(e).__name__)
        raise
    except FloodWait as fw:
        fw_val = int(getattr(fw, "value", 60) or 60)
        until = time.time() + fw_val + 5
        async with _MTPROTO_LOCK:
            if until > _MTPROTO_HALT_UNTIL.get(method_name, 0):
                _MTPROTO_HALT_UNTIL[method_name] = until
        log.warning("[mtproto-rl] %s FloodWait %ds → halt this method only",
                    method_name, fw_val)
        await _note_flood(method_name, until)
        raise


async def _note_flood(what: str, until: float) -> None:
    """Publish "Telegram asked us to wait" so the UI can say so truthfully
    instead of showing a generic failure. Best effort."""
    try:
        if db_pool is None:
            return
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO config (key, value) VALUES ('tg_flood', $1)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                   WHERE split_part(config.value, '|', 1)::float < split_part(EXCLUDED.value, '|', 1)::float""",
                f"{int(until)}|{what[:60]}")
    except Exception as e:
        log.debug("[mtproto-rl] note flood: %s", e)


# ── cross-DC authorization: the scarce call ───────────────────────────
# Reading a file that lives on another DC needs auth.ExportAuthorization.
# Telegram tolerates only a few of these in a short window (a few dozen in
# twenty minutes earns a ~30 minute FLOOD_WAIT for the whole account, during
# which nothing off the home DC can be fetched). Every path that can cause
# one — our cached media sessions AND Pyrogram's own downloader — must first
# reserve it here. Reservation never waits: when the budget is spent or a
# flood wait is active it raises FloodWait at once, callers keep the request
# queued, and nothing is sent to Telegram.
EXPORT_AUTH_MAX = int(os.environ.get("TGARR_EXPORT_AUTH_MAX", "4"))
EXPORT_AUTH_WINDOW_SEC = int(os.environ.get("TGARR_EXPORT_AUTH_WINDOW_SEC", "600"))
_export_auth_log: list = []


async def _export_auth_reserve(dc_id: int) -> None:
    now = time.time()
    left = max(_media_dc_blocked_until.get(dc_id, 0),
               _MTPROTO_HALT_UNTIL.get("exportAuthorization", 0)) - now
    if left > 0:
        raise FloodWait(value=int(left) + 1)
    _export_auth_log[:] = [t for t in _export_auth_log if now - t < EXPORT_AUTH_WINDOW_SEC]
    if len(_export_auth_log) >= EXPORT_AUTH_MAX:
        wait = int(EXPORT_AUTH_WINDOW_SEC - (now - _export_auth_log[0])) + 1
        log.info("[export-auth] local budget (%d per %ds) spent — holding cross-DC "
                 "work %ds rather than asking Telegram", EXPORT_AUTH_MAX,
                 EXPORT_AUTH_WINDOW_SEC, wait)
        raise FloodWait(value=wait)
    if not await _daily_budget_take("exportAuthorization"):
        wait = int(_secs_to_utc_midnight()) + 5
        _MTPROTO_HALT_UNTIL["exportAuthorization"] = now + wait
        await _note_flood("daily cross-DC authorization budget", now + wait)
        raise FloodWait(value=wait)
    _export_auth_log.append(now)


async def _export_auth_flooded(dc_id: int, wait: int, source: str) -> None:
    """Telegram refused a cross-DC authorization: park all cross-DC work."""
    until = time.time() + wait + 5
    _media_dc_blocked_until[dc_id] = until
    if until > _MTPROTO_HALT_UNTIL.get("exportAuthorization", 0):
        _MTPROTO_HALT_UNTIL["exportAuthorization"] = until
    log.warning("[export-auth] FLOOD_WAIT %ds on cross-DC authorization (%s, DC%s) — "
                "all cross-DC fetches parked until it clears", wait, source, dc_id)
    await _note_flood("cross-DC authorization", until)


def _media_dc(media) -> int:
    try:
        return FileId.decode(media.file_id).dc_id
    except Exception:
        return 0


async def _pyrogram_transfer_guard(media) -> int:
    """Call before handing a file to Pyrogram's own download/stream. Pyrogram
    authorizes cross-DC by itself, per file, and swallows the FloodWait (the
    transfer just yields nothing), so the reservation has to happen here.
    Returns the file's DC (0 = home DC / unknown, nothing reserved)."""
    dc = _media_dc(media)
    if dc and CURRENT_DC and dc != CURRENT_DC:
        await _export_auth_reserve(dc)
        return dc
    return 0


async def _pyrogram_transfer_empty(dc: int) -> None:
    """A cross-DC Pyrogram transfer produced nothing. The usual cause is a
    swallowed FLOOD_WAIT on the authorization; assume so and back off rather
    than trying the next file."""
    if dc:
        await _export_auth_flooded(dc, 900, "empty Pyrogram transfer")


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


async def _mtproto_aiter(method_name: str, agen):
    """Drive a Pyrogram async generator (get_chat_history / get_dialogs) under
    the same FloodWait discipline as _mtproto(): wait out an existing halt
    before the first page, and if Telegram answers a later page with FloodWait,
    REGISTER the halt before re-raising. Without this the generators bypassed
    the limiter entirely — a FloodWait on one channel was swallowed as a
    generic error and the caller moved straight on to hit the next channel.
    """
    await _mtproto_wait_clearance(method_name)
    try:
        async for item in agen:
            yield item
    except _REVOKED_AUTH_ERRORS as e:
        await _mark_revoked(type(e).__name__)
        raise
    except FloodWait as fw:
        fw_val = int(getattr(fw, "value", 60) or 60)
        until = time.time() + fw_val + 5
        async with _MTPROTO_LOCK:
            if until > _MTPROTO_HALT_UNTIL.get(method_name, 0):
                _MTPROTO_HALT_UNTIL[method_name] = until
        log.warning("[mtproto-rl] %s FloodWait %ds mid-iteration → halt this method",
                    method_name, fw_val)
        raise


def _hist(*a, **kw):
    """app.get_chat_history under the limiter."""
    return _mtproto_aiter("get_chat_history", app.get_chat_history(*a, **kw))


def _dialogs(*a, **kw):
    """app.get_dialogs under the limiter."""
    return _mtproto_aiter("get_dialogs", app.get_dialogs(*a, **kw))


# ═════════════════════════════════════════════════
# HTTP first
#
# Following a public channel used to start with a username lookup through
# the account. An account gets about 200 of those a day before Telegram
# makes it wait, so following a few hundred channels - or restarting on a
# day the allowance was spent - left channels unread until the next day.
#
# Almost nothing about READING a public channel needs the account:
#   1. tgarr.me already holds the file list of every channel some install
#      has indexed: one HTTPS request returns it, file identities included.
#   2. Telegram's own web preview (t.me/s/<name>) shows a public channel's
#      posts to anybody: text, file names and sizes, running times.
# Both are tried first, for every followed channel the session holds no
# handle for. The account is asked only
#   * when a file is actually wanted (played, saved, grabbed): one lookup
#     for that channel, once - after it the channel is read through the
#     account like before, which costs no further lookups;
#   * for a channel neither source knows (Telegram does not preview most
#     channels that carry films, nor any group).
# TGARR_WEB_FIRST=false restores the old behaviour.
# ═════════════════════════════════════════════════
WEB_FIRST = os.environ.get("TGARR_WEB_FIRST", "true").lower() != "false"
WEB_PAGE_DELAY_SEC = float(os.environ.get("TGARR_WEB_PAGE_DELAY_SEC", "1.5"))
WEB_PAGES_PER_POLL = int(os.environ.get("TGARR_WEB_PAGES_PER_POLL", "10"))
WEB_BACKFILL_POSTS = int(os.environ.get("TGARR_WEB_BACKFILL_POSTS", "3000"))
_web_lock = asyncio.Lock()
_web_last = [0.0]
_web_slow_until = [0.0]


async def _web_page(username: str, before=None):
    """One preview page, never faster than one request per WEB_PAGE_DELAY_SEC
    for the whole install, and not at all for ten minutes after the site
    asked us to slow down."""
    import tme
    if time.time() < _web_slow_until[0]:
        return "slowdown", ""
    async with _web_lock:
        gap = WEB_PAGE_DELAY_SEC - (time.time() - _web_last[0])
        if gap > 0:
            await asyncio.sleep(gap)
        state, body = await asyncio.to_thread(tme.fetch_page, username, before)
        _web_last[0] = time.time()
    if state == "slowdown":
        _web_slow_until[0] = time.time() + 600
    return state, body


async def _web_ingest(channel_id: int, tg_chat_id: int, username: str, posts: list) -> int:
    """Store what the page shows. One row per attachment (an album is several
    messages). Rows are marked via_web: their file identity is not known yet
    and is filled in when the account first reads the message."""
    new = 0
    async with db_pool.acquire() as conn:
        for post in posts:
            when = post.get("date")
            if not when:
                continue
            text = (post.get("text") or "").replace("\x00", "")[:1024] or None
            for it in post.get("items") or []:
                kind = it["kind"]
                fname = (it.get("file_name") or "").replace("\x00", "") or None
                keep, _why = _quality_check(fname, it.get("file_size"), kind, text)
                if not keep:
                    continue
                new_id = await conn.fetchval(
                    """INSERT INTO messages
                         (channel_id, tg_message_id, tg_chat_id, file_name, caption, file_size,
                          media_type, posted_at, detected_lang, duration_sec, width, height,
                          audio_duration_sec, via_web)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,TRUE)
                       ON CONFLICT (channel_id, tg_message_id) DO NOTHING
                       RETURNING id""",
                    channel_id, it["id"], tg_chat_id, fname, text, it.get("file_size"), kind, when,
                    _detect_lang(fname or ""),
                    it.get("duration") if kind == "video" else None,
                    it.get("width"), it.get("height"),
                    it.get("duration") if kind == "audio" else None)
                if not new_id:
                    continue
                new += 1
                if fname:
                    try:
                        await maybe_create_release(conn, new_id, fname, it.get("file_size"), when,
                                                   duration=it.get("duration"),
                                                   width=it.get("width"), height=it.get("height"))
                    except Exception as e:
                        log.warning("[web] release for msg %s failed: %s", new_id, e)
                _meili_enqueue("messages", {
                    "id": new_id, "file_name": fname, "audio_title": None, "audio_performer": None,
                    "audio_canonical_title": None, "detected_lang": _detect_lang(fname or "") or "unknown",
                    "media_type": kind, "channel_username": username, "audience": "sfw",
                    "has_local": False, "file_size": it.get("file_size") or 0,
                    "posted_at_ts": int(when.timestamp())})
    return new


async def _web_read(row) -> tuple:
    """Read a channel's web preview: the newest page, then older pages where
    the last visit stopped. Returns (state, new_rows, info). state is 'ok',
    'nopreview', 'slowdown' or 'error'."""
    import tme
    uname = row["username"]
    state, body = await _web_page(uname)
    if state != "ok":
        return state, 0, {}
    page = tme.parse_page(body)
    info = {"title": page["title"], "members": page["members"]}
    new = await _web_ingest(row["id"], row["tg_chat_id"], uname, page["posts"])
    async with db_pool.acquire() as conn:
        cur = await conn.fetchrow(
            "SELECT web_cursor, web_done, (SELECT count(*) FROM messages m WHERE m.channel_id = c.id "
            "AND m.via_web) AS n FROM channels c WHERE c.id = $1", row["id"])
    cursor, done = cur["web_cursor"], cur["web_done"]
    if cursor is None:
        cursor = page["before"]
    pages = 0
    while not done and cursor and pages < WEB_PAGES_PER_POLL and (cur["n"] + new) < WEB_BACKFILL_POSTS:
        st, body = await _web_page(uname, before=cursor)
        if st != "ok":
            if st == "nopreview":
                done = True                      # walked off the top of the history
            break
        older = tme.parse_page(body)
        if not older["posts"]:
            done = True
            break
        new += await _web_ingest(row["id"], row["tg_chat_id"], uname, older["posts"])
        nxt = older["before"]
        if not nxt or nxt >= cursor:
            done = True
            break
        cursor = nxt
        pages += 1
    if (cur["n"] + new) >= WEB_BACKFILL_POSTS:
        done = True
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE channels SET web_cursor = $2, web_done = $3 WHERE id = $1",
                           row["id"], cursor, bool(done))
    return "ok", new, info


def _web_channel_card(username: str) -> dict:
    """Title and size of a public channel from t.me/<name> - a page that
    exists even for channels Telegram does not preview. Blocking."""
    import html as _h
    import urllib.request
    import tme
    try:
        req = urllib.request.Request(f"https://t.me/{username}", headers={"User-Agent": tme.UA,
                                                                          "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(512 * 1024).decode("utf-8", "replace")
    except Exception:
        return {}
    out = {}
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', body)
    if m and m.group(1).strip() and not m.group(1).startswith("Telegram: "):
        out["title"] = _h.unescape(m.group(1)).strip()
    m = re.search(r'<div class="tgme_page_extra">([^<]*?)(?:subscribers?|members?)', body)
    if m:
        out["members"] = tme.parse_count(m.group(1).split(",")[0])
    return out


async def _poll_without_account(row) -> bool:
    """Serve one followed channel from tgarr.me and the web preview.

    True  = done for this round, the account was not needed.
    False = neither source can read it: the caller goes on to the account.
    """
    uname = row["username"]
    cid = row["id"]
    central = 0
    async with db_pool.acquire() as conn:
        pulled = await conn.fetchval("SELECT central_pulled_at FROM channels WHERE id = $1", cid)
    if not pulled:
        central = await _rehydrate_channel(cid, row["tg_chat_id"], uname)
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE channels SET central_pulled_at = NOW() WHERE id = $1", cid)
    state, new, info = await _web_read(row)
    async with db_pool.acquire() as conn:
        have = await conn.fetchval("SELECT count(*) FROM messages WHERE channel_id = $1", cid)
        placeholder_title = (row["title"] or "").startswith("@") and "(resolving" in (row["title"] or "")
        if state == "ok":
            await conn.execute(
                """UPDATE channels SET web_preview = TRUE, last_polled_at = NOW(), subscribe_error = NULL,
                       backfilled = TRUE,
                       title = CASE WHEN $2::text IS NOT NULL AND ($4 OR title IS NULL) THEN $2 ELSE title END,
                       members_count = COALESCE($3, members_count)
                   WHERE id = $1""", cid, info.get("title"), info.get("members"), placeholder_title)
            log.info("[web] @%s read without the account: %d new from the web preview%s", uname, new,
                     f", {central} from tgarr.me" if central else "")
            return True
        if state in ("slowdown", "error"):
            # a hiccup of the web page is not a reason to spend a lookup
            await conn.execute(
                "UPDATE channels SET last_polled_at = NOW() WHERE id = $1", cid)
            log.info("[web] @%s: preview not reachable right now (%s) - tried again next round", uname, state)
            return True
        # Telegram does not preview this channel
        await conn.execute("UPDATE channels SET web_preview = FALSE WHERE id = $1", cid)
    if placeholder_title:
        card = await asyncio.to_thread(_web_channel_card, uname)
        if card.get("title"):
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE channels SET title = $2, members_count = COALESCE($3, members_count) WHERE id = $1",
                    cid, card["title"], card.get("members"))
    if have:
        # Its files are listed (from tgarr.me) and can be played - the lookup
        # happens when one is. Reading NEW posts of a channel without a web
        # preview does need the account: let background allowance do that,
        # never the share kept for what the user asks for.
        log.info("[web] @%s has no web preview; %d files known from tgarr.me - "
                 "the account is asked when one is played, or when background allowance is free", uname, have)
    return False


async def _adopt_real_chat_id(channel_id: int, real_chat_id: int) -> int:
    """The account has resolved a channel that so far was only known by name:
    give the channel row (and its messages) the real chat id. If the real
    channel is already in the table (the account is a member of it), the rows
    read from the web move over to it. Returns the id of the surviving row."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            other = await conn.fetchval(
                "SELECT id FROM channels WHERE tg_chat_id = $1 AND id <> $2", real_chat_id, channel_id)
            if other:
                await conn.execute(
                    """UPDATE messages m SET channel_id = $2, tg_chat_id = $3
                       WHERE m.channel_id = $1 AND NOT EXISTS (
                           SELECT 1 FROM messages o WHERE o.channel_id = $2
                             AND o.tg_message_id = m.tg_message_id)""", channel_id, other, real_chat_id)
                await conn.execute("DELETE FROM channels WHERE id = $1", channel_id)
                await conn.execute(
                    "UPDATE channels SET subscribed = TRUE, handle_known = TRUE WHERE id = $1", other)
                return other
            await conn.execute(
                "UPDATE channels SET tg_chat_id = $2, handle_known = TRUE WHERE id = $1",
                channel_id, real_chat_id)
            await conn.execute(
                "UPDATE messages SET tg_chat_id = $2 WHERE channel_id = $1 AND tg_chat_id <> $2",
                channel_id, real_chat_id)
    return channel_id


async def _ensure_handle(chat_id: int) -> int:
    """A file is wanted from a channel read so far without the account: this
    is the moment the lookup is spent (once per channel). Returns the chat id
    to use from here on."""
    async with db_pool.acquire() as conn:
        ch = await conn.fetchrow(
            "SELECT id, username FROM channels WHERE tg_chat_id = $1 AND handle_known = FALSE", chat_id)
    if not ch or not ch["username"]:
        return chat_id
    uname = ch["username"]
    chat = await _mtproto("resolveUsername", lambda: app.get_chat(uname),
                          wait=False, user=True, username=uname)
    await _adopt_real_chat_id(ch["id"], chat.id)
    log.info("[web] @%s: a file was asked for - looked the channel up once, it is read through "
             "the account from now on", uname)
    return chat.id


async def _fill_from_message(msg) -> None:
    """A row that came from the web page (or from tgarr.me) meets the real
    message: take the file's identity, exact size and data centre from it."""
    media = msg.video or msg.document or msg.audio or msg.photo
    if not media or not getattr(msg, "chat", None):
        return
    try:
        dc = FileId.decode(media.file_id).dc_id
    except Exception:
        dc = None
    kind = ("video" if msg.video else "document" if msg.document else "audio" if msg.audio else "photo")
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE messages m SET
                   file_unique_id = COALESCE(m.file_unique_id, $3),
                   file_name = COALESCE($4, m.file_name),
                   file_size = COALESCE($5, m.file_size),
                   mime_type = COALESCE($6, m.mime_type),
                   media_type = $7,
                   file_dc = COALESCE($8, m.file_dc),
                   rel_ver = CASE WHEN m.via_web THEN NULL ELSE m.rel_ver END,
                   via_web = FALSE
               FROM channels c
               WHERE c.id = m.channel_id AND c.tg_chat_id = $1 AND m.tg_message_id = $2
                 AND (m.via_web OR m.file_unique_id IS NULL OR m.file_dc IS NULL)""",
            msg.chat.id, msg.id, getattr(media, "file_unique_id", None),
            (getattr(media, "file_name", None) or "").replace("\x00", "") or None,
            getattr(media, "file_size", None), getattr(media, "mime_type", None), kind, dc)


async def _mtproto_get_messages_with_auto_join(chat_id, msg_id):
    """get_messages with auto-join on CHANNEL_INVALID — for restricted channels
    where the session isn't yet a member. join_chat is itself rate-limited;
    if join also fails, propagates the original error to caller.
    """
    from pyrogram.errors import ChannelInvalid
    chat_id = await _ensure_handle(chat_id)
    try:
        _m = await _mtproto("get_messages",
                            lambda: app.get_messages(chat_id, msg_id))
        if _m and not isinstance(_m, list):
            try:
                await _fill_from_message(_m)
            except Exception as _e:
                log.debug("[web] fill from message failed: %s", _e)
        return _m
    except ChannelInvalid:
        # First try a refresh via get_chat_history (cheap, often fixes stale
        # access_hash). NOTE: get_chat_history is an async generator — can't
        # be wrapped in `await coro()`. Use clearance gate + iterate directly.
        try:
            await _mtproto_wait_clearance("get_chat_history")
            async for _ in _hist(chat_id, limit=1):
                break
            return await _mtproto("get_messages",
                                   lambda: app.get_messages(chat_id, msg_id))
        except ChannelInvalid:
            pass
        # A PUBLIC channel can be read without being a member. If we know its
        # username, resolve it (one lookup, then cached in the session) and
        # retry before spending a join — joins are capped at a handful per
        # day and clutter the user's own Telegram.
        try:
            async with db_pool.acquire() as conn:
                _uname = await conn.fetchval(
                    "SELECT username FROM channels WHERE tg_chat_id=$1", chat_id)
            if _uname:
                await _mtproto("resolveUsername", lambda: app.resolve_peer(_uname), wait=False,
                               username=_uname)
                return await _mtproto("get_messages",
                                      lambda: app.get_messages(chat_id, msg_id))
        except FloodWait:
            raise
        except Exception as e:
            log.info("[mtproto] read-without-join failed for %s (%s) — will try join",
                     chat_id, str(e)[:80])
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
        # Mark as tgarr-auto-joined so dialog_leave_worker may release it later
        # (vs Tom's personal joins, which stay auto_joined NULL = protected).
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE channels SET auto_joined=TRUE WHERE tg_chat_id=$1", chat_id)
        except Exception:
            pass
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
#
# Two-tier regex: CORE keywords apply to every media type; PHOTO keywords
# only apply when media_type='photo'. Photo keywords ("引流/招代理/推广/
# qrcode/微信号") legitimately appear in Chinese marketing/business EPUBs
# in book-archive channels and would false-positive on those if applied to
# documents.
#
# IMPORTANT: keep both regexes in lockstep with the inlined PG patterns in
# decay_eviction_worker (l.~3030). If you add a keyword here, add it there.
_AD_KEYWORDS_CORE = re.compile(
    r"(\bcrypto\b|\bnft\b|\busdt\b|赌博|赌场|博彩|casino|gamble|gambling|"
    r"投资理财|做单|赚钱|月入|代理|代充|代购|刷单|"
    r"telegram[\s_]*bot|airdrop|"
    r"加[\s_]*我[\s_]*(微信|tg|telegram|wx)|私[\s_]*聊|"
    r"limited[\s_]*offer|click[\s_]*here|join[\s_]*now[\s_]*free|"
    r"earn[\s_]*money|earn[\s_]*cash|"
    r"vip[\s_]*会员|赚钱机会|高佣金)",
    re.IGNORECASE)

_AD_KEYWORDS_PHOTO = re.compile(
    r"(qrcode|qr[\s_]*code|二维码|"
    r"微信号|wechat[\s_]*id|加微信|加[\s_]*wx|"
    r"contact[\s_]*us|联系我|联系方式|"
    r"招代理|招商|推广|引流|福利群)",
    re.IGNORECASE)

_MIN_SIZES = {
    # 5MB video proxy for ~3min @ 720p — sub-3min videos are usually
    # ads, teasers, news clips, social-media filler. Real movies/episodes
    # always cross this floor (movie min ~80-200MB; TV ep ~30-300MB).
    'video':    5_000_000,
    'audio':    100_000,      # < 100KB audio → voice note / clip
    'document': 10_000,       # < 10KB document → just text junk
    # 10KB photo baseline = JPEG below this is icon/logo/QR code. Per-channel
    # tier (members_count) bumps this floor higher in the eviction sweep:
    # tiny<500 → 50KB, small<5k → 30KB, mid+ → 10KB (this baseline).
    'photo':    10_000,
}


def _quality_check(file_name: str | None, file_size: int | None,
                   media_type: str | None, caption: str | None) -> tuple[bool, str | None]:
    """Multi-signal quality gate. Returns (keep?, reject_reason_if_drop).

    Composite scoring: any single hard signal → reject. Cheap O(1) checks only.
    file_name + caption are scanned with the same regex — captions land
    populated since v0.4.66 (commit 71d22a7), so this gate now catches
    promo-text on photo posts that previously slipped through (file_name on
    Telegram photos is typically a generic `photo_NNNN.jpg`).
    """
    blob = (file_name or "") + " " + (caption or "")
    if blob.strip():
        if _AD_KEYWORDS_CORE.search(blob):
            return False, "ad-keyword"
        if media_type == "photo" and _AD_KEYWORDS_PHOTO.search(blob):
            return False, "ad-keyword-photo"
    floor = _MIN_SIZES.get(media_type)
    if floor and file_size and file_size < floor:
        return False, f"micro-{media_type}"
    return True, None


# Hard-reject news/TV channels: they pass the media-density filter (every post
# is a video clip) but every clip is unique to that one channel, so the
# resulting file_unique_ids are singletons — federation/dedup value = 0.
# _CATEGORY_HINTS above only LABELS news for faster decay; this gate REJECTS
# outright before backfill. Strong-signal keywords only ("TV station",
# "Телеканал", "تلویزیون", "电视台" — not soft hits like "Daily" or "Live").
_NOISE_TITLE_RX = re.compile(
    r"(?:^|[\s\-\|\:_·•/【\(\[])"
    r"(?:"
    r"TV\d*|News\s+(?:Channel|Network|Agency)|Broadcast(?:ing)?|"  # English
    r"Телеканал|телевиден|тв[\s\-]*канал|СМИ|"                    # Russian
    r"تلویزیون|شبکه|"                                              # Persian
    r"تلفزيون|البث|قناة\s*إخبارية|"                                # Arabic
    r"电视台|新闻台"                                                # Chinese
    r")"
    r"(?:$|[\s\-\|\:_·•/】\)\]])",
    re.IGNORECASE | re.UNICODE,
)

# Username-side check. Titles like "Iran International" don't carry strong
# signals but the @username (IranintlTV, ManotoTV, tvrain, smi_*) does.
# Conservative: require ≥2 leading letters before "tv" to avoid noise on
# unrelated names; require "news" / "live" as a discrete segment.
_NOISE_USERNAME_RX = re.compile(
    r"(?:"
    r"[a-z]{2,}tv\d*$|"               # ...TV at end: iranintltv, manototv
    r"^tv[a-z]|"                       # ...starts: tvrain, tvchannel
    r"(?:^|_)(?:news|live|smi)(?:$|_|\d)|"  # _news_ / _live_ / _smi_ segments
    r"telekanal|telecast|telenews"
    r")",
    re.IGNORECASE,
)

# Service-spam / fan-club / sticker channels behave exactly like news/TV for
# federation purposes: their media are singleton hashes (escort-ad photos,
# fan-cam clips, sticker packs) shared by no one else → dedup/federation value
# = 0, but they post at high volume and dominate the index. Strong, unambiguous
# title signals only — must not catch real resource channels (incl. NSFW ones
# that carry actual video files, e.g. "强奸乱伦系列", which produce releases).
_JUNK_INTENT_RX = re.compile(
    r"(包养|学生妹|押金[\d\s]*[uU](?:sdt)?|外围(?:女|资源)|楼凤|"   # escort / sugar-baby ads
    r"球迷(?:群|频道|站|圈)|هواداران|"                              # sports fan clubs (هواداران = Persian "fans")
    r"мемач|\bмемы\b|стикер[ыа]?\b|sticker\s*pack|"                # meme / sticker-pack channels
    r"新闻|新聞|新闻台|новости|новини|новостей)",                   # generic news (singleton clips, fed value 0)
    re.IGNORECASE | re.UNICODE,
)


def _is_noise_title(title: str | None, username: str | None = None) -> tuple[bool, str | None]:
    """Returns (is_noise, matched_keyword). Checks title (multi-lang strong
    news/TV keywords) and username (camelCase TV suffix, news/live segments)."""
    if title:
        m = _NOISE_TITLE_RX.search(title)
        if m:
            return True, m.group(0).strip()
    if username:
        m = _NOISE_USERNAME_RX.search(username)
        if m:
            return True, f"@{username}:{m.group(0)}"
    if title:
        m = _JUNK_INTENT_RX.search(title)
        if m:
            return True, m.group(0).strip()
    return False, None


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
    # The database may still be starting, and on a first start the tables are
    # created by the API container, which comes up at the same moment: wait
    # for both instead of dying on "relation does not exist".
    for _i in range(90):
        try:
            db_pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=4)
            break
        except (OSError, asyncpg.PostgresError) as e:
            if _i == 89:
                raise
            if _i % 10 == 0:
                log.info("[boot] waiting for the database (%s)", type(e).__name__)
            await asyncio.sleep(2)
    for _i in range(90):
        try:
            async with db_pool.acquire() as conn:
                if await conn.fetchval(
                        "SELECT to_regclass('public.messages') IS NOT NULL "
                        "AND to_regclass('public.config') IS NOT NULL") and await conn.fetchval(
                        "SELECT 1 FROM config WHERE key = 'schema_ready'"):
                    break
        except asyncpg.PostgresError:
            pass
        if _i % 10 == 0:
            log.info("[boot] waiting for the API to prepare the database")
        await asyncio.sleep(2)
    else:
        log.warning("[boot] the API has not reported the database ready after 3 min — going on")
    # Crawl-and-release columns (also created by api _migrate_schema; ensure
    # here so crawler workers can query them regardless of migration order).
    async with db_pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE worker_status ADD COLUMN IF NOT EXISTS quiet_for_s INTEGER NOT NULL DEFAULT 0")
        await conn.execute(
            "ALTER TABLE channels ADD COLUMN IF NOT EXISTS unfollowed_at TIMESTAMPTZ")
        await conn.execute(
            "ALTER TABLE channels ADD COLUMN IF NOT EXISTS deep_retry_after TIMESTAMPTZ")
        # HTTP first (see _poll_without_account)
        for _ddl in (
                "ALTER TABLE channels ADD COLUMN IF NOT EXISTS handle_known BOOLEAN NOT NULL DEFAULT TRUE",
                "ALTER TABLE channels ADD COLUMN IF NOT EXISTS web_preview BOOLEAN",
                "ALTER TABLE channels ADD COLUMN IF NOT EXISTS web_cursor BIGINT",
                "ALTER TABLE channels ADD COLUMN IF NOT EXISTS web_done BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE channels ADD COLUMN IF NOT EXISTS central_pulled_at TIMESTAMPTZ",
                "ALTER TABLE messages ADD COLUMN IF NOT EXISTS via_web BOOLEAN NOT NULL DEFAULT FALSE"):
            await conn.execute(_ddl)
        # rows followed before this version and never resolved: known by name only
        await conn.execute(
            """UPDATE channels SET handle_known = FALSE
               WHERE handle_known AND title LIKE '@%' AND title LIKE '%(resolving%'""")
        await conn.execute(
            "ALTER TABLE discovered ADD COLUMN IF NOT EXISTS last_file_at TIMESTAMPTZ")
        # Channels marked "read to the end" by the old error handling without
        # one page ever having been read (see deep_backfill_worker): ask again.
        # A channel that really is at its end costs one more page and is marked
        # done properly; the others finally get their history.
        try:
            if not await conn.fetchval(
                    "SELECT 1 FROM config WHERE key = 'deep_done_recheck_0_5_7'"):
                res = await conn.execute(
                    """UPDATE channels SET deep_backfilled = FALSE
                       WHERE deep_backfilled = TRUE AND deep_oldest_tg_id IS NULL
                         AND (subscribed = TRUE OR is_joined = TRUE)
                         AND dead_at IS NULL""")
                log.info("[deep-backfill] one-time recheck of channels marked done "
                         "without a page read: %s", res)
                await conn.execute(
                    "INSERT INTO config (key, value) VALUES ('deep_done_recheck_0_5_7', 'done') "
                    "ON CONFLICT (key) DO NOTHING")
        except asyncpg.PostgresError as e:
            log.warning("[deep-backfill] one-time recheck skipped: %s", e)
        # Every job waits a while after a start before its first round (up to
        # ~15 min, on purpose, so installs do not all act at once). Until then
        # its last report is from before the restart; the System page showed
        # those jobs as failed after every update. Say what is true instead:
        # starting, expected back within half an hour. Jobs not heard from in
        # a week belong to an older version and are left alone.
        await conn.execute(
            """UPDATE worker_status SET last_seen = NOW(), last_action = 'starting', quiet_for_s = 1800
               WHERE last_seen > NOW() - INTERVAL '7 days'""")
        await conn.execute(
            "ALTER TABLE channels ADD COLUMN IF NOT EXISTS auto_joined BOOLEAN")
        await conn.execute(
            "ALTER TABLE channels ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ")
        await conn.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS thumb_accessed_at TIMESTAMPTZ")
        await conn.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS preview_accessed_at TIMESTAMPTZ")
        # Stale-index cleanup bookkeeping (see stale_index_sweeper).
        await conn.execute(
            "ALTER TABLE channels ADD COLUMN IF NOT EXISTS dead_at TIMESTAMPTZ")
        await conn.execute(
            "ALTER TABLE channels ADD COLUMN IF NOT EXISTS dead_reason TEXT")
        await conn.execute(
            "ALTER TABLE channels ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ")
        await conn.execute(
            "ALTER TABLE channels ADD COLUMN IF NOT EXISTS verify_cursor BIGINT")
        # Release re-judgement bookkeeping (see release_judge_worker).
        await conn.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS rel_ver SMALLINT")
        await conn.execute(
            "ALTER TABLE releases ADD COLUMN IF NOT EXISTS part SMALLINT")
        await conn.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS fetch_wanted_at TIMESTAMPTZ")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_rejudge ON messages (id) "
            "WHERE rel_ver IS NULL AND media_type IN ('video', 'document')")
        await conn.execute(
            "UPDATE messages SET rel_ver = NULL WHERE rel_ver IS NOT NULL AND rel_ver <> $1",
            RELEASE_JUDGE_VER)
        # Thumbnails and previews that were really the channel avatar (see
        # _tme_media_url): one stored picture standing in for several
        # different files. Forget them; they are fetched again, correctly,
        # the next time they are looked at.
        if not await conn.fetchval(
                "SELECT 1 FROM config WHERE key = 'thumb_avatar_fix_0_5_6'"):
            for col, folder in (("thumb_path", THUMBS_ROOT), ("preview_path", PREVIEWS_ROOT)):
                try:
                    bad = [r[0] for r in await conn.fetch(
                        f"""SELECT {col} FROM messages
                            WHERE {col} IS NOT NULL AND left({col}, 2) <> '__'
                              AND file_unique_id IS NOT NULL
                            GROUP BY {col} HAVING count(DISTINCT file_unique_id) >= 3""")]
                    if not bad:
                        continue
                    res = await conn.execute(
                        f"UPDATE messages SET {col} = NULL"
                        + (", thumb_md5 = NULL" if col == "thumb_path" else "")
                        + f" WHERE {col} = ANY($1::text[])", bad)
                    for name in bad:
                        try:
                            os.remove(os.path.join(folder, name))
                        except OSError:
                            pass
                    log.info("[thumbs] %d wrong %s picture(s) forgotten (%s): they will be "
                             "fetched again", len(bad), col.split("_")[0], res)
                except Exception as e:
                    log.warning("[thumbs] repair of %s failed: %s", col, e)
            await conn.execute(
                "INSERT INTO config (key, value) VALUES ('thumb_avatar_fix_0_5_6', 'done') "
                "ON CONFLICT (key) DO NOTHING")
        # What Telegram reports for a video message: running time + frame size.
        for _col in ("duration_sec", "width", "height"):
            await conn.execute(
                f"ALTER TABLE messages ADD COLUMN IF NOT EXISTS {_col} INTEGER")
        # Existing rows were indexed without them. The stale-index sweeper
        # already re-reads every indexed message in batches; restart its walk
        # once so it fills these in as it goes (no extra Telegram calls).
        if not await conn.fetchval(
                "SELECT 1 FROM config WHERE key='dims_backfill_0_5_6'"):
            await conn.execute(
                "UPDATE channels SET verified_at = NULL, verify_cursor = 0")
            await conn.execute(
                "INSERT INTO config (key, value) VALUES ('dims_backfill_0_5_6', 'started') "
                "ON CONFLICT (key) DO NOTHING")
        # Content profile the registry sends for Discover ranking.
        for _col in ("release_files", "video_files", "book_files", "audio_files"):
            await conn.execute(
                f"ALTER TABLE IF EXISTS discovered ADD COLUMN IF NOT EXISTS {_col} INTEGER")
    log.info("postgres pool ready")


async def _heartbeat(worker: str, action: str = "", error: str | None = None,
                     quiet_for: int = 0) -> None:
    """Write worker heartbeat into worker_status table.
    Call from inside each worker's main loop iteration.

    quiet_for: the job has nothing to do and will not report again for this
    many seconds. The System page then knows silence is expected - it used
    to show such a job in red, as if it had died.
    """
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE worker_status SET quiet_for_s = $2 WHERE worker = $1 "
                "AND quiet_for_s IS DISTINCT FROM $2", worker, int(quiet_for))
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


# ════════════════════════════════════════════════════════
# Release re-judgement
#
# A release is a verdict about a file, made once, when the file was indexed:
# with the parser of that day, without the running time (filled in later by
# the sweeper), and without the poster that carries the title when the file
# itself is called 01_0.mp4 (during a backfill the poster is indexed AFTER
# the file). Verdicts therefore go stale: "2026" and "迅雷影音" as film titles,
# five-minute clips as movies, real films with no release at all.
#
# This worker goes through every indexed video/document once per rule version
# (and again whenever something a verdict depends on changes: the running
# time arrives, a titled poster appears right before the file) and makes the
# release agree with the current rules: create, correct, or remove.
# Pure database work: no Telegram calls, nothing is downloaded, nothing is
# auto-grabbed. A release someone has downloaded or grabbed is never removed.
# ════════════════════════════════════════════════════════
RELEASE_JUDGE_VER = 1            # bump when the rules change: everything is re-judged
RELEASE_JUDGE_BATCH = 300
CONTEXT_WINDOW = 12              # messages back from a file to look for its poster


async def _context_title(conn, m):
    """(ctx, part, parts_total) for a file whose own name says nothing, from
    the caption that states the title: its own, or the poster's right before
    it. Strict on purpose - a wrong title is worse than none:
      - every message between the poster and the file must be there (a gap
        is a message we do not have: possibly the next title) and must be a
        nameless file or an uncaptioned photo;
      - file counters must rise (01, 02, 03): a restart is another work.
    """
    own = title_from_caption(m["caption"])
    my_part = part_number(m["file_name"])
    mid = m["tg_message_id"]
    around = {r["tg_message_id"]: r for r in await conn.fetch(
        """SELECT tg_message_id, media_type, file_name, caption FROM messages
           WHERE channel_id = $1 AND tg_message_id BETWEEN $2 AND $3""",
        m["channel_id"], mid - CONTEXT_WINDOW, mid + CONTEXT_WINDOW)}
    ctx, anchor = own, mid
    if not own:
        if m["caption"]:
            return None                    # it has a caption, and it states no title
        last = my_part
        i = mid - 1
        ctx = None
        while i >= mid - CONTEXT_WINDOW:
            r = around.get(i)
            if r is None:
                return None
            if r["caption"]:
                ctx = title_from_caption(r["caption"])
                anchor = i
                break
            if r["media_type"] != "photo":
                if not is_generic_name(r["file_name"]):
                    return None
                pn = part_number(r["file_name"])
                if pn is not None:
                    if last is not None and pn >= last:
                        return None
                    last = pn
            i -= 1
        if not ctx:
            return None
    # how many numbered files does this title have?
    total, lastp, j = 0, 0, anchor + (0 if own else 1)
    while j in around and j <= mid + CONTEXT_WINDOW:
        r = around[j]
        if j != anchor and (r["caption"] or (r["media_type"] != "photo"
                                             and not is_generic_name(r["file_name"]))):
            break
        pn = part_number(r["file_name"]) if r["media_type"] != "photo" else None
        if pn is not None:
            if pn <= lastp:
                break
            lastp = pn
            total += 1
        j += 1
    return ctx, my_part, total


def _release_row(parsed: dict, verdict: dict, file_name) -> dict | None:
    """The releases columns a verdict implies; None if it yields no name."""
    type_ = verdict["type"]
    p = dict(parsed)
    if verdict["quality"] and not p.get("quality"):
        p["quality"] = verdict["quality"]
    p["type"] = type_
    name = to_release_name(p, fallback=file_name or "")
    if not name:
        return None
    return {
        "name": name, "category": type_,
        "series_title": p.get("title") if type_ == "tv" else None,
        "season": p.get("season") if type_ == "tv" else None,
        "episode": p.get("episode") if type_ == "tv" else None,
        "movie_title": p.get("title") if type_ == "movie" else None,
        "movie_year": p.get("year") if type_ == "movie" else None,
        "quality": p.get("quality"), "source": p.get("source"), "codec": p.get("codec"),
        "parse_score": verdict["score"], "part": p.get("part"),
    }


_REL_IDENTITY = ("category", "series_title", "season", "episode", "movie_title",
                 "movie_year", "part")
_REL_COLS = ("name", "category", "series_title", "season", "episode", "movie_title",
             "movie_year", "quality", "source", "codec", "parse_score", "part")


def _meili_release_doc(rel_id, row, m):
    return {
        "id": rel_id, "name": row["name"], "canonical_title": row["name"],
        "series_title": row["series_title"], "movie_title": row["movie_title"],
        "category": row["category"], "quality": row["quality"] or "",
        "movie_year": row["movie_year"] or 0, "season": row["season"] or 0,
        "episode": row["episode"] or 0, "parse_score": float(row["parse_score"] or 0),
        "grab_count": 0, "size_bytes": m["file_size"] or 0,
        "posted_at_ts": int(m["posted_at"].timestamp()) if m["posted_at"] else 0,
        "audience": "sfw",
    }


async def _judge_message(conn, m, gone: list) -> str:
    """Make the release for one indexed file agree with the current rules.
    Returns what happened: created / updated / removed / same / none / kept."""
    fn = m["file_name"]
    row = None
    if is_video_file(fn, m["mime_type"], m["media_type"]):
        parsed = parse_filename(fn or "")
        if is_generic_name(fn) or not parsed.get("has_title"):
            c = await _context_title(conn, m)
            if c:
                parsed = with_context(parsed, c[0], c[1], c[2])
        verdict = decide_release(parsed, file_size=m["file_size"], duration=m["duration_sec"],
                                 width=m["width"], height=m["height"],
                                 min_score=PARSE_SCORE_MIN,
                                 min_movie_bytes=MIN_MOVIE_BYTES,
                                 min_tv_bytes=MIN_TV_BYTES)
        if verdict["create"]:
            row = _release_row(parsed, verdict, fn)
    rel_id = m["rel_id"]
    if row is None:
        if not rel_id:
            return "none"
        if m["protected"]:
            return "kept"
        await conn.execute("DELETE FROM releases WHERE id = $1", rel_id)
        gone.append(rel_id)
        return "removed"
    if rel_id:
        def _same(k):
            a, b = m["r_" + k], row[k]
            if k == "parse_score":
                return abs(float(a or 0) - float(b or 0)) < 0.005
            return a == b
        if all(_same(k) for k in _REL_COLS):
            return "same"
        reset = any(not _same(k) for k in _REL_IDENTITY)
        await conn.execute(
            """UPDATE releases SET name=$2, category=$3, series_title=$4, season=$5,
                      episode=$6, movie_title=$7, movie_year=$8, quality=$9, source=$10,
                      codec=$11, parse_score=$12, part=$13"""
            + (""", canonical_title=NULL, poster_url=NULL, overview=NULL,
                      metadata_source=NULL, metadata_lookup_at=NULL""" if reset else "")
            + " WHERE id=$1", rel_id, *[row[k] for k in _REL_COLS])
        _meili_enqueue("releases", _meili_release_doc(rel_id, row, m))
        return "updated"
    # no release yet: one per file
    if m["file_unique_id"] and await conn.fetchval(
            """SELECT 1 FROM releases r JOIN messages x ON x.id = r.primary_msg_id
               WHERE x.file_unique_id = $1 AND x.id <> $2 LIMIT 1""",
            m["file_unique_id"], m["id"]):
        return "none"
    new_id = await conn.fetchval(
        """INSERT INTO releases
             (name, category, series_title, season, episode, movie_title, movie_year,
              quality, source, codec, parse_score, part, size_bytes, posted_at, primary_msg_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING id""",
        *[row[k] for k in _REL_COLS], m["file_size"], m["posted_at"], m["id"])
    _meili_enqueue("releases", _meili_release_doc(new_id, row, m))
    return "created"


async def release_judge_worker():
    await asyncio.sleep(90)
    log.info("[judge] release re-judgement started (rules v%d)", RELEASE_JUDGE_VER)
    totals: dict = {}
    while True:
        try:
            gone: list = []
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT m.id, m.channel_id, m.tg_message_id, m.media_type, m.mime_type,
                              m.file_name, m.caption, m.file_size, m.posted_at,
                              m.file_unique_id, m.duration_sec, m.width, m.height,
                              r.id AS rel_id, r.name AS r_name, r.category AS r_category,
                              r.series_title AS r_series_title, r.season AS r_season,
                              r.episode AS r_episode, r.movie_title AS r_movie_title,
                              r.movie_year AS r_movie_year, r.quality AS r_quality,
                              r.source AS r_source, r.codec AS r_codec,
                              r.parse_score AS r_parse_score, r.part AS r_part,
                              (r.id IS NOT NULL AND (
                                   COALESCE(r.grab_count, 0) > 0
                                OR EXISTS (SELECT 1 FROM downloads d WHERE d.release_id = r.id)
                                OR EXISTS (SELECT 1 FROM monitored_movies mm
                                           WHERE mm.grabbed_release_id = r.id))) AS protected
                       FROM (SELECT * FROM messages
                             WHERE rel_ver IS NULL AND media_type IN ('video', 'document')
                               AND indexed_at < NOW() - INTERVAL '2 minutes'
                             ORDER BY id LIMIT $1) m
                       LEFT JOIN releases r ON r.primary_msg_id = m.id
                       ORDER BY m.id""", RELEASE_JUDGE_BATCH)
                if not rows:
                    if totals:
                        log.info("[judge] caught up: %s", totals)
                        totals = {}
                    await _heartbeat("release_judge_worker", "idle")
                    await asyncio.sleep(60)
                    continue
                counts: dict = {}
                async with conn.transaction():
                    for m in rows:
                        try:
                            async with conn.transaction():
                                what = await _judge_message(conn, m, gone)
                        except Exception as e:
                            what = "error"
                            log.warning("[judge] msg %s: %s", m["id"], e)
                        counts[what] = counts.get(what, 0) + 1
                    await conn.execute(
                        "UPDATE messages SET rel_ver = $1 WHERE id = ANY($2::bigint[])",
                        RELEASE_JUDGE_VER, list({m["id"] for m in rows}))
            if gone:
                await _meili_delete("releases", gone)
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v
            await _heartbeat("release_judge_worker",
                             ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
            await asyncio.sleep(1 if len(rows) >= RELEASE_JUDGE_BATCH else 30)
        except Exception as e:
            log.exception("[judge] loop")
            await _heartbeat("release_judge_worker", "error", error=str(e))
            await asyncio.sleep(60)


MIN_MOVIE_BYTES = 100 * 1024 * 1024   # 100 MB — anything smaller is sample/trailer/junk
MIN_TV_BYTES = 30 * 1024 * 1024       # 30 MB — short TV ep can be 50MB at 480p


async def maybe_create_release(conn, msg_row_id, file_name, file_size, posted_at,
                               duration=None, width=None, height=None):
    """Parse the file name and insert a release row if it qualifies. The
    decision (see parser.decide_release) also weighs the running time and
    frame size Telegram reported, when it did."""
    parsed = parse_filename(file_name or "")
    verdict = decide_release(parsed, file_size=file_size, duration=duration,
                             width=width, height=height,
                             min_score=PARSE_SCORE_MIN,
                             min_movie_bytes=MIN_MOVIE_BYTES,
                             min_tv_bytes=MIN_TV_BYTES)
    if not verdict["create"]:
        return None
    # One release per file: if this exact file (file_unique_id) already has a
    # release through another post, this post is just another source for it.
    if await conn.fetchval(
            """SELECT 1 FROM releases r JOIN messages m ON m.id = r.primary_msg_id
               WHERE m.file_unique_id = (SELECT file_unique_id FROM messages WHERE id = $1)
                 AND m.file_unique_id IS NOT NULL AND m.id <> $1
               LIMIT 1""", msg_row_id):
        return None
    score = verdict["score"]
    type_ = verdict["type"]
    if verdict["quality"] and not parsed.get("quality"):
        parsed["quality"] = verdict["quality"]
    parsed["type"] = type_
    rname = to_release_name(parsed, fallback=file_name or "")
    if not rname:
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


async def ingest_message(msg: Message, live: bool = False) -> bool:
    """Persist one message + maybe create release. Returns True if new row.

    `live=True` means the message came via on_message handler — TG only
    delivers updates for chats the account is a member of, so we can mark
    the channel as is_joined=TRUE.

    `live=False` (default) is for backfill/poll-history paths. Those can
    target channels the account has never joined (e.g. public-channel
    subscriptions via @username). Gated: only proceed if chat is already
    in the channels table as is_joined=TRUE OR subscribed=TRUE — never
    let a non-joined non-subscribed channel pollute the local DB.
    """
    chat_type = msg.chat.type.name
    if chat_type in ("PRIVATE", "BOT"):
        return False

    chat_id = msg.chat.id
    msg_id = msg.id

    if not live:
        async with db_pool.acquire() as conn:
            allowed = await conn.fetchval(
                """SELECT 1 FROM channels
                   WHERE tg_chat_id=$1
                     AND (is_joined=TRUE OR subscribed=TRUE)""",
                chat_id)
        if not allowed:
            return False

    file_name = None
    file_size = None
    mime_type = None
    file_unique_id = None
    media_type = None
    audio_title = None
    audio_performer = None
    audio_duration_sec = None
    duration_sec = width = height = None

    # Identify the media kind explicitly so /gallery can find photos cleanly.
    if msg.video:
        media, media_type = msg.video, "video"
        duration_sec = getattr(msg.video, "duration", None) or None
        width = getattr(msg.video, "width", None) or None
        height = getattr(msg.video, "height", None) or None
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
        if file_name:
            file_name = file_name.replace("\x00", "")   # NUL breaks the INSERT
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
        # is_joined=TRUE only when message arrived via live update (on_message)
        # — TG only delivers those for chats the account is a member of.
        # Non-live (backfill/poll) inserts come for already-known rows so the
        # existing is_joined value is preserved by the COALESCE pattern.
        ch_id = await conn.fetchval(
            """INSERT INTO channels (tg_chat_id, username, title, category,
                                     account_user_id, is_joined)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (tg_chat_id) DO UPDATE
                 SET username = COALESCE(NULLIF(EXCLUDED.username, ''), channels.username),
                     title = COALESCE(NULLIF(EXCLUDED.title, ''), channels.title),
                     account_user_id = EXCLUDED.account_user_id,
                     is_joined = channels.is_joined OR EXCLUDED.is_joined
               RETURNING id""",
            chat_id,
            msg.chat.username,
            msg.chat.title or "",
            chat_type.lower(),
            CURRENT_USER_ID or None,
            live,
        )
        # === Caption-mention extraction (always; contribute gated separately) ===
        # Even text-only messages can contain @mention / invite links — those
        # are pure discovery signal independent of media payload.
        # Only from PUBLIC channels: links people share in a private group or a
        # personal chat are not ours to forward to the registry.
        if caption and getattr(msg.chat, "username", None):
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
        # Caption = the human-written description of the resource (title,
        # synopsis, episode list, source, language). API detail-viewer renders
        # it; extract_catalog_mentions parses it; search joins on it. Cap at
        # 1024 — Telegram’s own media-caption limit.
        # Detect language from file_name first (release names are more reliable).
        detected_lang = _detect_lang(file_name) or _detect_lang(msg.chat.title or "")
        new_msg_id = await conn.fetchval(
            """INSERT INTO messages
                 (channel_id, tg_message_id, tg_chat_id,
                  file_unique_id, file_name, caption, file_size, mime_type,
                  media_type, audio_title, audio_performer, audio_duration_sec,
                  posted_at, file_dc, detected_lang, duration_sec, width, height)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
               ON CONFLICT (channel_id, tg_message_id) DO UPDATE SET
                   file_unique_id = EXCLUDED.file_unique_id,
                   file_name = COALESCE(EXCLUDED.file_name, messages.file_name),
                   file_size = COALESCE(EXCLUDED.file_size, messages.file_size),
                   mime_type = EXCLUDED.mime_type, media_type = EXCLUDED.media_type,
                   file_dc = EXCLUDED.file_dc, tg_chat_id = EXCLUDED.tg_chat_id,
                   duration_sec = COALESCE(EXCLUDED.duration_sec, messages.duration_sec),
                   width = COALESCE(EXCLUDED.width, messages.width),
                   height = COALESCE(EXCLUDED.height, messages.height),
                   audio_title = EXCLUDED.audio_title, audio_performer = EXCLUDED.audio_performer,
                   audio_duration_sec = COALESCE(EXCLUDED.audio_duration_sec, messages.audio_duration_sec),
                   rel_ver = NULL, via_web = FALSE
                 WHERE messages.via_web AND EXCLUDED.file_unique_id IS NOT NULL
               RETURNING CASE WHEN xmax = 0 THEN id END""",
            ch_id, msg_id, chat_id, file_unique_id, file_name,
            (caption.replace("\x00", "")[:1024] or None), file_size, mime_type, media_type,
            audio_title, audio_performer, audio_duration_sec, msg.date,
            file_dc, detected_lang, duration_sec, width, height,
        )

        if new_msg_id and caption and title_from_caption(caption):
            # a titled poster: the nameless files right after it get judged again
            await conn.execute(
                """UPDATE messages SET rel_ver = NULL
                   WHERE channel_id = $1 AND tg_message_id > $2
                     AND tg_message_id <= $2 + $3 AND rel_ver IS NOT NULL""",
                ch_id, msg_id, CONTEXT_WINDOW)

        if new_msg_id and file_name:
            try:
                await maybe_create_release(conn, new_msg_id, file_name, file_size, msg.date,
                                           duration=duration_sec, width=width, height=height)
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
    inserted = await ingest_message(msg, live=True)
    if inserted and msg.chat.type.name != "PRIVATE":
        media = msg.video or msg.document or msg.audio or msg.photo
        file_info = (getattr(media, "file_name", "") or "") if media else ""
        caption = (msg.caption or msg.text or "")[:80]
        log.info("[live] ch=%s msg=%s file=%s text=%s",
                 msg.chat.id, msg.id, file_info, caption)


async def backfill_channel(chat_id: int, title: str, username: str | None = None) -> int:
    """Backfill recent history with sample-then-decide noise filter.

    After NOISE_SAMPLE_AT messages, if media density < NOISE_THRESHOLD_PCT,
    mark channel as noise + abort remaining backfill. ~96% TG quota savings
    on metaindex / chat channels.
    """
    NOISE_SAMPLE_AT = 200
    NOISE_THRESHOLD_PCT = 5
    log.info("[backfill] start chat_id=%s title=%s limit=%s",
             chat_id, title, BACKFILL_LIMIT)
    # Title/username gate: reject news/TV up front (see _is_noise_title).
    async with db_pool.acquire() as conn:
        _followed = await conn.fetchval(
            "SELECT COALESCE(subscribed, FALSE) FROM channels WHERE tg_chat_id = $1", chat_id)
    noise_hit, noise_kw = (False, None) if _followed else _is_noise_title(title, username)
    if noise_hit:
        log.info("[backfill] chat_id=%s NOISE-TITLE kw=%r title=%s — abort",
                 chat_id, noise_kw, title)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """UPDATE channels
                   SET enabled=FALSE, category='noise',
                       content_category='noise', backfilled=TRUE
                   WHERE tg_chat_id=$1""", chat_id)
        return 0
    count = 0
    media_count = 0
    await _mtproto_wait_clearance()
    async for msg in _hist(chat_id, limit=BACKFILL_LIMIT):
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
    async for dialog in _dialogs():
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
            await backfill_channel(chat_id, title, dialog.chat.username)
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
        # api listens on 8088 internally (host port 8765 is publish-only).
        api_url = os.environ.get("TGARR_API_INTERNAL_URL",
                                 "http://tgarr-api:8088")
        req = urllib.request.Request(
            f"{api_url}/api/internal/arr-scan",
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
            _media = msg.video or msg.document or msg.audio
            _safe = os.path.basename(
                (getattr(_media, "file_name", None) or "").replace("\\", "/")
            ).strip().lstrip(".")
            if _safe:
                os.makedirs(target_dir, exist_ok=True)
                _final = os.path.join(target_dir, _safe)
                _part = _final + ".part"
                _total = int(getattr(_media, "file_size", 0) or 0)

                async def _download():
                    done = 0
                    try:
                        with open(_part, "wb") as f:
                            async for part in _stream_media_fast(msg, _media):
                                f.write(part)
                                done += len(part)
                                await _progress(done, _total or done)
                        if _total and done < _total:
                            raise RuntimeError(f"short download: {done} of {_total} bytes")
                        os.replace(_part, _final)
                        return _final
                    except BaseException:
                        try:
                            os.remove(_part)
                        except OSError:
                            pass
                        raise

                local_path = await _mtproto("download_media", _download)
            else:
                # no usable file name on the message: let Pyrogram name it
                await _pyrogram_transfer_guard(_media)
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
    except FloodWait as fw:
        # Telegram (or our own cross-DC budget) says wait. That is not a failed
        # download: put it back in the queue — a 'failed' here would make the
        # *arr blocklist a perfectly good release — and hold this dispatcher
        # slot so we do not immediately ask again.
        wait = int(getattr(fw, "value", 60) or 60)
        log.warning("[worker] id=%s deferred %ds by Telegram rate limit — requeued",
                    row["dl_id"], wait)
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """UPDATE downloads
                       SET status='pending', speed_kbps=0, error_message=$1
                       WHERE id=$2 AND status NOT IN ('cancelled','completed')""",
                    f"Telegram rate limit — resumes automatically in ~{max(1, wait // 60)} min",
                    row["dl_id"])
        except Exception:
            log.exception("[worker] failed to requeue id=%s", row["dl_id"])
        await asyncio.sleep(min(wait + 5, 900))
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
# 3 seeds an hour ≈ 70 username lookups a day, and never more than the
# background share of the day's allowance (_MTPROTO_BG_SHARE): this is work
# done for the federation, on the user's account, and it comes after the
# user's own. (6 an hour was ~144/day - 70% of the allowance; the first
# default, 20, was ~360.)
SEEDS_BATCH = int(os.environ.get("TGARR_SEEDS_BATCH", "3"))
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
    log.info("[startup] no Telegram session yet — open tgarr in a browser (http://<this-host>:8765 unless you changed TGARR_PORT) and sign in")
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
        username = getattr(chat_obj, "username", None)
        log.info("[live-join] new channel chat_id=%s title=%s", chat_id, title)
        asyncio.create_task(backfill_channel(chat_id, title, username))
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
            async for dialog in _dialogs(limit=NEW_DIALOG_SCAN_LIMIT):
                ctype = dialog.chat.type.name
                if ctype in ("PRIVATE", "BOT"):
                    continue
                if dialog.chat.id not in known:
                    new_dialogs.append(
                        (dialog.chat.id, dialog.chat.title or "", dialog.chat.username))
            for chat_id, title, username in new_dialogs:
                log.info("[new-dialog] discovered chat_id=%s title=%s",
                         chat_id, title)
                # Mark joined immediately — get_dialogs returned it as a real
                # dialog, so the account is a member.
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO channels (tg_chat_id, title, is_joined)
                           VALUES ($1, $2, TRUE)
                           ON CONFLICT (tg_chat_id) DO UPDATE
                             SET is_joined = TRUE,
                                 title = COALESCE(NULLIF(EXCLUDED.title, ''), channels.title)""",
                        chat_id, title)
                try:
                    await backfill_channel(chat_id, title, username)
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


# Which picture on a t.me embed page belongs to the message that was asked for.
#
# The page used to be searched for "the first CDN image" (thumbnails) or "the
# last one" (previews). The first image on the page is the channel's AVATAR,
# and a post that is part of an album renders the whole album - so every
# photo of a channel got the same thumbnail (the avatar), and album photos
# got each other's. On one install 2,330 of 3,352 stored thumbnails were
# wrong this way.
#
# Each picture on the page is an <a class="…photo_wrap…" href="…/<message
# id>?single" style="background-image:url('…')">; a video is an
# <a class="…video_player…" href="…/<id>…"> holding an <i class="…video_thumb"
# style="background-image:url('…')">. Take the one whose link is this
# message. If the page shows exactly one picture and it carries no link, it
# is the message itself. Anything else: no answer - the caller falls back to
# MTProto, which cannot be wrong about which file it is.
_TME_PHOTO_TAG_RX = re.compile(r'<a\b[^>]*\btgme_widget_message_photo_wrap\b[^>]*>')
_TME_VIDEO_TAG_RX = re.compile(
    r'<a\b[^>]*\btgme_widget_message_video_player\b[^>]*>.{0,600}?'
    r'<i\b[^>]*\btgme_widget_message_video_thumb\b[^>]*>', re.DOTALL)
_TME_BG_RX = re.compile(r"background-image:url\('([^']+)'\)")
_TME_HREF_ID_RX = re.compile(r'href="[^"]*/(\d+)(?:\?[^"]*)?"')
_TME_CDN_OK_RX = re.compile(r"^https://cdn[0-9]*\.[a-z0-9.\-]*(telesco\.pe|cdn-telegram\.org|telegram-cdn\.org)/")


def _tme_media_url(page: str, tg_message_id: int):
    cands = []
    for tag in _TME_PHOTO_TAG_RX.findall(page) + _TME_VIDEO_TAG_RX.findall(page):
        u = _TME_BG_RX.search(tag)
        if not u or not _TME_CDN_OK_RX.match(u.group(1)):
            continue
        h = _TME_HREF_ID_RX.search(tag)
        cands.append((int(h.group(1)) if h else None, u.group(1)))
    for mid, url in cands:
        if mid == tg_message_id:
            return url
    if len(cands) == 1 and cands[0][0] is None:
        return cands[0][1]
    return None


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
        target = _tme_media_url(html, tg_message_id)
        if not target:
            return None
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
        target = _tme_media_url(html, tg_message_id)
        if not target:
            return None
        try:
            req2 = urllib.request.Request(
                target, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req2, timeout=10) as r2:
                return r2.read()
        except Exception:
            return None

    return await asyncio.to_thread(_scrape)


_THUMB_HTTPS_SEM = asyncio.Semaphore(10)
_THUMB_MTPROTO_SEM = asyncio.Semaphore(1)
_THUMB_DL_SEM = asyncio.Semaphore(3)
_THUMB_BATCH = 60


# ── cached media sessions for small files (thumbnails) ─────────────────
# Pyrogram's get_file opens a brand-new media session for every file and, when
# the file lives on another DC, creates a new auth key and runs
# auth.ExportAuthorization/ImportAuthorization each time: ~4s per thumbnail,
# and ExportAuthorization is exactly the call Telegram flood-limits. Keep ONE
# authorized media session per DC for the life of the process instead; a
# thumbnail is then a single upload.GetFile round-trip.
_media_sessions: dict = {}
_media_sessions_lock = asyncio.Lock()
# dc_id → epoch seconds until which Telegram refuses to authorize a session on
# that DC (FLOOD_WAIT on auth.ExportAuthorization). While set, nothing even
# asks Telegram again: every caller gets the FloodWait immediately. This is the
# piece the method-level limiter could never provide — the export used to
# happen inside Pyrogram, once per file, where its FloodWait was swallowed
# (the download just produced 0 bytes) and so never reached the limiter.
_media_dc_blocked_until: dict = {}


async def _media_session(dc_id: int):
    from pyrogram import raw
    from pyrogram.session import Auth, Session
    async with _media_sessions_lock:
        sess = _media_sessions.get(dc_id)
        if sess is not None:
            return sess
        my_dc = await app.storage.dc_id()
        if dc_id != my_dc:
            await _export_auth_reserve(dc_id)   # raises FloodWait, never waits
        test_mode = await app.storage.test_mode()
        # Kurigram wants to be told where the data centre is (Pyrogram had
        # the addresses built in): ask Telegram's own configuration, the way
        # the library does for its own sessions.
        ipv6 = bool(getattr(app, "ipv6", False))
        if dc_id != my_dc:
            opt = await app.get_dc_option(dc_id, ipv6=ipv6)
            auth_key = await Auth(app, dc_id, opt.ip_address, opt.port, test_mode).create()
        else:
            auth_key = await app.storage.auth_key()
        mopt = await app.get_dc_option(dc_id, is_media=True, ipv6=ipv6)
        sess = Session(app, dc_id, mopt.ip_address, mopt.port, auth_key, test_mode, is_media=True)
        await sess.start()
        try:
            if dc_id != my_dc:
                exported = await app.invoke(
                    raw.functions.auth.ExportAuthorization(dc_id=dc_id))
                await sess.invoke(raw.functions.auth.ImportAuthorization(
                    id=exported.id, bytes=exported.bytes))
        except BaseException as e:
            try:
                await sess.stop()
            except Exception:
                pass
            if isinstance(e, FloodWait):
                await _export_auth_flooded(
                    dc_id, int(getattr(e, "value", 60) or 60), "media session")
            raise
        _media_sessions[dc_id] = sess
        log.info("[media-session] opened DC%s (%s)", dc_id,
                 "cross-DC, authorization exported once" if dc_id != my_dc else "home DC")
        return sess


async def _drop_media_session(dc_id: int) -> None:
    async with _media_sessions_lock:
        sess = _media_sessions.pop(dc_id, None)
    if sess is not None:
        try:
            await sess.stop()
        except Exception:
            pass


async def _fetch_file_bytes(media, max_total: int = 24 * 1024 * 1024):
    """Bytes of a file via the cached media session of its DC, in 1 MB chunks.

    Returns None when this fast path does not apply (CDN redirect, larger
    than `max_total`, session trouble after one rebuild) — the caller then
    falls back to another source. FloodWait propagates."""
    from pyrogram import raw
    from pyrogram.file_id import FileType
    fid = FileId.decode(media.file_id)
    if fid.file_type == FileType.CHAT_PHOTO:
        return None
    if fid.file_type == FileType.PHOTO:
        location = raw.types.InputPhotoFileLocation(
            id=fid.media_id, access_hash=fid.access_hash,
            file_reference=fid.file_reference, thumb_size=fid.thumbnail_size)
    else:
        location = raw.types.InputDocumentFileLocation(
            id=fid.media_id, access_hash=fid.access_hash,
            file_reference=fid.file_reference, thumb_size=fid.thumbnail_size)
    chunk = 1024 * 1024
    for attempt in (1, 2):
        sess = await _media_session(fid.dc_id)
        out = bytearray()
        offset = 0
        try:
            while True:
                r = await sess.invoke(
                    raw.functions.upload.GetFile(location=location, offset=offset, limit=chunk),
                    sleep_threshold=30)
                if not isinstance(r, raw.types.upload.File):
                    return None          # upload.FileCdnRedirect etc.
                out += r.bytes
                if len(r.bytes) < chunk:
                    return bytes(out)
                if len(out) >= max_total:
                    return None
                offset += chunk
        except FloodWait:
            raise
        except (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
            # dead/idle-dropped session: rebuild once, then give up to fallback
            log.info("[media-session] DC%s dropped (%s) — reopening", fid.dc_id,
                     type(e).__name__)
            await _drop_media_session(fid.dc_id)
    return None


class _FastPathUnavailable(Exception):
    """The cached-media-session path cannot serve this file (e.g. Telegram
    redirected it to a CDN). Nothing has been yielded; use Pyrogram's own
    downloader instead."""


async def _stream_file_cached(media, start: int = 0):
    """Yield a file's bytes in 1 MB chunks over the cached media session of
    its DC, from byte `start` (a multiple of 1 MB).

    Compared with Pyrogram's stream/download (a new media session per file,
    plus a new auth key and auth.ExportAuthorization for every cross-DC file):
    no per-file session setup, no per-file cross-DC authorization — which is
    the call a multi-file grab used to hit FloodWait on — and a dropped
    connection resumes at the current offset instead of failing the file.
    Raises _FastPathUnavailable before the first byte if this path does not
    apply. FloodWait propagates.
    """
    from pyrogram import raw
    from pyrogram.file_id import FileType
    fid = FileId.decode(media.file_id)
    if fid.file_type == FileType.CHAT_PHOTO:
        raise _FastPathUnavailable("chat photo")
    if fid.file_type == FileType.PHOTO:
        location = raw.types.InputPhotoFileLocation(
            id=fid.media_id, access_hash=fid.access_hash,
            file_reference=fid.file_reference, thumb_size=fid.thumbnail_size)
    else:
        location = raw.types.InputDocumentFileLocation(
            id=fid.media_id, access_hash=fid.access_hash,
            file_reference=fid.file_reference, thumb_size=fid.thumbnail_size)
    chunk = 1024 * 1024
    offset = start
    reopened = 0
    while True:
        sess = await _media_session(fid.dc_id)
        try:
            r = await sess.invoke(
                raw.functions.upload.GetFile(location=location, offset=offset, limit=chunk),
                sleep_threshold=30)
        except FloodWait:
            raise
        except (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
            reopened += 1
            await _drop_media_session(fid.dc_id)
            if reopened > 3:
                if offset == start:
                    raise _FastPathUnavailable(f"media session: {type(e).__name__}")
                raise RuntimeError(f"media session kept dropping at offset {offset}: {e}")
            log.info("[media-session] DC%s dropped at offset %d (%s) — resuming",
                     fid.dc_id, offset, type(e).__name__)
            continue
        if not isinstance(r, raw.types.upload.File):
            if offset == start:
                raise _FastPathUnavailable(type(r).__name__)
            raise RuntimeError(f"unexpected {type(r).__name__} at offset {offset}")
        if r.bytes:
            yield r.bytes
        if len(r.bytes) < chunk:
            return
        offset += chunk
        reopened = 0


# An MP4 cannot start playing without its index (the "moov" box). Encoders
# that do not "fast-start" a file put it at the very end, after all the
# picture data - so a browser playing a file that is still arriving had to
# wait for the last byte of a 2 GB film before showing the first frame.
# When the head of a file shows that layout, fetch the tail first and park it
# next to the file as "<file>.tail" (8 bytes: its offset, then the bytes);
# the API serves requests for that region from there until the file is whole.
_TAIL_MAX_BYTES = 64 * 1024 * 1024


def _mp4_tail_offset(head: bytes, total: int):
    """Offset of what follows the picture data when the index is not in front
    of it; None when the index comes first or this is not such a file."""
    off = 0
    while off + 8 <= len(head):
        n, kind = struct.unpack(">I4s", head[off:off + 8])
        if n == 1:
            if off + 16 > len(head):
                return None
            n = struct.unpack(">Q", head[off + 8:off + 16])[0]
        if n < 8 or kind == b"moov":
            return None
        if kind == b"mdat":
            nxt = off + n
            return nxt if 0 < nxt < total else None
        off += n
    return None


async def _fetch_tail_sidecar(media, target: str, head: bytes, total: int) -> None:
    """Best effort. FloodWait propagates; anything else just means the file
    plays once it has fully arrived, as before."""
    moov_at = _mp4_tail_offset(head, total)
    if moov_at is None:
        return
    mb = 1024 * 1024
    start = (moov_at // mb) * mb
    if total - start > _TAIL_MAX_BYTES:
        return
    tmp = target + ".tail.tmp"
    try:
        with open(tmp, "wb") as tf:
            tf.write(struct.pack(">Q", start))
            async for part in _stream_file_cached(media, start=start):
                tf.write(part)
        os.replace(tmp, target + ".tail")
        log.info("[on-demand-media] index at the end of the file: fetched the last %.1f MB first",
                 (total - start) / mb)
    except FloodWait:
        raise
    except Exception as e:
        log.info("[on-demand-media] tail-first not possible (%s)", e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# Some taggers write a FLAC's embedded cover (the PICTURE block) without a
# MIME type. Chrome's decoder then refuses the whole file - "format not
# supported" - and the song never plays, although the audio is fine.
# The type is filled in from the image's own signature and the same number of
# bytes is taken out of a padding block, so the file keeps its exact size
# (the API serves by the size Telegram reported). With no padding to take
# from, the picture block is relabelled as padding: no cover, but it plays.
_FLAC_META_MAX = 8 * 1024 * 1024


def _flac_meta_len(buf) -> int | None:
    """Length of "fLaC" + all metadata blocks if `buf` holds them whole;
    None if more bytes are needed; 0 if this is not a FLAC stream."""
    if len(buf) < 4:
        return None
    if bytes(buf[:4]) != b"fLaC":
        return 0
    off = 4
    while True:
        if off + 4 > len(buf):
            return None
        last = buf[off] & 0x80
        off += 4 + int.from_bytes(buf[off + 1:off + 4], "big")
        if off > len(buf):
            return None
        if last:
            return off


def _flac_fix_meta(meta: bytes) -> bytes:
    """`meta` ("fLaC" + metadata blocks) with untyped covers repaired; the
    same object if nothing needed doing. Never changes the length."""
    blocks, off = [], 4
    while off < len(meta):
        ln = int.from_bytes(meta[off + 1:off + 4], "big")
        blocks.append([meta[off] & 0x7F, meta[off + 4:off + 4 + ln]])
        off += 4 + ln
    changed = False
    for b in blocks:
        body = b[1]
        if b[0] != 6 or len(body) < 12 or int.from_bytes(body[4:8], "big") != 0:
            continue
        data_at = 12 + int.from_bytes(body[8:12], "big") + 16 + 4
        magic = body[data_at:data_at + 8]
        mime = (b"image/jpeg" if magic[:3] == b"\xff\xd8\xff" else
                b"image/png" if magic[:8] == b"\x89PNG\r\n\x1a\n" else
                b"image/gif" if magic[:4] == b"GIF8" else
                b"image/webp" if magic[:4] == b"RIFF" else b"")
        pad = next((x for x in blocks if x[0] == 1 and len(x[1]) >= len(mime)), None)
        if mime and pad is not None:
            b[1] = body[:4] + len(mime).to_bytes(4, "big") + mime + body[8:]
            pad[1] = pad[1][:len(pad[1]) - len(mime)]
        else:
            b[0] = 1
        changed = True
    if not changed:
        return meta
    out = bytearray(b"fLaC")
    for i, (t, body) in enumerate(blocks):
        out.append(t | (0x80 if i == len(blocks) - 1 else 0))
        out += len(body).to_bytes(3, "big") + body
    return bytes(out) if len(out) == len(meta) else meta


def _flac_fix_file(path: str) -> bool:
    """Repair a FLAC already on disk. True if it was changed."""
    try:
        with open(path, "r+b") as f:
            buf = f.read(1024 * 1024)
            n = _flac_meta_len(buf)
            if n is None:
                buf += f.read(_FLAC_META_MAX)
                n = _flac_meta_len(buf)
            if not n:
                return False
            fixed = _flac_fix_meta(buf[:n])
            if fixed is buf[:n] or fixed == buf[:n]:
                return False
            f.seek(0)
            f.write(fixed)
            return True
    except OSError:
        return False


def _flac_fix_existing() -> int:
    n = 0
    try:
        for name in os.listdir(AUDIO_ROOT):
            if name.lower().endswith(".flac") and _flac_fix_file(os.path.join(AUDIO_ROOT, name)):
                n += 1
    except OSError:
        pass
    return n


def _drop_tail_sidecar(target: str) -> None:
    try:
        os.remove(target + ".tail")
    except OSError:
        pass


async def _stream_media_fast(msg, media):
    """Stream a message's file: cached media session first, Pyrogram's own
    streamer when that path does not apply."""
    try:
        async for part in _stream_file_cached(media):
            yield part
        return
    except _FastPathUnavailable as e:
        log.info("[media-session] fast path n/a (%s) — using Pyrogram streamer", e)
    dc = await _pyrogram_transfer_guard(media)
    got = False
    async for part in app.stream_media(msg):
        got = True
        yield part
    if not got:
        await _pyrogram_transfer_empty(dc)


async def _fetch_small_file(media, max_bytes: int = 1024 * 1024):
    """A one-chunk file (a thumbnail). None if it is not that small."""
    return await _fetch_file_bytes(media, max_total=max_bytes)


# ════════════════════════════════════════════════════════════════════
# Stale-index cleanup
#
# The index is a set of POINTERS to Telegram messages. Channels delete posts,
# get banned, go private or disappear; the pointers stay behind and show up as
# cards that can never produce a thumbnail, preview or download. Two ways a
# dead pointer is found:
#   - reactively: a user-requested thumbnail fetch comes back "message empty";
#   - proactively: stale_index_sweeper re-checks indexed media in slow,
#     200-id get_messages batches.
# Rules: only an explicit answer from Telegram removes anything; a channel
# that has become unreachable is first marked dead and only purged after a
# grace period; personal chats, KwickPOS* channels, rows with a file the user
# downloaded, and anything with an active download are never removed.
# ════════════════════════════════════════════════════════════════════
STALE_SWEEP_ENABLED = os.environ.get("TGARR_STALE_SWEEP", "true").lower() == "true"
STALE_SWEEP_INTERVAL_SEC = int(os.environ.get("TGARR_STALE_SWEEP_INTERVAL_SEC", "120"))
# ids per get_messages. Telegram allows 200, but several 200-id calls in a
# row earn a ~30s flood wait on channels.GetMessages; 100 every 2 minutes
# stays far below that.
STALE_SWEEP_BATCH = int(os.environ.get("TGARR_STALE_SWEEP_BATCH", "100"))
STALE_RECHECK_DAYS = int(os.environ.get("TGARR_STALE_RECHECK_DAYS", "30"))
DEAD_CHANNEL_GRACE_DAYS = int(os.environ.get("TGARR_DEAD_CHANNEL_GRACE_DAYS", "7"))
STALE_SWEEP_RESOLVE_PER_DAY = 30  # username lookups the sweeper may spend
_GONE_CHANNEL_MARKS = ("CHANNEL_PRIVATE", "USERNAME_NOT_OCCUPIED", "USERNAME_INVALID",
                       "CHANNEL_BANNED", "CHAT_FORBIDDEN")
_sweep_resolves = ["", 0]        # [utc day, count]


async def _meili_delete(index: str, ids: list) -> None:
    """Best-effort removal of documents from the search index."""
    if not ids or not MEILI_KEY:
        return
    def _post():
        req = urllib.request.Request(
            f"{MEILI_URL}/indexes/{index}/documents/delete-batch",
            data=json.dumps(ids).encode(), method="POST",
            headers={"Authorization": f"Bearer {MEILI_KEY}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    try:
        await asyncio.to_thread(_post)
    except Exception as e:
        log.debug("[meili] delete %s x%d failed: %s", index, len(ids), e)


async def _drop_gone_messages(channel_id: int, msg_ids: list) -> int:
    """Remove index rows whose Telegram message no longer exists. Keeps rows
    that still have a file the user downloaded, and (via _purge_messages)
    anything with an active download. Returns rows deleted."""
    if not msg_ids:
        return 0
    async with db_pool.acquire() as conn:
        keep = {r["id"] for r in await conn.fetch(
            """SELECT id FROM messages WHERE id = ANY($1::bigint[])
                 AND local_path IS NOT NULL AND local_path NOT LIKE '\\_\\_%' ESCAPE '\\'""",
            msg_ids)}
        ids = [i for i in msg_ids if i not in keep]
        if not ids:
            return 0
        # A release whose source post is gone is not lost if the same file is
        # still posted somewhere else: move the release to that copy.
        moved = await conn.fetch(
            """UPDATE releases r
                  SET primary_msg_id = alt.id
                 FROM messages gone
                 JOIN LATERAL (
                        SELECT m2.id FROM messages m2
                        JOIN channels c2 ON c2.id = m2.channel_id
                        WHERE m2.file_unique_id = gone.file_unique_id
                          AND m2.id <> ALL($1::bigint[])
                          AND c2.dead_at IS NULL
                        ORDER BY COALESCE(c2.is_joined, FALSE) DESC,
                                 COALESCE(c2.subscribed, FALSE) DESC, m2.id DESC
                        LIMIT 1) alt ON TRUE
                WHERE gone.id = ANY($1::bigint[])
                  AND gone.file_unique_id IS NOT NULL
                  AND r.primary_msg_id = gone.id
                RETURNING r.id""", ids)
        if moved:
            log.info("[stale] %d release(s) moved to another copy of the same file", len(moved))
        rel_ids = [r["id"] for r in await conn.fetch(
            "SELECT id FROM releases WHERE primary_msg_id = ANY($1::bigint[])", ids)]
    n = await _purge_messages(channel_id, ids)
    if n:
        await _meili_delete("messages", ids)
        await _meili_delete("releases", rel_ids)
    return n


async def _mark_channel_dead(channel_id: int, reason: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE channels SET dead_at = COALESCE(dead_at, NOW()), dead_reason = $2
               WHERE id = $1""", channel_id, reason[:120])


async def _dead_channel_housekeeping() -> list:
    """Channels nobody can read any more. Returns the rows removed from the list.

    A private channel (no public name) the account is no longer a member of
    cannot be read, followed or re-joined from here: it is dead like one whose
    name Telegram no longer knows, and gets the same grace period. Rejoining
    within it takes the mark off again. After the grace period a dead channel
    with nothing left in it leaves the list; one that still holds saved files
    or favourites keeps its row."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE channels SET dead_at = NULL, dead_reason = NULL
               WHERE dead_reason = 'left-private' AND COALESCE(is_joined, FALSE) = TRUE""")
        await conn.execute(
            """UPDATE channels SET dead_at = NOW(), dead_reason = 'left-private'
               WHERE dead_at IS NULL AND username IS NULL
                 AND COALESCE(is_joined, FALSE) = FALSE
                 AND COALESCE(subscribed, FALSE) = FALSE
                 AND COALESCE(title, '') NOT ILIKE 'KwickPOS%'
                 AND COALESCE(category, '') <> 'personal'""")
        gone = await conn.fetch(
            """DELETE FROM channels c
               WHERE c.dead_at < NOW() - make_interval(days => $1)
                 AND COALESCE(c.is_joined, FALSE) = FALSE
                 AND COALESCE(c.title, '') NOT ILIKE 'KwickPOS%'
                 AND COALESCE(c.category, '') <> 'personal'
                 AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.channel_id = c.id)
               RETURNING c.username, c.dead_reason""", DEAD_CHANNEL_GRACE_DAYS)
    for g in gone:
        log.info("[stale] dead channel @%s (%s): removed from the list",
                 g["username"] or "(private)", g["dead_reason"])
    return list(gone)


async def _channel_gone(chat_id: int, username) -> "bool | None":
    """Is the channel itself gone/unreachable for good?
    True = Telegram said so explicitly. False = it answered normally.
    None = could not tell (flood wait, missing peer, network)."""
    hit = _GONE_CACHE.get(chat_id)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]          # asked recently: every empty batch must not cost a lookup
    res = await _channel_gone_ask(chat_id, username)
    if res is not None:
        if len(_GONE_CACHE) > 2000:
            _GONE_CACHE.clear()
        _GONE_CACHE[chat_id] = (time.time(), res)
    return res


_GONE_CACHE: dict = {}        # chat_id → (asked at, answer)


async def _channel_gone_ask(chat_id: int, username) -> "bool | None":
    try:
        if username:
            # never wait here: this runs inside the thumbnail and preview
            # dispatchers, and "cannot tell right now" is a valid answer
            await _mtproto("resolveUsername", lambda: app.get_chat(username), wait=False,
                           username=username)
        else:
            await _mtproto("get_chat", lambda: app.get_chat(chat_id), wait=False)
        return False
    except (FloodWait, DailyBudgetExhausted):
        return None
    except Exception as e:
        return True if any(m in str(e) for m in _GONE_CHANNEL_MARKS) else None


async def _reconcile_missing(channel_id: int, chat_id: int, username,
                             requested: list, found: dict) -> tuple:
    """Given index rows we asked Telegram about (`requested`: records with id +
    tg_message_id) and what it returned (`found`: tg_message_id → Message),
    drop the ones that no longer exist. Returns (removed, channel_dead).

    If EVERYTHING in a real batch came back empty, don't trust it blindly:
    confirm the channel still answers before deleting, and if it is gone mark
    it dead instead (purged later, after the grace period)."""
    missing = [r for r in requested if r["tg_message_id"] not in found]
    if not missing:
        return 0, False
    if len(missing) == len(requested) and len(requested) >= 3:
        gone = await _channel_gone(chat_id, username)
        if gone is True:
            await _mark_channel_dead(channel_id, "unreachable: all indexed messages empty")
            return 0, True
        if gone is None:
            return 0, False          # can't confirm → change nothing
    n = await _drop_gone_messages(channel_id, [r["id"] for r in missing])
    return n, False


async def stale_index_sweeper():
    """Slowly re-verify that indexed media still exists on Telegram.

    One get_messages call (100 message ids) per interval - about 72K
    pointers/day at the default 120s — walking each channel once every
    TGARR_STALE_RECHECK_DAYS. Also purges channels that have been dead for
    longer than TGARR_DEAD_CHANNEL_GRACE_DAYS. Metadata calls only; nothing is
    downloaded and nothing is joined. Opt-out: TGARR_STALE_SWEEP=false
    """
    if not STALE_SWEEP_ENABLED:
        log.info("[stale] TGARR_STALE_SWEEP=false — disabled")
        return
    log.info("[stale] sweeper started; batch=%d every %ds, recheck %dd, dead grace %dd",
             STALE_SWEEP_BATCH, STALE_SWEEP_INTERVAL_SEC, STALE_RECHECK_DAYS,
             DEAD_CHANNEL_GRACE_DAYS)
    await asyncio.sleep(600 + random.uniform(0, 300))
    while True:
        nap = STALE_SWEEP_INTERVAL_SEC
        try:
            # ── 0a/0b. mark unreachable private channels; drop empty dead ones ──
            await _dead_channel_housekeeping()
            # ── 0. retire channels that stayed dead through the grace period ──
            async with db_pool.acquire() as conn:
                dead = await conn.fetchrow(
                    """SELECT c.id, c.username, c.dead_reason FROM channels c
                       WHERE c.dead_at < NOW() - make_interval(days => $1)
                         AND COALESCE(c.title, '') NOT ILIKE 'KwickPOS%'
                         AND COALESCE(c.category, '') <> 'personal'
                         AND EXISTS (SELECT 1 FROM messages m WHERE m.channel_id = c.id
                                     AND (m.local_path IS NULL
                                          OR m.local_path LIKE '\\_\\_%' ESCAPE '\\'))
                       ORDER BY c.dead_at LIMIT 1""", DEAD_CHANNEL_GRACE_DAYS)
                dead_ids = []
                if dead:
                    dead_ids = [r["id"] for r in await conn.fetch(
                        """SELECT id FROM messages WHERE channel_id = $1
                             AND (local_path IS NULL
                                  OR local_path LIKE '\\_\\_%' ESCAPE '\\')
                           LIMIT 5000""", dead["id"])]
            if dead and dead_ids:
                n = await _drop_gone_messages(dead["id"], dead_ids)
                log.info("[stale] dead channel @%s (%s): purged %d index rows",
                         dead["username"] or dead["id"], dead["dead_reason"], n)
                await _heartbeat("stale_index_sweeper",
                                 f"dead @{dead['username'] or dead['id']}: purged {n}")
                if n == 0:   # only protected rows left; don't re-pick it forever
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE channels SET dead_at = NOW() WHERE id = $1", dead["id"])
                await asyncio.sleep(5)
                continue

            # ── 1. next channel + where we left off in it ──────────────────
            async with db_pool.acquire() as conn:
                ch = await conn.fetchrow(
                    """SELECT c.id, c.tg_chat_id, c.username,
                              COALESCE(c.verify_cursor, 0) AS cur
                       FROM channels c
                       WHERE c.dead_at IS NULL
                         AND (c.account_user_id = $1 OR c.account_user_id IS NULL)
                         AND COALESCE(c.title, '') NOT ILIKE 'KwickPOS%'
                         AND COALESCE(c.category, '') <> 'personal'
                         AND (c.verified_at IS NULL
                              OR c.verified_at < NOW() - make_interval(days => $2))
                         AND EXISTS (SELECT 1 FROM messages m WHERE m.channel_id = c.id
                                     AND m.file_unique_id IS NOT NULL)
                       ORDER BY c.verified_at ASC NULLS FIRST, c.id
                       LIMIT 1""", CURRENT_USER_ID, STALE_RECHECK_DAYS)
                rows = []
                if ch:
                    rows = await conn.fetch(
                        """SELECT id, tg_message_id FROM messages
                           WHERE channel_id = $1 AND file_unique_id IS NOT NULL
                             AND tg_message_id > $2
                           ORDER BY tg_message_id LIMIT $3""",
                        ch["id"], ch["cur"], STALE_SWEEP_BATCH)
            if not ch:
                await _heartbeat("stale_index_sweeper", "index fully verified")
                await asyncio.sleep(3600)
                continue
            uname = ch["username"] or f"chat-{ch['id']}"
            if not rows:      # reached the end of this channel
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE channels SET verified_at = NOW(), verify_cursor = 0 "
                        "WHERE id = $1", ch["id"])
                await asyncio.sleep(1)
                continue

            # ── 2. one round-trip ──────────────────────────────────────────
            today = _utc_day()
            if _sweep_resolves[0] != today:
                _sweep_resolves[0], _sweep_resolves[1] = today, 0
            may_resolve = (ch["username"] and _sweep_resolves[1] < STALE_SWEEP_RESOLVE_PER_DAY
                           and ch["username"] not in _thumb_resolve_tried)
            defer_sql = ("UPDATE channels SET verified_at = NOW() - make_interval(days => $2) "
                         "WHERE id = $1")          # look again tomorrow
            try:
                if may_resolve:
                    _sweep_resolves[1] += 1
                found = await _thumb_get_messages(
                    ch["tg_chat_id"], ch["username"] if may_resolve else None,
                    [r["tg_message_id"] for r in rows])
            except FloodWait as fw:
                nap = min(int(getattr(fw, "value", 60) or 60) + 5, 3600)
                log.warning("[stale] flood-wait %ds", nap)
                await asyncio.sleep(nap)
                continue
            except DailyBudgetExhausted:
                async with db_pool.acquire() as conn:
                    await conn.execute(defer_sql, ch["id"], STALE_RECHECK_DAYS - 1)
                await asyncio.sleep(nap)
                continue
            except Exception as e:
                es = str(e)
                if any(m in es for m in _GONE_CHANNEL_MARKS):
                    await _mark_channel_dead(ch["id"], es)
                    log.info("[stale] @%s marked dead: %s", uname, es[:80])
                else:
                    # No usable peer / transient. Not evidence of anything.
                    log.debug("[stale] @%s skipped: %s", uname, es[:80])
                async with db_pool.acquire() as conn:
                    await conn.execute(defer_sql, ch["id"], STALE_RECHECK_DAYS - 1)
                await asyncio.sleep(nap)
                continue

            # Same round-trip, extra value: record the running time and frame
            # size of every video we just looked at.
            try:
                _dims = [(mid, getattr(m.video, "duration", None) or None,
                          getattr(m.video, "width", None) or None,
                          getattr(m.video, "height", None) or None)
                         for mid, m in found.items() if getattr(m, "video", None)]
                if _dims:
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            """UPDATE messages m
                                  SET duration_sec = v.d, width = v.w, height = v.h,
                                      rel_ver = NULL
                                 FROM unnest($2::bigint[], $3::int[], $4::int[], $5::int[])
                                      AS v(mid, d, w, h)
                                WHERE m.channel_id = $1 AND m.tg_message_id = v.mid
                                  AND m.duration_sec IS NULL""",
                            ch["id"], [x[0] for x in _dims], [x[1] for x in _dims],
                            [x[2] for x in _dims], [x[3] for x in _dims])
            except Exception as e:
                log.warning("[stale] could not record video attributes for @%s: %s", uname, e)

            removed, is_dead = await _reconcile_missing(
                ch["id"], ch["tg_chat_id"], ch["username"], rows, found)
            async with db_pool.acquire() as conn:
                if is_dead:
                    await conn.execute(defer_sql, ch["id"], 0)
                else:
                    await conn.execute(
                        "UPDATE channels SET verify_cursor = $2, dead_at = NULL, "
                        "dead_reason = NULL WHERE id = $1",
                        ch["id"], rows[-1]["tg_message_id"])
            if removed or is_dead:
                log.info("[stale] @%s: checked %d, removed %d%s", uname, len(rows),
                         removed, " — channel marked dead" if is_dead else "")
            await _heartbeat("stale_index_sweeper",
                             f"@{uname}: checked {len(rows)}, removed {removed}"
                             + (" (dead)" if is_dead else ""))
        except Exception as e:
            log.exception("[stale] sweeper error: %s", e)
            await _heartbeat("stale_index_sweeper", error=str(e)[:200])
        await asyncio.sleep(nap)


def _pick_thumb_media(msg):
    """The small embedded thumbnail of a message's media (never the original)."""
    if msg.photo:
        pthumbs = getattr(msg.photo, "thumbs", None) or []
        return pthumbs[-1] if pthumbs else msg.photo
    for kind in ("video", "document", "animation", "audio"):
        m = getattr(msg, kind, None)
        if m is not None and getattr(m, "thumbs", None):
            return m.thumbs[-1]
    return None


def _thumb_target(row) -> tuple:
    safe_uid = SAFE_NAME.sub("_", row["file_unique_id"] or str(row["id"]))[:80]
    fname = f"{safe_uid}.jpg"
    return fname, os.path.join(THUMBS_ROOT, fname)


async def _thumb_store(row, fname: str, target: str) -> None:
    """md5-dedup the downloaded thumb across channels and record it."""
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
                "UPDATE messages SET thumb_path=$1, thumb_md5=$2, "
                "thumb_accessed_at=NOW() WHERE id=$3",
                existing, md5, row["id"])
        else:
            await conn.execute(
                "UPDATE messages SET thumb_path=$1, thumb_md5=$2, "
                "thumb_accessed_at=NOW() WHERE id=$3",
                fname, md5, row["id"])


_PEER_ERR_MARKS = ("CHANNEL_INVALID", "PEER_ID_INVALID", "Peer id invalid",
                   "ID not found", "CHANNEL_PRIVATE", "USERNAME_NOT_OCCUPIED",
                   "USERNAME_INVALID")


async def _thumb_fail(row, e, whole_channel: bool = False) -> None:
    es = str(e)
    channel_dead = whole_channel or any(m in es for m in _PEER_ERR_MARKS)
    async with db_pool.acquire() as conn:
        if channel_dead and row["tg_chat_id"]:
            # Fail every queued thumb of an unreachable channel in one shot
            # instead of grinding them one by one.
            res = await conn.execute(
                "UPDATE messages SET thumb_path='__failed__' "
                "WHERE tg_chat_id=$1 AND thumb_path='__user_queued__'",
                row["tg_chat_id"])
            log.info("[thumbs] channel %s unreachable (%s) — failed queued batch %s",
                     row["tg_chat_id"], es[:60], res)
        else:
            log.info("[thumbs] failed id=%s: %s", row["id"], es[:100])
            await conn.execute(
                "UPDATE messages SET thumb_path='__failed__' WHERE id=$1", row["id"])


# Public channels we have already tried to re-resolve by username in this
# process, so an unreachable one costs at most one lookup.
_thumb_resolve_tried: set = set()


async def _thumb_get_messages(chat_id: int, username, ids: list) -> dict:
    """ONE get_messages round-trip for all queued thumbs of a channel.

    If the session has no usable peer for the channel (fresh login, or a
    public channel the account is not a member of), resolve it once by
    username — reading a public channel needs no membership, and it never
    joins anything — then retry.
    """
    async def _call():
        return await _mtproto("get_messages",
                              lambda: app.get_messages(chat_id, ids))
    try:
        msgs = await _call()
    except FloodWait:
        raise
    except Exception as e:
        if not (username and any(m in str(e) for m in _PEER_ERR_MARKS)
                and username not in _thumb_resolve_tried):
            raise
        log.info("[thumbs] no peer for %s — resolving @%s once", chat_id, username)
        try:
            await _mtproto("resolveUsername", lambda: app.resolve_peer(username), wait=False,
                           username=username)
        except DailyBudgetExhausted:
            raise                        # not this channel's fault: try again another day
        except Exception:
            _thumb_resolve_tried.add(username)
            raise
        _thumb_resolve_tried.add(username)
        msgs = await _call()
    if not isinstance(msgs, list):
        msgs = [msgs]
    return {m.id: m for m in msgs if m is not None and not getattr(m, "empty", False)}


async def _thumb_process_batch(rows) -> None:
    """Materialize a batch of user-requested thumbnails.

    1. HTTPS scrape of the public t.me embed, in parallel — works for photo
       posts and small native videos, costs no MTProto.
    2. Everything else per channel: one get_messages for the whole group, then
       the tiny embedded thumbs. (Large video *files* have no web preview at
       all, so for a movie channel this is the only path — it used to be one
       rate-limited get_messages per thumbnail.)
    """
    async def https_one(r) -> bool:
        if not r["channel_username"]:
            return False
        fname, target = _thumb_target(r)
        try:
            async with _THUMB_HTTPS_SEM:
                data = await _fetch_thumb_https(r["channel_username"], r["tg_message_id"])
            if not data:
                return False
            # A scraped picture that is byte-for-byte the thumbnail of a
            # DIFFERENT file in the same channel is not this message's picture
            # (it is the avatar, or the page changed shape): ask Telegram.
            if r["file_unique_id"]:
                async with db_pool.acquire() as conn:
                    clash = await conn.fetchval(
                        """SELECT 1 FROM messages
                           WHERE thumb_md5 = $1 AND channel_id = $2
                             AND file_unique_id IS NOT NULL AND file_unique_id <> $3
                           LIMIT 1""",
                        hashlib.md5(data).hexdigest(), r["channel_id"], r["file_unique_id"])
                if clash:
                    return False
            with open(target, "wb") as f:
                f.write(data)
            await _thumb_store(r, fname, target)
            return True
        except Exception as e:
            log.debug("[thumbs] https id=%s: %s", r["id"], e)
            return False

    got = await asyncio.gather(*[https_one(r) for r in rows])
    rest = [r for r, ok in zip(rows, got) if not ok]
    if not rest:
        return
    if os.environ.get("TGARR_THUMB_ONLY", "false").lower() == "true":
        for r in rest:
            await _thumb_fail(r, RuntimeError("THUMB_ONLY mode: HTTPS miss + MTProto disabled"))
        return

    by_chat: dict = {}
    for r in rest:
        by_chat.setdefault(r["tg_chat_id"], []).append(r)

    for chat_id, grp in by_chat.items():
        try:
            async with _THUMB_MTPROTO_SEM:
                msgs = await _thumb_get_messages(
                    chat_id, grp[0]["channel_username"],
                    [r["tg_message_id"] for r in grp])
        except FloodWait as fw:
            wait = int(getattr(fw, "value", 60) or 60) + 5
            log.warning("[thumbs] flood-wait %ds — leaving %d queued", wait, len(grp))
            await asyncio.sleep(min(wait, 3600))
            return          # rows stay __user_queued__ for the next pass
        except DailyBudgetExhausted as e:
            # Cannot reach this channel right now. Take its rows out of the
            # queue (they are asked for again the next time someone looks) -
            # left queued they would be picked up again at once, for ever.
            log.info("[thumbs] %s — channel %s skipped for now (%d thumbnails)", e, chat_id, len(grp))
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE messages SET thumb_path = NULL "
                    "WHERE id = ANY($1::bigint[]) AND thumb_path = '__user_queued__'",
                    [r["id"] for r in grp])
            continue
        except Exception as e:
            await _thumb_fail(grp[0], e, whole_channel=True)
            continue

        # Telegram answered for this channel: any requested message that came
        # back empty has been deleted at the source. Drop those pointers now
        # rather than leaving dead cards behind.
        try:
            removed, is_dead = await _reconcile_missing(
                grp[0]["channel_id"], chat_id, grp[0]["channel_username"], grp, msgs)
            if removed or is_dead:
                log.info("[thumbs] channel %s: %d stale pointer(s) removed%s", chat_id,
                         removed, " — channel marked dead" if is_dead else "")
        except Exception as e:
            log.warning("[thumbs] stale reconcile for %s failed: %s", chat_id, e)

        async def dl(r, msgs=msgs):
            fname, target = _thumb_target(r)
            try:
                msg = msgs.get(r["tg_message_id"])
                if msg is None:
                    raise RuntimeError("message gone")
                media = _pick_thumb_media(msg)
                if media is None:
                    raise RuntimeError("no thumbnail available on message")
                async with _THUMB_DL_SEM:
                    data = await _mtproto("download_thumb",
                        lambda: _fetch_small_file(media))
                    if data:
                        with open(target, "wb") as f:
                            f.write(data)
                    else:
                        # fast path not applicable → Pyrogram's own downloader
                        _dc = await _pyrogram_transfer_guard(media)
                        await _mtproto("download_thumb",
                            lambda: app.download_media(media, file_name=target))
                        if not os.path.exists(target) or os.path.getsize(target) == 0:
                            await _pyrogram_transfer_empty(_dc)
                if not os.path.exists(target) or os.path.getsize(target) == 0:
                    raise RuntimeError("thumbnail download returned nothing")
                await _thumb_store(r, fname, target)
            except FloodWait as fw:
                log.warning("[thumbs] flood-wait %ss on download (id=%s) — left queued",
                            getattr(fw, "value", "?"), r["id"])
            except Exception as e:
                await _thumb_fail(r, e)

        await asyncio.gather(*[dl(r) for r in grp])


THUMB_TTL_HOURS = int(os.environ.get("TGARR_THUMB_TTL_HOURS", "5"))
PREVIEW_TTL_HOURS = int(os.environ.get("TGARR_PREVIEW_TTL_HOURS", "5"))
MEDIA_GC_INTERVAL_SEC = int(os.environ.get("TGARR_MEDIA_GC_INTERVAL_SEC", "1800"))


async def _gc_media_cache(label, path_col, accessed_col, root, ttl_hours):
    """Evict files in `root` whose EVERY referrer is stale (MAX accessed_at <
    now-ttl). md5-dedup-safe: a file shared by several messages is removed only
    when all are cold. Clearing the path re-queues it on next view. Returns
    (files_removed, bytes_freed, rows_cleared). path_col/accessed_col are fixed
    literals (not user input)."""
    async with db_pool.acquire() as conn:
        stale = await conn.fetch(f"""
            SELECT {path_col} AS p
            FROM messages
            WHERE {path_col} IS NOT NULL
              AND {path_col} NOT IN ('__user_queued__','__failed__','__deleted__')
            GROUP BY {path_col}
            HAVING MAX(COALESCE({accessed_col}, 'epoch'::timestamptz))
                   < NOW() - INTERVAL '{int(ttl_hours)} hours'
            LIMIT 2000
        """)
    removed = freed = cleared = 0
    for r in stale:
        fn = r["p"]
        fpath = os.path.join(root, fn)
        try:
            freed += os.path.getsize(fpath)
            os.remove(fpath)
            removed += 1
        except FileNotFoundError:
            pass  # already gone — still clear dangling refs
        except Exception as e:
            log.warning("[%s-gc] remove %s: %s", label, fn, e)
            continue
        async with db_pool.acquire() as conn:
            res = await conn.execute(
                f"UPDATE messages SET {path_col}=NULL WHERE {path_col}=$1", fn)
        try:
            cleared += int(res.split()[-1])
        except Exception:
            pass
    return removed, freed, cleared


async def thumb_cache_gc_worker() -> None:
    """Evict thumbnail AND preview files not READ within their TTL (default 5h
    each). On-demand caches re-fetch fast, so we don't hoard — and full-res
    previews are large, so they especially need trimming.
    """
    await asyncio.sleep(600)  # settle after boot
    log.info("[media-gc] started; thumb_ttl=%dh preview_ttl=%dh interval=%ds",
             THUMB_TTL_HOURS, PREVIEW_TTL_HOURS, MEDIA_GC_INTERVAL_SEC)
    while True:
        try:
            tr, tf, tc = await _gc_media_cache(
                "thumb", "thumb_path", "thumb_accessed_at",
                THUMBS_ROOT, THUMB_TTL_HOURS)
            pr, pf, pc = await _gc_media_cache(
                "preview", "preview_path", "preview_accessed_at",
                PREVIEWS_ROOT, PREVIEW_TTL_HOURS)
            if tr or pr:
                log.info("[media-gc] evicted thumbs %d (%.1fMB) / previews %d (%.1fMB)",
                         tr, tf / 1e6, pr, pf / 1e6)
            await _heartbeat("thumb_cache_gc_worker",
                             f"thumbs {tr} ({tf//1024//1024}MB), previews {pr} ({pf//1024//1024}MB)")
            await asyncio.sleep(MEDIA_GC_INTERVAL_SEC)
        except Exception:
            log.exception("[media-gc] outer loop")
            await asyncio.sleep(300)


_PREVIEW_SEM = asyncio.Semaphore(5)  # concurrent HTTPS fetches


_PREVIEW_DL_SEM = asyncio.Semaphore(3)


def _pick_preview_media(msg):
    """What to show full-screen: the photo itself (largest size), or for a
    non-photo message its best embedded thumbnail."""
    if msg.photo:
        return msg.photo
    for kind in ("video", "animation", "document"):
        m = getattr(msg, kind, None)
        if m is not None and getattr(m, "thumbs", None):
            return m.thumbs[-1]
    return None


async def _preview_store(row, blob: bytes) -> None:
    safe_uid = SAFE_NAME.sub("_", row["file_unique_id"] or str(row["id"]))[:80]
    fname = f"{safe_uid}.jpg"
    with open(os.path.join(PREVIEWS_ROOT, fname), "wb") as f:
        f.write(blob)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE messages SET preview_path=$1, preview_accessed_at=NOW() "
            "WHERE id=$2", fname, row["id"])


async def _preview_fail(row) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE messages SET preview_path='__failed__' WHERE id=$1", row["id"])


async def _preview_process_batch(rows) -> None:
    """Materialize the large images a user just opened.

    Full resolution straight from Telegram, for every DC: one get_messages per
    channel for the whole batch, then each photo over the cached media session
    of its DC (one cross-DC authorization per DC for the life of the process).
    This used to be one get_messages plus a brand-new media session per image,
    and cross-DC images were limited to the ~450px t.me embed to avoid
    re-authorizing each time. The embed stays as the fallback when MTProto
    cannot answer (no usable peer, flood wait).
    """
    by_chat: dict = {}
    for r in rows:
        by_chat.setdefault(r["tg_chat_id"], []).append(r)

    for chat_id, grp in by_chat.items():
        msgs = None
        try:
            async with _THUMB_MTPROTO_SEM:
                msgs = await _thumb_get_messages(
                    chat_id, grp[0]["channel_username"],
                    [r["tg_message_id"] for r in grp])
        except (FloodWait, DailyBudgetExhausted) as e:
            log.info("[preview] channel %s: MTProto unavailable (%s) — embed fallback",
                     chat_id, type(e).__name__)
        except Exception as e:
            log.debug("[preview] channel %s get_messages: %s", chat_id, str(e)[:100])

        if msgs is not None:
            try:
                removed, is_dead = await _reconcile_missing(
                    grp[0]["channel_id"], chat_id, grp[0]["channel_username"], grp, msgs)
                if removed or is_dead:
                    log.info("[preview] channel %s: %d stale pointer(s) removed%s", chat_id,
                             removed, " — channel marked dead" if is_dead else "")
            except Exception as e:
                log.warning("[preview] stale reconcile for %s failed: %s", chat_id, e)

        async def one(r, msgs=msgs):
            blob = None
            try:
                msg = msgs.get(r["tg_message_id"]) if msgs is not None else None
                if msgs is not None and msg is None:
                    await _preview_fail(r)      # gone at the source
                    return
                media = _pick_preview_media(msg) if msg is not None else None
                if media is not None:
                    try:
                        async with _PREVIEW_DL_SEM:
                            blob = await _mtproto("download_preview",
                                lambda: _fetch_file_bytes(media))
                            if not blob:
                                _dc = await _pyrogram_transfer_guard(media)
                                buf = await _mtproto("download_media",
                                    lambda: app.download_media(media, in_memory=True))
                                if buf is not None:
                                    blob = (buf.getvalue() if hasattr(buf, "getvalue")
                                            else bytes(buf))
                                if not blob:
                                    await _pyrogram_transfer_empty(_dc)
                    except FloodWait as fw:
                        log.info("[preview] flood-wait %ss id=%s — embed fallback",
                                 getattr(fw, "value", "?"), r["id"])
                    except Exception as e:
                        log.debug("[preview] mtproto id=%s: %s", r["id"], str(e)[:100])
                if not blob and r["channel_username"]:
                    try:
                        async with _PREVIEW_SEM:
                            blob = await _fetch_preview_https(
                                r["channel_username"], r["tg_message_id"])
                    except Exception as e:
                        log.debug("[preview] https err id=%s: %s", r["id"], e)
                if blob:
                    await _preview_store(r, blob)
                else:
                    await _preview_fail(r)
            except Exception as e:
                log.warning("[preview] failed id=%s: %s", r["id"], str(e)[:120])
                try:
                    await _preview_fail(r)
                except Exception:
                    pass

        await asyncio.gather(*[one(r) for r in grp])


# On-demand wake signals. The API NOTIFYs the moment it queues a thumb/preview;
# the dispatchers wait on these events so they fire INSTANTLY (0ms latency, no
# idle polling, no busy-loop). A short fallback timeout re-checks in case a
# NOTIFY is ever missed (e.g. listener reconnect).
_thumb_wake = asyncio.Event()
_preview_wake = asyncio.Event()
_ONDEMAND_IDLE_FALLBACK_SEC = 30


async def _ondemand_notify_listener() -> None:
    """Hold a dedicated connection LISTENing for on-demand enqueue NOTIFYs and
    set the wake events. Reconnects on error. This is what makes the gallery /
    slideshow feel instant — no 30-60s dispatcher nap."""
    while True:
        conn = None
        try:
            conn = await asyncpg.connect(DB_DSN)
            await conn.add_listener("thumb_queued", lambda *_: _thumb_wake.set())
            await conn.add_listener("preview_queued", lambda *_: _preview_wake.set())
            log.info("[ondemand] NOTIFY listener attached (thumb_queued, preview_queued)")
            while True:
                await asyncio.sleep(3600)  # keep the connection alive
        except Exception as e:
            log.warning("[ondemand] listener reconnect in 5s: %s", e)
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
            await asyncio.sleep(5)


async def preview_downloader() -> None:
    """Large-image fetcher for the lightbox / slideshow. LIFO batch (newest
    queued first) so the image the user just opened beats any backlog. Wakes
    instantly on preview_queued NOTIFY (no idle nap). See
    _preview_process_batch for the fetch strategy.
    """
    log.info("[preview] dispatcher started; root=%s conc=5", PREVIEWS_ROOT)
    os.makedirs(PREVIEWS_ROOT, exist_ok=True)
    while True:
        try:
            _preview_wake.clear()
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT m.id, m.channel_id, m.tg_chat_id, m.tg_message_id,
                              m.file_unique_id, m.file_dc,
                              c.username AS channel_username
                       FROM messages m JOIN channels c ON c.id = m.channel_id
                       WHERE m.preview_path = '__user_queued__'
                       ORDER BY m.id DESC LIMIT 20""")
            if rows:
                await _preview_process_batch(rows)
                continue  # drain immediately, no nap
            try:
                await asyncio.wait_for(_preview_wake.wait(),
                                       timeout=_ONDEMAND_IDLE_FALLBACK_SEC)
            except asyncio.TimeoutError:
                pass
        except Exception:
            log.exception("[preview] outer loop")
            await asyncio.sleep(5)


async def thumb_downloader() -> None:
    """Concurrent thumb-fetch dispatcher.

    Pulls a batch of queued rows, hands them to _thumb_process_batch,
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
            _thumb_wake.clear()  # clear before query so a NOTIFY during it isn't lost
            async with db_pool.acquire() as conn:
                # LIFO: newest queue requests first. User just switched channel
                # → those rows have the biggest m.id (recently INSERTed by
                # /api/thumb's UPDATE). Drain them before old leftovers so the
                # 12s polling timeout in /api/thumb is more likely to win.
                rows = await conn.fetch(
                    """SELECT m.id, m.channel_id, m.tg_chat_id, m.tg_message_id,
                              m.file_unique_id, c.username AS channel_username
                       FROM messages m JOIN channels c ON c.id = m.channel_id
                       WHERE m.thumb_path = '__user_queued__'
                       ORDER BY m.id DESC
                       LIMIT $1""", _THUMB_BATCH)
            if rows:
                await _thumb_process_batch(rows)
                continue  # drain immediately, no nap
            # queue empty → wake instantly on the next thumb_queued NOTIFY
            # (fallback re-check guards against a missed wakeup).
            try:
                await asyncio.wait_for(_thumb_wake.wait(),
                                       timeout=_ONDEMAND_IDLE_FALLBACK_SEC)
            except asyncio.TimeoutError:
                pass
        except Exception as e:
            log.exception("[thumbs] outer loop: %s", e)
            await asyncio.sleep(5)


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
                    """SELECT id, tg_chat_id, username, title, backfilled, handle_known, web_preview
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
            # HTTP first: a channel the session holds no handle for is read
            # from tgarr.me and the web preview; the account is not asked.
            central_only = False
            if WEB_FIRST and uname and not row["handle_known"]:
                try:
                    if await _poll_without_account(row):
                        continue
                    async with db_pool.acquire() as conn:
                        central_only = bool(await conn.fetchval(
                            "SELECT 1 FROM messages WHERE channel_id = $1 LIMIT 1", row["id"]))
                except Exception as e:
                    log.warning("[web] @%s: %s - falling back to the account", uname, str(e)[:160])
            try:
                # Discriminator: title pattern '@<uname> (resolving…)' means
                # row has placeholder chat_id from /api/channel/subscribe
                # (the int-range trick is broken — placeholder range overlaps
                # real TG channel ids in [-3e12, -1e12]).
                is_placeholder = (not row["handle_known"]) or (
                    (row["title"] or "").startswith("@") and "(resolving" in (row["title"] or ""))
                try:
                    if not is_placeholder:
                        chat = await _mtproto("get_chat",
                            lambda: app.get_chat(row["tg_chat_id"]))
                    else:
                        # The first lookup of a followed channel, counted like
                        # every other lookup. When its files are already listed
                        # (from tgarr.me) nothing is waiting on it: it may only
                        # use the background share of the allowance.
                        chat = await _mtproto("resolveUsername",
                            lambda: app.get_chat(uname),
                            wait=False, user=not central_only, username=uname)
                except (ChannelInvalid, ValueError) as e:
                    # Stale Pyrogram peer cache (access_hash bad or missing).
                    # Force fresh resolveUsername via raw API + repopulate peer
                    # storage, then retry. One-time cost; cache stays fresh
                    # for subsequent polls.
                    if isinstance(e, ValueError) and "Peer id invalid" not in str(e):
                        raise
                    # The session has lost (or never had) this channel's
                    # handle. Before a lookup is spent on getting it back:
                    # HTTP first, exactly as for a channel followed just now.
                    if WEB_FIRST and uname:
                        try:
                            if await _poll_without_account(row):
                                continue
                            async with db_pool.acquire() as conn:
                                central_only = bool(await conn.fetchval(
                                    "SELECT 1 FROM messages WHERE channel_id = $1 LIMIT 1", row["id"]))
                        except Exception as _we:
                            log.warning("[web] @%s: %s - falling back to the account", uname, str(_we)[:160])
                    log.info("[subscribe] @%s stale peer cache → force-resolve", uname)
                    from pyrogram.raw.functions.contacts import ResolveUsername
                    # a channel is followed because someone chose to follow it
                    # wait=False: when today's lookups are used up this
                    # ONE channel is skipped until tomorrow. Waiting here
                    # parked the whole poller - every followed channel went
                    # without its new posts for the rest of the day.
                    r = await _mtproto("resolveUsername",
                        lambda: app.invoke(ResolveUsername(username=uname.lstrip("@"))),
                        wait=False, user=not central_only)
                    await app.fetch_peers(r.chats + r.users)
                    chat = await _mtproto("get_chat",
                        lambda: app.get_chat(uname))
                real_chat_id = chat.id
                title = chat.title or uname
                members = getattr(chat, "members_count", None)
            except FloodWait as fw:
                # Transient — back off, leave subscribed intact, retry next cycle.
                log.warning("[subscribe] flood-wait %ds on @%s — transient",
                            fw.value, uname)
                await asyncio.sleep(min(fw.value + 2, 600))
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE channels SET subscribe_error=$1,
                           last_polled_at=NOW() WHERE id=$2""",
                        f"FloodWait {fw.value}s", row["id"])
                continue
            except DailyBudgetExhausted:
                log.info("[subscribe] @%s needs a username lookup and today's are used up "
                         "— it is polled again tomorrow; the other channels carry on", uname)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE channels SET subscribe_error=$1,
                           last_polled_at=NOW() WHERE id=$2""",
                        # a channel whose files are already listed is not in trouble:
                        # it only waits for spare allowance to be read live
                        None if central_only else "waiting for tomorrow's Telegram lookup allowance",
                        row["id"])
                continue
            except (UsernameNotOccupied, UsernameInvalid, ChannelPrivate) as e:
                # Permanent — channel gone / username dead / account banned.
                # Safe to disable.
                log.warning("[subscribe] disable @%s — permanent: %s",
                            uname, type(e).__name__)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE channels SET subscribed=FALSE,
                           subscribe_error=$1, last_polled_at=NOW()
                           WHERE id=$2""", type(e).__name__, row["id"])
                # Telegram said the name is gone / the channel is closed to us:
                # that is a dead channel. Marked like the ones the sweeper
                # finds, so it is cleared and removed after the grace period
                # instead of sitting in the list for ever with an error.
                await _mark_channel_dead(row["id"], type(e).__name__)
                continue
            except Exception as e:
                # Transient — PeerIdInvalid (Pyrogram session cache miss, may
                # recover after dialog rebuild), ChannelInvalid (stale access
                # hash), network errors, internal TG. Keep subscribed=TRUE so
                # the next poll cycle retries naturally. Bump last_polled_at
                # to move on this round.
                log.warning("[subscribe] transient err @%s: %s — keep subscribed",
                            uname, str(e)[:120])
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE channels SET subscribe_error=$1,
                           last_polled_at=NOW() WHERE id=$2""",
                        f"{type(e).__name__}: {str(e)[:160]}", row["id"])
                continue

            # News/TV title/username gate: reject before backfill (federation
            # value=0 — every clip is unique to one channel). See _is_noise_title.
            # (No name filter here: every channel the poller handles was
            # followed by the user. It used to be unfollowed again as "news/TV"
            # on its first poll.)

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
                               members_count=$3, last_polled_at=NOW(), handle_known=TRUE
                               WHERE id=$4""",
                            real_chat_id, title, members, row["id"])
                        # rows read from the web or from tgarr.me before the
                        # channel had a real id
                        await conn.execute(
                            "UPDATE messages SET tg_chat_id=$1 WHERE channel_id=$2 AND tg_chat_id<>$1",
                            real_chat_id, row["id"])
                        row = dict(row)
                        row["tg_chat_id"] = real_chat_id

            # Rehydrate: if this channel was purged after release (crawl-and-
            # release), pull its index back from central BEFORE polling so
            # grab/download works again; the backfill below then catches any
            # messages posted since the purge.
            async with db_pool.acquire() as conn:
                was_purged = await conn.fetchval(
                    "SELECT purged_at FROM channels WHERE id=$1", row["id"])
            if was_purged:
                restored = await _rehydrate_channel(
                    row["id"], row["tg_chat_id"], uname)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE channels SET purged_at=NULL WHERE id=$1", row["id"])
                log.info("[subscribe] @%s rehydrated %d rows from central (was purged)",
                         uname, restored)

            # Backfill on first poll, then incremental on later polls.
            limit = SUBSCRIBE_BACKFILL_LIMIT if not row["backfilled"] else 200
            count = media_count = 0
            try:
                await _mtproto_wait_clearance()
                async for msg in _hist(real_chat_id, limit=limit):
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
    # Tightened: loose \bcp\d+|\bcp_ false-matched innocent names (cp1250, cp_2023)
    r"pre[\s_-]*teen|under[\s_-]*age|cp\d{3,}|cp_(?:young|teen|kid|pthc|lolita|child))\b",
    re.IGNORECASE)
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
                async for m in _hist(chat.id, limit=1):
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
                # The list is asked for per audience. It used to be asked for
                # "sfw" only, whatever the setting said: someone who had
                # switched adult content on got an empty adult Discover.
                _adult = (await conn.fetchval(
                    "SELECT value FROM config WHERE key='nsfw_enabled'")) == "true"
                _since = {r["audience"]: r["t"] for r in await conn.fetch(
                    "SELECT audience, max(last_pulled_at) AS t FROM discovered GROUP BY 1")}
            channels = []
            for _aud in (["sfw", "nsfw"] if _adult else ["sfw"]):
                params = {"audience": _aud, "only_verified": "1", "limit": "5000"}
                if _since.get(_aud):
                    params["since"] = _since[_aud].isoformat()
                qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
                url = REGISTRY_URL + "/api/v1/registry?" + qs
                try:
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": TGARR_UA,
                                 "Accept": "application/json"},
                    )
                    resp = await asyncio.to_thread(
                        lambda: urllib.request.urlopen(req, timeout=30).read())
                    channels += json.loads(resp.decode()).get("channels", [])
                except Exception as e:
                    log.warning("[pull] registry GET (%s) failed: %s", _aud, e)

            inserted = updated = 0
            async with db_pool.acquire() as conn:
                for c in channels:
                    u = (c.get("username") or "").strip()
                    if not u:
                        continue
                    is_new = await conn.fetchval(
                        """INSERT INTO discovered
                             (username, title, members_count, media_count, audience,
                              language, category, distinct_contributors, verified,
                              release_files, video_files, book_files, audio_files,
                              last_file_at)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                           ON CONFLICT (username) DO UPDATE SET
                             last_file_at = COALESCE(EXCLUDED.last_file_at, discovered.last_file_at),
                             release_files = EXCLUDED.release_files,
                             video_files = EXCLUDED.video_files,
                             book_files = EXCLUDED.book_files,
                             audio_files = EXCLUDED.audio_files,
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
                        c.get("distinct_contributors", 0), c.get("verified", False),
                        c.get("release_files"), c.get("video_files"),
                        c.get("book_files"), c.get("audio_files"),
                        _iso_or_none(c.get("last_file_at")))
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
                "tgarr_version": TGARR_VERSION,
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
                             "User-Agent": TGARR_UA},
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
    # A restart is not a reason to validate another batch: wait out what is
    # left of the interval since the last one. (A day of updates used to mean
    # a batch per restart.)
    try:
        async with db_pool.acquire() as conn:
            _last = await conn.fetchval(
                "SELECT EXTRACT(EPOCH FROM (NOW() - updated_at)) FROM config "
                "WHERE key = 'fed_validator_last'")
        if _last is not None and _last < SEEDS_INTERVAL_SEC:
            log.info("[fed-validator] last batch %d min ago — next in %d min",
                     int(_last // 60), int((SEEDS_INTERVAL_SEC - _last) // 60))
            await asyncio.sleep(SEEDS_INTERVAL_SEC - _last)
    except Exception:
        pass

    while True:
        try:
            await _heartbeat("federation_validator", f"fetching seed batch (size={SEEDS_BATCH})")
            try:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO config (key, value) VALUES ('fed_validator_last', 'x')
                           ON CONFLICT (key) DO UPDATE SET updated_at = NOW()""")
            except Exception:
                pass
            # ─── 1. Fetch a batch from central ──────────────────────────
            try:
                url = f"{REGISTRY_URL}/api/v1/seeds?batch={SEEDS_BATCH}"
                req = urllib.request.Request(url, headers={
                    "User-Agent": TGARR_UA})
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
                        # CRITICAL: validate invite via PREVIEW, not join_chat.
                        # join_chat is hard-capped ~20/day by TG and was the root
                        # cause of the account sitting in permanent join FloodWait
                        # (validator burned the whole join budget on validation).
                        # get_chat() on an invite link resolves via
                        # messages.CheckChatInvite — returns title + member count
                        # WITHOUT joining, and never sends a join request either.
                        chat = await _mtproto("get_chat",
                            lambda: app.get_chat(invite))
                    else:
                        chat = await _mtproto("resolveUsername",
                            lambda: app.get_chat(uname), username=uname)
                    members = getattr(chat, "members_count", None)
                    _cu = getattr(chat, "username", None)
                    title = getattr(chat, "title", None) or (_cu or uname)
                    real_username = _cu or uname
                    # News/TV/escort/fan-club/sticker junk = singleton-hash
                    # noise, federation value 0. The subscribe path already
                    # rejects these; the validator must too, or it just feeds
                    # central garbage seeds that every client re-validates.
                    noise_hit, noise_kw = _is_noise_title(title, real_username)
                    if noise_hit:
                        log.info("[fed-validator] NOISE-TITLE @%s kw=%r title=%s — skip",
                                 real_username, noise_kw, (title or "")[:40])
                        await asyncio.sleep(PER_SEED_DELAY_SEC)
                        continue
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
                        "tgarr_version": TGARR_VERSION,
                        "channels": verified_alive,
                    }
                    req = urllib.request.Request(
                        REGISTRY_URL + "/api/v1/contribute",
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json",
                                 "User-Agent": TGARR_UA},
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
                # Mark with sentinel -1 so we don't retry forever. (0/NULL mean
                # "not looked up yet" and are what the selector above picks.)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE messages SET file_dc = -1 WHERE id=$1", row["id"])
                log.debug("[dc-backfill] msg %s unreadable (%s) → -1",
                          row["id"], type(e).__name__)
                await asyncio.sleep(2)
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
                    dc or -1, row["id"])
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


# Files a person asked for are fetched several at a time. One at a time meant
# a 2 GB film opened earlier held every later click - a 5 MB song included -
# until it had finished. A slot is always kept for small files.
ONDEMAND_SLOTS = 3
ONDEMAND_BIG_BYTES = 200 * 1024 * 1024
ONDEMAND_BIG_SLOTS = 2
_ondemand_active: dict = {}        # message row id -> is it a big file


async def on_demand_media_downloader() -> None:
    """Process messages marked local_path='__user_queued__' by API.

    Strictly user-driven: scans only for the queue marker, no proactive
    backlog work. Up to ONDEMAND_SLOTS files at once, small ones first, at
    most ONDEMAND_BIG_SLOTS of them big.
    """
    log.info("[on-demand-media] started — processes __user_queued__ marker")
    os.makedirs(AUDIO_ROOT, exist_ok=True)
    os.makedirs(LIBRARY_ROOT, exist_ok=True)
    os.makedirs(VIDEO_ROOT, exist_ok=True)
    try:
        _n = await asyncio.to_thread(_flac_fix_existing)
        if _n:
            log.info("[on-demand-media] repaired the cover type of %d FLAC file(s) "
                     "already downloaded", _n)
    except Exception as e:
        log.warning("[on-demand-media] FLAC check: %s", e)
    while True:
        try:
            if len(_ondemand_active) >= ONDEMAND_SLOTS:
                await asyncio.sleep(1)
                continue
            big_ok = sum(1 for v in _ondemand_active.values() if v) < ONDEMAND_BIG_SLOTS
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT m.id, m.tg_chat_id, m.tg_message_id, m.media_type,
                           m.file_name, m.file_size
                    FROM messages m
                    WHERE m.local_path = '__user_queued__'
                      AND m.id <> ALL($1::bigint[])
                      AND ($2 OR COALESCE(m.file_size, 0) < $3)
                    ORDER BY (COALESCE(m.file_size, 0) >= $3), m.id DESC
                    LIMIT 1
                """, list(_ondemand_active), big_ok, ONDEMAND_BIG_BYTES)
            if not row:
                await asyncio.sleep(1 if _ondemand_active else 2)
                continue
            _ondemand_active[row["id"]] = (row["file_size"] or 0) >= ONDEMAND_BIG_BYTES
            asyncio.create_task(_ondemand_run(row))
        except Exception:
            log.exception("[on-demand-media] outer loop")
            await asyncio.sleep(10)


async def _ondemand_run(row) -> None:
    try:
        await _ondemand_fetch_one(row)
    except Exception:
        log.exception("[on-demand-media] id=%s", row["id"])
    finally:
        _ondemand_active.pop(row["id"], None)


# Opening a film starts fetching the whole file. That is right while someone
# is watching it and wrong the moment they are not: ten films opened and
# closed were ten complete downloads - bandwidth, disk, and the account's
# cross-DC allowance spent on nothing.
#
# The API stamps messages.fetch_wanted_at for as long as a player, a reader
# or a browser download is asking for the file. A fetch of a big file checks
# the stamp as it goes and stops once nobody has asked for a while; what has
# arrived is kept, and the next request continues from there instead of
# starting again. Songs and books are small: they simply finish.
# (No stamp at all = an API that does not write them: behave as before.)
ONDEMAND_WATCH_BYTES = 64 * 1024 * 1024
ONDEMAND_IDLE_SEC = int(os.environ.get("TGARR_ONDEMAND_IDLE_SEC", "150"))


async def _ondemand_wanted(msg_id: int) -> bool:
    try:
        async with db_pool.acquire() as conn:
            v = await conn.fetchval(
                """SELECT fetch_wanted_at IS NULL
                          OR fetch_wanted_at > NOW() - make_interval(secs => $2)
                   FROM messages WHERE id = $1""", msg_id, float(ONDEMAND_IDLE_SEC))
        return v is not False
    except Exception:
        return True


async def _ondemand_fetch_one(row) -> None:
    mt = row["media_type"] or "document"
    await _heartbeat("on_demand_media_downloader", f"streaming msg {row['id']} ({mt})")
    root = (AUDIO_ROOT if mt == "audio"
            else VIDEO_ROOT if mt == "video"
            else LIBRARY_ROOT)
    base = (row["file_name"] or f"item-{row['id']}")
    safe = SAFE_NAME.sub("_", base)[:180]
    target = os.path.join(root, safe)
    rel = os.path.relpath(target, "/downloads")
    expected = row["file_size"] or 0
    watched = expected >= ONDEMAND_WATCH_BYTES
    mb = 1024 * 1024

    try:
        if watched and not await _ondemand_wanted(row["id"]):
            # asked for, then left before the fetch even began
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE messages SET local_path = NULL "
                    "WHERE id = $1 AND local_path = '__user_queued__'", row["id"])
            log.info("[on-demand-media] id=%s: nobody is waiting for it any more — not fetched",
                     row["id"])
            return
        have = os.path.getsize(target) if os.path.exists(target) else 0
        if expected and have >= expected:
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE messages SET local_path=$1 WHERE id=$2", rel, row["id"])
            _drop_tail_sidecar(target)
            return                               # already here in full

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
        # continue a fetch that was stopped half-way (whole megabytes only:
        # that is the unit Telegram serves)
        start = (have // mb) * mb if (watched and mb <= have < expected) else 0
        log.info("[on-demand-media] %s id=%s → %s%s", mt, row["id"], rel,
                 f" (continuing at {start // mb} MB)" if start else "")

        async def _fetch(start: int):
            written, paused, checked = start, False, time.time()
            if not start:
                _drop_tail_sidecar(target)
            # a FLAC's metadata is held back until it is whole, so that it can
            # be repaired before a single byte is visible to the player
            hold = bytearray() if (target.lower().endswith(".flac") and not start) else None
            with open(target, "r+b" if start else "wb") as f:
                if start:
                    f.seek(start)
                    f.truncate()
                src = (_stream_file_cached(media, start=start) if start
                       else _stream_media_fast(msg, media))
                async for chunk in src:
                    if hold is not None:
                        hold += chunk
                        n = _flac_meta_len(hold)
                        if n is None and len(hold) < _FLAC_META_MAX:
                            continue
                        if n:
                            fixed = _flac_fix_meta(bytes(hold[:n]))
                            if fixed != bytes(hold[:n]):
                                log.info("[on-demand-media] id=%s: FLAC cover had no type "
                                         "(browsers refuse such a file) — repaired", row["id"])
                            chunk = fixed + bytes(hold[n:])
                        else:
                            chunk = bytes(hold)
                        hold = None
                    f.write(chunk)
                    f.flush()
                    if written == 0 and expected > 8 * mb and mt != "audio":
                        await _fetch_tail_sidecar(media, target, chunk, expected)
                    written += len(chunk)
                    if watched and time.time() - checked > 10:
                        checked = time.time()
                        if not await _ondemand_wanted(row["id"]):
                            paused = True
                            break
                if hold:
                    f.write(hold)
                    written += len(hold)
            return written, paused

        try:
            written, paused = await _fetch(start)
        except _FastPathUnavailable:
            if not start:
                raise
            written, paused = await _fetch(0)     # cannot continue this one: start over

        if paused:
            log.info("[on-demand-media] id=%s: nobody is watching — stopped at %d%% (%d of %d MB); "
                     "it continues from here when someone opens it again",
                     row["id"], written * 100 // max(expected, 1), written // mb, expected // mb)
            return
        if not expected or written >= expected:
            _drop_tail_sidecar(target)       # the file is whole: no longer needed

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


DEEP_BACKFILL_ENABLED = os.environ.get("TGARR_DEEP_BACKFILL", "true").lower() == "true"
DEEP_BACKFILL_PAGE_SIZE = int(os.environ.get("TGARR_DEEP_BACKFILL_PAGE_SIZE", "200"))
DEEP_BACKFILL_DELAY_SEC = int(os.environ.get("TGARR_DEEP_BACKFILL_DELAY_SEC", "10"))
# Channels with fewer than this many TOTAL media items (TG-reported) are
# skipped. Saves quota on pure-text/discussion channels.
DEEP_BACKFILL_MIN_MEDIA = int(os.environ.get("TGARR_DEEP_BACKFILL_MIN_MEDIA", "100"))
# Realized-yield gate: a channel that has already surfaced >= MIN_MSGS messages
# but parses < MAX_RATE of them into releases AND is photo-dominated (> PHOTO_FRAC)
# is a singleton-hash photo dump (car ads, news, escort) — federation value ~0.
# Stop spending deep-backfill paging budget on it. The high-yield channels
# (e.g. docs at 19%) are far above MAX_RATE and never tripped.
DEEP_YIELD_MIN_MSGS = int(os.environ.get("TGARR_DEEP_YIELD_MIN_MSGS", "2000"))
DEEP_YIELD_MAX_RATE = float(os.environ.get("TGARR_DEEP_YIELD_MAX_RATE", "0.005"))
DEEP_YIELD_PHOTO_FRAC = float(os.environ.get("TGARR_DEEP_YIELD_PHOTO_FRAC", "0.70"))


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

            # Purge dead EMPTY phantom rows: 0 messages (no resource data to lose),
            # not joined, and either never-subscribed/forgotten or a confirmed
            # unreadable phantom (CHAT_ID_INVALID / username gone). Keeps the
            # channels table from growing unbounded with caption-mention discovery
            # noise. Never touches rows with data or KwickPOS*.
            async with db_pool.acquire() as conn:
                gc = await conn.execute("""
                    DELETE FROM channels c
                    WHERE NOT c.is_joined
                      AND c.title NOT ILIKE 'KwickPOS%'
                      AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.channel_id = c.id)
                      AND (
                        NOT c.subscribed
                        OR c.subscribe_error ILIKE '%CHAT_ID_INVALID%'
                        OR c.subscribe_error ILIKE '%USERNAME_NOT_OCCUPIED%'
                        OR c.subscribe_error ILIKE '%not found%'
                      )
                """)
                n = int(gc.split()[-1]) if gc.startswith("DELETE") else 0
            if n:
                log.info("[dialog-gc] purged %d empty phantom rows", n)

            # Mark resource-poor channels deep-backfilled so they stop lingering
            # in the deep-backfill queue forever. The deep_backfill_worker SELECT
            # excludes channels with < DEEP_BACKFILL_MIN_MEDIA remote media items
            # (passive WHERE filter), so they're never processed AND never marked
            # done → they show "pending" indefinitely (e.g. text channels like
            # KwickPOS HQ with 4 media / 660 text msgs). Actively close them out.
            async with db_pool.acquire() as conn:
                rp = await conn.execute("""
                    UPDATE channels SET deep_backfilled=TRUE
                    WHERE NOT COALESCE(deep_backfilled, FALSE)
                      AND remote_counts_refreshed_at IS NOT NULL
                      AND (COALESCE(remote_photos,0)+COALESCE(remote_videos,0)
                           +COALESCE(remote_audio,0)+COALESCE(remote_documents,0))
                          < $1
                """, DEEP_BACKFILL_MIN_MEDIA)
                rpn = int(rp.split()[-1]) if rp.startswith("UPDATE") else 0
            if rpn:
                log.info("[dialog-gc] closed %d resource-poor channels (< %d media)",
                         rpn, DEEP_BACKFILL_MIN_MEDIA)
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
            # Multi-signal quality sweep — orthogonal to time decay.
            #
            # NOTE: ad regexes below must stay in sync with _AD_KEYWORDS_CORE
            # and _AD_KEYWORDS_PHOTO (l.~285). Duplicated rather than fetched
            # from a config table to avoid a round-trip per ingest.
            async with db_pool.acquire() as conn:
                # 1a) Core ad keywords (crypto/gambling/scams) in file_name or
                # caption — applied to ALL media types.
                r1a = await conn.execute("""
                    DELETE FROM messages
                    WHERE (
                        COALESCE(file_name, '') ~* $1
                        OR COALESCE(caption, '') ~* $1
                    )
                    AND COALESCE(local_path, '') = ''
                    AND id NOT IN (SELECT primary_msg_id FROM releases WHERE primary_msg_id IS NOT NULL)
                """, r'(\bcrypto\b|\bnft\b|\busdt\b|赌博|博彩|casino|gamble|gambling|'
                     r'投资理财|做单|赚钱|月入|代理|代充|代购|刷单|'
                     r'telegram[\s_]*bot|airdrop|'
                     r'limited[\s_]*offer|click[\s_]*here|join[\s_]*now[\s_]*free|'
                     r'earn[\s_]*money|earn[\s_]*cash|vip[\s_]*会员)')
                # 1b) Photo-promo keywords (qrcode/微信号/招代理/引流) — PHOTO
                # ONLY. These legitimately appear in Chinese marketing-book
                # EPUB titles; applying them to documents false-positives.
                r1b = await conn.execute("""
                    DELETE FROM messages
                    WHERE media_type = 'photo'
                    AND (
                        COALESCE(file_name, '') ~* $1
                        OR COALESCE(caption, '') ~* $1
                    )
                    AND COALESCE(local_path, '') = ''
                    AND id NOT IN (SELECT primary_msg_id FROM releases WHERE primary_msg_id IS NOT NULL)
                """, r'(qrcode|qr[\s_]*code|二维码|'
                     r'微信号|wechat[\s_]*id|加微信|加[\s_]*wx|'
                     r'contact[\s_]*us|联系我|联系方式|'
                     r'招代理|招商|推广|引流|福利群)')
                ads_n = ((int(r1a.split()[-1]) if r1a.startswith("DELETE") else 0) +
                         (int(r1b.split()[-1]) if r1b.startswith("DELETE") else 0))

                # 2) Tiered photo eviction by channel members_count. The client
                # has no gold_score (federation-only) so members_count is the
                # best quality proxy. Tighter for low-trust channels.
                r2a = await conn.execute("""
                    DELETE FROM messages m
                    USING channels c
                    WHERE m.channel_id = c.id
                      AND m.media_type = 'photo'
                      AND (
                        -- tiny (no members or <500): 50KB floor
                        (COALESCE(c.members_count, 0) < 500   AND m.file_size < 50000) OR
                        -- small (500-5k): 30KB
                        (c.members_count BETWEEN 500 AND 4999 AND m.file_size < 30000) OR
                        -- mid/large (>=5k): 10KB (baseline, matches ingest gate)
                        (COALESCE(c.members_count, 0) >= 5000 AND m.file_size < 10000)
                      )
                      AND COALESCE(m.local_path, '') = ''
                      AND m.id NOT IN (SELECT primary_msg_id FROM releases WHERE primary_msg_id IS NOT NULL)
                """)
                photo_n = int(r2a.split()[-1]) if r2a.startswith("DELETE") else 0

                # 2b) Non-photo micro-media — unchanged thresholds.
                r2b = await conn.execute("""
                    DELETE FROM messages
                    WHERE (
                      (media_type='video'    AND file_size < 5000000) OR
                      (media_type='audio'    AND file_size < 100000)  OR
                      (media_type='document' AND file_size < 10000)
                    )
                    AND COALESCE(local_path, '') = ''
                    AND id NOT IN (SELECT primary_msg_id FROM releases WHERE primary_msg_id IS NOT NULL)
                """)
                micro_n = (int(r2b.split()[-1]) if r2b.startswith("DELETE") else 0) + photo_n

                # 3) Cross-channel hash spam — tightened 20 → 10. Same thumb
                # in 10+ channels = forwarded promo bundle (sticker drops,
                # coordinated drops), not legitimate organic reach.
                r3 = await conn.execute("""
                    DELETE FROM messages
                    WHERE thumb_md5 IN (
                        SELECT thumb_md5 FROM messages
                        WHERE thumb_md5 IS NOT NULL
                        GROUP BY thumb_md5
                        HAVING count(DISTINCT channel_id) > 10
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


DIALOG_LEAVE_DELAY_SEC = int(os.environ.get("TGARR_DIALOG_LEAVE_DELAY_SEC", "45"))
DIALOG_LEAVE_BATCH = int(os.environ.get("TGARR_DIALOG_LEAVE_BATCH", "20"))


async def dialog_leave_worker() -> None:
    """Leave tgarr-auto-joined channels to declutter the account's dialog list
    and free join slots. Gated by config flag 'dialog_leave_active'='true' (set
    via SQL — no container restart to start/stop). NEVER leaves KwickPOS* (Tom's
    own channels). Throttled HARD: leave_chat is rate-limited and bulk join/leave
    churn re-flags the account (we just recovered from a FloodWait session kick).
    On success sets is_joined=FALSE; public channels can be re-discovered + re-
    joined later.
    """
    await asyncio.sleep(120)
    while True:
        try:
            async with db_pool.acquire() as conn:
                active = await conn.fetchval(
                    "SELECT value FROM config WHERE key='dialog_leave_active'")
            if (active or "").lower() != "true":
                await asyncio.sleep(300)
                continue
            async with db_pool.acquire() as conn:
                # ── Release SUBSCRIBED channels (no TG call, local only). tgarr
                # "subscribe" is poll-only membership we created — never Tom's
                # personal joins — so once a subscribed channel is fully crawled
                # AND fully contributed to central, stop polling it. It drops off
                # the working set; a later grab re-subscribes + rehydrates.
                unsub = await conn.execute("""
                    UPDATE channels c SET subscribed = FALSE
                    WHERE subscribed = TRUE
                      AND COALESCE(deep_backfilled, FALSE) = TRUE
                      AND title NOT ILIKE 'KwickPOS%'
                      AND COALESCE(category, '') <> 'personal'
                      AND NOT EXISTS (
                        SELECT 1 FROM messages m
                        WHERE m.channel_id = c.id AND m.file_unique_id IS NOT NULL
                          AND m.contributed_at IS NULL)
                """)
                if unsub and unsub != "UPDATE 0":
                    log.info("[dialog-leave] released (unsubscribed) %s done+contributed channels", unsub)
                # ── Leave JOINED channels. SAFETY: only channels tgarr itself
                # auto-joined (auto_joined IS TRUE) — never Tom's personal joins
                # (auto_joined NULL) and never KwickPOS. Leave once fully crawled
                # AND fully contributed, OR if flagged junk (enabled=FALSE).
                rows = await conn.fetch("""
                    SELECT id, tg_chat_id, username, title
                    FROM channels c
                    WHERE is_joined = TRUE
                      AND auto_joined IS TRUE
                      AND title NOT ILIKE 'KwickPOS%'
                      AND COALESCE(category, '') <> 'personal'
                      AND (
                        NOT COALESCE(enabled, TRUE)
                        OR (COALESCE(deep_backfilled, FALSE) = TRUE
                            AND NOT EXISTS (
                              SELECT 1 FROM messages m
                              WHERE m.channel_id = c.id AND m.file_unique_id IS NOT NULL
                                AND m.contributed_at IS NULL))
                      )
                    ORDER BY id
                    LIMIT $1
                """, DIALOG_LEAVE_BATCH)
            if not rows:
                log.info("[dialog-leave] nothing left to leave — idle")
                await _heartbeat("dialog_leave_worker", "drained — idle")
                await asyncio.sleep(3600)
                continue
            for r in rows:
                # Re-check the kill switch before EVERY leave so it can be
                # halted mid-batch (not just between batches).
                async with db_pool.acquire() as conn:
                    if ((await conn.fetchval(
                            "SELECT value FROM config WHERE key='dialog_leave_active'")
                         ) or "").lower() != "true":
                        log.info("[dialog-leave] kill switch off — stopping")
                        break
                uname = r["username"] or f"chat-{r['tg_chat_id']}"
                try:
                    await _mtproto_wait_clearance()
                    await _mtproto("leave_chat",
                                   lambda: app.leave_chat(r["tg_chat_id"]))
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE channels SET is_joined=FALSE WHERE id=$1", r["id"])
                    log.info("[dialog-leave] left @%s (%s)", uname, (r["title"] or "")[:30])
                    await _heartbeat("dialog_leave_worker", f"left @{uname}")
                except FloodWait as fw:
                    log.warning("[dialog-leave] FloodWait %ds — backing off", fw.value)
                    await asyncio.sleep(fw.value + 5)
                    break
                except (ChannelInvalid, ChannelPrivate) as e:
                    # Not actually a member / gone — clear the flag and move on.
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE channels SET is_joined=FALSE WHERE id=$1", r["id"])
                    log.info("[dialog-leave] @%s not joinable (%s) — cleared flag",
                             uname, type(e).__name__)
                except Exception as e:
                    es = str(e)
                    # Stale is_joined: account isn't actually a member (peer not
                    # in cache / left already) — leave_chat can't help, clear the
                    # flag so we stop retrying it every batch (was an infinite loop).
                    if any(s in es for s in ("Peer id invalid", "PEER_ID_INVALID",
                                             "USER_NOT_PARTICIPANT", "CHANNEL_INVALID")):
                        async with db_pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE channels SET is_joined=FALSE WHERE id=$1", r["id"])
                        log.info("[dialog-leave] @%s not a member — cleared stale flag", uname)
                    else:
                        log.warning("[dialog-leave] @%s leave error: %s", uname, es[:120])
                await asyncio.sleep(DIALOG_LEAVE_DELAY_SEC)
        except Exception:
            log.exception("[dialog-leave] outer loop")
            await asyncio.sleep(60)


DIALOG_RECONCILE_INTERVAL_SEC = int(os.environ.get("TGARR_DIALOG_RECONCILE_INTERVAL_SEC", "10800"))


async def dialog_reconcile_worker() -> None:
    """Enumerate the account's REAL Telegram dialogs and reconcile them with
    tgarr's view. is_joined was only set from live updates, so it badly
    undercounts actual memberships — tgarr auto-joined many channels (old
    aggressive-join era) that still clutter the user's TG app; the other leave
    workers only touch the few DB rows flagged is_joined and miss the rest.

    Mode comes from config key 'dialog_reconcile_mode' (hot-flippable, no
    restart) or env TGARR_DIALOG_RECONCILE; default 'off':
      report — enumerate + classify + log counts/list, leave NOTHING (safe first)
      leave  — also leave the tgarr-auto channels, throttled (FloodWait-prone)
    NEVER leaves KwickPOS or channels flagged personal (auto_joined=FALSE OR
    category='personal'). Mark personal channels auto_joined=FALSE BEFORE
    switching to leave — everything else is treated as tgarr-auto. Config key
    'dialog_reconcile_stop'='true' is a mid-run kill switch.
    """
    await asyncio.sleep(180)
    while True:
        try:
            async with db_pool.acquire() as conn:
                mode = (await conn.fetchval(
                    "SELECT value FROM config WHERE key='dialog_reconcile_mode'")
                    or os.environ.get("TGARR_DIALOG_RECONCILE", "off")).lower()
            if mode not in ("report", "leave"):
                await asyncio.sleep(600)
                continue

            await _mtproto_wait_clearance()
            leavable = []   # (chat_id, username, title)
            real = protected = 0
            async for dialog in _dialogs():
                if dialog.chat.type.name not in ("CHANNEL", "SUPERGROUP"):
                    continue
                real += 1
                chat_id = dialog.chat.id
                title = dialog.chat.title or ""
                uname = dialog.chat.username
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT id, auto_joined, category FROM channels "
                        "WHERE tg_chat_id=$1", chat_id)
                    if row:  # reconcile the stale flag against ground truth
                        await conn.execute(
                            "UPDATE channels SET is_joined=TRUE WHERE id=$1 "
                            "AND COALESCE(is_joined,FALSE)=FALSE", row["id"])
                is_kwickpos = title.upper().startswith("KWICKPOS")
                personal = bool(row and (row["auto_joined"] is False
                                         or row["category"] == "personal"))
                if is_kwickpos or personal:
                    protected += 1
                    continue
                leavable.append((chat_id, uname, title))
            log.info("[dialog-reconcile] %s: real_channels=%d protected=%d leavable=%d",
                     mode, real, protected, len(leavable))
            for cid, un, ti in leavable[:60]:
                log.info("[dialog-reconcile] LEAVABLE @%s id=%s %s",
                         un or "-", cid, ti[:34])
            await _heartbeat("dialog_reconcile_worker",
                             f"{mode}: real={real} leavable={len(leavable)} protected={protected}")

            if mode == "leave":
                left = 0
                for cid, un, ti in leavable:
                    async with db_pool.acquire() as conn:
                        if ((await conn.fetchval(
                                "SELECT value FROM config WHERE key='dialog_reconcile_stop'")
                             ) or "").lower() == "true":
                            log.info("[dialog-reconcile] kill switch — stopping")
                            break
                    try:
                        await _mtproto_wait_clearance()
                        await _mtproto("leave_chat", lambda c=cid: app.leave_chat(c))
                        async with db_pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE channels SET is_joined=FALSE WHERE tg_chat_id=$1", cid)
                        left += 1
                        log.info("[dialog-reconcile] left @%s (%s)", un or cid, ti[:30])
                    except FloodWait as fw:
                        log.warning("[dialog-reconcile] FloodWait %ds — backing off", fw.value)
                        await asyncio.sleep(fw.value + 5)
                        continue
                    except Exception as e:
                        log.warning("[dialog-reconcile] leave %s err: %s", cid, str(e)[:100])
                    await asyncio.sleep(DIALOG_LEAVE_DELAY_SEC)
                log.info("[dialog-reconcile] leave pass done: left=%d", left)
            await asyncio.sleep(DIALOG_RECONCILE_INTERVAL_SEC)
        except Exception:
            log.exception("[dialog-reconcile] outer loop")
            await asyncio.sleep(300)


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
                    SELECT c.id, c.tg_chat_id, c.username, c.title, c.subscribed,
                           c.content_category,
                           c.remote_documents, c.remote_photos, c.remote_videos,
                           COALESCE(c.deep_oldest_tg_id,
                                    (SELECT min(tg_message_id) FROM messages m
                                     WHERE m.channel_id = c.id)) AS cursor
                    FROM channels c
                    WHERE COALESCE(c.deep_backfilled, FALSE) = FALSE
                      AND c.tg_chat_id > -2000000000000  -- exclude placeholder ids
                      AND (c.account_user_id = $2 OR c.account_user_id IS NULL)
                      AND (
                        -- a followed channel the poller can currently read (one
                        -- still waiting for its first lookup cannot be paged)
                        (c.subscribed = TRUE AND c.last_polled_at IS NOT NULL
                         AND c.subscribe_error IS NULL AND c.handle_known)
                        OR
                        -- a channel the account itself is a member of
                        (c.enabled = TRUE AND c.subscribed = FALSE
                         AND COALESCE(c.is_joined, FALSE) = TRUE)
                      )
                      -- not one that just failed: see the error handling below
                      AND (c.deep_retry_after IS NULL OR c.deep_retry_after < NOW())
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
                await _heartbeat("deep_backfill_worker", "all channels deep-backfilled",
                                 quiet_for=3600)
                await asyncio.sleep(3600)  # all done, idle hourly
                continue

            uname = row["username"] or f"chat-{row['tg_chat_id']}"

            # ── Gate A: noise title/username. The subscribe/backfill paths
            # already reject news/TV/escort/fan/meme, but the deep-backfill
            # selector did NOT — so news giants (Iran International, ТСН новини,
            # Україна Новини …) slipped in and dominated the queue, the worker
            # thrashing on 100K-300K-msg channels. Disable + mark done so they
            # leave the queue for good.
            # The junk filters below (A: news/TV names, B: almost nothing that
            # parses) exist for channels that are indexed without anybody
            # having asked for them - the ones the account merely is a member
            # of. A channel somebody FOLLOWED in tgarr is their decision; the
            # filters used to unfollow it behind their back minutes later.
            user_chose = bool(row["subscribed"])
            noise_hit, noise_kw = (False, None) if user_chose else \
                _is_noise_title(row["title"], row["username"])
            if noise_hit:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE channels SET deep_backfilled=TRUE, enabled=FALSE,
                           subscribed=FALSE, category='noise', content_category='noise'
                           WHERE id=$1""", row["id"])
                log.info("[deep-backfill] NOISE-TITLE skip @%s kw=%r — disabled",
                         uname, noise_kw)
                continue

            # ── Gate B: realized-yield. A channel that has surfaced enough
            # history but parses almost nothing into releases while being
            # MEDIA-dominated (photo OR video) is singleton-hash noise: a photo
            # dump (car ads) OR keyword-less VIDEO-NEWS (BBC Persian, Soloviev —
            # news clips, no real titles). Since v0.4.82 the parser recognizes
            # real CJK/latin titles, so genuine doc/movie channels earn releases
            # (rate > MAX_RATE) and are NOT gated — only title-less news/junk
            # falls through. Honors "no news videos".
            async with db_pool.acquire() as conn:
                ys = await conn.fetchrow("""
                    SELECT count(*) AS msgs,
                           count(*) FILTER (WHERE media_type='photo') AS photos,
                           count(*) FILTER (WHERE media_type='video') AS videos,
                           (SELECT count(*) FROM releases r
                              WHERE r.primary_msg_id IN
                                (SELECT id FROM messages mm WHERE mm.channel_id=$1)) AS rels
                    FROM messages WHERE channel_id=$1
                """, row["id"])
            # Gate if SOLELY photo-dominated (photo-dump) OR SOLELY video-dominated
            # (video-news). A single media type >70% = singleton noise. Mixed
            # photo+video channels (book covers + sample clips, archives) are NOT
            # gated — protects resource channels whose value is books/audio/docs
            # that don't parse into movie/TV releases.
            if not user_chose and ys and ys["msgs"] >= DEEP_YIELD_MIN_MSGS \
                    and ys["rels"] < DEEP_YIELD_MAX_RATE * ys["msgs"] \
                    and (ys["photos"] > DEEP_YIELD_PHOTO_FRAC * ys["msgs"]
                         or ys["videos"] > DEEP_YIELD_PHOTO_FRAC * ys["msgs"]):
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE channels SET deep_backfilled=TRUE, enabled=FALSE,
                           category='noise', content_category='noise' WHERE id=$1""",
                        row["id"])
                log.info("[deep-backfill] YIELD-GATE skip @%s: %d msgs / %d rels "
                         "(%.3f%%) / %dp %dv — singleton noise (photo-dump/video-news), stop",
                         uname, ys["msgs"], ys["rels"],
                         100.0 * ys["rels"] / max(ys["msgs"], 1), ys["photos"], ys["videos"])
                continue

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
            # Decay-cutoff ONLY for genuinely time-sensitive content
            # (news/sports/chat) — old clips there are worthless. ALL media
            # archives (movies/music/books/PDF/porn-resource, incl. the 'mixed'
            # default) keep their FULL history — old books/music/films stay
            # valuable. The old per-category cutoff truncated 'mixed' archives at
            # a date and marked them "done" (e.g. @EbookPDF_Library at 2021,
            # @Bh_PM at 35%). Mis-classified news still gets caught by Gate B
            # (media-dominant + ~0 release), so dropping 'mixed' decay is safe.
            if cat in ("news", "sports", "chat"):
                hl = _DECAY_HALF_LIFE_DAYS.get(cat)
                if hl:
                    cutoff_date = _dt.now(_tz.utc) - _td(days=3 * hl)
            try:
                await _mtproto_wait_clearance()
                offset_kw = {"offset_id": cursor} if cursor else {}
                async for msg in _hist(
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
                # Gate C: permanent-unreadable errors. The account can't read
                # this channel (subscribed but never joined → peer not in cache,
                # or private/gone) and we don't aggressive-join (account safety),
                # so it will NEVER deep-backfill. Mark done so the worker stops
                # re-selecting it every cycle — 300 such phantom subs were
                # thrashing the queue at ~30s each, burning resolveUsername quota.
                # Two different things used to be treated as one:
                #   gone      - Telegram says the channel is private to us or its
                #               name no longer exists. It will never be read:
                #               marked done (and dead, so it is cleaned up).
                #   unknown   - "channel / peer invalid": this session does not
                #               (yet) hold a valid handle for the channel. That
                #               is the normal state of a followed channel before
                #               its first lookup, and of every such channel on a
                #               day the lookup allowance is spent. Marking these
                #               "done" told tgarr the channel had been read to
                #               the end when it had not been read at all - its
                #               history was then never fetched.
                # Unknown ones are put aside for a few hours instead (the reason
                # they were made "permanent" was that a channel that keeps
                # failing is picked again at once and starves the others).
                es = str(e)
                gone = isinstance(e, ChannelPrivate) or any(
                    s in es for s in ("CHANNEL_PRIVATE", "USERNAME_NOT_OCCUPIED", "USERNAME_INVALID"))
                unknown = not gone and (isinstance(e, ChannelInvalid) or any(
                    s in es for s in ("CHAT_ID_INVALID", "PEER_ID_INVALID", "CHANNEL_INVALID")))
                async with db_pool.acquire() as conn:
                    if gone:
                        await conn.execute(
                            "UPDATE channels SET deep_backfilled=TRUE, "
                            "deep_last_run_at=NOW() WHERE id=$1", row["id"])
                    else:
                        await conn.execute(
                            "UPDATE channels SET deep_last_run_at=NOW(), "
                            "deep_retry_after = NOW() + make_interval(mins => $2) WHERE id=$1",
                            row["id"], 360 if unknown else 15)
                if gone:
                    await _mark_channel_dead(row["id"], type(e).__name__)
                log.warning("[deep-backfill] @%s page error%s: %s", uname,
                            " (gone — marked done)" if gone else
                            " (not reachable right now — tried again in 6 h)" if unknown else
                            " (tried again in 15 min)", e)
                await asyncio.sleep(2)
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
                      AND c.username IS NOT NULL AND c.username <> ''
                      -- Photos carry no file name and are of no use to an
                      -- index of movies / TV / books / music, yet they were
                      -- ~63% of everything stored centrally.
                      AND COALESCE(m.media_type, '') <> 'photo'
                    ORDER BY m.id DESC
                    LIMIT {CONTRIBUTE_RESOURCES_BATCH}
                """)
            if not rows:
                # adaptive: empty queue → long heartbeat sleep
                nap = _adaptive_sleep_seconds(0)
                await _heartbeat("contribute_resources_worker",
                                 f"queue empty; sleep {nap}s", quiet_for=nap)
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
                "tgarr_version": TGARR_VERSION,
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
                             "User-Agent": TGARR_UA},
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


# ═════════════════════════════════════════════════
# "Unfollow" means: I do not want this channel here.
#
# It used to mean "stop reading it" and nothing else - or, with
# TGARR_INDEX_PURGE on, "clear it once the channel has been read to the end,
# every file has been reported to tgarr.me AND tgarr.me confirms each one".
# A channel that was unfollowed is no longer read, so it never reaches the
# end; and tgarr.me does not confirm every file. In practice an unfollowed
# channel stayed in the list, with its films and files, for good.
#
# Those rails ("never delete what tgarr.me lacks") are right for what the
# PROGRAM lets go of by itself, and they still apply there. A channel the
# USER unfollowed is their decision: its files get a few hours to be
# reported (so the federation keeps the record), then the channel's index is
# cleared and the channel leaves the list. It can be followed again from
# Discover. Kept: files saved to this machine, favourites, and anything with
# a download in progress - a channel that still holds such rows stays in the
# list, marked cleared. Private chats and KwickPOS* are never touched.
# Nothing is sent to tgarr.me about the unfollow, and nothing is removed there.
# ═════════════════════════════════════════════════
UNFOLLOW_REPORT_GRACE_HOURS = int(os.environ.get("TGARR_UNFOLLOW_REPORT_GRACE_HOURS", "6"))

_UNFOLLOW_PICK_SQL = """
    SELECT c.id, c.username, c.title FROM channels c
    WHERE c.unfollowed_at IS NOT NULL
      AND COALESCE(c.is_joined, FALSE) = FALSE
      AND COALESCE(c.subscribed, FALSE) = FALSE
      AND COALESCE(c.title, '') NOT ILIKE 'KwickPOS%'
      AND COALESCE(c.category, '') <> 'personal'
      AND (c.unfollowed_at < NOW() - make_interval(hours => $1)
           OR NOT EXISTS (SELECT 1 FROM messages m
                          WHERE m.channel_id = c.id AND m.file_unique_id IS NOT NULL
                            AND m.contributed_at IS NULL))
    ORDER BY c.unfollowed_at LIMIT 1"""

# rows the user has a stake in: a file saved here, or a favourite
_UNFOLLOW_CLEARABLE_SQL = """
    SELECT m.id FROM messages m
    WHERE m.channel_id = $1
      AND (m.local_path IS NULL OR m.local_path LIKE '\\_\\_%' ESCAPE '\\')
      AND NOT EXISTS (SELECT 1 FROM favorites f WHERE f.msg_id = m.id)
    LIMIT 5000"""


async def _unfollow_clear_once() -> "str | None":
    """Clear one batch of one unfollowed channel. Returns what was done, or None."""
    async with db_pool.acquire() as conn:
        ch = await conn.fetchrow(_UNFOLLOW_PICK_SQL, UNFOLLOW_REPORT_GRACE_HOURS)
        if not ch:
            return None
        ids = [r["id"] for r in await conn.fetch(_UNFOLLOW_CLEARABLE_SQL, ch["id"])]
    uname = ch["username"] or f"chat-{ch['id']}"
    n = await _drop_gone_messages(ch["id"], ids) if ids else 0
    if n:
        log.info("[unfollow] @%s: cleared %d index rows", uname, n)
        return f"@{uname}: cleared {n}"
    # nothing (more) could be cleared: the channel is done
    async with db_pool.acquire() as conn:
        left = await conn.fetchval("SELECT count(*) FROM messages WHERE channel_id = $1", ch["id"])
        if left == 0:
            await conn.execute("DELETE FROM channels WHERE id = $1", ch["id"])
            log.info("[unfollow] @%s: cleared, removed from the list", uname)
            return f"@{uname}: removed"
        await conn.execute(
            "UPDATE channels SET unfollowed_at = NULL, purged_at = NOW() WHERE id = $1", ch["id"])
        log.info("[unfollow] @%s: cleared; %d saved / favourite / downloading row(s) kept", uname, left)
        return f"@{uname}: cleared, kept {left}"


async def unfollow_clear_worker() -> None:
    await asyncio.sleep(90)
    while True:
        try:
            did = await _unfollow_clear_once()
            if did:
                await _heartbeat("unfollow_clear_worker", did)
                await asyncio.sleep(3)
                continue
            await _heartbeat("unfollow_clear_worker", "nothing to clear", quiet_for=300)
        except Exception as e:
            log.exception("[unfollow] %s", e)
            await _heartbeat("unfollow_clear_worker", error=str(e)[:200])
        await asyncio.sleep(300)


# ── Crawl-and-release: once a channel is contributed + released, drop its
# local index. Central is the source of truth; grab rehydrates on demand. ──
INDEX_PURGE_ENABLED = os.environ.get("TGARR_INDEX_PURGE", "false").lower() == "true"
INDEX_PURGE_INTERVAL_SEC = int(os.environ.get("TGARR_INDEX_PURGE_INTERVAL_SEC", "180"))
INDEX_PURGE_BATCH = int(os.environ.get("TGARR_INDEX_PURGE_BATCH", "2000"))


async def _instance_uuid_val():
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT value FROM config WHERE key='instance_uuid'")


async def _central_post(path: str, payload: dict, timeout: int = 120) -> dict:
    """POST gzip JSON to central, return parsed JSON. Raises on failure."""
    body_gz = gzip.compress(json.dumps(payload).encode(), 5)
    req = urllib.request.Request(
        REGISTRY_URL + path, data=body_gz,
        headers={"Content-Type": "application/json",
                 "Content-Encoding": "gzip", "Accept-Encoding": "gzip",
                 "User-Agent": TGARR_UA},
        method="POST")
    def _post():
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("content-encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return raw
    resp = await asyncio.to_thread(_post)
    return json.loads(resp.decode())


async def _central_resolve(fuids: list) -> dict:
    """Ask central which fuids it durably holds (with a usable channel pointer).
    Returns {fuid: {channel_username, msg_id, ...}}. Empty on error so the
    caller treats them as UNconfirmed and keeps the local rows — we never
    purge data central might not have."""
    if not fuids:
        return {}
    try:
        uuid_val = await _instance_uuid_val()
        result = await _central_post("/api/v1/resolve_resources",
                                     {"instance_uuid": uuid_val or "",
                                      "file_unique_ids": list(fuids)})
        return result.get("resolved", {}) or {}
    except Exception as e:
        log.warning("[central-resolve] failed: %s", e)
        return {}


async def _central_channel_resources(username: str) -> list:
    """Fetch a channel's full resource index from central (for rehydrate)."""
    if not username:
        return []
    try:
        uuid_val = await _instance_uuid_val()
        result = await _central_post("/api/v1/channel_resources",
                                     {"instance_uuid": uuid_val or "",
                                      "channel_username": username.lstrip("@"),
                                      "limit": 100000})
        return result.get("resources", []) or []
    except Exception as e:
        log.warning("[rehydrate] channel_resources @%s failed: %s", username, e)
        return []


async def _rehydrate_channel(channel_id: int, tg_chat_id: int, username: str) -> int:
    """Re-seed a purged channel's local index from central so grab/download
    works again. Central indexes by username, so private/no-username channels
    can't rehydrate (returns 0). file_dc is absent in central → starts NULL and
    backfills on first fetch. Returns rows restored."""
    resources = await _central_channel_resources(username)
    if not resources:
        return 0
    from datetime import datetime as _dt
    restored = 0
    async with db_pool.acquire() as conn:
        for r in resources:
            fuid = r.get("file_unique_id")
            msg_id = r.get("msg_id")
            if not fuid or not msg_id:
                continue
            fname = r.get("file_name")
            posted_dt = None
            if r.get("posted_at"):
                try:
                    posted_dt = _dt.fromisoformat(r["posted_at"])
                except Exception:
                    posted_dt = None
            new_id = await conn.fetchval(
                """INSERT INTO messages
                     (channel_id, tg_message_id, tg_chat_id, file_unique_id,
                      file_name, file_size, mime_type, media_type, posted_at,
                      detected_lang, contributed_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())
                   ON CONFLICT (channel_id, tg_message_id) DO NOTHING
                   RETURNING id""",
                channel_id, msg_id, tg_chat_id, fuid, fname,
                r.get("file_size"), r.get("mime_type"), r.get("media_type"),
                posted_dt, _detect_lang(fname or ""))
            if new_id:
                restored += 1
                if fname:
                    try:
                        await maybe_create_release(
                            conn, new_id, fname, r.get("file_size"), posted_dt)
                    except Exception:
                        pass
    log.info("[rehydrate] @%s restored %d rows from central", username, restored)
    return restored


async def _purge_messages(channel_id: int, msg_ids) -> int:
    """FK-safe delete of messages for one channel (downloads → releases →
    messages, one transaction). msg_ids=None purges every message in the
    channel. Messages whose release has an active/queued download are kept.
    Returns count of messages deleted."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            if msg_ids is None:
                rel = await conn.fetch(
                    "SELECT id, primary_msg_id FROM releases WHERE primary_msg_id IN "
                    "(SELECT id FROM messages WHERE channel_id=$1)", channel_id)
            else:
                rel = await conn.fetch(
                    "SELECT id, primary_msg_id FROM releases "
                    "WHERE primary_msg_id = ANY($1::bigint[])", msg_ids)
            rel_ids = [r["id"] for r in rel]
            blocked_rel = set()
            if rel_ids:
                active = await conn.fetch(
                    "SELECT DISTINCT release_id FROM downloads "
                    "WHERE release_id = ANY($1::bigint[]) "
                    "AND status IN ('pending','downloading','queued','paused')", rel_ids)
                blocked_rel = {r["release_id"] for r in active}
                purge_rel = [r for r in rel_ids if r not in blocked_rel]
                if purge_rel:
                    await conn.execute(
                        "UPDATE monitored_movies SET grabbed_release_id=NULL "
                        "WHERE grabbed_release_id = ANY($1::bigint[])", purge_rel)
                    await conn.execute(
                        "DELETE FROM downloads WHERE release_id = ANY($1::bigint[])", purge_rel)
                    await conn.execute(
                        "DELETE FROM releases WHERE id = ANY($1::bigint[])", purge_rel)
            keep_msgs = {r["primary_msg_id"] for r in rel if r["id"] in blocked_rel}
            if msg_ids is None:
                if keep_msgs:
                    res = await conn.execute(
                        "DELETE FROM messages WHERE channel_id=$1 "
                        "AND id <> ALL($2::bigint[])", channel_id, list(keep_msgs))
                else:
                    res = await conn.execute(
                        "DELETE FROM messages WHERE channel_id=$1", channel_id)
            else:
                final_ids = [m for m in msg_ids if m not in keep_msgs]
                if not final_ids:
                    return 0
                res = await conn.execute(
                    "DELETE FROM messages WHERE id = ANY($1::bigint[])", final_ids)
    try:
        return int(res.split()[-1])
    except Exception:
        return 0


async def index_purge_worker() -> None:
    """Crawl-and-release. After a channel is fully crawled, contributed to
    central, and released (left + unsubscribed), delete its local index rows
    to reclaim storage — central is the source of truth and a later grab
    re-subscribes + rehydrates from it.

    DESTRUCTIVE → gated off by default (TGARR_INDEX_PURGE). Safety rails:
      • only released channels (NOT is_joined AND NOT subscribed) + deep_backfilled
      • never KwickPOS / personal channels
      • every fuid row must already be contributed_at NOT NULL, AND central must
        CONFIRM it holds the fuid (resolve_resources) — unconfirmed rows are
        kept + retried, so we never delete data central lacks
      • messages with an active/queued download are kept
      • FK-safe order in a transaction (downloads → releases → messages)
    """
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO config (key, value) VALUES ('mtproto_caps', $1)
                   ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()""",
                json.dumps({"caps": _MTPROTO_DAILY_MAX, "bg_share": _MTPROTO_BG_SHARE}))
    except Exception:
        pass
    # The API tells people what "unfollow" will do: it has to know this.
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO config (key, value) VALUES ('index_purge', $1)
                   ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()""",
                "on" if INDEX_PURGE_ENABLED else "off")
    except Exception:
        pass
    if not INDEX_PURGE_ENABLED:
        log.info("[index-purge] TGARR_INDEX_PURGE=false — disabled")
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE channels ADD COLUMN IF NOT EXISTS purged_at timestamptz")
    except Exception as e:
        log.warning("[index-purge] add purged_at col: %s", e)
    log.info("[index-purge] started; batch=%d interval=%ds",
             INDEX_PURGE_BATCH, INDEX_PURGE_INTERVAL_SEC)
    await asyncio.sleep(300)
    while True:
        try:
            # Noise channels first: central correctly REFUSES noise on contribute
            # (noise_flagged/rejected), so resolve_resources always returns 0 and
            # the central-confirm gate below would keep them FOREVER. Noise is
            # junk we want gone, not data to preserve — central lacking a copy is
            # the desired end state, not a reason to keep it. Hard-purge locally
            # without central-confirm, and without the username gate (a private
            # noise channel is still junk). Only condition: left + has rows.
            async with db_pool.acquire() as conn:
                nz = await conn.fetchrow("""
                    SELECT c.id, c.username, c.title
                    FROM channels c
                    WHERE (c.category='noise' OR c.content_category='noise')
                      AND COALESCE(c.is_joined,FALSE)=FALSE
                      AND COALESCE(c.subscribed,FALSE)=FALSE
                      AND COALESCE(c.title,'') NOT ILIKE 'KwickPOS%'
                      AND COALESCE(c.category,'') <> 'personal'
                      AND EXISTS (SELECT 1 FROM messages m WHERE m.channel_id=c.id)
                    ORDER BY c.id
                    LIMIT 1
                """)
            if nz:
                uname = nz["username"] or f"chat-{nz['id']}"
                deleted = await _purge_messages(nz["id"], None)
                async with db_pool.acquire() as conn:
                    remaining = await conn.fetchval(
                        "SELECT count(*) FROM messages WHERE channel_id=$1", nz["id"])
                    if remaining == 0:
                        await conn.execute(
                            "DELETE FROM channels WHERE id=$1", nz["id"])
                    else:
                        await conn.execute(
                            "UPDATE channels SET purged_at=NOW() WHERE id=$1", nz["id"])
                log.info("[index-purge] @%s NOISE hard-purged %d msgs "
                         "(central refuses noise — no confirm needed)", uname, deleted)
                await _heartbeat("index_purge_worker",
                                 f"@{uname} noise-purged {deleted}")
                await asyncio.sleep(INDEX_PURGE_INTERVAL_SEC)
                continue
            async with db_pool.acquire() as conn:
                ch = await conn.fetchrow("""
                    SELECT c.id, c.username, c.title
                    FROM channels c
                    WHERE COALESCE(c.deep_backfilled,FALSE)=TRUE
                      AND COALESCE(c.is_joined,FALSE)=FALSE
                      AND COALESCE(c.subscribed,FALSE)=FALSE
                      AND COALESCE(c.title,'') NOT ILIKE 'KwickPOS%'
                      AND COALESCE(c.category,'') <> 'personal'
                      -- Central indexes resources by channel USERNAME; a no-
                      -- username (private) channel's fuids are never resolvable
                      -- on central, so resolve_resources always confirms 0 and
                      -- purging would destroy the only copy. Exclude them — they
                      -- need re-join/re-contribute, not purge.
                      AND c.username IS NOT NULL
                      AND EXISTS (SELECT 1 FROM messages m
                                  WHERE m.channel_id=c.id AND m.file_unique_id IS NOT NULL)
                      AND NOT EXISTS (SELECT 1 FROM messages m
                                  WHERE m.channel_id=c.id AND m.file_unique_id IS NOT NULL
                                    AND m.contributed_at IS NULL)
                    ORDER BY c.deep_last_run_at ASC NULLS FIRST
                    LIMIT 1
                """)
            if not ch:
                await _heartbeat("index_purge_worker", "nothing eligible to purge")
                await asyncio.sleep(3600)
                continue
            uname = ch["username"] or f"chat-{ch['id']}"
            async with db_pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, file_unique_id FROM messages
                    WHERE channel_id=$1 AND file_unique_id IS NOT NULL
                      AND contributed_at IS NOT NULL
                    LIMIT $2
                """, ch["id"], INDEX_PURGE_BATCH)
            fuids = list({r["file_unique_id"] for r in rows})
            confirmed = await _central_resolve(fuids)
            del_ids = [r["id"] for r in rows if r["file_unique_id"] in confirmed]
            if not del_ids:
                # Central confirmed none of this channel's fuids. Keep the rows
                # (never delete data central lacks) BUT advance the cursor so we
                # don't re-pick the same channel forever — the SELECT orders by
                # deep_last_run_at ASC NULLS FIRST, so bumping it sends this
                # channel to the back and lets purgeable channels run. Without
                # this, one unconfirmable channel (private/no-username, or a
                # failed contribution) livelocks the whole worker.
                log.info("[index-purge] @%s: central confirmed 0/%d fuids — keep, defer",
                         uname, len(fuids))
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE channels SET deep_last_run_at=NOW() WHERE id=$1",
                        ch["id"])
                await asyncio.sleep(INDEX_PURGE_INTERVAL_SEC)
                continue
            deleted = await _purge_messages(ch["id"], del_ids)
            log.info("[index-purge] @%s: purged %d msgs (central-confirmed %d/%d fuids)",
                     uname, deleted, len(confirmed), len(fuids))
            await _heartbeat("index_purge_worker",
                             f"@{uname} purged {deleted} (confirmed {len(confirmed)}/{len(fuids)})")
            async with db_pool.acquire() as conn:
                remaining = await conn.fetchval(
                    "SELECT count(*) FROM messages WHERE channel_id=$1 "
                    "AND file_unique_id IS NOT NULL AND contributed_at IS NOT NULL",
                    ch["id"])
            if remaining == 0:
                await _purge_messages(ch["id"], None)  # sweep leftover text/no-fuid rows
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE channels SET purged_at=NOW() WHERE id=$1", ch["id"])
                log.info("[index-purge] @%s fully purged — marked purged_at", uname)
            await asyncio.sleep(INDEX_PURGE_INTERVAL_SEC)
        except Exception:
            log.exception("[index-purge] outer loop")
            await asyncio.sleep(600)


async def _session_revoke_watcher():
    """Periodic get_me() probe. Telegram doesn't always invalidate read-only
    calls (get_chat_history etc.) immediately after a server-side logout, so
    workers can keep polling on a residual auth_key for hours. A direct
    get_me() forces auth and surfaces AuthKeyUnregistered/SessionRevoked fast.
    Wrapped through _mtproto so the central trap handles the marker+exit.
    """
    await asyncio.sleep(45)  # let backfill / startup settle past the noisy phase
    while True:
        try:
            await _mtproto("get_me", lambda: app.get_me())
        except _REVOKED_AUTH_ERRORS:
            # _mtproto already marked + os._exit; this line is unreachable
            return
        except Exception as e:
            log.debug("[revoke-watch] non-auth probe err: %s", e)
        await asyncio.sleep(60)


MEMBERSHIP_SYNC_INTERVAL_SEC = int(os.environ.get("TGARR_MEMBERSHIP_SYNC_INTERVAL_SEC", "1800"))


async def membership_sync_worker():
    """Keep channels.is_joined equal to what the Telegram account is actually
    a member of.

    is_joined was only ever switched ON (when a dialog was seen or a message
    arrived) and OFF by the leave worker. Channels the user left in the
    Telegram app stayed "JOIN" forever: in the System page, in the
    deep-backfill queue, and as a reason not to release their index.

    Source of truth: the account dialog list, main list plus the Archive
    folder (get_dialogs skips archived chats). Flags are only cleared after a
    complete, error-free enumeration that found at least one chat, so a flood
    wait or a network hiccup can never mass-clear them. This only corrects a
    flag; it never joins or leaves anything. Personal chats are safe either
    way: the leave worker acts only on auto_joined channels.
    """
    from pyrogram import raw
    await asyncio.sleep(90)
    while True:
        try:
            member: set = set()
            complete = True
            try:
                async for d in _dialogs():
                    if d.chat and d.chat.type.name in ("CHANNEL", "SUPERGROUP", "GROUP"):
                        member.add(d.chat.id)
            except Exception as e:
                complete = False
                log.info("[membership] dialog list incomplete (%s) - flags not cleared this round",
                         str(e)[:80])
            if complete:
                try:
                    r = await _mtproto("get_dialogs", lambda: app.invoke(
                        raw.functions.messages.GetDialogs(
                            offset_date=0, offset_id=0,
                            offset_peer=raw.types.InputPeerEmpty(),
                            limit=100, hash=0, folder_id=1)))
                    for c in getattr(r, "chats", []) or []:
                        if getattr(c, "left", False) or "Forbidden" in type(c).__name__:
                            continue
                        if type(c).__name__ == "Channel":
                            member.add(int(f"-100{c.id}"))
                        elif type(c).__name__ == "Chat":
                            member.add(-c.id)
                    if len(getattr(r, "dialogs", []) or []) >= 100:
                        complete = False     # archive longer than one page: do not clear
                except Exception as e:
                    complete = False
                    log.info("[membership] archive folder not readable (%s) - flags not cleared",
                             str(e)[:80])
            if member:
                ids = list(member)
                async with db_pool.acquire() as conn:
                    on = await conn.fetch(
                        """UPDATE channels SET is_joined = TRUE
                           WHERE tg_chat_id = ANY($1::bigint[])
                             AND NOT COALESCE(is_joined, FALSE)
                           RETURNING COALESCE(username, title) AS n""", ids)
                    off = []
                    if complete:
                        off = await conn.fetch(
                            """UPDATE channels SET is_joined = FALSE
                               WHERE COALESCE(is_joined, FALSE)
                                 AND tg_chat_id <> ALL($1::bigint[])
                                 AND (account_user_id = $2 OR account_user_id IS NULL)
                               RETURNING COALESCE(username, title) AS n""",
                            ids, CURRENT_USER_ID)
                if on or off:
                    log.info("[membership] account is in %d chats; now joined: +%d %s; "
                             "no longer joined: -%d %s", len(member),
                             len(on), [x["n"] for x in on][:8],
                             len(off), [x["n"] for x in off][:8])
                await _heartbeat("membership_sync_worker",
                                 f"{len(member)} chats; +{len(on)} / -{len(off)}"
                                 + ("" if complete else " (partial list, nothing cleared)"))
            else:
                await _heartbeat("membership_sync_worker", "dialog list empty - skipped")
        except Exception as e:
            log.exception("[membership] sync error: %s", e)
            await _heartbeat("membership_sync_worker", error=str(e)[:200])
        await asyncio.sleep(MEMBERSHIP_SYNC_INTERVAL_SEC)


async def audience_classifier_worker():
    """Mark adult channels as audience='nsfw' from what they actually post.

    Channels used to keep whatever audience they were created with — in
    practice 'sfw' — so adult channels filled the default (SFW) views, the
    Newznab feed and the contributions sent to the registry. This is pure SQL
    over the local index: no Telegram calls.

    One-way and conservative: only sfw/NULL → nsfw. It never touches
    blocked_csam, a manually set audience (audience_manual), personal chats or
    KwickPOS* channels, and never flips a channel back.
    Opt-out: TGARR_AUDIENCE_CLASSIFIER=false
    """
    if os.environ.get("TGARR_AUDIENCE_CLASSIFIER", "true").lower() != "true":
        log.info("[audience] TGARR_AUDIENCE_CLASSIFIER=false — disabled")
        return
    await asyncio.sleep(300 + random.uniform(0, 300))
    while True:
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    WITH stats AS (
                      SELECT channel_id, count(*) AS n,
                             count(*) FILTER (
                               WHERE COALESCE(file_name, '') ~* $1
                                  OR COALESCE(caption, '')   ~* $1) AS hits
                      FROM messages
                      WHERE file_name IS NOT NULL
                         OR (caption IS NOT NULL AND media_type IN ('video','document'))
                      GROUP BY channel_id
                    )
                    UPDATE channels c SET audience = 'nsfw'
                    FROM (SELECT c2.id, st.n, st.hits
                          FROM channels c2 LEFT JOIN stats st ON st.channel_id = c2.id) x
                    WHERE c.id = x.id
                      AND COALESCE(c.audience, 'sfw') = 'sfw'
                      AND NOT COALESCE(c.audience_manual, FALSE)
                      AND COALESCE(c.category, '') <> 'personal'
                      AND COALESCE(c.title, '') NOT ILIKE 'KwickPOS%'
                      AND ( COALESCE(c.title, '') ~* $2
                         OR ( COALESCE(x.n, 0) >= $3 AND COALESCE(x.hits, 0) >= $4
                              AND x.hits::float / x.n >= $5 ) )
                    RETURNING c.username, c.title, x.n, x.hits
                    """,
                    ADULT_RX, ADULT_TITLE_RX, ADULT_MIN_FILES, ADULT_MIN_HITS,
                    ADULT_MIN_RATIO)
            for r in rows[:40]:
                log.info("[audience] nsfw ← @%s %r (%s/%s adult-named files)",
                         r["username"], (r["title"] or "")[:40],
                         r["hits"] or 0, r["n"] or 0)
            await _heartbeat("audience_classifier_worker",
                             f"marked {len(rows)} channel(s) nsfw")
        except Exception as e:
            log.exception("[audience] classifier error: %s", e)
            await _heartbeat("audience_classifier_worker", error=str(e)[:200])
        await asyncio.sleep(6 * 3600)


async def _session_change_watcher(boot_user_id: int):
    """Detect when /login QR-changes the underlying session to a different
    TG account. Crawler can't hot-swap Pyrogram client mid-flight; on change
    we self-exit and let docker's restart policy bring us back fresh.
    """
    import sqlite3
    try:
        boot_ino = os.stat(SESSION_PATH).st_ino
    except OSError:
        boot_ino = None
    await asyncio.sleep(60)  # initial settle
    while True:
        try:
            if os.path.exists(SESSION_PATH) and os.path.getsize(SESSION_PATH) > 0:
                # /login installs a freshly authorized session by atomic
                # rename, so a re-login (even as the SAME account) shows up as
                # a new inode. We are still holding the old, now-unlinked file;
                # restart to load the new auth key.
                cur_ino = os.stat(SESSION_PATH).st_ino
                if boot_ino is not None and cur_ino != boot_ino and _session_authed():
                    log.warning("[session-watch] session file was replaced by a "
                                "new login — self-exiting to load it")
                    await asyncio.sleep(2)
                    os._exit(0)
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


async def _session_saver() -> None:
    """Write what the session has learned to disk as we go.

    Pyrogram keeps the channels it knows (their access hashes, their
    @usernames) in memory and writes them to the session file only when it is
    stopped cleanly. A container is not stopped cleanly - it is killed - so
    the file held the login and nothing else: after every restart, and so
    after every update, the session knew no channel at all and each one had
    to be looked up by @username again. That is the scarcest allowance an
    account has, spent on every upgrade by every install.
    """
    import signal

    def _commit():
        try:
            app.storage.conn.commit()
        except Exception:
            pass
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: (_commit(), os._exit(0)))
    except (NotImplementedError, RuntimeError, ValueError):
        pass
    while True:
        await asyncio.sleep(60)
        try:
            await app.storage.save()
        except Exception as e:
            log.debug("[session] save: %s", e)


async def main() -> None:
    global CURRENT_USER_ID, CURRENT_DC
    await init_db()
    # If a prior boot or live worker detected revocation, refuse to touch TG
    # until the QR re-login flow clears the marker. Stay alive (so /api/health
    # can surface the state) without restart-looping. unless-stopped + this
    # block defeats the loop without leaning on compose restart policy.
    if os.path.exists(REVOKED_MARKER):
        log.warning("[boot] %s present — TG account marked revoked. "
                    "Refusing all TG ops. Re-login via /login QR to clear.",
                    REVOKED_MARKER)
        while os.path.exists(REVOKED_MARKER):
            await asyncio.sleep(30)
        log.info("[boot] %s cleared — restarting for clean Pyrogram init",
                 REVOKED_MARKER)
        await asyncio.sleep(2)
        os._exit(0)
    await wait_for_session()
    # app.start() + initial get_me() bypass _mtproto, so a revoked auth_key
    # would normally crash main() before any watcher fires. Catch the same
    # auth-revoke errors here to write the marker + clean exit.
    try:
        await app.start()
        app.add_handler(RawUpdateHandler(on_raw_update))
        me = await app.get_me()
    except _REVOKED_AUTH_ERRORS as e:
        await _mark_revoked(type(e).__name__)
        return  # unreachable — _mark_revoked calls os._exit
    CURRENT_USER_ID = me.id
    try:
        CURRENT_DC = await app.storage.dc_id()
    except Exception:
        CURRENT_DC = 0
    log.info("connected as @%s (id=%s) dc=%s",
             me.username or "-", me.id, CURRENT_DC)
    # Persist self-user beside the session (shared volume) so the API sidebar
    # shows the real username instead of @anonymous after a restart — the API
    # has no live client to get_me, so the crawler is the authoritative source.
    try:
        import json as _json
        with open("/app/session/user_info.json", "w", encoding="utf-8") as _f:
            _json.dump({"id": me.id, "username": me.username,
                        "first_name": me.first_name, "last_name": me.last_name}, _f)
    except Exception as _e:
        log.debug("[boot] persist user_info failed: %s", _e)
    asyncio.create_task(_profile_picture(me))
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
    asyncio.create_task(_session_revoke_watcher())
    asyncio.create_task(_cancel_watcher())

    # THUMB-ONLY MODE: emergency lockdown when MTProto cross-DC FloodWait is
    # escalating. Runs only HTTPS-safe workers (thumb_downloader uses t.me
    # web scrape, no MTProto). Skips deep_backfill, fed-validator, channel
    # meta refresher, dialog watchers — anything that hits cross-DC auth.
    THUMB_ONLY_MODE = os.environ.get("TGARR_THUMB_ONLY", "false").lower() == "true"

    asyncio.create_task(_ondemand_notify_listener())  # instant thumb/preview wake
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
    asyncio.create_task(dialog_leave_worker())
    asyncio.create_task(dialog_reconcile_worker())
    asyncio.create_task(index_purge_worker())
    asyncio.create_task(unfollow_clear_worker())
    asyncio.create_task(thumb_cache_gc_worker())
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
    asyncio.create_task(audience_classifier_worker())
    asyncio.create_task(stale_index_sweeper())
    asyncio.create_task(release_judge_worker())
    asyncio.create_task(membership_sync_worker())
    asyncio.create_task(_session_saver())
    # seed_validator disabled on client: leftover from pre-federation split.

    log.info("starting backfill (limit %s/channel)...", BACKFILL_LIMIT)
    await backfill_all()
    log.info("backfill complete, switching to live listen mode")
    await asyncio.Event().wait()


if __name__ == "__main__":
    app.run(main())
