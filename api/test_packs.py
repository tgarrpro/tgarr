"""Checks for what is fetched on demand: which file for which machine, what may be unpacked,
where things go. Nothing is fetched. Run: python test_packs.py"""
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


# ---- which build for which machine ------------------------------------------------------------
ok(P.machine("x86_64") == "x86_64" and P.machine("AMD64") == "x86_64", "a PC, however it calls itself")
ok(P.machine("aarch64") == "arm64" and P.machine("arm64") == "arm64", "an ARM machine: a Raspberry Pi 4, a Mac's Linux")
ok(P.machine("armv7l") == "" and P.machine("riscv64") == "" and P.machine("i686") == "", "a machine calibre has no build for")
ok(set(P.CALIBRE_FILES) == {"x86_64", "arm64"}, "one file for each kind of machine there is a build for")
ok(all(size > 100_000_000 and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha) for size, sha in P.CALIBRE_FILES.values()),
   "each with its size and its SHA-256")
ok(P.calibre_url("arm64") == f"https://github.com/kovidgoyal/calibre/releases/download/v{P.CALIBRE}/calibre-{P.CALIBRE}-arm64.txz",
   "from calibre's own release page, by version")
ok(P.size("books") in (0, P.CALIBRE_FILES["x86_64"][0], P.CALIBRE_FILES["arm64"][0]) and P.size("runtime") > 0, "how much is to be fetched")

# ---- what may be unpacked ---------------------------------------------------------------------------
for name in ("ebook-convert", "./ebook-convert", "lib/libQt6Core.so.6", "resources/images/x.png", "bin/"):
    ok(P.safe_member(name), f"{name}: inside")
for name in ("/etc/passwd", "../x", "lib/../../x", "a/../../../etc/cron.d/x", "", "..", "x\\..\\..\\y"):
    ok(not P.safe_member(name), f"{name!r}: not inside")
ok(P.safe_member("bin/calibre", True, "../lib/calibre") and P.safe_member("lib/a.so", True, "a.so.6") and P.safe_member("a/b/c", True, "../../d"),
   "a link to something else inside")
ok(not P.safe_member("bin/x", True, "/bin/sh") and not P.safe_member("bin/x", True, "../../etc/passwd") and not P.safe_member("x", True, "../y"),
   "a link that leads out")
ok(P.safe_member("a/b", True, "./c/../d"), "dots that stay inside")

# ---- where things go, and what is said before anything is there --------------------------------------
ok(P.books_dir().startswith(P.ROOT) and P.runtime_dir().startswith(P.ROOT) and P.books_dir() != P.runtime_dir(), "each in a folder of its own under the packs' folder")
ok(f"py{sys.version_info[0]}{sys.version_info[1]}" in P.runtime_dir(), "the runtime is kept for this Python: another Python gets another")
for name in P.NAMES:
    s = P.status(name)
    ok(s["installed"] is False and s["installing"] is False and s["done"] == 0 and s["error"] == "", f"{name}: not here, not on its way")
ok(P.program("ebook-convert") is None and P.program("ebook-meta") is None, "no converter: no program to run")
ok(P.program("sh") is None and P.program("../../bin/sh") is None, "and never a program that is not calibre's")
ok(P.activate() is False and P.runtime_dir() not in sys.path, "no runtime: nothing is put on Python's path")
ok(P.install("nonsense") is False and P.status("books")["installing"] is False, "a pack that does not exist is not fetched")

# ---- when it is there -----------------------------------------------------------------------------------
os.makedirs(P.books_dir())
open(os.path.join(P.books_dir(), "ebook-convert"), "w").close()
ok(P.program("ebook-convert") is None, "a folder without its mark is not an installed converter (it may be half unpacked)")
open(os.path.join(P.books_dir(), ".ready"), "w").close()
ok(P.program("ebook-convert") == os.path.join(P.books_dir(), "ebook-convert") and P.program("ebook-meta") is None, "with its mark: the program that is there, not one that is not")
ok(P.install("books") is False, "what is installed is not fetched again")
os.makedirs(P.runtime_dir())
open(os.path.join(P.runtime_dir(), ".ready"), "w").close()
ok(P.activate() is True and sys.path[-1] == P.runtime_dir(), "the runtime goes to the END of Python's path: nothing of the image is shadowed")
ok(P.activate() is True and sys.path.count(P.runtime_dir()) == 1, "once")
P.remove("runtime")
ok(not P.installed("runtime") and P.runtime_dir() not in sys.path, "removed: gone from the disk and from the path")
P.remove("books")
ok(not P.installed("books") and P.program("ebook-convert") is None, "the converter too")
ok(all(not p.startswith("-") and " " not in p for p in P.RUNTIME) and len(P.RUNTIME) == 4, "what pip is asked for: four packages, nothing that could be read as an option")

print(f"test_packs: {n} checks passed")
