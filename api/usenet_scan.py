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
import queue
import re
import threading
import time
import zlib

import usenet as U
import usenet_judge as J
import usenet_rules as R


# ───────────────────────── asking the server ─────────────────────────

def connect(cfg: dict, packed: bool = True) -> U.NNTP:
    nn = U.NNTP(cfg["host"], int(cfg.get("port") or 563), cfg.get("user") or None, cfg.get("pass") or None,
                use_ssl=cfg.get("ssl", True), timeout=60)
    nn.packed = False
    nn.at = None
    nn.wire = 0
    nn.server = (cfg.get("host") or "").lower()
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


def _answer(nn: U.NNTP) -> list:
    """The lines of a multi-line answer whose first line has been read already."""
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
        return plain.split(b"\r\n")
    lines = nn._multiline()
    nn.wire += sum(len(x) + 2 for x in lines)
    return lines


NO_FROM_FIELD = set()                  # servers that do not answer XHDR: asked once, remembered


def numbers_by(nn: U.NNTP, lo: int, hi: int, keep) -> list:
    """The numbers of the articles lo..hi whose From field keep() accepts. Asks for that one
    field only (XHDR From): about three bytes an article where all the headers are over
    forty. Raises NNTPError when the server does not have the command."""
    nn.sock.sendall(f"XHDR From {lo}-{hi}\r\n".encode())
    head = nn._line()
    nn.wire += len(head)
    if head.startswith(b"423") or head.startswith(b"420"):
        return []
    if not head.startswith(b"221"):
        raise U.NNTPError(f"XHDR {head[:3].decode('ascii', 'replace')}")
    out = []
    for ln in _answer(nn):
        num, sep, frm = ln.partition(b" ")
        if sep and keep(frm):
            try:
                out.append(int(num))
            except ValueError:
                continue
    return out


def xover_of(nn: U.NNTP, lo: int, hi: int, keep) -> list:
    """The headers of one poster's articles in lo..hi, without fetching everybody else's:
    first who posted each article, then all the headers of only the stretch where that
    poster is - nothing at all where there is none of theirs. A server without the
    one-field command is asked for everything, as before."""
    if nn.server not in NO_FROM_FIELD:
        try:
            nums = numbers_by(nn, lo, hi, keep)
        except U.NNTPError as e:
            if not str(e).startswith("XHDR "):
                raise
            NO_FROM_FIELD.add(nn.server)           # this server never will: do not ask again
            return xover(nn, lo, hi, keep)
        if not nums:
            return []
        lo, hi = min(nums), max(nums)
    return xover(nn, lo, hi, keep)


def xover(nn: U.NNTP, lo: int, hi: int, keep=None) -> list:
    """[(number, subject, poster, date, message-id, bytes, groups)] for lo..hi of the selected group.
    keep(raw From field) -> bool, when given, decides on a row before anything of it is
    decoded: looking for one poster's articles among half a million, decoding every subject
    was nine tenths of the time."""
    nn.sock.sendall(f"XOVER {lo}-{hi}\r\n".encode())
    head = nn._line()
    nn.wire += len(head)
    if head.startswith(b"423") or head.startswith(b"420"):
        return []
    if not head.startswith(b"224"):
        raise U.NNTPError(f"XOVER {head[:3].decode('ascii', 'replace')}")
    lines = _answer(nn)
    rows = []
    for ln in lines:
        if keep is not None:
            head3 = ln.split(b"\t", 3)
            if len(head3) < 4 or not keep(head3[2]):
                continue
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


STRETCH = 20000                        # headers asked for in one request when everything is read
TRIES = 4                              # how often one stretch is asked for before it is given up


def read_window(cfg: dict, grp: str, lo: int, hi: int, read=None, connections: int = 8, on_rows=None, stop=None,
                opener=None, pull=None, keep=None, pool=None, spans=None) -> dict:
    """Read lo..hi of a group over several connections. read = (m, every) for a sparse reading,
    None for everything (in stretches of 20,000). Rows go to on_rows(rows) as they come, from
    the reading threads.
    Returns what it cost - requests, bytes on the wire, seconds, headers - and "failed": the
    stretches [(a, b)] that could not be read after several tries (empty when all was read);
    "refused": how often a connection could not be opened (the provider's limit was reached).
    keep: see xover - rows it turns down are neither decoded nor counted.
    pool: a list of connections that are open already. They are used before any new one is
    opened and put back, still open, at the end (the caller closes them): reading in several
    rounds, signing in sixteen times for every round costs more than the reading.
    spans: several stretches of the group [(lo, hi), ...] read as one piece of work, in place
    of lo..hi (which are then not looked at).

    A stretch that fails goes back in the queue, so another connection takes it. A connection
    that cannot be opened twice running gives up while others are still working: a provider
    that allows fewer connections than were asked for is read with those it allows."""
    opener, pull = opener or connect, pull or xover
    todo = collections.deque()
    for lo_, hi_ in (spans if spans is not None else [(lo, hi)]):
        todo.extend((a, b, 0) for a, b in (
            probes(lo_, hi_, *read) if read else [(a, min(hi_, a + STRETCH - 1)) for a in range(lo_, hi_ + 1, STRETCH)]))
    lock = threading.Lock()
    st = {"requests": 0, "bytes": 0, "headers": 0, "errors": 0, "connections": 0, "refused": 0, "failed": []}
    n_workers = max(1, min(connections, len(todo) or 1))
    alive = [n_workers]
    busy = [0]                         # stretches taken and not yet settled
    t0 = time.time()

    def worker():
        nn, refused = None, 0
        if pool is not None:
            with lock:
                nn = pool.pop() if pool else None
        while not (stop and stop()):
            with lock:
                if not todo:
                    if busy[0] == 0:
                        break          # nothing left and nobody may put anything back
                    item = None
                else:
                    item = todo.popleft()
                    busy[0] += 1
            if item is None:
                time.sleep(0.05)       # another connection is on a stretch that may come back
                continue
            a, b, tries = item
            opening = nn is None
            try:
                if nn is None:
                    nn = opener(cfg)
                    with lock:
                        st["connections"] += 1
                refused, opening = 0, False
                select(nn, grp)
                before = nn.wire
                rows = pull(nn, a, b, keep) if keep is not None else pull(nn, a, b)
                if rows and on_rows:
                    on_rows(rows)
                with lock:
                    st["requests"] += 1
                    st["bytes"] += nn.wire - before
                    st["headers"] += len(rows)
                    busy[0] -= 1
                continue
            except Exception:
                try:
                    nn.sock.close()
                except Exception:
                    pass
                nn = None
                quit_now = False
                with lock:
                    st["errors"] += 1
                    busy[0] -= 1
                    if opening:
                        refused += 1
                        st["refused"] += 1
                        if refused >= 2 and alive[0] > 1:
                            quit_now = True                 # the others carry on with this stretch
                            todo.append((a, b, tries))      # not this stretch's fault: no try counted
                    if not quit_now:
                        if tries + 1 < TRIES:
                            todo.append((a, b, tries + 1))
                        else:
                            st["failed"].append((a, b))
                if quit_now:
                    break
                time.sleep(min(6.0, 0.5 + tries * 1.5))
        with lock:
            alive[0] -= 1
            if nn is not None and pool is not None:
                pool.append(nn)                    # still open: for the next round
                nn = None
        try:
            if nn:
                nn.sock.close()
        except Exception:
            pass

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with lock:                         # stopped early, or every connection gave up: what is left was not read
        st["failed"] += [(a, b) for a, b, _ in todo]
        todo.clear()
    st["failed"].sort()
    st["seconds"] = round(time.time() - t0, 1)
    return st


class Feeder:
    """Hands rows from the reading threads to one function on a thread of its own. Judging
    subjects in the reading threads held every connection up; collecting all rows first and
    judging afterwards needs the whole window in memory (a window read in full is twenty
    million headers). A short queue does neither: readers wait when the judge is behind."""

    def __init__(self, fn, waiting: int = 64):
        self.fn = fn
        self.q = queue.Queue(maxsize=waiting)
        self.error = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while True:
            rows = self.q.get()
            if rows is None:
                return
            if self.error is None:
                try:
                    self.fn(rows)
                except Exception as e:             # keep taking, so that no reader waits for ever
                    self.error = e

    def put(self, rows) -> None:
        self.q.put(rows)

    def close(self) -> None:
        self.q.put(None)
        self.t.join()
        if self.error is not None:
            raise self.error


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
RX_PART_OF = re.compile(r"\(\s*(\d+)\s*/\s*\d+\s*\)\s*(?:\d+\s*)?$")
# A song, a book, a picture is often posted whole in a handful of articles. Read in full,
# such a file is listed under its card with the Message-IDs of its parts: it can be fetched
# straight from the card, with no reading of headers at all. (The registry's limits.)
MAX_IDS = 20                           # parts in one file: "small" ends here
MAX_ITEMS = 2000                       # files listed under one card
ITEM_KINDS = ("audio", "audiobook", "ebook", "comic", "photo")
RX_HELPER = re.compile(r"\.(par2|nfo|sfv|nzb|md5)$", re.I)
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

    def __init__(self, grp: str, items: bool = False):
        self.grp = grp
        self.items = items             # a full reading: remember the Message-IDs of small files
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
        if self.items and 0 < total <= MAX_IDS and len(r["files"]) <= MAX_ITEMS:
            pm = RX_PART_OF.search(subject)
            if pm:
                if len(f) == 3:
                    f.append({})
                f[3].setdefault(int(pm.group(1)), mid)
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
            listed = self.items_of(r) if (detail and self.items) else []
            groups = sorted(r["groups"] | {self.grp})
            judged = {"name": r["name"], "groups": groups, "items": listed}
            if R.forbidden(judged):
                self.count["never kept"] += 1      # not sent, not stored: see usenet_rules
                continue
            out.append({
                "name": r["name"][:300], "kind": r["kind"], "size": size, "files": len(r["files"]), "files_expected": r["files_expected"],
                "segments": r["seen"] if detail else None, "complete": bool(whole) if detail else None,
                "posted_from": r["t0"], "posted_to": r["t1"], "poster": r["poster"], "key": r["key"][:300],
                "groups": groups, "first_message_id": r["mid"], "art_lo": r["lo"], "art_hi": r["hi"],
                "audience": "nsfw" if R.adult(judged) else "sfw", "detail": detail})
            if listed:
                out[-1]["items"] = listed
        return out

    @staticmethod
    def items_of(r: dict) -> list:
        """The files of a release with the Message-IDs of their parts, in order - when every
        file of it is small and what it holds is the kind fetched one file at a time. Only
        files seen whole are listed."""
        if r["kind"] not in ITEM_KINDS:
            return []
        payload = {k: v for k, v in r["files"].items() if k and not RX_HELPER.search(k)}
        if not payload or len(payload) > MAX_ITEMS or any(v[0] > MAX_IDS or v[0] < 1 for v in payload.values()):
            return []
        out = []
        for name in sorted(payload):
            v = payload[name]
            ids = v[3] if len(v) > 3 else {}
            if all(p in ids for p in range(1, v[0] + 1)):
                out.append({"name": name[:300], "size": int(v[2] * 0.97), "message_ids": [ids[p] for p in range(1, v[0] + 1)]})
        return out

    def poster_lines(self, least: int = 5) -> list:
        return [{"h": h, "headers": v[0], "wanted": v[1], "junk": v[2]} for h, v in self.posters.items() if v[0] >= least]


# ───────────────────────── the NZB of a card ─────────────────────────

def rebuild(cfg: dict, grp: str, poster: str, key: str, art_lo: int, art_hi: int, pad: int = 6000, name: str = "",
            connections: int = 1, opener=None, pull=None) -> dict:
    """Read every header around where a release was seen and put its NZB together.
    A sparse reading saw a few articles between art_lo and art_hi; the release may begin
    before the first and end after the last, so the reading starts `pad` wider and goes on
    outwards for as long as parts keep turning up at the edge. The stretches are read side
    by side over `connections` connections (a 2 GB release is some four hundred thousand
    headers: a minute over one connection).
    Returns {"nzb", "complete", "size", "files", "segments", "art_lo", "art_hi", "unread"};
    "nzb" is empty when nothing of it is there any more; "unread" counts the stretches the
    server did not give - with any of those the NZB is never called complete. Also what it
    cost, and for whoever sets the number of connections next time: "connections" (the most
    that were open at once), "wanted" (the most there was work for) and "refused"."""
    files = {}
    memo = {}
    span = [None, None]
    cost = {"unread": 0, "requests": 0, "seconds": 0.0, "connections": 0, "refused": 0, "wanted": 0}
    connections = max(1, min(int(connections or 1), 50))
    pool = []                                      # opened once, used for every round, closed at the end
    t_all = time.time()
    mine = {}

    def keep(raw_from: bytes) -> bool:
        # this poster's articles only, told from the raw field: each poster is decoded once
        v = mine.get(raw_from)
        if v is None:
            if len(mine) > 100_000:
                mine.clear()
            v = mine[raw_from] = U.poster_hash(U.decode(raw_from)) == poster
        return v

    def take(spans, exploring=False):
        spans = [(max(1, a), b) for a, b in spans if b >= max(1, a)]
        if not spans:
            return 0
        got = []
        # Where the release is known to be, everything is asked for at once. Beyond its
        # edge - where most stretches hold nothing of it - first the one small question of
        # who posted what, and the headers only where this poster is.
        st = read_window(cfg, grp, 0, 0, connections=connections, on_rows=got.append, opener=opener,
                         pull=pull or (xover_of if exploring else xover), keep=keep, pool=pool, spans=spans)
        cost["unread"] += len(st["failed"])
        cost["refused"] += st["refused"]
        cost["wanted"] = max(cost["wanted"], min(connections, sum((b - a) // STRETCH + 1 for a, b in spans)))
        cost["requests"] += st["requests"]
        cost["connections"] = max(cost["connections"], len(pool))      # the most that were open at once
        if st["failed"] and not st["requests"]:
            raise U.NNTPError("the news server gave none of the headers asked for")
        n = 0
        # in the order of the articles, whichever connection brought them: of a part posted
        # twice the first is taken, as with one connection
        for num, subject, frm, date, mid, size, _groups in sorted((r for rows in got for r in rows), key=lambda r: r[0]):
            if U.poster_hash(frm) != poster:
                continue
            m = RX_PART_NO.sub("", subject)
            d = memo.get(m)
            if d is None:
                d = memo[m] = describe(subject)
            if d[3] != key or J.is_program(d[4]):
                continue
            ps = U.parse_subject(subject)
            f = files.setdefault(d[4], {"total": d[5], "parts": {}, "bytes": 0, "subject": subject, "poster": frm, "date": date,
                                        "lowest": ps["part"]})
            if ps["part"] < f["lowest"]:
                # the file is described by its lowest part, whichever stretch was read first:
                # the same NZB comes out however many connections read it
                f.update(subject=subject, poster=frm, date=date, lowest=ps["part"])
            if ps["part"] not in f["parts"]:
                f["parts"][ps["part"]] = (mid, size)
                f["bytes"] += size
                n += 1
            span[0] = num if span[0] is None else min(span[0], num)
            span[1] = num if span[1] is None else max(span[1], num)
        return n

    try:
        return _rebuild(cfg, grp, key, art_lo, art_hi, pad, name, connections, take, files, span, cost)
    finally:
        cost["seconds"] = round(time.time() - t_all, 1)
        for nn in pool:
            try:
                nn.sock.close()
            except Exception:
                pass


def _rebuild(cfg, grp, key, art_lo, art_hi, pad, name, connections, take, files, span, cost) -> dict:
    lo, hi = max(1, art_lo - pad), art_hi + pad
    take([(lo, hi)])
    # outwards: four stretches each side to begin with, twice as many every round up to one
    # per connection. A release that ends just past the edge costs little; one that runs on
    # for a million articles is read at full width from the third round.
    width = min(connections, 4)
    for _ in range(25):                            # while the edge still holds parts of it
        step = STRETCH * max(1, width // 2)        # `width` stretches a round: half each side
        width = min(connections, width * 2)
        more = []                                  # both edges in the same round, side by side
        if span[0] is not None and span[0] - lo < 3000 and lo > 1:
            more.append((lo - step, lo - 1))
            lo = max(1, lo - step)
        if span[1] is not None and hi - span[1] < 3000:
            more.append((hi + 1, hi + step))
            hi += step
        if not more or take(more, True) == 0:
            break
    if not files:
        return {"nzb": "", "complete": False, "size": 0, "files": 0, "segments": 0, "art_lo": art_lo, "art_hi": art_hi, **cost}
    seen = sum(len(f["parts"]) for f in files.values())
    expected = sum(f["total"] for f in files.values())
    payload = [k for k in files if not RX_HELPER.search(k or "")]
    return {"nzb": U.build_nzb({"title": name or key, "files": files, "group": grp}),
            "complete": seen >= expected and not cost["unread"], "size": int(sum(f["bytes"] for f in files.values()) * 0.97),
            "files": len(payload) or len(files), "segments": seen, "art_lo": span[0], "art_hi": span[1], **cost}


NZB_CONNECTIONS = 16                   # to begin with
NZB_CONNECTIONS_STEP = 6
NZB_CONNECTIONS_MOST = 50


def next_connections(asked, got: dict) -> int:
    """How many connections the next NZB is read with, from how the last one went. An NZB is
    wanted now, by somebody waiting: it takes as many connections as the provider gives.
    Sixteen to begin with. All of them open, none refused, and there was work for all: six
    more next time. Refused: as many as did open, and that is the level until it is tried
    again. A small release that never needed them all says nothing either way."""
    try:
        asked = int(asked)
    except (TypeError, ValueError):
        return NZB_CONNECTIONS
    if asked < 1:
        return NZB_CONNECTIONS
    opened, wanted, refused = got.get("connections") or 0, got.get("wanted") or 0, got.get("refused") or 0
    if refused:
        return max(1, min(asked, opened))
    if wanted >= asked and opened >= asked:
        return min(NZB_CONNECTIONS_MOST, asked + NZB_CONNECTIONS_STEP)
    return asked
