"""Wayback Machine CDX scrape — extract every archived t.me/<username>.

Internet Archive's CDX API indexes every URL Wayback has ever snapshotted.
Querying with `url=t.me/*` prefix-matches returns the raw list of t.me
URLs that have ever been archived, which includes a huge fraction of all
public Telegram channels that have ever been linked anywhere on the web.

This bypasses every aggregator's anti-bot — we're consuming archive data,
not live pages. The CDX endpoint is rate-limited but generous.

Output: /tmp/wayback-imported.yaml. Expected yield 50K-500K unique
usernames depending on how deep the CDX pagination goes.
"""
import json
import os
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

UA = "Mozilla/5.0 (compatible; tgarr-discover/0.1; +https://tgarr.me)"
CDX = "https://web.archive.org/cdx/search/cdx"

# Telegram username constraints: letter-start, [A-Za-z0-9_], 5-32 chars
USERNAME_RX = re.compile(
    r"^t\.me/(?!(?:joinchat|addstickers|addtheme|share|setlanguage|c|s|\+))"
    r"([A-Za-z][A-Za-z0-9_]{4,31})"
    r"(?:/|\?|$)",
    re.IGNORECASE,
)

# CDX queries are HARD-LIMITED somewhere around 150K rows. Pagination via
# `resumeKey` returned in the last row of each batch.
PAGE_LIMIT = 50000
MAX_PAGES = int(os.environ.get("WAYBACK_MAX_PAGES", "10"))


def fetch_cdx(params, retries=3):
    url = CDX + "?" + urlencode(params)
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"  ✗ {url[:80]}: {type(e).__name__}", file=sys.stderr)
            if attempt == retries - 1:
                return []
            time.sleep(5 + attempt * 5)


def main():
    seen = set()
    resume_key = None
    for page in range(MAX_PAGES):
        params = {
            "url": "t.me/*",
            "matchType": "prefix",
            "output": "json",
            "fl": "original",          # only original URL column
            "collapse": "urlkey",       # dedup by url
            "limit": str(PAGE_LIMIT),
            "showResumeKey": "true",
        }
        if resume_key:
            params["resumeKey"] = resume_key
        rows = fetch_cdx(params)
        if not rows or len(rows) < 2:
            break
        # rows[0] is header (["original"]). Last row(s) is resume key.
        # Walk rows[1:] until we hit blank rows then resume key.
        next_resume = None
        new_in_page = 0
        for row in rows[1:]:
            if not row or not row[0]:
                continue
            # Resume key rows are usually a single token, no "://"
            if "://" not in row[0] and "t.me" not in row[0]:
                next_resume = row[0]
                continue
            # Strip protocol
            url = row[0]
            for prefix in ("https://", "http://", "//"):
                if url.startswith(prefix):
                    url = url[len(prefix):]
            m = USERNAME_RX.match(url)
            if m:
                u = m.group(1)
                if u not in seen:
                    seen.add(u)
                    new_in_page += 1
        print(f"  page {page+1:2d} → {new_in_page:5d} new (cumulative {len(seen):6d})",
              file=sys.stderr)
        if not next_resume:
            print(f"  no resumeKey, done.", file=sys.stderr)
            break
        resume_key = next_resume
        time.sleep(2.0)  # be polite to IA

    print(f"\n# total unique: {len(seen)}", file=sys.stderr)
    out = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels": [
            {"username": u, "source": "wayback-cdx",
             "tags": ["wayback-cdx"], "audience": "sfw"}
            for u in sorted(seen)
        ],
    }
    yaml.safe_dump(out, sys.stdout, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()
