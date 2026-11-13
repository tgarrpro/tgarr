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

print(f"ok - {n} checks")
