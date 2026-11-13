"""Sign-in and access tests for the operator panel. No database needed.

    python test_admin.py
"""
import json
import os
import sys
import tempfile
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

tmp = tempfile.mkdtemp()
os.environ["ADMIN_CONFIG"] = os.path.join(tmp, "admin.json")
import admin  # noqa: E402

app = FastAPI()
admin.setup(app, lambda: None, "test", lambda request: "iphash")
# the cookie is Secure: talk to the app as https
c = TestClient(app, base_url="https://testserver", follow_redirects=False)
n = 0


def ok(cond, what):
    global n
    n += 1
    if not cond:
        sys.exit(f"FAIL: {what}")


async def fake_overview():
    return {"hello": 1}
admin._overview = fake_overview

# 1. no config file: the panel does not exist
for path in ("/admin", "/admin/", "/admin/login", "/admin/api/overview", "/admin/auth/start"):
    ok(c.get(path).status_code == 404, f"{path} is 404 when not configured")
ok(c.post("/admin/login", data={"password": "x"}).status_code == 404, "login post 404 when not configured")

# 2. a config that is not good enough is the same as none
with open(os.environ["ADMIN_CONFIG"], "w") as f:
    json.dump({"secret": "short", "password_hash": admin.hash_password("pw")}, f)
ok(c.get("/admin/").status_code == 404, "short secret is refused")

# 3. password sign-in
os.remove(os.environ["ADMIN_CONFIG"])
time.sleep(0.01)
with open(os.environ["ADMIN_CONFIG"], "w") as f:
    json.dump({"secret": "s" * 64, "password_hash": admin.hash_password("correct horse")}, f)
r = c.get("/admin/")
ok(r.status_code == 302 and r.headers["location"] == "/admin/login", "signed out -> login")
ok(c.get("/admin/api/overview").status_code == 401, "api needs a session")
ok(c.get("/admin/auth/start").status_code == 404, "google routes are off without google config")
r = c.get("/admin/login")
ok(r.status_code == 200 and 'type="password"' in r.text, "login form")
ok(r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY", "headers on login")

r = c.post("/admin/login", data={"password": "wrong"})
ok(r.status_code == 200 and "Wrong password" in r.text and admin.COOKIE not in r.cookies, "wrong password")
r = c.post("/admin/login", data={"password": "correct horse"})
ok(r.status_code == 303 and admin.COOKIE in r.cookies, "right password signs in")
sc = r.headers["set-cookie"].lower()
ok("httponly" in sc and "secure" in sc and "samesite=lax" in sc and "path=/admin" in sc, "cookie flags")
token = r.cookies[admin.COOKIE]
c.cookies.set(admin.COOKIE, token)
r = c.get("/admin/")
ok(r.status_code == 200 and "tgarr.me registry" in r.text and "operator" in r.text, "page after sign-in")
ok("script-src" in r.headers["content-security-policy"], "csp on page")
r = c.get("/admin/api/overview")
ok(r.status_code == 200 and r.json() == {"hello": 1} and r.headers["cache-control"] == "no-store", "api with session")
ok(c.get("/admin/api/channels?q=ab").status_code == 400, "short search refused")

# 4. a cookie that was changed, or has run out, or is of the wrong kind
body, mac = token.split(".")
c.cookies.set(admin.COOKIE, body[:-2] + "AA." + mac)
ok(c.get("/admin/api/overview").status_code == 401, "tampered cookie")
cfg = admin._cfg()
c.cookies.set(admin.COOKIE, admin._sign(cfg, {"k": "s", "u": "x", "exp": int(time.time()) - 5}))
ok(c.get("/admin/api/overview").status_code == 401, "expired cookie")
c.cookies.set(admin.COOKIE, admin._sign(cfg, {"k": "o", "u": "x", "exp": int(time.time()) + 500}))
ok(c.get("/admin/api/overview").status_code == 401, "sign-in-state cookie is not a session")
c.cookies.set(admin.COOKIE, admin._sign({"secret": "t" * 64}, {"k": "s", "u": "x", "exp": int(time.time()) + 500}))
ok(c.get("/admin/api/overview").status_code == 401, "cookie signed with another secret")
c.cookies.clear()

# 5. five wrong passwords lock that address out, even with the right one
admin._fails.clear()
for _ in range(5):
    c.post("/admin/login", data={"password": "nope"})
r = c.post("/admin/login", data={"password": "correct horse"})
ok(r.status_code == 200 and "Too many attempts" in r.text and admin.COOKIE not in r.cookies, "lock-out")
admin._fails.clear()

# 6. sign out
c.cookies.set(admin.COOKIE, token)
r = c.post("/admin/logout")
ok(r.status_code == 303 and 'tgarr_admin=""' in r.headers["set-cookie"], "sign out clears the cookie")
c.cookies.clear()

# 7. with Google configured the password door is closed
os.remove(os.environ["ADMIN_CONFIG"])
time.sleep(0.01)
with open(os.environ["ADMIN_CONFIG"], "w") as f:
    json.dump({"secret": "s" * 64, "password_hash": admin.hash_password("correct horse"),
               "google": {"client_id": "cid", "client_secret": "cs"}, "emails": ["Boss@Example.com"]}, f)
ok(c.post("/admin/login", data={"password": "correct horse"}).status_code == 404, "password off under google")
ok("Sign in with Google" in c.get("/admin/login").text, "google button")
r = c.get("/admin/auth/start", headers={"host": "tgarr.example"})
loc = r.headers["location"]
ok(r.status_code == 302 and loc.startswith("https://accounts.google.com/") and "code_challenge=" in loc
   and "redirect_uri=https%3A%2F%2Ftgarr.example%2Fadmin%2Fauth%2Fcallback" in loc, "google start")
state_cookie = r.cookies[admin.COOKIE + "_o"]
o = admin._unsign(admin._cfg(), state_cookie)
c.cookies.set(admin.COOKIE + "_o", state_cookie)


def claims(**kw):
    base = {"iss": "https://accounts.google.com", "aud": "cid", "exp": time.time() + 600,
            "nonce": o["n"], "email": "boss@example.com", "email_verified": True}
    base.update(kw)
    return base


def try_cb(cl, state=None):
    admin._google_exchange = lambda *_a: cl
    return c.get("/admin/auth/callback", params={"code": "abc", "state": state or o["s"]})


ok(admin.COOKIE not in try_cb(claims(), state="other").cookies, "wrong state")
ok(admin.COOKIE not in try_cb(claims(email="mallory@example.com")).cookies, "e-mail not on the list")
ok(admin.COOKIE not in try_cb(claims(email_verified=False)).cookies, "unverified e-mail")
ok(admin.COOKIE not in try_cb(claims(aud="someone-else")).cookies, "token for another client")
ok(admin.COOKIE not in try_cb(claims(nonce="replayed")).cookies, "wrong nonce")
ok(admin.COOKIE not in try_cb(claims(exp=time.time() - 1)).cookies, "expired token")
ok(admin.COOKIE not in try_cb(claims(iss="https://evil.example")).cookies, "wrong issuer")
r = try_cb(claims())
ok(r.status_code == 303 and admin.COOKIE in r.cookies, "allowed google account signs in")
c.cookies.clear()
c.cookies.set(admin.COOKIE, r.cookies[admin.COOKIE])
ok("boss@example.com" in c.get("/admin/").text, "page shows who is signed in")

print(f"ok - {n} checks")
