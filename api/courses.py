"""Courses - the part with no web framework and no database in it.

Besides courses the catalogue may hold index entries ("links"): free material that its publisher
keeps on its own site - a textbook series, say. tgarr only points there: it shows the publisher, the
publisher's own terms and the addresses; whoever uses it opens or downloads it from that site in their
own browser. Nothing of it is fetched, copied or kept by tgarr.

A course is a list of sources in an order: a "pack" - one of the lists shipped with tgarr
(coursepacks/<language>/*.json), which names either items of the Internet Archive or a public
Telegram channel (all of it, or the posts that carry one tag) - a folder of one's own files, or
a channel one follows.
It keeps no lessons of its own: its lessons are the files those sources hold, in their order
(main.py reads them out of the library and keeps the list in the table course_items).

Also here: when a lesson counts as done for a reward, and what the reward comes to. A lesson
is not done because the bar was dragged to the end - the time really spent on it (counted by
the server from the "still here" signals of the page, as screen time is) has to be a fair part
of its length as well.
"""
import json
import os
import re

SLUG_RX = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
IA_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
TG_RX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")          # a public channel's @name, without the @
TAG_RX = re.compile(r"^#[^\s#]{1,40}$")                    # one tag: the posts of a channel that belong to this course
LEVELS = ("all", "5-8", "8-12", "12-16", "16+")
SUBJECTS = ("reading", "language", "math", "science", "history", "arts", "music", "computing", "life", "other")
RIGHTS = ("PD", "CC BY", "CC BY-SA", "CC BY-NC", "CC BY-NC-SA", "CC BY-ND", "CC BY-NC-ND", "OLD", "USGOV", "NOCR",
          "PUB")                                       # PUB: the publisher's own terms - an index entry quotes them
PARTS_MOST = 400
LINKS_MOST = 120
URL_RX = re.compile(r"^https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?(?:/[^\s<>\"']*)?$")

# rewards: what a parent may set, and the limits it is kept within
REWARD_DEFAULT = {"on": False, "per_lesson": 20, "most": 120, "share": 70, "book_minutes": 20, "learn_free": True}


def natural_key(name: str):
    """'Lesson 2' before 'Lesson 10': the digits of a name are compared as numbers."""
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in re.split(r"(\d+)", name or "") if p != ""]


def clean_pack(raw) -> dict:
    """One list of the folder courses/, checked: a dict as main.py uses it, or ValueError saying
    what is wrong with it (a list that is wrong is left out, with the reason in the log)."""
    if not isinstance(raw, dict):
        raise ValueError("not an object")
    slug = str(raw.get("slug") or "")
    if not SLUG_RX.match(slug):
        raise ValueError("slug: lower-case letters, digits and hyphens")
    title = " ".join(str(raw.get("title") or "").split())[:120]
    if not title:
        raise ValueError("no title")
    level, subject, right = str(raw.get("level") or "all"), str(raw.get("subject") or "other"), str(raw.get("right") or "")
    if level not in LEVELS:
        raise ValueError(f"level: one of {LEVELS}")
    if subject not in SUBJECTS:
        raise ValueError(f"subject: one of {SUBJECTS}")
    if right not in RIGHTS:
        raise ValueError(f"right: one of {RIGHTS} (a list names the right its material is under)")
    channel, tag = str(raw.get("channel") or "").lstrip("@"), str(raw.get("tag") or "")
    if raw.get("links"):
        # an index entry: where the publisher keeps it, and on what terms - never parts, never a channel
        if raw.get("parts") or channel:
            raise ValueError("an index entry names links only, not parts or a channel")
        if right != "PUB":
            raise ValueError("an index entry is under its publisher's own terms: right PUB")
        terms_url = str(raw.get("terms_url") or "")
        if not URL_RX.match(terms_url):
            raise ValueError("terms_url: the https address of the publisher's terms")
        terms = " ".join(str(raw.get("terms") or "").split())[:500]
        if not terms:
            raise ValueError("terms: the publisher's own words on how it may be used")
        links = []
        for x in raw.get("links") or []:
            if not isinstance(x, dict) or not URL_RX.match(str(x.get("url") or "")):
                raise ValueError("a link is {url: https://..., title, file: true when it is a file to save}")
            links.append({"url": str(x["url"]), "title": " ".join(str(x.get("title") or "").split())[:160], "file": bool(x.get("file"))})
        if len(links) > LINKS_MOST:
            raise ValueError(f"more than {LINKS_MOST} links")
        return {"slug": slug, "title": title, "about": " ".join(str(raw.get("about") or "").split())[:600],
                "lang": str(raw.get("lang") or "en")[:12], "level": level, "subject": subject, "right": right,
                "credit": " ".join(str(raw.get("credit") or "").split())[:300], "parts": [], "channel": "", "tag": "", "upto": 0,
                "links": links, "terms": terms, "terms_url": terms_url}
    if channel:
        # the lessons are the files posted in a public channel, in the order posted
        if raw.get("parts"):
            raise ValueError("a list names parts or a channel, not both")
        if not TG_RX.match(channel):
            raise ValueError("channel: the @name of a public channel")
        if tag and not TAG_RX.match(tag):
            raise ValueError("tag: one #tag without spaces")
        # Whoever may post in the channel could post anything under the tag tomorrow, and it would be a
        # lesson on a child's screen the same day. So a list is taken up to one post - the last that was
        # read when the list was checked - and what is posted later waits for the list to be raised.
        upto = raw.get("upto")
        if isinstance(upto, bool) or not isinstance(upto, int) or not 0 < upto < 2 ** 31:
            raise ValueError("upto: the number of the last post that was read (a channel's list is taken up to it)")
        return {"slug": slug, "title": title, "about": " ".join(str(raw.get("about") or "").split())[:600],
                "lang": str(raw.get("lang") or "en")[:12], "level": level, "subject": subject, "right": right,
                "credit": " ".join(str(raw.get("credit") or "").split())[:300], "parts": [], "channel": channel, "tag": tag, "upto": upto}
    parts, seen = [], set()
    for p in raw.get("parts") or []:
        if not isinstance(p, dict):
            raise ValueError("a part is an object")
        ia = str(p.get("ia") or "")
        if not IA_RX.match(ia):
            raise ValueError(f"part: not an Internet Archive identifier: {ia[:40]!r}")
        if ia in seen:
            raise ValueError(f"part twice: {ia}")
        seen.add(ia)
        parts.append({"ia": ia, "title": " ".join(str(p.get("title") or "").split())[:160]})
    if not parts:
        raise ValueError("no parts")
    if len(parts) > PARTS_MOST:
        raise ValueError(f"more than {PARTS_MOST} parts")
    return {"slug": slug, "title": title, "about": " ".join(str(raw.get("about") or "").split())[:600],
            "lang": str(raw.get("lang") or "en")[:12], "level": level, "subject": subject, "right": right,
            "credit": " ".join(str(raw.get("credit") or "").split())[:300], "parts": parts, "channel": "", "tag": "", "upto": 0}


def load_packs(folder: str, log=None) -> dict:
    """{slug: pack} of every *.json in the folder that is in order."""
    out = {}
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".json"))
    except OSError:
        return out
    for n in names:
        try:
            p = clean_pack(json.load(open(os.path.join(folder, n), encoding="utf-8")))
            if p["slug"] in out:
                raise ValueError("this slug is used by another list")
            out[p["slug"]] = p
        except Exception as e:
            if log:
                log(f"courses/{n}: left out: {e}")
    return out


# ── the catalogue: lists the registry hands out between releases ──
# A list a volunteer made in @tgarr0 should not wait for a release. So besides the lists in the
# image an install takes a catalogue from the registry. What keeps that safe is not the
# registry: the catalogue is signed where it is written, with a key the registry never holds,
# and an install takes only a catalogue whose signature is good under a key named here. On top
# of that a catalogue's list may name Internet Archive items, or the one channel named here -
# nothing else - and a catalogue older than the one an install has is not taken (so that an old
# one, with a list since withdrawn, cannot be played back to it).
CATALOGUE_KEYS = ("UNQXsA23nwAYFx7yGTvc9gSxHHOXID49Rby5MaPpvMQ=",)
CATALOGUE_CHANNELS = ("tgarr0", "tgarr_vi", "tgarr_ko", "tgarr_tl", "tgarr_fa")   # tgarr's own: Chinese, Vietnamese, Korean, Filipino, Persian
CATALOGUE_MOST, CATALOGUE_BYTES = 2000, 8 * 1024 * 1024


def clean_catalogue_pack(raw) -> dict:
    """A list of the catalogue: in order as any list is (clean_pack), and of no channel but ours."""
    p = clean_pack(raw)
    if p["channel"] and p["channel"].lower() not in CATALOGUE_CHANNELS:
        raise ValueError("a catalogue's list names no channel but " + ", ".join("@" + c for c in CATALOGUE_CHANNELS))
    if p["channel"] and not p["tag"]:
        raise ValueError("a catalogue's list over the channel names its tag")
    return p


def open_catalogue(doc, sig, least_serial: int = 0, keys=None) -> dict:
    """{"serial", "packs": {slug: pack}, "withdrawn": [slug]} of a signed catalogue, or ValueError
    saying why it is not taken. `doc`: the text that was signed; `sig`: its signature, base64."""
    import base64
    import ed25519
    if not isinstance(doc, str) or not isinstance(sig, str) or not doc or len(doc) > CATALOGUE_BYTES:
        raise ValueError("no catalogue")
    try:
        s = base64.b64decode(sig, validate=True)
    except Exception:
        raise ValueError("the signature is not readable")
    raw = doc.encode("utf-8")
    good = False
    for k in (CATALOGUE_KEYS if keys is None else keys):
        try:
            good = good or ed25519.verify(base64.b64decode(k), raw, s)
        except Exception:
            pass
    if not good:
        raise ValueError("the signature is not ours")
    try:
        d = json.loads(doc)
    except Exception:
        raise ValueError("not readable")
    if not isinstance(d, dict) or d.get("v") != 1:
        raise ValueError("a catalogue of a kind this version does not know")
    serial = d.get("serial")
    if isinstance(serial, bool) or not isinstance(serial, int) or serial <= 0:
        raise ValueError("no serial")
    if serial < least_serial:
        raise ValueError(f"older than the one in use ({serial} < {least_serial})")
    packs = {}
    for one in (d.get("packs") if isinstance(d.get("packs"), list) else [])[:CATALOGUE_MOST]:
        try:
            p = clean_catalogue_pack(one)
            packs.setdefault(p["slug"], p)
        except ValueError:
            continue                                   # a list this version cannot read is left out; the others stand
    gone = [g for g in (d.get("withdrawn") if isinstance(d.get("withdrawn"), list) else []) if isinstance(g, str) and SLUG_RX.match(g)][:CATALOGUE_MOST]
    for g in gone:
        packs.pop(g, None)
    return {"serial": serial, "packs": packs, "withdrawn": gone}


def ia_of(caption: str) -> str:
    """The Internet Archive item a fetched file came from, read out of its caption
    ("Title · archive.org/details/<identifier> · 1923"), or ''."""
    m = re.search(r"archive\.org/details/([A-Za-z0-9][A-Za-z0-9._-]{0,99})", caption or "")
    return m.group(1) if m else ""


def order_lessons(parts, files) -> list:
    """The lessons of a pack: for each part in its order, the files fetched from that item, in
    the natural order of their names. files: [(msg_id, ia, name)] -> [msg_id]."""
    by = {}
    for mid, ia, name in files:
        by.setdefault(ia, []).append((natural_key(name), mid))
    out = []
    for p in parts:
        out += [mid for _k, mid in sorted(by.get(p["ia"], []))]
    return out


def report(rows, shipped: dict, catalogue: dict = None) -> list:
    """What the registry is told about this install's courses. rows: [{kind, slug, title,
    lang, level, subject, license, parts (JSON text), lessons, done: [lessons done by each
    person who began it]}].

    Only a course made of Internet Archive items is told of at all - one of the shipped lists
    by its name, any other by the items it names (pointers to what is public at archive.org).
    A course over a channel, a folder or file names is not in what comes back: nothing of it
    leaves the install. With each go two numbers: how many began it, and the middle of how
    many lessons they did. Never who.

    One kind of channel course is told of: a list of the signed catalogue (`catalogue`:
    {slug: pack} as open_catalogue gave it) - a course we published ourselves, in our own
    channel, whose name says nothing of the household. It is told of by that name alone, and
    only while the course is what the catalogue says it is (the same channel, tag and post).
    A channel a family chose - also that same channel, taken whole - is never told of."""
    out = []
    for r in rows:
        if r.get("kind") == "channel":
            pk = (catalogue or {}).get(r.get("slug") or "")
            if pk and pk.get("channel") and pk["channel"].lower() in CATALOGUE_CHANNELS \
                    and (r.get("ref") or "") == f"@{pk['channel']} {pk['tag'] or '-'} {pk['upto']}":
                done = sorted(int(d) for d in (r.get("done") or []))
                out.append({"slug": pk["slug"], "lessons": int(r.get("lessons") or 0), "learners": len(done),
                            "done_median": done[len(done) // 2] if done else 0})
            continue
        if r.get("kind") != "pack":
            continue
        try:
            parts = [p["ia"] for p in json.loads(r.get("parts") or "[]")]
        except Exception:
            continue
        if not parts or not all(isinstance(p, str) and IA_RX.match(p) for p in parts):
            continue
        done = sorted(int(d) for d in (r.get("done") or []))
        one = {"title": r.get("title") or "", "lang": r.get("lang") or "", "level": r.get("level") or "all",
               "subject": r.get("subject") or "other", "right": r.get("license") or "", "lessons": int(r.get("lessons") or 0),
               "learners": len(done), "done_median": done[len(done) // 2] if done else 0}
        pk = shipped.get(r.get("slug") or "")
        if pk and [p["ia"] for p in pk["parts"]] == parts:
            one["slug"] = pk["slug"]                   # a shipped list, unchanged: its name says all
        else:
            one["parts"] = parts
        out.append(one)
    return out


def clean_reward(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}

    def num(k, lo, hi):
        try:
            return max(lo, min(hi, int(raw.get(k))))
        except (TypeError, ValueError):
            return REWARD_DEFAULT[k]
    return {"on": bool(raw.get("on")), "per_lesson": num("per_lesson", 0, 240), "most": num("most", 0, 720),
            "share": num("share", 10, 100), "book_minutes": num("book_minutes", 5, 120),
            "learn_free": raw.get("learn_free") is not False}


def lesson_done(rule: dict, spent_s: int, length_s, reached_end: bool) -> bool:
    """Has this lesson been done, for a reward? A thing with a length (a film, a recording):
    its end was reached and the time spent on it is the rule's share of its length. A thing
    without one (a book): it never counts this way - see credits()."""
    if not length_s or length_s <= 0:
        return False
    return bool(reached_end) and spent_s >= length_s * rule["share"] / 100.0 - 5


def credits(rule: dict, done_lessons: int, book_seconds: int) -> int:
    """How many lessons' worth today: the films and recordings done, and one for every so many
    minutes spent reading."""
    return max(0, int(done_lessons)) + max(0, int(book_seconds)) // (rule["book_minutes"] * 60)


def bonus_seconds(rule: dict, done_lessons: int, book_seconds: int) -> int:
    """The entertainment time earned today, in seconds (0 when rewards are off)."""
    if not rule.get("on"):
        return 0
    return min(rule["most"], credits(rule, done_lessons, book_seconds) * rule["per_lesson"]) * 60
