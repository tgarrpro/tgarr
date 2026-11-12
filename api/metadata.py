"""Metadata lookup with two-tier fallback.

Tier 1 — TMDB (themoviedb.org), if user has set an API key in Settings.
         Best quality: posters, canonical titles, overview, release date,
         vote average. Requires user registration at themoviedb.org.

Tier 2 — Wikipedia REST API. Free, no key. Posters are en.wikipedia.org
         thumbnails (Commons-licensed). Lower coverage and quality than
         TMDB but acceptable as a zero-friction default.

The caller (typically a background worker in main.py) reads the TMDB key
from the `config` table and passes it in via `tmdb_key=`.
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
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_REST = "https://en.wikipedia.org/api/rest_v1/page/summary"
USER_AGENT = "tgarr/0.3 (+https://tgarr.me)"


# Returned by lookup() when a source DID answer but nothing it offered is the
# title we asked about. Distinct from None (nothing found at all): callers
# should neither attach this metadata nor treat the release as fake.
UNMATCHED = {"source": "unmatched"}

_NOISE_TOKENS = re.compile(
    r"(?i)(?<![a-z0-9])(2160p|1080p|1080i|720p|576p|480p|360p|4k|8k|uhd|fhd|hd|sd|hdr|"
    r"hdr10|dv|web[ ._-]?dl|webrip|web|bluray|blu[ ._-]?ray|bdrip|brrip|hdtv|hdrip|"
    r"dvdrip|dvd|remux|x264|x265|h264|h265|hevc|avc|av1|aac|ac3|dts|flac|mp4|mkv|avi|"
    r"mov|ts|rmvb|wmv)(?![a-z0-9])")
_SCREEN_WORK = re.compile(
    r"(?i)\b(film|movie|television|tv series|tv show|miniseries|mini-series|anime|"
    r"documentary|sitcom|web series|drama series|drama|telenovela|animated series|"
    r"animation|soap opera|ova|short film|serial)\b")


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


# ─────────────── Wikipedia tier ───────────────────────────────────────
async def _wiki_search_title(query: str) -> str | None:
    """Use the search API to find the best Wikipedia page title."""
    try:
        data = await asyncio.to_thread(_fetch, WIKI_API, {
            "action": "query", "list": "search", "format": "json",
            "srsearch": query, "srlimit": 1, "srprop": "size",
        })
    except Exception as e:
        log.warning("wiki search %r: %s", query, e)
        raise LookupFailed(f"wiki search: {e}") from e
    hits = (data.get("query") or {}).get("search") or []
    return hits[0]["title"] if hits else None


async def _wiki_summary(title: str) -> dict | None:
    """REST summary endpoint — gives thumbnail + extract."""
    url = WIKI_REST + "/" + urllib.parse.quote(title.replace(" ", "_"), safe="")
    try:
        data = await asyncio.to_thread(_fetch, url, None)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # page genuinely absent = a real miss
        log.warning("wiki summary %r: %s", title, e)
        raise LookupFailed(f"wiki summary: {e}") from e
    except Exception as e:
        log.warning("wiki summary %r: %s", title, e)
        raise LookupFailed(f"wiki summary: {e}") from e
    if data.get("type") != "standard":
        return None
    # Only pages about a screen work. Search happily returns universities,
    # actors, phones and airports for arbitrary strings.
    if not _SCREEN_WORK.search(data.get("description") or ""):
        return UNMATCHED
    thumb = data.get("thumbnail") or {}
    orig = data.get("originalimage") or {}
    return {
        "source": "wikipedia",
        "tmdb_id": None,
        "canonical_title": data.get("title"),
        "poster_url": thumb.get("source"),
        "backdrop_url": orig.get("source") if orig.get("width", 0) >= 800 else None,
        "overview": (data.get("extract") or "")[:2000],
        "release_date": None,
        "vote_average": None,
    }


async def _wiki_lookup(category: str, title: str,
                      year: int | None) -> dict | None:
    # Search refinement: try "{title} ({year} film)" / "({year} TV series)" first,
    # then "{title}" alone if no hit.
    candidates = []
    if year:
        if category == "tv":
            candidates.append(f"{title} ({year} TV series)")
        else:
            candidates.append(f"{title} ({year} film)")
        candidates.append(f"{title} {year}")
    candidates.append(title)

    saw_page = False
    for q in candidates:
        wiki_title = await _wiki_search_title(q)
        if not wiki_title:
            continue
        saw_page = True
        # "Hibiscus Town (film)" → compare without the disambiguator
        if not title_matches(title, re.sub(r"\s*\([^)]*\)\s*$", "", wiki_title)):
            continue
        result = await _wiki_summary(wiki_title)
        if result and result is not UNMATCHED and result.get("poster_url"):
            return result
    return UNMATCHED if saw_page else None


# ─────────────── Unified entry point ──────────────────────────────────
async def lookup(category: str, title: str, year: int | None,
                 tmdb_key: str | None = None) -> dict | None:
    """Try TMDB first if key configured, then Wikipedia.

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
    out = await _wiki_lookup(category, title, year)
    if out is UNMATCHED:
        unmatched, out = True, None
    if out:
        return out
    if tmdb_err is not None:
        raise tmdb_err
    return UNMATCHED if unmatched else None
