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

# Kurigram's Auth and Session take the server's address and port; Pyrogram's
# did not. The QR sign-in moves to the account's own data centre by hand.
login = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api", "login.py"), encoding="utf-8").read()
for name, text in (("crawler", src), ("sign-in", login)):
    for kind, least in (("Auth", 5), ("Session", 6)):
        for m in re.finditer(r"(?<![A-Za-z_.])" + kind + r"\(", text):
            depth, i = 1, m.end()
            while depth and i < len(text):
                depth += {"(": 1, ")": -1}.get(text[i], 0)
                i += 1
            args = [a for a in re.sub(r"\([^()]*\)", "", text[m.end():i - 1]).split(",") if a.strip()]
            ok(len(args) >= least, f"{name}: {kind}(...) is given an address and a port ({len(args)} arguments)")
ok("client.storage.server_address(opt.ip_address)" in login and "client.storage.port(opt.port)" in login,
   "the sign-in stores the new data centre's address with the session")

# The start-up backfill walks the account's dialogs. ingest_message keeps only
# messages of chats that have a row marked joined or followed, so the walk
# must make that row first - or it reads every chat and keeps nothing.
walk = src[src.index("async for dialog in _dialogs():"):]
walk = walk[:walk.index("await backfill_channel(chat_id, title, dialog.chat.username)")]
ok("INSERT INTO channels (tg_chat_id, title, is_joined)" in walk and "is_joined = TRUE" in walk,
   "the start-up backfill registers a dialog as joined before reading it")

# What is in a private chat is kept whole: the sweeps that clear public-channel
# junk (tiny photos, short clips, advert words, repeated pictures, old news)
# must each leave private chats alone.
sweep = src[src.index("async def decay_eviction_worker()"):src.index("DIALOG_LEAVE_DELAY_SEC = int(")]
deletes = sweep.count("DELETE FROM messages")
spared = (sweep.count("channel_id NOT IN (SELECT id FROM channels WHERE username IS NULL")
          + sweep.count("channel_id NOT IN (SELECT id FROM channels WHERE {PRIVATE_CHAT_SQL})")
          + sweep.count("AND NOT (c.username IS NULL AND COALESCE(c.members_count, 0) < 200)"))
ok(deletes >= 6 and spared == deletes, f"every sweep spares private chats ({spared} of {deletes})")
ok('PRIVATE_CHAT_SQL = "username IS NULL AND COALESCE(members_count, 0) < 200"' in src,
   "a private chat is a small chat with no public name")
ok("(True, None) if is_private else _quality_check(" in src, "no size floor or advert filter on a private chat's files")
ok('st["noise"] and not st["followed"] and not is_private' in src,
   "a chat judged noise is not indexed, unless it is followed or private")

print(f"ok - {n} checks")
