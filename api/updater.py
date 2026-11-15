"""Updating now, from the Settings page.

tgarr's containers are kept current by a small updater beside them (watchtower, in
docker-compose.yml): every half hour it looks whether a newer image was released. This lets
somebody say "look now" - the api cannot replace itself, it can only ask the updater, over
the private network the containers share, with a token both were given by the compose file.

    configured()            was this install set up with an updater that can be asked?
    ask(timeout)            ask it; -> (state, what to tell the user)
    verdict(code, error)    the same decision, from what the request came to (pure)

States: 'asked' (it is working; the page waits for the new version), 'current' (it looked
and there was nothing newer), 'none' (this install's compose file has no such updater),
'away' (it is not running), 'refused' (the token is not the updater's), 'failed'.
"""
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("TGARR_UPDATER_URL", "").strip().rstrip("/")
TOKEN = os.environ.get("TGARR_UPDATER_TOKEN", "").strip()
BY_HAND = "docker compose pull && docker compose up -d"


def configured() -> bool:
    return bool(URL and TOKEN)


def beside(dest: str, own: str) -> bool:
    """Is `dest` an address on the private network this container itself stands on? Both
    private, and in the same /16 (Docker gives a network of containers a /16 or less)."""
    try:
        d, o = ipaddress.ip_address(dest), ipaddress.ip_address(own)
    except ValueError:
        return False
    if d.version != 4 or o.version != 4 or not (d.is_private and o.is_private) or d.is_loopback:
        return False
    return d.packed[:2] == o.packed[:2]


def where_is(url: str) -> tuple:
    """(the address the updater's name stands for, this container's own address towards it).
    Raises OSError when the name stands for nothing."""
    u = urllib.parse.urlsplit(url)
    dest = socket.gethostbyname(u.hostname or "")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest, u.port or 80))                       # (nothing is sent: it only chooses the route)
        return dest, s.getsockname()[0]
    finally:
        s.close()


def verdict(code=None, error: str = "") -> tuple:
    """(state, message). code: the HTTP status, if there was an answer; error: 'timeout',
    'refused' (nothing listens), 'unknown host', or anything else that went wrong."""
    if code == 200:
        return "current", "The updater looked: nothing newer than what is running was released."
    if code in (401, 403):
        return "refused", ("The updater would not take the request: its token is not the one this install was given. "
                           "Run `docker compose up -d` in the folder that holds docker-compose.yml, so that both are started from the same file.")
    if code is not None:
        return "failed", f"The updater answered {code}. To update by hand: `{BY_HAND}`"
    if error == "timeout":
        return "asked", "The updater is fetching the new version. tgarr restarts when it has it - a minute or two; this page comes back by itself."
    if error in ("refused", "unknown host"):
        return "away", ("The updater is not running. In the folder that holds docker-compose.yml: "
                        f"`docker compose up -d` starts it; `{BY_HAND}` updates now.")
    return "failed", f"The updater could not be asked ({error or 'no answer'}). To update by hand: `{BY_HAND}`"


def ask(timeout: float = 600.0) -> tuple:
    """Ask the updater to look now. It answers only when it is done - and if it replaces
    this very container, never; so the caller waits a few seconds for a quick no and then
    takes silence for 'working' (see verdict's 'timeout')."""
    if not configured():
        return "none", ("This install was set up from a docker-compose.yml older than 0.5.11, whose updater cannot be asked. "
                        f"To update now, in the folder that holds it: `{BY_HAND}`. "
                        "To have this button work, replace docker-compose.yml with the current one from GitHub and run `docker compose up -d`.")
    # When the updater is not running its name is nobody's on tgarr's network, and the
    # resolver may go on to ask outside - where some router or provider answers for every
    # name. The request, with its token, must never go there: it is sent only to an address
    # beside this container's own. (Seen in testing: a stopped updater "answered 404".)
    try:
        dest, own = where_is(URL)
    except OSError:
        return verdict(error="unknown host")
    if not beside(dest, own):
        return verdict(error="unknown host")
    req = urllib.request.Request(URL + "/v1/update", headers={"Authorization": "Bearer " + TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return verdict(r.status)
    except urllib.error.HTTPError as e:
        return verdict(e.code)
    except (socket.timeout, TimeoutError):
        return verdict(error="timeout")
    except urllib.error.URLError as e:
        why = e.reason
        if isinstance(why, (socket.timeout, TimeoutError)):
            return verdict(error="timeout")
        if isinstance(why, ConnectionRefusedError):
            return verdict(error="refused")
        if isinstance(why, socket.gaierror):
            return verdict(error="unknown host")
        return verdict(error=str(why)[:120])
    except Exception as e:                                   # the connection cut while the answer came: it is at work
        return verdict(error="timeout" if isinstance(e, (ConnectionError, OSError)) else str(e)[:120])
