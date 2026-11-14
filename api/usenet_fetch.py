"""Fetching small things from Usenet: a book, a song, a picture.

Films and series are never fetched by tgarr - Sonarr and Radarr hand their NZB to a real
downloader, which repairs and unpacks. What is taken here is only what was posted whole and
can be opened as it is: an .epub, an .mp3, a .jpg. Nothing packed (rar, zip, 7z), nothing
that needs repair (par2), never a program, never video. A file that does not arrive whole
and unharmed (its size and checksum are in the post) is not kept.

This file is the part with no database and no web framework in it:

    parse_nzb(text)            the files of an NZB and their parts
    plan(files)                which of them are taken, and why not when none is
    decode(lines)              one yEnc article -> its bytes
    fetch_file(body, f, path)  a whole file, part by part, checked
"""
import os
import re
import zlib
import xml.etree.ElementTree as ET

TAKEN = {"ebook": "epub mobi azw azw3 pdf djvu fb2 cbz cbr".split(),
         "audio": "mp3 flac m4a m4b ogg opus aac wav ape wv".split(),
         "photo": "jpg jpeg png gif webp".split()}
_KIND = {e: k for k, v in TAKEN.items() for e in v}
MB = 1024 * 1024
MOST = {"ebook": 400 * MB, "audio": 600 * MB, "photo": 60 * MB}      # one file
MOST_ALL = 2048 * MB                                                  # one release
MOST_FILES = 400
_HELPER = re.compile(r"\.(par2|nfo|sfv|nzb|md5|txt|url|diz)$", re.I)
_PACKED = re.compile(r"\.(rar|r\d\d|zip|7z|\d{3}|tar|gz|iso|bin)$", re.I)
_VIDEO = re.compile(r"\.(mkv|mp4|avi|m4v|ts|m2ts|wmv|mpg|mpeg|vob|mov|divx|flv|webm)$", re.I)
_MID = re.compile(r"^[^\s<>]{3,250}$")
_T = bytes((i - 42) & 255 for i in range(256))
_ESC = re.compile(rb"=(.)", re.S)


class Missing(Exception):
    """An article is not on the server any more, or did not arrive unharmed."""


def kind_of(name: str):
    """'ebook' | 'audio' | 'photo' for a file that is taken as it is; None for anything else."""
    return _KIND.get(name.rsplit(".", 1)[-1].lower()) if "." in (name or "") else None


def file_name(subject: str) -> str:
    """The file a subject line speaks of: what stands in quotes, else the word before 'yEnc'."""
    m = re.search(r'"([^"]{1,240})"', subject or "")
    if m:
        return m.group(1).strip()
    m = re.search(r"(\S+\.[A-Za-z0-9]{2,5})\s+(?:yEnc|\(\d+/\d+\))", subject or "")
    return m.group(1) if m else ""


def parse_nzb(text: str) -> list:
    """[{"name", "subject", "bytes", "segments": [(number, message-id, bytes)]}], parts in order."""
    out = []
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return out
    for f in root.iter():
        if not f.tag.endswith("}file") and f.tag != "file":
            continue
        segs = []
        for s in f.iter():
            if s.tag.endswith("}segment") or s.tag == "segment":
                mid = (s.text or "").strip().strip("<>")
                if _MID.match(mid):
                    segs.append((int(s.get("number") or 0), mid, int(s.get("bytes") or 0)))
        if segs:
            segs.sort()
            subject = f.get("subject") or ""
            out.append({"name": file_name(subject), "subject": subject, "bytes": sum(b for _n, _m, b in segs), "segments": segs})
    return out


def plan(files: list) -> dict:
    """{"take": [file, ...] each with "kind", "why": ''} - or nothing to take and why, in words
    for the person who asked."""
    payload = [f for f in files if not _HELPER.search(f["name"] or "")]
    take = []
    for f in payload:
        k = kind_of(f["name"])
        if k and f["bytes"] * 0.97 <= MOST[k]:
            take.append(dict(f, kind=k))
    if not take:
        if any(_VIDEO.search(f["name"] or "") for f in payload):
            why = "video is not fetched by tgarr - give it to Sonarr or Radarr"
        elif any(_PACKED.search(f["name"] or "") for f in payload):
            why = "packed in an archive: it needs a real downloader to repair and unpack"
        elif any(kind_of(f["name"]) for f in payload):
            why = "too large to fetch here"
        else:
            why = "nothing in it that can be opened as it is"
        return {"take": [], "why": why}
    if len(take) > MOST_FILES:
        return {"take": [], "why": f"more than {MOST_FILES} files: too many to fetch here"}
    if sum(f["bytes"] for f in take) * 0.97 > MOST_ALL:
        return {"take": [], "why": "too large to fetch here"}
    return {"take": take, "why": ""}


def _fields(line: bytes) -> dict:
    """'=ybegin part=1 line=128 size=123 name=a b.txt' -> {"part": "1", ..., "name": "a b.txt"}"""
    text = line.decode("utf-8", "replace")
    out = {}
    if " name=" in text:
        text, out["name"] = text.split(" name=", 1)
        out["name"] = out["name"].strip()
    for m in re.finditer(r"(\w+)=(\S+)", text):
        out[m.group(1)] = m.group(2)
    return out


def decode(lines: list) -> tuple:
    """One yEnc article (its body, as lines without line ends) -> (bytes, info).
    info: "name", "size" (of the whole file), "begin" (1-based place of this part, None for a
    file posted in one piece). Raises Missing when the article is not yEnc or is damaged."""
    i, n = 0, len(lines)
    while i < n and not lines[i].startswith(b"=ybegin "):
        i += 1
    if i >= n:
        raise Missing("not a yEnc article")
    head = _fields(lines[i])
    i += 1
    begin = None
    if i < n and lines[i].startswith(b"=ypart "):
        part = _fields(lines[i])
        begin = int(part.get("begin") or 1)
        i += 1
    out = bytearray()
    tail = None
    while i < n:
        ln = lines[i]
        i += 1
        if ln.startswith(b"=yend"):
            tail = _fields(ln)
            break
        if b"=" in ln:
            ln = _ESC.sub(lambda m: bytes(((m.group(1)[0] - 64) & 255,)), ln)
        out += ln.translate(_T)
    if tail is None:
        raise Missing("the article is cut short")
    data = bytes(out)
    if tail.get("size", "").isdigit() and int(tail["size"]) != len(data):
        raise Missing("the article is not the size it says")
    crc = tail.get("pcrc32") or (tail.get("crc32") if begin is None else None)
    if crc:
        try:
            if int(crc, 16) != zlib.crc32(data) & 0xFFFFFFFF:
                raise Missing("the article arrived damaged")
        except ValueError:
            pass
    size = int(head["size"]) if head.get("size", "").isdigit() else None
    return data, {"name": head.get("name", ""), "size": size, "begin": begin}


def fetch_file(body, f: dict, path: str, on_bytes=None) -> dict:
    """Fetch every part of `f` (as parse_nzb gives it) and write the file to `path`.
    `body(message_id)` gives an article's body as lines, or None when the server has it no
    more. The file appears under its name only when it is whole. {"size", "name"} - "name"
    as the post itself calls the file. Raises Missing."""
    tmp = path + ".part"
    size = name = None
    written = 0
    try:
        with open(tmp, "wb") as out:
            for _n, mid, _b in f["segments"]:
                lines = body(mid)
                if lines is None:
                    raise Missing("part of it is no longer on the news server")
                data, info = decode(lines)
                size = info["size"] if info["size"] is not None else size
                name = name or info["name"]
                if info["begin"] is not None:
                    out.seek(info["begin"] - 1)
                out.write(data)
                written += len(data)
                if on_bytes:
                    on_bytes(len(data))
            end = out.seek(0, os.SEEK_END)
        if not written or (size is not None and (end != size or written != size)):
            raise Missing("it did not arrive whole")
        os.replace(tmp, path)
        return {"size": end, "name": name or ""}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
