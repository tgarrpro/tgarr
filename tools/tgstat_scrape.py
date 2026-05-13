"""tgstat.com HTML scraper — registry seeder.

tgstat.com renders public channel rankings as HTML (no API key required).
Each category page lists Telegram channels with @username links. We scrape
the username, category, region, then write into tgarr's registry.yaml format
as unverified entries. Health-check verifies each later.

Categories to scrape are listed in CATEGORIES. Regional subdomains
(in., ir., cn., ar., es., pt., br.) for non-English markets.

Run:  python tgstat_scrape.py > registry-imported.yaml
"""
import re
import sys
import time
from urllib.request import Request, urlopen

import yaml

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# (category_slug, our_category_enum) — map TGStat to our taxonomy
CATEGORIES = [
    ("cinema",       "cinema"),
    ("edutainment",  "education"),
    ("news",         "news"),
    ("tech",         "tech"),
    ("books",        "ebook"),
    ("music",        "music"),
    ("games",        "game"),
    ("travel",       "mixed"),
    ("design",       "mixed"),
    ("science",      "science"),
    ("politics",     "news"),
    ("languages",    "language"),
]

# Regional subdomains (empty string = international)
REGIONS = [
    ("",   "global", "en"),
    ("in", "in",     "hi"),     # India
    ("ir", "ir",     "fa"),     # Iran (Persian/Farsi)
    ("ar", "mena",   "ar"),     # Arabic
    ("es", "es",     "es"),     # Spanish
    ("pt", "br",     "pt"),     # Brazil/Portuguese
    ("cn", "cn",     "zh"),     # China
]

USERNAME_RE = re.compile(
    r'href="https?://(?:[a-z]{2}\.)?tgstat\.(?:com|ru)(?:/en)?/channel/@([A-Za-z0-9_]{5,32})"'
)

# Channel @usernames returned by the regex include duplicates;
# keep first occurrence per (username) since first encounter is typically
# the highest-ranked.


def fetch(url: str, retries: int = 2) -> str:
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == retries:
                print(f"# FETCH FAIL {url}: {e}", file=sys.stderr)
                return ""
            time.sleep(2 + attempt * 3)
    return ""


def scrape_category(subdomain: str, region_label: str, language: str,
                    cat_slug: str, cat_enum: str) -> list[dict]:
    """Scrape one category page on one regional subdomain."""
    if subdomain:
        url = f"https://{subdomain}.tgstat.com/{cat_slug}"
    else:
        url = f"https://tgstat.com/{cat_slug}"
    html = fetch(url)
    if not html:
        return []
    usernames = []
    seen = set()
    for m in USERNAME_RE.finditer(html):
        u = m.group(1)
        if u not in seen:
            seen.add(u)
            usernames.append(u)
    print(f"# {url}  →  {len(usernames)} channels", file=sys.stderr)
    return [
        {
            "username": u,
            "category": cat_enum,
            "audience": "family",
            "region": region_label,
            "language": language,
            "status": "unverified",
            "source": "tgstat-scrape",
            "tags": [f"tgstat:{cat_slug}", f"region:{region_label}"],
        }
        for u in usernames
    ]


def main():
    out = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels": [],
    }
    seen_usernames = set()
    for subdomain, region_label, language in REGIONS:
        for cat_slug, cat_enum in CATEGORIES:
            entries = scrape_category(subdomain, region_label, language, cat_slug, cat_enum)
            for e in entries:
                if e["username"] not in seen_usernames:
                    seen_usernames.add(e["username"])
                    out["channels"].append(e)
            time.sleep(1.5)  # gentle rate-limit

    print(f"# TOTAL unique channels scraped: {len(out['channels'])}", file=sys.stderr)
    yaml.safe_dump(out, sys.stdout, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()
