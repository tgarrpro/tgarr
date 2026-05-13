"""Telegram login flow — QR code (preferred) + phone/SMS fallback.

Uses Pyrogram's raw auth.ExportLoginToken API for QR login. Same Pyrogram
Client instance is reused for the main crawler after auth succeeds.

State machine:
  unauthed --[POST /login/qr/start]--> qr_pending --[user scans]--> authed
  unauthed --[POST /login/sms/send]--> sms_sent --[POST /login/sms/verify]--> authed
                                                  --[2FA required]--> needs_2fa
"""
import asyncio
import base64
import io
import logging
import os
from typing import Optional

import qrcode
from pyrogram import Client
from pyrogram.errors import (
    PhoneCodeInvalid,
    PhoneCodeExpired,
    SessionPasswordNeeded,
    PasswordHashInvalid,
)
from pyrogram.raw.functions.auth import ExportLoginToken, ImportLoginToken
from pyrogram.raw.types.auth import (
    LoginToken,
    LoginTokenMigrateTo,
    LoginTokenSuccess,
)

log = logging.getLogger("tgarr.login")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_DIR = "/app/session"

# Singleton state — only one login attempt at a time.
class LoginState:
    def __init__(self):
        self.client: Optional[Client] = None
        self.qr_token: Optional[bytes] = None
        self.qr_expires: int = 0
        self.polling: bool = False
        self.status: str = "idle"       # idle|qr_pending|sms_sent|needs_2fa|success|error
        self.message: str = ""
        self.sms_phone: Optional[str] = None
        self.sms_phone_code_hash: Optional[str] = None
        self.user_info: Optional[dict] = None

    def reset(self):
        self.qr_token = None
        self.qr_expires = 0
        self.polling = False
        self.status = "idle"
        self.message = ""
        self.sms_phone = None
        self.sms_phone_code_hash = None
        self.user_info = None


state = LoginState()


def session_exists() -> bool:
    """Pyrogram saves session as <name>.session SQLite. Detect existing auth."""
    path = os.path.join(SESSION_DIR, "tgarr.session")
    return os.path.exists(path) and os.path.getsize(path) > 0


async def _ensure_client() -> Client:
    """Lazy-create + connect Pyrogram client (no auth required for connect)."""
    if state.client is None:
        state.client = Client(
            name="tgarr",
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=SESSION_DIR,
            no_updates=True,
        )
    if not state.client.is_connected:
        await state.client.connect()
    return state.client


async def _save_user_and_disconnect(user_obj):
    """After auth success: persist Pyrogram session + disconnect login client.
    The main app (crawler) will load the same session afterward.
    """
    if state.client and state.client.is_connected:
        try:
            await state.client.storage.user_id(user_obj.id)
            await state.client.storage.is_bot(False)
            await state.client.storage.save()
        except Exception as e:
            log.warning("storage.save during finalize: %s", e)
        try:
            await state.client.disconnect()
        except Exception:
            pass
    state.user_info = {
        "id": user_obj.id,
        "username": getattr(user_obj, "username", None),
        "first_name": getattr(user_obj, "first_name", None),
    }
    state.status = "success"
    state.message = f"signed in as @{state.user_info['username'] or state.user_info['first_name']}"
    log.info("[login] success: %s", state.message)


# ─────── QR LOGIN ───────────────────────────────────────────────────
async def qr_start() -> dict:
    """Start a QR login. Returns base64 PNG of QR + status."""
    if session_exists():
        return {"status": "already_authed",
                "message": "session already present; remove _data/session/ to re-login"}

    client = await _ensure_client()
    try:
        r = await client.invoke(ExportLoginToken(
            api_id=API_ID, api_hash=API_HASH, except_ids=[]))
    except Exception as e:
        state.status = "error"
        state.message = f"export token: {e}"
        return {"status": "error", "message": state.message}

    if isinstance(r, LoginToken):
        state.qr_token = r.token
        state.qr_expires = r.expires
        token_b64 = base64.urlsafe_b64encode(r.token).decode().rstrip("=")
        qr_url = f"tg://login?token={token_b64}"
        img = qrcode.make(qr_url, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_b64 = base64.b64encode(buf.getvalue()).decode()
        state.status = "qr_pending"
        state.message = "scan with Telegram app → Settings → Devices → Link Desktop Device"
        if not state.polling:
            state.polling = True
            asyncio.create_task(_qr_poll_loop())
        return {"status": "qr_pending",
                "qr_png_base64": png_b64,
                "qr_url": qr_url,
                "expires_at": r.expires,
                "message": state.message}
    if isinstance(r, LoginTokenSuccess):
        await _save_user_and_disconnect(r.authorization.user)
        return {"status": "success", "user": state.user_info, "message": state.message}
    return {"status": "error", "message": f"unexpected: {type(r).__name__}"}


async def _qr_poll_loop():
    """Poll ExportLoginToken until success/migrate/timeout. Runs in background."""
    client = state.client
    try:
        for _ in range(120):  # ~4 min
            await asyncio.sleep(2)
            try:
                r = await client.invoke(ExportLoginToken(
                    api_id=API_ID, api_hash=API_HASH, except_ids=[]))
            except Exception as e:
                log.warning("[login] qr poll: %s", e)
                continue
            if isinstance(r, LoginTokenSuccess):
                await _save_user_and_disconnect(r.authorization.user)
                return
            if isinstance(r, LoginTokenMigrateTo):
                # DC migration: switch DC + import token there
                try:
                    await client.session.stop()
                    await client.storage.dc_id(r.dc_id)
                    await client.session.start()
                    r2 = await client.invoke(ImportLoginToken(token=r.token))
                    if isinstance(r2, LoginTokenSuccess):
                        await _save_user_and_disconnect(r2.authorization.user)
                        return
                except Exception as e:
                    log.exception("[login] DC migration: %s", e)
                    state.status = "error"
                    state.message = f"DC migration failed: {e}"
                    return
            # else: still LoginToken — keep polling
    finally:
        state.polling = False
        if state.status == "qr_pending":
            state.status = "error"
            state.message = "QR code expired without scan"


def qr_status() -> dict:
    return {"status": state.status, "message": state.message,
            "user": state.user_info, "polling": state.polling,
            "expires_at": state.qr_expires}


# ─────── SMS LOGIN (fallback) ───────────────────────────────────────
async def sms_send(phone: str) -> dict:
    """Send confirmation code to the user's Telegram on their phone."""
    if session_exists():
        return {"status": "already_authed"}
    client = await _ensure_client()
    try:
        sent = await client.send_code(phone)
    except Exception as e:
        return {"status": "error", "message": f"send_code: {e}"}
    state.sms_phone = phone
    state.sms_phone_code_hash = sent.phone_code_hash
    state.status = "sms_sent"
    state.message = f"code sent to Telegram on {phone}"
    return {"status": "sms_sent", "message": state.message}


async def sms_verify(code: str, password: Optional[str] = None) -> dict:
    """Submit the SMS code (and 2FA password if needed)."""
    if not state.sms_phone or not state.sms_phone_code_hash:
        return {"status": "error", "message": "no SMS in progress; call /login/sms/send first"}
    client = state.client
    try:
        await client.sign_in(
            phone_number=state.sms_phone,
            phone_code_hash=state.sms_phone_code_hash,
            phone_code=code,
        )
    except SessionPasswordNeeded:
        if not password:
            state.status = "needs_2fa"
            state.message = "2FA password required"
            return {"status": "needs_2fa", "message": state.message}
        try:
            await client.check_password(password)
        except PasswordHashInvalid:
            return {"status": "error", "message": "wrong 2FA password"}
    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"sign_in: {e}"}

    me = await client.get_me()
    await _save_user_and_disconnect(me)
    return {"status": "success", "user": state.user_info, "message": state.message}
