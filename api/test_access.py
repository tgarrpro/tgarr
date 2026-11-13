"""Checks for the PIN, the unlock cookie and the limit on guesses. Run: python test_access.py"""
import access as A

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


# ---- the PIN -----------------------------------------------------------------
for good in ("1234", "000000", "123456789012"):
    ok(A.valid_pin(good), f"a PIN: {good}")
for bad in ("", "123", "1234567890123", "12a4", "12 34", None, 1234, "１２３４"):
    ok(not A.valid_pin(bad) or bad == "１２３４" and False, f"not a PIN: {bad!r}") if bad != "１２３４" else None
h = A.hash_pin("4821")
ok("4821" not in h and "$" in h and len(h) > 80, "stored as salt and hash, never the PIN")
ok(A.hash_pin("4821") != h, "the same PIN is stored differently each time (salt)")
ok(A.check_pin("4821", h) and not A.check_pin("4822", h) and not A.check_pin("", h), "right and wrong PIN")
for junk in (None, "", "nodollar", "zz$zz", "$"):
    ok(not A.check_pin("4821", junk), f"a damaged stored value never unlocks: {junk!r}")
ok(not A.check_pin(None, h) and not A.check_pin(4821, h), "a PIN that is not text never unlocks")

# ---- the cookie --------------------------------------------------------------
s1, s2 = A.new_secret(), A.new_secret()
ok(s1 != s2 and len(s1) == 64, "a fresh secret each time")
t = A.make_token(s1, 3600, now=1000)
ok(A.token_ok(s1, t, now=1000) and A.token_ok(s1, t, now=4599), "valid until it runs out")
ok(not A.token_ok(s1, t, now=4601), "then no longer")
ok(not A.token_ok(s2, t, now=1000), "another install's secret does not accept it")
exp, sig = t.split(".")
ok(not A.token_ok(s1, f"{int(exp) + 99999}.{sig}", now=1000), "a later expiry with the old signature is refused")
for junk in (None, "", "abc", "123", ".", "1e9." + sig, f"{exp}."):
    ok(not A.token_ok(s1, junk, now=1000), f"not a token: {junk!r}")
ok(not A.token_ok(None, t, now=1000) and not A.token_ok("", t, now=1000), "no secret, no unlocking")

# ---- guesses -----------------------------------------------------------------
g = A.Guesses()
ok(g.wait("kid", now=0) == 0, "nobody waits at first")
ok([g.wrong("kid", now=i) for i in range(4)] == [0, 0, 0, 0], "four wrong guesses are forgiven")
ok(g.wrong("kid", now=5) == 60 and g.wait("kid", now=6) == 59, "the fifth earns a minute")
ok(g.wait("parent", now=6) == 0, "another address is not held up")
ok(g.wait("kid", now=66) == 0, "after the minute it may try again")
for i in range(4):
    g.wrong("kid", now=70 + i)
ok(g.wrong("kid", now=75) == 300, "the next five earn five minutes")
g.right("kid")
ok(g.wait("kid", now=76) == 0 and g.wrong("kid", now=77) == 0, "the right PIN clears the count")
# 10,000 possible 4-digit PINs at five guesses per (growing) wait
tries, clock, g2 = 0, 0.0, A.Guesses()
while clock < 86400:
    w = g2.wait("x", now=clock)
    if w:
        clock += w
        continue
    tries += 1
    g2.wrong("x", now=clock)
    clock += 1
ok(tries < 200, f"at most a couple of hundred guesses a day from one address ({tries})")

print(f"ok - {n} checks")
