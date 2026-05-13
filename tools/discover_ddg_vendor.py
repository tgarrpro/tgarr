"""Vendor-targeted DDG sub-scraper — Source #5.

Hunts for TG channel @usernames that appear in TV-box vendor listings,
manuals, review forums, and IPTV-bouquet posts. These channels are highly
prized: vendors curate them for paying customers, so they tend to be
public + active + video-rich.

Run in parallel with discover_ddg.py — different output path, different
seed, queries do not overlap.
"""
import html as htmlmod
import os
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import yaml

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
DDG = 'https://html.duckduckgo.com/html/'

USERNAME_RX = re.compile(
    r'(?:t\.me|telegram\.me|telegram\.dog)/'
    r'(?:s/)?'
    r'(?!(?:joinchat|addstickers|addtheme|share|setlanguage|c/|\+))'
    r'([A-Za-z][A-Za-z0-9_]{4,31})\b', re.IGNORECASE)
B_RX = re.compile(r'</?b\b[^>]*>', re.IGNORECASE)

# ~80 vendor / TV-box / IPTV / channel-list queries
QUERIES = [
    # Generic vendor-box patterns
    'telegram box channel list',
    'telegram tv box channels preloaded',
    'android tv box telegram movie channels',
    'iptv box telegram bouquet',
    'best telegram channels for tv box',
    'tv box telegram movies free',
    'premium telegram movie channels list',
    'telegram channels for kodi alternative',
    # Brand-specific common TV box models
    'MXQ pro telegram channels',
    'X96 max telegram',
    'TX6 telegram channels',
    'TX9 telegram channels',
    'TX3 telegram',
    'H96 max telegram channels',
    'T95 telegram',
    'X88 pro telegram',
    'A95X telegram',
    'Vontar telegram channels',
    'Magicsee telegram',
    'Beelink telegram channels',
    'firestick telegram channels',
    'shield tv telegram',
    # Regional content vendors
    'telegram tamil movie box channel list',
    'tamil iptv telegram channels',
    'malayalam tv box telegram',
    'hindi movie box telegram channels',
    'bollywood box telegram channels list',
    'pakistani iptv telegram',
    'arabic iptv telegram channels',
    'arabic tv box telegram',
    'persian iptv telegram',
    'iranian tv box telegram channels',
    'turkish iptv telegram',
    'kurdish iptv telegram',
    'russian iptv telegram channels',
    'spanish iptv telegram',
    'latino tv box telegram',
    'chinese tv box telegram channels',
    'vietnamese iptv telegram',
    'indonesian iptv telegram',
    'thai iptv telegram',
    'korean drama tv box telegram',
    # IPTV / pirate streaming culture
    'iptv telegram bouquet free',
    'iptv free telegram channels',
    'free movies telegram link',
    'free hindi movies telegram channel link',
    'free tamil dubbed telegram',
    'webrip telegram channels',
    'bluray telegram channels',
    'netflix originals telegram channels',
    'amazon prime telegram channels',
    'disney plus telegram channels',
    'hbo telegram channels list',
    'cricket live telegram channels',
    'sports iptv telegram channels',
    # User-curated lists on forums
    'site:reddit.com telegram channels movie box',
    'site:reddit.com telegram iptv channels',
    'site:reddit.com r/Piracy telegram',
    'site:reddit.com r/Piracy t.me',
    'site:xda-developers.com telegram channels',
    'site:forum.xda-developers.com telegram tv box',
    'site:github.com awesome telegram piracy',
    'site:github.com tv box telegram channels',
    'site:medium.com telegram channels list 2026',
    'site:medium.com telegram channels list 2025',
    'site:medium.com best telegram channels movies',
    # Setup/install docs
    'telegram setup guide tv box',
    'how to use telegram on android tv box',
    'telegram channel list pdf',
    'telegram channels handbook',
    'telegram movies channels collection',
    'telegram channels ultimate list',
    'mega telegram channels list',
    # Country-specific TV-box markets
    'india tv box channels telegram',
    'pakistan iptv box telegram',
    'middle east iptv telegram',
    'latin america iptv telegram',
    'usa free movies telegram channels',
    'uk telegram channels free movies',
]


def fetch(q, offset):
    body = urlencode({'q': q, 's': offset, 'kl': 'us-en'})
    req = Request(DDG, data=body.encode(), headers={
        'User-Agent': UA, 'Accept': 'text/html',
        'Accept-Language': 'en-US,en;q=0.9',
        'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urlopen(req, timeout=25) as r:
            return r.read().decode('utf-8', 'replace')
    except Exception as e:
        print(f'  fetch_err {type(e).__name__}: {q[:40]}', file=sys.stderr)
        return ''


def extract(h):
    clean = B_RX.sub('', htmlmod.unescape(h))
    return set(m.group(1) for m in USERNAME_RX.finditer(clean))



def _write_yaml(out_path, seen, queries_run, total, partial=False):
    import os, yaml, time
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    out = {
        'version': 1,
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'partial': partial,
        'queries_run': queries_run,
        'queries_total': total,
        'channels': [{'username': u, 'source': 'ddg-vendor', 'tags': ['ddg-vendor', 'tv-box'], 'audience': 'sfw'} for u in sorted(seen)],
    }
    tmp = out_path + '.tmp'
    with open(tmp, 'w') as f:
        yaml.safe_dump(out, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, out_path)
    with open(out_path + '.progress', 'w') as f:
        f.write(str(queries_run) + chr(10))


def _load_resume(out_path):
    import os, yaml
    seen = set()
    skip = 0
    if os.path.exists(out_path):
        try:
            doc = yaml.safe_load(open(out_path)) or {}
            for c in doc.get('channels', []):
                u = (c.get('username') or '').strip()
                if u: seen.add(u)
        except Exception:
            pass
    p = out_path + '.progress'
    if os.path.exists(p):
        try:
            skip = int(open(p).read().strip())
        except Exception:
            skip = 0
    return seen, skip


def main():
    out_path = os.environ.get('DDG_VENDOR_OUT', '/var/www/tgarr.me/app/_data/seed/ddg-vendor-out.yaml')
    pages = int(os.environ.get('DDG_PAGES', '4'))
    delay = float(os.environ.get('DDG_DELAY', '5'))
    checkpoint_every = int(os.environ.get('DDG_CHECKPOINT_EVERY', '20'))
    resume = os.environ.get('DDG_RESUME', '0').lower() in ('1', 'true', 'yes')

    total = len(QUERIES)
    seen = set()
    skip = 0
    if resume:
        seen, skip = _load_resume(out_path)
        print(f'# resumed: {len(seen)} channels, skipping first {skip} queries', file=sys.stderr)

    queries = QUERIES[skip:]
    print(f'# vendor — {total} total ({len(queries)} this run), {pages} pages, {delay}s delay, ckpt={checkpoint_every}, out={out_path}',
          file=sys.stderr)
    t0 = time.time()
    for qi, q in enumerate(queries, 1):
        qseen = set()
        for page in range(pages):
            h = fetch(q, page * 30)
            if not h: break
            names = extract(h)
            new = names - qseen
            qseen |= names
            if not new and page > 0: break
            time.sleep(delay)
        new_global = qseen - seen
        seen |= qseen
        print(f'  [{qi:3d}/{len(queries)}] +{len(new_global):3d} (q={len(qseen):3d}) total={len(seen):5d} t={int(time.time()-t0)}s :: {q[:55]}',
              file=sys.stderr)
        if checkpoint_every > 0 and (qi % checkpoint_every == 0):
            _write_yaml(out_path, seen, skip + qi, total, partial=True)
            print(f'  [ckpt] wrote {out_path} ({len(seen)})', file=sys.stderr)

    _write_yaml(out_path, seen, skip + len(queries), total, partial=False)
    print(f'# wrote {out_path} ({len(seen)} channels, complete)', file=sys.stderr)

if __name__ == '__main__':
    main()
