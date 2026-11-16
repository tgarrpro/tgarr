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
ok(p["channel"] == "" and p["tag"] == "", "a list of Archive items names no channel")
ch = C.clean_pack({"slug": "zh-pinyin", "title": "拼音入门", "lang": "zh-Hans", "level": "5-8", "subject": "reading", "right": "CC BY-SA",
                   "channel": "@tgarr_courses", "tag": "#拼音", "upto": 120})
ok(ch["channel"] == "tgarr_courses" and ch["tag"] == "#拼音" and ch["parts"] == [] and ch["upto"] == 120, "a list may be a public channel, or the posts of it under one tag")
chan = {"slug": "x-y", "title": "t", "right": "CC BY", "channel": "tgarr_courses", "upto": 50}
ok(bad({k: v for k, v in chan.items() if k != "upto"}, "upto") and bad(dict(chan, upto=0), "upto") and bad(dict(chan, upto="9"), "upto") and bad(dict(chan, upto=True), "upto"),
   "a channel's list is taken up to the last post that was read: without that number it is not loaded")
ok(C.clean_pack(chan)["tag"] == "", "the tag is optional: then it is all of the channel")
ok(bad(dict(chan, channel="ab"), "channel") and bad(dict(chan, channel="has space"), "channel"), "the channel is an @name")
ok(bad(dict(chan, tag="two words"), "tag") and bad(dict(chan, tag="notag"), "tag") and bad(dict(chan, tag="#a#b"), "tag"), "the tag is one #tag")
ok(C.clean_pack(dict(chan, tag="#zh_pinyin1"))["tag"] == "#zh_pinyin1", "a tag may have an underscore: language first, as the guide says")
ok(bad(dict(chan, parts=[{"ia": "abc"}]), "not both"), "parts or a channel, not both")
ok(bad(dict(chan, right=""), "right"), "a channel's list names its right as well")
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

# ── what the registry is told ──
shipped = {"mcguffey-readers": p}
rows = [
    {"kind": "pack", "slug": "mcguffey-readers", "title": "McGuffey's Readers", "lang": "en", "level": "5-8", "subject": "reading", "license": "OLD",
     "parts": json.dumps(p["parts"]), "lessons": 6, "done": [1, 6, 3]},
    {"kind": "pack", "slug": None, "title": "An imported list", "parts": json.dumps([{"ia": "abc_item"}, {"ia": "def_item"}]), "lessons": 9, "done": []},
    {"kind": "pack", "slug": "mcguffey-readers", "title": "changed by hand", "parts": json.dumps([{"ia": "only_one"}]), "lessons": 1, "done": [1]},
    {"kind": "channel", "slug": None, "title": "Anna's piano lessons", "ref": "@a_private_thing", "parts": None, "lessons": 30, "done": [4]},
    {"kind": "folder", "slug": None, "title": "Home videos of Ben", "ref": "1:family/ben", "parts": None, "lessons": 12, "done": [2]},
    {"kind": "list", "slug": None, "title": "School", "parts": json.dumps([{"name": "Ben - maths test.pdf"}]), "lessons": 1, "done": [1]},
    {"kind": "pack", "slug": None, "title": "broken", "parts": "{not json", "lessons": 1, "done": []},
]
rep = C.report(rows, shipped)
ok(len(rep) == 3, "of seven courses the three made of Archive items are told of")
ok(rep[0] == {"title": "McGuffey's Readers", "lang": "en", "level": "5-8", "subject": "reading", "right": "OLD", "lessons": 6,
              "learners": 3, "done_median": 3, "slug": "mcguffey-readers"}, "a shipped list by its name, with how many began it and the middle of what they did")
ok("parts" not in rep[0] and rep[1]["parts"] == ["abc_item", "def_item"] and "slug" not in rep[1], "another by the items it names")
ok(rep[2].get("parts") == ["only_one"] and "slug" not in rep[2], "a shipped name over other items is not passed off as the shipped list")
told = json.dumps(rep)
ok(not any(w in told for w in ("Anna", "Ben", "piano", "a_private_thing", "family", "maths")), "nothing of a channel's, a folder's or a file list's course is in it")
ok(C.report([], shipped) == [] and C.report([{"kind": "pack", "parts": "[]"}], shipped) == [], "no courses, nothing told")

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
# ── the lists shipped with tgarr are all in order, and the template is ──
base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coursepacks")
tpl = json.load(open(os.path.join(base, "template.json.example"), encoding="utf-8"))
ok(C.clean_pack(tpl)["channel"] == "your_public_channel", "the template is a list in order")
slugs = {}
for lang in sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))):
    said = []
    got = C.load_packs(os.path.join(base, lang), said.append)
    ok(not said, f"coursepacks/{lang}: every list is in order: {said}")
    for slug, pk in got.items():
        ok(slug not in slugs, f"{slug}: a slug used by two lists ({lang} and {slugs.get(slug)})")
        slugs[slug] = lang
        ok(pk["lang"].split("-")[0] == lang.split("-")[0], f"{slug}: its language ({pk['lang']}) is its folder's ({lang})")
print(f"test_courses: {n} checks passed ({len(slugs)} lists shipped)")
