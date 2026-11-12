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

print(f"ok - {n} checks")
