"""Metadata lookup: TMDB search, then Wikidata.

TMDB (themoviedb.org) — used when the user has set an API key in Settings.
    Posters, localized titles, overview, release date. The search runs in the
    language of the title and a result is only accepted when it really is that
    title (see pick_tmdb_hit).

Wikidata — free, no key, every language. Resolves a title to a screen work
    and its TMDB / IMDb ids. With a TMDB key the poster and details are then
    fetched by id; without one the user still gets the verified title, year
    and type.

lookup() returns a dict on a match, None when nothing was found, UNMATCHED
when the sources answered but not with this title, and raises LookupFailed
when a source could not be reached.

The caller (a background worker in main.py) reads the TMDB key from the
`config` table and passes it in via `tmdb_key=`.
"""
import asyncio
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("tgarr.metadata")

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p"
USER_AGENT = "tgarr/0.5 (+https://tgarr.me; metadata lookup)"


# Returned by lookup() when a source DID answer but nothing it offered is the
# title we asked about. Distinct from None (nothing found at all): callers
# should neither attach this metadata nor treat the release as fake.
UNMATCHED = {"source": "unmatched"}

_NOISE_TOKENS = re.compile(
    r"(?i)(?<![a-z0-9])(2160p|1080p|1080i|720p|576p|480p|360p|4k|8k|uhd|fhd|hd|sd|hdr|"
    r"hdr10|dv|web[ ._-]?dl|webrip|web|bluray|blu[ ._-]?ray|bdrip|brrip|hdtv|hdrip|"
    r"dvdrip|dvd|remux|x264|x265|h264|h265|hevc|avc|av1|aac|ac3|dts|flac|mp4|mkv|avi|"
    r"mov|ts|rmvb|wmv)(?![a-z0-9])")


def is_junk_title(title: str) -> bool:
    """True when a parsed 'title' carries no title at all — only resolution /
    codec / container tokens, digits and punctuation (e.g. "1080P.1080p",
    "720P.720p", "2026-01.01"). Looking these up finds unrelated works: TMDB
    really does have a film called "1080p"."""
    t = _NOISE_TOKENS.sub(" ", title or "")
    letters = re.sub(r"[\W\d_]+", "", t, flags=re.UNICODE)
    return len(letters) < 2


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").lower(), flags=re.UNICODE)


def title_matches(query: str, *candidates) -> bool:
    """Is any candidate the same title as `query`? Equal after stripping
    punctuation/case, or one contains the other and they are of similar length
    (>= 60%). 'Kiki' does not match 'Kiki's Delivery Service'."""
    q = _norm(re.sub(r"[ ._(\[-]+(19|20)\d{2}[)\]]?$", "", (query or "").strip()))
    if not q:
        return False
    for c in candidates:
        n = _norm(c)
        if not n:
            continue
        if q == n:
            return True
        short, long_ = (q, n) if len(q) <= len(n) else (n, q)
        if short in long_ and len(short) / len(long_) >= 0.6:
            return True
    return False


# Script of the query -> TMDB language. Asking in the query's own language makes
# TMDB return the localized title, which is the only thing a translated title
# ("福尔摩斯小姐2" for Enola Holmes 2) can be compared against: such a film's
# title and original_title are both English.
_SCRIPT_LANG = (
    ("[\u3040-\u30ff]", "ja-JP"),
    ("[\uac00-\ud7af]", "ko-KR"),
    ("[\u4e00-\u9fff]", "zh-CN"),
    ("[\u0400-\u04ff]", "ru-RU"),
    ("[\u0590-\u05ff]", "he-IL"),
    ("[\u0600-\u06ff]", "ar-SA"),
    ("[\u0900-\u097f]", "hi-IN"),
)


_AD_SUFFIX = re.compile(
    r"\s*[-\u2013\u2014~\uff5e|\uff5c]+\s*\S*(TG|tg|\u7535\u62a5|\u9891\u9053|\u5f71\u9662|\u8d44\u6e90|@).*$")


def clean_query_title(title: str) -> str:
    """Strip posting decorations that are not part of the title before asking
    a metadata source: a leading '#', a list index ("1 La La Land"), a channel
    advert after a dash ("Title -TG cinema ~ hot movies"), and a dangling
    opening bracket left by the parser."""
    t = (title or "").strip()
    t = re.sub(r"^[#\uff03\s]+", "", t)
    t = _AD_SUFFIX.sub("", t)
    t = re.sub(r"^\d{1,2}\s+(?=\D)", "", t)
    t = re.sub(r"[\s(\[{<\uff08\u3010\u300a\u300c\u300e]+$", "", t)
    return t.strip() or (title or "").strip()


def query_language(title: str):
    for rx, lang in _SCRIPT_LANG:
        if re.search(rx, title or ""):
            return lang
    return None


def _shared_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _hit_year(hit: dict):
    d = (hit.get("release_date") or hit.get("first_air_date") or "")[:4]
    return int(d) if d.isdigit() else None


def pick_tmdb_hit(title: str, year, results: list, lang):
    """Choose the search result that is really `title`, or None.

    1. A result whose title / original title equals the query (any script).
    2. Non-Latin query: a result whose LOCALIZED title starts the same way
       (>= 2 characters and at least half of the shorter one) — sequels and
       subtitles differ after the common stem.
    3. Non-Latin query with a year: the top result, if its year is within one
       of ours. TMDB search matches translated/alternative titles we cannot
       see, and the year is independent confirmation.
    Latin queries stay strict (rule 1 only): that is where the wrong posters
    came from ("Kiki" -> "Kiki's Delivery Service").
    """
    top = results[:5]
    for h in top:
        if title_matches(title, h.get("title"), h.get("name"),
                         h.get("original_title"), h.get("original_name")):
            return h
    if not lang:
        return None
    q = _norm(re.sub(r"[ ._(\[-]+(19|20)\d{2}[)\]]?$", "", (title or "").strip()))
    for h in top:
        for cand in (h.get("title"), h.get("name")):
            c = _norm(cand)
            if not c or not q:
                continue
            k = _shared_prefix(q, c)
            short = min(len(q), len(c))
            # a real common stem (>= 3 chars and at least half), or one title
            # is entirely the beginning of the other
            if (k >= 3 and k * 2 >= short) or (k >= 2 and k == short):
                return h
    if year and top:
        hy = _hit_year(top[0])
        if hy is not None and abs(hy - int(year)) <= 1 and len(q) >= 2:
            return top[0]
    return None


class LookupFailed(Exception):
    """A metadata source errored (network, 5xx, bad key). NOT a miss."""


def _fetch(url: str, params: dict | None = None, timeout: int = 12) -> dict:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ─────────────── TMDB tier ────────────────────────────────────────────
async def _tmdb_search(category: str, title: str, year: int | None,
                       key: str) -> dict | None:
    params = {"query": title, "include_adult": "false", "api_key": key}
    lang = query_language(title)
    if lang:
        params["language"] = lang
    if category == "tv" and year:
        params["first_air_date_year"] = year
        path = "/search/tv"
    elif category == "movie" and year:
        params["year"] = year
        path = "/search/movie"
    elif category == "tv":
        path = "/search/tv"
    else:
        path = "/search/movie"

    try:
        data = await asyncio.to_thread(_fetch, TMDB_BASE + path, params)
    except Exception as e:
        log.warning("tmdb %s %r %s: %s", path, title, year, e)
        raise LookupFailed(f"tmdb: {e}") from e

    results = data.get("results") or []
    if not results:
        return None
    # The first search result is not necessarily our title.
    hit = pick_tmdb_hit(title, year, results, lang)
    if hit is None:
        return UNMATCHED
    return {
        "source": "tmdb",
        "tmdb_id": hit.get("id"),
        "canonical_title": hit.get("title") or hit.get("name"),
        "poster_url": (f"{TMDB_IMG}/w342{hit['poster_path']}"
                       if hit.get("poster_path") else None),
        "backdrop_url": (f"{TMDB_IMG}/w780{hit['backdrop_path']}"
                         if hit.get("backdrop_path") else None),
        "overview": (hit.get("overview") or "")[:2000],
        "release_date": hit.get("release_date") or hit.get("first_air_date"),
        "vote_average": hit.get("vote_average"),
    }


# ─────────────── Wikidata tier ────────────────────────────────────────
# Free, keyless and multilingual: every entity carries its title in all
# languages (plus aliases), a type, a date, and the TMDB / IMDb ids. Used
#   * as the resolver when TMDB search did not find the title, and
#   * as the only source when the user has no TMDB key.
# It replaces the old English-Wikipedia fallback, which returned whatever page
# a search happened to find (universities, actors, phones).
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_WD_LANG = {"zh-CN": "zh", "ja-JP": "ja", "ko-KR": "ko", "ru-RU": "ru",
            "he-IL": "he", "ar-SA": "ar", "hi-IN": "hi"}
# Plain "zh" labels are often the Traditional / Taiwan title; a Chinese query
# is most likely Simplified, so prefer those labels when they exist.
_WD_LABEL_PREF = {"zh": ("zh-cn", "zh-hans", "zh", "zh-hant")}
# instance-of values that are screen works. Not exhaustive on purpose: an
# entity that carries a TMDB id or an IMDb title id is accepted as well.
_WD_FILM_TYPES = {
    "Q11424", "Q24869", "Q202866", "Q29168811", "Q20650540", "Q506240",
    "Q93204", "Q24862", "Q229390", "Q130232", "Q17013749", "Q18011172",
}
_WD_TV_TYPES = {
    "Q5398426", "Q63952888", "Q1259759", "Q581714", "Q15416", "Q526877",
    "Q1261214", "Q117467246", "Q21191270", "Q7725310", "Q113671041",
}


def _wd_claim(claims: dict, prop: str):
    for st in claims.get(prop, []):
        snak = st.get("mainsnak") or {}
        if "datavalue" in snak:
            return snak["datavalue"]["value"]
    return None


def _wd_year(claims: dict):
    for prop in ("P577", "P580"):          # publication date, start time
        v = _wd_claim(claims, prop)
        if isinstance(v, dict):
            y = (v.get("time") or "")[1:5]
            if y.isdigit():
                return int(y)
    return None


async def _wikidata_resolve(category: str, title: str, year: int | None) -> dict | None:
    """Find the screen work called `title` (in any language). Returns
    {kind, tmdb_id, imdb_id, title, year, image} / UNMATCHED / None."""
    lang = _WD_LANG.get(query_language(title) or "", "en")
    try:
        found = await asyncio.to_thread(_fetch, WIKIDATA_API, {
            "action": "wbsearchentities", "search": title, "language": lang,
            "uselang": lang, "type": "item", "limit": 7, "format": "json"})
    except Exception as e:
        log.warning("wikidata search %r: %s", title, e)
        raise LookupFailed(f"wikidata search: {e}") from e
    cands = found.get("search") or []
    if not cands:
        return None
    try:
        ents = (await asyncio.to_thread(_fetch, WIKIDATA_API, {
            "action": "wbgetentities", "ids": "|".join(c["id"] for c in cands),
            "props": "claims|labels",
            "languages": "|".join(_WD_LABEL_PREF.get(lang, (lang,)) + ("en",)),
            "format": "json"})).get("entities") or {}
    except Exception as e:
        log.warning("wikidata entities %r: %s", title, e)
        raise LookupFailed(f"wikidata entities: {e}") from e

    for c in cands:
        ent = ents.get(c["id"]) or {}
        claims = ent.get("claims") or {}
        types = {st["mainsnak"]["datavalue"]["value"]["id"]
                 for st in claims.get("P31", [])
                 if "datavalue" in (st.get("mainsnak") or {})}
        tmdb_movie = _wd_claim(claims, "P4947")
        tmdb_tv = _wd_claim(claims, "P4983")
        imdb = _wd_claim(claims, "P345")
        is_tv = bool(tmdb_tv) or bool(types & _WD_TV_TYPES)
        is_film = bool(tmdb_movie) or bool(types & _WD_FILM_TYPES)
        if not (is_tv or is_film or (isinstance(imdb, str) and imdb.startswith("tt"))):
            continue                         # a person, a place, a company …
        # The search matches labels and aliases, also loosely. Insist that the
        # matched text really is our title.
        matched = (c.get("match") or {}).get("text") or c.get("label") or ""
        if not (title_matches(title, matched, c.get("label"))
                or pick_tmdb_hit(title, None, [{"title": matched}], "x")):
            continue
        ey = _wd_year(claims)
        if year and ey and abs(ey - int(year)) > 1:
            continue                         # same name, different work
        labels = ent.get("labels") or {}
        kind = "tv" if (is_tv and not is_film) or (is_tv and category == "tv") else "movie"
        image = _wd_claim(claims, "P18")
        return {
            "kind": kind,
            "tmdb_id": (tmdb_tv if kind == "tv" else tmdb_movie) or tmdb_movie or tmdb_tv,
            "imdb_id": imdb if isinstance(imdb, str) else None,
            "title": (next(((labels.get(l) or {}).get("value")
                            for l in _WD_LABEL_PREF.get(lang, (lang,))
                            if (labels.get(l) or {}).get("value")), None)
                      or (labels.get("en") or {}).get("value") or matched),
            "year": ey,
            "image": image if isinstance(image, str) else None,
        }
    return UNMATCHED


# ─────────────── a series' names in other languages ──────────────────
# Sonarr knows a series by its English name and gives no translations, so a
# release named in Chinese, Japanese or Korean could never be matched to it.
_WD_NAMES_Q = """SELECT ?l WHERE { ?item wdt:P4835 "%d" .
  { ?item rdfs:label ?l } UNION { ?item skos:altLabel ?l } } LIMIT 400"""


def names_from_tmdb(found: dict, alt: dict, trans: dict) -> set:
    """Every name TMDB holds for a series: the original one, the alternative
    titles and each translation's name."""
    out = set()
    for hit in (found.get("tv_results") or [])[:1]:
        out.update(x for x in (hit.get("original_name"), hit.get("name")) if x)
    out.update(x.get("title") for x in (alt.get("results") or []) if x.get("title"))
    out.update((x.get("data") or {}).get("name") for x in (trans.get("translations") or [])
               if (x.get("data") or {}).get("name"))
    return {n.strip() for n in out if n and n.strip()}


def _series_names_sync(tvdb_id: int, key: str | None):
    if key:
        found = _fetch(f"{TMDB_BASE}/find/{int(tvdb_id)}",
                       {"external_source": "tvdb_id", "api_key": key})
        hits = found.get("tv_results") or []
        if hits:
            base = f"{TMDB_BASE}/tv/{int(hits[0]['id'])}"
            return names_from_tmdb(found, _fetch(base + "/alternative_titles", {"api_key": key}),
                                   _fetch(base + "/translations", {"api_key": key})), "tmdb"
    data = _fetch("https://query.wikidata.org/sparql",
                  {"format": "json", "query": _WD_NAMES_Q % int(tvdb_id)}, timeout=25)
    names = {b["l"]["value"].strip() for b in data.get("results", {}).get("bindings", [])
             if b.get("l", {}).get("value", "").strip()}
    return names, "wikidata"


async def series_names(tvdb_id: int, key: str | None):
    """(names, source) for a series known by its TVDB number. TMDB when a key
    is set (it has nearly everything, new series included); Wikidata
    otherwise (older and better-known series). Raises on a failed request so
    the caller can try again later instead of recording 'no names'."""
    return await asyncio.to_thread(_series_names_sync, tvdb_id, key)


async def _tmdb_by_id(kind: str, tmdb_id, key: str, lang) -> dict | None:
    """Details of a known TMDB id (no searching, so no wrong match)."""
    path = f"/{'tv' if kind == 'tv' else 'movie'}/{tmdb_id}"
    params = {"api_key": key}
    if lang:
        params["language"] = lang
    try:
        hit = await asyncio.to_thread(_fetch, TMDB_BASE + path, params)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise LookupFailed(f"tmdb id: {e}") from e
    except Exception as e:
        raise LookupFailed(f"tmdb id: {e}") from e
    return {
        "source": "tmdb",
        "tmdb_id": hit.get("id"),
        "canonical_title": hit.get("title") or hit.get("name"),
        "poster_url": (f"{TMDB_IMG}/w342{hit['poster_path']}"
                       if hit.get("poster_path") else None),
        "backdrop_url": (f"{TMDB_IMG}/w780{hit['backdrop_path']}"
                         if hit.get("backdrop_path") else None),
        "overview": (hit.get("overview") or "")[:2000],
        "release_date": hit.get("release_date") or hit.get("first_air_date"),
        "vote_average": hit.get("vote_average"),
    }


async def _wikidata_lookup(category: str, title: str, year: int | None,
                           tmdb_key: str | None) -> dict | None:
    wd = await _wikidata_resolve(category, title, year)
    if wd is None or wd is UNMATCHED:
        return wd
    if tmdb_key and wd.get("tmdb_id"):
        out = await _tmdb_by_id(wd["kind"], wd["tmdb_id"], tmdb_key,
                                query_language(title))
        if out:
            return out
    poster = None
    if wd.get("image"):
        poster = ("https://commons.wikimedia.org/wiki/Special:FilePath/"
                  + urllib.parse.quote(wd["image"].replace(" ", "_")) + "?width=342")
    return {
        "source": "wikidata",
        "tmdb_id": wd.get("tmdb_id"),
        "canonical_title": wd["title"],
        "poster_url": poster,
        "backdrop_url": None,
        "overview": "",
        "release_date": f"{wd['year']}-01-01" if wd.get("year") else None,
        "vote_average": None,
    }


# ─────────────── Unified entry point ──────────────────────────────────
async def lookup(category: str, title: str, year: int | None,
                 tmdb_key: str | None = None) -> dict | None:
    """Try TMDB search first if a key is configured, then Wikidata (which
    resolves titles in any language and hands back a TMDB id when it has one).

    Returns None ONLY on a genuine miss (both sources answered and had nothing).
    Raises LookupFailed when a source could not be reached / errored, so callers
    never mistake an outage for "this title does not exist".
    """
    if not title or len(title.strip()) < 2:
        return None
    title = clean_query_title(title)
    if is_junk_title(title):
        return UNMATCHED
    tmdb_err = None
    unmatched = False
    if tmdb_key:
        try:
            out = await _tmdb_search(category, title, year, tmdb_key)
        except LookupFailed as e:
            tmdb_err = e
            out = None
        if out is UNMATCHED:
            unmatched = True
        elif out:
            return out
    out = await _wikidata_lookup(category, title, year, tmdb_key)
    if out is UNMATCHED:
        unmatched, out = True, None
    if out:
        return out
    if tmdb_err is not None:
        raise tmdb_err
    return UNMATCHED if unmatched else None
