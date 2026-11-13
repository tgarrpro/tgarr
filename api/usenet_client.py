"""Talking to the registry about the Usenet index: taking a window of work, handing in cards,
searching. Plain HTTPS with the standard library; nothing here knows about NNTP.

What leaves this machine: the install's id (as for every other contribution), a short hash
of the news provider's domain (so that the registry can tell provider networks apart - never
the account), counts, hashes of posters, and cards: names and pointers.
"""
import gzip
import hashlib
import json
import urllib.parse
import urllib.request

# two-part public suffixes common among news providers; anything else: the last two labels
_TWO_PART = ("co.uk", "com.au", "co.nz", "co.za", "com.br")


def provider_key(host: str) -> str:
    """The same for every server name of one provider (news.example.com, eu.news.example.com,
    ssl-us.example.com): a hash of example.com."""
    h = (host or "").strip().lower().rstrip(".")
    labels = [x for x in h.split(".") if x]
    if not labels:
        return ""
    if labels[-1].isdigit():                       # an address, not a name: all of it
        dom = h
    else:
        n = 3 if ".".join(labels[-2:]) in _TWO_PART else 2
        dom = ".".join(labels[-n:])
    return hashlib.sha256(dom.encode()).hexdigest()[:16]


def _call(base: str, path: str, body=None, query=None, timeout: int = 60) -> dict:
    url = base.rstrip("/") + path + ("?" + urllib.parse.urlencode(query) if query else "")
    headers = {"User-Agent": "tgarr/usenet-index", "Accept": "application/json"}
    data = None
    if body is not None:
        data = gzip.compress(json.dumps(body, separators=(",", ":")).encode())
        headers.update({"Content-Type": "application/json", "Content-Encoding": "gzip"})
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def lease(base: str, instance_uuid: str, version: str, host: str, oldest=None, budget_articles: int = 0) -> list:
    out = _call(base, "/api/v1/usenet/lease", {"instance_uuid": instance_uuid, "tgarr_version": version,
                                               "provider_key": provider_key(host), "oldest": oldest,
                                               "budget_articles": budget_articles})
    return out.get("windows") or []


def pages(cards: list, size: int = 2000):
    """Cards in calls of `size`; always at least one call, so that a window with nothing in it
    is closed too."""
    if not cards:
        yield [], False
        return
    for i in range(0, len(cards), size):
        yield cards[i:i + size], i + size < len(cards)


def report(base: str, instance_uuid: str, version: str, host: str, window_id: int, stats: dict, posters: list,
           cards: list, empty: bool = False) -> dict:
    """Hand in one window, in as many calls as it takes. Returns the totals the registry gave."""
    total = {"accepted": 0, "refused": {}}
    first = True
    for chunk, more in pages(cards):
        out = _call(base, "/api/v1/usenet/report", {
            "instance_uuid": instance_uuid, "tgarr_version": version, "provider_key": provider_key(host),
            "window_id": window_id, "stats": stats if first else {}, "posters": posters if first else [],
            "cards": chunk, "more": more, "empty": empty and not cards}, timeout=180)
        if out.get("status") != "ok":
            raise RuntimeError(out.get("message") or "the registry did not take the report")
        total["accepted"] += out.get("accepted", 0)
        for k, v in (out.get("refused") or {}).items():
            total["refused"][k] = total["refused"].get(k, 0) + v
        first = False
    return total


def search(base: str, host: str, q: str, kind: str = "", limit: int = 100) -> list:
    out = _call(base, "/api/v1/usenet/search", query={"q": q or "", "kind": kind, "provider_key": provider_key(host),
                                                      "limit": limit, "audience": "any"}, timeout=20)
    return out.get("cards") or []
