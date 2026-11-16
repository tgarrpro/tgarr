"""The registry's catalogue of course lists (courses.open_catalogue, ed25519.py): only what we signed is taken.

The catalogues below were signed once with a key made for these tests and thrown away; its public half is KEY.
"""
import base64
import courses
import ed25519

n = 0


def ok(cond, what):
    global n
    n += 1
    assert cond, what


def no(what, *a, **k):
    try:
        courses.open_catalogue(*a, **k)
    except ValueError as e:
        return str(e)
    raise AssertionError("taken: " + what)


KEY = '56pfzA81D1QfBjsos+653B5ObpXT9iQM7o80sDTMAxA='
A = ('{"made":"2026-11-16T00:00:00Z","packs":[{"lang":"en","level":"8-12","parts":[{"ia":"blue_fairy_book_0707_librivox","title":"Blue"},{"ia":"red_fairy_book_0908_librivox","title":"Red"}],"right":"PD","slug":"two-fairy-books","subject":"reading","title":"Two fairy books"},{"about":"三十节短课","channel":"tgarr0","lang":"zh-Hans","level":"5-8","right":"CC BY-SA","slug":"zh-pinyin-1","subject":"language","tag":"#zh_pinyin1","title":"拼音入门（上）","upto":57},{"about":"三十节短课","channel":"some_other_channel","lang":"zh-Hans","level":"5-8","right":"CC BY-SA","slug":"elsewhere","subject":"language","tag":"#zh_pinyin1","title":"拼音入门（上）","upto":57},{"about":"三十节短课","channel":"tgarr0","lang":"zh-Hans","level":"5-8","right":"CC BY-SA","slug":"no-tag","subject":"language","tag":"","title":"拼音入门（上）","upto":57},{"slug":"Bad Slug","title":"x"},{"lang":"en","level":"8-12","parts":[{"ia":"wizardofoz_1008_librivox","title":"x"}],"right":"PD","slug":"land-of-oz","subject":"reading","title":"x"}],"serial":100,"v":1,"withdrawn":["land-of-oz","junior-classics"]}', 'KFFXoAThIbK8aJLR+0ZguiqBPUtOQxkwaEmPcxCS2EUFkWYfIqRQD4KLl78ee4F1hYIEIS2X7AStXEb1rwl8BA==')
B = ('{"made":"2026-11-16T00:00:00Z","packs":[{"lang":"en","level":"8-12","parts":[{"ia":"blue_fairy_book_0707_librivox","title":"Blue"},{"ia":"red_fairy_book_0908_librivox","title":"Red"}],"right":"PD","slug":"two-fairy-books","subject":"reading","title":"Two fairy books"},{"about":"三十节短课","channel":"tgarr0","lang":"zh-Hans","level":"5-8","right":"CC BY-SA","slug":"zh-pinyin-1","subject":"language","tag":"#zh_pinyin1","title":"拼音入门（上）","upto":80}],"serial":200,"v":1,"withdrawn":[]}', 'QZKYg5C0kKmSzMFpNmz25xw9yVuMHKVwQbUw9ul20XZGFHnldsz5bZ/0Li/tQdGtrmHCJWF9PUA3BVKOXK8HBQ==')
V2 = ('{"v": 2, "serial": 300, "packs": []}', 'Do9oNB9jdc63CLg4lqVMG1ay29fsoO1mCucAY6A6hkh1jbwQzQ6agw+2CY0eLK+urPs8GDe4JBAffvTMvtkLDg==')

# RFC 8032, test 1
ok(ed25519.verify(bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"), b"",
                  bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")),
   "the signature check knows a good signature")
ok(not ed25519.verify(b"", b"", b"") and not ed25519.verify(b"x" * 32, b"m", b"y" * 64), "and is not thrown by nonsense")
a = courses.open_catalogue(*A, keys=(KEY,))
ok(a["serial"] == 100 and sorted(a["packs"]) == ["two-fairy-books", "zh-pinyin-1"], f"a signed catalogue is read: {sorted(a['packs'])}")
ok(a["packs"]["zh-pinyin-1"]["channel"] == "tgarr0" and a["packs"]["zh-pinyin-1"]["upto"] == 57 and a["packs"]["zh-pinyin-1"]["tag"] == "#zh_pinyin1",
   "a list over our channel: its tag, and the post it is taken up to")
ok("elsewhere" not in a["packs"], "a list over any other channel is left out, signed or not")
ok("no-tag" not in a["packs"] and "Bad Slug" not in a["packs"], "so is one over the whole channel, and one that is not in order")
ok(a["withdrawn"] == ["land-of-oz", "junior-classics"] and "land-of-oz" not in a["packs"], "what is taken back is named, and is not among the lists even when it is sent")
ok("not ours" in no("under the keys of the image", *A), "a catalogue not signed with a key of the image is not taken")
ok(courses.CATALOGUE_KEYS and all(len(base64.b64decode(k)) == 32 for k in courses.CATALOGUE_KEYS) and KEY not in courses.CATALOGUE_KEYS,
   "the image names its keys, and the tests' key is not one")
ok("not ours" in no("changed", A[0].replace("57", "58"), A[1], keys=(KEY,)), "one sign changed: not taken")
ok("not ours" in no("another's signature", A[0], B[1], keys=(KEY,)), "another catalogue's signature: not taken")
ok("not readable" in no("no signature", A[0], "!!", keys=(KEY,)) and "no catalogue" in no("empty", "", A[1], keys=(KEY,)), "no signature, no catalogue")
ok("older" in no("played back", *A, least_serial=200, keys=(KEY,)), "an older catalogue than the one in use is not taken")
b = courses.open_catalogue(*B, least_serial=100, keys=(KEY,))
ok(b["serial"] == 200 and b["packs"]["zh-pinyin-1"]["upto"] == 80 and b["withdrawn"] == [], "a newer one is: the channel's list now reaches further")
ok(courses.open_catalogue(*B, least_serial=200, keys=(KEY,))["serial"] == 200, "the same one again is taken (and changes nothing)")
ok("does not know" in no("v2", *V2, keys=(KEY,)), "a catalogue of a later kind is left alone")
ok("no catalogue" in no("endless", "x" * (courses.CATALOGUE_BYTES + 1), A[1], keys=(KEY,)), "nor one beyond all measure")
for raw, why in (({"slug": "a-b", "title": "t", "right": "PD", "channel": "tgarr0", "tag": "#x_y", "upto": 5}, None),
                 ({"slug": "a-b", "title": "t", "right": "PD", "channel": "TGARR0", "tag": "#x_y", "upto": 5}, None),
                 ({"slug": "a-b", "title": "t", "right": "PD", "channel": "tgarr0x", "tag": "#x_y", "upto": 5}, "no channel but"),
                 ({"slug": "a-b", "title": "t", "right": "PD", "channel": "tgarr0", "tag": "#x_y"}, "upto")):
    try:
        courses.clean_catalogue_pack(raw)
        ok(why is None, f"taken: {raw}")
    except ValueError as e:
        ok(why and why in str(e), f"{raw}: {e}")
print(f"test_catalogue: {n} checks passed")
