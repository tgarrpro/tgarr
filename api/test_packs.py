"""Checks for what is fetched on demand: where it goes, what is said before anything is there,
and that the calibre an older version fetched is cleared away. Nothing is fetched.
Run: python test_packs.py"""
import os
import sys
import tempfile

os.environ["TGARR_PACKS_DIR"] = tempfile.mkdtemp(prefix="tgarr-packs-test-")
import packs as P  # noqa: E402

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


# ---- which machine ------------------------------------------------------------------------------------
ok(P.machine("x86_64") == "x86_64" and P.machine("AMD64") == "x86_64", "a PC, however it calls itself")
ok(P.machine("aarch64") == "arm64" and P.machine("arm64") == "arm64", "an ARM machine: a Raspberry Pi 4, a Mac's Linux")
ok(P.machine("armv7l") == "" and P.machine("riscv64") == "", "another kind of machine")

# ---- where things go, and what is said before anything is there --------------------------------------
ok(P.NAMES == ("runtime",), "one thing is fetched on demand: what the models run on. No book converter any more")
ok(P.runtime_dir().startswith(P.ROOT), "in a folder under the packs' folder")
ok(f"py{sys.version_info[0]}{sys.version_info[1]}" in P.runtime_dir(), "kept for this Python: another Python gets another")
s = P.status("runtime")
ok(s["installed"] is False and s["installing"] is False and s["done"] == 0 and s["error"] == "" and s["bytes"] > 0, "not here, not on its way")
ok(P.activate() is False and P.runtime_dir() not in sys.path, "no runtime: nothing is put on Python's path")
ok(P.install("nonsense") is False and P.install("books") is False and not P.installed("books"), "a pack that does not exist is not fetched")
ok(not hasattr(P, "program"), "there is no program of calibre's to ask for")

# ---- when it is there -----------------------------------------------------------------------------------
os.makedirs(P.runtime_dir())
ok(not P.installed("runtime"), "a folder without its mark is not installed (it may be half made)")
open(os.path.join(P.runtime_dir(), ".ready"), "w").close()
ok(P.install("runtime") is False, "what is installed is not fetched again")
ok(P.activate() is True and sys.path[-1] == P.runtime_dir(), "the runtime goes to the END of Python's path: nothing of the image is shadowed")
ok(P.activate() is True and sys.path.count(P.runtime_dir()) == 1, "once")

# ---- the calibre of a version before this one ---------------------------------------------------------
ok(P.forget_books() == 0, "nothing of calibre here: nothing to delete")
old = os.path.join(P.ROOT, "calibre")
os.makedirs(os.path.join(old, "lib"))
open(os.path.join(old, "ebook-convert"), "wb").write(b"x" * 1000)
open(os.path.join(old, "lib", "libQt6Core.so.6"), "wb").write(b"y" * 5000)
open(os.path.join(P.ROOT, "calibre.txz.part"), "wb").write(b"z" * 300)
ok(P.forget_books() == 6300, "what an older version fetched is deleted, and how much that was is told")
ok(not os.path.exists(old) and not os.path.exists(os.path.join(P.ROOT, "calibre.txz.part")), "the folder and the half-fetched archive are gone")
ok(P.installed("runtime") and os.path.exists(P.runtime_dir()), "and the runtime beside it is left alone")
ok(P.forget_books() == 0, "a second time there is nothing left")

P.remove("runtime")
ok(not P.installed("runtime") and P.runtime_dir() not in sys.path, "removed: gone from the disk and from the path")
ok(all(not p.startswith("-") and " " not in p for p in P.RUNTIME) and len(P.RUNTIME) == 4, "what pip is asked for: four packages, nothing that could be read as an option")

print(f"test_packs: {n} checks passed")
