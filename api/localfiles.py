"""The household's own files, beside what comes from Telegram.

Two sources:
  - files uploaded through the browser, kept in /downloads/mine/
  - folders that already exist somewhere else - a NAS share, a USB disk,
    a cloud drive mounted on the host - made visible to the container
    under /downloads/external/<name>/ and read where they are, never
    copied, never changed.

Each source appears as one "chat" of its own, so that every player, the
search, the Media Center and its allow-list work on these files exactly as
they do on anything else. Such a chat has an id no Telegram chat can have
(see LOCAL_CHAT_MAX), which is how the rest of the program tells them apart.

Nothing here touches the database or the network: names in, decisions out.
"""
import hashlib
import os
import re
import zlib

# Telegram's chat ids are at most 13-14 digits. Everything at or below this
# is ours.
LOCAL_CHAT_MAX = -8_000_000_000_000_000
MINE = "mine"
EXTERNAL = "external"

_KINDS = {
    "video": "mp4 mkv avi mov m4v webm wmv flv ts m2ts mpg mpeg 3gp rmvb",
    "audio": "mp3 flac m4a aac ogg opus wav wma ape alac aiff dsf wv",
    "photo": "jpg jpeg png webp gif bmp heic heif tif tiff",
    "document": "pdf epub mobi azw azw3 djvu fb2 cbz cbr txt doc docx xls xlsx ppt pptx zip rar 7z srt ass",
}
_EXT = {e: k for k, v in _KINDS.items() for e in v.split()}
_MIME = {"mp4": "video/mp4", "mkv": "video/x-matroska", "webm": "video/webm", "mov": "video/quicktime",
         "avi": "video/x-msvideo", "m4v": "video/mp4", "ts": "video/mp2t",
         "mp3": "audio/mpeg", "flac": "audio/flac", "m4a": "audio/mp4", "ogg": "audio/ogg",
         "opus": "audio/ogg", "wav": "audio/wav", "aac": "audio/aac",
         "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
         "gif": "image/gif", "pdf": "application/pdf", "epub": "application/epub+zip",
         "zip": "application/zip", "txt": "text/plain"}
_JUNK = re.compile(r"^(\.|~\$|thumbs\.db$|desktop\.ini$)|\.(part|tmp|crdownload|partial|!qb)$", re.I)
_BAD = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


def ext(name: str) -> str:
    return os.path.splitext(name or "")[1][1:].lower()


def classify(name: str, known_only: bool = False):
    """(kind, mime type) of a file by its name. A browser upload is taken
    whatever it is; a folder is read for media and documents only
    (known_only), so that a share full of installers and system files does
    not flood the library. None = leave it out."""
    base = os.path.basename(name or "")
    if not base or _JUNK.search(base):
        return None
    e = ext(base)
    kind = _EXT.get(e)
    if kind is None:
        if known_only:
            return None
        kind = "document"
    return kind, _MIME.get(e, "application/octet-stream")


def safe_name(name: str) -> str:
    """The uploaded file's own name, without anything that could place it
    outside its folder or that a file system refuses."""
    s = _BAD.sub("_", os.path.basename((name or "").replace("\\", "/"))).strip(" .")
    if len(s.encode("utf-8")) > 200:
        stem, e = os.path.splitext(s)
        while len((stem + e).encode("utf-8")) > 200 and stem:
            stem = stem[:-1]
        s = stem + e
    return s or "file"


def free_name(taken, name: str) -> str:
    """name, or 'name (2).ext', 'name (3).ext' ... - whichever is not in use.
    `taken` answers "is this name in use?"."""
    if not taken(name):
        return name
    stem, e = os.path.splitext(name)
    n = 2
    while taken(f"{stem} ({n}){e}"):
        n += 1
    return f"{stem} ({n}){e}"


def chat_id(root: str) -> int:
    """The made-up chat id of a source: the same every time for the same
    source, and never one Telegram could use."""
    return LOCAL_CHAT_MAX - zlib.crc32(root.encode("utf-8"))


def is_local(tg_chat_id) -> bool:
    return tg_chat_id is not None and int(tg_chat_id) <= LOCAL_CHAT_MAX


def unique_id(rel: str) -> str:
    return "local:" + hashlib.sha1(rel.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def title(root: str) -> str:
    if root == MINE:
        return "My files"
    return "📁 " + root.split("/", 1)[1] if "/" in root else root


def walk(base: str, limit: int = 200_000):
    """Every media file and document under base as (path below base, size,
    time changed). Hidden folders are skipped; a link is followed only if it
    does not lead back up (no going round in circles); an unreadable folder
    is passed over rather than ending the walk."""
    seen = set()
    n = 0
    for folder, dirs, files in os.walk(base, followlinks=True, onerror=lambda e: None):
        try:
            real = os.path.realpath(folder)
        except OSError:
            real = folder
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "@", "$", "#")))
        for f in sorted(files):
            if classify(f, known_only=True) is None:
                continue
            full = os.path.join(folder, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_size <= 0:
                continue
            yield os.path.relpath(full, base).replace(os.sep, "/"), st.st_size, int(st.st_mtime)
            n += 1
            if n >= limit:
                return


def diff(found: dict, known: dict):
    """found: path -> size as the folder is now; known: path -> size as it
    was indexed. Returns (new, gone, changed) lists of paths."""
    new = sorted(p for p in found if p not in known)
    gone = sorted(p for p in known if p not in found)
    changed = sorted(p for p in found if p in known and found[p] != known[p])
    return new, gone, changed
