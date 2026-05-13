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
import urllib.parse
import urllib.request

log = logging.getLogger("tgarr.metadata")

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p"
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_REST = "https://en.wikipedia.org/api/rest_v1/page/summary"
USER_AGENT = "tgarr/0.3 (+https://tgarr.me)"


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
        return None

    results = data.get("results") or []
    if not results:
        return None
    hit = results[0]
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
        return None
    hits = (data.get("query") or {}).get("search") or []
    return hits[0]["title"] if hits else None


async def _wiki_summary(title: str) -> dict | None:
    """REST summary endpoint — gives thumbnail + extract."""
    url = WIKI_REST + "/" + urllib.parse.quote(title.replace(" ", "_"), safe="")
    try:
        data = await asyncio.to_thread(_fetch, url, None)
    except Exception as e:
        log.warning("wiki summary %r: %s", title, e)
        return None
    if data.get("type") != "standard":
        return None
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

    for q in candidates:
        wiki_title = await _wiki_search_title(q)
        if not wiki_title:
            continue
        result = await _wiki_summary(wiki_title)
        if result and result.get("poster_url"):
            return result
    return None


# ─────────────── Unified entry point ──────────────────────────────────
async def lookup(category: str, title: str, year: int | None,
                 tmdb_key: str | None = None) -> dict | None:
    """Try TMDB first if key configured, then Wikipedia. Returns None on miss."""
    if not title or len(title.strip()) < 2:
        return None
    title = title.strip()
    if tmdb_key:
        out = await _tmdb_search(category, title, year, tmdb_key)
        if out:
            return out
    return await _wiki_lookup(category, title, year)
