"""Release filename parser — extracts title/year/season/episode/quality/codec/etc.

Approach: layered regex over normalized filename. Each regex extracts one
field and the remainder collapses into a clean title. Outputs a dict with a
`score` field (0.0-1.0) representing parse confidence.
"""
import re
import time

RX_EXT = re.compile(
    r"\.(mkv|mp4|avi|m4v|mov|webm|flv|wmv|mpg|mpeg|3gp|ts|m2ts|iso|rmvb|rm)$",
    re.IGNORECASE,
)
# not \b: a year glued to CJK text ("美好的一年2006") has no word boundary
RX_YEAR = re.compile(r"(?<![0-9A-Za-z])(19\d{2}|20\d{2})(?![0-9A-Za-z])")
RX_TV_SXEY = re.compile(r"S(\d{1,3})\s*E(\d{1,3})(?:-E?(\d{1,3}))?", re.IGNORECASE)
RX_TV_NXM = re.compile(r"\b(\d{1,2})x(\d{1,3})\b")
RX_TV_VERBOSE = re.compile(
    r"Season[\s_.\-]*(\d{1,3})[\s_.\-]*Episode[\s_.\-]*(\d{1,3})", re.IGNORECASE
)
RX_TV_SEASON_PACK = re.compile(r"\bS(\d{1,2})(?!\s*E)\b|\bSeason[\s_.\-]+(\d{1,2})\b", re.IGNORECASE)
RX_RES = re.compile(r"\b(2160p|1440p|1080p|720p|576p|480p|360p|240p|4K|UHD)\b", re.IGNORECASE)
RX_SOURCE = re.compile(
    r"\b(BluRay|Blu[-_. ]?Ray|BDRip|BDMux|BR[-_. ]?Rip|WEB[-_. ]?DL|WEB[-_. ]?Rip|WEBMux|HDTV|HDRip|HC[-_. ]?HDRip|DVDRip|DVDR|HDTS|TELESYNC|HDCAM|HD[-_. ]?CAM|CAMRip|PDVD|PreDVD|VHSRip|TVRip|SDTV)\b",
    re.IGNORECASE,
)
# Short tags that are also ordinary words or initials ("The Web", "TS Eliot",
# "Cam"). Only a source when written in capitals AND sitting in the release-tag
# part of the name (after the year / resolution), never inside the title.
RX_SOURCE_WEAK = re.compile(r"\b(WEB|DVD|R5|CAM|TS|TC)\b")
# A year that is really part of a date or timestamp is not a release year:
#   2024-08-18, 2024.08.18, 20240818, 2024-08, 18-08-2024, 2024年8月
RX_DATE = re.compile(
    r"(?<!\d)(?:"
    r"(?:19|20)\d{2}[-./_ ]?(?:0[1-9]|1[0-2])[-./_ ]?(?:0[1-9]|[12]\d|3[01])(?!\d)"
    r"|(?:19|20)\d{2}[-/_](?:0[1-9]|1[0-2])(?!\d)"
    r"|(?:0?[1-9]|[12]\d|3[01])[-./_](?:0?[1-9]|1[0-2])[-./_](?:19|20)\d{2}(?!\d)"
    r"|(?:19|20)\d{2}\s*\u5e74\s*\d{1,2}\s*\u6708"
    r")")
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

# === regional extensions ===

# CJK/full-width brackets: 【】「」『』 + half-width 〔〕
RX_CJK_BRACKETS = re.compile(r"[【「『〔][^】」』〕]*[】」』〕]")

# Chinese audio/subtitle/source descriptors → map to standard fields
RX_CN_QUALITY = re.compile(r"(蓝光原盘|蓝光|藍光|超清|高清|原盘|原碟|国英双语|国粤双语|国语|粤语|英语|双语|中字|简繁字幕|繁字|内嵌字幕|外挂字幕|内封字幕|HD国语|4K高清|4K HDR|HDR10\+?)", re.IGNORECASE)
_CN_QUALITY_MAP = {
    "蓝光": ("source", "BluRay"), "藍光": ("source", "BluRay"),
    "蓝光原盘": ("source", "BluRay"), "原盘": ("source", "BluRay"),
    "原碟": ("source", "BluRay"),
    "超清": ("quality", "1080p"), "高清": ("quality", "720p"),
    "4K高清": ("quality", "2160p"), "4K HDR": ("quality", "2160p"),
    "HD国语": ("quality", "720p"),
    "国语": ("language", "Mandarin"), "粤语": ("language", "Cantonese"),
    "国英双语": ("language", "Mandarin"), "国粤双语": ("language", "Mandarin"),
    "双语": ("language", "Dual"), "英语": ("language", "English"),
    "中字": ("subtitle", "ChsSub"), "繁字": ("subtitle", "ChtSub"),
    "简繁字幕": ("subtitle", "ChsCht"), "内嵌字幕": ("subtitle", "Hardcoded"),
    "外挂字幕": ("subtitle", "Softsub"), "内封字幕": ("subtitle", "Embedded"),
    "HDR10": ("hdr", "HDR10"), "HDR10+": ("hdr", "HDR10+"),
}

# Chinese season/episode: 第N季 / 第N集 / 第N话 / 第N期 / EP12 / 12集
RX_CN_SEASON = re.compile(r"第\s*(\d{1,3})\s*[季部]")
# (?<!\d): "2024集" is not episode 24
RX_CN_EPISODE = re.compile(r"(?:第\s*)?(?<!\d)(\d{1,3})\s*(?:集|话|話|期|话)")
RX_CN_EPISODE_RANGE = re.compile(r"(\d{1,3})\s*[-—~]\s*(\d{1,3})\s*集")

# Arabic language/release tags
RX_AR_LANG = re.compile(r"(العربية|مدبلج|مترجم|دبلجة|ترجمة)")
# Persian/Farsi
RX_FA_LANG = re.compile(r"(فارسی|دوبله|زیرنویس|دانلود)")
# Korean
RX_KR_LANG = re.compile(r"(한국어|자막|더빙)")
# Japanese
RX_JP_LANG = re.compile(r"(日本語|字幕|吹替|生放送|RAW)\b", re.IGNORECASE)
# Russian
RX_RU_LANG = re.compile(r"(Русский|Дубляж|субтитры)", re.IGNORECASE)
# Spanish / Brazilian Portuguese variants
RX_ES_PT_LANG = re.compile(r"\b(Castellano|Latino|Español|Dublado|Legendado|Brazilian|BR-PT|ES-LA|ES-ES)\b", re.IGNORECASE)
# Turkish
RX_TR_LANG = re.compile(r"\b(Türkçe|TR-DUB|TR-SUB)\b", re.IGNORECASE)

# Extended release groups by region (boosts group detection beyond Latin-letter-only RX_GROUP)
# CN: CMCT, WiKi, FRDS, HDS, HDChina, TLF, HDH, NTb, MTeam, Yuanma, Mp4Ba, DBTV
# AR: KILLERS, EgyDead, ArabSeed, FaridT
# IN: TG, RZRBX, SaiKrishna, GalaxyRG, ESub
# TR: SAMETMAC, TRG
# Anime: SubsPlease, Erai-raws, Judas, ASW
RX_REGIONAL_GROUP = re.compile(
    r"[-\.\[]"
    r"(CMCT|WiKi|FRDS|HDS|HDChina|TLF|HDH|NTb|MTeam|Yuanma|Mp4Ba|DBTV|"
    r"KILLERS|EgyDead|ArabSeed|FaridT|"
    r"TG|RZRBX|SaiKrishna|GalaxyRG|ESub|"
    r"SAMETMAC|TRG|"
    r"SubsPlease|Erai-raws|Judas|ASW|HorribleSubs|Anime[-_. ]?Time)"
    r"\b",
    re.IGNORECASE,
)
RX_GROUP = re.compile(r"[-\.]([A-Z0-9][A-Z0-9._-]{2,15})$")
RX_AT_HANDLE = re.compile(r"[@\[\(]\w[\w_]{2,30}[\]\)]?", re.IGNORECASE)
RX_BRACKETS = re.compile(r"\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}")
RX_DOTSPACE = re.compile(r"[._]+")
RX_WS = re.compile(r"\s+")


_RES_NORM = {"4k": "2160p", "uhd": "2160p", "fhd": "1080p", "hd": "720p"}


def quality_from_dims(width, height):
    """Resolution label from the real frame size Telegram reports for a video.
    Width decides (a 1920x804 scope film is 1080p); height is the fallback
    for portrait or unusual frames."""
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    if w < h:                      # portrait: compare the long side
        w, h = h, w
    if w >= 3800 or h >= 2000:
        return "2160p"
    if w >= 2500 or h >= 1400:
        return "1440p"
    if w >= 1900 or h >= 1000:
        return "1080p"
    if w >= 1270 or h >= 700:
        return "720p"
    if h >= 560:
        return "576p"
    if h >= 460:
        return "480p"
    return "360p"


CLIP_SECONDS = 5 * 60          # shorter than this is never a release
SHORT_SECONDS = 20 * 60        # under this, a bare name is not enough
FEATURE_SECONDS = 60 * 60      # feature length


def decide_release(parsed: dict, *, file_size=None, duration=None, width=None,
                   height=None, min_score: float = 0.30,
                   min_movie_bytes: int = 100 * 1024 * 1024,
                   min_tv_bytes: int = 30 * 1024 * 1024) -> dict:
    """Should this file become a release, and as what?

    The file name alone cannot tell a film from a five-minute clip: any name
    with three CJK characters used to clear the bar. When Telegram reports the
    running time and frame size (it does for every message sent as a video),
    use them:
      - under 5 minutes: a clip, never a release;
      - under 20 minutes with nothing but a name (no year, season, episode or
        quality in it): not enough, score - 0.15;
      - 20 minutes or more: + 0.05; an hour or more: + 0.15, and a titled file
        of unknown type is a movie;
      - the real resolution fills in a quality the name did not state.
    With no duration (documents, old rows) the name-only score stands.

    Returns {"create": bool, "reason": str, "score": float, "type": str,
             "quality": str | None}.
    """
    score = float(parsed.get("score") or 0.0)
    type_ = parsed.get("type") or "unknown"
    quality = parsed.get("quality") or quality_from_dims(width, height)
    has_meta = (parsed.get("year") is not None or parsed.get("season") is not None
                or parsed.get("episode") is not None or bool(parsed.get("quality")))
    out = {"create": False, "reason": "", "score": score, "type": type_, "quality": quality}
    if duration is not None:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = None
    if duration is not None:
        if duration < CLIP_SECONDS:
            out["reason"] = "clip"
            return out
        if duration < SHORT_SECONDS and not has_meta:
            score -= 0.15
        elif duration >= FEATURE_SECONDS:
            score += 0.15
            if type_ == "unknown" and parsed.get("has_title"):
                type_ = "movie"
        elif duration >= SHORT_SECONDS:
            score += 0.05
        if not parsed.get("quality") and quality and duration >= SHORT_SECONDS:
            score += 0.05
    score = round(min(max(score, 0.0), 1.0), 2)
    out.update(score=score, type=type_)
    if score < min_score:
        out["reason"] = "score"
        return out
    if file_size:
        if type_ == "movie" and file_size < min_movie_bytes:
            out["reason"] = "too small for a movie"
            return out
        if type_ == "tv" and file_size < min_tv_bytes:
            out["reason"] = "too small for an episode"
            return out
    out["create"] = True
    return out


# ── names that say nothing, and where the title is then ─────────────────
# Many channels post a film as a poster with a caption ("名称: 消失的她 (HD国语)
# ... 年份: 2023") followed by the file itself, named by an uploader tool:
# 01_0.mp4, 02_0.mp4, 720P.mp4, IMG_4857.MP4. The name is useless; the title
# is in the caption of the file or of the poster right before it.
RX_GENERIC_STEM = re.compile(
    r"^(?:[\d\s_().\-\u6708\u65e5\u5e74]+"
    r"|(?:IMG|VID|MOV|DSC|PXL|MVI|video|trim|document|file)[\s_\-]*[\d_\-. ()]*"
    r"|(?:HD|FHD|UHD|4K|\d{3,4}[pP])?[\s_.\-|\uff5c/]*"
    r"(?:(?:\u56fd\u8bed|\u7ca4\u8bed|\u4e2d\u5b57|\u56fd\u7ca4|\u53cc\u8bed|\u82f1\u8bed|\u65e5\u8bed|\u97e9\u8bed)[\s_.\-|\uff5c/]*)*"
    r")$", re.IGNORECASE)
_RX_ANY_EXT = re.compile(r"\.(?=[0-9]*[A-Za-z])[A-Za-z0-9]{2,4}$")
RX_PART_STEM = re.compile(r"^0*(\d{1,2})(?:_0)?$")
_VIDEO_EXT = re.compile(
    r"\.(mkv|mp4|avi|m4v|mov|webm|flv|wmv|mpg|mpeg|3gp|ts|m2ts|iso|rmvb|rm)$", re.IGNORECASE)


def is_video_file(file_name, mime_type=None, media_type=None) -> bool:
    """Only video can be a film or an episode: not a PDF, a zip or a song
    that happens to have a year in its name."""
    if media_type == "video":
        return True
    if mime_type and str(mime_type).lower().startswith("video/"):
        return True
    return bool(file_name and _VIDEO_EXT.search(file_name))


def is_generic_name(name) -> bool:
    """True for a file name that carries no title: empty, digits, a camera
    counter, or just a quality/language tag."""
    if not name:
        return True
    stem = _RX_ANY_EXT.sub("", name).strip()
    return not stem or bool(RX_GENERIC_STEM.match(stem))


def part_number(name):
    """01_0.mp4 -> 1, 08.mp4 -> 8; None when the name is not a bare counter."""
    if not name:
        return None
    m = RX_PART_STEM.match(_RX_ANY_EXT.sub("", name).strip())
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 99 else None


RX_CAP_LABEL = re.compile(
    r"^[^\w\n]{0,4}(?:\u540d\u79f0|\u7247\u540d|\u5267\u540d|\u5f71\u7247\u540d\u79f0|\u7535\u5f71\u540d|\u4e2d\u6587\u540d|\u8bd1\u540d"
    r"|Title|Name|Movie|Film)\s*[:\uff1a]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE)
RX_CAP_YEAR = re.compile(
    r"(?:\u5e74\u4efd|\u5e74\u4ee3|\u4e0a\u6620|Year|Released?)\s*[:\uff1a]?\s*((?:19|20)\d{2})(?!\d)",
    re.IGNORECASE)
# "帝陵·西汉帝陵 (2015)   第8集"
RX_CAP_TITLE_YEAR = re.compile(
    r"^\s*([^\n#@]{2,60}?)\s*[\uff08(]((?:19|20)\d{2})[)\uff09]"
    r"(?:\s*\u7b2c\s*(\d{1,3})\s*[\u96c6\u8bdd\u8a71\u671f])?")
RX_CAP_SERIES = re.compile(
    r"(\u8fde\u7eed\u5267|\u96fb\u8996\u5287|\u7535\u89c6\u5267|\u5267\u96c6|\u56fd\u4ea7\u5267|\u97e9\u5267|\u7f8e\u5267|\u65e5\u5267|\u6e2f\u5267|\u53f0\u5267|\u6cf0\u5267"
    r"|\u52a8\u6f2b|\u756a\u5267|\u7efc\u827a|\u5168\s*\d+\s*\u96c6|\u66f4\u65b0\u81f3|\u7b2c\s*\d+\s*\u96c6|TV Series|Season\s*\d)",
    re.IGNORECASE)
_RX_CAP_SEASON = re.compile(
    r"\u7b2c\s*([\d一\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3})\s*[\u5b63\u90e8]|\bSeason\s*(\d{1,2})\b",
    re.IGNORECASE)
_CN_NUM = {"一": 1, "\u4e8c": 2, "\u4e09": 3, "\u56db": 4, "\u4e94": 5, "\u516d": 6,
           "\u4e03": 7, "\u516b": 8, "\u4e5d": 9, "\u5341": 10}
_RX_TRAIL_TAG = re.compile(r"\s*[\uff08(\u3010\[]([^\uff09)\u3011\]]*)[\uff09)\u3011\]]\s*$")


def title_from_caption(caption):
    """The title a caption states, or None.

    Only an explicit statement counts: a labelled line ("名称: …", "Title: …")
    or a first line shaped "Title (2015)". Free text is not a title: captions
    are mostly descriptions, hashtags and adverts.

    Returns {"title", "year", "season", "episode", "series", "quality"}.
    """
    if not caption:
        return None
    cap = caption.replace("\r", "")
    year = episode = None
    tags = ""
    m = RX_CAP_LABEL.search(cap)
    if m:
        title = m.group(1)
    else:
        m = RX_CAP_TITLE_YEAR.match(cap)
        if not m:
            return None
        title, year = m.group(1), int(m.group(2))
        if m.group(3):
            episode = int(m.group(3))
    # trailing "(HD国语)", "(2023)", "【完结】"
    for _ in range(4):
        t = _RX_TRAIL_TAG.search(title)
        if not t:
            break
        inner = t.group(1).strip()
        if re.fullmatch(r"(?:19|20)\d{2}", inner):
            year = year or int(inner)
        else:
            tags += " " + inner
        title = title[:t.start()]
    title = re.split(r"\s+[/|\uff5c]\s+", title)[0]
    season = None
    if (sm := _RX_CAP_SEASON.search(title)):
        g = sm.group(1) or sm.group(2)
        season = int(g) if g.isdigit() else _CN_NUM.get(g)
        if season:
            title = title[:sm.start()] + " " + title[sm.end():]
    title = re.sub(r"[#@]\S+", " ", title)
    title = RX_WS.sub(" ", title).strip(" -_.:\uff1a\u300a\u300b\"'")
    if not (1 <= len(title) <= 80) or not any(ch.isalpha() for ch in title):
        return None
    if year is None and (y := RX_CAP_YEAR.search(cap)):
        year = int(y.group(1))
    if year is not None and year > time.gmtime().tm_year + 1:
        year = None
    quality = None
    if (q := RX_RES.search(tags)):
        v = q.group(1).lower()
        quality = _RES_NORM.get(v, v)
    return {"title": title, "year": year, "season": season, "episode": episode,
            "series": bool(season or episode is not None or RX_CAP_SERIES.search(cap)),
            "quality": quality}


def with_context(parsed: dict, ctx: dict, part=None, parts_total: int = 1) -> dict:
    """Parsed fields for a file whose title comes from a caption (its own or
    the poster's before it). `part` is the file's counter (01_0.mp4 -> 1);
    under a series caption it is the episode, otherwise - when the group has
    several files - the part of a film posted in pieces."""
    out = {k: v for k, v in parsed.items()
           if k in ("quality", "source", "codec", "audio", "hdr", "language", "subtitle")}
    out["title"] = ctx["title"]
    out["has_title"] = True
    out["from_context"] = True
    if ctx.get("year"):
        out["year"] = ctx["year"]
    if not out.get("quality") and ctx.get("quality"):
        out["quality"] = ctx["quality"]
    episode = ctx.get("episode")
    if episode is None and ctx.get("series") and part is not None:
        episode = part
    if ctx.get("series"):
        out.update(type="tv", season=ctx.get("season") or 1)
        if episode is not None:
            out["episode"] = episode
    else:
        out["type"] = "movie"
        if part is not None and parts_total > 1:
            out["part"] = part
    # a title someone wrote out on purpose is worth more than a bare name
    s = 0.40
    if out.get("year"):
        s += 0.20
    if out["type"] == "tv":
        s += 0.30
    if out.get("quality"):
        s += 0.15
    out["score"] = round(min(s, 1.0), 2)
    return out


# Streaming-site rips carry the site's slogan after the title:
#   隐秘的生活_高清_欧乐影院_欧乐影院面向海外华人的在线视频媒体平台…
#   本心-免费在线观看-爱壹帆国际版
RX_SITE_TAIL = re.compile(
    r"[\s_\-.]*(?:(?:\u9ad8\u6e05|\u8d85\u6e05|\u84dd\u5149)[\s_\-.]*)?"
    r"(?:\u514d\u8d39)?(?:\u5728\u7ebf\u89c2\u770b|\u6b27\u4e50\u5f71\u9662|\u7231\u58f9\u5e06).*$")


def parse_filename(name: str) -> dict:
    """Parse a release-style filename into structured fields.

    Returns dict with possibly: title, year, season, episode, episode_end,
    type (movie|tv|unknown), quality, source, codec, audio, hdr, language,
    group, score (0.0..1.0).
    """
    if not name:
        return {"score": 0.0, "type": "unknown"}

    base = RX_EXT.sub("", name)
    base = RX_SITE_TAIL.sub("", base) or base
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

    # Year. Not every 4-digit number is one:
    #  - dates and timestamps ("2024-08-18 21", "20240818") are masked out;
    #  - nothing later than next year ("2046", "2077", "2048.cc");
    #  - when several remain, the release year is the LAST one before the
    #    quality tags ("Blade.Runner.2049.2017.1080p" -> 2017, "2012.2009" ->
    #    2009), unless that would leave no title at all.
    _masked = RX_DATE.sub(lambda m_: " " * len(m_.group(0)), work)
    _max_year = time.gmtime().tm_year + 1
    _cands = [m_ for m_ in RX_YEAR.finditer(_masked) if int(m_.group(1)) <= _max_year]
    _tag = min((m_.start() for rx in (RX_RES, RX_SOURCE, RX_CODEC)
                if (m_ := rx.search(_masked))), default=len(_masked))
    _before = [m_ for m_ in _cands if m_.start() < _tag] or _cands
    _year_m = None
    if _before:
        _year_m = _before[-1]
        if not re.search(r"[^\W\d_]", _masked[:_year_m.start()]) and len(_before) == 1:
            # the only "year" is the first thing in the name and nothing
            # title-like precedes it: "2012.1080p.mkv" - keep it as the title,
            # not as a year.
            _year_m = None if _year_m.start() == 0 else _year_m
    if _year_m and is_generic_name(name):
        _year_m = None          # IMG_2024.jpg, VID_2019_0001.mp4: a counter
    if _year_m:
        out["year"] = int(_year_m.group(1))
    _year_pos = _year_m.start() if _year_m else None

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
    # ambiguous short tags: only in the tag part of the name
    _anchor = _year_pos if _year_pos is not None else (_tag if _tag < len(_masked) else None)
    if _anchor is not None and _anchor < len(work):
        _tail = work[_anchor:]
        if (m := RX_SOURCE_WEAK.search(_tail)):
            out.setdefault("source", m.group(1))
            work = work[:_anchor] + RX_SOURCE_WEAK.sub(" ", _tail)

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

    # === Regional language extractors (boost language detection) ===
    if (m := RX_AR_LANG.search(work)):
        out["language"] = out.get("language") or "Arabic"
    if (m := RX_FA_LANG.search(work)):
        out["language"] = out.get("language") or "Persian"
    if (m := RX_KR_LANG.search(work)):
        out["language"] = out.get("language") or "Korean"
    if (m := RX_JP_LANG.search(work)):
        out["language"] = out.get("language") or "Japanese"
    if (m := RX_RU_LANG.search(work)):
        out["language"] = out.get("language") or "Russian"
    if (m := RX_ES_PT_LANG.search(work)):
        out["language"] = out.get("language") or m.group(1).title()
    if (m := RX_TR_LANG.search(work)):
        out["language"] = out.get("language") or "Turkish"
    # Strip them so they don't pollute the title
    work = RX_AR_LANG.sub(" ", work)
    work = RX_FA_LANG.sub(" ", work)
    work = RX_KR_LANG.sub(" ", work)
    work = RX_JP_LANG.sub(" ", work)
    work = RX_RU_LANG.sub(" ", work)
    work = RX_ES_PT_LANG.sub(" ", work)
    work = RX_TR_LANG.sub(" ", work)

    # === CN quality/source/audio/subtitle extraction ===
    for m_ in RX_CN_QUALITY.finditer(work):
        kw = m_.group(1)
        if kw in _CN_QUALITY_MAP:
            field, value = _CN_QUALITY_MAP[kw]
            out.setdefault(field, value)
    work = RX_CN_QUALITY.sub(" ", work)

    # === CN season/episode (only if not already set by English regex) ===
    if "season" not in out:
        if (m_ := RX_CN_SEASON.search(work)):
            out["season"] = int(m_.group(1))
            out["type"] = "tv"
            work = work[:m_.start()] + " " + work[m_.end():]
    if "episode" not in out:
        if (m_ := RX_CN_EPISODE_RANGE.search(work)):
            out["episode"] = int(m_.group(1))
            out["episode_end"] = int(m_.group(2))
            out["type"] = "tv"
            work = work[:m_.start()] + " " + work[m_.end():]
        elif (m_ := RX_CN_EPISODE.search(work)):
            ep_val = int(m_.group(1))
            # Avoid catching years like "2024集" misfire — require small num
            if ep_val <= 999:
                out["episode"] = ep_val
                out["type"] = "tv"
                work = work[:m_.start()] + " " + work[m_.end():]

    # === Regional release groups (extend Latin-letter RX_GROUP coverage) ===
    if "group" not in out:
        if (m_ := RX_REGIONAL_GROUP.search(work)):
            out["group"] = m_.group(1)
            work = work[:m_.start()]

    if (m := RX_GROUP.search(work)):
        # Heuristic: group must be at very end and not a common false-positive
        cand = m.group(1)
        if cand.upper() not in {"MP3", "MP4", "MKV", "PART"}:
            out["group"] = cand
            work = work[: m.start()]

    # Strip @channel handles and bracketed annotations.
    work = RX_AT_HANDLE.sub(" ", work)
    work = RX_BRACKETS.sub(" ", work)
    work = RX_CJK_BRACKETS.sub(" ", work)

    # Cut at year if present (title is everything before the year). The LAST
    # occurrence: an earlier one belongs to the title ("Blade Runner 2049").
    if "year" in out:
        ystr = str(out["year"])
        i = work.rfind(ystr)
        if i > 0:
            work = work[:i]
    # dates are not part of a title either
    work = RX_DATE.sub(" ", work)

    # Normalize separators → spaces.
    work = RX_DOTSPACE.sub(" ", work)
    work = RX_WS.sub(" ", work).strip(" -_.(" + "（")
    # an uploader's counter in front of a CJK title: "1-战狼"
    work = re.sub(r"^\d{1,2}\s*[-_.]\s*(?=[一-鿿])", "", work)

    title = work
    if not title:
        title = RX_DOTSPACE.sub(" ", base[:80]).strip()

    out["title"] = title

    # A title needs letters. "2024 08 18 21", "001", "1080p" are not titles.
    # (resolution / codec words do not count: the fallback title of a
    # tag-only name is the name itself)
    _probe = RX_CODEC.sub(" ", RX_RES.sub(" ", title))
    _letters = sum(1 for ch in _probe if ch.isalpha())
    out["has_title"] = _letters >= 2

    # Decide type
    if "type" not in out:
        if "year" in out and out["has_title"]:
            out["type"] = "movie"
        else:
            out["type"] = "unknown"

    # Score (0.0–1.0)
    s = 0.0
    _t = out.get("title") or ""
    if _t and out.get("has_title"):
        s += 0.20
        # Substantial real title is itself a strong release signal even without
        # year/episode/quality metadata — common for CJK doc/movie files whose
        # title lives in the file_name (e.g. "侯季然《書店裡的影像詩》", "我是无名女").
        # A bare ≥3-CJK-char title or ≥2 real (non-numeric) words clears 0.30;
        # generic stubs like "02", "8.mp4" stay at 0.20 and are still rejected.
        # CJK-only boost: a ≥3-CJK-char title is almost always a real
        # movie/doc name. Latin/Cyrillic multi-word titles are NOT boosted —
        # they'd false-match news headlines ("Москвач Новости") and real
        # latin releases usually carry year/quality metadata that already scores.
        _cjk = len(re.findall(r"[一-鿿぀-ヿ가-힯]", _t))
        if _cjk >= 3:
            s += 0.15
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
    if out.get("subtitle"):
        s += 0.03
    if out.get("language"):
        s += 0.02
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
    if parsed.get("part"):
        parts.append(f"Part{int(parsed['part']):02d}")
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
        # CN
        "霸王别姬.1993.4K.HDR.x265-CMCT.mkv",
        "【4K高清】流浪地球2.2023.国语中字-FRDS.mkv",
        "甄嬛传.第3季.第15集.1080p.WEB-DL.x264-HDH.mkv",
        "[BluRay]肖申克的救赎.1994.蓝光原盘.内嵌字幕-WiKi.mkv",
        # AR
        "Loki.S02E03.1080p.WEB-DL.مترجم.x264-EgyDead.mkv",
        # FA
        "Avatar.2022.1080p.BluRay.دوبله.فارسی.x265.mkv",
        # IN
        "[Hindi+Tamil+Telugu] RRR.2022.1080p.BluRay.DD5.1.x264.mkv",
        "Pathaan.2023.1080p.WEB-DL.HQ.Hindi.x264-RZRBX.mkv",
        # JP/anime
        "[SubsPlease] Frieren - 28 (1080p).mkv",
        "進撃の巨人.S04E28.1080p.WEB.RAW.mkv",
        # KR
        "오징어게임.S01E01.1080p.NF.WEB-DL.한국어.자막.x264.mkv",
        # ES/PT
        "Casa.de.Papel.S05E10.1080p.Latino.Castellano.mkv",
        # TR
        "Diriliş.Ertuğrul.S05E150.1080p.WEB-DL.Türkçe.x264-SAMETMAC.mkv",
        # English baseline
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
