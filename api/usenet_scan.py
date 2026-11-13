"""Reading one window of a group for the index (docs/USENET_INDEX.md).

Three ways of reading, as the registry leases them:
  coarse   10 neighbouring headers every 2,000 articles  - finds every release of 1 GB or more
  medium    2 headers every 400                          - nearly all of 100 MB or more
  full     every header                                  - small files, exact sizes

A sparse reading sees a handful of the thousands of headers of a release. That is enough to
know its name, who posted it, about where it is and about how large: every header of a part
says how many parts its file has ("(35/273)"), and often how many files the release has
("[10/30]"). `Sketch` puts cards together from that.

Reading is done by plain threads, one connection each: the NNTP code is blocking and the
work is waiting for the server.
"""
import collections
import re
import threading
import time
import zlib

import usenet as U
import usenet_judge as J


# ───────────────────────── asking the server ─────────────────────────

def connect(cfg: dict, packed: bool = True) -> U.NNTP:
    nn = U.NNTP(cfg["host"], int(cfg.get("port") or 563), cfg.get("user") or None, cfg.get("pass") or None,
                use_ssl=cfg.get("ssl", True), timeout=60)
    nn.packed = False
    nn.at = None
    nn.wire = 0
    if packed:                         # an eighth of the bytes; every multi-line answer is packed from here on
        code, _ = nn.cmd("XFEATURE COMPRESS GZIP")
        nn.packed = code in (290, 291, 200)
    return nn


def select(nn: U.NNTP, grp: str):
    if nn.at != grp:
        code, text = nn.cmd(f"GROUP {grp}")
        if code != 211:
            raise U.NNTPError(f"group not carried ({code})")
        nn.at = grp


def xover(nn: U.NNTP, lo: int, hi: int) -> list:
    """[(number, subject, poster, date, message-id, bytes, groups)] for lo..hi of the selected group."""
    nn.sock.sendall(f"XOVER {lo}-{hi}\r\n".encode())
    head = nn._line()
    nn.wire += len(head)
    if head.startswith(b"423") or head.startswith(b"420"):
        return []
    if not head.startswith(b"224"):
        raise U.NNTPError(f"XOVER {head[:3].decode('ascii', 'replace')}")
    if nn.packed:
        d = zlib.decompressobj()
        plain = b""
        while not d.eof:               # the packed stream says itself where it ends
            chunk = nn.f.read1(65536)
            if not chunk:
                raise U.NNTPError("connection closed")
            nn.wire += len(chunk)
            plain += d.decompress(chunk)
        tail = d.unused_data
        while not tail.endswith(b".\r\n"):
            more = nn.f.read1(64)
            if not more:
                break
            nn.wire += len(more)
            tail += more
        lines = plain.split(b"\r\n")
    else:
        lines = nn._multiline()
        nn.wire += sum(len(x) + 2 for x in lines)
    rows = []
    for ln in lines:
        p = ln.split(b"\t")
        if len(p) < 7:
            continue
        try:
            xref = p[8].decode("ascii", "replace") if len(p) > 8 else ""
            groups = tuple(x.split(":")[0] for x in xref.split() if ":" in x and not x.lower().startswith("xref"))
            rows.append((int(p[0]), U.decode(p[1]), U.decode(p[2]), p[3].decode("ascii", "replace"),
                         p[4].decode("ascii", "replace"), int(p[6] or 0), groups))
        except ValueError:
            continue
    return rows


def probes(lo: int, hi: int, m: int, every: int) -> list:
    """Where a sparse reading looks: m neighbouring articles, every `every`."""
    return [(a, min(hi, a + m - 1)) for a in range(lo, hi + 1, every)]


def read_window(cfg: dict, grp: str, lo: int, hi: int, read=None, connections: int = 8, on_rows=None, stop=None) -> dict:
    """Read lo..hi of a group over several connections. read = (m, every) for a sparse reading,
    None for everything (in stretches of 20,000). Rows go to on_rows(rows) as they come.
    Returns what it cost: requests, bytes on the wire, seconds, headers."""
    todo = collections.deque(probes(lo, hi, *read) if read else [(a, min(hi, a + 19999)) for a in range(lo, hi + 1, 20000)])
    lock = threading.Lock()
    st = {"requests": 0, "bytes": 0, "headers": 0, "errors": 0, "connections": 0}
    t0 = time.time()

    def worker():
        nn = None
        while not (stop and stop()):
            with lock:
                if not todo:
                    break
                a, b = todo.popleft()
            for attempt in range(3):
                try:
                    if nn is None:
                        nn = connect(cfg)
                        with lock:
                            st["connections"] += 1
                    select(nn, grp)
                    before = nn.wire
                    rows = xover(nn, a, b)
                    with lock:
                        st["requests"] += 1
                        st["bytes"] += nn.wire - before
                        st["headers"] += len(rows)
                    if rows and on_rows:
                        on_rows(rows)
                    break
                except Exception:
                    with lock:
                        st["errors"] += 1
                    try:
                        nn.sock.close()
                    except Exception:
                        pass
                    nn = None
                    time.sleep(1 + attempt * 2)
        try:
            if nn:
                nn.sock.close()
        except Exception:
            pass

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, connections))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st["seconds"] = round(time.time() - t0, 1)
    return st


# ───────────────────────── cards from a handful of headers ─────────────────────────

RX_VOLUME_NO = (
    (re.compile(r"\.part(\d{1,4})\.rar$", re.I), 0),      # name.part07.rar: the 7th volume
    (re.compile(r"\.r(\d{2})$", re.I), 2),                # name.r05: the 7th (name.rar, then r00, r01, ...)
    (re.compile(r"\.(\d{3})$"), 0),                       # name.007
)


def volume_no(file_name: str):
    """Which volume of its archive this file is (1 = the first), or None."""
    n = (file_name or "").strip()
    for rx, add in RX_VOLUME_NO:
        m = rx.search(n)
        if m:
            return int(m.group(1)) + add
    if n.lower().endswith(".rar"):
        return 1
    return None


def volumes_in_all(seen: set) -> float:
    """How many volumes there are when only some were seen: the highest number seen, plus
    the usual gap between those seen. (Seeing volumes 12, 30 and 47 of an unknown number:
    about 62 - the same sum that counts tanks from their serial numbers.)"""
    if not seen:
        return 0.0
    k, m = len(seen), max(seen)
    return max(float(k), m + m / k - 1.0)


RX_PART_NO = re.compile(r"\(\s*\d+\s*/\s*\d+\s*\)\s*(?:\d+\s*)?$")
RX_TAG_ONLY = re.compile(r"^(tv|music|movies?|games?|apps?|ebook|xxx|hd|sd|dvd|misc|full|repost|new)$", re.I)


def best_name(title: str, file_name: str) -> str:
    """The name to show: the words of the subject when they say something; otherwise the
    file's own name (boards put only their tag and a counter in front of the file)."""
    t = J.strip_tags((title or "").strip())
    core = re.sub(r"\W", "", t)
    if len(core) >= 8 and not RX_TAG_ONLY.match(t) and not J.looks_made_up(t):
        return t
    return J.strip_tags(U.base_name(file_name)) or U.base_name(file_name) or t


def describe(subject: str):
    """What a subject line is and which release it belongs to:
    (class, kind, name, release key, file name, parts in the file, files in the release).
    The key is what all headers of one release have in common; a card carries it, and the
    release is found again by it when its NZB is made."""
    c, kind, title, fname = J.judge(subject)
    if c in ("not a part",):
        return c, None, "", "", "", 0, None
    s = U.parse_subject(subject)
    name = best_name(title, fname)
    key = U.release_key({"title": title if name == title else "", "file": fname}) or name.lower()
    return c, kind, name, key, fname, s["total"], s["files"]


class Sketch:
    """Cards from whatever headers were seen - a few of each release, or all of them."""

    def __init__(self, grp: str):
        self.grp = grp
        self.rel = {}
        self.posters = collections.defaultdict(lambda: [0, 0, 0])      # hash -> [headers, wanted, junk]
        self.count = collections.Counter()
        self.lock = threading.Lock()
        self._judged = {}              # the same file's parts differ only in "(35/273)": judged once

    def add_rows(self, rows) -> None:
        with self.lock:
            for r in rows:
                self._add(*r)

    def _add(self, num, subject, poster, date, mid, size, groups=()):
        memo = RX_PART_NO.sub("", subject)
        hit = self._judged.get(memo)
        if hit is None:
            if len(self._judged) > 200_000:
                self._judged.clear()
            hit = self._judged[memo] = describe(subject)
        c, kind, name, key, fname, total, files_n = hit
        self.count[c] += 1
        ph = U.poster_hash(poster)
        p = self.posters[ph]
        p[0] += 1
        p[1] += c == "wanted"
        p[2] += c in ("hidden", "not a part", "password")
        if c not in ("wanted", "unknown") or J.is_program(fname):
            if J.is_program(fname):
                self.count["program"] += 1
            return
        r = self.rel.get((ph, key))
        if r is None:
            r = self.rel[(ph, key)] = {"poster": ph, "key": key, "name": name, "kind": kind, "files": {}, "files_expected": None,
                                       "lo": num, "hi": num, "mid": mid, "t0": None, "t1": None, "groups": set(), "seen": 0,
                                       "vols": set()}
        if kind and not r["kind"]:
            r["kind"] = kind
        f = r["files"].setdefault(fname, [total, 0, 0])                 # parts in the file, parts seen, bytes seen
        v = volume_no(fname)
        if v:
            r["vols"].add(v)
        f[1] += 1
        f[2] += size
        r["seen"] += 1
        if files_n:
            r["files_expected"] = max(r["files_expected"] or 0, files_n)
        t = U.when(date)
        if t:
            r["t0"] = t if r["t0"] is None else min(r["t0"], t)
            r["t1"] = t if r["t1"] is None else max(r["t1"], t)
        if num < r["lo"]:
            r["lo"], r["mid"] = num, mid
        r["hi"] = max(r["hi"], num)
        r["groups"].update(g for g in groups if g)

    @staticmethod
    def size_of(r: dict) -> int:
        """About how large. A file that was seen is (its number of parts) x (the usual size of
        a part). A sparse reading sees only some of the files, so the number of files is worked
        out: from the volume numbers seen when it is a split archive, else from "file n of N"
        in the subject, else only what was seen is counted."""
        payload = {k: v for k, v in r["files"].items() if not re.search(r"\.(par2|nfo|sfv|nzb|md5)$", k or "", re.I)} or r["files"]
        sizes = sorted(v[0] * (v[2] / v[1]) for v in payload.values() if v[1])
        if not sizes:
            return 0
        seen = sum(sizes)
        vols = volumes_in_all(r.get("vols") or set())
        if vols > len(payload):
            # volumes are all the same size but the last: the usual one, times how many
            total = sizes[len(sizes) // 2] * vols
        elif (r["files_expected"] or 0) > len(r["files"]):
            total = seen * r["files_expected"] / len(r["files"])
        else:
            total = seen
        return int(total * 0.97)            # yEnc adds about 3% to what is sent

    def cards(self, detail: int = 0, min_size: int = 0) -> list:
        """The cards, as the registry takes them. Kind unknown is left out: a sparse reading
        cannot settle it and the registry does not index what it cannot name."""
        out = []
        for r in self.rel.values():
            if r["kind"] not in U.WANTED:
                continue
            size = self.size_of(r)
            if size < min_size:
                continue
            whole = all(v[1] >= v[0] for v in r["files"].values()) and (not r["files_expected"] or len(r["files"]) >= r["files_expected"])
            out.append({
                "name": r["name"][:300], "kind": r["kind"], "size": size, "files": len(r["files"]), "files_expected": r["files_expected"],
                "segments": r["seen"] if detail else None, "complete": bool(whole) if detail else None,
                "posted_from": r["t0"], "posted_to": r["t1"], "poster": r["poster"], "key": r["key"][:300],
                "groups": sorted(r["groups"] | {self.grp}), "first_message_id": r["mid"], "art_lo": r["lo"], "art_hi": r["hi"],
                "detail": detail})
        return out

    def poster_lines(self, least: int = 5) -> list:
        return [{"h": h, "headers": v[0], "wanted": v[1], "junk": v[2]} for h, v in self.posters.items() if v[0] >= least]


# ───────────────────────── the NZB of a card ─────────────────────────

def rebuild(cfg: dict, grp: str, poster: str, key: str, art_lo: int, art_hi: int, pad: int = 6000, name: str = "") -> dict:
    """Read every header around where a release was seen and put its NZB together.
    A sparse reading saw a few articles between art_lo and art_hi; the release may begin
    before the first and end after the last, so the reading starts `pad` wider and goes on
    outwards for as long as parts keep turning up at the edge.
    Returns {"nzb", "complete", "size", "files", "segments", "art_lo", "art_hi"}; "nzb" is
    empty when nothing of it is there any more."""
    nn = connect(cfg)
    files = {}
    memo = {}
    span = [None, None]

    def take(lo, hi):
        n = 0
        a = max(1, lo)
        while a <= hi:
            for num, subject, frm, date, mid, size, _groups in xover(nn, a, min(hi, a + 19999)):
                if U.poster_hash(frm) != poster:
                    continue
                m = RX_PART_NO.sub("", subject)
                d = memo.get(m)
                if d is None:
                    d = memo[m] = describe(subject)
                if d[3] != key or J.is_program(d[4]):
                    continue
                ps = U.parse_subject(subject)
                f = files.setdefault(d[4], {"total": d[5], "parts": {}, "bytes": 0, "subject": subject, "poster": frm, "date": date})
                if ps["part"] not in f["parts"]:
                    f["parts"][ps["part"]] = (mid, size)
                    f["bytes"] += size
                    n += 1
                span[0] = num if span[0] is None else min(span[0], num)
                span[1] = num if span[1] is None else max(span[1], num)
            a += 20000
        return n

    try:
        select(nn, grp)
        lo, hi = art_lo - pad, art_hi + pad
        take(lo, hi)
        for _ in range(25):                        # outwards while the edge still holds parts of it
            grew = False
            if span[0] is not None and span[0] - lo < 3000:
                grew = take(lo - 20000, lo - 1) > 0 or grew
                lo -= 20000
            if span[1] is not None and hi - span[1] < 3000:
                grew = take(hi + 1, hi + 20000) > 0 or grew
                hi += 20000
            if not grew:
                break
    finally:
        try:
            nn.sock.close()
        except Exception:
            pass
    if not files:
        return {"nzb": "", "complete": False, "size": 0, "files": 0, "segments": 0, "art_lo": art_lo, "art_hi": art_hi}
    seen = sum(len(f["parts"]) for f in files.values())
    expected = sum(f["total"] for f in files.values())
    payload = [k for k in files if not re.search(r"\.(par2|nfo|sfv|nzb|md5)$", k or "", re.I)]
    return {"nzb": U.build_nzb({"title": name or key, "files": files, "group": grp}),
            "complete": seen >= expected, "size": int(sum(f["bytes"] for f in files.values()) * 0.97),
            "files": len(payload) or len(files), "segments": seen, "art_lo": span[0], "art_hi": span[1]}
