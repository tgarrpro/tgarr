"""One-shot bulk-join script for demo / testing.

Joins a curated list of public channels with a sleep delay between calls to
avoid tripping Telegram's anti-spam (joining 100+ channels in a short window
is the main account-ban trigger).

Run:  docker compose run --rm crawler python join_demo.py
"""
import os
import time

from pyrogram import Client

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

CHANNELS = [
    # Legit / media-rich
    ("legit", "@LinuxFoundation"),
    ("legit", "@MotherboardVice"),
    ("legit", "@PCGamesN"),
    ("legit", "@TheVerge"),
    ("legit", "@nasa"),
    ("legit", "@TASS_news"),
    ("legit", "@bbcbreaking"),
    ("legit", "@reuterstech"),
    # Piracy-pattern guesses (most will fail/not exist, some may be ad-funnels)
    ("piracy", "@HDMoviesHub_Official"),
    ("piracy", "@hindi_movies_world"),
    ("piracy", "@bollywood_hd_movies"),
    ("piracy", "@tamil_movies_hd"),
    ("piracy", "@cinemaxx"),
    ("piracy", "@MovieZoneOfficial"),
    ("piracy", "@netflix_series_hd"),
    ("piracy", "@HollywoodMoviesHD"),
]

DELAY_SECONDS = 45  # gap between join_chat calls


def main():
    results = []
    with Client("tgarr", api_id=API_ID, api_hash=API_HASH, workdir="/app/session") as app:
        for i, (label, ch) in enumerate(CHANNELS, 1):
            try:
                chat = app.join_chat(ch)
                results.append((label, ch, "OK", chat.id, chat.title))
                print(f"[{i}/{len(CHANNELS)}] ✓ {label:7} {ch:30} id={chat.id} title={chat.title!r}")
            except Exception as e:
                results.append((label, ch, "FAIL", None, str(e)[:80]))
                print(f"[{i}/{len(CHANNELS)}] ✗ {label:7} {ch:30} {type(e).__name__}: {str(e)[:60]}")
            if i < len(CHANNELS):
                time.sleep(DELAY_SECONDS)

    print()
    print("=== summary ===")
    ok = sum(1 for r in results if r[2] == "OK")
    fail = len(results) - ok
    print(f"  joined: {ok}/{len(results)}  failed: {fail}/{len(results)}")
    print()
    for label, ch, status, chat_id, info in results:
        marker = "✓" if status == "OK" else "✗"
        print(f"  {marker} {label:7} {ch:30} {info}")


if __name__ == "__main__":
    main()
