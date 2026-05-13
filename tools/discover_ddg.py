"""DuckDuckGo HTML search v2 — mine t.me/<username> from organic results.

v1 → v2 (2026-05-13):
- Drop quoted "t.me/" queries (DDG penalizes literal-quote spam patterns).
- Build queries from {category × language × template} Cartesian product.
- Regex now accepts `t.me/s/<name>` web-preview URLs (was excluded).
- Strip <b>...</b> tags that DDG injects around the matched keyword,
  which used to break the username regex match.

Env:
- DDG_QUERY_LIMIT   max queries to run (default unlimited)
- DDG_PAGES         pages per query (default 5; each page = 30 results)
- DDG_DELAY         seconds between requests (default 5)
- DDG_OUT           output path (default /tmp/ddg-imported.yaml)
- DDG_SEED          shuffle seed (default 42 → deterministic ordering)
"""
import html as htmlmod
import os
import random
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DDG_HTML = "https://html.duckduckgo.com/html/"

# Regex changes vs v1:
#  - removed `s/` from exclusions (it is a real preview-URL prefix, channel
#    name follows it)
#  - made the s/ prefix an optional consumed group so it does not affect
#    capture position
USERNAME_RX = re.compile(
    r"(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:s/)?"
    r"(?!(?:joinchat|addstickers|addtheme|share|setlanguage|c/|\+))"
    r"([A-Za-z][A-Za-z0-9_]{4,31})\b",
    re.IGNORECASE,
)
B_TAG_RX = re.compile(r"</?b\b[^>]*>", re.IGNORECASE)

CATEGORIES = [
    "wallpaper", "music", "book", "ebook", "movie", "tv series",
    "anime", "manga", "news", "tech", "programming",
    "podcast", "art", "photography", "design", "fashion",
    "fitness", "food", "recipe", "crypto", "finance",
    "trading", "stock", "gaming", "esport", "sports",
    "education", "course", "language learning", "math", "science",
    "wallpapers hd", "movies hd", "songs", "audiobook",
    "pdf", "premium courses", "tutorial", "udemy",
]

LANGS = [
    "", "hindi", "tamil", "telugu", "malayalam", "urdu", "bengali",
    "arabic", "persian", "russian", "chinese", "japanese", "korean",
    "spanish", "portuguese", "french", "german", "italian",
    "turkish", "vietnamese", "indonesian", "thai",
]

TEMPLATES = [
    "telegram {lang} {cat} channel",
    "telegram {cat} {lang}",
    "best telegram {cat} {lang}",
    "t.me {cat} {lang}",
]

SITE_QUERIES = [
    "site:reddit.com telegram channel",
    "site:reddit.com t.me",
    "site:github.com awesome telegram",
    "site:gist.github.com t.me",
    "site:medium.com best telegram channels",
]


def build_queries() -> list[str]:
    raw = []
    for tpl in TEMPLATES:
        for cat in CATEGORIES:
            for lang in LANGS:
                q = tpl.format(lang=lang, cat=cat)
                # collapse double spaces from empty {lang}
                q = " ".join(q.split())
                raw.append(q)
    raw.extend(SITE_QUERIES)
    # dedupe preserving order
    seen = set()
    out = []
    for q in raw:
        if q in seen:
            continue
        seen.add(q)
        out.append(q)
    return out


def fetch(url, post_data=None, retries=2):
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html",
                "Accept-Language": "en-US,en;q=0.9",
            })
            if post_data:
                req.data = post_data.encode()
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == retries:
                print(f"  fetch_err {type(e).__name__}: {url[:60]}", file=sys.stderr)
                return ""
            time.sleep(4 + attempt * 6)


def extract_usernames(html: str) -> set[str]:
    # Decode HTML entities + strip inline <b>...</b> highlights
    clean = htmlmod.unescape(html)
    clean = B_TAG_RX.sub("", clean)
    return set(m.group(1) for m in USERNAME_RX.finditer(clean))




def _write_yaml(out_path: str, seen: set, queries_run: int, total_queries: int, partial: bool = False):
    import os, yaml, time
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    out = {
        "version": 2,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "partial": partial,
        "queries_run": queries_run,
        "queries_total": total_queries,
        "channels": [
            {"username": u, "source": "ddg-search", "tags": ["ddg-search"], "audience": "sfw"}
            for u in sorted(seen)
        ],
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump(out, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, out_path)
    with open(out_path + ".progress", "w") as f:
        f.write(str(queries_run) + chr(10))


def _load_resume(out_path: str):
    import os, yaml
    seen = set()
    skip = 0
    if os.path.exists(out_path):
        try:
            doc = yaml.safe_load(open(out_path)) or {}
            for c in doc.get("channels", []):
                u = (c.get("username") or "").strip()
                if u: seen.add(u)
        except Exception as e:
            print(f"# resume: failed to read {out_path}: {e}")
    p = out_path + ".progress"
    if os.path.exists(p):
        try:
            skip = int(open(p).read().strip())
        except Exception:
            skip = 0
    return seen, skip


def main():
    qlimit = int(os.environ.get("DDG_QUERY_LIMIT", "0"))
    pages = int(os.environ.get("DDG_PAGES", "5"))
    delay = float(os.environ.get("DDG_DELAY", "5"))
    out_path = os.environ.get("DDG_OUT", "/var/www/tgarr.me/app/_data/seed/ddg-out.yaml")
    seed = int(os.environ.get("DDG_SEED", "42"))
    checkpoint_every = int(os.environ.get("DDG_CHECKPOINT_EVERY", "50"))
    resume = os.environ.get("DDG_RESUME", "0").lower() in ("1", "true", "yes")

    queries = build_queries()
    random.Random(seed).shuffle(queries)
    if qlimit > 0:
        queries = queries[:qlimit]
    total = len(queries)

    seen_global: set[str] = set()
    skip = 0
    if resume:
        seen_global, skip = _load_resume(out_path)
        queries = queries[skip:]
        print(f"# resumed: {len(seen_global)} channels, skipping first {skip} queries", file=sys.stderr)

    print(f"# v2 — {total} total queries ({len(queries)} this run), {pages} pages, {delay}s delay, "
          f"seed={seed}, checkpoint_every={checkpoint_every}, out={out_path}", file=sys.stderr)

    t0 = time.time()
    for qi, q in enumerate(queries, 1):
        qseen: set[str] = set()
        for page in range(pages):
            offset = page * 30
            body = urlencode({"q": q, "s": offset, "kl": "us-en"})
            html = fetch(DDG_HTML, post_data=body)
            if not html:
                break
            names = extract_usernames(html)
            new = names - qseen
            qseen |= names
            if not new and page > 0:
                # End of useful pagination for this query
                break
            time.sleep(delay)
        new_global = qseen - seen_global
        seen_global |= qseen
        elapsed = int(time.time() - t0)
        print(f"  [{qi:4d}/{len(queries)}] +{len(new_global):3d} "
              f"(q={len(qseen):3d}) total={len(seen_global):6d} "
              f"t={elapsed}s :: {q[:60]}",
              file=sys.stderr)

    print(f"\n# total unique: {len(seen_global)} in {int(time.time()-t0)}s",
          file=sys.stderr)

    _write_yaml(out_path, seen_global, skip + len(queries), total, partial=False)
    print(f"# wrote {out_path} ({len(seen_global)} channels, complete)", file=sys.stderr)


if __name__ == "__main__":
    main()
