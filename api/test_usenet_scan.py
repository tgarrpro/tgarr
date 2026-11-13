"""Checks for reading a window and sketching cards, and for talking to the registry.
Run: python test_usenet_scan.py   (no network)"""
import usenet as U
import usenet_client as C
import usenet_judge as J
import usenet_scan as S

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


# ---- what a subject is -----------------------------------------------------------------
ok(J.judge('[ TOWN ]-[ www.town.ag ]-[ MUSIC ] [15/22] - "VA-Nu_Disco_Beats_Vol_6-WEB-2017-POSH.part14.rar" - 280,73 MB yEnc (37/40)')[0] == "wanted",
   "a board's tag does not make a release an advert")
ok(J.judge("{E26BAEF8-3605-4DC7-9681-6E04BC1A9327} (0970/1147)")[0] == "hidden", "a GUID is a name made to mean nothing")
ok(J.judge('[102/111] - "k576PYo2cA.part101.rar" - 10,28 GB yEnc (235/273)')[0] == "hidden", "so are ten mixed characters")
ok(J.judge("Click It Graph 2.6.0.rar (020/131)")[0] == "software", "a program by its version number")
ok(J.judge('[073/114] - <Password> - "u28B49ChhKB4vM3nMGYP.part072.rar" yEnc (139/686)')[0] == "password", "marked as locked")
ok(J.judge('Wickie auf grosser Fahrt 3D [Bluray - Untouched] [Password protected] - "w.part01.rar" yEnc (1/200)')[0] == "password", "in square brackets too")
ok(J.judge("just some words")[0] == "not a part", "not a part of a file")
ok(J.is_program("holiday.jpg.scr") and J.is_program("Setup.EXE") and not J.is_program("film.mkv") and not J.is_program("noending"), "programs by their ending")

d = S.describe('[10/30] - "Dark.Matter.S03E08.1080p.WEBRip.x264-GRP.part09.rar" - 2,39 GB [ TOWN ]-[ www.town.ag ]-[ TV ] yEnc (35/273)')
ok(d[0] == "wanted" and d[1] == "video" and d[2] == "Dark.Matter.S03E08.1080p.WEBRip.x264-GRP", "the name is the file's when the subject holds only tags")
ok(d[5] == 273 and d[6] == 30, "with the parts of the file and the files of the release")
d2 = S.describe('[11/30] - "Dark.Matter.S03E08.1080p.WEBRip.x264-GRP.part10.rar" - 2,39 GB [ TOWN ]-[ www.town.ag ]-[ TV ] yEnc (1/273)')
ok(d[3] == d2[3], "two volumes of one release have the same key")
ok(S.best_name("www.totalhd.org", "www.totalhd.org Neverland 2011 1080p Bluray.mkv").startswith("Neverland"), "a site's tag is taken off a file name too")

# ---- where a sparse reading looks ----------------------------------------------------------
p = S.probes(1000, 9999, 10, 2000)
ok(p == [(1000, 1009), (3000, 3009), (5000, 5009), (7000, 7009), (9000, 9009)], "ten neighbours every two thousand")
ok(S.probes(1, 5, 10, 2000) == [(1, 5)] and S.probes(10, 9, 2, 400) == [], "never past the end; nothing for an empty stretch")

# ---- how many volumes ---------------------------------------------------------------------------
ok((S.volume_no("a.part07.rar"), S.volume_no("a.r05"), S.volume_no("a.rar"), S.volume_no("a.007"), S.volume_no("a.mkv")) == (7, 7, 1, 7, None),
   "which volume a file is, in the three ways of naming them")
ok(61 < S.volumes_in_all({12, 30, 47}) < 63 and S.volumes_in_all({1, 2, 3}) == 3.0 and S.volumes_in_all(set()) == 0, "how many there are, from those seen")


def release(name, volumes, parts, part_bytes, start, poster="Poster <p@x>"):
    rows, num = [], start
    for v in range(1, volumes + 1):
        for part in range(1, parts + 1):
            rows.append((num, f'[{v:02d}/{volumes}] - "{name}.part{v:02d}.rar" yEnc ({part}/{parts})', poster,
                         "Mon, 05 Mar 2012 10:00:00 GMT", f"<{num}@x>", part_bytes, ("alt.binaries.hdtv", "alt.binaries.boneless")))
            num += 1
    return rows


# a 4.2 GB film (60 volumes x 100 parts x 700 KB) followed by a 350 MB episode
film = release("Local.Hero.1983.1080p.BluRay.x264-GRP", 60, 100, 700_000, 10_000)
ep = release("Show.S01E02.720p.HDTV.x264-GRP", 5, 100, 700_000, 16_000)
junk = [(i, "{E26BAEF8-3605-4DC7-9681-6E04BC1A9327} (%d/900)" % (i - 20000 + 1), "x <x@x>", "", f"<{i}@x>", 700_000, ()) for i in range(20_000, 20_900)]
everything = film + ep + junk
true_film, true_ep = 60 * 100 * 700_000 * 0.97, 5 * 100 * 700_000 * 0.97

full = S.Sketch("alt.binaries.hdtv")
full.add_rows(everything)
cards = {c["name"]: c for c in full.cards(detail=1)}
ok(len(cards) == 2, "read in full: the film and the episode, not the rubbish")
ok(abs(cards["Local.Hero.1983.1080p.BluRay.x264-GRP"]["size"] - true_film) < 1 and cards["Local.Hero.1983.1080p.BluRay.x264-GRP"]["complete"], "exact size, and complete")
ok(cards["Show.S01E02.720p.HDTV.x264-GRP"]["groups"] == ["alt.binaries.boneless", "alt.binaries.hdtv"], "with every group it was posted to")
ok(full.count["hidden"] == 900 and full.count["wanted"] == 6500, "and a count of what the headers were")

coarse = S.Sketch("alt.binaries.hdtv")
for a in range(0, len(everything), 2000):
    coarse.add_rows(everything[a:a + 10])
got = {c["name"]: c for c in coarse.cards(detail=0, min_size=100_000_000)}
ok("Local.Hero.1983.1080p.BluRay.x264-GRP" in got, "ten headers every two thousand: the film is found")
f = got["Local.Hero.1983.1080p.BluRay.x264-GRP"]
ok(0.75 < f["size"] / true_film < 1.25, f"its size within a quarter ({f['size'] / true_film:.2f}) from four glimpses of sixty volumes")
ok(f["detail"] == 0 and f["complete"] is None and f["art_lo"] >= 10_000 and f["art_hi"] < 16_000, "marked as a sketch, with about where it is")

medium = S.Sketch("alt.binaries.hdtv")
for a in range(0, len(everything), 400):
    medium.add_rows(everything[a:a + 2])
got = {c["name"]: c for c in medium.cards(detail=0, min_size=100_000_000)}
ok(len(got) == 2 and 0.9 < got["Local.Hero.1983.1080p.BluRay.x264-GRP"]["size"] / true_film < 1.1, "two every four hundred: both, the film within a tenth")
ok(0.6 < got["Show.S01E02.720p.HDTV.x264-GRP"]["size"] / true_ep < 1.4, "and the episode about right")
lines = {x["h"]: x for x in full.poster_lines()}
ok(lines[U.poster_hash("Poster <p@x>")]["wanted"] == 6500 and lines[U.poster_hash("x <x@x>")]["junk"] == 900, "what each poster sent")

prog = S.Sketch("g")
prog.add_rows([(1, '"Holiday.Photos.2012.jpg.exe" yEnc (1/1)', "p", "", "<1@x>", 300_000, ()), (2, '[1/1] - "Comic 01 (2012).exe" yEnc (1/3)', "p", "", "<2@x>", 300_000, ())])
ok(prog.cards(detail=1) == [] and prog.count["program"] == 2, "a program never becomes a card")

# ---- talking to the registry --------------------------------------------------------------------------
ok(C.provider_key("news.example.com") == C.provider_key("ssl-eu.news.example.com") != C.provider_key("news.other.com"), "one key for all of a provider's servers")
ok(C.provider_key("a.example.co.uk") == C.provider_key("b.example.co.uk") != C.provider_key("a.other.co.uk"), "two-part endings understood")
ok(len(C.provider_key("x.y")) == 16 and C.provider_key("") == "" and C.provider_key("10.0.0.7") != C.provider_key("10.0.0.8"), "sixteen characters; nothing for nothing; an address is itself")
ok([(len(c), m) for c, m in C.pages(list(range(4500)))] == [(2000, True), (2000, True), (500, False)], "cards go in calls of two thousand, the last one saying so")
ok(list(C.pages([])) == [([], False)], "a window with nothing in it is still reported")


# ---- reading over several connections (a made-up news server, no network) --------------------
import threading
import time as _time


class _Sock:
    def __init__(self, srv=None):
        self.srv = srv

    def close(self):
        if self.srv is not None:                # a closed connection is a free place again
            with self.srv.lock:
                self.srv.opened -= 1
            self.srv = None


class FakeServer:
    """Articles 1..n of one group. Each is one part of some file of some release."""

    def __init__(self, articles, refuse_after=None, bad=(), slow=0.0):
        self.articles = articles          # number -> (subject, poster, date, message-id, bytes)
        self.refuse_after = refuse_after  # connections it allows at once
        self.bad = dict.fromkeys(bad, 0) if not isinstance(bad, dict) else bad      # (a, b) -> times it fails (None: always)
        self.failures = collections.Counter()
        self.opened = 0
        self.asked = []
        self.slow = slow
        self.lock = threading.Lock()

    def opener(self, cfg):
        with self.lock:
            if self.refuse_after is not None and self.opened >= self.refuse_after:
                raise U.NNTPError("502 too many connections")
            self.opened += 1
        nn = type("NN", (), {})()
        nn.at, nn.wire, nn.sock = "alt.binaries.test", 0, _Sock(self)
        self.most = max(getattr(self, "most", 0), self.opened)
        return nn

    def pull(self, nn, a, b, keep=None):
        with self.lock:
            self.asked.append((a, b))
            if (a, b) in self.bad:
                self.failures[(a, b)] += 1
                if self.bad[(a, b)] is None or self.failures[(a, b)] <= self.bad[(a, b)]:
                    raise U.NNTPError("connection closed")
        if self.slow:
            _time.sleep(self.slow)
        rows = [(n,) + self.articles[n] + ((),) for n in range(a, b + 1)
                if n in self.articles and (keep is None or keep(self.articles[n][1].encode()))]
        nn.wire += 40 * len(rows)
        return rows


import collections

POSTER = "Poster One <one@example.com>"


def post(arts, start, name, files, parts, poster=POSTER, tag="Some.Film.2010.1080p.BluRay.x264-GRP"):
    """Lay a release down from article `start`: `files` files of `parts` parts each."""
    n = start
    for fi in range(1, files + 1):
        fname = f"{name}.part{fi:02d}.rar" if files > 1 else name
        for p in range(1, parts + 1):
            arts[n] = (f'{tag} [{fi}/{files}] - "{fname}" yEnc ({p}/{parts})', poster, "Mon, 04 Jan 2010 10:00:00 +0000",
                       f"<{name}-{fi}-{p}@test>", 700_000)
            n += 1
    return n


ARTS = {}
_end = post(ARTS, 30_000, "some.film.2010.1080p.bluray.x264-grp", 40, 3000)            # 120,000 articles: 30,000..149,999
for _n in range(1, 30_000):                                                                # other people's posts before it
    ARTS[_n] = (f'junk [{_n % 7 + 1}/7] - "other{_n // 50}.bin" yEnc ({_n % 50 + 1}/50)', "Somebody Else <x@example.com>",
                "Mon, 04 Jan 2010 09:00:00 +0000", f"<o{_n}@test>", 500_000)
ok(_end == 150_000, "the made-up release is 120,000 articles")

srv = FakeServer(ARTS, slow=0.05)
got = []
st = S.read_window({}, "alt.binaries.test", 1, 149_999, connections=6, on_rows=got.append, opener=srv.opener, pull=srv.pull)
ok(st["headers"] == len(ARTS) and st["failed"] == [] and st["connections"] == 6 and st["requests"] == 8, f"six connections read everything, eight stretches: {st}")
ok(sorted(r[0] for rows in got for r in rows) == sorted(ARTS), "every article once, none twice")

# the provider allows two connections, twelve were asked for
srv = FakeServer(ARTS, refuse_after=2, slow=0.01)
got = []
st = S.read_window({}, "alt.binaries.test", 1, 149_999, connections=12, on_rows=got.append, opener=srv.opener, pull=srv.pull)
ok(st["failed"] == [] and st["headers"] == len(ARTS) and srv.most == 2, f"read in full with the two it allows: {srv.most} at once, {st['refused']} refusals")
ok(sorted(r[0] for rows in got for r in rows) == sorted(ARTS), "and still every article exactly once")

# a stretch that fails twice is read by another try; one that always fails is reported
srv = FakeServer(ARTS, bad={(20_001, 40_000): 2, (100_001, 120_000): None})
got = []
st = S.read_window({}, "alt.binaries.test", 1, 149_999, connections=4, on_rows=got.append, opener=srv.opener, pull=srv.pull)
ok(st["failed"] == [(100_001, 120_000)], f"the stretch the server never gives is named: {st['failed']}")
ok(srv.failures[(100_001, 120_000)] == S.TRIES and srv.failures[(20_001, 40_000)] == 3, "after four tries, and the flaky one came on its third")
ok(st["headers"] == len(ARTS) - 20_000, "everything else was read")
st = S.read_window({}, "alt.binaries.test", 1, 100, connections=5, opener=FakeServer(ARTS, refuse_after=0).opener, pull=srv.pull)
ok(st["failed"] == [(1, 100)] and st["headers"] == 0, "no connection at all: the stretch is reported, nothing hangs")
st = S.read_window({}, "alt.binaries.test", 1, 149_999, connections=3, stop=lambda: True, opener=srv.opener, pull=srv.pull)
ok(len(st["failed"]) == 8 and st["requests"] == 0, "stopped before it began: every stretch is reported unread")

# while somebody waits for an NZB the index reading begins nothing new
_held = [True]
_srv = FakeServer(ARTS)
_t = threading.Thread(target=lambda: S.read_window({}, "alt.binaries.test", 1, 149_999, connections=4, opener=_srv.opener, pull=_srv.pull, hold=lambda: _held[0]))
_t.start()
_time.sleep(0.5)
ok(_srv.asked == [], "held: not one stretch is asked for")
_held[0] = False
_t.join(20)
ok(len(_srv.asked) == 8 and not _t.is_alive(), "let go: it reads everything")

# ---- the NZB, over one connection and over twelve -------------------------------------------
PH = U.poster_hash(POSTER)
KEY = S.describe(ARTS[30_000][0])[3]
srv1 = FakeServer(ARTS)
one = S.rebuild({}, "alt.binaries.test", PH, KEY, 60_000, 61_000, 6000, "Some Film", connections=1, opener=srv1.opener, pull=srv1.pull)
srv12 = FakeServer(ARTS)
twelve = S.rebuild({}, "alt.binaries.test", PH, KEY, 60_000, 61_000, 6000, "Some Film", connections=12, opener=srv12.opener, pull=srv12.pull)
ok(one["seconds"] > 0, f"it says how long it took ({one['seconds']} s)")
ok(one["segments"] == 120_000 and one["complete"] and one["files"] == 40 and one["art_lo"] == 30_000 and one["art_hi"] == 149_999,
   f"seen at two articles in its middle, found whole: {one['segments']} parts, {one['art_lo']}..{one['art_hi']}")
ok(twelve["nzb"] == one["nzb"] and twelve["segments"] == one["segments"] and twelve["complete"], "twelve connections make the same NZB, byte for byte")
ok(twelve["connections"] > 1 and one["connections"] == 1, f"and did use several ({twelve['connections']})")
ok(one["nzb"].count("<segment ") == 120_000 and one["nzb"].count("<file ") == 40, "every part of every file is in it")
slow1, slow12 = FakeServer(ARTS, slow=0.3), FakeServer(ARTS, slow=0.3)
t = _time.time(); S.rebuild({}, "alt.binaries.test", PH, KEY, 60_000, 61_000, 6000, connections=1, opener=slow1.opener, pull=slow1.pull); t1 = _time.time() - t
t = _time.time(); S.rebuild({}, "alt.binaries.test", PH, KEY, 60_000, 61_000, 6000, connections=12, opener=slow12.opener, pull=slow12.pull); t12 = _time.time() - t
ok(len(slow12.asked) <= len(slow1.asked) * 3 + 2, f"without asking for much more ({len(slow1.asked)} requests against {len(slow12.asked)})")
ok(t12 < t1 - 0.6, f"and sooner when the server takes its time ({t1:.2f}s against {t12:.2f}s)")
# a stretch the server will not give: the NZB is what was read, and is not called complete
holed = FakeServer(ARTS, bad={(74_000 - 6000 + 20_000 * 0 + 0, 0): None})
bad_srv = FakeServer(ARTS)
orig_pull = bad_srv.pull


def _pull_with_hole(nn, a, b, keep=None):
    if a <= 100_500 <= b:
        raise U.NNTPError("connection closed")
    return orig_pull(nn, a, b, keep)


part = S.rebuild({}, "alt.binaries.test", PH, KEY, 60_000, 61_000, 6000, connections=4, opener=bad_srv.opener, pull=_pull_with_hole)
ok(part["unread"] >= 1 and not part["complete"] and 0 < part["segments"] < 120_000 and part["nzb"],
   f"with a stretch unread it says so and is not called complete ({part['segments']} of 120,000 parts, {part['unread']} unread)")
gone = S.rebuild({}, "alt.binaries.test", PH, "no such release", 60_000, 61_000, 6000, connections=4, opener=bad_srv.opener, pull=orig_pull)
ok(gone["nzb"] == "" and gone["segments"] == 0, "a release that is not there gives no NZB")
try:
    S.rebuild({}, "alt.binaries.test", PH, KEY, 60_000, 61_000, 6000, connections=4, opener=FakeServer(ARTS, refuse_after=0).opener, pull=orig_pull)
    ok(False, "no connection must be an error, not an empty NZB")
except U.NNTPError:
    ok(True, "no connection at all is an error - not 'no longer on the server'")

ok(twelve["refused"] == 0 and twelve["wanted"] >= 4 and one["wanted"] == 1, f"it says how many connections it had work for ({twelve['wanted']}) and that none was refused")
tight = FakeServer(ARTS, refuse_after=3, slow=0.02)
few = S.rebuild({}, "alt.binaries.test", PH, KEY, 60_000, 61_000, 6000, "Some Film", connections=16, opener=tight.opener, pull=tight.pull)
ok(few["nzb"] == one["nzb"] and tight.most == 3 and few["refused"] > 0, f"a provider that allows three: the same NZB over three, and the refusals are counted ({few['refused']})")
ok(S.next_connections(16, few) <= 3, f"so the next NZB asks for no more than it allows ({S.next_connections(16, few)})")

# how many connections next time
ok(S.next_connections(16, {"connections": 16, "wanted": 16, "refused": 0}) == 22, "sixteen were open and none refused: twenty-two next time")
ok(S.next_connections(22, {"connections": 22, "wanted": 22, "refused": 0}) == 28, "and on, six at a time")
ok(S.next_connections(46, {"connections": 46, "wanted": 46, "refused": 0}) == 50, "to fifty at most")
ok(S.next_connections(22, {"connections": 17, "wanted": 22, "refused": 9}) == 17, "refused beyond seventeen: seventeen it is")
ok(S.next_connections(16, {"connections": 0, "wanted": 16, "refused": 16}) == 1, "never below one")
ok(S.next_connections(16, {"connections": 3, "wanted": 3, "refused": 0}) == 16, "a small release that had work for three says nothing about sixteen")
ok(S.next_connections(16, {"connections": 16, "wanted": 16, "refused": 2}) == 16, "all open in the end but some had to ask twice: this is the level")
ok(S.next_connections(None, {}) == 16 and S.next_connections("junk", {}) == 16, "sixteen to begin with")

# rows of other posters are turned down before they are decoded
_decoded = []
_real_decode = U.decode
U.decode = lambda b: (_decoded.append(1), _real_decode(b))[1]
try:
    srv_k = FakeServer(ARTS)
    S.rebuild({}, "alt.binaries.test", PH, KEY, 60_000, 61_000, 6000, connections=4, opener=srv_k.opener, pull=srv_k.pull)
finally:
    U.decode = _real_decode
ok(len(_decoded) <= 4, f"each poster is decoded once, however many articles ({len(_decoded)} decodes for 150,000 articles)")

# ---- asking the server for one field first -----------------------------------------------------
class WireNN:
    """A connection that answers from the made-up articles, speaking the protocol's lines."""

    def __init__(self, arts, xhdr=True, server="news.test"):
        self.arts, self.xhdr, self.server = arts, xhdr, server
        self.packed, self.wire, self.at, self.said = False, 0, "alt.binaries.test", []
        self.sock, self.f = self, self
        self.head, self.lines = b"", []

    def sendall(self, data):
        cmd = data.decode().strip()
        self.said.append(cmd)
        word, rest = cmd.split(" ", 1)
        lo, hi = (int(x) for x in rest.split(" ")[-1].split("-"))
        nums = [n for n in range(lo, hi + 1) if n in self.arts]
        if word == "XHDR" and not self.xhdr:
            self.head, self.lines = b"500 What?", []
        elif word == "XHDR":
            self.head, self.lines = b"221 From follows", [f"{n} {self.arts[n][1]}".encode() for n in nums]
        else:
            self.head = b"224 overview follows"
            self.lines = ["\t".join([str(n), self.arts[n][0], self.arts[n][1], self.arts[n][2], self.arts[n][3], "", str(self.arts[n][4]), "1",
                                     "Xref: news.test alt.binaries.test:" + str(n)]).encode() for n in nums]

    def _line(self):
        return self.head

    def _multiline(self):
        return self.lines

    def close(self):
        pass


MIXED = dict(ARTS)
for _n in range(200_000, 220_000):                                    # a stretch where the poster has 40 articles among 20,000
    MIXED[_n] = ARTS[30_000 + (_n - 200_000) // 500] if _n % 500 == 250 else (
        'x - "noise.bin" yEnc (1/1)', "Somebody Else <x@example.com>", "Mon, 04 Jan 2010 09:00:00 +0000", f"<n{_n}@test>", 500_000)
_mine = lambda raw: U.poster_hash(U.decode(raw)) == PH
nn = WireNN(MIXED)
rows = S.xover_of(nn, 200_000, 219_999, _mine)
ok(len(rows) == 40 and all(r[2] == POSTER for r in rows), f"the poster's forty articles out of twenty thousand ({len(rows)})")
ok(nn.said == ["XHDR From 200000-219999", "XOVER 200250-219750"], f"asked who posted what, then for the stretch they are in and no wider: {nn.said}")
nn = WireNN(MIXED)
ok(S.xover_of(nn, 1, 20_000, _mine) == [] and nn.said == ["XHDR From 1-20000"], "a stretch with nothing of theirs costs one small question and no headers")
ok(S.numbers_by(WireNN(MIXED), 29_990, 30_010, _mine) == list(range(30_000, 30_011)), "the numbers, in order")
old = WireNN(MIXED, xhdr=False, server="old.test")
rows = S.xover_of(old, 200_000, 219_999, _mine)
ok(len(rows) == 40 and old.said == ["XHDR From 200000-219999", "XOVER 200000-219999"], "a server without the one-field command is asked for everything")
old2 = WireNN(MIXED, xhdr=False, server="old.test")
S.xover_of(old2, 200_000, 219_999, _mine)
ok(old2.said == ["XOVER 200000-219999"] and "old.test" in S.NO_FROM_FIELD and "news.test" not in S.NO_FROM_FIELD, "and is not asked for it a second time")

# ---- small files, read in full: listed with their Message-IDs --------------------------------
SMALL = {}
_n = 1
for _i, (_nm, _parts) in enumerate((("Author A - First Book.epub", 2), ("Author B - Second Book.pdf", 5), ("Author C - Third.epub", 1))):
    for _p in range(1, _parts + 1):
        SMALL[_n] = (f'Ebook collection March [{_i + 1}/3] - "{_nm}" yEnc ({_p}/{_parts})', POSTER, "Mon, 04 Jan 2010 10:00:00 +0000", f"<b{_i}-{_p}@test>", 300_000)
        _n += 1
sk = S.Sketch("alt.binaries.e-book", items=True)
sk.add_rows([(k,) + v + ((),) for k, v in SMALL.items()])
cards = sk.cards(detail=1)
ok(len(cards) == 1 and cards[0]["kind"] == "ebook", f"one card for the post: {[(c['name'], c['kind']) for c in cards]}")
its = cards[0].get("items") or []
ok([(i["name"], len(i["message_ids"])) for i in its] == [("Author A - First Book.epub", 2), ("Author B - Second Book.pdf", 5), ("Author C - Third.epub", 1)],
   f"its three books listed, each with the Message-IDs of its parts: {[(i['name'], len(i['message_ids'])) for i in its]}")
ok(its[1]["message_ids"] == [f"<b1-{p}@test>" for p in range(1, 6)], "in the order of the parts")
half = S.Sketch("alt.binaries.e-book", items=True)
half.add_rows([(k,) + v + ((),) for k, v in SMALL.items() if k != 4])                      # one part of the second book missing
ok([i["name"] for i in half.cards(detail=1)[0]["items"]] == ["Author A - First Book.epub", "Author C - Third.epub"], "a book with a part missing is not listed")
sparse = S.Sketch("alt.binaries.e-book")
sparse.add_rows([(k,) + v + ((),) for k, v in SMALL.items()])
ok("items" not in sparse.cards(detail=0)[0], "a sparse reading lists nothing: it has not seen every part")
film = S.Sketch("alt.binaries.test", items=True)
film.add_rows([(k,) + ARTS[k] + ((),) for k in range(30_000, 36_000)])
ok(all("items" not in c for c in film.cards(detail=1)), "a film is never listed file by file")

# ---- judging on a thread of its own ----------------------------------------------------------
seen_rows = []
fd = S.Feeder(seen_rows.extend, waiting=2)
for _k in range(50):
    fd.put([_k])
fd.close()
ok(seen_rows == list(range(50)), "everything handed in is judged, in order, before close returns")
fd = S.Feeder(lambda rows: 1 / 0, waiting=2)
for _k in range(10):
    fd.put([_k])                                    # must not block for ever when the judge has failed
try:
    fd.close()
    ok(False, "a failure while judging must come out")
except ZeroDivisionError:
    ok(True, "a failure while judging comes out at close, and no reader was left waiting")

# ---- what is never kept and what is adult: judged here before anything is sent or stored ------
import usenet_rules as R


def _c(name, groups, **k):
    return dict({"name": name, "inner_name": None, "groups": groups, "items": [], "audience": "sfw"}, **k)


for _card, _forb, _adult, _why in (
    (_c("Some.Film.2010.1080p.BluRay.x264-GRP", ["alt.binaries.movies"]), False, False, "an ordinary film"),
    (_c("Lolita.1962.1080p.BluRay.x264-GRP", ["alt.binaries.movies"]), False, False, "Kubrick's film, in a film group"),
    (_c("Coffret Melissa Lauren.Vicieuses Lolitas.French", ["alt.binaries.french.xxx"]), True, True, "the same word in an adult group"),
    (_c("Private.Gold.123.XXX.DVDRip", ["alt.binaries.erotica"]), False, True, "an adult film: kept, labelled"),
    (_c("pthc collection", ["alt.binaries.multimedia"]), True, False, "a term for it, in any group"),
    (_c("holiday pictures", ["alt.binaries.pictures.erotica.pre-teen"]), True, True, "a group named for it"),
    (_c("Some.Adult.Film.XXX", ["alt.binaries.erotica", "alt.binaries.erotica.teens"]), False, True, "an adult film cross-posted to a group that is not read"),
    (_c("cute 14yo XXX", ["alt.binaries.erotica"]), True, True, "an age under eighteen in an adult setting"),
    (_c("My.14.Years.Old.Son.Plays.Piano", ["alt.binaries.sounds.mp3"]), False, False, "the same age where nothing is adult"),
    (_c("Cory in the House - 1x12", ["alt.binaries.tv.disney-channel", "alt.binaries.multimedia.teen-idols"]), False, False, "a Disney series cross-posted to a group that is not read"),
    (_c("Two and a Half Men - 6x17 - The 'Ocu' or the 'Pado'", ["alt.binaries.hdtv"]), False, False, "an episode title"),
    (_c("Analysis of Sussex County Records", ["alt.binaries.e-book"]), False, False, "'anal' and 'sex' inside other words"),
    (_c("Album", ["alt.binaries.sounds.mp3"], items=[{"name": "01 - preteen xxx.jpg"}]), True, False, "a listed file's name counts"),
):
    ok(R.forbidden(_card) == _forb and R.adult(_card) == _adult, f"{_why}: never kept {R.forbidden(_card)}, adult {R.adult(_card)}")
ok(R.group_never("alt.binaries.erotica.teen") and R.group_never("alt.binaries.multimedia.teen-idols") and not R.group_never("alt.binaries.erotica")
   and not R.group_never("alt.binaries.movies.kids"), "the groups that are not read")
ok(R.forbidden({"name": "x", "groups": ["alt.binaries.tv"], "audience": None}) is False and R.forbidden({"name": "pthc", "groups": None}) is True, "cards with things missing are judged all the same")


def _post(arts, start, subject_of, n, poster=POSTER):
    for k in range(n):
        arts[start + k] = (subject_of(k), poster, "Mon, 04 Jan 2010 10:00:00 +0000", f"<r{start + k}@test>", 700_000)


_arts = {}
_post(_arts, 1, lambda k: f'Private.Gold.123.XXX.DVDRip.XviD-GRP [01/40] - "private.gold.123.xxx.part01.rar" yEnc ({k + 1}/200)', 200)
_post(_arts, 1000, lambda k: f'Some.Film.2010.1080p.BluRay.x264-GRP [01/40] - "some.film.2010.part01.rar" yEnc ({k + 1}/200)', 200)
_post(_arts, 2000, lambda k: f'pthc.collection.2009.DVDRip.XviD [01/40] - "collection.2009.part01.rar" yEnc ({k + 1}/200)', 200)
sk = S.Sketch("alt.binaries.multimedia")
sk.add_rows([(k,) + v + ((),) for k, v in _arts.items()])
_cards = {c["name"][:12]: c for c in sk.cards(detail=0)}
ok(len(_cards) == 2 and sk.count["never kept"] == 1, f"of three releases read, the one that is never kept makes no card: {sorted(_cards)}")
ok(_cards["Private.Gold"]["audience"] == "nsfw" and _cards["Some.Film.20"]["audience"] == "sfw", "and the adult one is sent labelled adult")
sk2 = S.Sketch("alt.binaries.pictures.erotica.pre-teen")
sk2.add_rows([(k,) + v + ((),) for k, v in _arts.items() if 1000 <= k < 2000])
ok(sk2.cards(detail=0) == [], "nothing read in a group named for it makes a card, whatever it is called")
# the registry's copy of the rules and this one must be the same text
ok("FORBIDDEN_SQL" in dir(R) and R.FORBIDDEN_RX.pattern == R.FORBIDDEN_SQL and R.MINOR_RX.pattern == R.MINOR_SQL, "the patterns Postgres is given are the ones Python uses")

ok(R.group_never("alt.binaries.pictures.erotica.teenfemale") and R.group_never("alt.binaries.pictures.erotica.teensex")
   and R.group_never("alt.binaries.erotica.youngmen") and not R.group_never("alt.binaries.tv.teen") and not R.group_never("alt.binaries.sounds.mp3.youngsters"),
   "an adult group named for the young, also when the word runs on; a group for the young that is not adult is an ordinary group")

# ---- the request boards: the counter is read, the board's tag is not part of the name --------
_r1 = ['a.b.mm@efnet - req 73838 - Criminal.Minds.S01.DVDRip - %03d/280 - "cm.s01.part%03d.rar" yEnc (%d/101)' % (f, f, p) for f in (1, 83, 280) for p in (1, 50)]
_d = [S.describe(x) for x in _r1]
ok(len({d[3] for d in _d}) == 1, f"every file of the release has the same key, whichever file it is: {sorted({d[3] for d in _d})}")
ok(_d[0][2] == "Criminal.Minds.S01.DVDRip" and _d[0][6] == 280, f"its name without the board's tag and the counter, and 280 files expected: {_d[0][2]!r}, {_d[0][6]}")
ok(U.parse_subject(_r1[2])["file_no"] == 83, "and which file this is")
_b = S.describe('[12345]-[#altbin@efnet]-[FULL]-[Who.Do.You.Think.You.Are.S06E01.720p.HDTV.x264-GRP]-[03/41] - "wdytya.s06e01.r01" yEnc (5/66)')
ok(_b[2] == "Who.Do.You.Think.You.Are.S06E01.720p.HDTV.x264-GRP" and _b[6] == 41, f"the bracketed form: {_b[2]!r}")
ok(J.strip_request("a.b.mm@efnet - req xxxxx - Stargate.SG1.S01E01.AC3.DivX.DVDRip") == "Stargate.SG1.S01E01.AC3.DivX.DVDRip", "a request with its number crossed out")
ok(J.strip_request("#a.b.teevee@EFNet - Some.Show.S02E03.HDTV.XviD-GRP") == "Some.Show.S02E03.HDTV.XviD-GRP", "a channel alone in front")
for _plain in ("Some.Film.2010.1080p.BluRay.x264-GRP", "Miles Davis At Newport", "user@example.com wrote a long thing here", "Top Gear - 12/2009 Special"):
    ok(J.strip_request(_plain) == _plain or len(J.strip_request(_plain)) >= 6, f"an ordinary name is left as it is, or nearly: {J.strip_request(_plain)!r}")
ok(J.strip_request("Some.Film.2010.1080p.BluRay.x264-GRP") == "Some.Film.2010.1080p.BluRay.x264-GRP" and J.strip_request("Miles Davis At Newport") == "Miles Davis At Newport", "exactly as it is")
_p = U.parse_subject('Some Show - 9/11 Special - "some.show.911.special.avi" yEnc (1/50)')
ok(_p["title"].startswith("Some Show") and "9/11" in _p["title"] and _p["files"] is None, f"a date in the middle of a title is not a counter: {_p['title']!r}")
_p = U.parse_subject('Big Set - 300/200 - "x.rar" yEnc (1/50)')
ok(_p["files"] is None, "nor are two numbers the wrong way round")
_sk = S.Sketch("alt.binaries.multimedia")
_sk.add_rows([(k, x, POSTER, "Mon, 04 Jan 2010 10:00:00 +0000", f"<q{k}@t>", 700_000, ()) for k, x in enumerate(_r1, 1)])
_cc = _sk.cards(detail=0)
ok(len(_cc) == 1 and _cc[0]["files"] == 3 and _cc[0]["files_expected"] == 280 and _cc[0]["name"] == "Criminal.Minds.S01.DVDRip",
   f"three files of one release seen: one card, not three ({[(c['name'], c['files']) for c in _cc]})")

print(f"ok - {n} checks")
