"""What is fetched when it is first wanted, and not carried in the image.

tgarr's image used to weigh 2.3 GB, and of that tgarr itself was one megabyte. The rest was
there for two things most households never touch: turning a .mobi or a .docx into a PDF
(calibre, with the browser engine it prints through - 1.5 GB), and the libraries the voice and
the picture model run on (230 MB), whose models were already fetched on demand. Both are now
fetched like the models: once, when somebody asks, into the downloads folder, where they
outlive an update of the image.

    books     calibre's own standalone build, from its GitHub releases: a pinned version, a
              pinned SHA-256 for each kind of machine. Unpacked beside the downloads.
    runtime   numpy, onnxruntime, sherpa-onnx, jieba - installed with pip into a folder that
              is put on Python's path.

    status(name)            {installed, installing, done, bytes, error}
    install(name)           start fetching it (returns at once); True if it was started
    ensure(name)            fetch it now, in this thread, if it is not there; raises if it cannot be
    remove(name)
    program(name)           the path of one of calibre's programs, or None
    activate()              put the runtime's folder on Python's path (called when this is imported)

The pure parts - which file for which machine, is an archive member safe to unpack - are
checked by test_packs.py.
"""
import hashlib
import importlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request

log = logging.getLogger("tgarr.packs")

ROOT = os.environ.get("TGARR_PACKS_DIR", "/downloads/.packs")

CALIBRE = "9.18.0"
# (the file's size in bytes, its SHA-256) as fetched from calibre's release page on 2026-11-15
CALIBRE_FILES = {
    "x86_64": (240948148, "da638166fb3dd204be77cf0f871a40d803536b3e5948969c5233b8e4f1e1dbe6"),
    "arm64": (227149592, "7f29f1786dcfc5b6f3b6d29b245998a6701501169fbd4c853f17c35d3a635e91"),
}
CALIBRE_UNPACKED = 910 * 1024 * 1024               # what it takes on the disk, about
RUNTIME = ("numpy>=1.26,<3", "sherpa-onnx>=1.12.40,<1.13", "onnxruntime>=1.19,<2", "jieba==0.42.1")
RUNTIME_BYTES = 230 * 1024 * 1024                  # about; pip does not say beforehand

NAMES = ("books", "runtime")
_state = {n: {"doing": False, "done": 0, "error": ""} for n in NAMES}
_locks = {n: threading.Lock() for n in NAMES}


def machine(m: str = "") -> str:
    """This machine as calibre names it: 'x86_64', 'arm64', or '' for one it has no build for."""
    m = (m or platform.machine() or "").lower()
    return {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(m, "")


def calibre_url(arch: str, version: str = CALIBRE) -> str:
    return f"https://github.com/kovidgoyal/calibre/releases/download/v{version}/calibre-{version}-{arch}.txz"


def books_dir() -> str:
    return os.path.join(ROOT, "calibre")


def runtime_dir() -> str:
    # (one for each Python and machine: what pip installs is built for exactly that)
    return os.path.join(ROOT, f"py{sys.version_info[0]}{sys.version_info[1]}-{machine() or 'any'}")


def _ready(name: str) -> str:
    return os.path.join(books_dir() if name == "books" else runtime_dir(), ".ready")


def installed(name: str) -> bool:
    return os.path.exists(_ready(name))


def size(name: str) -> int:
    if name == "books":
        return CALIBRE_FILES.get(machine(), (0, ""))[0]
    return RUNTIME_BYTES


def status(name: str) -> dict:
    s = _state[name]
    return {"installed": installed(name), "installing": s["doing"], "done": s["done"], "bytes": size(name), "error": s["error"],
            "possible": bool(machine()) if name == "books" else True}


def program(prog: str):
    """calibre's 'ebook-convert' or 'ebook-meta', if the books pack is there."""
    if prog not in ("ebook-convert", "ebook-meta") or not installed("books"):
        return None
    p = os.path.join(books_dir(), prog)
    return p if os.path.exists(p) else None


def activate() -> bool:
    """The runtime's folder on Python's path - behind what the image itself brings, so that
    nothing of the image is ever shadowed by it."""
    d = runtime_dir()
    if not installed("runtime"):
        return False
    if d not in sys.path:
        sys.path.append(d)
        importlib.invalidate_caches()
    return True


def safe_member(name: str, is_link: bool = False, link_to: str = "") -> bool:
    """May this member of an archive be unpacked under the pack's folder? Not one that names
    a place outside it - by an absolute path, by '..', or by a link that leads out."""
    n = (name or "").replace("\\", "/")
    if not n or n.startswith("/") or ".." in n.split("/"):
        return False
    if is_link:
        t = (link_to or "").replace("\\", "/")
        if t.startswith("/"):
            return False
        depth = len([p for p in n.split("/")[:-1] if p not in ("", ".")])
        for part in t.split("/"):
            if part == "..":
                depth -= 1
                if depth < 0:
                    return False
            elif part not in ("", "."):
                depth += 1
    return True


# ───────────────────────── fetching ─────────────────────────

def _fetch_books() -> None:
    arch = machine()
    if not arch:
        raise RuntimeError("calibre has no build for this kind of machine (" + (platform.machine() or "unknown") + ")")
    want_size, want_sha = CALIBRE_FILES[arch]
    os.makedirs(ROOT, exist_ok=True)
    if shutil.disk_usage(ROOT).free < want_size + CALIBRE_UNPACKED + (1 << 30):
        raise RuntimeError("not enough room on the disk: it needs about 2 GB while it is unpacked")
    part = os.path.join(ROOT, "calibre.txz.part")
    h = hashlib.sha256()
    s = _state["books"]
    req = urllib.request.Request(calibre_url(arch), headers={"User-Agent": "tgarr"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            s["done"] += len(chunk)
    try:
        if os.path.getsize(part) != want_size or h.hexdigest() != want_sha:
            raise RuntimeError("what arrived is not the calibre that was expected (it may have been replaced where it is kept); not installed")
        tmp = books_dir() + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        with tarfile.open(part, "r:xz") as t:
            for m in t:
                if not safe_member(m.name, m.issym() or m.islnk(), m.linkname) or not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
                    raise RuntimeError("the archive holds something that does not belong in it: " + m.name[:80])
                t.extract(m, tmp)
        if not os.path.exists(os.path.join(tmp, "ebook-convert")):
            raise RuntimeError("the archive is not calibre's: no ebook-convert in it")
        shutil.rmtree(books_dir(), ignore_errors=True)
        os.replace(tmp, books_dir())
        with open(_ready("books"), "w") as f:
            f.write(CALIBRE + " " + arch + " " + time.strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        try:
            os.unlink(part)
        except OSError:
            pass
        shutil.rmtree(books_dir() + ".new", ignore_errors=True)


def _fetch_runtime() -> None:
    d = runtime_dir()
    tmp = d + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "--no-warn-script-location",
                            # ready-built packages only (there is no compiler here) - but jieba, which is plain
                            # Python and published as source alone
                            "--only-binary", ":all:", "--no-binary", "jieba", "--target", tmp] + list(RUNTIME),
                           capture_output=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError("pip could not install it: " + (r.stderr.decode("utf-8", "replace").strip().splitlines() or ["no reason given"])[-1][:200])
        shutil.rmtree(d, ignore_errors=True)
        os.replace(tmp, d)
        with open(_ready("runtime"), "w") as f:
            f.write(" ".join(RUNTIME) + " " + time.strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    activate()


def ensure(name: str) -> None:
    """Here and now, unless it is there already. One at a time for each pack."""
    with _locks[name]:
        if installed(name):
            if name == "runtime":
                activate()
            return
        s = _state[name]
        s.update(doing=True, done=0, error="")
        try:
            (_fetch_books if name == "books" else _fetch_runtime)()
            log.info("[packs] %s installed", name)
        except Exception as e:
            s["error"] = str(e)[:300]
            log.warning("[packs] installing %s: %s", name, e)
            raise
        finally:
            s["doing"] = False


def install(name: str) -> bool:
    if name not in NAMES or _state[name]["doing"] or installed(name):
        return False
    _state[name].update(doing=True, done=0, error="")

    def run():
        try:
            ensure(name)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()
    return True


def remove(name: str) -> None:
    if name not in NAMES:
        return
    with _locks[name]:
        d = books_dir() if name == "books" else runtime_dir()
        shutil.rmtree(d, ignore_errors=True)
        if name == "runtime" and d in sys.path:
            sys.path.remove(d)
        _state[name].update(done=0, error="")


activate()
