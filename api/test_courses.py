"""courses.py: lists of courses, the order of lessons, and what a lesson done is worth."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import courses as C

n = 0


def ok(cond, what):
    global n
    n += 1
    assert cond, what


def bad(raw, word):
    try:
        C.clean_pack(raw)
    except ValueError as e:
        return word in str(e)
    return False


# ── the order of names ──
names = ["Lesson 10.mp3", "lesson 2.mp3", "Lesson 1.mp3", "Intro.mp3", "Lesson 2b.mp3"]
ok(sorted(names, key=C.natural_key) == ["Intro.mp3", "Lesson 1.mp3", "lesson 2.mp3", "Lesson 2b.mp3", "Lesson 10.mp3"], "2 comes before 10")
ok(sorted(["b/02.mp4", "a/10.mp4", "a/9.mp4"], key=C.natural_key) == ["a/9.mp4", "a/10.mp4", "b/02.mp4"], "folders first, then numbers")
ok(C.natural_key("") == [] and C.natural_key(None) == [], "nothing sorts as nothing")

# ── a list ──
good = {"slug": "mcguffey-readers", "title": " McGuffey's   Readers ", "about": "Six readers.", "lang": "en", "level": "5-8",
        "subject": "reading", "right": "OLD", "credit": "W. H. McGuffey", "parts": [{"ia": "mcguffeysfirstec00mcgu", "title": "First Reader"},
                                                                                  {"ia": "mcguffeyssecond00mcgu"}]}
p = C.clean_pack(good)
ok(p["title"] == "McGuffey's Readers" and len(p["parts"]) == 2 and p["parts"][1]["title"] == "", "a list in order is taken, tidied")
ok(bad(dict(good, slug="Bad Slug"), "slug") and bad(dict(good, title=""), "title"), "a slug and a title are needed")
ok(bad(dict(good, right=""), "right") and bad(dict(good, right="mine"), "right"), "a list names the right its material is under")
ok(bad(dict(good, level="toddlers"), "level") and bad(dict(good, subject="gossip"), "subject"), "levels and subjects are from the known ones")
ok(bad(dict(good, parts=[]), "no parts") and bad(dict(good, parts=[{"ia": "../etc/passwd"}]), "identifier"), "parts are Archive identifiers")
ok(bad(dict(good, parts=[{"ia": "a1"}, {"ia": "a1"}]), "twice") and bad(dict(good, parts=["x"]), "object"), "each once, each an object")
ok(bad(dict(good, parts=[{"ia": f"i{i}"} for i in range(C.PARTS_MOST + 1)]), "more than"), "not endless")
ok(bad([], "object") and bad(None, "object"), "a list is an object")
d = tempfile.mkdtemp()
json.dump(good, open(os.path.join(d, "a.json"), "w"))
json.dump(dict(good, title="Another"), open(os.path.join(d, "b.json"), "w"))       # the same slug again
json.dump(dict(good, slug="x-ok", right="nope"), open(os.path.join(d, "c.json"), "w"))
open(os.path.join(d, "d.json"), "w").write("{not json")
json.dump(dict(good, slug="fsi-german"), open(os.path.join(d, "e.json"), "w"))
said = []
packs = C.load_packs(d, said.append)
ok(sorted(packs) == ["fsi-german", "mcguffey-readers"] and packs["mcguffey-readers"]["title"] == "McGuffey's Readers", "the lists in order are loaded")
ok(len(said) == 3 and all("left out" in s for s in said), f"the others are left out, each with its reason: {said}")
ok(C.load_packs(os.path.join(d, "nowhere")) == {}, "no folder, no lists")

# ── lessons ──
ok(C.ia_of("First Reader · archive.org/details/mcguffeysfirstec00mcgu · 1879") == "mcguffeysfirstec00mcgu", "the item a file came from")
ok(C.ia_of("a film") == "" and C.ia_of(None) == "", "or none")
files = [(5, "b", "02 two.mp3"), (4, "b", "10 ten.mp3"), (9, "a", "x.pdf"), (6, "b", "1 one.mp3"), (7, "zz", "other.mp3")]
ok(C.order_lessons([{"ia": "a"}, {"ia": "b"}, {"ia": "c"}], files) == [9, 6, 5, 4], "parts in the list's order, files in the order of their names")
ok(C.order_lessons([], files) == [], "no parts, no lessons")

# ── rewards ──
r = C.clean_reward({"on": True, "per_lesson": 20, "most": 60, "share": 70, "book_minutes": 20})
ok(C.clean_reward(None) == C.REWARD_DEFAULT and C.clean_reward({"per_lesson": "x", "share": 5})["share"] == 10, "a rule as typed in is made safe")
ok(C.clean_reward({"learn_free": False})["learn_free"] is False and C.clean_reward({})["learn_free"] is True, "learning is free of the day unless told otherwise")
ok(C.lesson_done(r, 600, 800, True), "watched most of it, to the end: done")
ok(not C.lesson_done(r, 60, 800, True), "dragged to the end after a minute: not done")
ok(not C.lesson_done(r, 790, 800, False), "all the time but never the end: not done")
ok(not C.lesson_done(r, 9999, 0, True) and not C.lesson_done(r, 9999, None, True), "a thing without a length is never done this way")
ok(C.credits(r, 2, 0) == 2 and C.credits(r, 0, 45 * 60) == 2 and C.credits(r, 1, 19 * 60) == 1, "lessons done, and one for every twenty minutes of reading")
ok(C.bonus_seconds(r, 2, 0) == 40 * 60, "two lessons: forty minutes")
ok(C.bonus_seconds(r, 9, 0) == 60 * 60, "never more than the day's most")
ok(C.bonus_seconds(dict(r, on=False), 9, 9999) == 0, "nothing when rewards are off")
print(f"test_courses: {n} checks passed")
