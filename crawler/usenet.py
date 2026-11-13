"""Usenet indexing - prototype (not wired into the crawler yet).

The idea: an install that has a Usenet account reads a slice of a newsgroup's
article headers, works out which articles belong together, and keeps one small
"card" per release - what it is called, what kind of media it is, how big,
where and when it was posted. The registry would hold only such cards. The
NZB (the list of every article of a release) is never stored: it is rebuilt
when somebody wants it, by reading that stretch of headers again, which takes
seconds.

Measured on a real account (one connection): 16,000 - 35,000 headers a second.
Posts from 2008 to about 2014 carry readable names in their subjects; most
newer ones are deliberately random and are not named by anything in this file.

Only standard library; no nntplib (removed from Python 3.13).
"""
import email.header
import email.utils
import hashlib
import re
import socket
import ssl
from xml.sax.saxutils import escape, quoteattr

# ───────────────────────── the wire ─────────────────────────


class NNTPError(Exception):
    pass


class NNTP:
    """The few NNTP commands indexing needs, over TLS, on one connection."""

    def __init__(self, host, port=563, user=None, password=None, use_ssl=True, timeout=45):
        raw = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if use_ssl else raw
        self.f = self.sock.makefile("rb")
        self.bytes_in = 0
        code, _ = self._reply()
        if code not in (200, 201):
            raise NNTPError(f"greeting {code}")
        if user:
            code, _ = self.cmd(f"AUTHINFO USER {user}")
            if code == 381:
                code, _ = self.cmd(f"AUTHINFO PASS {password}")
            if code != 281:
                raise NNTPError("the news server refused the account")   # never echo the reply: it may repeat the name
        try:
            self.cmd("MODE READER")          # some servers want it said; the answer does not matter
        except NNTPError:
            pass

    def _line(self):
        ln = self.f.readline()
        if not ln:
            raise NNTPError("connection closed")
        self.bytes_in += len(ln)
        return ln

    def _reply(self):
        ln = self._line().decode("utf-8", "replace").rstrip("\r\n")
        try:
            return int(ln[:3]), ln[4:]
        except ValueError:
            raise NNTPError("unexpected reply")

    def cmd(self, line):
        self.sock.sendall(line.encode("utf-8") + b"\r\n")
        return self._reply()

    def _multiline(self):
        out = []
        while True:
            ln = self._line()
            if ln in (b".\r\n", b".\n"):
                return out
            if ln.startswith(b".."):
                ln = ln[1:]
            out.append(ln.rstrip(b"\r\n"))

    def group(self, name):
        code, text = self.cmd(f"GROUP {name}")
        if code != 211:
            raise NNTPError(f"group not carried ({code})")
        count, first, last = (int(x) for x in text.split()[:3])
        return count, first, last

    def over(self, lo, hi):
        """[(article number, subject, poster, date, message-id, bytes)] for a range."""
        # OVER is the standard name, XOVER the older one; some large providers
        # only know the old one (and say so with 400, not 500). Remember which.
        verb = getattr(self, "_over", "OVER")
        code, _text = self.cmd(f"{verb} {lo}-{hi}")
        if code in (400, 500, 501, 502) and verb == "OVER":
            self._over = "XOVER"
            code, _text = self.cmd(f"XOVER {lo}-{hi}")
        if code == 423:
            return []
        if code != 224:
            raise NNTPError(f"OVER {code} {_text[:80]}")
        rows = []
        for ln in self._multiline():
            p = ln.split(b"\t")
            if len(p) < 7:
                continue
            try:
                rows.append((int(p[0]), decode(p[1]), decode(p[2]), p[3].decode("ascii", "replace"),
                             p[4].decode("ascii", "replace"), int(p[6] or 0)))
            except ValueError:
                continue
        return rows

    def stat(self, message_id) -> bool:
        """Does the server still hold this article? Nothing is downloaded."""
        code, _ = self.cmd(f"STAT {message_id}")
        return code == 223

    def quit(self):
        try:
            self.cmd("QUIT")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def decode(raw: bytes) -> str:
    """A header as text: UTF-8 if it is, Latin-1 otherwise, MIME words unpacked."""
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("latin-1")
    if "=?" in s:
        try:
            s = "".join(t.decode(c or "utf-8", "replace") if isinstance(t, bytes) else t
                        for t, c in email.header.decode_header(s))
        except Exception:
            pass
    return s


def when(date: str):
    """Unix time of a Date header, or None."""
    try:
        return int(email.utils.parsedate_to_datetime(date).timestamp())
    except Exception:
        return None


# ───────────────────────── subjects ─────────────────────────
# One article is one segment of one file. Its subject says which, in one of a
# handful of shapes that changed over the years:
#   [12/40] - "Show.S02E05.part03.rar" yEnc (17/66)            yEnc, the common one
#   Title [03/17] yEnc "01-track.flac" (1/20)                  yEnc before the name
#   Title - File 60 of 61 - yEnc "name.r58" (1/40)             "File n of m"
#   name.rar (03/26)                                           uuencode era: no quotes, no yEnc
#   "Show.S01E01.mkv" yEnc 734003200 (1/1024)                  a size between yEnc and the part

_PART = r"\((?P<part>\d{1,5})\s*/\s*(?P<total>\d{1,5})\)"
RX_SEG = re.compile(r"^(?P<head>.*?)\s*" + _PART + r"\s*(?:\d+)?\s*$", re.S)
RX_QUOTED = re.compile(r'"([^"]{1,300})"')
RX_YENC_TAIL = re.compile(r"\s*-?\s*yEnc(?:\s+\d+)?\s*$", re.I)
RX_BARE_FILE = re.compile(r"(\S+\.[A-Za-z0-9]{2,5})\s*$")
RX_COUNTER = re.compile(r"[\[(]\s*(\d{1,4})\s*(?:/|of)\s*(\d{1,4})\s*[\])]|\bFile\s+(\d{1,4})\s+of\s+(\d{1,4})\b", re.I)
RX_VOLUME = re.compile(
    r"(\.vol\d+\+\d+)?(\.part\d+)?(\.(rar|r\d{2,3}|s\d{2}|par2?|p\d{2}|7z|zip|\d{3}|nzb|nfo|sfv|md5|m3u|log|cue|txt|jpg|jpeg|png))*$",
    re.I)


def parse_subject(subject: str):
    """{title, file, part, total, file_no, files} for a segment, or None if the
    subject does not name a part of a file."""
    s = re.sub(r"\s+", " ", subject or "").strip()
    m = RX_SEG.match(s)
    if not m:
        return None
    head = RX_YENC_TAIL.sub("", m["head"]).strip()
    q = RX_QUOTED.findall(head)
    if q:
        fname = q[-1].strip()
        title = head[:head.rfind('"' + q[-1] + '"')]
    else:
        b = RX_BARE_FILE.search(head)
        fname = b.group(1) if b else ""
        title = head[:b.start()] if b else head
    title = RX_YENC_TAIL.sub("", title)
    c = RX_COUNTER.search(title)
    file_no = files = None
    if c:
        file_no, files = int(c.group(1) or c.group(3)), int(c.group(2) or c.group(4))
    title = RX_COUNTER.sub(" ", title)
    title = re.sub(r"\s+", " ", title).strip(" -–—:|>*=#")
    part, total = int(m["part"]), int(m["total"])
    if part < 0 or total < 1 or part > total:
        return None
    return {"title": title, "file": fname, "part": part, "total": total, "file_no": file_no, "files": files}


def base_name(fname: str) -> str:
    """A file's name without what only says which volume or helper file it is."""
    return RX_VOLUME.sub("", fname or "").strip(" .-_")


RX_RANDOM = re.compile(r"^[A-Za-z0-9+/=_-]{14,}$")


def is_random(text: str) -> bool:
    """A name made to mean nothing: one long run of letters and digits, no words."""
    t = (text or "").strip()
    if not t:
        return True
    core = re.sub(r"[\s._\-\[\](){}]", "", t)
    if len(core) < 6:
        return True
    if RX_RANDOM.match(t) and not re.search(r"[A-Za-z]{4,}[ ._-][A-Za-z]{3,}", t):
        # hex or mixed-case soup; a real word run of 5+ letters twice would have separators
        letters = re.findall(r"[A-Za-z]+", t)
        return not letters or max(len(x) for x in letters) < 9 or bool(re.search(r"\d", t))
    return False


def release_key(p: dict) -> str:
    """What the files of one release have in common. The words before the file
    name when they say something ("Miles Davis At Newport [03/17]" over 17
    differently named tracks); otherwise the file name without its volume
    number ("show.part03.rar", "show.part04.rar")."""
    if not is_random(p["title"]) and len(re.sub(r"\W", "", p["title"])) >= 8:
        return p["title"].lower()
    return base_name(p["file"]).lower() or p["title"].lower()


# ───────────────────────── what kind of thing ─────────────────────────

EXT = {
    "video": "mkv mp4 avi m4v ts m2ts wmv mpg mpeg vob mov divx ogm flv webm".split(),
    "audio": "mp3 flac ape wav m4a ogg wv aac alac opus m4b".split(),
    "ebook": "epub mobi azw azw3 pdf djvu cbr cbz fb2 lit chm".split(),
    "photo": "jpg jpeg png gif tif tiff bmp webp".split(),
}
RX_VIDEO = re.compile(r"S\d{1,2}E\d{1,3}|\b\d{1,2}x\d{2}\b|\b(480|576|720|1080|2160)[pi]\b|x26[45]|h\.?26[45]|xvid|divx|"
                      r"dvdrip|bdrip|brrip|bluray|blu-ray|web-?dl|webrip|hdtv|pdtv|dsr|dvd[59r]?\b|remux|\bntsc\b|\bpal\b", re.I)
RX_AUDIO = re.compile(r"\b(mp3|flac|lossless|\d{3}\s?kbps|v0|vbr|cdda|web-flac|vinyl|album|discography|bootleg|wavpack|ost)\b|-(cd|web|lp|ep)-", re.I)
RX_EBOOK = re.compile(r"\b(e-?books?|epub|mobi|azw3?|retail|magazines?|comics?|audiobook)\b", re.I)
RX_SOFTWARE = re.compile(
    r"\b(keygen|crack(ed)?|patch(ed)?|serial|activat(or|ion)|portable|incl\.|x86|x64|win(dows)?\s?(xp|7|8|10|11)?|"
    r"macos|osx|linux|setup|installer|\.exe|\.msi|\.dmg|apk|plugin|vst|adobe|microsoft|autodesk|office|"
    r"v\d+\.\d+(\.\d+)*\s?(build|multilingual)?|ps[2-5]|xbox|nsw|wii|gog|repack-|codex|skidrow|reloaded)\b", re.I)


def classify(name: str, files: list) -> str:
    """'video' | 'audio' | 'ebook' | 'photo' | 'software' | 'unknown'.
    By the files' own extensions when they show; by the words of the name when
    everything is packed in archives."""
    counts = {k: 0 for k in EXT}
    for f in files:
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
        for k, exts in EXT.items():
            if ext in exts:
                counts[k] += 1
    # a couple of cover pictures or a sample beside something else do not decide
    media = {k: v for k, v in counts.items() if v and k != "photo"}
    if media:
        return max(media, key=media.get)
    text = name + " " + " ".join(files[:6])
    if RX_VIDEO.search(text):
        return "video"
    if RX_SOFTWARE.search(text):
        return "software"
    if RX_AUDIO.search(text):
        return "audio"
    if RX_EBOOK.search(text):
        return "ebook"
    if counts["photo"] and counts["photo"] >= max(1, len(files) // 2):
        return "photo"
    return "unknown"


WANTED = ("video", "audio", "ebook", "photo")


# ───────────────────────── putting articles together ─────────────────────────

def poster_hash(poster: str) -> str:
    return hashlib.sha1((poster or "").strip().lower().encode("utf-8", "replace")).hexdigest()[:16]


class Assembler:
    """Feed it headers in any order; ask it for releases."""

    def __init__(self, group: str):
        self.group = group
        self.rel = {}                       # (poster hash, key) -> release
        self.headers = self.segments = 0

    def add(self, num, subject, poster, date, message_id, size):
        self.headers += 1
        p = parse_subject(subject)
        if not p:
            return None
        self.segments += 1
        key = release_key(p)
        if not key:
            return None
        ph = poster_hash(poster)
        r = self.rel.get((ph, key))
        if r is None:
            r = self.rel[(ph, key)] = {
                "key": key, "poster": ph, "group": self.group, "title": p["title"], "files": {},
                "files_expected": None, "bytes": 0, "t0": None, "t1": None,
                "art_lo": num, "art_hi": num, "first_message_id": message_id}
        f = r["files"].get(p["file"])
        if f is None:
            f = r["files"][p["file"]] = {"total": p["total"], "parts": {}, "bytes": 0, "subject": subject,
                                         "poster": poster, "date": date}
        if p["part"] not in f["parts"]:
            f["parts"][p["part"]] = (message_id, size)
            f["bytes"] += size
            r["bytes"] += size
        if p["files"]:
            r["files_expected"] = max(r["files_expected"] or 0, p["files"])
        t = when(date)
        if t:
            r["t0"] = t if r["t0"] is None else min(r["t0"], t)
            r["t1"] = t if r["t1"] is None else max(r["t1"], t)
        if num < r["art_lo"]:
            r["art_lo"], r["first_message_id"] = num, message_id
        r["art_hi"] = max(r["art_hi"], num)
        return r

    def releases(self):
        return list(self.rel.values())


def display_name(r: dict) -> str:
    """The name to show and to search: the words of the subject when they say
    something, otherwise the largest file's name."""
    t = r["title"]
    t = re.sub(r"\[\s*#?[\w.@-]+@\w+\s*\]|#[\w.]+@\w+", " ", t)          # [#a.b.teevee@EFNet]
    t = re.sub(r"\[\s*(FULL|REQ|REPOST|\d{3,})\s*\]|\bREQ\s*\d+\b", " ", t, flags=re.I)
    t = re.sub(r"[\[\]<>{}]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—:|>*=#.")
    if is_random(t) or len(re.sub(r"\W", "", t)) < 6:
        big = max(r["files"].items(), key=lambda kv: kv[1]["bytes"])[0] if r["files"] else ""
        t = base_name(big)
    return t


def card(r: dict):
    """The small record the registry would hold for one release, or None when
    there is nothing worth holding (no readable name, software, a scrap)."""
    files = [f for f in r["files"] if f]
    name = display_name(r)
    if is_random(name):
        return None
    kind = classify(name, files)
    if kind not in WANTED:
        return None
    seen = sum(len(f["parts"]) for f in r["files"].values())
    expected = sum(f["total"] for f in r["files"].values())
    payload = [f for f in files if not re.search(r"\.(par2|nfo|sfv|nzb|md5)$", f, re.I)]
    whole = seen >= expected and (not r["files_expected"] or len(r["files"]) >= r["files_expected"])
    cid = hashlib.sha1(f'{r["group"]}|{r["poster"]}|{r["key"]}'.encode("utf-8", "replace")).hexdigest()[:20]
    return {
        "id": cid, "name": name[:200], "kind": kind, "size": r["bytes"], "group": r["group"],
        "poster": r["poster"], "posted_from": r["t0"], "posted_to": r["t1"],
        "files": len(payload) or len(files), "files_expected": r["files_expected"],
        "segments": seen, "segments_expected": expected, "complete": bool(whole),
        "first_message_id": r["first_message_id"], "key": r["key"],
        # where this provider keeps it - a shortcut for this install only;
        # article numbers differ from one provider to the next
        "art_lo": r["art_lo"], "art_hi": r["art_hi"],
    }


# ───────────────────────── finding a time in a group ─────────────────────────

def article_at(nn: NNTP, first: int, last: int, ts: int) -> int:
    """The number of an article posted at about `ts` (binary search over
    article numbers, which rise with time). A few dozen small requests."""
    def dated(n):
        # the first dated article at or after n; a stretch of removed articles is stepped over
        for hop in range(6):
            rows = nn.over(n + hop * 5000, n + hop * 5000 + 99)
            for r in rows:
                t = when(r[3])
                if t:
                    return t
        return None
    lo, hi = first, last
    while hi - lo > 2000:
        mid = (lo + hi) // 2
        t = dated(mid)
        if t is None or t < ts:
            lo = mid
        else:
            hi = mid
    return lo


# ───────────────────────── the NZB, made when asked for ─────────────────────────

def build_nzb(r: dict) -> str:
    """NZB text for an assembled release (r as kept by Assembler)."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">',
           '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">',
           f' <head><meta type="name">{escape(display_name(r))}</meta></head>']
    for fname, f in sorted(r["files"].items()):
        out.append(f' <file poster={quoteattr(f["poster"])} date="{when(f["date"]) or 0}" subject={quoteattr(f["subject"])}>')
        out.append(f'  <groups><group>{escape(r["group"])}</group></groups>')
        out.append('  <segments>')
        for n in sorted(f["parts"]):
            mid, size = f["parts"][n]
            out.append(f'   <segment bytes="{size}" number="{n}">{escape(mid.strip("<>"))}</segment>')
        out.append('  </segments>')
        out.append(' </file>')
    out.append('</nzb>')
    return "\n".join(out) + "\n"
