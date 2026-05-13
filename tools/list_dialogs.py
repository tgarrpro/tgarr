"""Active probe — list current TG dialogs of the configured account.

Uses messages.getDialogs (different rate-limit bucket than resolveUsername),
so safe to run during a resolveUsername FloodWait.

Must be run with crawler stopped (shared Pyrogram session).

Usage:
  docker compose run --rm crawler python /app/tools/list_dialogs.py
"""
import os
from pyrogram import Client

API_ID = int(os.environ['TG_API_ID'])
API_HASH = os.environ['TG_API_HASH']

with Client('tgarr', api_id=API_ID, api_hash=API_HASH, workdir='/app/session') as app:
    me = app.get_me()
    print(f'connected as @{me.username} (id={me.id})')
    print()
    print(f"{'chat_id':>16}  {'@username':30}  {'type':10}  {'members':>9}  title")
    print('-' * 100)
    n = 0
    for d in app.get_dialogs():
        c = d.chat
        n += 1
        u = c.username or '(priv)'
        m = getattr(c, 'members_count', None)
        m_str = str(m) if m is not None else '-'
        t = (c.title or '')[:50]
        print(f'{c.id:>16}  @{u[:29]:29}  {c.type.name[:10]:10}  {m_str:>9}  {t}')
    print()
    print(f'total dialogs: {n}')
