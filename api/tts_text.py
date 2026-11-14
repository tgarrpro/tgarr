"""Reading aloud: what happens to the text before the voice gets it.

The voice (a small open model run on this machine's CPU) says what it is given, and its own
idea of how Chinese is read is one reading per character plus a list of words: 长 is always
zhang3, 得 is always de2, and "路还长着呢" comes out wrong. So the text is prepared here:

  tidy        marks the voice stumbles over are taken out or turned into pauses; in an English
              sentence numbers become words ("platform 9" was read "platform jiu").
  decide      how each Chinese character should sound, in this order:
                1. a short list of rules of our own - each a case the others get wrong;
                2. a word of the voice's own list, when it really is a word of this sentence;
                3. a model that reads the whole sentence (optional, see g2pw_lite.py), allowed
                   to decide a fixed list of characters only.
  stand in    a character the voice would say wrongly is replaced by a stand-in: a character
              no book contains, added to the voice's list as "this syllable and nothing else".
              One sound changes; the words and the flow around it do not.

Nothing here needs the voice or any model: everything is plain functions (test_tts_text.py).
"""
import re

HAN = re.compile(r"[一-鿿]")
SYLLABLE = re.compile(r"[a-z]+[1-5]")
STANDIN_BASE = 0xE000            # private-use characters: one per syllable the voice can say

# ───────────────────────── tidy ─────────────────────────

_QUOTES = "“”„‟«»「」『』〝〞＂\"‘’‚‛`"
_RX_APOSTROPHE = re.compile(r"(?<=[A-Za-z])[’'](?=[A-Za-z])")
_RX_DASH = re.compile(r"\s*(?:—+|–|―|──+|-{2,})\s*")
_RX_DOTS = re.compile(r"(?:…+|\.{3,}|。{2,})")
_RX_BRACKETS = re.compile(r"[\(\)\[\]\{\}（）【】〔〕《》〈〉]")
_RX_SPACE = re.compile(r"[ \t\r\n　]+")
_RX_CJK_SPACE = re.compile(r"(?<=[　-鿿＀-￯]) +(?=[　-鿿＀-￯])")

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen").split()
_TENS = "x x twenty thirty forty fifty sixty seventy eighty ninety".split()
_ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
_BIG = ((10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand"))


def number_words(n: int) -> str:
    """12 -> 'twelve', 1234 -> 'one thousand two hundred thirty-four'."""
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    if n < 1000:
        return _ONES[n // 100] + " hundred" + (" " + number_words(n % 100) if n % 100 else "")
    for size, name in _BIG:
        if n >= size:
            if n >= size * 1000:
                break
            return number_words(n // size) + " " + name + (" " + number_words(n % size) if n % size else "")
    return " ".join(_ONES[int(d)] for d in str(n))          # too long to be a quantity: digit by digit


def ordinal_words(n: int) -> str:
    w = number_words(n)
    head, sep, last = w.rpartition("-") if "-" in w.split(" ")[-1] else w.rpartition(" ")
    last = _ORD.get(last) or (last[:-1] + "ieth" if last.endswith("y") else last + "th")
    return (head + sep + last) if head else last


def year_words(y: int) -> str:
    """1984 -> 'nineteen eighty-four', 2005 -> 'two thousand five', 2024 -> 'twenty twenty-four'."""
    if y % 1000 == 0:
        return number_words(y)
    hi, lo = divmod(y, 100)
    if lo == 0:
        return number_words(hi) + " hundred"
    if 2000 <= y < 2010:
        return "two thousand " + number_words(lo)
    return number_words(hi) + " " + ("oh " + number_words(lo) if lo < 10 else number_words(lo))


def _spoken(digits: str) -> str:
    return " ".join(_ONES[int(d)] for d in digits)


_RX_MONEY = re.compile(r"([$£€])\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
_RX_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
_RX_TIME = re.compile(r"\b(\d{1,2}):([0-5]\d)\b")
_RX_ORDINAL = re.compile(r"\b(\d{1,4})(st|nd|rd|th)\b", re.I)
_RX_DECIMAL = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d+)\.(\d+)\b")
_RX_YEAR = re.compile(r"(?<![\d.,$£€])(1[1-9]\d\d|20\d\d)(?![\d,]|\.\d)(s?)\b")
_RX_INT = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")
_MONEY = {"$": ("dollar", "cent"), "£": ("pound", "penny"), "€": ("euro", "cent")}


def numbers_to_english(text: str) -> str:
    """Every figure in an English sentence as the words a person would say."""
    def money(m):
        unit, part = _MONEY[m.group(1)]
        n = int(m.group(2).replace(",", ""))
        out = number_words(n) + " " + unit + ("" if n == 1 else "s")
        if m.group(3):
            c = int(m.group(3).ljust(2, "0"))
            if c:
                out += " and " + number_words(c) + " " + ("pence" if part == "penny" and c != 1 else part + ("" if c == 1 or part == "penny" else "s"))
        return out

    def decimal(m):
        return number_words(int(m.group(1).replace(",", ""))) + " point " + _spoken(m.group(2))

    def year(m):
        w = year_words(int(m.group(1)))
        if m.group(2):                                       # "the 1990s"
            w = w[:-1] + "ies" if w.endswith("y") else w + "s"
        return w

    t = _RX_MONEY.sub(money, text)
    t = _RX_PERCENT.sub(lambda m: (decimal(_RX_DECIMAL.match(m.group(1))) if "." in m.group(1) else number_words(int(m.group(1)))) + " percent", t)
    t = _RX_TIME.sub(lambda m: number_words(int(m.group(1))) + " " + ("o'clock" if m.group(2) == "00" else
                                                                      ("oh " if m.group(2)[0] == "0" else "") + number_words(int(m.group(2)))), t)
    t = _RX_ORDINAL.sub(lambda m: ordinal_words(int(m.group(1))), t)
    t = _RX_DECIMAL.sub(decimal, t)
    t = _RX_YEAR.sub(year, t)
    return _RX_INT.sub(lambda m: number_words(int(m.group().replace(",", ""))), t)


def tidy(text: str) -> str:
    """The text as the voice should be given it: no marks it would skip a word over, pauses
    where a dash or dots stood, and - in a sentence without Chinese - figures as words. (In a
    Chinese sentence the voice's own tables read dates and quantities: 12月31日, 3.5吨.)"""
    t = _RX_APOSTROPHE.sub("'", text or "")
    for q in _QUOTES:
        t = t.replace(q, " ")
    t = re.sub(r"(?<![A-Za-z])'|'(?![A-Za-z])", " ", t)      # a quote mark, not the apostrophe of don't
    chinese = bool(HAN.search(t))
    t = _RX_DOTS.sub("。" if chinese else ". ", t)
    t = _RX_DASH.sub("，" if chinese else ", ", t)
    t = _RX_BRACKETS.sub("，" if chinese else ", ", t)
    if not chinese:
        t = numbers_to_english(t)
    t = _RX_SPACE.sub(" ", t).strip()
    if chinese:                    # a space between two Chinese characters (or their punctuation) is where a mark was
        t = _RX_CJK_SPACE.sub("", t)
    t = re.sub(r"\s+([,.;:!?，。；：！？])", r"\1", t)
    t = re.sub(r"([，,])(?:\s*[，,])+", r"\1", t)
    return re.sub(r"[，,\s]+$", "", re.sub(r"^[，,。.\s]+", "", t))


# ───────────────────────── the voice's own word list ─────────────────────────

class Lexicon:
    """The voice's list of words (word -> syllables), and a stand-in character for every
    syllable it can say. `words` maps a string to its list of syllables."""

    def __init__(self, words: dict, syllables):
        self.words = words
        self.longest = max((len(w) for w in words if HAN.search(w)), default=1)
        self.standin, k = {}, 0
        for s in sorted(set(syllables)):
            while chr(STANDIN_BASE + k) in words:
                k += 1
            self.standin[s] = chr(STANDIN_BASE + k)
            k += 1
        self.sound_of = {c: s for s, c in self.standin.items()}

    @classmethod
    def read(cls, lexicon_path: str, tokens_path: str) -> "Lexicon":
        words = {}
        with open(lexicon_path, encoding="utf-8") as f:
            for ln in f:
                p = ln.split()
                if len(p) >= 2 and p[0] not in words:
                    words[p[0]] = p[1:]
        with open(tokens_path, encoding="utf-8") as f:
            syll = [ln.split()[0] for ln in f if ln.strip() and SYLLABLE.fullmatch(ln.split()[0])]
        return cls(words, syll)

    def extra_lines(self) -> str:
        """The lines to add to the voice's list so that it knows the stand-ins."""
        return "".join(f"{c} {s}\n" for s, c in self.standin.items())

    def spans(self, text: str) -> list:
        """(start, end, [syllables]) for each stretch the voice reads as one entry: the
        longest entry at each place, which is how the voice itself looks them up."""
        out, i, n = [], 0, len(text)
        while i < n:
            ch = text[i]
            if ch in self.sound_of:
                out.append((i, i + 1, [self.sound_of[ch]]))
                i += 1
                continue
            if not HAN.match(ch):
                i += 1
                continue
            for size in range(min(self.longest, n - i), 0, -1):
                w = text[i:i + size]
                r = self.words.get(w)
                if r is not None and len(r) == size and not any(c in self.sound_of for c in w):
                    out.append((i, i + size, r))
                    i += size
                    break
            else:
                i += 1
        return out

    def said(self, text: str) -> list:
        """What the voice says for each character when left alone (None: not a syllable)."""
        out = [None] * len(text)
        for a, b, r in self.spans(text):
            out[a:b] = r
        return out


# ───────────────────────── decide ─────────────────────────

# Characters whose tone moves with what follows; the voice was trained its own way with them.
KEEP = frozenset("一不")
# The sentence model was trained on readings as taught in Taiwan (成绩 with ji1, 和 as han4). It
# may decide only characters that have more than one reading in mainland usage, and never one
# of the readings that differ across the strait.
POLYPHONIC = frozenset(
    "长行重得地的着了还数教难好大差乐只发觉都为种处当分更给会几间将降角解空落没蒙闷泡片强曲塞散少盛省似宿弹挑调帖相兴应载占涨正中转传藏朝称冲臭创答待担倒钉斗恶干杆冠观号"
    "和喝横哄划混夹假监结禁劲卷看壳累量凉笼露绿率论么模磨宁泊铺切亲圈任丧扇舍什石熟说提同吐鲜削血咽要饮与予晕扎炸挣症轴钻作背便别薄参曾叉场车乘匙畜揣打逮单掸读度肚囤垛"
    "坊菲佛否服脯杆供勾估骨桧过哈汗巷貉核荷吓虹哗豁奇缉济系茄贾槛嚼侥脚校节芥矜仅尽靓颈据卡咳吭溃烙勒擂蠡俩撩淋馏溜碌抡脉埋蔓氓眯靡秘泌抹娜呢粘弄疟排迫胖刨炮喷漂撇仆朴"
    "曝栖蹊茜呛悄翘雀阙撒色厦杉煞汤苔通拓瓦圩尾纤肖吁旋轧殷荫约钥择殖爪拽幢综柞坐")
ACROSS_THE_STRAIT = frozenset({
    ("和", "han4"), ("绩", "ji1"), ("期", "qi2"), ("质", "zhi2"), ("危", "wei2"), ("微", "wei2"), ("企", "qi4"), ("识", "shi4"),
    ("液", "yi4"), ("击", "ji2"), ("携", "xi1"), ("括", "gua1"), ("血", "xie3"), ("熟", "shou2"), ("谁", "shei2"), ("角", "jiao3"),
    ("说", "shui4"), ("薄", "bo2"), ("给", "ji3"), ("露", "lou4"), ("壳", "qiao4"), ("色", "shai3"), ("剥", "bo1"), ("削", "xue1")})

_ADVERBS = "很还太更最多不好挺真这那么蛮较越再又也都"
_GROWS = "大得成出高胖进满了着"
_RX_NAN = re.compile("遇到了难|有难|遭难|大难|受难|落难|逃难|避难|发难|责难|非难|刁难|灾难|苦难|患难|磨难|罹难|殉难|遇难|空难|海难")


def rules(text: str, py: list) -> list:
    """Ours, over everything else. Each rule is here because a real sentence was read wrongly
    by the voice's list AND by the model; when another turns up it is added - with its test."""
    for m in re.finditer("长", text):
        i = m.start()
        before, after, third = text[i - 1:i], text[i + 1:i + 2], text[i + 2:i + 3]
        # "路还长着呢", "日子长着呢": long, and going on - not "he has a beard" (长着胡子)
        if after == "着" and (third == "呢" or not HAN.match(third or "。")):
            py[i] = "chang2"
        # 很长 / 太长 / 多长 / 不长 / 那么长: long - unless something grows (长大, 长得, 长高 ...)
        elif before and before in _ADVERBS and (not after or after not in _GROWS):
            py[i] = "chang2"
    for m in re.finditer("着急", text):
        py[m.start()] = "zhao2"
    for m in re.finditer("大夫", text):                        # the doctor; the ancient title is rare in what people read
        py[m.start()], py[m.start() + 1] = "dai4", "fu5"
    for m in _RX_NAN.finditer(text):
        py[m.start() + m.group().index("难")] = "nan4"
    return py


def decide(text: str, lex: Lexicon, word_spans=None, model=None) -> list:
    """How every character of `text` should sound: a syllable, or None for what is not a
    Chinese character the voice knows.
    word_spans: {(start, end)} of the sentence's real words (a word splitter's answer), so
        that an entry of the voice's list counts only where it is a word of this sentence -
        the voice takes the longest entry at each place and finds "着数" (a move in chess) in
        数着数. None: every entry counts.
    model: one reading per character from a model that read the whole sentence, or None."""
    said = lex.said(text)
    py = list(said)
    if model is not None:
        for i, ch in enumerate(text):
            m = model[i] if i < len(model) else None
            if m and said[i] and m != said[i] and ch in POLYPHONIC and (ch, m) not in ACROSS_THE_STRAIT and m in lex.standin:
                py[i] = m
    for a, b, r in lex.spans(text):
        if b - a > 1 and (word_spans is None or (a, b) in word_spans):
            py[a:b] = r
    py = rules(text, py)
    return [said[i] if (said[i] is None or ch in KEEP or not py[i] or py[i] not in lex.standin) else py[i]
            for i, ch in enumerate(text)]


def stand_in(text: str, lex: Lexicon, want: list) -> str:
    """`text` with every character the voice would say otherwise than wanted replaced by the
    stand-in for the wanted syllable. The same length as `text`."""
    said = lex.said(text)
    return "".join(lex.standin[want[i]] if (want[i] and said[i] and want[i] != said[i]) else ch
                   for i, ch in enumerate(text))


def prepare(text: str, lex: Lexicon, word_spans_of=None, model_of=None) -> str:
    """Everything, for one piece of text: tidied, decided, stood in. The two helpers are
    optional (a word splitter, the sentence model); without them the rules and the voice's
    own words decide."""
    t = tidy(text)
    if not t or not HAN.search(t):
        return t
    spans = word_spans_of(t) if word_spans_of else None
    model = model_of(t) if model_of else None
    return stand_in(t, lex, decide(t, lex, spans, model))


def split(text: str, limit: int = 260) -> list:
    """Pieces of at most `limit` characters, cut at the end of a sentence where there is one."""
    out, buf = [], ""
    for s in re.findall(r"[^.!?。！？；;\n]+[.!?。！？；;\n]*", text or "") or ([text] if text else []):
        if buf and len(buf) + len(s) > limit:
            out.append(buf)
            buf = ""
        buf += s
        while len(buf) > limit:
            out.append(buf[:limit])
            buf = buf[limit:]
    if buf.strip():
        out.append(buf)
    return [x.strip() for x in out if x.strip()]
