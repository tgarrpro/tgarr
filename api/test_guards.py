"""The guards added after the field report of 17-18 Nov 2026 (QA #26, #32-#44, #55-#59, #64). The pure helpers are
lifted out of main.py and run; the rest is read from the source, as test_library.py does for the crawler.
Run: python test_guards.py"""
import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


def lift(name: str, **env):
    """One top-level function of main.py, defined on its own."""
    tree = ast.parse(src)
    fn = next(x for x in tree.body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == name)
    ns = dict(env)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    return ns[name]


class _Log:
    def warning(self, *a):
        pass


# --- QA #32: a missing disk forgets nothing --------------------------------------------------------------
forget = lift("_lost_to_forget", log=_Log())
paths = [f"usenet/a/{i}.mkv" for i in range(30)]
ok(forget(paths, 100, set(paths), {"usenet": True}) == [], "a third of the paths missing at once: a mount problem, nothing forgotten")
ok(forget(["usenet/x.mkv"], 100, set(), {"usenet": True}) == [], "missing once is not enough")
ok(forget(["usenet/x.mkv"], 100, {"usenet/x.mkv"}, {"usenet": True}) == ["usenet/x.mkv"], "missing twice, disk there: forgotten")
ok(forget(["usenet/x.mkv"], 100, {"usenet/x.mkv"}, {"usenet": False}) == [], "its disk looks absent: kept")
ok(forget([], 100, set(), {}) == [], "nothing missing: nothing forgotten")
ok("really = _lost_to_forget(lost, len(rows), _LOST_LAST, tops_ok)" in src, "the sweep goes through the guard")

# --- QA #34: a channel's title is escaped on /system -------------------------------------------------------
ok("${esc((c.title || \"\").substring(0, 40))}" in src and "${(c.title || \"\").substring(0, 40)}" not in src,
   "the deep-backfill table escapes channel titles")

# --- QA #33 / #43 / #44: the PIN --------------------------------------------------------------------------
un = src[src.index('@app.post("/api/unlock")'):src.index('@app.get("/lock")')]
ok("_GUESSES.right(who)" in un and un.count("_GUESSES.right(who)") == 1, "only the owner's PIN resets the count")
ok("_GUESSES.undo(who)" in un, "a person's PIN takes back one guess")
ok(un.index("earned = _GUESSES.wrong(who)") < un.index("await request.json()"), "the guess is counted before anything is awaited")
ok("access.check_pin(" not in src, "no PIN check runs on the event loop")
kp = src[src.index('@app.post("/api/kids/pin")'):src.index('@app.post("/api/kids/profile")')]
ok("secret = access.new_secret()" in kp and "'access_secret'" in kp, "a changed or removed PIN ends the old cookies")
ok("That PIN belongs to one of the people" in kp, "the owner's PIN cannot be a person's")

# --- QA #64: a hard ban stays --------------------------------------------------------------------------------
ok("AND audience IS DISTINCT FROM 'blocked_csam'" in src, "the audience endpoint never undoes blocked_csam")

# --- QA #40 / #41 / #56: the download-client emulation -------------------------------------------------------
sab = src[src.index("async def sab_api("):src.index("async def _sab_queue(")]
ok('mode in ("queue", "history") and name == "delete"' in sab, "queue/history delete as the *arr sends it")
gr = src[src.index("async def _handle_grab("):src.index("# SABnzbd download-client emulation")]
ok("INSERT INTO downloads" not in gr, "fetching the NZB starts nothing")
add = src[src.index("async def _sab_addurl("):src.index("async def _sab_history_delete(")]
ok("SET sab_cat = COALESCE($2, sab_cat)" in add, "an item already being fetched takes the *arr's category")
ok("os.path.exists" in add, "a re-grab fetches again when the file is gone")

# --- QA #55 / #57: feeds ------------------------------------------------------------------------------------
ok('("ORDER BY " if rss else' in src and "r.posted_at DESC NULLS LAST, r.id DESC" in src, "RSS is newest first, with a tie-breaker")
ok('("LIMIT 2000" if by_season' in src and "rows = rows[max(0, offset):max(0, offset) + limit_val]" in src,
   "a season search pages after filtering")

# --- QA #59: a shared file stays --------------------------------------------------------------------------------
ok(src.count("lp not in shared") == 1 and "and not shared:" in src, "a file another row uses is not removed")

# --- QA #42 / #31: exports ---------------------------------------------------------------------------------------
ok('"failed" if st["ready"] == 0 and st["missed"] else "ready"' in src, "an export where nothing arrived says failed")
ok("AND local_path = '__failed__'" in src, "a file that failed is tried again for a new export")
ok('"photo/")' in src, "photo/ is a saved-file folder")

# --- QA #26: one header allowance -----------------------------------------------------------------------------
iw = src[src.index("async def _usenet_index_worker("):]
ok("cfg[\"per_day\"] // 2 - iused_n" in iw and "usenet_index_today" in iw, "the shared index has half the daily allowance at most")
ok('wins = [x for x in wins if x.get("grp") in own]' in iw, "only the operator's own groups are read for the shared index")

# --- review of 0.5.17 --------------------------------------------------------------------------------------------
ok(src.count('await _usenet_count(conn, "usenet_today", day, n)') == 1 and 'for key in ("usenet_today", "usenet_index_today")' in src,
   "both workers add to the header count in one statement")
ok("COALESCE(m.local_path, '') <> '__deleted__'" in src, "the export picker takes files that failed before")
ok("stalled.append(r[\"id\"])        # its file is gone" in src, "an export file missing from disk is fetched again")
ok("rss = not q and season is None and ep is None" in src, "RSS order for tvsearch/movie without a query too")

print(f"ok - {n} checks")
