"""Checks for the Media Center's time rules. Run: python test_screentime.py"""
import calendar

import screentime as S

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


def at(day, hhmm):          # a UTC moment; 2026-11-09 is a Monday
    h, m = hhmm.split(":")
    return calendar.timegm((2026, 11, day, int(h), int(m), 0))


R = S.clean({"on": True, "wd": {"from": "16:00", "to": "20:00", "limit": 90},
             "we": {"from": "09:00", "to": "21:00", "limit": 180}, "session": 40, "rest": 10, "tz": 0})

# ---- rules as typed in ----------------------------------------------------
ok(S.clean(None)["on"] is False and S.clean({})["wd"]["from"] == "16:00", "nothing typed: off, with the defaults")
c = S.clean({"on": 1, "wd": {"from": "25:99", "to": "nope", "limit": "abc"}, "session": -5, "rest": 99999, "tz": 5000})
ok(c["wd"] == {"from": "16:00", "to": "20:00", "limit": 90}, "nonsense hours and limit fall back")
ok(c["session"] == 0 and c["rest"] == 240 and c["tz"] == 840, "numbers are kept in range")
ok(S.clean({"wd": "x", "we": 3})["we"]["limit"] == 180, "a part that is not a table falls back")

# ---- the clock ----------------------------------------------------------------
ok(S.local(at(9, "17:30"), 0)[1:] == (17 * 60 + 30, False), "Monday is a school day")
ok(S.local(at(14, "10:00"), 0)[2] is True and S.local(at(15, "10:00"), 0)[2] is True, "Saturday and Sunday are the weekend")
ok(S.local(at(13, "10:00"), 0)[2] is False, "Friday is not")
ok(S.local(at(13, "20:00"), 480)[2] is True and S.local(at(13, "20:00"), 480)[1] == 4 * 60,
   "Friday 20:00 UTC is already Saturday 04:00 eight hours ahead")
ok(S.local(at(14, "02:00"), -360)[2] is False, "and Saturday 02:00 UTC is still Friday evening six hours behind")

# ---- may it be used now? -------------------------------------------------------
v = S.verdict(R, at(9, "17:00"), 0, 0, 0)
ok(v["ok"] and v["left_s"] == 90 * 60, "inside the hours, nothing used")
v = S.verdict(R, at(9, "15:59"), 0, 0, 0)
ok(not v["ok"] and v["why"] == "hours" and v["until"] == "16:00", "before opening time")
ok(S.verdict(R, at(9, "20:00"), 0, 0, 0)["why"] == "hours", "closed from the closing minute on")
ok(S.verdict(R, at(14, "20:30"), 0, 0, 0)["ok"], "the weekend has its own, longer hours")
v = S.verdict(R, at(9, "17:00"), 90 * 60, 0, 0)
ok(not v["ok"] and v["why"] == "day" and v["left_s"] == 0, "the day's minutes are used up")
ok(S.verdict(R, at(14, "17:00"), 90 * 60, 0, 0)["ok"], "the same amount is fine at the weekend")
v = S.verdict(R, at(9, "17:00"), 600, 0, at(9, "17:07"))
ok(not v["ok"] and v["why"] == "rest" and v["until"] == "17:07", "resting, and when the rest ends")
ok(S.verdict(dict(R, on=False), at(9, "03:00"), 99999, 99999, at(9, "09:00"))["ok"], "switched off: no rule applies")
night = S.clean({"on": True, "wd": {"from": "22:00", "to": "06:00", "limit": 0}})
ok(S.verdict(night, at(9, "23:30"), 0, 0, 0)["ok"] and S.verdict(night, at(9, "05:00"), 0, 0, 0)["ok"]
   and not S.verdict(night, at(9, "12:00"), 0, 0, 0)["ok"], "hours that run past midnight")
ok(S.verdict(night, at(9, "23:30"), 10 ** 6, 0, 0)["left_s"] is None, "a limit of 0 means no daily limit")

# ---- counting time ---------------------------------------------------------------
D = S.Devices()
t = at(9, "17:00")
ok(D.ping("tablet", R, t)["ok"] and D.by["tablet"]["used"] == 0, "the first signal counts nothing")
for i in range(1, 11):
    D.ping("tablet", R, t + 30 * i)
ok(D.by["tablet"]["used"] == 300, "ten signals half a minute apart are five minutes")
D.ping("tablet", R, t + 300 + 3600)
ok(D.by["tablet"]["used"] == 300, "an hour with the page closed is not counted")
ok(D.by.get("phone") is None and D.ping("phone", R, t)["left_s"] == 90 * 60, "another device has its own count")
# one sitting of 40 minutes earns a 10 minute rest
D2, t = S.Devices(), at(9, "16:00")
last = None
for i in range(0, 81):
    last = D2.ping("tv", R, t + 30 * i)
ok(not last["ok"] and last["why"] == "rest", "after 40 minutes in one go: rest")
ok(not D2.check("tv", R, t + 30 * 80 + 300)["ok"], "five minutes later still resting")
ok(D2.check("tv", R, t + 30 * 80 + 601)["ok"], "after ten minutes it may go on")
used = D2.by["tv"]["used"]
D2.ping("tv", R, t + 30 * 80 + 100)
ok(D2.by["tv"]["used"] == used, "signals during the rest are not counted")
# the day's limit, and a new day
D3, t = S.Devices(), at(14, "09:00")            # Saturday: 180 minutes, breaks off to see the limit alone
RW = dict(R, session=0)
for i in range(0, 361):
    last = D3.ping("tv", RW, t + 30 * i)
ok(not last["ok"] and last["why"] == "day" and D3.by["tv"]["used"] >= 180 * 60, "the day's 180 minutes run out")
ok(D3.ping("tv", RW, at(15, "09:00"))["ok"] and D3.by["tv"]["used"] == 0, "the next day starts from nothing")

print(f"ok - {n} checks")
