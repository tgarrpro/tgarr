"""Many files as one download.

"Give me every photo of this chat" / "everything I have saved" as a single
zip. The zip is never written to disk: it is produced piece by piece while
the browser receives it, so exporting 40 GB needs no 40 GB of spare room.
Nothing here knows about Telegram or the database - names in, bytes out.
"""
import os
import re
import zipfile

_BAD = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
        "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/quicktime": ".mov", "video/webm": ".webm",
        "audio/mpeg": ".mp3", "audio/flac": ".flac", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
        "application/pdf": ".pdf", "application/epub+zip": ".epub", "application/zip": ".zip"}
KINDS = ("photo", "video", "audio", "document", "book", "all")


def safe_name(name, fallback="file") -> str:
    """A name every unzip program on every system accepts."""
    s = _BAD.sub("_", (name or "").strip()).strip(" .")
    if len(s) > 150:
        stem, ext = os.path.splitext(s)
        s = stem[:150 - len(ext)] + ext if len(ext) <= 10 else s[:150]
    return s or fallback


def item_name(item: dict) -> str:
    """The file's own name; a photo (Telegram gives those none) is named by
    its day and its place in the chat, so that a folder of them sorts in
    the order they were posted."""
    name = item.get("file_name")
    if name:
        return safe_name(name)
    day = item.get("posted_at")
    day = day.strftime("%Y-%m-%d") if hasattr(day, "strftime") else "undated"
    kind = item.get("media_type") or "file"
    ext = _EXT.get((item.get("mime_type") or "").lower()) or (".jpg" if kind == "photo" else "")
    return f"{kind}_{day}_{item.get('tg_message_id') or item.get('id')}{ext}"


def arc_names(items: list) -> list:
    """(name inside the zip, item) for each item: one folder per chat, and
    no two files with the same name (a second 'IMG_0001.jpg' becomes
    'IMG_0001 (2).jpg' rather than silently replacing the first)."""
    seen = {}
    out = []
    for it in items:
        folder = safe_name(it.get("folder"), "chat")
        name = item_name(it)
        key = (folder + "/" + name).lower()
        n = seen.get(key, 0) + 1
        seen[key] = n
        if n > 1:
            stem, ext = os.path.splitext(name)
            name = f"{stem} ({n}){ext}"
            seen[(folder + "/" + name).lower()] = 1
        out.append((folder + "/" + name, it))
    return out


class _Sink:
    """What the zip writer writes into: it only collects, never seeks."""

    def __init__(self):
        self.parts = []

    def write(self, b):
        self.parts.append(bytes(b))
        return len(b)

    def flush(self):
        pass

    def take(self) -> bytes:
        data = b"".join(self.parts)
        self.parts.clear()
        return data


def zip_stream(pairs, chunk: int = 1 << 20):
    """Yield the bytes of a zip holding (name inside the zip, path on disk)
    for each pair. Files are stored, not squeezed: photos, films and music
    are compressed already, and squeezing them again only costs time.
    A file that has gone missing is left out; the rest still arrives."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for arc, path in pairs:
            try:
                info = zipfile.ZipInfo.from_file(path, arc)
                src = open(path, "rb")
            except OSError:
                continue
            info.compress_type = zipfile.ZIP_STORED
            with src, zf.open(info, "w", force_zip64=True) as dst:
                while True:
                    block = src.read(chunk)
                    if not block:
                        break
                    dst.write(block)
                    if sink.parts:
                        yield sink.take()
            if sink.parts:
                yield sink.take()
    if sink.parts:
        yield sink.take()


def human(n) -> str:
    n = int(n or 0)
    if n <= 0:
        return ""
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            v = n / size
            return f"{v:.1f} {unit}" if v < 10 else f"{v:.0f} {unit}"
    return f"{n} B"
