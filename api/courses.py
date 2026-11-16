"""Courses - the part with no web framework and no database in it.

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
TAG_RX = re.compile(r"^#[^\s#%_]{1,40}$")                    # one tag: the posts of a channel that belong to this course
LEVELS = ("all", "5-8", "8-12", "12-16", "16+")
SUBJECTS = ("reading", "language", "math", "science", "history", "arts", "music", "computing", "life", "other")
RIGHTS = ("PD", "CC BY", "CC BY-SA", "CC BY-NC", "CC BY-NC-SA", "CC BY-ND", "CC BY-NC-ND", "OLD", "USGOV", "NOCR")
PARTS_MOST = 400

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
