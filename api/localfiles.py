"""The household's own files, beside what comes from Telegram.

Files uploaded through the browser are kept in /downloads/mine/ and appear
as one "chat" of their own, so that every player, the search, the Media
Center and its allow-list work on them exactly as they do on anything else.
That chat has an id no Telegram chat can have (see LOCAL_CHAT_MAX), which
is how the rest of the program tells it apart.

Nothing here touches the database or the network: names in, decisions out.
"""
import hashlib
import os
import re
import zlib

# Telegram chat ids have at most 13-14 digits, and the placeholders this program
# makes up for channels it has not resolved yet stay above -1e16. Everything at or below this
# is ours.
LOCAL_CHAT_MAX = -100_000_000_000_000_000
MINE = "mine"

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
    """(kind, mime type) of a file by its name. An upload is taken whatever
    it is (known_only=True: media and documents only). None = leave it out:
    system files, unfinished downloads."""
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
    return "My files" if root == MINE else root


def orphans(files, known, sidecars) -> list:
    """Of the saved files found on the disk, those nothing in the index points at.
    `files`: paths relative to the downloads folder; `known`: every local_path the index
    holds; `sidecars`: endings of the files kept beside a saved one ('.cover.jpg', ...).
    A file is spoken for when it is known itself, or is the sidecar of a known one -
    'book.epub.cover.jpg' beside 'book.epub', and 'film.tail' beside 'film'."""
    known = set(known)
    out = []
    for f in files:
        if f in known:
            continue
        if any(suf and f.endswith(suf) and f[:-len(suf)] in known for suf in sidecars):
            continue
        out.append(f)
    return out
