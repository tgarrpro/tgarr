"""Checks for what happens to text before the voice gets it. No voice, no model, no network.
Run: python test_tts_text.py"""
import tts_text as T
import g2pw_lite as G

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


# ---- figures in an English sentence -------------------------------------------------------
for num, want in ((0, "zero"), (9, "nine"), (13, "thirteen"), (40, "forty"), (42, "forty-two"), (100, "one hundred"), (101, "one hundred one"),
                  (999, "nine hundred ninety-nine"), (1000, "one thousand"), (1234, "one thousand two hundred thirty-four"),
                  (1_000_000, "one million"), (2_500_000, "two million five hundred thousand"), (-5, "minus five")):
    ok(T.number_words(num) == want, f"{num}: {T.number_words(num)!r}")
ok(T.number_words(12345678901234567890) == "one two three four five six seven eight nine zero one two three four five six seven eight nine zero",
   "a number too long to be a quantity is read digit by digit")
for num, want in ((1, "first"), (2, "second"), (3, "third"), (4, "fourth"), (5, "fifth"), (8, "eighth"), (9, "ninth"), (12, "twelfth"),
                  (20, "twentieth"), (21, "twenty-first"), (40, "fortieth"), (100, "one hundredth"), (101, "one hundred first")):
    ok(T.ordinal_words(num) == want, f"{num}: {T.ordinal_words(num)!r}")
for y, want in ((1984, "nineteen eighty-four"), (1900, "nineteen hundred"), (1905, "nineteen oh five"), (2000, "two thousand"),
                (2005, "two thousand five"), (2010, "twenty ten"), (2024, "twenty twenty-four"), (1066, "ten sixty-six")):
    ok(T.year_words(y) == want, f"{y}: {T.year_words(y)!r}")
for text, want in (
    ("the snow kept falling on platform 9.", "the snow kept falling on platform nine."),
    ("It weighed 3.5 tons.", "It weighed three point five tons."),
    ("In 1984 he was 36.", "In nineteen eighty-four he was thirty-six."),
    ("the 1990s were long", "the nineteen nineties were long"),
    ("She paid $5 and then $12.50.", "She paid five dollars and then twelve dollars and fifty cents."),
    ("It cost £1.", "It cost one pound."),
    ("About 12% of 1,250 people.", "About twelve percent of one thousand two hundred fifty people."),
    ("He came at 3:30 and left at 4:05, back by 7:00.", "He came at three thirty and left at four oh five, back by seven o'clock."),
    ("the 2nd and the 21st of May", "the second and the twenty-first of May"),
    ("Chapter 12", "Chapter twelve"),
    ("no figures here", "no figures here"),
):
    ok(T.numbers_to_english(text) == want, f"{text!r} -> {T.numbers_to_english(text)!r}")

# ---- tidying ---------------------------------------------------------------------------------
ok(T.tidy('"Well," said the old man, "some journeys take longer, don\'t they?"') == "Well, said the old man, some journeys take longer, don't they?",
   "quote marks go (a voice skipped the word they were stuck to); the apostrophe of don't stays: "
   + repr(T.tidy('"Well," said the old man, "some journeys take longer, don\'t they?"')))
ok(T.tidy("他说：“这件事很重要。”") == "他说：这件事很重要。", "Chinese quote marks too, leaving no gap: " + repr(T.tidy("他说：“这件事很重要。”")))
ok(T.tidy("他打开 iPhone，看了一眼 Wi-Fi 信号") == "他打开 iPhone，看了一眼 Wi-Fi 信号" and T.tidy("他说 “好” 吧") == "他说好吧",
   "only the gap between two Chinese characters goes; an English word in a Chinese sentence keeps its spaces")
ok(T.tidy("He stopped — then went on... slowly.") == "He stopped, then went on. slowly.", "a dash is a pause, dots are a stop: " + repr(T.tidy("He stopped — then went on... slowly.")))
ok(T.tidy("他停了一下——然后说……好吧。") == "他停了一下，然后说。好吧。", "the same in Chinese: " + repr(T.tidy("他停了一下——然后说……好吧。")))
ok(T.tidy("我们得在12月31日之前把3.5吨货物运走。") == "我们得在12月31日之前把3.5吨货物运走。", "figures in a Chinese sentence are left to the voice's own tables")
ok(T.tidy("It's the dog's bone, isn’t it?") == "It's the dog's bone, isn't it?", "apostrophes inside words are kept, whichever kind")
ok(T.tidy("  \n ") == "" and T.tidy(None) == "" and T.tidy("“”") == "", "nothing in, nothing out")
ok(T.tidy("see (page 3) and [note]") == "see, page three, and, note", "brackets become pauses: " + repr(T.tidy("see (page 3) and [note]")))
ok(T.split("一。二。三。", 4) == ["一。二。", "三。"] and T.split("", 10) == [] and all(len(x) <= 50 for x in T.split("a" * 500, 50)), "pieces no longer than asked")

# ---- the voice's list, a small one of our own ----------------------------------------------
WORDS = {
    "长": ["zhang3"], "着": ["zhe5"], "行": ["xing2"], "重": ["zhong4"], "得": ["de2"], "地": ["di4"], "数": ["shu4"], "教": ["jiao4"],
    "难": ["nan2"], "大": ["da4"], "夫": ["fu1"], "还": ["hai2"], "路": ["lu4"], "呢": ["ne5"], "很": ["hen3"], "我": ["wo3"], "们": ["men5"],
    "他": ["ta1"], "说": ["shuo1"], "对": ["dui4"], "你": ["ni3"], "去": ["qu4"], "不": ["bu4"], "急": ["ji2"], "银": ["yin2"], "的": ["de5"],
    "新": ["xin1"], "学": ["xue2"], "同": ["tong2"], "遇": ["yu4"], "到": ["dao4"], "了": ["le5"], "一": ["yi1"], "头": ["tou2"], "发": ["fa4"],
    "胡": ["hu2"], "子": ["zi5"], "成": ["cheng2"], "绩": ["ji4"], "和": ["he2"], "吨": ["dun1"], "在": ["zai4"], "走": ["zou3"], "高": ["gao1"],
    "银行": ["yin2", "hang2"], "行长": ["hang2", "zhang3"], "重新": ["chong2", "xin1"], "着急": ["zhao2", "ji2"], "着数": ["zhao1", "shu4"],
    "数学": ["shu4", "xue2"], "大夫": ["da4", "fu1"], "头发": ["tou2", "fa4"], "成绩": ["cheng2", "ji4"],
}
SYLL = sorted({s for r in WORDS.values() for s in r} | {"chang2", "dei3", "de5", "shu3", "jiao1", "nan4", "dai4", "fu5", "zhe5", "ji1", "han4", "dun4"})
L = T.Lexicon(WORDS, SYLL)
ok(L.said("银行的行长重新") == ["yin2", "hang2", "de5", "hang2", "zhang3", "chong2", "xin1"], "the voice by itself: the longest entry at each place")
ok(L.said("路还长着呢") == ["lu4", "hai2", "zhang3", "zhe5", "ne5"], "and one reading per character where no word helps")
ok(L.said("a长，") == [None, "zhang3", None], "what is not Chinese has no syllable")
ok(len(set(L.standin.values())) == len(SYLL) and all(0xE000 <= ord(c) <= 0xF8FF for c in L.standin.values()), "one stand-in per syllable, all in the private range")
ok(L.extra_lines().count("\n") == len(SYLL) and (L.standin["chang2"] + " chang2\n") in L.extra_lines(), "and the lines that teach them to the voice")


def read(text, spans=None, model=None):
    want = T.decide(text, L, spans, model)
    fixed = T.stand_in(text, L, want)
    return L.said(fixed), fixed


# rules
ok(read("路还长着呢")[0] == ["lu4", "hai2", "chang2", "zhe5", "ne5"], "路还长着呢: long, and going on")
ok(read("他长着胡子")[0][1] == "zhang3", "他长着胡子: he has a beard")
ok(read("很长")[0] == ["hen3", "chang2"] and read("长大了")[0][0] == "zhang3" and read("长高")[0][0] == "zhang3", "很长 is long; 长大, 长高 grow")
ok(read("不着急")[0] == ["bu4", "zhao2", "ji2"], "着急")
ok(read("大夫说")[0][:2] == ["dai4", "fu5"], "the doctor, though the voice's list has the ancient title")
ok(read("遇到了难")[0][-1] == "nan4" and read("很难走")[0][1] == "nan2", "trouble, and hard")
# the same length, and only what is wrong is touched
s, fixed = read("路还长着呢")
ok(len(fixed) == 5 and fixed[0] == "路" and fixed[3:] == "着呢" and fixed[2] == L.standin["chang2"], "one character replaced, the rest as written")
ok(read("银行的行长重新")[1] == "银行的行长重新", "what the voice says rightly is not touched at all")
# a word of the voice's list counts only where it is a word of the sentence
ok(read("同学们数着数", spans=None)[0][3:] == ["shu4", "zhao1", "shu4"], "left to its list the voice finds 着数 (a move in chess) in 数着数")
m = [None, None, None, "shu3", "zhe5", "shu4"]
ok(read("同学们数着数", spans={(0, 2), (2, 3), (3, 4), (4, 5), (5, 6)}, model=m)[0][3:] == ["shu3", "zhe5", "shu4"],
   "split into its real words, 着数 is no word here and the model's reading stands")
ok(read("不着急", spans={(0, 1), (1, 3)}, model=[None, "zhao1", "ji2"])[0] == ["bu4", "zhao2", "ji2"], "a real word of the list stands over the model")
# what the model may decide
ok(read("我们得走", model=[None, None, "dei3", None])[0][2] == "dei3", "得 as 'must': the model decides a listed character")
ok(read("说得对", model=[None, "de5", None])[0][1] == "de5", "and 得 after a verb")
ok(read("成绩", spans={(0, 2)}, model=["cheng2", "ji1"])[0] == ["cheng2", "ji4"], "but not 绩, which has one reading here whatever is taught elsewhere")
ok(read("我和你", model=[None, "han4", None])[0][1] == "he2", "nor 和 as han4")
ok(read("三吨", model=[None, "dun4"])[0][-1] == "dun1", "nor a character that is not on the list")
ok(read("一头", model=["yi4", None])[0][0] == "yi1", "一 and 不 are left to the voice")
ok(read("我们得走", model=[None, None, "xyz9", None])[0][2] == "de2", "a sound the voice does not have changes nothing")
# prepare: everything together
ok(T.prepare("“路还长着呢，”他说。", L) == L.standin["chang2"].join(["路还", "着呢，他说。"]), "tidied, decided and stood in: " + repr(T.prepare("“路还长着呢，”他说。", L)))
ok(T.prepare("platform 9", L) == "platform nine" and T.prepare("", L) == "", "an English piece is only tidied")
called = []
T.prepare("no chinese", L, lambda t: called.append(1), lambda t: called.append(2))
ok(called == [], "and neither the word splitter nor the model is asked about it")

# ---- 得 and 地 without the big model: from where they stand among the words ------------------
# (the words and their parts of speech exactly as the word splitter gives them)
def P(tagged):
    text = "".join(w for w, _ in tagged)
    return {text[i - 1:i + 2] if i else text[:2]: v for i, v in T.particles(tagged).items()}


ok(P([("你", "r"), ("得", "ud"), ("去一趟", "v")]) == {"你得去": "dei3"}, "你得去: must")
ok(P([("我们", "r"), ("得", "ud"), ("在", "p"), ("月底", "t")]) == {"们得在": "dei3"}, "我们得在月底…: must")
ok(P([("还", "d"), ("得", "ud"), ("等", "u"), ("一会儿", "m")]) == {}, "an unusual tag after it: left alone rather than guessed")
ok(P([("这事", "r"), ("得", "ud"), ("你", "r"), ("来", "v")]) == {"事得你": "dei3"}, "这事得你来办")
ok(P([("头发", "n"), ("长", "a"), ("得", "ud"), ("很", "zg"), ("长", "a")]) == {"长得很": "de5"}, "长得很长: how it is")
ok(P([("种子", "n"), ("要", "v"), ("种", "m"), ("得", "ud"), ("深", "a")]) == {"种得深": "de5"}, "种得深, though the splitter calls 种 a measure word")
ok(P([("他", "r"), ("得", "ud"), ("了", "ul"), ("第一名", "m")]) == {}, "他得了第一名: he got it - the voice's own reading stands")
ok(P([("我", "r"), ("觉得", "v"), ("他", "r"), ("说得对", "v")]) == {"说得对": "de5"}, "说得对 taken as one word: the 得 in its middle")
ok(P([("她", "r"), ("乐呵呵", "z"), ("地", "uv"), ("说", "v")]) == {"呵地说": "de5"} and P([("慢慢", "d"), ("地", "uv"), ("走", "v")]) == {"慢地走": "de5"},
   "乐呵呵地说, 慢慢地走: the way it is done")
ok(P([("这块", "r"), ("地", "uv"), ("很", "zg"), ("肥", "n")]) == {} and P([("土地", "n"), ("辽阔", "a")]) == {} and P([("他", "r"), ("地位", "n"), ("很", "d"), ("高", "a")]) == {},
   "这块地很肥, 土地, 地位: land and standing, left alone")
ok(T.spans_of([("我们", "r"), ("得", "ud"), ("走", "v")]) == {(0, 2), (2, 3), (3, 4)} and T.spans_of(["我们", "走"]) == {(0, 2), (2, 3)}, "where each word of a sentence lies")
# a hint stands where the model says nothing, and gives way where it speaks
ok(T.decide("我们得走", L, None, None, {2: "dei3"})[2] == "dei3", "without the model the hint decides")
ok(T.decide("我们得走", L, None, [None, None, "de5", None], {2: "dei3"})[2] == "de5", "with it, the model does")
ok(T.decide("我们得走", L, None, None, {2: "nope1", 9: "de5"})[2] == "de2", "a hint that makes no sense changes nothing")
ok(T.prepare("你得去", L, lambda t: [("你", "r"), ("得", "ud"), ("去", "v")]) == "你" + L.standin["dei3"] + "去", "all of it together, from the splitter's words")
try:
    import logging
    import jieba
    import jieba.posseg
    jieba.setLogLevel(logging.WARNING)
    cut = lambda t: [(w, f) for w, f in jieba.posseg.cut(t, HMM=False)]
    for s, want in (("我觉得他说得对，你得去一趟。", {"说得对": "de5", "你得去": "dei3"}), ("她乐呵呵地说，慢慢地走。", {"呵地说": "de5", "慢地走": "de5"}),
                    ("这块地很肥，土地辽阔。", {}), ("头发长得很长。", {"长得很": "de5"})):
        got = {s[i - 1:i + 2]: v for i, v in T.particles(cut(s)).items()}
        ok(got == want, f"with the real word splitter: {s} -> {got}")
except ImportError:
    pass                               # the splitter is not installed where this runs: the cases above stand in for it

# ---- the sentence model's own arithmetic: pieces of words as BERT cuts them ----------------
V = {t: i for i, t in enumerate(["[PAD]", "[UNK]", "[CLS]", "[SEP]", "他", "打", "开", "iphone", "wi", "##fi", "ok", "，", "了", "play", "##ing", "3"])}
ok(G.wordpieces("playing", V) == [("play", 4), ("##ing", 3)] and G.wordpieces("zzz", V) == [("[UNK]", 3)], "longest pieces first; an unknown word is one unknown")
toks, at = G.tokens_of("他打开 iphone，wifi了", V)
ok(toks == ["他", "打", "开", "iphone", "，", "wi", "##fi", "了"], f"characters one by one, Latin words in pieces: {toks}")
ok(at[0] == 0 and at[3] is None and at[4] == 3 and at[9] == 3 and at[11] == 5 and at[13] == 6 and at[15] == 7, f"and every character knows its token: {at}")
ok(G.tokens_of("龘", V)[0] == ["[UNK]"], "a character BERT does not know is one unknown token")

print(f"ok - {n} checks")
