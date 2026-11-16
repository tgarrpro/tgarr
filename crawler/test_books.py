"""What the crawler takes for a book, and the name a fetched file is kept under: read out of
main.py without starting it. Run: python test_books.py"""
import ast
import os
import re

here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, "main.py"), encoding="utf-8").read()
want = {"SAFE_NAME", "BOOK_KINDS", "BOOK_RX", "BOOK_SQL", "BOOK_MIN_BYTES", "stored_name", "_MIN_SIZES", "_AD_KEYWORDS_CORE", "_AD_KEYWORDS_PHOTO",
        "_quality_check"}
ns = {"re": re}
for node in ast.parse(src).body:
    names = []
    if isinstance(node, ast.FunctionDef):
        names = [node.name]
    elif isinstance(node, ast.Assign):
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
    if want & set(names):
        exec(compile(ast.Module([node], []), "main.py", "exec"), ns)
n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


stored_name, check = ns["stored_name"], ns["_quality_check"]
ok(stored_name("My Book (2020).epub", 7) == "My_Book_2020_.epub", "the sender's name, made safe")
ok(stored_name("My Book (2020).epub", 7, taken=True) == "My_Book_2020_.7.epub", "another file has that name: this row's number goes in, before the kind")
ok(stored_name(None, 7) == "item-7" and stored_name("", 7, taken=True) == "item-7.7", "a file without a name")
long = stored_name("x" * 400 + ".azw3", 1)
ok(long.endswith(".azw3") and len(long) <= 180, "a very long name is cut, and still says what kind of file it is")
ok(stored_name("x" * 400 + ".azw3", 123456789, taken=True).endswith(".123456789.azw3"), "and so is one with a number in it")
ok(stored_name("archive.tar.gz", 1) == "archive.tar.gz" and stored_name("noext", 5, taken=True) == "noext.5", "names with two dots, or none")
ok(stored_name("../../etc/passwd", 1) == ".._.._etc_passwd" and "/" not in stored_name("a/b\\c.pdf", 1), "never a path")

# the api's list of what a reader opens, and this one, are the same list
api = open(os.path.join(here, "..", "api", "main.py"), encoding="utf-8").read()
own = open(os.path.join(here, "..", "api", "ownbooks.py"), encoding="utf-8").read()
kinds = re.search(r"^KINDS = \(([^)]*)\)", own, re.M | re.S).group(1)
api_kinds = set(re.findall(r'"(\w+)"', kinds)) | {"pdf", "epub", "djvu", "djv", "cbz", "cbr"}
ok('("pdf", "epub", "djvu", "djv", "cbz", "cbr") + ownbooks.KINDS' in api, "the api builds its list from these six and what ownbooks reads")
ok(set(ns["BOOK_KINDS"]) == api_kinds, f"crawler and api agree on what a book is: {sorted(set(ns['BOOK_KINDS']) ^ api_kinds)}")
ok(ns["BOOK_RX"].search("War and Peace.FB2") and not ns["BOOK_RX"].search("setup.exe") and not ns["BOOK_RX"].search("fb2"), "by the end of the name")
ok(ns["BOOK_SQL"] == "file_name ~* '\\.(" + "|".join(ns["BOOK_KINDS"]) + ")$'", "the same list for PostgreSQL")

# small books are kept; small junk is not
ok(check("story.txt", 5000, "document", "")[0], "a 5 KB story is a book")
ok(check("poem.fb2", 1200, "document", None)[0], "a short poem too")
ok(not check("x.txt", 300, "document", "")[0], "a note of a few words is not")
ok(not check("installer.bin", 5000, "document", "")[0] and not check(None, 5000, "document", "")[0], "a small file that is not a book is still dropped")
ok(check("installer.bin", 50000, "document", "")[0] and check("film.mkv", 900_000_000, "video", "")[0], "what was kept before is kept")
ok(not check("clip.mp4", 100_000, "video", "")[0], "a tiny video is still dropped")
print(f"test_books: {n} checks passed")
