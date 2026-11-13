"""Usenet indexing prototype - local only. Nothing is sent to a registry, nothing is downloaded.

  scan   <group> <YYYY-MM-DD> <headers>   read that many headers from that day on; keep the cards
  search <words...>                       search the cards kept so far
  nzb    <card id> <file.nzb>             build the NZB by reading the headers again; check that
                                          the articles still exist (asks, downloads nothing)
  stats                                   what has been kept

The news server comes from a SABnzbd configuration file (USENET_SAB_INI, read here and never
printed) or from USENET_HOST / USENET_PORT / USENET_USER / USENET_PASS.
Cards are kept in a SQLite file (USENET_DB, default ./usenet_cards.db).
"""
import calendar
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "crawler"))
import usenet as U  # noqa: E402

DB = os.environ.get("USENET_DB", "usenet_cards.db")
COLS = ("id", "name", "kind", "size", "grp", "poster", "posted_from", "posted_to", "files", "files_expected",
        "segments", "segments_expected", "complete", "first_message_id", "key", "art_lo", "art_hi", "alive")
CHECK = 6        # articles asked about per release: headers outlive the articles they describe


def server():
    ini = os.environ.get("USENET_SAB_INI")
    if not ini:
        return {"host": os.environ["USENET_HOST"], "port": os.environ.get("USENET_PORT", "563"),
                "username": os.environ.get("USENET_USER"), "password": os.environ.get("USENET_PASS"), "ssl": "1"}
    cur, servers, inside = None, [], False
    for line in open(ini, encoding="utf-8", errors="replace"):
        s = line.strip()
        if s.startswith("[[") and inside:
            cur = {}
            servers.append(cur)
        elif s.startswith("[") and not s.startswith("[["):
            inside, cur = s == "[servers]", None
        elif cur is not None and "=" in s:
            k, v = s.split("=", 1)
            cur[k.strip()] = v.strip().strip('"')
    live = sorted((x for x in servers if x.get("enable", "1") == "1" and x.get("host")),
                  key=lambda x: int(x.get("priority", "0") or 0))
    if not live:
        sys.exit("no enabled news server in that file")
    return live[0]


def connect():
    sv = server()
    return U.NNTP(sv["host"], int(sv.get("port", "563") or 563), sv.get("username") or None,
                  sv.get("password") or None, use_ssl=sv.get("ssl", "1") == "1")


def db():
    con = sqlite3.connect(DB)
    con.execute(f"""CREATE TABLE IF NOT EXISTS cards ({", ".join(c + (" TEXT PRIMARY KEY" if c == "id" else "") for c in COLS)})""")
    con.execute("CREATE TABLE IF NOT EXISTS scans (grp TEXT, day TEXT, headers INT, seconds REAL, bytes INT, cards INT, at INT)")
    return con


def read_range(nn, group, lo, hi, on_row):
    step = 20000
    while lo <= hi:
        for row in nn.over(lo, min(hi, lo + step - 1)):
            on_row(row)
        lo += step


def scan(group, day, want):
    con, nn = db(), connect()
    _count, first, last = nn.group(group)
    ts = calendar.timegm(time.strptime(day, "%Y-%m-%d"))
    t0 = time.time()
    start = U.article_at(nn, first, last, ts)
    located = time.time() - t0
    asm = U.Assembler(group)
    t1, b0 = time.time(), nn.bytes_in
    read_range(nn, group, start, min(last, start + want - 1), lambda r: asm.add(*r))
    took, used = time.time() - t1, nn.bytes_in - b0
    rels = asm.releases()
    cards, t2 = [], time.time()
    for r in rels:
        c = U.card(r)
        if not c:
            continue
        # Is it still there? Old headers stay long after the articles were
        # taken down, so a card without this is a promise that may be empty.
        ids = [m for f in r["files"].values() for m, _s in f["parts"].values()]
        sample = random.sample(ids, min(CHECK, len(ids)))
        c["alive"] = round(sum(1 for m in sample if nn.stat(m)) / max(1, len(sample)), 2)
        cards.append(c)
    checked = time.time() - t2
    kinds, skipped = {}, {"random name": 0, "software": 0, "unknown kind": 0}
    for r in rels:
        name = U.display_name(r)
        if U.is_random(name):
            skipped["random name"] += 1
        else:
            k = U.classify(name, [f for f in r["files"] if f])
            if k == "software":
                skipped["software"] += 1
            elif k == "unknown":
                skipped["unknown kind"] += 1
    for c in cards:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        row = dict(c)
        row["grp"] = row.pop("group")
        con.execute(f"INSERT OR REPLACE INTO cards ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                    [int(row[k]) if isinstance(row[k], bool) else row[k] for k in COLS])
    con.execute("INSERT INTO scans VALUES (?,?,?,?,?,?,?)", (group, day, asm.headers, took, used, len(cards), int(time.time())))
    con.commit()
    whole = sum(1 for c in cards if c["complete"])
    there = [c for c in cards if c["alive"] >= 0.99]
    gone = [c for c in cards if c["alive"] <= 0.34]
    span = [c["posted_from"] for c in cards if c["posted_from"]] + [c["posted_to"] for c in cards if c["posted_to"]]
    print(f"{group} from {day}: found the place in {located:.1f}s; {asm.headers:,} headers in {took:.1f}s "
          f"({int(asm.headers / max(took, 0.1)):,}/s), {used / 1e6:.1f} MB")
    if span:
        print(f"  covers {time.strftime('%Y-%m-%d %H:%M', time.gmtime(min(span)))} to "
              f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(max(span)))} UTC")
    print(f"  uploads seen: {len(rels):,} | cards kept: {len(cards):,} {kinds} | whole: {whole:,} | "
          f"not kept: {skipped}")
    print(f"  the cards describe {sum(c['size'] for c in cards) / 1e9:.1f} GB; as stored they take "
          f"{sum(len(str(c)) for c in cards) / 1e3:.0f} KB ({used / max(1, sum(len(str(c)) for c in cards)):.0f}x smaller than the headers read)")
    print(f"  still on the server ({CHECK} articles asked per release, {checked:.0f}s): all there {len(there):,} "
          f"({sum(c['size'] for c in there) / 1e9:.1f} GB) | mostly gone {len(gone):,} "
          f"({sum(c['size'] for c in gone) / 1e9:.1f} GB) | in between {len(cards) - len(there) - len(gone):,}")
    for c in sorted(cards, key=lambda c: -c["size"])[:6]:
        print(f"    {c['kind']:5s} {c['size'] / 1e6:8.0f} MB  {'whole' if c['complete'] else 'part '}  "
              f"{'there' if c['alive'] >= 0.99 else 'GONE ' if c['alive'] <= 0.34 else 'some '}  {c['name'][:70]}")
    nn.quit()


def search(words):
    con = db()
    q = ("SELECT id, kind, size, complete, grp, posted_from, name FROM cards WHERE alive >= 0.99 AND "
         + " AND ".join("name LIKE ?" for _ in words))
    rows = con.execute(q + " ORDER BY posted_from DESC LIMIT 25", [f"%{w}%" for w in words]).fetchall()
    print(f"{len(rows)} card(s) for {' '.join(words)!r}")
    for i, kind, size, whole, grp, t, name in rows:
        print(f"  {i}  {kind:5s} {size / 1e6:7.0f} MB  {'whole' if whole else 'part '}  "
              f"{time.strftime('%Y-%m-%d', time.gmtime(t or 0))}  {name[:70]}")


def nzb(card_id, out):
    con = db()
    row = con.execute(f"SELECT {', '.join(COLS)} FROM cards WHERE id LIKE ?", (card_id + "%",)).fetchone()
    if not row:
        sys.exit("no such card")
    c = dict(zip(COLS, row))
    nn = connect()
    nn.group(c["grp"])
    asm, t0 = U.Assembler(c["grp"]), time.time()
    pad = 2000

    def take(r):
        if U.poster_hash(r[2]) == c["poster"]:
            p = U.parse_subject(r[1])
            if p and U.release_key(p) == c["key"]:
                asm.add(*r)
    read_range(nn, c["grp"], c["art_lo"] - pad, c["art_hi"] + pad, take)
    rels = asm.releases()
    if not rels:
        sys.exit("the headers of this release are no longer there")
    r = rels[0]
    text = U.build_nzb(r)
    open(out, "w", encoding="utf-8").write(text)
    ids = [m for f in r["files"].values() for m, _s in f["parts"].values()]
    sample = random.sample(ids, min(40, len(ids)))
    alive = sum(1 for m in sample if nn.stat(m))
    print(f"{c['name'][:70]}")
    print(f"  rebuilt in {time.time() - t0:.1f}s from {asm.headers:,} matching headers: {len(r['files'])} files, "
          f"{len(ids):,} segments, {r['bytes'] / 1e6:.0f} MB; NZB is {len(text) / 1e3:.0f} KB -> {out}")
    print(f"  articles still on the server: {alive} of {len(sample)} asked (nothing downloaded)")
    nn.quit()


def stats():
    con = db()
    n, size = con.execute("SELECT count(*), coalesce(sum(size),0) FROM cards").fetchone()
    print(f"{n:,} cards describing {size / 1e12:.2f} TB; database file {os.path.getsize(DB) / 1e6:.1f} MB")
    for kind, k, s, w in con.execute("SELECT kind, count(*), sum(size), sum(complete) FROM cards GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {kind:6s} {k:7,} cards  {s / 1e9:9.1f} GB  whole {w:,}")
    h, sec, b = con.execute("SELECT coalesce(sum(headers),0), coalesce(sum(seconds),0), coalesce(sum(bytes),0) FROM scans").fetchone()
    print(f"  read so far: {h:,} headers in {sec:.0f}s, {b / 1e6:.0f} MB")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["scan"]:
        scan(a[1], a[2], int(a[3]))
    elif a[:1] == ["search"]:
        search(a[1:])
    elif a[:1] == ["nzb"]:
        nzb(a[1], a[2])
    elif a[:1] == ["stats"]:
        stats()
    else:
        sys.exit(__doc__)
