"""Checks for the Internet Archive source: what is asked, what is understood of the answer,
and which of an item's files are taken. No network. Run: python test_archive_org.py"""
import json
import urllib.parse

import archive_org as A

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


# ---- what is asked ----------------------------------------------------------------------------
ok(A.clean_query('night of the "living" dead') == "night of the living dead", "plain words")
ok(A.clean_query("title:(x) AND NOT y OR z") == "title x y z", "the search's own grammar is not passed on: " + A.clean_query("title:(x) AND NOT y OR z"))
ok(A.clean_query("don't look now") == "don't look now" and A.clean_query("三国演义 第1回") == "三国演义 第1回", "apostrophes and other scripts stay")
ok(A.clean_query("Duck and Cover") == "Duck and Cover" and A.clean_query("to be or not to be") == "to be or not to be", "and, or, not are words when they are written small")
ok(A.clean_query("  ") == "" and A.clean_query(None) == "" and len(A.clean_query("a " * 500)) <= 200, "nothing, and not endless")
u = urllib.parse.urlparse(A.search_url("sherlock holmes", "audiobook", 3))
q = urllib.parse.parse_qs(u.query)
ok(u.netloc == "archive.org" and u.path == "/advancedsearch.php", "the search's address")
ok(q["q"] == ["(sherlock holmes) AND collection:librivoxaudio AND NOT access-restricted-item:true"], f"the words, the shelf, and never what can only be borrowed: {q['q']}")
ok(q["page"] == ["3"] and q["rows"] == [str(A.ROWS)] and q["output"] == ["json"] and q["sort[]"] == ["downloads desc"], "which page, how many, most wanted first")
ok("collection" not in q["fl[]"] and "identifier" in q["fl[]"], "only the fields that are used (an item can be in hundreds of collections)")
ok(urllib.parse.parse_qs(urllib.parse.urlparse(A.search_url("", "film")).query)["q"] == ["mediatype:movies AND NOT access-restricted-item:true"],
   "no words: the shelf's most downloaded")
ok(A.file_url("some.item", "a b/c#1.mp3") == "https://archive.org/download/some.item/a%20b/c%231.mp3", "a file's address, with what must be escaped")

# ---- what is understood of the answer ---------------------------------------------------------
body = json.dumps({"response": {"numFound": 987, "docs": [
    {"identifier": "DuckandC1951", "title": "Duck and Cover", "creator": "Archer Productions, Inc.", "year": 1951, "item_size": 792066032, "downloads": 950988, "mediatype": "movies"},
    {"identifier": "two_creators", "title": ["A", "B"], "creator": ["X", "Y"], "downloads": 5},
    {"identifier": "bad id with spaces", "title": "no"},
    {"identifier": "untitled_1"}]}})
total, items = A.parse_search(body)
ok(total == 987 and [i["id"] for i in items] == ["DuckandC1951", "two_creators", "untitled_1"], "the items, without one whose name is not a name")
ok(items[0] == {"id": "DuckandC1951", "title": "Duck and Cover", "creator": "Archer Productions, Inc.", "year": "1951", "size": 792066032, "downloads": 950988}, f"one item: {items[0]}")
ok(items[1]["title"] == "A, B" and items[1]["creator"] == "X, Y" and items[2]["title"] == "untitled_1" and items[2]["size"] == 0, "lists are joined; no title: its name")
ok(A.parse_search("not json") == (0, []) and A.parse_search("{}") == (0, []), "an answer that is none")


# ---- which files are taken ----------------------------------------------------------------------
def F(name, size=1000, source="original", **more):
    return dict({"name": name, "size": str(size), "source": source}, **more)


def item(kind, files, **md):
    return {"metadata": dict({"identifier": "x", "title": "The Title", "mediatype": kind, "year": "1968"}, **md), "files": files}


def names(p):
    return [f["name"] for f in p["take"]]


p = A.plan(item("movies", [F("film.mpeg", 4_000_000_000), F("film.mp4", 900_000_000, "derivative", original="film.mpeg"),
                           F("film_512kb.mp4", 300_000_000, "derivative", original="film.mpeg"), F("film.ogv", 500_000_000, "derivative", original="film.mpeg"),
                           F("film.thumbs/film_000001.jpg", 4000, "derivative"), F("__ia_thumb.jpg", 20000), F("x_meta.xml", 900), F("x_files.xml", 900)]))
ok(p["shelf"] == "film" and names(p) == ["film.mp4"] and p["bytes"] == 900_000_000 and p["title"] == "The Title" and p["year"] == "1968",
   f"a film: the copy a browser plays, the better of the two - not the original it cannot play, not the six other things: {names(p)}")
p = A.plan(item("movies", [F("part1.mp4", 500), F("part2.mp4", 600), F("part1.ia.mp4", 100, "derivative", original="part1.mp4")]))
ok(names(p) == ["part1.mp4", "part2.mp4"], "the uploader's own playable files, all of them, in order")
p = A.plan(item("movies", [F("old.avi", 700_000_000)]))
ok(names(p) == ["old.avi"], "no copy a browser plays: the original (it is converted when it is watched)")
p = A.plan(item("audio", [F("book.m4b", 295_000_000), F("ch_02.mp3", 56_000_000, title="02 - The Red-Headed League"), F("ch_01.mp3", 62_000_000, title="01 - A Scandal in Bohemia"),
                          F("ch_01.ogg", 36_000_000, "derivative", original="ch_01.mp3"), F("ch_01_64kb.mp3", 31_000_000, "derivative", original="ch_01.mp3"),
                          F("ch_01_spectrogram.png", 250_000, "derivative"), F("cover.jpg", 120_000)], collection=["librivoxaudio", "audio_bookspoetry"]))
ok(p["shelf"] == "audiobook" and names(p) == ["ch_01.mp3", "ch_02.mp3"] and p["take"][0]["title"] == "01 - A Scandal in Bohemia",
   f"an audiobook: its chapters as they were uploaded, once: {names(p)}")
p = A.plan(item("audio", [F("a.flac", 30_000_000), F("a.mp3", 6_000_000, "derivative", original="a.flac"), F("a_64kb.mp3", 3_000_000, "derivative", original="a.flac")]))
ok(p["shelf"] == "audio" and names(p) == ["a.mp3"], "music kept as flac with an mp3 made of it: the mp3 plays everywhere")
p = A.plan(item("etree", [F("d1t01.flac", 30_000_000), F("d1t02.flac", 31_000_000)]))
ok(p["shelf"] == "audio" and names(p) == ["d1t01.flac", "d1t02.flac"], "a live recording that is only flac: the flac")
p = A.plan(item("texts", [F("book.pdf", 9_000_000), F("book.epub", 400_000, "derivative", original="book.pdf"), F("book_djvu.txt", 300_000, "derivative"),
                          F("book_jp2.zip", 80_000_000, "derivative"), F("book_abbyy.gz", 2_000_000, "derivative")]))
ok(p["shelf"] == "book" and names(p) == ["book.epub"], "a book: the epub when there is one")
p = A.plan(item("texts", [F("issue1.pdf", 9_000_000), F("issue2.pdf", 8_000_000), F("issue1_text.pdf", 1_000_000, "derivative", original="issue1.pdf")]))
ok(names(p) == ["issue1.pdf", "issue2.pdf"], "a run of magazines: every issue")
p = A.plan(item("texts", [F("comic.cbz", 40_000_000)]))
ok(names(p) == ["comic.cbz"], "a comic")
p = A.plan(item("texts", [F("19033-8.txt", 74735), F("19033-8.zip", 27958), F("19033-h/19033-h.htm", 92237), F("19033-h/images/cover.jpg", 102933)]))
ok(names(p) == ["19033-8.txt"], "a book typed in by hand, kept as plain text: the text")
p = A.plan(item("texts", [F("scan_djvu.txt", 300_000, "derivative"), F("scan_jp2.zip", 80_000_000, "derivative")]))
ok(p["take"] == [] and p["why"], "a scan of which only the machine-read text is open: not taken")
p = A.plan(item("image", [F("map.jpg", 5_000_000), F("map_thumb.jpg", 9000, "derivative"), F("map.tif", 90_000_000), F("__ia_thumb.jpg", 9000)]))
ok(p["shelf"] == "picture" and names(p) == ["map.jpg"], "a picture, not its thumbnail and not what a browser cannot show")
# what stands in the way
ok(A.plan(item("movies", [F("f.mp4")], **{"access-restricted-item": "true"}))["why"].startswith("this one can only be borrowed"), "the lending library")
ok(A.plan({"is_dark": True})["why"] and A.plan({})["why"] and A.plan(None)["why"], "an item that is gone")
ok(A.plan(item("software", [F("game.zip")]))["why"] == "not a film, a recording, a book or a picture", "software is not taken")
ok(A.plan(item("movies", [F("setup.exe"), F("readme.txt")]))["why"] == "nothing in it that can be played or read as it is", "no program, whatever shelf it sits on")
ok(A.plan(item("movies", [F("f.mp4", private="true")]))["take"] == [], "a file the Archive keeps to itself")
ok(A.plan(item("movies", [F("../../etc/passwd.mp4"), F("/abs.mp4")]))["take"] == [], "a name that reaches outside its folder")
ok(A.plan(item("movies", [F("huge.mp4", A.MOST_BYTES + 1)]))["why"] == "too large to fetch here", "over what one fetch may be")
p = A.plan(item("movies", [F("a.mp4", A.MOST_BYTES - 10), F("b.mp4", 100)]))
ok(names(p) == ["a.mp4"] and p["why"].startswith("the first 1 of 2 files"), "a series too large as a whole: what fits, and it says so")
ok(A.shelf_of({"mediatype": "audio", "collection": "librivoxaudio"}) == "audiobook" and A.shelf_of({"mediatype": "data"}) == "" and A.shelf_of({}) == "", "which shelf")
ok({s[0] for s in A.SHELVES} == {"film", "audiobook", "audio", "book", "picture"} and len({s[4] for s in A.SHELVES}) == 5, "five shelves, a folder each")

# ---- for Radarr ---------------------------------------------------------------------------------
ok(A.split_year("Night of the Living Dead 1968") == ("Night of the Living Dead", "1968") and A.split_year("2001") == ("2001", "")
   and A.split_year("Duck and Cover") == ("Duck and Cover", ""), "the year Radarr adds to what it asks")
ok(A.is_the_film("Night of the Living Dead", "1968", "night of the living dead", "1968"), "the film itself")
ok(A.is_the_film("Night Of The Living Dead (1968) [HD]", "1968", "Night of the Living Dead", "1968"), "with a year and a mark in its title")
ok(not A.is_the_film("Night of the Living Dead", "1990", "night of the living dead", "1968"), "the remake is another film")
ok(A.is_the_film("Night of the Living Dead", "", "night of the living dead", "1968"), "an item that names no year is not ruled out by one")
ok(not A.is_the_film("A lecture about why Night of the Living Dead changed horror films forever", "2015", "night of the living dead", ""), "a talk about the film is not the film")
ok(not A.is_the_film("Day of the Dead", "1985", "night of the living dead", "") and not A.is_the_film("Anything", "", "", ""), "another film; nothing asked")
ok(A.release_name("Night of the Living Dead", "1968", 1080) == "Night.of.the.Living.Dead.1968.1080p.WEB-archive.org", "a name Radarr reads")
ok(A.release_name("Duck & Cover!", "1951", 480) == "Duck.Cover.1951.480p.WEB-archive.org" and A.release_name("X", "", 0) == "X.SD.WEB-archive.org"
   and A.release_name("Y", "1999", 720).endswith(".720p.WEB-archive.org"), "quality from the picture's height; no height: SD")
p = A.plan(item("movies", [F("a.mp4", 500, height="480"), F("b.mp4", 900, height="1080")]))
ok(A.film_file(p)["name"] == "b.mp4" and A.film_file(p)["height"] == 1080, "of two cuts, the larger - and how tall its picture is")
ok(A.film_file(A.plan(item("audio", [F("a.mp3")]))) is None and A.film_file({}) is None, "what is not a film is not offered as one")

print(f"test_archive_org: {n} checks passed")
