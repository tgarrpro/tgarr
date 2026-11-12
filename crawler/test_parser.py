"""Parser regression tests. Run:  python crawler/test_parser.py

No test framework needed; exits non-zero on the first set of failures so CI
can gate on it. Every case here is a real file name (or a minimal version of
one) that the parser has gotten wrong at some point.
"""
import sys

from parser import decide_release, parse_filename, quality_from_dims

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
    if failures:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(f"FAILED {len(failures)} check(s) in {len(CASES) + len(DIMS) + len(VERDICTS)} cases:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"parser: {len(CASES)} name cases, {len(DIMS)} frame sizes, {len(VERDICTS)} release verdicts OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
