"""Parser regression tests. Run:  python crawler/test_parser.py

No test framework needed; exits non-zero on the first set of failures so CI
can gate on it. Every case here is a real file name (or a minimal version of
one) that the parser has gotten wrong at some point.
"""
import sys

from parser import parse_filename

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


def main() -> int:
    failures = []
    for name, expected in CASES:
        got = parse_filename(name)
        for field, want in expected.items():
            have = got.get(field)
            if have != want:
                failures.append(f"{name!r}: {field} = {have!r}, expected {want!r}")
    if failures:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(f"FAILED {len(failures)} check(s) in {len(CASES)} cases:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"parser: {len(CASES)} cases OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
