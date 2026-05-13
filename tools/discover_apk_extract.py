"""APK channel extractor — Source #6.

Decompile any Android APK with apktool, grep its resources/strings/dex for
`t.me/<channel>` references, filter out TG system URI prefixes
(joinchat/addstickers/proxy/socks/etc.), output YAML with the rest.

Usage:
  python3 tools/discover_apk_extract.py <apk-path> [output.yaml]

Yields are highest for vendor TV-box / IPTV / channel-aggregator APKs
(can be 100s-1000s of channels per box).
Telegram-official APK ~7 real channels — useful as plumbing sanity check.

Requires apktool installed on host.
"""
import os
import re
import subprocess
import sys
import tempfile
import time
import yaml

# TG system URI prefixes — NEVER channel names
SYSTEM_PREFIXES = {
    'joinchat', 'addstickers', 'addtheme', 'addemoji', 'addstyle',
    'share', 'setlanguage', 'boost', 'folder', 'giftcode',
    'socks', 'proxy', 'c', 's', 'title', 'iv',
    'login', 'confirmphone', 'msg_url',
}

RX = re.compile(
    r'(?:t\.me|telegram\.me|telegram\.dog)/'
    r'([A-Za-z][A-Za-z0-9_]{4,31})\b',
    re.IGNORECASE)


def decompile(apk_path: str, work_dir: str) -> str:
    out = os.path.join(work_dir, 'decoded')
    print(f'[1/3] apktool d {apk_path} → {out}', file=sys.stderr)
    subprocess.run(['apktool', 'd', '-o', out, '-f', apk_path],
                   check=True, capture_output=True)
    return out


def extract(decoded_dir: str) -> set[str]:
    print(f'[2/3] grepping {decoded_dir} for t.me refs', file=sys.stderr)
    # Use grep -r for speed on large trees
    r = subprocess.run(
        ['grep', '-rohE', r't\.me/[A-Za-z][A-Za-z0-9_]{4,31}', decoded_dir],
        capture_output=True, text=True)
    raw = set()
    for line in r.stdout.splitlines():
        m = RX.match(line)
        if m:
            raw.add(m.group(1))
    return raw


def filter_channels(raw: set[str]) -> set[str]:
    return {u for u in raw if u.lower() not in SYSTEM_PREFIXES}


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    apk = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else '/tmp/apk-extracted.yaml'
    if not os.path.isfile(apk):
        print(f'no such file: {apk}', file=sys.stderr); sys.exit(2)

    apk_name = os.path.basename(apk)
    with tempfile.TemporaryDirectory(prefix='apkextract-') as work:
        decoded = decompile(apk, work)
        raw = extract(decoded)
        chans = filter_channels(raw)
    print(f'[3/3] raw={len(raw)} after-filter={len(chans)}', file=sys.stderr)
    for u in sorted(chans):
        print(f'  @{u}', file=sys.stderr)

    out = {
        'version': 1,
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'source_apk': apk_name,
        'channels': [
            {'username': u, 'source': 'apk-extract',
             'tags': ['apk-extract', apk_name],
             'audience': 'sfw'} for u in sorted(chans)],
    }
    with open(out_path, 'w') as f:
        yaml.safe_dump(out, f, sort_keys=False, allow_unicode=True)
    print(f'wrote {out_path} ({len(chans)} channels)', file=sys.stderr)


if __name__ == '__main__':
    main()
