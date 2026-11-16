"""What is fetched when it is first wanted, and not carried in the image.

tgarr's image used to weigh 2.3 GB, and of that tgarr itself was one megabyte. The rest was
there for two things most households never touch: calibre, to turn a .mobi or a .docx into a
PDF (1.5 GB with the browser engine it prints through), and the libraries the voice and the
picture model run on (230 MB), whose models were already fetched on demand.

calibre is gone altogether: the reader opens those books by itself now (ownbooks.py). What an
install fetched of it while it was still used is deleted by `forget_books()`.

    runtime   numpy, onnxruntime, sherpa-onnx, jieba - installed with pip into a folder that
              is put on Python's path, once, when the first model is fetched; it outlives an
              update of the image.

    status(name)            {installed, installing, done, bytes, error}
    install(name)           start fetching it (returns at once); True if it was started
    ensure(name)            fetch it now, in this thread, if it is not there; raises if it cannot be
    remove(name)
    activate()              put the runtime's folder on Python's path (called when this is imported)
    forget_books()          delete the calibre an older version fetched; bytes given back
"""
import importlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time

log = logging.getLogger("tgarr.packs")

ROOT = os.environ.get("TGARR_PACKS_DIR", "/downloads/.packs")

RUNTIME = ("numpy>=1.26,<3", "sherpa-onnx>=1.12.40,<1.13", "onnxruntime>=1.19,<2", "jieba==0.42.1")
RUNTIME_BYTES = 230 * 1024 * 1024                  # about; pip does not say beforehand

NAMES = ("runtime",)
_state = {n: {"doing": False, "done": 0, "error": ""} for n in NAMES}
_locks = {n: threading.Lock() for n in NAMES}


def machine(m: str = "") -> str:
    """This machine by the name its ready-built packages carry: 'x86_64', 'arm64', or ''."""
    m = (m or platform.machine() or "").lower()
    return {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(m, "")


def runtime_dir() -> str:
    # (one for each Python and machine: what pip installs is built for exactly that)
    return os.path.join(ROOT, f"py{sys.version_info[0]}{sys.version_info[1]}-{machine() or 'any'}")


def _ready(name: str) -> str:
    return os.path.join(runtime_dir(), ".ready")


def installed(name: str) -> bool:
    return name in NAMES and os.path.exists(_ready(name))


def size(name: str) -> int:
    return RUNTIME_BYTES


def status(name: str) -> dict:
    s = _state[name]
    return {"installed": installed(name), "installing": s["doing"], "done": s["done"], "bytes": size(name), "error": s["error"],
            "possible": True}


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


def forget_books() -> int:
    """The calibre a version before this one fetched (about 1 GB), deleted: nothing uses it
    any more. Returns the bytes given back; 0 when there was none."""
    freed = 0
    for name in ("calibre", "calibre.new", "calibre.txz.part"):
        p = os.path.join(ROOT, name)
        if os.path.isdir(p) and not os.path.islink(p):
            for base, _, files in os.walk(p):
                for f in files:
                    try:
                        freed += os.lstat(os.path.join(base, f)).st_size
                    except OSError:
                        pass
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            try:
                freed += os.path.getsize(p)
                os.unlink(p)
            except OSError:
                pass
    return freed


# ───────────────────────── fetching ─────────────────────────

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
            activate()
            return
        s = _state[name]
        s.update(doing=True, done=0, error="")
        try:
            _fetch_runtime()
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
        d = runtime_dir()
        shutil.rmtree(d, ignore_errors=True)
        if d in sys.path:
            sys.path.remove(d)
        _state[name].update(done=0, error="")


activate()
