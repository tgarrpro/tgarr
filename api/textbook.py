"""A plain .txt read as a book.

A text file has no layout of its own: what a novel looks like in one depends on who saved it.
Four things are worked out here, so the e-book reader can show it like any other book:

  - which characters the bytes are (UTF-8, UTF-16, GB18030, Big5, Shift-JIS, EUC-KR, Windows-1251/1252)
  - where paragraphs are: a file cut at 72 columns has its lines joined again, one that keeps a
    paragraph per line is left alone, and the indent typed in front of a paragraph is dropped
    (the reader indents by itself)
  - where chapters are: "第十二章 …", "Chapter 12", "Part Two", "序章", "# Heading". A book
    without any is cut into sections of a readable length, and so is a chapter too long to lay out
  - which lines are only decoration (a row of asterisks between scenes, a row of = under a title)

Nothing here touches the disk or the network except `load`, which reads the file."""
import html
import os
import re
import unicodedata

MOST_BYTES = 64 * 1024 * 1024       # a text longer than this is opened up to here
SECTION_CHARS = 24000               # a book without chapters is cut about this often
CHAPTER_MOST = 60000                # a chapter longer than this is cut into parts
_CACHE: dict = {}                   # path -> (mtime, book)
_CACHE_MOST = 3

# ── which characters ──────────────────────────────────────────────────────────────────
_COMMON = {
    "gb18030": "的一是不了人我在有他这为之大来以个中上们到说国和地也子时道出而要于就下得可你年生自会那后能对着事其里所去行过家十用发天如然作方成者多日都三小军二无同么经法当起与好看学进种将还分此心前面又定见只主没公从",
    "big5": "的一是不了人我在有他這為之大來以個中上們到說國和地也子時道出而要於就下得可你年生自會那後能對著事其裡所去行過家十用發天如然作方成者多日都三小軍二無同麼經法當起與好看學進種將還分此心前面又定見只主沒公從",
    "shift_jis": "のにはをたがでてとしれさあるいもなっかこらりまうくよすえんけそだアイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワン、。",
    "euc_kr": "이다는을의에가를은한로하고지도서기리사어나그대자으시아있부인정수라과일해전게만성제상원보",
    "cp1251": "оеаинтсрвлкмдпуяыьгзбчйхжшюцщэфОЕАИНТСРВЛКМДПУЯ",
}
_COMMON_SETS = {k: set(v) for k, v in _COMMON.items()}


def _score(text: str, enc: str) -> float:
    """How much of what is not ASCII is an everyday character of that writing. 0..1."""
    other = [c for c in text if ord(c) > 0x7f]
    if not other:
        return 0.0
    bad = sum(1 for c in other if c == "�" or (0x80 <= ord(c) < 0xa0) or unicodedata.category(c) in ("Co", "Cn"))
    good = sum(1 for c in other if c in _COMMON_SETS[enc])
    return (good - 3 * bad) / len(other)


def decode(data: bytes) -> tuple:
    """(text, name of the encoding). Never raises."""
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace"), "utf-8"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace"), "utf-16"
    head = data[:4000]
    if head and head.count(b"\x00") > len(head) // 4:
        # two bytes a character and no mark in front: the zeros say which half is the high one
        even = head[0::2].count(b"\x00")
        odd = head[1::2].count(b"\x00")
        enc = "utf-16-be" if even > odd else "utf-16-le"
        return data.decode(enc, "replace"), enc
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as e:
        # cut in the middle of a character at the very end (a file opened up to MOST_BYTES)
        if e.start >= len(data) - 4:
            return data[:e.start].decode("utf-8", "replace"), "utf-8"
        first_high = e.start
    # a few damaged bytes in a text that is otherwise UTF-8: it is UTF-8
    probe = data[max(0, first_high - 2000):first_high + 200000]
    as_utf8 = probe.decode("utf-8", "replace")
    many = sum(1 for c in as_utf8 if ord(c) > 0x7f)
    if many > 20 and as_utf8.count("\ufffd") * 50 < many:
        return data.decode("utf-8", "replace"), "utf-8"
    # judged from where the text stops being plain ASCII (a long English preface must not
    # decide for the Chinese book behind it)
    at = next((i for i, b in enumerate(data) if b > 0x7f), 0)
    at = data.rfind(b"\n", 0, at) + 1
    sample = data[at:at + 120000]
    high = sum(1 for b in sample if b > 0x7f)
    best, best_score = "gb18030", -9.0
    for enc in ("gb18030", "big5", "shift_jis", "euc_kr", "cp1251"):
        text = sample.decode(enc, "replace")
        s = _score(text, enc)
        if enc == "cp1251" and sum(1 for c in text if c.isalpha() and ord(c) > 0x7f) * 2 < sum(1 for c in text if c.isalpha()):
            s = -1.0      # Russian is written in its own letters; a few among Latin ones are French accents misread
        if enc == "cp1251" and s > 0:
            cyr = [c for c in text if "Ѐ" <= c <= "ӿ"]
            if sum(1 for c in cyr if c.islower()) * 4 < len(cyr) * 3:
                s = -1.0  # capitals in the middle of every word: Chinese bytes read as Russian
        if s > best_score:
            best, best_score = enc, s
    if best_score < 0.12:
        # nothing reads as a language written in those: accented letters and curly quotes
        # in a western text
        # in a western text: there such a byte stands alone between plain letters, where a
        # character of two bytes always has a neighbour
        paired = sum(1 for i in range(1, len(sample)) if sample[i] > 0x7f and sample[i - 1] > 0x7f)
        if paired * 4 < high:
            return data.decode("cp1252", "replace"), "cp1252"
        for enc in ("gb18030", "big5", "shift_jis", "euc_kr"):
            try:
                sample[:len(sample) - 4 if len(sample) == 120000 else len(sample)].decode(enc)
                return data.decode(enc, "replace"), enc
            except UnicodeDecodeError:
                continue
    return data.decode(best, "replace"), best


# ── lines into paragraphs ─────────────────────────────────────────────────────────────
def _wide(c: str) -> bool:
    return unicodedata.east_asian_width(c) in ("W", "F")


def _width(s: str) -> int:
    return sum(2 if _wide(c) else 1 for c in s)


_INDENT = " \t　 "
_RULE_RX = re.compile(r"^[\s*＊\-—–_=~～·•.。#☆★◇◆※]{3,}$")
_DOTS_RX = re.compile(r"^[.。…]{1,8}$")          # "......" on a line of its own is a pause somebody wrote, not a rule
_END_RX = re.compile(r"[.!?。！？…:：;；\"'”’」』）)\]】》〉—]$")


def _first_word(s: str) -> str:
    if not s:
        return ""
    if _wide(s[0]):
        return s[0]
    m = re.match(r"\S+", s)
    return m.group(0) if m else ""


def _wrapped_at(lines: list) -> int:
    """The column a text was cut at, or 0 when it keeps one paragraph a line."""
    widths = sorted(_width(x.rstrip()) for x in lines if x.strip())
    if len(widths) < 20:
        return 0
    w = widths[min(len(widths) - 1, int(len(widths) * 0.98))]
    if w > 130 or w < 20:
        return 0
    pairs = full = 0
    for a, b in zip(lines, lines[1:]):
        if not a.strip() or not b.strip():
            continue
        pairs += 1
        if _width(a.rstrip()) + 1 + _width(_first_word(b.strip())) > w - 2:
            full += 1
    return w if pairs >= 15 and full * 2 >= pairs else 0


def paragraphs(text: str) -> list:
    """The text as a list of paragraphs. A scene break is the paragraph "***"."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").replace("\x0c", "\n")
    lines = text.split("\n")
    # whether the text was cut at a column, looked at in three places: a preface set one way
    # must not decide for a book set the other
    n = len(lines)
    looks = [lines[:2500]] if n <= 7500 else [lines[:2500], lines[n // 2 - 1250:n // 2 + 1250], lines[-2500:]]
    widths = [_wrapped_at(x) for x in looks]
    w = max(widths) if sum(1 for x in widths if x) * 2 > len(widths) else 0
    out, cur = [], []

    def flush():
        if cur:
            out.append("".join(cur))
            cur.clear()

    prev = ""
    for raw in lines:
        line = raw.rstrip()
        bare = line.strip(_INDENT)
        if not bare:
            flush()
            prev = ""
            continue
        if _RULE_RX.match(bare) and not _DOTS_RX.match(bare):
            flush()
            if out and out[-1] != "***":
                out.append("***")
            prev = ""
            continue
        if not w:
            out.append(bare)
            continue
        indented = line[:1] in "\t\u3000" or (line[:1] in _INDENT and len(line) - len(line.lstrip(_INDENT)) >= 2)
        # a heading in a text cut at a column: in Chinese it stands on a line of its own, in
        # English it also begins a block (or "part two of his plan" in mid-sentence would be one)
        head = is_heading(bare) and (not cur or _wide(bare[0]) or bare[0] == "#")
        joins = (cur and not indented and not head and not is_heading(prev)
                 and _width(prev) + 1 + _width(_first_word(bare)) > w - 2)
        if joins:
            if cur[-1].endswith("-") and len(cur[-1]) > 1 and cur[-1][-2].isalpha() and bare[:1].isalpha():
                pass                                   # "twenty-" / "five": the hyphen is the word's own, no space
            elif not (_wide(cur[-1][-1]) and _wide(bare[0])):
                cur.append(" ")
            cur.append(bare)
        else:
            flush()
            cur.append(bare)
        prev = line.rstrip()
    flush()
    while out and out[-1] == "***":
        out.pop()
    return out


# ── chapters ──────────────────────────────────────────────────────────────────────────
_NUM = r"[0-9０-９零〇一二两兩三四五六七八九十百千万萬壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]+"
_ROMAN = r"[IVXLCDM]+"
_WORDNUM = (r"(?i:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
            r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|"
            r"the\s+first|the\s+second|the\s+third|the\s+last)")
_HEAD_RXS = [
    # "第三章 风起", and "第三章风起" with nothing between - but then only after the words that
    # are not also everyday ones (第一回见到他, 第二节课, 第一季度, 第一部分, 第三集团 are sentences)
    re.compile(r"^(?:正文\s*)?第\s*" + _NUM + r"\s*[章回卷节節部集篇幕话話折册冊季](?:$|[\s:：、.．·\-—].{0,40}$)"),
    re.compile(r"^(?:正文\s*)?第\s*" + _NUM + r"\s*[章卷篇幕话話折][^\s，。！？；的了是在].{0,14}$"),
    re.compile(r"^[卷册冊]\s*" + _NUM + r"(?:$|[\s:：、.．·\-—]).{0,40}$"),
    re.compile(r"^(?:序章|序幕|序言|自序|代序|序|楔子|引子|引言|前言|题记|題記|后记|後記|尾声|尾聲|终章|終章|"
               r"完本感言|番外|外传|外傳|附录|附錄|跋|简介|簡介|内容简介|內容簡介)(?:$|[\s:：、.．·\-—（(].{0,30}$)"),
    # (the Roman number in capitals only, and not followed by a small letter: "Part I liked
    #  best" and "Act Civil" are sentences)
    re.compile(r"^(?i:chapter|chap\.|part|book|volume|vol\.|section|act|canto|letter)\s+"
               r"(?:\d+|" + _ROMAN + r"(?![A-Za-z'’])(?!\s+[a-z])|" + _WORDNUM + r"(?:[\s-]\w+)?)\b.{0,60}$"),
    re.compile(r"^(?:prologue|epilogue|preface|foreword|introduction|afterword|contents|interlude|"
               r"acknowledge?ments|author'?s note|appendix(?:\s+\w+)?)\b.{0,40}$", re.I),
    re.compile(r"^#{1,3}\s+\S.{0,80}$"),
    re.compile(r"^(?:\d{1,4}|" + _ROMAN + r")[.．、]?$"),      # (last: chapters() asks for it by place)
]
_HEAD_NOT_END = "。，,；;！？!?"


def is_heading(line: str) -> bool:
    s = line.strip(_INDENT)
    if not s or len(s) > 80 or s[-1] in _HEAD_NOT_END:
        return False
    if "。" in s or "，" in s:
        return False
    if s[0].isalpha() and ord(s[0]) < 0x250 and not s[0].isupper():
        return False                       # "part two of his plan": a sentence, not a title
    if ord(s[0]) < 0x250 and len(s.split()) > 12:
        return False
    return any(rx.match(s) for rx in _HEAD_RXS)


def _clean_title(s: str) -> str:
    s = re.sub(r"^#{1,3}\s+", "", s.strip(_INDENT))
    s = re.sub(r"\s+", " ", s)
    return s[:120]


def _cut(paras: list, most: int) -> list:
    """Paragraphs into runs of about `most` characters, never inside a paragraph."""
    runs, cur, size = [], [], 0
    for p in paras:
        if cur and size + len(p) > most:
            runs.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += len(p)
    if cur:
        runs.append(cur)
    return runs


def chapters(paras: list) -> list:
    """[{"title": str, "paras": [str]}] in reading order."""
    marks = [i for i, p in enumerate(paras) if is_heading(p)]
    # a lone bare number is a chapter only among others like it; a table of contents at the
    # front lists every heading once more with nothing under it, and is dropped
    bare = [i for i in marks if _HEAD_RXS[-1].match(paras[i].strip(_INDENT))]
    if len(bare) < 3 or len(bare) * 4 < len(marks):
        gone = set(bare)
        marks = [i for i in marks if i not in gone]
    out = []
    if len(marks) >= 2:
        if marks[0] > 0:
            front = paras[:marks[0]]
            if any(p != "***" for p in front):
                out.append({"title": "", "paras": front})
        for n, i in enumerate(marks):
            end = marks[n + 1] if n + 1 < len(marks) else len(paras)
            out.append({"title": _clean_title(paras[i]), "paras": paras[i + 1:end]})
        # drop the list of contents: several headings in a row with nothing between them,
        # each of which comes again later
        later = {}
        for c in out:
            later[c["title"]] = later.get(c["title"], 0) + 1
        kept, k = [], 0
        while k < len(out):
            c = out[k]
            if c["title"] and not c["paras"] and later.get(c["title"], 0) > 1:
                later[c["title"]] -= 1
                k += 1
                continue
            kept.append(c)
            k += 1
        out = kept
    if len(out) < 2:
        runs = _cut(paras, SECTION_CHARS)
        return [{"title": "", "paras": r} for r in runs] or [{"title": "", "paras": []}]
    cut = []
    for c in out:
        if sum(len(p) for p in c["paras"]) <= CHAPTER_MOST:
            cut.append(c)
            continue
        for n, r in enumerate(_cut(c["paras"], SECTION_CHARS)):
            cut.append({"title": c["title"] if n == 0 else f'{c["title"]} ({n + 1})', "paras": r,
                        "more": n > 0})
    return cut


# ── a chapter as HTML ─────────────────────────────────────────────────────────────────
CSS = ("p.tb-rule{text-indent:0;text-align:center;letter-spacing:.6em;opacity:.55;margin:1.6em 0}"
       "h2.tb-h{text-align:center;margin:1em 0 1.4em}"
       "body.tb-latin p{text-indent:1.5em;margin:.55em 0;line-height:1.7;hyphens:auto}"
       "body.tb-latin{font-family:Georgia,'Iowan Old Style','Times New Roman',serif}")


def chapter_html(ch: dict) -> str:
    bits = []
    if ch.get("title") and not ch.get("more"):
        bits.append(f'<h2 class="tb-h">{html.escape(ch["title"])}</h2>')
    for p in ch["paras"]:
        if p == "***":
            bits.append('<p class="tb-rule">＊＊＊</p>')
        else:
            bits.append(f"<p>{html.escape(p)}</p>")
    return "\n".join(bits) or "<p></p>"


def is_latin(paras: list) -> bool:
    sample = "".join(paras[:400])[:20000]
    letters = [c for c in sample if c.isalpha()]
    return bool(letters) and sum(1 for c in letters if ord(c) < 0x250) * 10 >= len(letters) * 9


def book_title(file_name: str) -> str:
    t = re.sub(r"\.txt$", "", file_name or "", flags=re.I)
    return t.strip() or "text"


def build(data: bytes) -> dict:
    text, enc = decode(data)
    paras = paragraphs(text)
    chs = chapters(paras)
    return {"encoding": enc, "latin": is_latin(paras), "chapters": chs,
            "titles": [c["title"] for c in chs]}


def load(path: str) -> dict:
    """The book of a file, kept for the next chapter that is asked for."""
    mtime = os.path.getmtime(path)
    hit = _CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    with open(path, "rb") as f:
        data = f.read(MOST_BYTES)
    book = build(data)
    while len(_CACHE) >= _CACHE_MOST:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[path] = (mtime, book)
    return book
