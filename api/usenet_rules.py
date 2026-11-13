"""What is never kept, what is never read, and what is adult, on Usenet.

Usenet carries everything. An index of it must not: sexual material with children is never
indexed, by name or by the group it was posted to, and adult material is labelled so that it
is only shown to who asked for it.
"""
import re

# ───────────────────────── what is never kept, and what is adult ─────────────────────────
# tgarr.me applies these same rules to whatever an install sends (and does not take an
# install's word for either). They are applied here first: before a card is sent, before it
# is stored on this machine, and before it is offered to Sonarr. Three rules:
#   never kept   a card whose name, inner name or listed file names carry a term for sexual
#                material with children, or that was posted to a group named for it; and a
#                card in an adult setting whose name says "minor" (an age under eighteen,
#                "schoolgirl", "lolitas", ...). Counted, never stored.
#   never read   a group named for it, and an adult group named for the young ("erotica.teen",
#                "erotica.young"): no window of it is handed out, whatever the survey found.
#   adult        a card posted to an adult group or with an adult word in its name is 'nsfw',
#                whatever the install said. Adult material is indexed - labelled, and left
#                out of every answer that did not ask for it.
# The patterns are written so that Postgres reads them exactly as Python does (no \b, no
# look-behind): the same strings clear out what is already stored.
_B0, _B1 = r"(^|[^a-z0-9])", r"($|[^a-z0-9])"
FORBIDDEN_SQL = _B0 + (r"(pthc|ptsc|hussyfan|r@ygold|raygold|babyshivid|kingpass|jail.?bait|pre.?teens?|lolicon|loli|shotacon|shota|"
                       r"p[ae]doph[a-z]*|kiddie|kiddy|kinder.?(porn|sex)|child(ren)?[^a-z0-9]*(porn|sex|erotic[a-z]*|nude|rape)|"
                       r"kids?[^a-z0-9]*(porn|sex)|under.?age[a-z]*|cp[0-9]{3,})") + _B1
MINOR_SQL = _B0 + (r"(lolitas?|p[ae]dos?|([1-9]|1[0-7])[^a-z0-9]?(yo|y[^a-z0-9]?o|yrs?|years?[^a-z0-9]?old|jahre|ans)|school.?girls?|school.?boys?|"
                   r"very.?young|tiny.?teens?|young.?(girls?|boys?)|little.?(girls?|boys?)|kids?|child(ren)?|minors?|prepubescent|toddlers?)") + _B1
ADULT_GROUP_SQL = _B0 + (r"(erotic[a-z]*|sex|porn[a-z]*|xxx|nude[a-z]*|nudis[a-z]*|hentai|fetish[a-z]*|bdsm|bondage|playboy|penthouse|hustler|"
                         r"voyeur[a-z]*|incest|lingerie)") + _B1
ADULT_NAME_SQL = _B0 + (r"(xxx|porn[a-z]*|hentai|erotic[a-z]*|blowjobs?|cumshots?|gangbang[a-z]*|milfs?|bdsm|creampie[a-z]*|deepthroat[a-z]*|"
                        r"handjobs?|threesome|orgy|pornstars?|brazzers|bangbros|naughtyamerica|playboy|penthouse|hustler|anal)") + _B1
GROUP_FORBIDDEN_SQL = _B0 + (r"(pre.?teens?|lolitas?|loli|p[ae]do[a-z]*|pthc|jail.?bait|kiddie|kiddy)") + _B1
# not read, but a card found elsewhere and merely cross-posted there is an ordinary card:
# the Disney Channel's series were posted to "multimedia.teen-idols" too
GROUP_NOT_READ_SQL = _B0 + (r"(teen.?idols?|child.?(models?|stars?))") + _B1
# ("teen" and "young" also as the start of a longer word: erotica.teenfemale, erotica.teensex)
GROUP_YOUNG_SQL = _B0 + (r"(teen[a-z]*|young[a-z]*|youth|child(ren)?|kids?|school.?(girls?|boys?)|early.?teens?|under.?age)") + _B1
FORBIDDEN_RX, MINOR_RX = re.compile(FORBIDDEN_SQL, re.I), re.compile(MINOR_SQL, re.I)
ADULT_GROUP_RX, ADULT_NAME_RX = re.compile(ADULT_GROUP_SQL, re.I), re.compile(ADULT_NAME_SQL, re.I)
GROUP_FORBIDDEN_RX, GROUP_YOUNG_RX = re.compile(GROUP_FORBIDDEN_SQL, re.I), re.compile(GROUP_YOUNG_SQL, re.I)
GROUP_NOT_READ_RX = re.compile(GROUP_NOT_READ_SQL, re.I)


def group_forbidden(grp: str) -> bool:
    """A group whose posts are never kept, wherever else they were posted too: one named
    for sexual material with children."""
    return bool(GROUP_FORBIDDEN_RX.search(grp or ""))


def group_never(grp: str) -> bool:
    """A group no window of which is handed out: those; an adult group named for the young
    ("erotica.teens" - what is posted there by name is of adults, and what is not cannot be
    told from a subject line: not read); and a few more not worth the risk. An adult film
    found in another group and cross-posted to such a one is an ordinary adult card."""
    g = grp or ""
    return bool(group_forbidden(g) or (ADULT_GROUP_RX.search(g) and GROUP_YOUNG_RX.search(g)) or GROUP_NOT_READ_RX.search(g))


def adult(card: dict) -> bool:
    return bool(any(ADULT_GROUP_RX.search(g) for g in card.get("groups") or ())
                or ADULT_NAME_RX.search(card.get("name") or "") or ADULT_NAME_RX.search(card.get("inner_name") or ""))


def forbidden(card: dict) -> bool:
    """Never kept (see above)."""
    items = card.get("items") if isinstance(card.get("items"), list) else ()      # (a count, on the way out of a search)
    names = [card.get("name") or "", card.get("inner_name") or ""] + [it.get("name") or "" for it in items]
    if any(FORBIDDEN_RX.search(x) for x in names) or any(group_forbidden(g) for g in card.get("groups") or ()):
        return True
    return (card.get("audience") == "nsfw" or adult(card)) and any(MINOR_RX.search(x) for x in names)
