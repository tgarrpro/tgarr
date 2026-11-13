"""Checks that the crawler calls the Telegram library the way the library
(Kurigram) actually works. They read the source; no network, no account.
Run: python test_library.py"""
import os
import re

src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), encoding="utf-8").read()
n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


# Kurigram's get_chat_history ignores offset_id (deprecated, silently replaced
# by max_id or 0). Paging with it re-read a channel's newest page for ever.
calls = re.findall(r"_hist\((?:[^()]|\([^()]*\))*\)", src)
ok(len(calls) >= 4, "the history calls were found")
ok(not any("offset_id" in c for c in calls), "no history call passes offset_id")
ok('"offset_id": cursor' not in src and "offset_id=cursor" not in src,
   "the deep backfill does not page with offset_id")
ok('{"max_id": cursor - 1}' in src, "the deep backfill pages with max_id (inclusive, so cursor - 1)")
# Client.run() takes no coroutine in Kurigram: the entry point must be asyncio.run
ok("asyncio.run(main())" in src and "app.run(main())" not in src, "started with asyncio.run")

print(f"ok - {n} checks")
