"""Scrape telega.io — Telegram analytics site, ~100K channels indexed.

Strategy: walk each category's paginated channel list, extract t.me/<username>
links. Like our tgstat scraper but for a second aggregator.

telega.io uses query params /catalog?category=X&page=N
"""
import re
import sys
import time
from urllib.request import Request, urlopen
import yaml

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# telega.io's category slugs (probed manually + via training data).
# Some may 404; scraper skips silently.
CATEGORIES = [
    "blogs", "marketing", "design", "tech", "education", "humor",
    "cinema", "music", "books", "games", "news", "politics",
    "sports", "travel", "art", "business", "cryptocurrency", "economics",
    "psychology", "religion", "media", "fashion", "food", "cars",
    "health", "fitness", "languages", "science", "lifestyle",
    "video", "photo", "shopping",
]

PAGES_PER = int(__import__("os").environ.get("TELEGA_PAGES", "8"))

USERNAME_RX = re.compile(
    r'(?:t\.me/|@)([A-Za-z][A-Za-z0-9_]{4,31})\b'
)
HREF_RX = re.compile(
    r'href="(?:https?:)?//t\.me/([A-Za-z][A-Za-z0-9_]{4,31})"'
)


def fetch(url):
    try:
        req = Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ✗ {url}: {type(e).__name__}", file=sys.stderr)
        return ""


def scrape_category_page(category, page):
    # Try a couple URL shapes telega.io uses
    candidates = [
        f"https://telega.io/catalog?category={category}&page={page}",
        f"https://telega.io/catalog/{category}?page={page}",
        f"https://telega.io/channels/{category}?page={page}",
    ]
    for url in candidates:
        html = fetch(url)
        if not html:
            continue
        # Prefer href= matches (more specific), fall back to general regex
        names = set(HREF_RX.findall(html))
        if not names:
            names = set(m.group(1) for m in USERNAME_RX.finditer(html))
        if names:
            return names
    return set()


def main():
    seen = set()
    cat_breakdown = {}
    for cat in CATEGORIES:
        cat_seen = set()
        for page in range(1, PAGES_PER + 1):
            names = scrape_category_page(cat, page)
            if not names:
                break
            new = names - seen
            seen.update(new)
            cat_seen.update(new)
            time.sleep(1.0)
        print(f"  {cat:20s} → {len(cat_seen)} unique", file=sys.stderr)
        cat_breakdown[cat] = cat_seen

    out_channels = []
    written = set()
    for cat, names in cat_breakdown.items():
        for u in names:
            if u in written:
                continue
            written.add(u)
            out_channels.append({
                "username": u,
                "category": cat,
                "source": "telega-io",
                "tags": [f"telega-io:{cat}"],
            })
    out = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels": out_channels,
    }
    print(f"\n# total unique: {len(seen)}", file=sys.stderr)
    yaml.safe_dump(out, sys.stdout, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()
