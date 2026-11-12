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
import json
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
REVOKED_MARKER = os.path.join(SESSION_DIR, ".revoked")
# user_info lives in-memory (lost on container restart) but the session file
# persists on the mounted volume. Persist the self-user beside it so the
# sidebar doesn't fall back to @anonymous after a watchtower restart.
USER_INFO_FILE = os.path.join(SESSION_DIR, "user_info.json")

# The crawler owns the LIVE session file (tgarr.session). The login flow never
# opens it: every attempt runs against a scratch session and the result is
# moved into place only once Telegram has actually authorized a user. A QR that
# is shown but never scanned, a DC migration, or an aborted SMS attempt can
# therefore no longer rewrite (or half-initialize) the session the crawler is
# running on.
LIVE_SESSION = os.path.join(SESSION_DIR, "tgarr.session")
LOGIN_NAME = "tgarr-login"
LOGIN_SESSION = os.path.join(SESSION_DIR, LOGIN_NAME + ".session")
_last_authed = False


def _reset_scratch() -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = LOGIN_SESSION + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception as e:
                log.warning("[login] remove scratch %s: %s", p, e)


def _install_scratch_as_live() -> None:
    """Atomically promote the authorized scratch session to the live path."""
    import sqlite3
    con = sqlite3.connect(f"file:{LOGIN_SESSION}?mode=ro", uri=True, timeout=5)
    try:
        row = con.execute("SELECT user_id FROM sessions LIMIT 1").fetchone()
    finally:
        con.close()
    if not (row and row[0]):
        raise RuntimeError("scratch session has no user_id — not installing")
    os.replace(LOGIN_SESSION, LIVE_SESSION)   # atomic on the same filesystem
    for suffix in ("-journal", "-wal", "-shm"):
        for base in (LIVE_SESSION, LOGIN_SESSION):
            p = base + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

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


def _persist_user_info() -> None:
    """Write the self-user to the session volume so it survives restarts."""
    try:
        if state.user_info:
            with open(USER_INFO_FILE, "w", encoding="utf-8") as f:
                json.dump(state.user_info, f)
    except Exception as e:
        log.warning("[login] persist user_info failed: %s", e)


def ensure_user_info() -> Optional[dict]:
    """Return the cached self-user, lazily restoring it from disk after a
    restart. user_info is in-memory only, but we persist a copy beside the
    session file — reload it here so callers (sidebar, settings) show the
    real username instead of @anonymous until a get_me re-populates it.
    """
    if state.user_info:
        return state.user_info
    if not session_exists():
        return None
    try:
        if os.path.exists(USER_INFO_FILE):
            with open(USER_INFO_FILE, "r", encoding="utf-8") as f:
                state.user_info = json.load(f)
    except Exception as e:
        log.warning("[login] load user_info failed: %s", e)
    return state.user_info


def session_exists() -> bool:
    """True only if Pyrogram session has a signed-in user_id, not just a handshake-only auth_key.
    Without this check, a half-finished login (token exported, never scanned) leaves
    the file on disk with auth_key set but user_id NULL — crawler then tries
    Pyrogram phone-prompt authorize and crashes with EOFError (no TTY).
    A .revoked marker forces False — the session file may still parse fine
    but the upstream account is gone, so we must re-auth.
    """
    global _last_authed
    if os.path.exists(REVOKED_MARKER):
        _last_authed = False
        return False
    import sqlite3
    path = LIVE_SESSION
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        _last_authed = False
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("SELECT user_id FROM sessions LIMIT 1").fetchone()
        finally:
            con.close()
        _last_authed = bool(row and row[0])
        return _last_authed
    except sqlite3.OperationalError as e:
        # Busy/locked = the crawler is writing to it right now. That is not
        # evidence of "signed out"; keep the last known answer.
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            return _last_authed
        _last_authed = False
        return False
    except Exception:
        _last_authed = False
        return False


async def _ensure_client() -> Client:
    """Lazy-create + connect the LOGIN client on a scratch session.

    Never touches the live session file — see LIVE_SESSION / LOGIN_SESSION.
    """
    if state.client is not None and not state.client.is_connected:
        state.client = None
    if state.client is None:
        _reset_scratch()   # every fresh attempt starts from a clean handshake
        state.client = Client(
            name=LOGIN_NAME,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=SESSION_DIR,
            no_updates=True,
        )
    if not state.client.is_connected:
        try:
            await state.client.connect()
        except Exception as e:
            # Common failure: stale storage corrupt → reset and retry once
            log.warning("[login] connect failed (%s); resetting client", e)
            try:
                await state.client.disconnect()
            except Exception:
                pass
            state.client = None
            _reset_scratch()
            state.client = Client(
                name=LOGIN_NAME,
                api_id=API_ID,
                api_hash=API_HASH,
                workdir=SESSION_DIR,
                no_updates=True,
            )
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
    state.client = None
    try:
        _install_scratch_as_live()
    except Exception as e:
        log.exception("[login] could not install new session: %s", e)
        state.status = "error"
        state.message = f"signed in, but saving the session failed: {e}"
        return
    state.user_info = {
        "id": user_obj.id,
        "username": getattr(user_obj, "username", None),
        "first_name": getattr(user_obj, "first_name", None),
    }
    state.status = "success"
    state.message = f"signed in as @{state.user_info['username'] or state.user_info['first_name']}"
    _persist_user_info()
    log.info("[login] success: %s", state.message)
    # Clear .revoked marker — fresh auth = healthy account again. Crawler's
    # boot-time check will see clean state on next start.
    try:
        if os.path.exists(REVOKED_MARKER):
            os.remove(REVOKED_MARKER)
            log.info("[login] cleared %s", REVOKED_MARKER)
    except Exception as e:
        log.warning("[login] could not clear %s: %s", REVOKED_MARKER, e)


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
                # DC migration: each DC has its own auth_key namespace.
                # Must regenerate via DH handshake on new DC; reusing the old
                # DC auth_key triggers AUTH_TOKEN_EXPIRED on ImportLoginToken.
                from pyrogram.session import Auth, Session
                try:
                    await client.session.stop()
                    await client.storage.dc_id(r.dc_id)
                    new_auth_key = await Auth(
                        client,
                        await client.storage.dc_id(),
                        await client.storage.test_mode(),
                    ).create()
                    await client.storage.auth_key(new_auth_key)
                    client.session = Session(
                        client,
                        await client.storage.dc_id(),
                        await client.storage.auth_key(),
                        await client.storage.test_mode(),
                    )
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
        if state.status != "success":
            # Abandoned attempt: drop the scratch client + file so nothing
            # half-authorized lingers on the session volume.
            try:
                if client is not None and client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
            if state.client is client:
                state.client = None
            _reset_scratch()


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


def logout() -> dict:
    """Wipe the on-disk session so the next request loops back to /login.
    Crawler container will detect the disappearance via its sqlite-aware
    `_session_authed()` poll and re-enter wait state on its next iteration.
    """
    state.reset()
    state.client = None
    removed = []
    _reset_scratch()
    for fn in ("tgarr.session", "tgarr.session-journal", "user_info.json"):
        path = os.path.join(SESSION_DIR, fn)
        if os.path.exists(path):
            try:
                os.remove(path)
                removed.append(fn)
            except Exception as e:
                log.warning("logout: remove %s: %s", fn, e)
    log.info("[logout] removed %s", removed)
    return {"status": "ok", "removed": removed,
            "message": "signed out — restart the crawler container to fully reset"}


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
