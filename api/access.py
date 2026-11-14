"""Who may see what on this install.

tgarr used to show everything to whoever opened its address: once the owner
had signed in to Telegram, anybody on the same network saw the owner's
channels, private photos and settings. With a PIN set, a browser sees only
what the owner has put on the kids' list until the PIN is entered in it.

This file is the part with no web framework and no database in it: the PIN,
the cookie that remembers an unlocked browser, and the limit on guesses.
"""
import hashlib
import hmac
import secrets
import time

COOKIE = "tgarr_unlock"
UNLOCK_SECONDS = 12 * 3600            # an unlocked browser stays unlocked this long
REMEMBER_SECONDS = 30 * 86400         # ... or this long when asked to remember
_ROUNDS = 200_000


def valid_pin(pin: str) -> bool:
    """4 to 12 digits."""
    return isinstance(pin, str) and pin.isascii() and pin.isdigit() and 4 <= len(pin) <= 12


def hash_pin(pin: str, salt: str | None = None) -> str:
    """'salt$hash' - the PIN itself is never stored."""
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), _ROUNDS).hex()
    return f"{salt}${h}"


def check_pin(pin: str, stored: str | None) -> bool:
    if not stored or "$" not in stored or not isinstance(pin, str):
        return False
    salt, _h = stored.split("$", 1)
    try:
        return hmac.compare_digest(hash_pin(pin, salt), stored)
    except ValueError:
        return False


def new_secret() -> str:
    return secrets.token_hex(32)


def make_token(secret: str, seconds: int, now: float | None = None, who: str = "") -> str:
    """'expiry.signature' - says only "this browser was unlocked until then": the owner's.
    With `who` ('p3': a person the owner has added) 'expiry.who.signature' - that browser is
    still locked, it is only known whose it is."""
    exp = int((now if now is not None else time.time()) + seconds)
    if who:
        if not who.isalnum():
            raise ValueError("who: letters and digits")
        sig = hmac.new(secret.encode(), f"{exp}.{who}".encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{who}.{sig}"
    sig = hmac.new(secret.encode(), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def token_who(secret: str | None, token: str | None, now: float | None = None) -> str | None:
    """Whose browser: '' the owner's (unlocked), 'p3' that person's, None nobody's."""
    if token_ok(secret, token, now):
        return ""
    parts = (token or "").split(".")
    if not secret or len(parts) != 3:
        return None
    exp, who, sig = parts
    if not exp.isdigit() or not who.isalnum() or int(exp) < (now if now is not None else time.time()):
        return None
    good = hmac.new(secret.encode(), f"{exp}.{who}".encode(), hashlib.sha256).hexdigest()
    return who if hmac.compare_digest(good, sig) else None


def token_ok(secret: str | None, token: str | None, now: float | None = None) -> bool:
    if not secret or not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    if not exp.isdigit() or int(exp) < (now if now is not None else time.time()):
        return False
    good = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)


class Guesses:
    """A PIN is short: five wrong guesses from one address and that address
    waits, longer each time (1, 5, 15, 60 minutes)."""
    WAITS = (60, 300, 900, 3600)

    def __init__(self):
        self.by = {}                  # address -> [wrong in a row, locked until, times locked]

    def wait(self, who: str, now: float | None = None) -> int:
        """Seconds this address must still wait; 0 when it may try."""
        now = now if now is not None else time.time()
        st = self.by.get(who)
        return max(0, int(st[1] - now)) if st else 0

    def wrong(self, who: str, now: float | None = None) -> int:
        """Record a wrong guess; returns the wait it earned (0 if none yet)."""
        now = now if now is not None else time.time()
        st = self.by.setdefault(who, [0, 0.0, 0])
        st[0] += 1
        if st[0] >= 5:
            w = self.WAITS[min(st[2], len(self.WAITS) - 1)]
            st[0], st[1], st[2] = 0, now + w, st[2] + 1
            return w
        if len(self.by) > 5000:       # do not grow without bound
            for k in [k for k, v in self.by.items() if v[1] < now and k != who][:2500]:
                self.by.pop(k, None)
        return 0

    def right(self, who: str) -> None:
        self.by.pop(who, None)
