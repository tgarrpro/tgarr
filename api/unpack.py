"""Two small programs of the system, asked for what Python cannot open by itself.

    bsdtar (libarchive-tools)   a RAR archive: the comics that are a real .cbr
    7zz    (7zip)               a .chm, Windows' compiled help: a book of HTML pages

Both are run with a time limit, never through a shell, and what they print is read up to a
limit - an archive is somebody else's file. Where a program is not installed (an image built
before it was added) the caller is told so with `have()` and goes another way."""
import os
import re
import shutil
import subprocess

RAR_MAGIC = b"Rar!\x1a\x07"
CHM_MAGIC = b"ITSF"
LIST_MOST = 20000          # entries of one archive looked at


def have(tool: str) -> bool:
    return bool(_which(tool))


def _which(tool: str):
    if tool == "7zz":
        return shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
    return shutil.which(tool)


def magic(path: str, n: int = 8) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def _run(args: list, most: int, timeout: int) -> bytes:
    """What a program prints, up to `most` bytes (more is an error). Raises OSError."""
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise OSError("the archive took too long to read")
    if len(out) > most:
        raise OSError("more came out of the archive than is taken")
    if p.returncode != 0 and not out:
        raise OSError(f"the archive could not be read (code {p.returncode})")
    return out


# ── RAR ───────────────────────────────────────────────────────────────────────────────
def rar_names(path: str) -> list:
    """The files of a RAR archive, by name, in its own order."""
    tool = _which("bsdtar")
    if not tool:
        raise OSError("no program here opens RAR archives")
    out = _run([tool, "-tf", path], 8 * 1024 * 1024, 60)
    names = [x for x in out.decode("utf-8", "replace").split("\n") if x and not x.endswith("/")]
    return names[:LIST_MOST]


def _literal(name: str) -> str:
    """A name as bsdtar must be given it to mean that one file (it reads * ? [ as patterns)."""
    return re.sub(r"([\\*?\[\]])", r"\\\1", name)


def rar_read(path: str, name: str, most: int) -> bytes:
    """One file of a RAR archive."""
    tool = _which("bsdtar")
    if not tool:
        raise OSError("no program here opens RAR archives")
    return _run([tool, "-xOf", path, "--fast-read", "--", _literal(name)], most, 90)


# ── CHM (and whatever else 7-Zip lists) ───────────────────────────────────────────────
def seven_list(path: str) -> list:
    """[(name, size)] of the files in an archive 7-Zip can open."""
    tool = _which("7zz")
    if not tool:
        raise OSError("no program here opens this kind of archive")
    out = _run([tool, "l", "-slt", "-ba", "-sccUTF-8", path], 16 * 1024 * 1024, 60).decode("utf-8", "replace")
    files, name, size, is_dir = [], None, 0, False

    def push():
        if name and not is_dir:
            files.append((name.replace("\\", "/"), size))
    for line in out.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("Path = "):
            push()
            name, size, is_dir = line[7:], 0, False
        elif line.startswith("Size = ") and line[7:].isdigit():
            size = int(line[7:])
        elif line.startswith("Folder = ") or line.startswith("Attributes = D"):
            is_dir = is_dir or line.endswith("+") or line.startswith("Attributes = D")
    push()
    return files[:LIST_MOST]


def seven_extract(path: str, into: str, timeout: int = 180) -> None:
    """Everything in the archive, unpacked under `into` (7-Zip never writes outside it)."""
    tool = _which("7zz")
    if not tool:
        raise OSError("no program here opens this kind of archive")
    p = subprocess.run([tool, "x", "-y", "-bd", "-bso0", "-bsp0", "-o" + into, path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=timeout)
    if p.returncode not in (0, 1):          # 1 = warnings (a file it could not name, a locked one)
        raise OSError(f"the archive could not be unpacked (code {p.returncode})")


def seven_read(path: str, name: str, most: int) -> bytes:
    """One file of the archive."""
    tool = _which("7zz")
    if not tool:
        raise OSError("no program here opens this kind of archive")
    return _run([tool, "e", "-so", "-bd", "-bso0", "-bsp0", path, "--", name.replace("/", os.sep)], most, 90)
