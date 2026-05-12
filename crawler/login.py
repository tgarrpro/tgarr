"""One-shot interactive Pyrogram login.

Run this once to seed the session file. Pyrogram prompts for the SMS code
which you type interactively — the code never appears in chat or scripts.

  docker compose run --rm crawler python login.py
"""
import os

from pyrogram import Client

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE = os.environ["TG_PHONE"]

app = Client(
    "tgarr",
    api_id=API_ID,
    api_hash=API_HASH,
    phone_number=PHONE,
    workdir="/app/session",
)

with app:
    me = app.get_me()
    print()
    print(f"✅ Logged in as {me.first_name} (id={me.id}, username=@{me.username or '-'})")
    print("Session saved to /app/session/tgarr.session")
    print("Future runs of `docker compose up -d` will reuse this session — no SMS needed.")
