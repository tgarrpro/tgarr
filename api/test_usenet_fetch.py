"""usenet_fetch: yEnc, what is taken from an NZB, a file put together and checked."""
import os
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usenet as U  # noqa: E402
import usenet_fetch as F  # noqa: E402

n = 0


def check(what, got, want):
    global n
    n += 1
    assert got == want, f"{what}: {got!r}, wanted {want!r}"


def raises(what, fn):
    global n
    n += 1
    try:
        fn()
    except F.Missing:
        return
    raise AssertionError(f"{what}: nothing was raised")


def encode(data: bytes, name: str, part=None, total_size=None, begin=1, line=128, crc=True) -> list:
    """A yEnc article as a posting program writes it (lines without line ends)."""
    body, row = [], bytearray()
    for b in data:
        c = (b + 42) & 255
        if c in (0, 10, 13, 61) or (not row and c in (9, 32, 46)):
            row += bytes((61, (c + 64) & 255))
        else:
            row.append(c)
        if len(row) >= line:
            body.append(bytes(row))
            row = bytearray()
    if row:
        body.append(bytes(row))
    c32 = "%08x" % (zlib.crc32(data) & 0xFFFFFFFF)
    if part is None:
        return ([b"=ybegin line=%d size=%d name=%s" % (line, len(data), name.encode())] + body
                + [b"=yend size=%d" % len(data) + (b" crc32=" + c32.encode() if crc else b"")])
    return ([b"=ybegin part=%d line=%d size=%d name=%s" % (part, line, total_size, name.encode()),
             b"=ypart begin=%d end=%d" % (begin, begin + len(data) - 1)] + body
            + [b"=yend size=%d part=%d" % (len(data), part) + (b" pcrc32=" + c32.encode() if crc else b"")])


every = bytes(range(256)) * 9 + b"=\r\n.\x00 tail"
# ---- one article
data, info = F.decode(encode(every, "A book.epub"))
check("every byte comes back", data, every)
check("its name, with the space", info["name"], "A book.epub")
check("its size", info["size"], len(every))
check("one piece: no place", info["begin"], None)
data, info = F.decode([b"some text before", b""] + encode(b"hello", "x.txt", part=2, total_size=1000, begin=501))
check("a part", (data, info["begin"], info["size"]), (b"hello", 501, 1000))
check("nothing in it", F.decode(encode(b"", "e.jpg"))[0], b"")
raises("not yEnc", lambda: F.decode([b"just a letter", b"to the group"]))
raises("cut short", lambda: F.decode(encode(every, "a.epub")[:-1]))
bad = encode(every, "a.epub")
bad[3] = bad[3][:-1] + bytes(((bad[3][-1] + 1) & 255 or 65,))
raises("one byte changed on the way", lambda: F.decode(bad))
short = encode(every, "a.epub")
del short[4]
raises("a line lost on the way", lambda: F.decode(short))
check("no checksum given: the size still has to be right", F.decode(encode(every, "a.epub", crc=False))[0], every)

# ---- which files of an NZB
check("a book", F.kind_of("Dune.EPUB"), "ebook")
check("a song", F.kind_of("01 - Intro.flac"), "audio")
check("a picture", F.kind_of("cover.jpg"), "photo")
for name in ("film.mkv", "setup.exe", "book.rar", "book.epub.exe", "noext", "", "a.par2", "x.zip", "run.bat", "page.html"):
    check("not taken: " + name, F.kind_of(name), None)
check("name in quotes", F.file_name('[01/12] - "My Book - Vol 2.epub" yEnc (1/3)'), "My Book - Vol 2.epub")
check("name without quotes", F.file_name("Some.Song.mp3 yEnc (1/9)"), "Some.Song.mp3")
check("no name", F.file_name("hello there"), "")


def rel(files: dict) -> dict:
    return {"title": "t", "group": "alt.binaries.test", "files": {
        k: {"poster": "p", "date": "", "subject": f'"{k}" yEnc (1/{len(v)})', "bytes": sum(v), "total": len(v),
            "parts": {i + 1: (f"<m{k.replace(' ', '_')}{i}@x>", s) for i, s in enumerate(v)}}
        for k, v in files.items()}}


nzb = U.build_nzb(rel({"Book.epub": [700000, 500000], "Book.par2": [40000], "Book.nfo": [900]}))
files = F.parse_nzb(nzb)
check("files of the NZB", sorted(f["name"] for f in files), ["Book.epub", "Book.nfo", "Book.par2"])
book = [f for f in files if f["name"] == "Book.epub"][0]
check("its parts in order", [s[1] for s in book["segments"]], ["mBook.epub0@x", "mBook.epub1@x"])
check("its bytes", book["bytes"], 1200000)
p = F.plan(files)
check("the book is taken, the helpers are not", [(f["name"], f["kind"]) for f in p["take"]], [("Book.epub", "ebook")])
p = F.plan(F.parse_nzb(U.build_nzb(rel({"Book.part01.rar": [5000000], "Book.part02.rar": [5000000], "Book.par2": [1]}))))
check("packed: nothing taken", (p["take"], "archive" in p["why"]), ([], True))
p = F.plan(F.parse_nzb(U.build_nzb(rel({"Film.2010.mkv": [900000000], "cover.jpg": [90000]}))))
check("a film's cover is taken, the film is not", [f["name"] for f in p["take"]], ["cover.jpg"])
p = F.plan(F.parse_nzb(U.build_nzb(rel({"Film.2010.mkv": [900000000]}))))
check("video: nothing taken, and said why", (p["take"], "video" in p["why"]), ([], True))
p = F.plan(F.parse_nzb(U.build_nzb(rel({"setup.exe": [900000], "readme.txt": [100]}))))
check("a program: nothing taken", p["take"], [])
p = F.plan(F.parse_nzb(U.build_nzb(rel({"huge.pdf": [F.MOST["ebook"] + 50 * F.MB]}))))
check("too large", (p["take"], p["why"]), ([], "too large to fetch here"))
p = F.plan(F.parse_nzb(U.build_nzb(rel({f"{i:03d}.jpg": [1000] for i in range(F.MOST_FILES + 1)}))))
check("too many", (p["take"], "too many" in p["why"]), ([], True))
check("an album", len(F.plan(F.parse_nzb(U.build_nzb(rel({f"{i:02d} - Song.mp3": [4000000, 3000000] for i in range(12)}))))["take"]), 12)
check("not an NZB", F.parse_nzb("<html>no</html>"), [])
check("broken text", F.parse_nzb("<nzb><file"), [])
evil = nzb.replace("mBook.epub0@x", "a@b>\r\nQUIT")
check("a message-id with a command in it is dropped", [s[1] for f in F.parse_nzb(evil) if f["name"] == "Book.epub" for s in f["segments"]], ["mBook.epub1@x"])

# ---- a whole file
tmp = tempfile.mkdtemp()
blob = os.urandom(300000) + every
cuts = [(0, 120000), (120000, 240000), (240000, len(blob))]
store = {f"p{i}@x": encode(blob[a:b], "Real name.epub", part=i + 1, total_size=len(blob), begin=a + 1) for i, (a, b) in enumerate(cuts)}
f = {"name": "x.epub", "segments": [(i + 1, f"p{i}@x", 0) for i in range(3)]}
seen = []
got = F.fetch_file(store.get, f, os.path.join(tmp, "x.epub"), seen.append)
check("the file is whole", open(os.path.join(tmp, "x.epub"), "rb").read() == blob, True)
check("what is said of it", (got["size"], got["name"]), (len(blob), "Real name.epub"))
check("counted as it came", sum(seen), len(blob))
check("no leftover", os.listdir(tmp), ["x.epub"])
back = dict(f, segments=list(reversed(f["segments"])))
F.fetch_file(store.get, back, os.path.join(tmp, "y.epub"))
check("parts in any order", open(os.path.join(tmp, "y.epub"), "rb").read() == blob, True)
gone = dict(store)
del gone["p1@x"]
raises("a part is gone", lambda: F.fetch_file(gone.get, f, os.path.join(tmp, "z.epub")))
check("nothing kept of it", sorted(os.listdir(tmp)), ["x.epub", "y.epub"])
two = dict(f, segments=f["segments"][:2])
raises("the last part was never posted", lambda: F.fetch_file(store.get, two, os.path.join(tmp, "z.epub")))
check("nothing kept of that either", sorted(os.listdir(tmp)), ["x.epub", "y.epub"])
one = {"name": "s.mp3", "segments": [(1, "s@x", 0)]}
F.fetch_file({"s@x": encode(every, "s.mp3")}.get, one, os.path.join(tmp, "s.mp3"))
check("a file in one piece", open(os.path.join(tmp, "s.mp3"), "rb").read() == every, True)

print(f"test_usenet_fetch: {n} checks passed")
