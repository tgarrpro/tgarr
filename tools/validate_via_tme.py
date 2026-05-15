"""Validate seed_candidates via t.me/s/<username> web preview.

Per channel: 2 HTTP fetches give complete metadata:
  1. GET t.me/s/<u>                — newest 20 posts → last_msg_ts, max_msg_id, counters, mentions, IPTV
  2. GET t.me/s/<u>?before=21      — oldest ~20 posts → first_msg_ts (channel age)

Derived: lifespan_days, post_rate_per_day, content_density, gold_score.

Save: structured JSON (NOT raw HTML), zstd-19 (no dict yet — train later on JSON corpus).
"""
import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from html import unescape
from math import log10

import asyncpg

DB_DSN = os.environ["DB_DSN"]
DELAY_SEC = float(os.environ.get("DELAY_SEC", "2.5"))
BATCH_LIMIT = int(os.environ.get("BATCH_LIMIT", "0"))

# Transparent UA: identifies tgarr, provides abuse contact. Standard bot-identification
# pattern (cf. GPTBot, Googlebot). Lets t.me distinguish us from script-kiddie scraping
# and reach us if they need to negotiate rate or disallow paths.
UA = "tgarr.me-bot/0.4.0 (+https://tgarr.me; mailto:abuse@tgarr.me)"

# Off-peak slowdown: UTC 14:00-22:00 = US business day + EU evening = t.me peak load.
# Double the delay during that window to lighten footprint when they're busy.
def current_delay():
    h = datetime.now(timezone.utc).hour
    peak = 14 <= h < 22
    base = DELAY_SEC * (2.0 if peak else 1.0)
    return base + random.uniform(0, base)  # jitter 0–100%

# Backoff state — 429/503/403 = "TG asks us to slow down". Respect it.
_consec_block = 0
_block_until_ts = 0.0
def note_block_response(status):
    """Called on any 4xx/5xx-rate-limit response. Pauses + escalates on repeats."""
    global _consec_block, _block_until_ts
    if status in (429, 503, 403):
        _consec_block += 1
        pause = 300 if _consec_block < 3 else 1800
        _block_until_ts = max(_block_until_ts, time.time() + pause)
        print(f"  [polite] got {status} (#{_consec_block} consec) — sleeping {pause}s",
              file=sys.stderr)
    elif 200 <= status < 400:
        _consec_block = 0
def wait_if_blocked():
    remaining = _block_until_ts - time.time()
    if remaining > 0:
        time.sleep(remaining)

CACHE_ROOT = os.environ.get("TME_CACHE_ROOT", "/var/www/tgarr.me/app/_data/tme_cache")
os.makedirs(CACHE_ROOT, exist_ok=True)

# ---------- regexes ----------
OG_TITLE_RX  = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"')
OG_DESC_RX   = re.compile(r'<meta\s+property="og:description"\s+content="([^"]*)"')
INFO_BLOCK_RX = re.compile(r'<div class="tgme_channel_info"', re.I)
COUNTER_RX   = re.compile(
    r'<div\s+class="tgme_channel_info_counter">\s*'
    r'<span\s+class="counter_value">([^<]+)</span>\s*'
    r'<span\s+class="counter_type">([^<]+)</span>', re.S)
DESC_RX      = re.compile(r'<div\s+class="tgme_channel_info_description"[^>]*>(.*?)</div>', re.S)

# Per-message extraction (one .tgme_widget_message_wrap per post)
MSG_WRAP_RX  = re.compile(r'<div\s+class="tgme_widget_message_wrap[^"]*"[^>]*>(.+?)</div>\s*</div>\s*</div>', re.S)
MSG_DATA_POST_RX = re.compile(r'data-post="[^/]+/(\d+)"')
MSG_TIME_RX  = re.compile(r'<time[^>]+datetime="([^"]+)"')
MSG_TEXT_RX  = re.compile(r'<div\s+class="tgme_widget_message_text[^"]*"[^>]*>(.+?)</div>', re.S)
MSG_VIEWS_RX = re.compile(r'<span\s+class="tgme_widget_message_views">([^<]+)</span>')
MSG_FWD_RX   = re.compile(r'<a[^>]+class="tgme_widget_message_forwarded_from_name"[^>]*href="https?://t\.me/([A-Za-z][A-Za-z0-9_]{4,31})"')
MSG_STICKER_RX = re.compile(r'tgme_widget_message_sticker')
MSG_SERVICE_RX = re.compile(r'tgme_widget_message_service')
MSG_MEDIA_SRC_RX  = re.compile(r'\bsrc="(https?://[^"]+cdn[1-9]?\.cdn-telegram\.org/[^"]+)"')
MSG_MEDIA_BG_RX   = re.compile(r"background-image:url\(['\"]?(https?://[^'\")]+cdn[1-9]?\.cdn-telegram\.org/[^'\")]+)")
MSG_VIDEO_SRC_RX  = re.compile(r'<video[^>]+src="(https?://[^"]+)"')
MSG_VIDEO_DATA_RX = re.compile(r'\bdata-src="(https?://[^"]+)"')
MSG_PHOTO_WRAP_RX = re.compile(r'tgme_widget_message_photo_wrap')
MSG_VIDEO_WRAP_RX = re.compile(r'tgme_widget_message_video_wrap|tgme_widget_message_video_player')
MSG_DOC_WRAP_RX   = re.compile(r'tgme_widget_message_document')
MSG_VOICE_RX      = re.compile(r'tgme_widget_message_voice')
MSG_POLL_RX       = re.compile(r'tgme_widget_message_poll')
MSG_TOO_BIG_RX    = re.compile(r'message_media_not_supported_label')
MSG_DURATION_RX   = re.compile(r'<time[^>]+class="message_video_duration[^"]*"[^>]*>([^<]+)</time>')
MSG_LINK_PREVIEW_HREF_RX = re.compile(r'<a[^>]+class="[^"]*tgme_widget_message_link_preview[^"]*"[^>]+href="(https?://[^"]+)"')
MSG_EXTLINK_RX    = re.compile(r'<a[^>]+href="(https?://[^"]+)"[^>]*target="_blank"')
TAG_STRIP_RX = re.compile(r'<[^>]+>')

# Layer-2 harvest from posts
TRAILING = r"[/?#\s\"<>),\]\}\x27|]"
MENTION_RX = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
TME_URL_USERNAME_RX = re.compile(
    r"(?:https?://)?t(?:elegram)?\.(?:me|dog)/"
    r"(?!s/|joinchat/|\+|addstickers/|addtheme/|addemoji/|share/|setlanguage/|boost/|c/|iv\b)"
    r"([A-Za-z][A-Za-z0-9_]{4,31})(?=" + TRAILING + r"|$)", re.I)
INVITE_RX = re.compile(
    r"(?:https?://)?t(?:elegram)?\.(?:me|dog)/(?:\+|joinchat/)([A-Za-z0-9_-]{16,})", re.I)
IPTV_PATTERNS = [
    ("m3u_url", re.compile(r"https?://[A-Za-z0-9.\-]+(?::\d+)?/[^\s\"<>]*\.m3u8?\b[^\s\"<>]*", re.I)),
    ("xtream",  re.compile(r"https?://[A-Za-z0-9.\-]+(?::\d+)?/(?:get\.php|player_api\.php)\?[^\s\"<>]*username=[^\s&\"<>]+[^\s\"<>]*", re.I)),
]
SKIP_GENERIC = {"media", "context", "graph", "share", "joinchat"}
USERNAME_RX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

# ---------- counter parsing ----------
def parse_count(s):
    """3.5K -> 3500, 1.2M -> 1200000, 850 -> 850."""
    s = (s or "").strip().replace(",", "").replace(" ", "")
    if not s:
        return None
    mult = 1
    if s[-1].lower() == "k":
        mult = 1000; s = s[:-1]
    elif s[-1].lower() == "m":
        mult = 1_000_000; s = s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None

# ---------- compression ----------
_compressor = None
def get_compressor():
    global _compressor
    if _compressor is None:
        import zstandard
        # No dict yet — will train on JSON corpus later. zstd-19 alone hits ~5x on JSON.
        _compressor = zstandard.ZstdCompressor(level=19)
    return _compressor

def save_json(username, doc):
    """Save extracted doc as <username>.json.zst sharded by 2-char prefix."""
    shard = username[:2].lower()
    d = os.path.join(CACHE_ROOT, shard)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{username}.json.zst")
    blob = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(p, "wb") as f:
        f.write(get_compressor().compress(blob))

# ---------- HTTP ----------
def fetch(url, retry=1):
    wait_if_blocked()
    last_err = None
    for attempt in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.7",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=20) as r:
                note_block_response(r.status)
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    body = gzip.decompress(body)
                return r.status, body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            note_block_response(e.code)
            return e.code, ""
        except Exception as e:
            last_err = e
            if attempt < retry:
                time.sleep(2)
    return None, str(last_err)

# ---------- parsing ----------
EMOJI_HEAVY_RX = re.compile(r'[\U0001F000-\U0001FFFF☀-➿]')
PROMO_INVITE_RX = re.compile(r't\.me/\+[A-Za-z0-9_-]{16,}|t\.me/joinchat/[A-Za-z0-9_-]{16,}', re.I)

def tg_html_to_md(html):
    """Convert TG message text-div HTML to clean markdown.
    Handles: b/strong, i/em, s/del, code, pre, a, br, tg-emoji. Strips other tags."""
    s = html
    # Links FIRST (preserve href + visible text)
    s = re.sub(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
               lambda m: f"[{m.group(2)}]({m.group(1)})", s, flags=re.S)
    s = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', s, flags=re.S)
    s = re.sub(r'<(?:b|strong)>(.*?)</(?:b|strong)>', r'**\1**', s, flags=re.S)
    s = re.sub(r'<(?:i|em)>(.*?)</(?:i|em)>', r'*\1*', s, flags=re.S)
    s = re.sub(r'<(?:s|del)>(.*?)</(?:s|del)>', r'~~\1~~', s, flags=re.S)
    s = re.sub(r'<pre[^>]*>(.*?)</pre>', r'```\n\1\n```', s, flags=re.S)
    s = re.sub(r'<code[^>]*>(.*?)</code>', r'`\1`', s, flags=re.S)
    s = re.sub(r'<br\s*/?>', '\n', s)
    s = TAG_STRIP_RX.sub('', s)
    s = unescape(s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

def parse_post_raw(block):
    """Extract one post as markdown body + metadata flags. Does NOT drop anything yet."""
    p = {}
    m = MSG_DATA_POST_RX.search(block)
    if m: p["id"] = int(m.group(1))
    m = MSG_TIME_RX.search(block)
    if m: p["t"] = m.group(1)
    m = MSG_VIEWS_RX.search(block)
    if m: p["v"] = m.group(1).strip()
    m = MSG_FWD_RX.search(block)
    if m: p["fwd"] = m.group(1).lower()

    # Media TYPE flags (independent of URL availability — TG hides URLs for big media)
    mt = []
    if MSG_PHOTO_WRAP_RX.search(block): mt.append("photo")
    if MSG_VIDEO_WRAP_RX.search(block): mt.append("video")
    if MSG_DOC_WRAP_RX.search(block):   mt.append("doc")
    if MSG_VOICE_RX.search(block):      mt.append("voice")
    if MSG_POLL_RX.search(block):       mt.append("poll")
    if mt: p["mt"] = mt
    if MSG_TOO_BIG_RX.search(block):    p["too_big"] = True
    m = MSG_DURATION_RX.search(block)
    if m: p["dur"] = m.group(1).strip()
    if MSG_STICKER_RX.search(block): p["_sticker"] = True
    if MSG_SERVICE_RX.search(block): p["_service"] = True

    # Build MD body: prepend media refs (URLs are info, even if image content not stored), then text.
    parts = []
    photos = set(MSG_MEDIA_SRC_RX.findall(block)) | set(MSG_MEDIA_BG_RX.findall(block))
    for url in sorted(photos)[:8]:
        parts.append(f"![photo]({url})")
    videos = set(MSG_VIDEO_SRC_RX.findall(block))
    videos.update(u for u in MSG_VIDEO_DATA_RX.findall(block) if "cdn-telegram" in u)
    for url in sorted(videos)[:4]:
        dur = f" ({p['dur']})" if p.get('dur') else ""
        parts.append(f"![video{dur}]({url})")
    if p.get("too_big") and "video" in (p.get("mt") or []) and not videos:
        parts.append(f"[video too big — fetch via TG client]"
                     + (f" duration: {p['dur']}" if p.get('dur') else ""))
    # Text body in MD
    m = MSG_TEXT_RX.search(block)
    if m:
        md_text = tg_html_to_md(m.group(1))
        if md_text: parts.append(md_text)
    # Link-preview (external article/video card)
    for url in MSG_LINK_PREVIEW_HREF_RX.findall(block):
        if not any(s in url for s in ("//t.me/", "telesco.pe", "cdn-telegram.org")):
            parts.append(f"→ {url}")
    if parts:
        p["md"] = "\n\n".join(parts)[:3000]
    return p

MD_MEDIA_STRIP_RX = re.compile(r'!\[[^\]]*\]\([^)]+\)\s*\n?')
MD_LINKARROW_RX   = re.compile(r'^→ \S+\s*\n?', re.M)
MD_LINK_INLINE_RX = re.compile(r'\[([^\]]+)\]\([^)]+\)')

def md_text_only(md):
    """Strip media refs and arrow-links from MD, leaving plain text portion."""
    if not md: return ""
    s = MD_MEDIA_STRIP_RX.sub('', md)
    s = MD_LINKARROW_RX.sub('', s)
    # Collapse [text](url) → text for plain-text view
    s = MD_LINK_INLINE_RX.sub(r'\1', s)
    return s.strip()

def is_valuable_post(p):
    """Drop spam: sticker-only, service msgs, thin forwards, emoji-only,
    multi-invite promo blasts. Keep real text + media-rich posts."""
    if p.get("_service"): return False
    md = p.get("md", "")
    text = md_text_only(md)
    has_media = bool(p.get("mt"))  # mt = media type tag (photo/video/doc/voice/poll)
    fwd = p.get("fwd")

    if p.get("_sticker") and not text: return False
    if not text and not has_media: return False
    if fwd and len(text) < 30 and not has_media: return False

    if text and len(text) < 200:
        emoji_chars = len(EMOJI_HEAVY_RX.findall(text))
        alphanum_chars = sum(1 for c in text if c.isalnum())
        if emoji_chars >= 3 and alphanum_chars < 10:
            return False

    if text and len(text) < 300 and len(PROMO_INVITE_RX.findall(text)) >= 2:
        return False

    return True

def clean_post(p):
    """Strip internal _ flags before persisting."""
    return {k: v for k, v in p.items() if not k.startswith("_")}

def post_sig_hex(p):
    """16-char hex hash of post text. CDN URLs are channel-specific (each
    channel re-uploads its own copy, getting different URLs) — hash text only
    so the same content forwarded across N channels gets the same signature."""
    text = md_text_only(p.get("md") or "")[:500].strip().lower()
    if not text: return None
    h = hashlib.blake2b(digest_size=8)
    h.update(text.encode("utf-8"))
    return h.hexdigest()

def dedupe_posts(posts):
    """Drop within-channel reposts: same text body = bot repost. Keep one copy."""
    seen = set()
    kept = []
    dup_count = 0
    for p in posts:
        key = md_text_only(p.get("md") or "")[:300].strip().lower()
        if not key:
            kept.append(p); continue
        if key in seen:
            dup_count += 1
            continue
        seen.add(key)
        kept.append(p)
    return kept, dup_count, len(posts)

def parse_page(html_body):
    """Return dict: {title, desc, counters{}, posts[]}. Empty posts = forbidden/dead."""
    out = {"title": "", "desc": "", "counters": {}, "posts": []}
    m = OG_TITLE_RX.search(html_body)
    if m: out["title"] = unescape(m.group(1))
    m = OG_DESC_RX.search(html_body)
    if m: out["desc"] = unescape(m.group(1))
    for val, kind in COUNTER_RX.findall(html_body):
        out["counters"][kind.strip().lower()] = parse_count(val)
    m = DESC_RX.search(html_body)
    if m:
        d = TAG_STRIP_RX.sub(" ", m.group(1))
        d = unescape(re.sub(r"\s+", " ", d)).strip()
        if d: out["channel_desc"] = d[:1000]
    for wrap in MSG_WRAP_RX.finditer(html_body):
        p = parse_post_raw(wrap.group(1))
        if p: out["posts"].append(p)
    return out

def filter_and_dedupe(posts):
    """Apply value filter + within-channel dedup. Returns (kept, stats)."""
    total = len(posts)
    fwd_count = sum(1 for p in posts if p.get("fwd"))
    valuable = [p for p in posts if is_valuable_post(p)]
    deduped, dup_count, _ = dedupe_posts(valuable)
    # Tag each surviving post with stable content hash for future cross-channel analysis
    for p in deduped:
        h = post_sig_hex(p)
        if h: p["h"] = h
    return [clean_post(p) for p in deduped], {
        "total_raw": total,
        "filtered_out": total - len(valuable),
        "deduped_out": dup_count,
        "kept": len(deduped),
        "fwd_count": fwd_count,
    }

def harvest_links(html_body):
    """Pull @mentions, t.me/<u>, t.me/+invite, IPTV urls from page body."""
    mentions = set(m.group(1).lower() for m in MENTION_RX.finditer(html_body))
    mentions |= set(m.group(1).lower() for m in TME_URL_USERNAME_RX.finditer(html_body))
    mentions = {u for u in mentions if USERNAME_RX.match(u) and u not in SKIP_GENERIC}
    invites = {m.group(1): "https://t.me/+" + m.group(1) for m in INVITE_RX.finditer(html_body)}
    iptv = []
    for kind, rx in IPTV_PATTERNS:
        iptv.extend((kind, m.group(0)) for m in rx.finditer(html_body))
    return mentions, invites, iptv

def compute_gold(subs, max_msg_id, lifespan_days, photos, videos, files, links,
                 iptv_count, spam_ratio=0.0, forward_ratio=0.0, audience=None):
    """Gold = downloadable-resource channels + IPTV streams + NSFW resources.
    Mainstream news (high-post-rate + low files + photos-as-preview-only) is
    SUPPRESSED — Tom can find CNN/BBC anywhere; tgarr's job is long-tail resources.

    Re-weighted 2026-05-13 after seeing top 20 dominated by news outlets."""
    if not max_msg_id or max_msg_id < 5: return 0.0
    if not lifespan_days or lifespan_days < 30: return 0.0  # <30d = likely new bot

    videos = videos or 0
    files  = files or 0
    photos = photos or 0
    iptv   = iptv_count or 0

    # Resource score — files = gold (no cap), IPTV = max gold.
    # Videos/photos CAPPED — news channels accumulate 10K+ media just from preview
    # cards; capping prevents that volume from drowning out genuine resource curators.
    # 2026-05-15: Removed iptv_bonus. m3u8 URLs in caption text point to EXTERNAL
    # CDNs (yzzy/lzcdn/etc.) — they don't make a channel valuable to tgarr's
    # "TG-as-CDN" mission (bdpd8 had 0 TG videos but 19 caption-m3u8 → 9500
    # phantom points → false gold). iptv_count is still passed in for future
    # channel_type classification, but NOT in gold_score.
    resources = (
        files * 100 +
        min(videos, 200) * 2 +
        min(photos, 200) * 0.1
    )
    base = resources
    if base < 100: return 0.0

    # Post-rate signal: high-rate channels are ~always news/bots/aggregators.
    # Curated resource channels post 1-5/day. Real "gold" rarely exceeds 10/day.
    post_rate = max_msg_id / max(1, lifespan_days)
    if   post_rate > 30: rate_mult = 0.02  # mainstream news/bot — strongly suppress
    elif post_rate > 10: rate_mult = 0.1   # heavy news
    elif post_rate > 5:  rate_mult = 0.3
    elif post_rate > 2:  rate_mult = 0.8
    elif post_rate > 0.3: rate_mult = 1.8  # curated sweet spot (1/day-ish)
    elif post_rate > 0.05: rate_mult = 1.2 # slow but alive
    else: rate_mult = 0.4                  # nearly dormant

    # Subscriber bell curve: peaks at ~10K (the sweet spot for resource channels —
    # active enough to maintain, not so big they get DMCA'd). Above 1M = mainstream-news
    # zone, drops sharply.
    s = subs or 0
    if s < 50:
        sub_factor = 0.3
    elif s > 5_000_000:
        sub_factor = 0.4
    else:
        # Distance in log10 from peak at 10K (= log10(10000)=4)
        ls = log10(max(1, s))
        sub_factor = max(0.3, 1.5 - abs(ls - 4) / 2.5)  # peak 1.5 at s=10K

    # Age: prefer multi-year-proven channels for resources
    age_factor = min(1.0, lifespan_days / 365.0)

    # NSFW bonus — Tom said NSFW resource channels are equal-class gold
    audience_mult = 1.3 if audience == "nsfw" else 1.0

    # Quality multiplier — within-channel spam + forwarder penalty
    quality_mult = max(0.05, (1.0 - spam_ratio) * (1.0 - 0.5 * forward_ratio))

    return round(base * sub_factor * age_factor * rate_mult *
                 audience_mult * quality_mult, 3)

# ---------- per-channel ----------
async def process_channel(u, conn, counts, harvest_stats, sem):
    """Returns (status, meta_doc)."""
    # PAGE 1: newest
    s1, body1 = fetch(f"https://t.me/s/{u}")
    if s1 != 200 or not body1:
        counts["err"] += 1
        await conn.execute(
            "UPDATE seed_candidates SET validation_status='err', validation_error=$2, "
            "validated_at=NOW(), validation_attempts=validation_attempts+1 WHERE username=$1",
            u, f"http {s1}")
        return "err", None
    if not INFO_BLOCK_RX.search(body1):
        # No channel header (private/bot/group/missing/IV-only)
        counts["forbidden"] += 1
        await conn.execute(
            "UPDATE seed_candidates SET validation_status='forbidden', validated_at=NOW(), "
            "validation_attempts=validation_attempts+1 WHERE username=$1", u)
        return "forbidden", None

    page1 = parse_page(body1)
    mentions, invites, iptv = harvest_links(body1)
    post_ids = [p["id"] for p in page1["posts"] if "id" in p]
    post_times = [p["t"] for p in page1["posts"] if "t" in p]
    max_msg_id = max(post_ids) if post_ids else None
    last_msg_at = max(post_times) if post_times else None
    page1_min_id = min(post_ids) if post_ids else None

    # PAGE 2: oldest (only if there's history beyond what page1 shows)
    page_old = None
    first_msg_at = None
    if page1_min_id and page1_min_id > 21:
        # Channel has > 20 messages — fetch oldest batch
        s2, body2 = fetch(f"https://t.me/s/{u}?before=21")
        if s2 == 200 and body2:
            page_old = parse_page(body2)
            old_times = [p["t"] for p in page_old["posts"] if "t" in p]
            old_ids = [p["id"] for p in page_old["posts"] if "id" in p]
            if old_times:
                first_msg_at = min(old_times)
            # also harvest from oldest page
            m_old, i_old, ip_old = harvest_links(body2)
            mentions |= m_old
            invites.update(i_old)
            iptv.extend(ip_old)
    else:
        # All messages fit on page 1 (small channel) → first = oldest on page 1
        if post_times:
            first_msg_at = min(post_times)

    # Parse ISO strings → datetime objects (asyncpg needs datetime for timestamptz)
    def _dt(s):
        if not s: return None
        try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, AttributeError): return None

    first_msg_at = _dt(first_msg_at)
    last_msg_at  = _dt(last_msg_at)

    lifespan_days = None
    if first_msg_at and last_msg_at:
        lifespan_days = max(1, int((last_msg_at - first_msg_at).total_seconds() / 86400))

    post_rate = None
    if max_msg_id and lifespan_days:
        post_rate = round(max_msg_id / lifespan_days, 3)

    c = page1["counters"]
    subs    = c.get("subscribers")
    photos  = c.get("photos")
    videos  = c.get("videos")
    files   = c.get("files")
    links_  = c.get("links")
    media_total = (photos or 0) + (videos or 0) + (files or 0)
    content_density = None
    if max_msg_id and max_msg_id > 0:
        content_density = round(((videos or 0) + (files or 0) + (photos or 0) + (links_ or 0)) / max_msg_id, 4)
    # Apply spam filter + within-channel dedup BEFORE saving
    recent_clean, recent_stats = filter_and_dedupe(page1["posts"])
    oldest_clean, oldest_stats = filter_and_dedupe(page_old["posts"] if page_old else [])
    sample_total = recent_stats["total_raw"] + oldest_stats["total_raw"]
    filtered_out = recent_stats["filtered_out"] + oldest_stats["filtered_out"]
    deduped_out = recent_stats["deduped_out"] + oldest_stats["deduped_out"]
    fwd_count = recent_stats["fwd_count"] + oldest_stats["fwd_count"]
    kept_total = recent_stats["kept"] + oldest_stats["kept"]
    spam_ratio = round((filtered_out + deduped_out) / max(1, sample_total), 3)
    forward_ratio = round(fwd_count / max(1, sample_total), 3)

    # Look up audience hint (NSFW vs SFW) to factor into gold
    aud_row = await conn.fetchrow(
        "SELECT audience_hint FROM seed_candidates WHERE username=$1", u)
    audience = (aud_row and aud_row["audience_hint"]) or "sfw"
    gold = compute_gold(subs, max_msg_id, lifespan_days, photos, videos, files, links_,
                        len(iptv), spam_ratio, forward_ratio, audience)

    # Save only if channel has surviving valuable content OR resources in counters.
    # Pure-spam channels (all posts filtered out, no resources) → DB row only, no cache.
    has_resources = (videos or 0) + (files or 0) > 0
    save_cache = kept_total > 0 or has_resources
    if save_cache:
        doc = {
            "u": u, "fetched_at": datetime.now(timezone.utc).isoformat(),
            "title": page1["title"], "og_desc": page1["desc"],
            "channel_desc": page1.get("channel_desc", ""),
            "counters": page1["counters"],
            "posts_recent": recent_clean,
            "posts_oldest": oldest_clean,
            "metrics": {
                "max_msg_id": max_msg_id,
                "first_msg_at": first_msg_at.isoformat() if first_msg_at else None,
                "last_msg_at":  last_msg_at.isoformat()  if last_msg_at  else None,
                "lifespan_days": lifespan_days,
                "post_rate_per_day": post_rate, "content_density": content_density,
                "gold_score": gold, "iptv_count": len(iptv),
                "sample_total": sample_total, "sample_kept": kept_total,
                "spam_ratio": spam_ratio, "forward_ratio": forward_ratio,
            },
        }
        save_json(u, doc)

    # Layer-2 harvest insert
    new_m_added = 0; new_inv = 0; new_iptv = 0
    for cand in mentions - {u}:
        r = await conn.execute(
            "INSERT INTO seed_candidates (username, source, audience_hint, validation_status) "
            "VALUES ($1, 'tme-harvest', 'sfw', 'pending') ON CONFLICT (username) DO NOTHING", cand)
        if r.endswith("1"): new_m_added += 1
    for h_, url in invites.items():
        r = await conn.execute(
            "INSERT INTO seed_candidates (username, source, audience_hint, validation_status, invite_link) "
            "VALUES ($1, 'tme-harvest-invite', 'sfw', 'pending', $2) ON CONFLICT (username) DO NOTHING",
            f"INVITE:{h_}", url)
        if r.endswith("1"): new_inv += 1
    for kind, raw in iptv:
        r = await conn.execute(
            "INSERT INTO iptv_sources (kind, raw_match, channel_username) VALUES ($1,$2,$3) "
            "ON CONFLICT (kind, raw_match) DO UPDATE SET hit_count=iptv_sources.hit_count+1, last_seen_at=NOW()",
            kind, raw, u)
        new_iptv += 1
    harvest_stats["mentions_new"] += new_m_added
    harvest_stats["invites_new"] += new_inv
    harvest_stats["iptv"] += new_iptv

    # UPSERT registry_channels
    await conn.execute("""
        INSERT INTO registry_channels (
            username, title, description, members_count,
            photos_count, videos_count, files_count, links_count, media_count,
            first_msg_at, last_msg_at, max_msg_id, lifespan_days,
            post_rate_per_day, content_density, gold_score,
            audience, health_status, health_checked_at, seeded,
            quality_signals
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,'sfw','alive',NOW(),TRUE,$17::jsonb)
        ON CONFLICT (username) DO UPDATE SET
          title              = COALESCE(EXCLUDED.title, registry_channels.title),
          description        = COALESCE(EXCLUDED.description, registry_channels.description),
          members_count      = GREATEST(COALESCE(registry_channels.members_count,0), COALESCE(EXCLUDED.members_count,0)),
          photos_count       = COALESCE(EXCLUDED.photos_count, registry_channels.photos_count),
          videos_count       = COALESCE(EXCLUDED.videos_count, registry_channels.videos_count),
          files_count        = COALESCE(EXCLUDED.files_count,  registry_channels.files_count),
          links_count        = COALESCE(EXCLUDED.links_count,  registry_channels.links_count),
          media_count        = GREATEST(COALESCE(registry_channels.media_count,0), COALESCE(EXCLUDED.media_count,0)),
          first_msg_at       = LEAST(COALESCE(registry_channels.first_msg_at, EXCLUDED.first_msg_at), COALESCE(EXCLUDED.first_msg_at, registry_channels.first_msg_at)),
          last_msg_at        = GREATEST(COALESCE(registry_channels.last_msg_at, EXCLUDED.last_msg_at), COALESCE(EXCLUDED.last_msg_at, registry_channels.last_msg_at)),
          max_msg_id         = GREATEST(COALESCE(registry_channels.max_msg_id,0), COALESCE(EXCLUDED.max_msg_id,0)),
          lifespan_days      = EXCLUDED.lifespan_days,
          post_rate_per_day  = EXCLUDED.post_rate_per_day,
          content_density    = EXCLUDED.content_density,
          gold_score         = EXCLUDED.gold_score,
          quality_signals    = registry_channels.quality_signals || EXCLUDED.quality_signals,
          health_status      = 'alive',
          health_checked_at  = NOW(),
          seeded             = TRUE""",
        u, page1["title"] or None,
        page1.get("channel_desc") or page1["desc"] or None,
        subs,
        photos, videos, files, links_, media_total,
        first_msg_at, last_msg_at, max_msg_id, lifespan_days,
        post_rate, content_density, gold,
        json.dumps({"iptv_count": len(iptv), "harvest_mentions": len(mentions),
                    "spam_ratio": spam_ratio, "forward_ratio": forward_ratio,
                    "sample_total": sample_total, "sample_kept": kept_total,
                    "cache_saved": save_cache}))
    await conn.execute(
        "UPDATE seed_candidates SET validation_status='alive', validated_at=NOW(), "
        "validation_attempts=validation_attempts+1 WHERE username=$1", u)
    counts["alive"] += 1
    return "alive", doc

# ---------- main ----------
async def main():
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT username FROM seed_candidates "
            "WHERE validation_status='pending' AND invite_link IS NULL "
            "AND username NOT LIKE 'INVITE:%' "
            "ORDER BY added_at ASC " +
            (f"LIMIT {BATCH_LIMIT}" if BATCH_LIMIT else ""))
    unames = [r["username"] for r in rows]
    print(f"# {len(unames)} pending to validate via t.me/s/", file=sys.stderr)

    counts = {"alive": 0, "dead": 0, "forbidden": 0, "err": 0}
    harvest_stats = {"mentions_new": 0, "invites_new": 0, "iptv": 0}
    t0 = time.time()
    sem = None

    async with pool.acquire() as conn:
        for i, u in enumerate(unames, 1):
            try:
                await process_channel(u, conn, counts, harvest_stats, sem)
            except Exception as e:
                counts["err"] += 1
                print(f"  ERR @{u}: {type(e).__name__}: {e}", file=sys.stderr)
                try:
                    await conn.execute(
                        "UPDATE seed_candidates SET validation_status='err', validation_error=$2, "
                        "validated_at=NOW(), validation_attempts=validation_attempts+1 WHERE username=$1",
                        u, f"{type(e).__name__}: {str(e)[:200]}")
                except Exception:
                    pass

            if i % 25 == 0 or i == len(unames):
                elapsed = int(time.time() - t0)
                rate = i / max(1, elapsed)
                eta = int((len(unames) - i) / max(0.01, rate))
                print(f"  [{i}/{len(unames)}] elapsed={elapsed}s rate={rate:.1f}/s "
                      f"alive={counts['alive']} forbidden={counts['forbidden']} err={counts['err']} "
                      f"harvest={harvest_stats['mentions_new']}m/{harvest_stats['invites_new']}i/{harvest_stats['iptv']}iptv "
                      f"eta={eta}s",
                      file=sys.stderr)
            time.sleep(current_delay())

    print(f"\n# DONE in {int(time.time()-t0)}s  alive={counts['alive']} forbidden={counts['forbidden']} err={counts['err']}", file=sys.stderr)
    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
