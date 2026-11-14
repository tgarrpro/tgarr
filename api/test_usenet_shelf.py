"""The shelves of the Usenet page, the name one fetched file is kept under, comic pages:
read out of main.py without starting it."""
import ast
import os
import re
import tempfile
import zipfile

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), encoding="utf-8").read()
tree = ast.parse(src)
want = {"_UN_SHELVES", "_UN_AUDIOBOOK_GRP", "_UN_COMIC_GRP", "_UN_SHELF_SQL", "_UN_FOLDER", "_un_shelf", "_un_single_name",
        "_AUDIOBOOK_WORDS", "_AUDIOBOOK_PATH", "_AUDIOBOOK_SQL", "_is_audiobook", "_COMIC_PAGE_RX", "_COMIC_PAGE_MOST", "_COMIC_CACHE", "_comic_pages"}
ns = {"re": re, "os": os, "_zipfile": zipfile}
for node in tree.body:
    names = []
    if isinstance(node, ast.FunctionDef):
        names = [node.name]
    elif isinstance(node, ast.Assign):
        for tg in node.targets:
            names += [e.id for e in (tg.elts if isinstance(tg, ast.Tuple) else [tg]) if isinstance(e, ast.Name)]
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        names = [node.target.id]
    if want & set(names):
        exec(compile(ast.Module([node], []), "main.py", "exec"), ns)
n = 0


def check(what, got, wanted):
    global n
    n += 1
    assert got == wanted, f"{what}: {got!r}, wanted {wanted!r}"


S = ns["_un_shelf"]
check("an audiobook by its group", S("audio", "alt.binaries.sounds.audiobooks.repost", "x"), "audiobook")
check("an audiobook by its name", S("audio", "alt.binaries.sounds.mp3", "Dune (Unabridged)"), "audiobook")
check("an audiobook file", S("audio", "alt.binaries.sounds.mp3", "Dune.m4b"), "audiobook")
check("books read aloud", S("audio", "alt.binaries.sounds.mp3.books", "x"), "audiobook")
check("music", S("audio", "alt.binaries.sounds.flac", "An Album 2012"), "audio")
check("a comic by its group", S("ebook", "alt.binaries.pictures.comics", "Gazette 176"), "comic")
check("a comic by its name", S("ebook", "alt.binaries.ebook", "Thing 01.cbz"), "comic")
check("a magazine", S("ebook", "alt.binaries.e-book.magazines", "Wired 2014-03"), "magazine")
check("a book", S("ebook", "alt.binaries.e-book.technical", "Learning C"), "ebook")
check("a book about audiobooks is a book", S("ebook", "alt.binaries.ebook", "The Audiobook Guide"), "ebook")
check("pictures", S("photo", "alt.binaries.pictures.comics", "x"), "photo")
check("every shelf has a folder", sorted(ns["_UN_FOLDER"]), sorted(s[0] for s in ns["_UN_SHELVES"]))
check("the SQL names every shelf it can give", sorted(set(re.findall(r"THEN '(\w+)'", ns["_UN_SHELF_SQL"])) | {"ebook", "audio", "photo"}),
      sorted(s[0] for s in ns["_UN_SHELVES"]))

N = ns["_un_single_name"]
check("a name that says nothing gets the release in front", N("The Menomonee Falls Gazette 176", "(1975-04-28)(c2c)(Pete).cbz"),
      "The Menomonee Falls Gazette 176 (1975-04-28)(c2c)(Pete).cbz")
check("a cover", N("Miles Davis - Kind of Blue", "AlbumArtLarge.jpg"), "Miles Davis - Kind of Blue AlbumArtLarge.jpg")
check("a name that already says it", N("Dune - Frank Herbert", "Dune - Frank Herbert (1965).epub"), "Dune - Frank Herbert (1965).epub")
check("the release is the file's name and more", N("Frank Herbert - Dune.epub yEnc", "Frank Herbert - Dune.epub"), "Frank Herbert - Dune.epub")
check("no release name", N("", "a.epub"), "a.epub")
check("never longer than a file name may be", len(N("x" * 400, "a.epub")) <= 160, True)

A = ns["_is_audiobook"]
check("an .m4b is a book", A("Dune.m4b"), True)
check("fetched from the audiobooks shelf", A("01.mp3", "usenet/audiobooks/Dune/01.mp3"), True)
check("and its 18+ twin", A("01.mp3", "usenet18/audiobooks/X/01.mp3"), True)
check("a folder of one's own called Audiobooks", A("01.mp3", "mine/Audiobooks/Dune/01.mp3"), True)
check("called one in its album", A("ch01.mp3", "", "Chapter 1", "Dune (Unabridged)"), True)
check("in Chinese", A("三体 有声书 第01集.mp3"), True)
check("an hour of dance music is not one", A("DJ - House Mix vol.9.mp3", "", "House Mix", ""), False)
check("a song about books is not one", A("Paperback Writer.flac", "usenet/music/The Beatles/Paperback Writer.flac", "Paperback Writer", "1"), False)
check("the SQL has the same three ways", [x in ns["_AUDIOBOOK_SQL"] for x in (ns["_AUDIOBOOK_WORDS"], ns["_AUDIOBOOK_PATH"], "m4b")], [True] * 3)

tmp = tempfile.mkdtemp()
z = os.path.join(tmp, "c.cbz")
with zipfile.ZipFile(z, "w") as zf:
    for name in ("p10.jpg", "p2.jpg", "p1.JPG", "__MACOSX/._p1.jpg", "notes.txt", "sub/", ".hidden.png", "cover.png"):
        zf.writestr(name, b"x")
check("pages in reading order, nothing else", ns["_comic_pages"](z), ["cover.png", "p1.JPG", "p2.jpg", "p10.jpg"])
bad = os.path.join(tmp, "r.cbr")
open(bad, "wb").write(b"Rar!\x1a\x07\x00 not a zip")
try:
    ns["_comic_pages"](bad)
    raise AssertionError("a RAR was read as a zip")
except zipfile.BadZipFile:
    n += 1
print(f"test_usenet_shelf: {n} checks passed")
