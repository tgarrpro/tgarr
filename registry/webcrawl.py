"""The registry's own slow reader of Telegram's public web preview (t.me/s).

No Telegram account, no MTProto: plain web pages, one at a time, a few
seconds apart, from one thread beside the web server. It keeps what the
registry knows about channels current without waiting for an install to
report them:

  * channels installs have reported (or reported files for): once a day
  * names waiting in seed_candidates: analysed once, the same way the bulk
    run did it (tme_validate.process_channel)
  * every other channel that is not blocked: in turn, best first

Per channel it stores numbers, not posts: members, counters, newest post,
newest file post, the score, and whether the preview still exists. New
@names and invite links seen on a page become candidates.

Off switch: REGISTRY_WEBCRAWL=0, or a file named webcrawl.off beside this one.
"""
import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import asyncpg

log = logging.getLogger("registry.webcrawl")

ENABLED = os.environ.get("REGISTRY_WEBCRAWL", "1") != "0"
OFF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webcrawl.off")
MAX_PER_DAY = int(os.environ.get("WEBCRAWL_MAX_PER_DAY", "20000"))     # pages a day, at most
BEST_SCORE = float(os.environ.get("WEBCRAWL_BEST_SCORE", "1000"))
BEST_EVERY_DAYS = int(os.environ.get("WEBCRAWL_BEST_EVERY_DAYS", "7"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_webcrawl_days (
    day              DATE PRIMARY KEY,
    pages            INTEGER NOT NULL DEFAULT 0,   -- web pages asked for
    daily_done       INTEGER NOT NULL DEFAULT 0,   -- channels installs know, refreshed
    best_done        INTEGER NOT NULL DEFAULT 0,   -- high-score channels refreshed
    tail_done        INTEGER NOT NULL DEFAULT 0,   -- the rest
    alive            INTEGER NOT NULL DEFAULT 0,
    no_preview       INTEGER NOT NULL DEFAULT 0,
    gone             INTEGER NOT NULL DEFAULT 0,
    errors           INTEGER NOT NULL DEFAULT 0,
    slowdowns        INTEGER NOT NULL DEFAULT 0,   -- times Telegram asked us to slow down
    candidates_done  INTEGER NOT NULL DEFAULT 0,
    candidates_alive INTEGER NOT NULL DEFAULT 0,
    new_names        INTEGER NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
_COUNTERS = ("pages", "daily_done", "best_done", "tail_done", "alive", "no_preview", "gone",
             "errors", "slowdowns", "candidates_done", "candidates_alive", "new_names")

_state = {"thread": None}


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def summarize(page: dict, first_msg_at=None, audience: str = "sfw", V=None) -> dict:
    """What one preview page says about a channel. `page` is tme_validate.parse_page()
    output; `first_msg_at` is the channel's oldest known post (from an earlier look)."""
    posts = page.get("posts") or []
    ids = [p["id"] for p in posts if "id" in p]
    times = [t for t in (_dt(p.get("t")) for p in posts) if t]
    file_times = [t for t in (_dt(p.get("t")) for p in posts
                              if {"doc", "video"} & set(p.get("mt") or [])) if t]
    now = datetime.now(timezone.utc)
    c = page.get("counters") or {}
    out = {
        "title": page.get("title") or None,
        "description": page.get("channel_desc") or page.get("desc") or None,
        "members": c.get("subscribers"),
        "photos": c.get("photos"), "videos": c.get("videos"),
        "files": c.get("files"), "links": c.get("links"),
        "max_msg_id": max(ids) if ids else None,
        # a page cannot hold a post from the future
        "last_msg_at": min(max(times), now) if times else None,
        "last_file_at": min(max(file_times), now) if file_times else None,
        "lifespan_days": None, "post_rate": None, "density": None, "gold": None,
    }
    first = first_msg_at or (min(times) if times and out["max_msg_id"] and out["max_msg_id"] <= 21 else None)
    if first and out["last_msg_at"]:
        out["lifespan_days"] = max(1, int((out["last_msg_at"] - first).total_seconds() / 86400))
    if out["max_msg_id"] and out["lifespan_days"]:
        out["post_rate"] = round(out["max_msg_id"] / out["lifespan_days"], 3)
    if out["max_msg_id"]:
        out["density"] = round(sum(out[k] or 0 for k in ("videos", "files", "photos", "links"))
                               / out["max_msg_id"], 4)
    if V is not None and out["lifespan_days"]:
        _kept, st = V.filter_and_dedupe(posts)
        total = max(1, st["total_raw"])
        out["gold"] = V.compute_gold(
            out["members"], out["max_msg_id"], out["lifespan_days"], out["photos"], out["videos"],
            out["files"], out["links"], 0,
            round((st["filtered_out"] + st["deduped_out"]) / total, 3),
            round(st["fwd_count"] / total, 3), audience)
    return out


def page_kind(status, body: str, V) -> str:
    """'alive' (a channel with a public preview), 'no_preview' (exists, preview
    off or not a channel), 'gone' (no such name), 'slowdown', or 'error'."""
    if status in (429, 503, 403):
        return "slowdown"
    if status != 200 or not body:
        return "error"
    if V.INFO_BLOCK_RX.search(body):
        return "alive"
    return "no_preview" if "tgme_page_title" in body else "gone"


_PAGE_EXTRA_RX = re.compile(r'<div class="tgme_page_extra">([^<]*)</div>')


def page_members(body: str):
    """Followers shown on a channel's plain page (the one without posts):
    '12 345 subscribers'. None for a person, a bot or a group ('members')."""
    m = _PAGE_EXTRA_RX.search(body or "")
    if not m or "subscriber" not in m.group(1):
        return None
    digits = re.sub(r"[^0-9]", "", m.group(1).split("subscriber")[0])
    return int(digits) if digits else None


class _Crawler:
    def __init__(self):
        import tme_validate as V          # the bulk run's analysis, reused as is
        self.V = V
        if not getattr(V.fetch, "_counted", False):
            plain = V.fetch

            def counted(url, retry=1):
                self.n["pages"] += 1
                status, body = plain(url, retry)
                if status in (429, 503, 403):
                    self.n["slowdowns"] += 1
                return status, body
            counted._counted = True
            V.fetch = counted
        self.conn = None
        self.day = ""
        self.n = dict.fromkeys(_COUNTERS, 0)
        self.daily_names: list = []
        self.queue: list = []             # [(kind, username)]
        self.flip = 0

    async def _db(self):
        if self.conn is None or self.conn.is_closed():
            self.conn = await asyncpg.connect(os.environ["DB_DSN"])
        return self.conn

    async def _save_day(self):
        conn = await self._db()
        cols = ", ".join(_COUNTERS)
        vals = ", ".join(f"${i + 2}" for i in range(len(_COUNTERS)))
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in _COUNTERS)
        await conn.execute(
            f"""INSERT INTO registry_webcrawl_days (day, {cols}) VALUES ($1::date, {vals})
                ON CONFLICT (day) DO UPDATE SET {sets}, updated_at = NOW()""",
            datetime.strptime(self.day, "%Y-%m-%d").date(), *[self.n[c] for c in _COUNTERS])

    async def _new_day(self):
        conn = await self._db()
        self.day = _today()
        row = await conn.fetchrow("SELECT * FROM registry_webcrawl_days WHERE day = $1::date",
                                  datetime.strptime(self.day, "%Y-%m-%d").date())
        self.n = {c: (row[c] if row else 0) for c in _COUNTERS}
        # the channels installs know about: reported by one, or files reported for it
        rows = await conn.fetch(
            """SELECT username FROM registry_contributor_seen
               UNION SELECT channel_username FROM registry_resource_channels""", timeout=300)
        self.daily_names = [r[0] for r in rows]
        self.queue = []
        log.info("[webcrawl] %s: %s channels are known to installs and are looked at daily",
                 self.day, len(self.daily_names))

    async def _refill(self):
        conn = await self._db()
        rows = await conn.fetch(
            """SELECT username FROM registry_channels
               WHERE username = ANY($1::text[]) AND NOT blocked
                 AND (health_checked_at IS NULL OR health_checked_at < NOW() - INTERVAL '23 hours')
               ORDER BY health_checked_at NULLS FIRST LIMIT 40""", self.daily_names)
        if rows:
            self.queue = [("daily", r[0]) for r in rows]
            return
        cand = await conn.fetch(
            """SELECT username FROM seed_candidates
               WHERE (validation_status IS NULL OR validation_status = 'pending')
                 AND invite_link IS NULL AND username NOT LIKE 'INVITE:%'
               ORDER BY added_at LIMIT 20""")
        best = await conn.fetch(
            """SELECT username FROM registry_channels
               WHERE NOT blocked AND health_status = 'alive' AND gold_score >= $1
                 AND health_checked_at < NOW() - make_interval(days => $2)
               ORDER BY health_checked_at LIMIT 20""", BEST_SCORE, BEST_EVERY_DAYS)
        tail = [] if best else await conn.fetch(
            """SELECT username FROM registry_channels
               WHERE NOT blocked AND health_status = 'alive'
                 AND health_checked_at < NOW() - INTERVAL '23 hours'
               ORDER BY health_checked_at LIMIT 20""")
        a = [("candidate", r[0]) for r in cand]
        b = [("best", r[0]) for r in best] + [("tail", r[0]) for r in tail]
        # candidates and known channels take turns
        self.queue = [x for pair in zip(a, b) for x in pair] + a[len(b):] + b[len(a):]

    async def _candidate(self, u):
        conn = await self._db()
        counts = {"alive": 0, "dead": 0, "forbidden": 0, "err": 0}
        stats = {"mentions_new": 0, "invites_new": 0, "iptv": 0}
        try:
            status, _doc = await self.V.process_channel(u, conn, counts, stats, None)
        except Exception as e:
            status = "err"
            await conn.execute(
                "UPDATE seed_candidates SET validation_status='err', validation_error=$2, "
                "validated_at=NOW(), validation_attempts=COALESCE(validation_attempts,0)+1 WHERE username=$1",
                u, f"{type(e).__name__}: {str(e)[:200]}")
        self.n["candidates_done"] += 1
        if status == "alive":
            self.n["candidates_alive"] += 1
        self.n["new_names"] += stats["mentions_new"] + stats["invites_new"]

    async def _refresh(self, kind, u):
        V, conn = self.V, await self._db()
        status, body = V.fetch(f"https://t.me/s/{u}")     # this thread only: waiting here is fine
        what = page_kind(status, body, V)
        self.n[kind + "_done"] += 1
        if what == "slowdown":
            return                                  # V.fetch waits before the next page
        if what == "error":
            self.n["errors"] += 1
            await conn.execute("UPDATE registry_channels SET health_checked_at = NOW() WHERE username = $1", u)
            return
        if what != "alive":
            self.n[what] += 1
            # a channel without a preview still shows how many follow it
            await conn.execute(
                """UPDATE registry_channels SET health_status = $2, health_checked_at = NOW(),
                          members_count = COALESCE($3, members_count) WHERE username = $1""",
                u, what, page_members(body) if what == "no_preview" else None)
            return
        self.n["alive"] += 1
        row = await conn.fetchrow(
            "SELECT first_msg_at, audience FROM registry_channels WHERE username = $1", u)
        s = summarize(V.parse_page(body), row and row["first_msg_at"],
                      (row and row["audience"]) or "sfw", V)
        await conn.execute(
            """UPDATE registry_channels SET
                 title             = COALESCE($2, title),
                 description       = COALESCE($3, description),
                 members_count     = COALESCE($4, members_count),
                 photos_count      = COALESCE($5, photos_count),
                 videos_count      = COALESCE($6, videos_count),
                 files_count       = COALESCE($7, files_count),
                 links_count       = COALESCE($8, links_count),
                 max_msg_id        = GREATEST(COALESCE(max_msg_id, 0), COALESCE($9, 0)),
                 last_msg_at       = GREATEST(last_msg_at, $10),
                 last_file_at      = GREATEST(last_file_at, $11),
                 lifespan_days     = COALESCE($12, lifespan_days),
                 post_rate_per_day = COALESCE($13, post_rate_per_day),
                 content_density   = COALESCE($14, content_density),
                 gold_score        = COALESCE($15, gold_score),
                 health_status = 'alive', health_checked_at = NOW()
               WHERE username = $1""",
            u, s["title"], s["description"], s["members"], s["photos"], s["videos"], s["files"],
            s["links"], s["max_msg_id"], s["last_msg_at"], s["last_file_at"], s["lifespan_days"],
            s["post_rate"], s["density"], s["gold"])
        mentions, invites, _iptv = V.harvest_links(body)
        for name in mentions - {u.lower()}:
            r = await conn.execute(
                "INSERT INTO seed_candidates (username, source, audience_hint, validation_status) "
                "VALUES ($1, 'tme-daily', 'sfw', 'pending') ON CONFLICT (username) DO NOTHING", name)
            self.n["new_names"] += r.endswith("1")
        for h, url in invites.items():
            r = await conn.execute(
                "INSERT INTO seed_candidates (username, source, audience_hint, validation_status, invite_link) "
                "VALUES ($1, 'tme-daily-invite', 'sfw', 'pending', $2) ON CONFLICT (username) DO NOTHING",
                f"INVITE:{h}", url)
            self.n["new_names"] += r.endswith("1")

    async def run(self):
        conn = await self._db()
        await conn.execute(SCHEMA)
        log.info("[webcrawl] started: at most %s pages a day, %.1f s or more between pages",
                 MAX_PER_DAY, self.V.DELAY_SEC)
        done = 0
        while True:
            try:
                if os.path.exists(OFF_FILE):
                    await asyncio.sleep(60)
                    continue
                if self.day != _today():
                    if self.day:
                        await self._save_day()
                    await self._new_day()
                if self.n["pages"] >= MAX_PER_DAY:
                    await self._save_day()
                    await asyncio.sleep(300)
                    continue
                if not self.queue:
                    await self._refill()
                    if not self.queue:
                        await self._save_day()
                        await asyncio.sleep(600)     # nothing is due
                        continue
                kind, u = self.queue.pop(0)
                if kind == "candidate":
                    await self._candidate(u)
                else:
                    await self._refresh(kind, u)
                done += 1
                if done % 25 == 0:
                    await self._save_day()
                if done % 500 == 0:
                    log.info("[webcrawl] today: %s", json.dumps(self.n))
                await asyncio.sleep(self.V.current_delay())
            except Exception as e:
                log.warning("[webcrawl] %s: %s - continuing in a minute", type(e).__name__, e)
                self.queue = []
                await asyncio.sleep(60)


def _thread_main():
    try:
        asyncio.run(_Crawler().run())
    except Exception:
        log.exception("[webcrawl] stopped")


def start():
    """Start the reader in its own thread (its own event loop and database
    connection), so a slow page never holds up a request to the registry."""
    if not ENABLED:
        log.info("[webcrawl] off (REGISTRY_WEBCRAWL=0)")
        return
    if _state["thread"] and _state["thread"].is_alive():
        return
    _state["thread"] = threading.Thread(target=_thread_main, name="webcrawl", daemon=True)
    _state["thread"].start()
