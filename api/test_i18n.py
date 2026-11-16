"""The words on the pages: every one in every language, with its blanks kept; which language
a request gets; and that no page asks for a word the catalogue does not have."""
import os
import re

import i18n as I

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


# ── the catalogue is whole ──
ok(I.CODES[0] == "en" and len(I.CODES) == len(set(I.CODES)) == 12, "twelve languages, English first")
for text, row in I.T.items():
    ok(len(row) == len(I.CODES) - 1, f"{text!r}: one translation for each language ({len(row)})")
    ok(all(isinstance(x, str) and x.strip() for x in row), f"{text!r}: none of them empty")
    blanks = sorted(re.findall(r"\{(\w+)\}", text))
    for code, x in zip(I.CODES[1:], row):
        ok(sorted(re.findall(r"\{(\w+)\}", x)) == blanks, f"{text!r} in {code}: the same blanks as the English")
        ok("[[" not in x and "]]" not in x, f"{text!r} in {code}: no marks of the page inside a word")

# ── a word in a language ──
ok(I.t("zh-Hans", "Find") == "查找" and I.t("en", "Find") == "Find" and I.t("xx", "Find") == "Find", "the word, or the English")
ok(I.t("de", "no such text anywhere") == "no such text anywhere", "what is not in the catalogue stays as written")
ok(I.t("fr", "It opens again at {t}.", t="16:00") == "Elle rouvre à 16:00.", "a blank filled in")
ok(I.t("ja", "It opens again at {t}.", t="16:00") == "16:00 にまた開きます。", "wherever the language puts it")

# ── which language ──
ok(I.pick("", "", "") == "en", "nothing known: English")
ok(I.pick("", "zh-CN,zh;q=0.9,en;q=0.8", "") == "zh-Hans" and I.pick("", "zh-TW", "") == "zh-Hant" and I.pick("", "zh-HK,en", "") == "zh-Hant",
   "Chinese as it is written where the browser is from")
ok(I.pick("", "pt-BR,pt;q=0.9", "") == "pt" and I.pick("", "iw-IL", "") == "he" and I.pick("", "EN_us", "") == "en", "regions, an old code, odd spelling")
ok(I.pick("", "sv-SE,sv;q=0.9,de;q=0.7,en;q=0.5", "") == "de", "the first language asked for that is spoken here")
ok(I.pick("", "en;q=0.3,fr;q=0.9", "") == "fr", "by the weight the browser gives")
ok(I.pick("ko", "fr", "de") == "ko" and I.pick("", "fr", "de") == "de" and I.pick("nonsense", "fr", "") == "fr",
   "this screen's choice, then the household's, then the browser's")
ok(I.pick("", "tlh, x;q=abc", "") == "en", "a language not spoken here, a weight that is no number")
ok(I.direction("he") == "rtl" and I.direction("ar") == "rtl" and I.direction("zh-Hans") == "ltr", "right to left for Hebrew and Arabic")

# ── a page filled in ──
ok(I.fill("es", "<b>[[@Find]]</b>") == "<b>Buscar</b>", "a mark replaced")
ok(I.fill("en", '<p>[[@Watch]] &amp; [[@no such]]</p>') == "<p>Watch &amp; no such</p>", "English, and a word nobody translated")
ok(I.fill("fr", 'x = "[[@js:Could not reach tgarr.]]";') == 'x = "Impossible de joindre tgarr.";', "inside a script")
real = I.T["That is not the PIN."]
I.T["That is not the PIN."] = tuple(['a"b</script><i>'] * 11)
ok(I.fill("de", 'x = "[[@js:That is not the PIN.]]";') == 'x = "a\\"b<\\/script><i>";', "a word cannot end the string or the script it stands in")
ok(I.fill("de", "<p title=\"[[@That is not the PIN.]]\">") == '<p title="a&quot;b&lt;/script&gt;&lt;i&gt;">', "nor the tag")
I.T["That is not the PIN."] = real

# ── every word a page asks for is in the catalogue ──
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), encoding="utf-8").read()
asked = set(m.group(2) for m in I._MARK.finditer(src))
asked |= set(re.findall(r'\btr\("((?:[^"\\]|\\.)+)"', src))
asked |= set(re.findall(r'i18n\.t\(lang, "((?:[^"\\]|\\.)+)"', src))
asked |= set(re.findall(r'\b_tj?\("((?:[^"\\]|\\.)+)"', src))            # _t("Open"), _tj("Saved")
themes = re.search(r"_MC_THEMES = \((.*?)\n\)\n", src, re.S).group(1)
for key, name, about in re.findall(r'\("(\w+)", "([^"]+)", "([^"]*)"', themes):
    asked |= {name} | ({about} if about else set())
asked |= {"Watch", "Listen", "Read", "Photos"}                 # the four shelves of the Media Center
# the frame of the main pages: the side menu, each page's title, the search boxes
nav = re.search(r"^NAV_ITEMS = \[(.*?)^\]", src, re.S | re.M).group(1)
asked |= set(re.findall(r'^\s*\("/[^"]*",\s*"([^"]+)"', nav, re.M))
asked |= set(re.findall(r'_layout\("([^"]+)"', src)) | set(re.findall(r'page_title="([^"]+)"', src))
asked |= set(re.findall(r'"/\w+":\s+\("/\w+",\s*"(Search [^"]+)"\)', src)) | {"Search…"}
ok(len(asked) > 40, f"the pages ask for {len(asked)} words")
missing = sorted(a for a in asked if a not in I.T)
ok(not missing, f"asked for by a page, not in the catalogue: {missing}")
# (a word may reach a page through a list or a table of the code rather than a call: it is
#  enough that the English stands somewhere in main.py, in quotes)
# (and the sentences of the book readers, which stand in their own files and are translated where shown)
src += "".join(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), f), encoding="utf-8").read() for f in ("ownbooks.py", "textbook.py"))
unused = sorted(k for k in I.T if k not in asked and ('"' + k + '"') not in src and ("'" + k + "'") not in src)
ok(not unused, f"in the catalogue, asked for by no page: {unused}")
# the same English is not given two translations in two files
import glob
seen = {}
for path in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "words_*.py"))):
    ns = {}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ns)
    for text, row in ns.get("T", {}).items():
        ok(text not in seen or seen[text][1] == row, f"{text!r} is translated differently in {seen.get(text, ('', ''))[0]} and {os.path.basename(path)}")
        seen.setdefault(text, (os.path.basename(path), row))
print(f"test_i18n: {n} checks passed")
