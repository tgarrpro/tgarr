"""Checks for the work the registry hands to installs. No network, no database.
Run: python test_seeds.py"""
import os
import sys

os.environ.setdefault("DB_DSN", "postgresql://unused")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main as M  # noqa: E402

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


# names are the registry's own job now; installs get invite links
ok(M._seed_kind_sql("any") == "invite_link IS NOT NULL", "'any' means invite links")
ok(M._seed_kind_sql("invite") == "invite_link IS NOT NULL", "invite")
ok(M._seed_kind_sql("username") == "invite_link IS NULL", "names only when asked for by name")
ok(M._seed_kind_sql("'; drop table x") == "invite_link IS NOT NULL", "anything else is treated as 'any'")

# an install's report of what it found
r = M._seed_results_clean([
    {"username": "INVITE:abcdefghijklmnop", "status": "dead"},
    {"username": "some_channel", "status": "alive"},
    {"username": "hidden_one", "status": "private"},
    {"username": "weird_status", "status": "csam"},
    {"username": "abc", "status": "dead"},
    {"username": 12345, "status": "dead"},
    "not a dict",
])
ok(r == [("INVITE:abcdefghijklmnop", "dead"), ("some_channel", "alive"), ("hidden_one", "forbidden")],
   "known statuses only, 'private' stored as 'forbidden', bad rows dropped")
ok(M._seed_results_clean(None) == [] and M._seed_results_clean("x") == [] and M._seed_results_clean({}) == [],
   "not a list: nothing")
ok(len(M._seed_results_clean([{"username": f"name_{i:04d}", "status": "dead"} for i in range(500)])) == 100,
   "at most 100 in one report")
ok(M._seed_results_clean([{"username": "x" * 81, "status": "dead"}]) == [], "an over-long key is dropped")

# its own daily allowance, separate from the channel list's
M._seeds_day[0], M._seeds_day[1] = "", {}
ok(all(M._seeds_allow("ip-a") for _ in range(M.SEEDS_PULLS_PER_IP_DAY)), "allowed up to the limit")
ok(not M._seeds_allow("ip-a"), "then refused")
ok(M._seeds_allow("ip-b"), "another address is counted on its own")
M._seeds_day[0] = "1999-01-01"
ok(M._seeds_allow("ip-a"), "a new day starts from nothing")
ok(M.SEEDS_PULLS_PER_IP_DAY >= 24, "an install asking once an hour is never refused")

print(f"ok - {n} checks")
