"""What a subject line is: something wanted, a program, a name made to mean nothing, or not a
part of a file at all.

Written after looking at what the first rules got wrong on seven million surveyed headers
(docs/USENET_INDEX.md):

  - a board's or a site's tag in the subject ("[ TOWN ]-[ www.town.ag ]", "Powered by http://...")
    is not an advert: it is how those boards post real releases. The tag is taken off and the
    rest is judged as usual.
  - names made to mean nothing come in more shapes than one long run of letters: GUIDs in
    braces, "[PRiVATE]" followed by runs of hex, ten mixed characters, a short code repeated as the file name.
  - "Some Program 2.6.0.rar" is software even without the word crack in it.
  - programs are never indexed, whatever they are posted as (EXECUTABLE).
"""
import re

import usenet as U

RX_TAG = re.compile(
    r"[\[<({:.\s>-]*\b(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9-]{1,30}\.(?:com|net|org|info|eu|nl|de|ag|tk|to|xxx|biz|cc|me|ws|in|us|co)\b[^\s\]>)}]*[\]>)}:.\s<-]*",
    re.I)
RX_BOARD = re.compile(
    r"(?:powered|sponsore?d|presented|brought to you)\s+by\b[^\[\"]{0,40}|partner of\b[^\[\"]{0,40}|\bpost(?:ed)?\s+op\b[^\[\"]{0,40}|"
    r"bezoek ook eens[^\[\"]{0,40}|\[\s*TOWN\s*\]|<\s*TOWN\s*>|-\[\s*(?:TV|MUSIC|MOVIES?|GAMES?|APPS?|PR0N[^\]]*|EBOOK|XXX)\s*\]|"
    r"usenet-space-cowboys|>>+\s*[\w-]{3,20}\s*<<+|<<+\s*[\w-]{3,20}\s*>>+|\b\d+[,.]\d+\s?[GM]B\b", re.I)
RX_PASSWORD = re.compile(r"[<\[(]\s*passw(?:or[dt])\b|\bpassw(?:or[dt])\s*(?:protected|required|geschuetzt|:)|\bPW\s*:", re.I)
RX_GUID = re.compile(r"\{?[0-9A-Z]{8}-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{12}\}?", re.I)
RX_PRIVATE = re.compile(r"\[\s*PRiVATE\s*\]|\[newzNZB\]|::[0-9a-f]{8,}", re.I)
RX_WORD = re.compile(r"[A-Za-z]{4,}")
RX_VERSION = re.compile(r"\b(?:v(?:er(?:sion)?)?\.?\s?)?\d{1,4}\.\d{1,4}(?:\.\d{1,5}){0,3}\b|\b(?:build|b)\s?\d{3,}\b|\b(?:19|20)\d\d\s*(?:edition|suite|pro|premium)\b", re.I)
RX_SOFT_WORDS = re.compile(r"\b(bookware|iso-|keygen|crack|portable|multilingual|retail|oem|x86|x64|32-?bit|64-?bit|win(?:dows)?|linux|macos|"
                           r"studio|suite|pro|professional|ultimate|enterprise|manager|converter|toolkit|driver|plugin|antivirus|backup|burner|"
                           r"video2brain|lynda|tutorial|udemy|pluralsight)\b", re.I)
RX_HELPER = re.compile(r"\.(par2|sfv|nfo|nzb|txt|md5|diz)$|\.vol\d+\+\d+\.par2$", re.I)
VOWELS = set("aeiouAEIOU")


def strip_tags(text: str) -> str:
    t = RX_TAG.sub(" ", text or "")
    t = RX_BOARD.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip(" -–—:|>*=#.<[]()")


def looks_made_up(token: str) -> bool:
    """A name of the 'k576PYo2cA' kind: letters and digits with no word in them."""
    t = re.sub(r"\.(part\d+|vol\d+\+\d+|r\d\d|\d{3})$", "", token or "", flags=re.I)
    t = re.sub(r"\.(rar|par2|zip|7z|mkv|mp4|avi|nzb)$", "", t, flags=re.I)
    core = re.sub(r"[^A-Za-z0-9]", "", t)
    if len(core) < 6:
        return True
    if RX_GUID.search(token) or RX_PRIVATE.search(token):
        return True
    if len(t) > 40 and " " not in t and not re.search(r"[._-]", t):
        return True
    hexish = sum(c in "0123456789abcdefABCDEF" for c in core)
    if len(core) >= 16 and hexish >= 0.9 * len(core) and re.search(r"\d", core):
        return True
    if re.search(r"[\s._-][A-Za-z]{3,}[\s._-]", " " + t + " ") and len(RX_WORD.findall(t)) >= 2:
        return False                                # several words with separators: a name
    words = RX_WORD.findall(core)
    wordy = [w for w in words if any(c in VOWELS for c in w) and not re.search(r"[^aeiouAEIOU]{5,}", w)]
    switches = sum(1 for a, b in zip(core, core[1:]) if (a.isdigit() != b.isdigit()) or (a.isalpha() and b.isalpha() and a.islower() != b.islower()))
    if len(core) <= 14:
        return switches >= 3 and not any(len(w) >= 5 and w.islower() for w in wordy)
    return switches >= len(core) // 3 and not any(len(w) >= 6 for w in wordy)


def judge(subject: str):
    """(class, kind, clean title, file) - class is one of
    wanted / unknown / software / hidden / not a part / helper / password."""
    p = U.parse_subject(subject)
    if p is None:
        return "not a part", None, "", ""
    if RX_PASSWORD.search(subject):
        return "password", None, "", p["file"]
    title = strip_tags(p["title"])
    name = U.base_name(p["file"])
    if RX_GUID.search(subject) or RX_PRIVATE.search(subject):
        return "hidden", None, "", p["file"]
    title_empty = U.is_random(title) or looks_made_up(title) or len(re.sub(r"\W", "", title)) < 5
    if title_empty and (U.is_random(name) or looks_made_up(name)):
        return "hidden", None, "", p["file"]
    if RX_HELPER.search(p["file"] or ""):
        return "helper", None, title, p["file"]
    text = (title if not title_empty else "") + " " + name
    kind = U.classify(text, [p["file"]] if p["file"] else [])
    if kind in U.WANTED:
        return "wanted", kind, title or name, p["file"]
    if kind == "software" or (RX_VERSION.search(text) and not U.RX_VIDEO.search(text)) or RX_SOFT_WORDS.search(text):
        return "software", None, title or name, p["file"]
    return "unknown", None, title or name, p["file"]


# Never indexed, whatever group it is in and whatever it says it is.
EXECUTABLE = frozenset("exe scr msi bat com cmd pif vbs vbe js jse jar apk dll lnk ps1 sh dmg pkg deb rpm appx hta cpl".split())


def is_program(file_name: str) -> bool:
    n = (file_name or "").strip().lower()
    return "." in n and n.rsplit(".", 1)[-1] in EXECUTABLE
