"""Time rules for the Media Center - the part with no web framework and no database in it.

Rules (all optional, set by the owner):
  * the hours of the day the Media Center is open, separately for school days and weekends
  * how many minutes a day, separately for school days and weekends
  * a break: after so many minutes in one go, so many minutes of rest

Time is counted per device (by its address on the network), from small "still here" signals
the pages send while they are open and visible. A browser that has entered the PIN is never
limited.
"""
import re

DEFAULT = {"on": False,
           "wd": {"from": "16:00", "to": "20:00", "limit": 90},     # Monday to Friday
           "we": {"from": "09:00", "to": "21:00", "limit": 180},    # Saturday, Sunday
           "session": 40, "rest": 10, "tz": 0}                      # tz: minutes ahead of UTC
PING_SECONDS = 30
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _minutes(hhmm, fallback: int) -> int:
    m = _HHMM.match(str(hhmm or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else fallback


def clean(raw) -> dict:
    """Rules as typed in, made safe: missing parts take the defaults, numbers are kept in range."""
    raw = raw if isinstance(raw, dict) else {}
    out = {"on": bool(raw.get("on"))}
    for k in ("wd", "we"):
        d, r = DEFAULT[k], raw.get(k) if isinstance(raw.get(k), dict) else {}
        f, t = str(r.get("from") or d["from"]), str(r.get("to") or d["to"])
        out[k] = {"from": f if _HHMM.match(f) else d["from"], "to": t if _HHMM.match(t) else d["to"],
                  "limit": _num(r.get("limit"), d["limit"], 0, 1440)}
    out["session"] = _num(raw.get("session"), DEFAULT["session"], 0, 600)
    out["rest"] = _num(raw.get("rest"), DEFAULT["rest"], 0, 240)
    out["tz"] = _num(raw.get("tz"), 0, -840, 840)
    return out


def _num(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def local(now: float, tz_minutes: int):
    """(day number since 1970 in local time, minutes into the day, True on Saturday or Sunday)."""
    t = int(now) + tz_minutes * 60
    day = t // 86400
    return day, (t % 86400) // 60, (day + 3) % 7 >= 5          # day 0 was a Thursday


def verdict(rules: dict, now: float, used_today_s: int, session_s: int, rest_until: float) -> dict:
    """May this device use the Media Center right now?
    {'ok': bool, 'why': '' | 'hours' | 'day' | 'rest', 'until': local 'HH:MM' it opens again or '',
     'left_s': seconds left today (None = no daily limit)}"""
    if not rules.get("on"):
        return {"ok": True, "why": "", "until": "", "left_s": None}
    _day, minute, weekend = local(now, rules["tz"])
    r = rules["we" if weekend else "wd"]
    start, end = _minutes(r["from"], 0), _minutes(r["to"], 1440)
    left = None if not r["limit"] else max(0, r["limit"] * 60 - used_today_s)
    hhmm = lambda m: f"{(m // 60) % 24:02d}:{m % 60:02d}"          # noqa: E731
    inside = (start <= minute < end) if start <= end else (minute >= start or minute < end)
    if not inside:
        return {"ok": False, "why": "hours", "until": r["from"], "left_s": left}
    if left is not None and left <= 0:
        return {"ok": False, "why": "day", "until": "", "left_s": 0}
    if rest_until > now:
        return {"ok": False, "why": "rest", "until": hhmm(minute + int((rest_until - now + 59) // 60)), "left_s": left}
    return {"ok": True, "why": "", "until": "", "left_s": left}


class Devices:
    """What each device has used: seconds today, seconds in the current sitting, and until when it rests."""

    def __init__(self):
        self.by = {}      # who -> {"day", "used", "session", "last", "rest_until"}

    def get(self, who: str, day: int) -> dict:
        d = self.by.get(who)
        if d is None or d["day"] != day:
            d = self.by[who] = {"day": day, "used": d["used"] if d and d["day"] == day else 0,
                                "session": 0, "last": 0.0, "rest_until": d["rest_until"] if d else 0.0}
        return d

    def ping(self, who: str, rules: dict, now: float) -> dict:
        """A page says "still here". Counts the time since the last signal (when
        that was recent), starts a break when one sitting has been long enough,
        and answers with the verdict."""
        day, _m, _w = local(now, rules.get("tz", 0))
        d = self.get(who, day)
        gap = now - d["last"] if d["last"] else None
        rest_s = max(1, rules.get("rest", 0)) * 60
        if gap is not None and gap >= rest_s:
            d["session"] = 0                      # away long enough: that was the break
        v = verdict(rules, now, d["used"], d["session"], d["rest_until"])
        if v["ok"] and gap is not None and gap <= PING_SECONDS * 3:
            add = int(min(gap, PING_SECONDS * 1.5))
            d["used"] += add
            d["session"] += add
            if rules.get("on") and rules.get("session") and d["session"] >= rules["session"] * 60 and rules.get("rest"):
                d["rest_until"], d["session"] = now + rules["rest"] * 60, 0
            v = verdict(rules, now, d["used"], d["session"], d["rest_until"])
        d["last"] = now
        return v

    def check(self, who: str, rules: dict, now: float) -> dict:
        """The verdict without counting anything (for every request the server answers)."""
        day, _m, _w = local(now, rules.get("tz", 0))
        d = self.get(who, day)
        return verdict(rules, now, d["used"], d["session"], d["rest_until"])
