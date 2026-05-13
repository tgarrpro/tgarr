"""Deep tgstat.com scraper — top-N per category × per region × pagination.

The original tgstat_scrape.py hit just /<category> (first page only).
This version walks pagination ?page=1..MAX_PAGES per category and adds
many more categories. Expect 30K-100K unique @usernames depending on what
tgstat's HTML serves us.

Output: /tmp/tgstat-deep-imported.yaml in load_seed_candidates format.
"""
import re
import sys
import time
from urllib.request import Request, urlopen
import yaml

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# More categories than the first scraper. tgstat exposes ~30 in their UI.
CATEGORIES = [
    ("cinema",         "cinema"),
    ("edutainment",    "education"),
    ("news",           "news"),
    ("tech",           "tech"),
    ("books",          "ebook"),
    ("music",          "music"),
    ("games",          "game"),
    ("travel",         "travel"),
    ("design",         "art"),
    ("science",        "science"),
    ("politics",       "news"),
    ("languages",      "language"),
    ("cars",           "auto"),
    ("crypto",         "crypto"),
    ("economics",      "finance"),
    ("blogs",          "mixed"),
    ("business",       "business"),
    ("marketing",      "business"),
    ("health",         "health"),
    ("psychology",     "lifestyle"),
    ("erotic",         "nsfw"),   # tgstat exposes this; will be classified NSFW
    ("humor",          "humor"),
    ("art",            "art"),
    ("history",        "education"),
    ("sport",          "sport"),
    ("food",           "food"),
    ("fashion",        "fashion"),
    ("religion",       "religion"),
    ("lifestyle",      "lifestyle"),
    ("children",       "education"),  # NOTE: SFW kids content; CSAM block runs server-side anyway
]

# Regional subdomains. tgstat localizes per market.
REGIONS = [
    ("",   "global", "en"),
    ("in", "in",     "hi"),
    ("ir", "ir",     "fa"),
    ("cn", "cn",     "zh"),
    ("ru", "ru",     "ru"),    # Russian flagship — likely best coverage
    ("ua", "ua",     "uk"),
]

PAGES_PER = int(__import__("os").environ.get("TGSTAT_PAGES", "5"))  # default ~5 pages = top 500

USERNAME_RE = re.compile(
    r'href="https?://(?:[a-z]{2}\.)?tgstat\.(?:com|ru)(?:/en)?/channel/@'
    r'([A-Za-z][A-Za-z0-9_]{4,31})"'
)


def fetch(url):
    try:
        req = Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        with urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ✗ {url}: {type(e).__name__}", file=sys.stderr)
        return ""


def scrape_page(subdomain, cat_slug, page):
    host = f"{subdomain}.tgstat.com" if subdomain else "tgstat.com"
    url = f"https://{host}/{cat_slug}?page={page}"
    html = fetch(url)
    return list({m.group(1) for m in USERNAME_RE.finditer(html)})


def main():
    seen = set()
    seen_per_cat = {}
    for subdomain, region_label, language in REGIONS:
        for cat_slug, cat_enum in CATEGORIES:
            cat_seen = set()
            for page in range(1, PAGES_PER + 1):
                names = scrape_page(subdomain, cat_slug, page)
                if not names:
                    break
                new = set(names) - seen
                seen.update(new)
                cat_seen.update(new)
                time.sleep(1.0)  # gentle to tgstat
            print(f"  {region_label:8s} {cat_slug:14s} → {len(cat_seen)} new",
                  file=sys.stderr)
            seen_per_cat[(region_label, language, cat_enum)] = cat_seen

    # Emit YAML — pick first (region, language, category) we saw for each
    print(f"\n# total unique: {len(seen)}", file=sys.stderr)
    out_channels = []
    written = set()
    for (region, lang, cat), names in seen_per_cat.items():
        for u in names:
            if u in written:
                continue
            written.add(u)
            out_channels.append({
                "username": u,
                "category": cat,
                "region": region,
                "language": lang,
                "audience": "nsfw" if cat == "nsfw" else "sfw",
                "source": "tgstat-deep",
                "tags": [f"tgstat-deep:{cat}", f"region:{region}"],
            })

    out = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels": out_channels,
    }
    yaml.safe_dump(out, sys.stdout, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()
