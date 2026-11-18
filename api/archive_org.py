"""The Internet Archive (archive.org) as a source.

Old films, radio plays, audiobooks read by volunteers, scanned books and magazines, live
recordings: tens of millions of items that anybody may download, over plain HTTPS, without an
account. tgarr searches it when asked (nothing of it is indexed here), and fetches an item
when somebody presses Get - into the same folders and lists as everything else.

    search_url(q, shelf, page)        the address of one page of a search
    parse_search(body)                (how many in all, [items])
    plan(meta)                        of an item's files, the ones to take - one playable set,
                                      not the six formats the Archive keeps of everything
    download(url, dest, progress)     one file

Nothing but plan() and the parsing is checked by test_archive_org.py; the two functions that
touch the network are fetch_json() and download().
"""
import json
import os
import re
import urllib.parse
import urllib.request

UA = "tgarr (+https://tgarr.me)"
BASE = "https://archive.org"
# (key, sign, what the page calls it, what is asked of the Archive, the folder a fetch goes to)
SHELVES = (
    ("film", "🎬", "Films", "mediatype:movies", "films"),
    ("audiobook", "🎧", "Audiobooks", "collection:librivoxaudio", "audiobooks"),
    ("audio", "🎵", "Music and radio", "mediatype:audio AND NOT collection:librivoxaudio", "music"),
    ("book", "📖", "Books and magazines", "mediatype:texts", "books"),
    ("picture", "🖼", "Pictures", "mediatype:image", "pictures"),
)
SHELF = {s[0]: s for s in SHELVES}
ID_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
MOST_BYTES = 8 << 30                    # one fetch: a film in good quality, a long audiobook
MOST_FILES = 500
ROWS = 40

_VIDEO_WEB = ("mp4", "m4v", "webm")                         # a browser plays these as they are
_VIDEO_OTHER = ("mkv", "avi", "mpg", "mpeg", "mov", "ogv", "wmv", "flv")
_AUDIO = ("mp3", "flac", "m4a", "ogg", "opus", "wav", "m4b")
_TEXT = ("epub", "pdf", "cbz", "cbr", "mobi", "azw3", "djvu", "txt")
_IMAGE = ("jpg", "jpeg", "png", "gif", "webp", "avif")
_PROGRAM = frozenset("exe scr msi bat com cmd pif vbs vbe js jse jar apk dll lnk ps1 sh dmg pkg deb rpm appx hta cpl iso".split())
_NOT_THE_THING = re.compile(r"(^__ia|_thumb\.|\.thumbs/|_spectrogram\.png$|_meta\.|_files\.xml$)", re.I)
_SMALL_COPY = re.compile(r"(_64kb|_vbr|_512kb|_small|\.ia)\.(mp3|mp4)$", re.I)


def ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def clean_query(q: str) -> str:
    """What somebody typed, as plain words: the Archive's search has a grammar of its own
    (field:value, AND, brackets) that nobody means when typing a title."""
    words = re.findall(r"[^\W_]+(?:['’][^\W_]+)?", q or "", re.UNICODE)
    return " ".join(w for w in words if w not in ("AND", "OR", "NOT"))[:200]      # (in capitals only: "duck and cover" keeps its and)


def search_url(q: str, shelf: str, page: int = 1, rows: int = ROWS) -> str:
    """One page of what a shelf holds for these words, the most downloaded first. Items that
    can only be borrowed (the lending library) are left out: they cannot be fetched.
    Every word must be in the title or in the maker's name. Asked without naming a field, the
    Archive looks through the descriptions too, and with the most downloaded first that put
    whatever is popular and mentions the words above the thing itself: "night of the living
    dead" began with six items that were not it."""
    want = SHELF[shelf][3]
    words = " AND ".join(f'(title:"{w}" OR creator:"{w}")' for w in clean_query(q).split())
    query = (f"({words}) AND " if words else "") + f"{want} AND NOT access-restricted-item:true"
    params = [("q", query)] + [("fl[]", f) for f in ("identifier", "title", "creator", "year", "mediatype", "downloads", "item_size")]
    params += [("rows", str(rows)), ("page", str(max(1, int(page)))), ("sort[]", "downloads desc"), ("output", "json")]
    return BASE + "/advancedsearch.php?" + urllib.parse.urlencode(params)


def _one(v) -> str:
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v[:3])
    return str(v or "").strip()


def parse_search(body) -> tuple:
    """(how many in all, [{id, title, creator, year, size, downloads}])."""
    try:
        r = (json.loads(body) if isinstance(body, (str, bytes)) else body)["response"]
    except (ValueError, KeyError, TypeError):
        return 0, []
    out = []
    for d in r.get("docs") or []:
        ident = _one(d.get("identifier"))
        if not ID_RX.fullmatch(ident):
            continue
        try:
            size = int(d.get("item_size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append({"id": ident, "title": _one(d.get("title"))[:300] or ident, "creator": _one(d.get("creator"))[:200],
                    "year": _one(d.get("year"))[:4], "size": size, "downloads": int(d.get("downloads") or 0)})
    return int(r.get("numFound") or 0), out


def metadata_url(ident: str) -> str:
    return f"{BASE}/metadata/{urllib.parse.quote(ident)}"


def file_url(ident: str, name: str) -> str:
    return f"{BASE}/download/{urllib.parse.quote(ident)}/{urllib.parse.quote(name)}"


def shelf_of(md: dict) -> str:
    """Which shelf an item belongs on, from what the Archive says it is."""
    kind = _one(md.get("mediatype")).lower()
    col = md.get("collection")
    cols = [str(c).lower() for c in (col if isinstance(col, list) else [col] if col else [])]
    if kind == "movies":
        return "film"
    if kind in ("audio", "etree"):
        return "audiobook" if "librivoxaudio" in cols else "audio"
    if kind == "texts":
        return "book"
    if kind == "image":
        return "picture"
    return ""


def _usable(files) -> list:
    out = []
    for f in files or []:
        name = str(f.get("name") or "")
        try:
            size = int(f.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if (not name or size <= 0 or str(f.get("private") or "").lower() == "true" or _NOT_THE_THING.search(name)
                or ext(name) in _PROGRAM or ".." in name or name.startswith("/")):
            continue
        try:
            height = int(float(f.get("height") or 0))
        except (TypeError, ValueError):
            height = 0
        out.append({"name": name, "size": size, "ext": ext(name), "original": str(f.get("source") or "") == "original",
                    "of": str(f.get("original") or ""), "title": _one(f.get("title"))[:200], "height": height})
    return out


def _best_copies(files) -> list:
    """Of copies the Archive made itself, one for each original: the largest that is not
    one of its small ones, or the small one when that is all there is."""
    by = {}
    for f in files:
        key = f["of"] or f["name"]
        cur = by.get(key)
        rank = (0 if _SMALL_COPY.search(f["name"]) else 1, f["size"])
        if cur is None or rank > cur[0]:
            by[key] = (rank, f)
    return [f for _r, f in by.values()]


def _first_kind(files, exts) -> list:
    """The files of the first of `exts` the item has: the uploader's own where there are
    any, the Archive's copies of them otherwise."""
    for e in exts:
        got = [f for f in files if f["ext"] == e]
        if got:
            own = [f for f in got if f["original"]]
            return own or _best_copies(got)
    return []


def plan(meta: dict) -> dict:
    """{"shelf", "title", "year", "take": [{name, size, title}], "bytes", "why"}: the one set
    of an item's files worth having. `why` says, when nothing is taken, what stands in the way."""
    md = meta.get("metadata") if isinstance(meta, dict) else None
    out = {"shelf": "", "title": "", "year": "", "take": [], "bytes": 0, "why": ""}
    if not isinstance(md, dict) or meta.get("is_dark"):
        out["why"] = "the Internet Archive no longer shows this item"
        return out
    out.update(shelf=shelf_of(md), title=_one(md.get("title"))[:300] or _one(md.get("identifier")),
               year=(_one(md.get("year")) or _one(md.get("date")))[:4])
    if str(_one(md.get("access-restricted-item"))).lower() == "true":
        out["why"] = "this one can only be borrowed at archive.org, not downloaded"
        return out
    files = _usable(meta.get("files"))
    if out["shelf"] == "film":
        take = _first_kind(files, ("mp4", "m4v", "webm")) or [f for f in files if f["ext"] in _VIDEO_OTHER and f["original"]]
    elif out["shelf"] in ("audio", "audiobook"):
        take = _first_kind(files, ("mp3", "flac", "m4a", "ogg", "opus", "m4b", "wav"))
    elif out["shelf"] == "book":
        # (plain text last, and only the uploader's own: the Archive's own .txt of a scan is what a
        # machine read off the pages, misprints and all)
        take = (_first_kind(files, ("epub", "pdf", "cbz", "cbr", "mobi", "azw3", "djvu", "fb2", "azw", "prc", "chm", "docx", "rtf"))
                or [f for f in files if f["ext"] == "txt" and f["original"]][:1])
        if take and take[0]["ext"] == "epub":
            take = take[:1]                                  # one book: the Archive makes several of the same
    elif out["shelf"] == "picture":
        take = [f for f in files if f["ext"] in _IMAGE and f["original"]]
    else:
        out["why"] = "not a film, a recording, a book or a picture"
        return out
    take.sort(key=lambda f: f["name"].lower())
    if not take:
        out["why"] = "nothing in it that can be played or read as it is"
        return out
    kept, total = [], 0
    for f in take[:MOST_FILES]:
        if total + f["size"] > MOST_BYTES:
            break
        kept.append({"name": f["name"], "size": f["size"], "title": f["title"], "height": f["height"]})
        total += f["size"]
    if not kept:
        out["why"] = "too large to fetch here"
        return out
    out.update(take=kept, bytes=total)
    if len(kept) < len(take):
        out["why"] = f"the first {len(kept)} of {len(take)} files: the rest is over what one fetch may be"
    return out


# ───────────────────────── for Radarr ─────────────────────────
# A film of the Archive offered to Radarr (through tgarr's own indexer and download client:
# nobody needs an account for any of it). Radarr decides by the name of a release, so the
# item gets one of the kind it reads: Title.Year.Quality.WEB-archive.org.

def _words(text: str) -> list:
    return [w.casefold() for w in re.findall(r"[^\W_]+", text or "", re.UNICODE)]


def split_year(q: str) -> tuple:
    """('night of the living dead', '1968') from what Radarr asks; no year: ''."""
    words = clean_query(q).split()
    if len(words) > 1 and re.fullmatch(r"(19|20)\d\d", words[-1]):
        return " ".join(words[:-1]), words[-1]
    return " ".join(words), ""


# a word beside the title that says the item is something about the film, or a piece of it
_NOT_THE_FILM = frozenset("trailer trailers teaser promo preview clip clips excerpt review commentary soundtrack ost podcast "
                          "featurette interview making outtakes bloopers audiobook radio lecture".split())


def is_the_film(title: str, year: str, asked: str, asked_year: str) -> bool:
    """Is an item what was asked for? Its title is the asked words and nothing more than a
    few besides (the Archive's search also finds every talk ABOUT a film), and where both
    name a year, the same one."""
    have, want = _words(title), _words(asked)
    if not want or any(w not in have for w in want):
        return False
    extra = [w for w in have if w not in want and not re.fullmatch(r"(19|20)\d\d", w)]
    if len(extra) > 3 or any(w in _NOT_THE_FILM for w in extra):
        return False
    return not (year and asked_year and year != asked_year)


def registry_url(registry: str, asked: str, year: str = "", kind: str = "film") -> str:
    """Where tgarr.me is asked for the Archive's items under a title. It keeps a list of the
    ones people download (a few million of 85 million), so that a Radarr with a long list of
    films does not put each of them to the Archive's own search from every install."""
    params = [("q", asked), ("kind", kind)] + ([("year", year)] if year else [])
    return registry.rstrip("/") + "/api/v1/archive/search?" + urllib.parse.urlencode(params)


def parse_registry(body) -> list:
    """Its answer in the shape parse_search() gives. Raises ValueError when it is no answer
    (so that the caller asks the Archive itself)."""
    d = json.loads(body) if isinstance(body, (str, bytes)) else body
    if not isinstance(d, dict) or d.get("status") != "ok" or not isinstance(d.get("items"), list):
        raise ValueError("no answer")
    out = []
    for i in d["items"]:
        ident = _one(i.get("id")) if isinstance(i, dict) else ""
        if not ID_RX.fullmatch(ident):
            continue
        out.append({"id": ident, "title": _one(i.get("title"))[:300] or ident, "creator": _one(i.get("creator"))[:200],
                    "year": _one(i.get("year"))[:4], "size": int(i.get("size") or 0), "downloads": int(i.get("downloads") or 0)})
    return out


def release_name(title: str, year: str, height: int, asked: str = "") -> str:
    """Title.Year.Quality.WEB-archive.org. With the title that was asked for, the name starts where that
    title starts in the item's: "tweakers.6325 Tears of Steel" -> Tears.of.Steel (an uploader's prefix in front
    made Radarr refuse it, QA 2026-11-17 #18)."""
    quality = "1080p" if height >= 1000 else "720p" if height >= 700 else "480p" if height else "SD"
    words = re.findall(r"[^\W_]+", title or "", re.UNICODE)
    want = [w.lower() for w in re.findall(r"[^\W_]+", asked or "", re.UNICODE)]
    if want:
        low = [w.lower() for w in words]
        at = next((k for k in range(len(low) - len(want) + 1) if low[k:k + len(want)] == want), 0)
        words = words[at:]
    name = ".".join(words)[:150] or "Untitled"
    return f"{name}{'.' + year if year else ''}.{quality}.WEB-archive.org"


def film_file(p: dict):
    """Of a planned film, the one file Radarr gets: the largest (an item often holds two cuts)."""
    return max(p["take"], key=lambda f: f["size"]) if p.get("shelf") == "film" and p.get("take") else None


# ───────────────────────── the network ─────────────────────────

def fetch_json(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(64 << 20).decode("utf-8", "replace"))


def download(url: str, dest: str, progress=None, want: int = 0, timeout: int = 60) -> int:
    """One file to `dest` (through dest + '.part'). Returns its size; raises OSError when it
    did not arrive whole."""
    part = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    n = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
                if progress:
                    progress(len(chunk))
        if n == 0 or (want and n != want):
            raise OSError(f"{n} of {want} bytes arrived")
        os.replace(part, dest)
        return n
    finally:
        try:
            os.unlink(part)
        except OSError:
            pass
