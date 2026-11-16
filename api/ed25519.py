"""Checking an Ed25519 signature (RFC 8032), in plain Python: nothing to install.

Only checking - tgarr never signs. Used for the catalogue of course lists the registry hands
out (courses.open_catalogue): the lists are signed where they are written, on a machine that
is not the registry, so that whoever got hold of the registry could still not put a list on
a child's screen. One check takes a few milliseconds; it is done when a catalogue arrives.
"""
import hashlib

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _x_of(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _x_of(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _add(a, b):
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % _P, (a[1] + a[0]) * (b[1] + b[0]) % _P
    C, Dd = 2 * a[3] * b[3] * _D % _P, 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s: int, pt):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _point(b: bytes):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _x_of(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """Whether `signature` (64 bytes) is that of `message` under the key `public` (32 bytes)."""
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        a, r = _point(public), _point(signature[:32])
        s = int.from_bytes(signature[32:], "little")
        if a is None or r is None or s >= _L:
            return False
        h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _L
        left, right = _mul(s, _G), _add(r, _mul(h, a))
        return (left[0] * right[2] - right[0] * left[2]) % _P == 0 and (left[1] * right[2] - right[1] * left[2]) % _P == 0
    except Exception:
        return False
