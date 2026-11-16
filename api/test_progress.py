"""progress.py: what a browser may store as its place, and what a page is handed back."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import progress as P

n = 0


def ok(cond, what):
    global n
    n += 1
    assert cond, what


# ── what is kept ──
ok(P.clean({"key": "tgarr.v.", "val": "125", "frac": 0.25}) == {"key": "tgarr.v.", "val": "125", "frac": 0.25, "done": False, "total": None}, "seconds into a film")
ok(P.clean({"key": "tgarr.v.", "val": "125", "total": 500})["total"] == 500.0, "and how long the film is")
for t in (0, -5, "x", True, float("inf"), 9e9):
    ok(P.clean({"key": "tgarr.v.", "val": "1", "total": t})["total"] is None, f"a length that is none: {t}")
ok(P.clean({"key": "tgarr.v.", "val": 125})["val"] == "125", "a number is kept as text")
ok(P.clean({"key": "tgarr.read.", "val": '{"ch":3,"frac":0.5}', "frac": None})["frac"] is None, "no fraction is fine")
ok(P.clean({"key": "comic-", "val": "7", "frac": 2})["frac"] == 1.0, "a fraction is held within 0..1")
ok(P.clean({"key": "comic-", "val": "7", "frac": -3})["frac"] == 0.0, "from below as well")
ok(P.clean({"key": "tgarr.a.", "val": None, "done": True}) == {"key": "tgarr.a.", "val": None, "frac": 1.0, "done": True, "total": None}, "finished: no place, all of it")
ok(P.clean({"key": "tgarr.v.", "val": "5", "done": "yes"})["done"] is False, "only a real true means finished")
ok(P.clean({"key": "tgarr.v.", "val": "5", "frac": float("nan")})["frac"] is None, "not-a-number is no fraction")
ok(P.clean({"key": "tgarr.v.", "val": "5", "frac": True})["frac"] is None, "true is not a fraction")
# ── what is refused ──
for bad in (None, [], "x", {}, {"key": "tgarr.v."}, {"key": "tgarr.v.", "val": None},
            {"key": "tgarr.video", "val": "1"}, {"key": "tgarr.v.9", "val": "1"}, {"key": "x' OR 1=1", "val": "1"},
            {"key": "tgarr.v.", "val": "x" * (P.VAL_MOST + 1)}, {"key": "tgarr.v.", "val": {"a": 1}}, {"key": 5, "val": "1"}):
    ok(P.clean(bad) is None, f"refused: {str(bad)[:40]}")
for k in P.KEYS:
    ok(P.clean({"key": k, "val": "1"}) is not None, f"every key of a player is known: {k}")

# ── what a page is handed ──
ok(P.seed(None) == "null" and P.seed({"key": "tgarr.v.", "val": None, "at": 1}) == "null", "nothing stored, nothing handed")
s = json.loads(P.seed({"key": "tgarr.v.", "val": "125", "at": 1700000000000.0}))
ok(s == {"keys": ["tgarr.v.", "tgarr.play."], "val": "125", "at": 1700000000000}, "a film's place serves both of its players")
ok(json.loads(P.seed({"key": "tgarr.play.", "val": "9", "at": 5}))["keys"] == ["tgarr.play.", "tgarr.v."], "either way round")
ok(json.loads(P.seed({"key": "tgarr.read.", "val": '{"ch":1}', "at": 5}))["keys"] == ["tgarr.read."], "a book's place is the reader's alone")
evil = P.seed({"key": "comic-", "val": "</script><script>alert(1)</script>", "at": 5})
ok("</script" not in evil and json.loads(evil)["val"].startswith("</script>"), "a stored value cannot close the script it is written into")
page = P.script(42, {"key": "tgarr.a.", "val": "30", "at": 7})
ok("var ID = 42," in page and '"val": "30"' in page and "__ID__" not in page and "__SEED__" not in page, "the script is filled in")
ok(P.script(42, None).count("SEED = null") == 1, "and says null when there is nothing")
ok(P.script(42, None).count("NAV = null") == 1 and "__NAV__" not in page, "a file that is no lesson has no course to show")
lesson = P.script(42, None, {"n": 3, "total": 20, "prev": 41, "next": 43, "back": "/mediacenter?kind=learn&course=7",
                             "title": "A </script> course", "word_next": "Next", "word_done": "done"})
ok('"next": 43' in lesson and "</script> course" not in lesson and lesson.count("</script>") == 1, "a lesson is told its place; a course's title cannot close the script")
ok(page.strip().startswith("<script>") and page.strip().endswith("</script>") and page.count("<script>") == 1, "one script, closed")
# the keys the script watches are the keys clean() accepts
rx = re.search(r"var RX = /\^\((.+?)\)\(\\d\+\)\$/;", P.JS).group(1)
ok(rx == r"tgarr\.(?:v|play|read|pdf|a)\.|comic-" and P._KEY_RX.pattern == "^(" + rx + ")$", "the page and the server know the same keys")
print(f"test_progress: {n} checks passed")
