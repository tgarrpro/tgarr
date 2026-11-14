"""usenet_rules.never_kept: what is never fetched and never kept, by every name a file has."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usenet_rules as R  # noqa: E402

n = 0


def check(what, got, wanted):
    global n
    n += 1
    assert got == wanted, f"{what}: {got!r}, wanted {wanted!r}"


K = R.never_kept
TERM, GROUP, YOUNG = "a term for it in its name", "posted to a group named for it", "adult material named for the young"
check("an ordinary book", K(["Dune.epub", "[1/1] Dune.epub yEnc (1/3)"], ["alt.binaries.e-book"]), "")
check("an ordinary adult set", K(["set 18/001.jpg", "Top Set 18"], ["alt.binaries.pictures.erotica"], True), "")
check("a term in the file's name", K(["pthc_001.jpg"], ["alt.binaries.pictures"]), TERM)
check("a term in the subject line only", K(["001.jpg", '[1/9] "001.jpg" lolicon set yEnc'], ["alt.binaries.pictures"]), TERM)
check("a term in the folder", K(["001.jpg", "usenet/pictures/preteen models/001.jpg"]), TERM)
check("a term in the name inside the post", K(["jailbait-3.jpg", "An innocent release"]), TERM)
check("the group it came from", K(["001.jpg"], ["alt.binaries.pictures.lolita"]), GROUP)
check("adult by its label, young by its name", K(["schoolgirl 04.jpg"], [], True), YOUNG)
check("adult by its group, young by its subject", K(["04.jpg", "12yo set"], ["alt.binaries.pictures.erotica.x"]), YOUNG)
check("adult by a word, young by an age", K(["xxx 15 years old.jpg"]), YOUNG)
check("young but not adult: a school photo", K(["schoolgirl uniform history.pdf"], ["alt.binaries.e-book"]), "")
check("children's books are books", K(["Stories for children.epub"], ["alt.binaries.e-book"]), "")
check("the same, found to be adult by looking at it", K(["Stories for children.jpg"], [], True), YOUNG)
check("a word that only contains one", K(["Lolita - Nabokov.epub", "a kidney.pdf", "shotgun.jpg"], ["alt.binaries.e-book"]), "")
check("no names at all", K([None, ""], [None]), "")
check("a term is a term whatever the group", K(["raygold.avi"], ["alt.binaries.e-book"], False), TERM)
print(f"test_usenet_guard: {n} checks passed")
