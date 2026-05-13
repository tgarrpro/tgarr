"""Mine Reddit comments + submissions for t.me/ channel mentions.

PullPush.io (api.pullpush.io) is the community-run successor to Pushshift,
which Reddit shut down in 2023. It mirrors Reddit comment + submission
data with a similar HTTP/JSON query interface.

Strategy:
- Query PullPush for "t.me" matches across all Reddit, walking backwards
  in time via `before=<epoch>` pagination.
- Also query specific subreddits known for Telegram channel sharing
  (r/Telegram, r/Piracy, r/megalinks, etc).
- Regex extract t.me/<username> from comment + submission body+title.
- 5-year sweep ~= 50-200K raw matches → 10-30K unique usernames after dedup.

Output: /tmp/pullpush-imported.yaml in standard seed_candidates format.

Pacing: 1-2 seconds between requests. PullPush is lenient but we want to
stay below noisy-neighbor threshold.
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
PP_COMMENT = "https://api.pullpush.io/reddit/search/comment/"
PP_SUBMISSION = "https://api.pullpush.io/reddit/search/submission/"

# Excludes joinchat, addstickers, addtheme, share — those aren't @usernames
USERNAME_RX = re.compile(
    r"(?:t\.me/|telegram\.me/|telegram\.dog/)"
    r"(?!(?:joinchat|addstickers|addtheme|share|setlanguage|c/|\+|s/)/?)"
    r"([A-Za-z][A-Za-z0-9_]{4,31})\b",
    re.IGNORECASE,
)

# Where to mine. None=all-reddit. Subreddits are higher signal but smaller.
TARGETS = [
    # subreddit (None=all), label
    (None, "all"),
    ("Telegram", "r/Telegram"),
    ("Piracy", "r/Piracy"),
    ("megalinks", "r/megalinks"),
    ("MegalinksDirectory", "r/MegalinksDirectory"),
    ("MAME", "r/MAME"),
    ("CrackWatch", "r/CrackWatch"),
    ("FreeMoviesAndPDF", "r/FreeMoviesAndPDF"),
    ("opendirectories", "r/opendirectories"),
    ("Anime_For_Humans", "r/Anime_For_Humans"),
    ("animepiracy", "r/animepiracy"),
    ("watchfreemovies", "r/watchfreemovies"),
    ("freeloaders", "r/freeloaders"),
    ("LinuxActionShow", "r/LinuxActionShow"),
    ("Telegram_bots", "r/Telegram_bots"),
    ("TelegramChannel", "r/TelegramChannel"),
    ("MoviesOnTelegram", "r/MoviesOnTelegram"),
    ("seriesonline", "r/seriesonline"),
]

MAX_BATCHES_PER_TARGET = int(os.environ.get("PP_BATCHES", "20"))  # 20 × 500 = 10K rows per target
BATCH_SIZE = 500
PAUSE_SEC = float(os.environ.get("PP_PAUSE", "1.5"))


def fetch(url, max_retries=3):
    """One JSON fetch with simple retry on transient failures."""
    for attempt in range(max_retries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"  ✗ {url}: {type(e).__name__}: {e}", file=sys.stderr)
                return None
            time.sleep(2 + attempt * 3)


def mine_target(endpoint, sub, label, source_tag):
    """Walk one (endpoint, subreddit) pair backward in time. Yield usernames."""
    before = None
    seen_in_target = set()
    for batch in range(MAX_BATCHES_PER_TARGET):
        params = {
            "q": "t.me",
            "size": BATCH_SIZE,
            "sort": "desc",
            "sort_type": "created_utc",
        }
        if sub:
            params["subreddit"] = sub
        if before:
            params["before"] = before
        url = endpoint + "?" + urlencode(params)
        data = fetch(url)
        if not data or not data.get("data"):
            break
        rows = data["data"]
        if not rows:
            break
        new_in_batch = 0
        oldest_ts = None
        for row in rows:
            text = (row.get("body") or row.get("selftext") or "") + " " + (row.get("title") or "")
            for m in USERNAME_RX.finditer(text):
                u = m.group(1)
                if u in seen_in_target:
                    continue
                seen_in_target.add(u)
                new_in_batch += 1
                yield u, source_tag
            ts = row.get("created_utc")
            if ts and (oldest_ts is None or ts < oldest_ts):
                oldest_ts = ts
        endpoint_kind = "comment" if "/comment/" in endpoint else "post"
        print(f"  {label:30s} [{endpoint_kind} batch {batch+1:2d}] +{new_in_batch} "
              f"(cumulative {len(seen_in_target)})", file=sys.stderr)
        if not oldest_ts:
            break
        before = oldest_ts
        time.sleep(PAUSE_SEC)


def main():
    seen = {}  # username -> set of source labels
    for sub, label in TARGETS:
        # Hit BOTH comments and submissions endpoints for each target
        for endpoint, kind in ((PP_COMMENT, "c"), (PP_SUBMISSION, "s")):
            try:
                src_tag = f"pullpush-{label}-{kind}"
                for u, tag in mine_target(endpoint, sub, label, src_tag):
                    seen.setdefault(u, set()).add(tag)
            except Exception as e:
                print(f"  fatal in {label}/{kind}: {e}", file=sys.stderr)
            time.sleep(1)
    print(f"\n# total unique: {len(seen)}", file=sys.stderr)
    out = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels": [
            {"username": u, "source": "pullpush-reddit",
             "tags": sorted(src_tags),
             "audience": "sfw"}
            for u, src_tags in sorted(seen.items())
        ],
    }
    yaml.safe_dump(out, sys.stdout, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()
