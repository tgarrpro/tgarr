"""Release filename parser — extracts title/year/season/episode/quality/codec/etc.

Approach: layered regex over normalized filename. Each regex extracts one
field and the remainder collapses into a clean title. Outputs a dict with a
`score` field (0.0-1.0) representing parse confidence.
"""
import re

RX_EXT = re.compile(
    r"\.(mkv|mp4|avi|m4v|mov|webm|flv|wmv|mpg|mpeg|3gp|ts|m2ts|iso|rmvb|rm)$",
    re.IGNORECASE,
)
RX_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
RX_TV_SXEY = re.compile(r"S(\d{1,3})\s*E(\d{1,3})(?:-E?(\d{1,3}))?", re.IGNORECASE)
RX_TV_NXM = re.compile(r"\b(\d{1,2})x(\d{1,3})\b")
RX_TV_VERBOSE = re.compile(
    r"Season[\s_.\-]*(\d{1,3})[\s_.\-]*Episode[\s_.\-]*(\d{1,3})", re.IGNORECASE
)
RX_TV_SEASON_PACK = re.compile(r"\bS(\d{1,2})(?!\s*E)\b|\bSeason[\s_.\-]+(\d{1,2})\b", re.IGNORECASE)
RX_RES = re.compile(r"\b(2160p|1440p|1080p|720p|576p|480p|360p|240p|4K|UHD)\b", re.IGNORECASE)
RX_SOURCE = re.compile(
    r"\b(BluRay|Blu[-_. ]?Ray|BDRip|BDMux|BR[-_. ]?Rip|WEB[-_. ]?DL|WEB[-_. ]?Rip|WEBMux|WEB|HDTV|HDRip|HC[-_. ]?HDRip|DVDRip|DVDR|DVD|R5|HDTS|TELESYNC|HDCAM|HD[-_. ]?CAM|CAMRip|CAM|TS|TC|PDVD|PreDVD|VHSRip|TVRip|SDTV)\b",
    re.IGNORECASE,
)
RX_CODEC = re.compile(r"\b(x264|x265|h\.?264|h\.?265|HEVC|AVC|AV1|XviD|DivX|VP9|MPEG2)\b", re.IGNORECASE)
RX_AUDIO = re.compile(
    r"\b(DTS[-_. ]?HD[-_. ]?MA|DTS[-_. ]?HD|DTS[-_. ]?X|DTS|DDP?5\.?1|EAC3|AC3|TrueHD|Atmos|AAC2?\.?\d?|MP3|FLAC|Opus|PCM|LPCM)\b",
    re.IGNORECASE,
)
RX_HDR = re.compile(r"\b(HDR10\+?|HDR|DV|Dolby[-_. ]?Vision|10bit|8bit|HLG)\b", re.IGNORECASE)
# Common Telegram release-channel language tags (English-name form).
RX_LANG = re.compile(
    r"\b(Hindi|Tamil|Telugu|Malayalam|Kannada|Bengali|Punjabi|English|Russian|Persian|Farsi|Arabic|Chinese|Mandarin|Cantonese|Japanese|Korean|Vietnamese|Indonesian|Thai|Spanish|Castellano|Portuguese|Brazilian|French|German|Italian|Turkish|Urdu|Marathi|Gujarati|Polish|Dutch|Greek|Hebrew|Multi|Dual[-_. ]?Audio|Eng[-_. ]?Sub|Eng[-_. ]?Subs?)\b",
    re.IGNORECASE,
)
RX_GROUP = re.compile(r"[-\.]([A-Z0-9][A-Z0-9._-]{2,15})$")
RX_AT_HANDLE = re.compile(r"[@\[\(]\w[\w_]{2,30}[\]\)]?", re.IGNORECASE)
RX_BRACKETS = re.compile(r"\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}")
RX_DOTSPACE = re.compile(r"[._]+")
RX_WS = re.compile(r"\s+")


_RES_NORM = {"4k": "2160p", "uhd": "2160p", "fhd": "1080p", "hd": "720p"}


def parse_filename(name: str) -> dict:
    """Parse a release-style filename into structured fields.

    Returns dict with possibly: title, year, season, episode, episode_end,
    type (movie|tv|unknown), quality, source, codec, audio, hdr, language,
    group, score (0.0..1.0).
    """
    if not name:
        return {"score": 0.0, "type": "unknown"}

    base = RX_EXT.sub("", name)
    work = base

    out = {}

    # TV episode
    m = RX_TV_SXEY.search(work)
    if m:
        out["season"] = int(m.group(1))
        out["episode"] = int(m.group(2))
        if m.group(3):
            out["episode_end"] = int(m.group(3))
        out["type"] = "tv"
        work = work[: m.start()] + " " + work[m.end():]
    else:
        m = RX_TV_NXM.search(work)
        if m:
            out["season"] = int(m.group(1))
            out["episode"] = int(m.group(2))
            out["type"] = "tv"
            work = work[: m.start()] + " " + work[m.end():]
        else:
            m = RX_TV_VERBOSE.search(work)
            if m:
                out["season"] = int(m.group(1))
                out["episode"] = int(m.group(2))
                out["type"] = "tv"
                work = work[: m.start()] + " " + work[m.end():]
            else:
                m = RX_TV_SEASON_PACK.search(work)
                if m:
                    out["season"] = int(m.group(1) or m.group(2))
                    out["type"] = "tv"
                    work = work[: m.start()] + " " + work[m.end():]

    # Year (only first match — usually the release year)
    m = RX_YEAR.search(work)
    if m:
        out["year"] = int(m.group(1))

    # Quality / source / codec / audio / hdr — each extracted via search,
    # also stripped from working title.
    def extract(rx, out_key, transform=None):
        m_ = rx.search(work)
        if m_:
            val = m_.group(1)
            if transform:
                val = transform(val)
            out[out_key] = val
            return m_

    def strip(rx):
        nonlocal work
        work = rx.sub(" ", work)

    if (m := RX_RES.search(work)):
        v = m.group(1).lower()
        out["quality"] = _RES_NORM.get(v, v)
    strip(RX_RES)

    if (m := RX_SOURCE.search(work)):
        out["source"] = m.group(1).replace("-", "").replace("_", "").replace(" ", "")
    strip(RX_SOURCE)

    if (m := RX_CODEC.search(work)):
        v = m.group(1).lower().replace(".", "").replace("-", "")
        v = v.replace("h264", "x264").replace("h265", "x265").replace("hevc", "x265")
        out["codec"] = v
    strip(RX_CODEC)

    if (m := RX_AUDIO.search(work)):
        out["audio"] = m.group(1)
    strip(RX_AUDIO)

    if (m := RX_HDR.search(work)):
        out["hdr"] = m.group(1)
    strip(RX_HDR)

    langs = sorted({m.group(0).title() for m in RX_LANG.finditer(work)})
    if langs:
        out["language"] = langs[0]
        if len(langs) > 1:
            out["all_languages"] = langs
    strip(RX_LANG)

    if (m := RX_GROUP.search(work)):
        # Heuristic: group must be at very end and not a common false-positive
        cand = m.group(1)
        if cand.upper() not in {"MP3", "MP4", "MKV", "PART"}:
            out["group"] = cand
            work = work[: m.start()]

    # Strip @channel handles and bracketed annotations.
    work = RX_AT_HANDLE.sub(" ", work)
    work = RX_BRACKETS.sub(" ", work)

    # Cut at year if present (title is everything before the year).
    if "year" in out:
        ystr = str(out["year"])
        i = work.find(ystr)
        if i > 0:
            work = work[:i]

    # Normalize separators → spaces.
    work = RX_DOTSPACE.sub(" ", work)
    work = RX_WS.sub(" ", work).strip(" -_.")

    title = work
    if not title:
        title = RX_DOTSPACE.sub(" ", base[:80]).strip()

    out["title"] = title

    # Decide type
    if "type" not in out:
        if "year" in out:
            out["type"] = "movie"
        else:
            out["type"] = "unknown"

    # Score (0.0–1.0)
    s = 0.0
    if out.get("title") and len(out["title"]) >= 2:
        s += 0.20
    if out.get("year"):
        s += 0.20
    if out.get("season") is not None:
        s += 0.20
    if out.get("episode") is not None:
        s += 0.10
    if out.get("quality"):
        s += 0.15
    if out.get("source"):
        s += 0.05
    if out.get("codec"):
        s += 0.05
    if out.get("group"):
        s += 0.05
    out["score"] = round(min(s, 1.0), 2)

    return out


def to_release_name(parsed: dict, fallback: str = "") -> str:
    """Build a normalized release-name from parsed fields."""
    title = parsed.get("title")
    if not title:
        return fallback
    parts = [title.replace(" ", ".")]
    if parsed.get("type") == "tv" and parsed.get("season") is not None:
        sep = f"S{parsed['season']:02d}"
        if parsed.get("episode") is not None:
            sep += f"E{parsed['episode']:02d}"
            if parsed.get("episode_end") is not None:
                sep += f"-E{parsed['episode_end']:02d}"
        parts.append(sep)
    elif parsed.get("year"):
        parts.append(str(parsed["year"]))
    if parsed.get("quality"):
        parts.append(parsed["quality"])
    if parsed.get("source"):
        parts.append(parsed["source"])
    if parsed.get("codec"):
        parts.append(parsed["codec"])
    name = ".".join(parts)
    if parsed.get("group"):
        name += f"-{parsed['group']}"
    return name


if __name__ == "__main__":
    TESTS = [
        "Avengers.Endgame.2019.1080p.BluRay.x264-AMIABLE.mkv",
        "Game.of.Thrones.S08E06.1080p.WEB-DL.x265.mkv",
        "Soni.2019.720p.@HindiHDCinema.mkv",
        "Stranger.Things.S04E01.WEBRip.1080p.x265.HEVC-GROUP.mkv",
        "Star.Trek.Discovery.S05E10.2160p.HDR.HEVC.DDP5.1-FLUX.mkv",
        "[Hindi+Tamil+Telugu] RRR.2022.1080p.BluRay.DD5.1.x264.mkv",
        "Some Random Movie 2024 4K HDR.mkv",
        "IMG_0596.MP4",
        "Friends.S01E01.The.One.Where.Monica.Gets.A.Roommate.720p.WEB-DL.mkv",
    ]
    for t in TESTS:
        p = parse_filename(t)
        print(f"{t}")
        print(f"  → {p}")
        print(f"  → {to_release_name(p)}\n")
