"""What the Dashboard says about updates, for every state an install can be in.

    python test_update.py

Needs no database and no network. Third-party modules the api imports but
this logic never touches are stood in for when they are not installed.
"""
import os
import sys
import types


class _Anything(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Anything(name)

    def __call__(self, *a, **k):
        return _Anything("call")


try:
    import pyrogram  # noqa: F401
    import qrcode    # noqa: F401
except Exception:
    # the sign-in module needs Telegram libraries; none of this logic does
    sys.modules["login"] = _Anything("login")
    for mod in ("pyrogram", "qrcode", "tgcrypto"):
        sys.modules.setdefault(mod, _Anything(mod))

os.environ.setdefault("DB_DSN", "postgresql://unused")
import main  # noqa: E402

n = 0


def ok(cond, what):
    global n
    n += 1
    if not cond:
        sys.exit(f"FAIL: {what}")


H = 3600
NOW = 2_000_000_000.0
main.TGARR_VERSION = "0.5.7"
main.UPDATE_CHECK_ENABLED = True
main._PINNED_TAG = ""
S = main._update_state

# version comparison is numeric, not alphabetical
ok(main._ver_tuple("0.10.0") > main._ver_tuple("0.9.9"), "0.10.0 is newer than 0.9.9")
ok(main._ver_tuple("1.0.0") > main._ver_tuple("0.99.99"), "1.0.0 is newer than 0.99.99")
for bad in ("", "latest", "0.5", "0.5.7-rc1", "0.5.7; rm -rf /", "<b>1.2.3</b>", "1.2.3.4", "v1.2.3"):
    ok(main._ver_tuple(bad) == (), f"not a version: {bad!r}")

# nothing known yet, same version, or the registry is behind us (a dev build)
ok(S({}, NOW)["kind"] == "current", "nothing known yet")
ok(S({"update_latest": "0.5.7"}, NOW)["kind"] == "current", "same version")
ok(S({"update_latest": "0.5.6"}, NOW)["kind"] == "current", "registry older than this build")
ok(S({"update_latest": "garbage"}, NOW)["kind"] == "current", "garbage from the registry is ignored")

# a newer version: quiet while the updater has had no time, a notice once it clearly failed
new = {"update_latest": "0.5.8", "update_seen": f"0.5.8|{int(NOW - 1 * H)}"}
ok(S(new, NOW)["kind"] == "pending", "one hour behind: the updater may still be on its way")
new["update_seen"] = f"0.5.8|{int(NOW - 4 * H)}"
st = S(new, NOW)
ok(st["kind"] == "stalled" and st["hours"] == 4 and st["latest"] == "0.5.8", "four hours behind: stalled")
new["update_seen"] = f"0.5.8|{int(NOW - 50 * H)}"
ok(S(new, NOW)["hours"] == 50, "hours keep counting")

# skipping versions: the clock starts again for each newer version learned of
ok(S({"update_latest": "0.8.0", "update_seen": f"0.5.8|{int(NOW - 90 * H)}"}, NOW)["kind"] == "pending",
   "a still newer version restarts the clock")
ok(S({"update_latest": "0.8.0", "update_seen": f"0.8.0|{int(NOW - 5 * H)}"}, NOW)["kind"] == "stalled",
   "…and stalls again if that one is not installed either")
ok(S({"update_latest": "0.5.8", "update_seen": "0.5.8|not-a-number"}, NOW)["kind"] == "pending",
   "a damaged timestamp is not a crash")

# hidden for this version only
stalled = {"update_latest": "0.5.8", "update_seen": f"0.5.8|{int(NOW - 9 * H)}"}
ok(S(dict(stalled, update_dismissed="0.5.8"), NOW)["kind"] == "dismissed", "hidden")
ok(S(dict(stalled, update_dismissed="0.5.7"), NOW)["kind"] == "stalled", "hiding an older version does not hide this one")

# a version pinned on purpose is not a broken updater
main._PINNED_TAG = "0.5.7"
st = S(stalled, NOW)
ok(st["kind"] == "pinned" and st["tag"] == "0.5.7", "pinned")
for tag in ("latest", "edge", ""):
    main._PINNED_TAG = tag
    ok(S(stalled, NOW)["kind"] == "stalled", f"TGARR_TAG={tag!r} follows releases, so it can stall")

# switched off: says nothing at all
main.UPDATE_CHECK_ENABLED = False
ok(S(stalled, NOW)["kind"] == "current", "switched off")

# disk warning levels: an absolute floor and a share, whichever is worse
GB = 1024 ** 3
L = main._disk_level
ok(L(159 * GB, 232 * GB) == "ok", "plenty free")
ok(L(19 * GB, 232 * GB) == "warn", "under 20 GB free is a warning even on a big disk")
ok(L(4 * GB, 232 * GB) == "bad", "under 5 GB free is red")
ok(L(60 * GB, 1000 * GB) == "warn", "under 10% free is a warning even with 60 GB left")
ok(L(25 * GB, 1000 * GB) == "bad", "under 3% free is red")
ok(L(30 * GB, 64 * GB) == "ok", "a small disk half empty is fine")
ok(L(0, 0) == "off", "unknown disk")
ok(main._fmt_uptime(12 * 86400 + 21 * 3600) == "12 d 21 h" and main._fmt_uptime(3 * 3600 + 120) == "3 h 2 min",
   "uptime wording")

# ---- other-language names of a series -----------------------------------
import metadata as _md  # noqa: E402

_names = _md.names_from_tmdb(
    {"tv_results": [{"id": 1, "name": "Cases Between Us", "original_name": "京城奇探"}]},
    {"results": [{"iso_3166_1": "CN", "title": "京城奇探 "}, {"iso_3166_1": "US", "title": "CBU"}, {"title": ""}]},
    {"translations": [{"iso_639_1": "ko", "data": {"name": "경성기탐"}}, {"iso_639_1": "fr", "data": {"name": ""}},
                      {"iso_639_1": "en", "data": {"name": "Cases Between Us"}}]})
ok(_names == {"Cases Between Us", "京城奇探", "CBU", "경성기탐"}, "original, alternative and translated names, trimmed, no blanks")
ok(_md.names_from_tmdb({}, {}, {}) == set(), "nothing found: no names")
_al = main._names_to_aliases(_names | {"1", "Ov", "x" * 90}, "Cases Between Us")
ok(_al == ["京城奇探", "경성기탐"],
   "kept: the names that differ from the title and are specific enough (not 'CBU', '1', 'Ov', or 90 characters)")
ok(main._names_to_aliases({"cases between us", "CASES BETWEEN US"}, "Cases Between Us") == [],
   "the title itself in another case is not an alias")

# ---- the season a release is offered under (Newznab TV search) ----------
_saved_aliases = main._series_aliases_cache
main._series_aliases_cache = [("舌尖上的中国第三季", "A Bite of China", 3, "tv"),
                              ("京城奇探", "Cases Between Us", None, "tv")]


def _rel(name, season, episode):
    return {"name": name, "category": "tv", "season": season, "episode": episode,
            "quality": "2160p", "movie_year": None, "file_dc": 5}


ok(main._offered_season(_rel("#京城奇探.2160p", None, 12)) == 1, "no season of its own: offered as season 1")
ok(main._rewrite_release_title_sync(_rel("#京城奇探.2160p", None, 12)).startswith("Cases.Between.Us.S01E12."),
   "and named that way on the way out")
ok(main._offered_season(_rel("舌尖上的中国第三季.第2集", None, 2)) == 3, "the alias names the season")
ok(main._offered_season(_rel("Some.Show.S04E01.1080p", 4, 1)) == 4, "its own season wins")
ok(main._offered_season(_rel("没有别名的剧.第5集", None, 5)) == 1, "no alias and no season: 1")
main._series_aliases_cache = _saved_aliases

print(f"ok - {n} checks")
