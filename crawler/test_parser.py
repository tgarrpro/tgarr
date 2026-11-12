"""Parser regression tests. Run:  python crawler/test_parser.py

No test framework needed; exits non-zero on the first set of failures so CI
can gate on it. Every case here is a real file name (or a minimal version of
one) that the parser has gotten wrong at some point.
"""
import sys

from parser import (decide_release, is_generic_name, is_video_file, parse_filename,
                    part_number, quality_from_dims, title_from_caption, to_release_name,
                    with_context)

# (file name, {field: expected}) — only the listed fields are checked.
CASES = [
    # ── plain releases ────────────────────────────────────────────────
    ("Scream.7.2026.1080p.WEB-DL.x264.mkv",
     {"title": "Scream 7", "year": 2026, "type": "movie", "quality": "1080p", "source": "WEBDL"}),
    ("Breaking.Bad.S01E02.720p.BluRay.x265.mkv",
     {"title": "Breaking Bad", "type": "tv", "season": 1, "episode": 2, "quality": "720p"}),
    ("The.Office.US.3x07.HDTV.avi", {"type": "tv", "season": 3, "episode": 7}),

    # ── the release year is the LAST one before the tags ──────────────
    ("Blade.Runner.2049.2017.2160p.UHD.BluRay.mkv",
     {"title": "Blade Runner 2049", "year": 2017, "type": "movie"}),
    ("2012.2009.1080p.BluRay.mkv", {"title": "2012", "year": 2009}),
    ("1917.2019.1080p.mkv", {"title": "1917", "year": 2019}),
    ("2001.A.Space.Odyssey.1968.1080p.mkv", {"title": "2001 A Space Odyssey", "year": 1968}),

    # ── dates and timestamps are not years, and such files are not movies ──
    ("2024-08-18 21.mp4", {"year": None, "type": "unknown", "has_title": False}),
    ("2025-07-15 15.mp4", {"year": None, "type": "unknown"}),
    ("20240818_213045.mp4", {"year": None, "type": "unknown"}),
    ("2020 05 16.mp4", {"year": None, "type": "unknown"}),
    ("IMG_2024.jpg", {"type": "unknown"}),
    ("VID_20231105_101500.mp4", {"year": None, "type": "unknown"}),
    ("电报搜.2022-11-28.mp4", {"year": None, "type": "unknown"}),
    ("18-08-2024 party.mp4", {"year": None}),

    # ── numbers that are not years ────────────────────────────────────
    ("2048.cc-001.mp4", {"year": None, "type": "unknown"}),
    ("Cyberpunk.2077.mp4", {"year": None}),
    ("2046.2004.1080p.mkv", {"title": "2046", "year": 2004}),

    # ── a title needs letters ─────────────────────────────────────────
    ("1080P.1080p.mp4", {"has_title": False, "type": "unknown"}),
    ("001.mp4", {"has_title": False, "type": "unknown"}),
    ("02.mkv", {"has_title": False}),

    # ── short source tags must not be eaten out of titles ─────────────
    ("The.Web.of.Lies.2019.1080p.mkv", {"title": "The Web of Lies", "year": 2019}),
    ("Cam.2018.720p.WEBRip.mkv", {"title": "Cam", "year": 2018, "source": "WEBRip"}),
    ("Movie.Name.2021.TS.x264.mkv", {"title": "Movie Name", "year": 2021, "source": "TS"}),
    ("Some.Film.2020.1080p.WEB.h264.mkv", {"title": "Some Film", "source": "WEB"}),

    # ── CJK ───────────────────────────────────────────────────────────
    ("活着.1994.1080p.mkv", {"title": "活着", "year": 1994, "type": "movie"}),
    ("福尔摩斯小姐2.2022.mp4", {"title": "福尔摩斯小姐2", "year": 2022, "type": "movie"}),
    ("舌尖上的中国 第3季 第5集.mkv", {"type": "tv", "season": 3, "episode": 5}),
    ("某剧 2024集.mkv", {"episode": None}),
    ("南方车站的聚会.mkv", {"title": "南方车站的聚会", "has_title": True}),
]


# (width, height) -> label. Real frame sizes reported by Telegram.
# More names the parser got wrong, found when every indexed file was re-judged.
CASES += [
    # a year glued to CJK text
    ("不夜城1998.mp4", {"title": "不夜城", "year": 1998, "type": "movie"}),
    # ...but a camera counter is not a year
    ("IMG_2024.jpg", {"year": None, "type": "unknown"}),
    # streaming-site slogans after the title
    ("杀手妻子的保镖_高清_欧乐影院_欧乐影院面向海外华人的在线视频媒体平台.mp4", {"title": "杀手妻子的保镖"}),
    ("本心-免费在线观看-爱壹帆国际版.mp4", {"title": "本心"}),
    ("杀戮轮回_立即播放_欧乐影院面向海外华人.mp4", {"title": "杀戮轮回"}),
    # site names in front of it - but never a real title
    ("阳光电影dygod.org.浴血狙击.2024.HD.1080P.mp4", {"title": "浴血狙击", "year": 2024, "quality": "1080p"}),
    ("[电影天堂www.dytt8.net]流浪地球2.2023.mkv", {"title": "流浪地球2", "year": 2023}),
    ("The.Net.1995.720p.BluRay.mkv", {"title": "The Net", "year": 1995}),
    # a resolution glued to a source tag
    ("记忆大师.Battle.of.Memories.2017.HD1080P.X264.AAC.mp4", {"year": 2017, "quality": "1080p", "codec": "x264"}),
    ("星际传奇.Pitch.Black.2000.BD720p.mkv", {"year": 2000, "quality": "720p"}),
    # uploader counters and book-title marks
    ("1-战狼（2015）【吴京】-1080P 高码率-AVC.mp4", {"title": "战狼", "year": 2015, "quality": "1080p"}),
    ("091.《古怪战舰》.1960年理查德墨菲导演.mp4", {"title": "古怪战舰", "year": 1960}),
    # a dated post is a talk or a news clip: no year, no film
    ("2024_11_27秘液、肉体、弑母、邪典……2024年最佳！一个现代肉体邪典，.mp4", {"year": None, "type": "unknown", "score": 0.2}),
    ("2025_07_03_17_00_00今年的九三阅兵，是震慑美国和全世界亲美派的好时机.mp4", {"year": None, "score": 0.2}),
    # a sentence is not a title; a title with one comma still is
    ("身价1个亿，人均1上师！中国巨富为什么这么爱供养上师？.mp4", {"score": 0.2}),
    ("你好，李焕英.2021.mkv", {"title": "你好，李焕英", "year": 2021, "type": "movie"}),
    # "2026" at the end is not a release group
    ("隐秘刺客.伊朗·1979.2026.mp4", {"group": None}),
]

# file name -> (carries no title, counter)
GENERIC = [
    ("01_0.mp4", True, 1), ("08.mp4", True, 8), ("720P.mp4", True, None),
    ("HD国语.mp4", True, None), ("IMG_4857.MP4", True, None), ("111.mp4", True, None),
    ("5月17日(17).mp4", True, None), ("20250722_020307.mp4", True, None), (None, True, None),
    ("Avatar.2022.1080p.mkv", False, None), ("美好的一年2006.mp4", False, None),
    ("2012.2009.1080p.mkv", False, None),
]

# caption -> what it states (None: it states no title)
CAPTIONS = [
    ("名称: 消失的她 (HD国语)\n别名: Lost in the Stars / 为单身汉设下的陷阱\n类型: #剧情片 #大陆 年份: 2023",
     {"title": "消失的她", "year": 2023, "series": False}),
    ("名称: 九门 (HD)\n演员: #王栎鑫\n年份: 2021 \n更新时间: 2022-10-19 22:35:56",
     {"title": "九门", "year": 2021}),
    ("帝陵·西汉帝陵 (2015)   第8集\n请关注群组https://t.me/x",
     {"title": "帝陵·西汉帝陵", "year": 2015, "episode": 8, "series": True}),
    ("🎬 片名：庆余年 第二季 (更新至20集)\n类型：国产剧", {"title": "庆余年", "season": 2, "series": True}),
    ("Title: The Bear (2022)\nSeason 3", {"title": "The Bear", "year": 2022, "series": True}),
    ("断卡风暴\n\n公安题材重点网络电影，全民反诈", None),
    ("#麻豆传媒 想念前男友的鸟", None),
    ("", None), (None, None),
]

# (file name, caption of the title, counter, numbered files under it) -> release name
CONTEXT = [
    ("01_0.mp4", "名称: 消失的她 (HD国语)\n年份: 2023", 1, 2, "消失的她.2023.Part01"),
    ("01_0.mp4", "名称: 消失的她 (HD国语)\n年份: 2023", 1, 1, "消失的她.2023"),
    ("720P.mp4", "名称: 消失的凶手 (HD国语)\n年份: 2015", None, 2, "消失的凶手.2015.720p"),
    ("08.mp4", "帝陵·西汉帝陵 (2015)   第8集", 8, 1, "帝陵·西汉帝陵.S01E08"),
    ("03.mp4", "片名：庆余年 第二季\n类型：国产剧", 3, 20, "庆余年.S02E03"),
    # anime: a film in three pieces, or a series - the number of files decides
    ("02_0.mp4", "名称: 新猪猪侠大电影 (HD国语)\n类型: #动漫 年份: 2021", 2, 3, "新猪猪侠大电影.2021.Part02"),
    ("07.mp4", "名称: 某动画\n类型: #动漫", 7, 12, "某动画.S01E07"),
]

# (file name, mime, media type) -> can it be a film or an episode at all
VIDEO = [
    ("a.mkv", None, "document", True), ("a.mp4", "video/mp4", "video", True),
    (None, None, "video", True), ("a.bin", "video/x-matroska", "document", True),
    ("认知觉醒.2020.pdf", "application/pdf", "document", False),
    ("book.epub", None, "document", False), ("tool.2024.exe", None, "document", False),
    ("song.2019.flac", "audio/flac", "audio", False),
]

DIMS = [
    ((1920, 804), "1080p"), ((1920, 1080), "1080p"), ((1280, 720), "720p"),
    ((3840, 1600), "2160p"), ((1280, 536), "720p"), ((854, 480), "480p"),
    ((720, 576), "576p"), ((1080, 1920), "1080p"), ((640, 360), "360p"),
    ((0, 0), None), ((None, None), None),
]

MB = 1024 * 1024
# (file name, kwargs for decide_release, {field: expected})
VERDICTS = [
    # a 3-CJK-character name used to be enough on its own
    ("某个短片段.mp4", dict(file_size=40 * MB, duration=150), {"create": False, "reason": "clip"}),
    ("某个短片段.mp4", dict(file_size=300 * MB, duration=14 * 60), {"create": False}),
    ("某个短片段.mp4", dict(file_size=600 * MB, duration=35 * 60), {"create": True}),
    # no duration known (document, old row): the name-only score stands
    ("南方车站的聚会.mkv", dict(file_size=2000 * MB), {"create": True}),
    # feature length + a real title => a movie even without a year; real resolution fills quality
    ("赌神.mp4", dict(file_size=810 * MB, duration=125 * 60, width=1280, height=720),
     {"create": True, "type": "movie", "quality": "720p"}),
    # a normal release is unaffected, and a short episode is still an episode
    ("Scream.7.2026.1080p.WEB-DL.mkv", dict(file_size=2300 * MB, duration=114 * 60, width=1920, height=800),
     {"create": True, "type": "movie", "quality": "1080p"}),
    ("Some.Show.S02E05.720p.mkv", dict(file_size=180 * MB, duration=11 * 60), {"create": True, "type": "tv"}),
    # trailers / samples
    ("Scream.7.2026.1080p.TRAILER.mkv", dict(file_size=60 * MB, duration=140), {"create": False, "reason": "clip"}),
    ("Scream.7.2026.1080p.mkv", dict(file_size=40 * MB), {"create": False}),
    # names with no title never qualify, however long the video
    ("2024-08-18 21.mp4", dict(file_size=900 * MB, duration=90 * 60), {"create": False}),
    ("01_0.mp4", dict(file_size=900 * MB, duration=40 * 60), {"create": False}),
]


def main() -> int:
    failures = []
    for dims, want in DIMS:
        have = quality_from_dims(*dims)
        if have != want:
            failures.append(f"quality_from_dims{dims} = {have!r}, expected {want!r}")
    for name, kw, expected in VERDICTS:
        got = decide_release(parse_filename(name), **kw)
        for field, want in expected.items():
            if got.get(field) != want:
                failures.append(f"decide {name!r} {kw}: {field} = {got.get(field)!r}, "
                                f"expected {want!r} (verdict {got})")
    for name, expected in CASES:
        got = parse_filename(name)
        for field, want in expected.items():
            have = got.get(field)
            if have != want:
                failures.append(f"{name!r}: {field} = {have!r}, expected {want!r}")
    for name, generic, part in GENERIC:
        if is_generic_name(name) != generic:
            failures.append(f"is_generic_name({name!r}) = {not generic}, expected {generic}")
        if part_number(name) != part:
            failures.append(f"part_number({name!r}) = {part_number(name)!r}, expected {part!r}")
    for cap, expected in CAPTIONS:
        got = title_from_caption(cap)
        if expected is None:
            if got is not None:
                failures.append(f"caption {cap!r}: {got}, expected no title")
            continue
        for field, want in expected.items():
            if (got or {}).get(field) != want:
                failures.append(f"caption {cap!r}: {field} = {(got or {}).get(field)!r}, "
                                f"expected {want!r}")
    for name, cap, part, total, want in CONTEXT:
        have = to_release_name(with_context(parse_filename(name), title_from_caption(cap),
                                            part, total))
        if have != want:
            failures.append(f"context {name!r} under {cap!r}: {have!r}, expected {want!r}")
    for name, mime, mt, want in VIDEO:
        if is_video_file(name, mime, mt) != want:
            failures.append(f"is_video_file({name!r}, {mime!r}, {mt!r}) = {not want}")
    if failures:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(f"FAILED {len(failures)} check(s) in {len(CASES) + len(DIMS) + len(VERDICTS)} cases:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"parser: {len(CASES)} name cases, {len(DIMS)} frame sizes, {len(VERDICTS)} release "
          f"verdicts, {len(GENERIC)} generic names, {len(CAPTIONS)} captions, "
          f"{len(CONTEXT)} titles from context, {len(VIDEO)} file kinds OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
