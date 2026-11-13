"""Checks for the Usenet indexing prototype. No network. Run: python test_usenet.py"""
import xml.dom.minidom

import usenet as U

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


# ---- subjects, in the shapes of different years ---------------------------
p = U.parse_subject('[12/40] - "Some.Show.S02E05.1080p.WEB.h264-GRP.part03.rar" yEnc (17/66)')
ok(p and p["file"].endswith("part03.rar") and (p["part"], p["total"], p["file_no"], p["files"]) == (17, 66, 12, 40),
   "the common yEnc shape")
p = U.parse_subject('Miles Davis At Newport 1955-1975 [03/17] yEnc "01-beginning.flac" (1/20)')
ok(p and p["title"] == "Miles Davis At Newport 1955-1975" and p["file"] == "01-beginning.flac" and p["files"] == 17,
   "yEnc before the file name; the words in front are the title")
p = U.parse_subject('Live_Feed_UNRATED_(2006)_DVDRip_XviD - File 60 of 61 - yEnc "lf-xvid.r58" (1/40)')
ok(p and (p["file_no"], p["files"]) == (60, 61) and p["title"].startswith("Live_Feed_UNRATED"), '"File n of m"')
p = U.parse_subject("CD Ripper 2002 2.0.rar (03/26)")
ok(p and p["file"] == "2.0.rar" and p["part"] == 3 and p["total"] == 26, "uuencode era: no quotes, no yEnc")
p = U.parse_subject('"Show.S01E01.mkv" yEnc 734003200 (1/1024)')
ok(p and p["file"] == "Show.S01E01.mkv" and p["total"] == 1024, "a size between yEnc and the part")
p = U.parse_subject('[ 15930 ]-[FULL]-[#a.b.teevee@EFNet]-[ Law.and.Order.S18E01.HDTV.XviD-LOL ] "law.and.order.1801.hdtv-lol.r22" yEnc (37/40)')
ok(p and "Law.and.Order.S18E01" in p["title"] and p["file"].endswith(".r22"), "the scene shape of 2008-2012")
ok(U.parse_subject("KABdQ1ExZVNi9wrAsyO85jDqrYs3nPUa") is None, "a subject with no part number is not a segment")
ok(U.parse_subject("Re: looking for that film (5/3)") is None, "part 5 of 3 is not a segment")
ok(U.parse_subject("") is None and U.parse_subject(None) is None, "nothing to parse")

# ---- volume names and random names ----------------------------------------
ok(U.base_name("Some.Show.S02E05.part03.rar") == "Some.Show.S02E05" == U.base_name("Some.Show.S02E05.vol03+04.par2"),
   "volumes and repair files of one release share a base name")
ok(U.base_name("law.and.order.1801.hdtv-lol.r22") == "law.and.order.1801.hdtv-lol", "old .rNN volumes")
for r in ("a8f3c9d2e7b14f60a8f3c9d2e7b14f60", "kJ2nQ8xLp0ZrT5vB7hN", "H21dAHOtqdMvMWROOF4CaWp1", "", "ab"):
    ok(U.is_random(r), f"random: {r!r}")
for r in ("Some.Show.S02E05.1080p.WEB.h264-GRP", "Miles Davis At Newport 1955-1975", "Sledge.Hammer.1986.Complete",
          "Suffocation-Breeding_the_spawn"):
    ok(not U.is_random(r), f"a name: {r!r}")

# ---- what kind of thing ------------------------------------------------------
ok(U.classify("Some.Show.S02E05.1080p.WEB.h264-GRP", ["x.part01.rar", "x.part02.rar"]) == "video", "video by its name")
ok(U.classify("Miles Davis At Newport", ["01-a.flac", "02-b.flac", "cover.jpg"]) == "audio", "audio by its files; a cover does not make it photos")
ok(U.classify("Springer Nano-Antimicrobials 2012 Retail Ebook", ["book.rar"]) == "ebook", "e-book by its name")
ok(U.classify("holiday", ["img001.jpg", "img002.jpg", "img003.jpg"]) == "photo", "photos")
ok(U.classify("Microsoft Office 2024 Pro Plus x64 incl. activator", ["o.part1.rar"]) == "software", "software")
ok(U.classify("Sleeping beauty NL", ["sb.part01.rar"]) == "unknown", "nothing says what it is")
ok("software" not in U.WANTED and "unknown" not in U.WANTED, "only the four kinds of media are indexed")

# ---- articles into releases into cards --------------------------------------
A = U.Assembler("alt.binaries.test")
poster = "someone@example.invalid (someone)"
num = 1000
for fno, fname, parts in ((1, "Some.Show.S02E05.720p.HDTV.x264-GRP.part1.rar", 3),
                          (2, "Some.Show.S02E05.720p.HDTV.x264-GRP.part2.rar", 3),
                          (3, "Some.Show.S02E05.720p.HDTV.x264-GRP.par2", 1)):
    for part in range(1, parts + 1):
        num += 1
        A.add(num, f'[{fno}/3] - "{fname}" yEnc ({part}/{parts})', poster,
              "Tue, 01 Mar 2011 10:00:%02d +0000" % part, f"<m{num}@x>", 500000)
# another poster's random upload and a piece of software, in the same stretch
for part in (1, 2):
    A.add(2000 + part, f'[1/1] - "a8f3c9d2e7b14f60a8f3c9d2.part1.rar" yEnc ({part}/2)', "other@x", "Tue, 01 Mar 2011 10:01:00 +0000", f"<r{part}@x>", 500000)
    A.add(3000 + part, f'[1/1] - "Adobe.Photoshop.x64.incl.keygen.rar" yEnc ({part}/2)', "third@x", "Tue, 01 Mar 2011 10:02:00 +0000", f"<s{part}@x>", 500000)
A.add(4000, "just a text post", "x@x", "Tue, 01 Mar 2011 10:03:00 +0000", "<t@x>", 900)
rels = A.releases()
ok(len(rels) == 3, f"three uploads told apart ({len(rels)})")
cards = [c for c in (U.card(r) for r in rels) if c]
ok(len(cards) == 1, "one card: the random upload and the software are not indexed")
c = cards[0]
ok(c["name"].startswith("Some.Show.S02E05") and c["kind"] == "video", "named and classified")
ok(c["size"] == 7 * 500000 and c["segments"] == 7 == c["segments_expected"] and c["complete"], "sizes and completeness")
ok(c["files"] == 2 and c["files_expected"] == 3, "two payload files (the repair file is not counted), three announced")
ok(c["posted_from"] and c["posted_to"] >= c["posted_from"] and c["first_message_id"] == "<m1001@x>", "when, and where it starts")
ok(len(c["poster"]) == 16 and "example" not in c["poster"], "the poster is kept as a hash only")
ok(len(str(c)) < 700, "a card is small")
# a release missing a segment is not complete
B = U.Assembler("g")
B.add(1, '"Film.2010.DVDRip.XviD.avi" yEnc (1/3)', poster, "Tue, 01 Mar 2011 10:00:00 +0000", "<a@x>", 100)
B.add(3, '"Film.2010.DVDRip.XviD.avi" yEnc (3/3)', poster, "Tue, 01 Mar 2011 10:00:02 +0000", "<c@x>", 100)
ok(U.card(B.releases()[0])["complete"] is False, "a missing segment shows")
# different track names under one title are one release
D = U.Assembler("g")
for i, t in enumerate(("01-beginning.flac", "02-middle.flac", "03-end.flac"), 1):
    D.add(i, f'Miles Davis At Newport 1955-1975 [{i}/3] yEnc "{t}" (1/1)', poster, "Tue, 01 Mar 2011 10:00:00 +0000", f"<d{i}@x>", 100)
ok(len(D.releases()) == 1 and U.card(D.releases()[0])["kind"] == "audio", "an album is one release")

# ---- the NZB is built from the headers, not stored --------------------------
nzb = U.build_nzb([r for r in rels if r["title"] == "" and "some.show" in r["key"]][0])
doc = xml.dom.minidom.parseString(nzb)
ok(len(doc.getElementsByTagName("file")) == 3 and len(doc.getElementsByTagName("segment")) == 7, "every file and segment listed")
seg = [s for s in doc.getElementsByTagName("segment") if s.firstChild.data == "m1001@x"]
ok(len(seg) == 1 and seg[0].getAttribute("number") == "1" and seg[0].getAttribute("bytes") == "500000",
   "segments carry the article id without brackets, their number and size")
ok(doc.getElementsByTagName("group")[0].firstChild.data == "alt.binaries.test", "the group is named")
ok("&" not in U.build_nzb({**rels[0], "files": {'a&b "x".rar': {**list(rels[0]["files"].values())[0], "subject": 'a&b "x"'}}}).replace("&amp;", "").replace("&quot;", ""),
   "odd characters are escaped")

# ---- headers ----------------------------------------------------------------
ok(U.decode(b"=?UTF-8?B?5Lit5paH?= title") == "中文 title" or "中文" in U.decode(b"=?UTF-8?B?5Lit5paH?= title"), "MIME words")
ok(U.decode(b"caf\xe9") == "café", "Latin-1 when it is not UTF-8")
ok(U.when("Tue, 01 Mar 2011 10:00:00 +0000") == 1298973600 and U.when("nonsense") is None, "dates")

print(f"ok - {n} checks")
