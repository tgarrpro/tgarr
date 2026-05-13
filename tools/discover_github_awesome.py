"""Scrape Telegram @usernames from GitHub awesome-telegram-channels lists.

Strategy: each awesome list is a Markdown file with @channel_name or
t.me/channel_name links scattered throughout. We don't try to parse the
list structure — just regex every line for username patterns. Aggressive
but works across hundreds of differently-formatted repos.

Output goes to /tmp/github-awesome-imported.yaml in the format that
load_seed_candidates.py understands.
"""
import re
import sys
import time
from urllib.request import Request, urlopen
import yaml

# Real repos discovered via github.com/search/repositories ranked by stars.
# Each (owner, repo, branch) → tries README.md at that branch path.
# Both `master` and `main` attempted automatically.
GITHUB_REPOS = [
    ("ebertti", "awesome-telegram"),                  # 4962⭐ Brazilian curated
    ("goq", "telegram-list"),                          # 4944⭐ giant general
    ("A-gambit", "awesome-telegram-chats"),
    ("palark", "awesome-devops-telegram"),
    ("GuidoPenta", "awesome-italian-tech-communities"),
    ("kalanakt", "awesome-telegram"),
    ("AminTaheri23", "Awesome-AI-telegram-gp-and-channel"),
    ("mehrazino", "tg-cybersec"),
    ("mirojo", "Hacking-Directory"),
    ("j2a1ck", "telegram-list"),
    ("Yochanes", "telegram-awesome_channel"),
    ("jozi", "iranian-developers-in-telegram"),
    ("Sateetje", "awesome-nem-telegram"),
    ("simplefastfunnels254", "tg-cybersec"),
    ("MhdiTaheri", "V2rayCollector"),
    ("MhdiTaheri", "ProxyCollector"),
    ("sajjad-021", "Telegram-Marketing-Software"),
    ("citcheese", "telegramMonitor"),
    ("erensunar", "Telegram-Private-Channel-Listener"),
    ("VariabileAleatoria", "Telegram-Facebook-Pages-Bot"),
    ("arashstar1", "bot-lua"),
    ("AshishMK", "indian_meesenger"),
    ("neelgeek", "AWPbot"),
]

# Build candidate URL list — try README.md and readme.md on both branches.
RAW_URLS = []
for owner, repo in GITHUB_REPOS:
    for branch in ("master", "main"):
        for filename in ("README.md", "readme.md"):
            RAW_URLS.append(
                f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{filename}")

UA = "Mozilla/5.0 (compatible; tgarr-discover/0.1; +https://tgarr.me)"

# Match @username OR t.me/username (case-insensitive). Excludes obvious
# false-positives (joinchat, +invite, addtheme).
USERNAME_RX = re.compile(
    r"(?:@|t\.me/|telegram\.me/|telegram\.dog/)"
    r"(?!(?:joinchat|addstickers|addtheme|share|setlanguage)/)"
    r"([A-Za-z][A-Za-z0-9_]{4,31})\b",
    re.IGNORECASE,
)


def fetch(url):
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/plain"})
    try:
        with urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ✗ {url}: {type(e).__name__}", file=sys.stderr)
        return ""


def main():
    seen = set()
    per_repo = []
    for url in RAW_URLS:
        text = fetch(url)
        if not text:
            continue
        found = set()
        for m in USERNAME_RX.finditer(text):
            u = m.group(1)
            # filter obvious non-channels
            if u.lower() in {"telegram", "username", "channel", "yourname",
                             "share", "yourbot", "bot"}:
                continue
            # 8-bit category guess from the surrounding URL/repo text
            found.add(u)
        new = found - seen
        seen.update(found)
        per_repo.append((url, len(found), len(new)))
        print(f"  {url[-60:]:60s} → {len(found)} matches, {len(new)} new",
              file=sys.stderr)
        time.sleep(0.5)

    out = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels": [
            {"username": u, "source": "github-awesome",
             "audience": "sfw",
             "tags": ["github-awesome"]}
            for u in sorted(seen)
        ],
    }
    print(f"\n# total unique: {len(seen)}", file=sys.stderr)
    yaml.safe_dump(out, sys.stdout, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()
