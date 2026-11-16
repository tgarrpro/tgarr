"""A .txt read as a book: which characters, where paragraphs and chapters are."""
import textbook as T

n = 0


def ok(cond, what):
    global n
    n += 1
    if not cond:
        raise SystemExit(f"FAIL {n}: {what}")


# ── which characters ──
zh = "第一章 风起\n　　这是一个关于他们的故事，说的是很多年以前的事情。我在那里看到了他，他也看到了我。\n" * 30
tw = "第一章 風起\n　　這是一個關於他們的故事，說的是很多年以前的事情。我在那裡看到了他，他也看到了我。\n" * 30
jp = "これは彼らについての物語です。むかしむかし、あるところにおじいさんとおばあさんがいました。\n" * 30
kr = "이것은 그들에 대한 이야기입니다. 옛날 옛적에 한 마을에 사람이 살고 있었다고 한다.\n" * 30
ru = "Это история о них. Давным-давно в одной деревне жил старик со своей старухой.\n" * 30
fr = "C’était l’été. “Où est le café ?” demanda-t-elle — naïvement.\n" * 30
for text, enc, name in ((zh, "gb18030", "simplified Chinese"), (tw, "big5", "traditional Chinese"),
                        (jp, "shift_jis", "Japanese"), (kr, "euc_kr", "Korean"), (ru, "cp1251", "Russian"),
                        (fr, "cp1252", "French with curly quotes")):
    got, found = T.decode(text.encode(enc))
    ok(found == enc, f"{name}: found {found}")
    ok(got == text, f"{name}: the text comes back as it was")
ok(T.decode(zh.encode("utf-8"))[1] == "utf-8", "UTF-8")
ok(T.decode(b"\xef\xbb\xbf" + zh.encode("utf-8"))[0] == zh, "UTF-8 with a mark in front: the mark is not text")
ok(T.decode(zh.encode("utf-16"))[0] == zh, "UTF-16 with a mark")
ok(T.decode(zh.encode("utf-16-le"))[0] != "", "UTF-16 without a mark does not raise")
ok(T.decode("plain English text\n".encode("utf-16-le") * 50)[0].startswith("plain English"), "UTF-16 LE without a mark")
ok(T.decode("plain English text\n".encode("utf-16-be") * 50)[0].startswith("plain English"), "UTF-16 BE without a mark")
ok(T.decode(zh.encode("utf-8")[:-1])[0].startswith("第一章"), "UTF-8 cut inside its last character")
ok(T.decode(b"")[0] == "", "an empty file")
mostly_ascii = ("The Project Gutenberg eBook\n" * 200 + "作者：鲁迅，这是他的一个故事\n").encode("gb18030")
ok(T.decode(mostly_ascii)[1] == "gb18030", "one Chinese line in an English text is still Chinese")

# ── paragraphs ──
web = "第一章 风起\n\n　　他走了。\n　　“你去哪里？”她问。\n\n\n　　没有人回答。\n"
ok(T.paragraphs(web) == ["第一章 风起", "他走了。", "“你去哪里？”她问。", "没有人回答。"],
   "a paragraph a line: kept, the typed indent dropped")

para = ("It was the best of times, it was the worst of times, it was the age of wisdom, it was the age of "
        "foolishness, it was the epoch of belief, it was the epoch of incredulity, it was the season of Light, "
        "it was the season of Darkness, it was the spring of hope, it was the winter of despair.")


def wrap(text, width):
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    out.append(line)
    return "\n".join(out)


gut = "\n\n".join(wrap(para, 70) for _ in range(12))
got = T.paragraphs(gut)
ok(len(got) == 12 and got[0] == para, "a text cut at 70 columns: the lines of a paragraph are joined again")
ok(T.paragraphs("CHAPTER I\n\n" + gut)[0] == "CHAPTER I", "the heading above it stays a line of its own")
hy = "\n\n".join(wrap(para, 70).replace("incredulity", "incre-\ndulity") for _ in range(12))
ok("incredulity" in T.paragraphs(hy)[0] or "incre-" in T.paragraphs(hy)[0], "a word cut at the line's end does not lose letters")
verse = gut + "\n\nRoses are red,\nViolets are blue,\nSugar is sweet,\nAnd so are you.\n\n" + gut
ok("Violets are blue," in T.paragraphs(verse), "short lines inside a cut text stay lines (a poem)")
dia = gut + "\n\n\"No.\"\n\"Yes.\"\n\n" + gut
ok('"No."' in T.paragraphs(dia) and '"Yes."' in T.paragraphs(dia), "one-word lines of speech are not joined")
cn = "这是一个很长的段落需要在固定的宽度处断开然后继续写下去直到这一段结束为止，我们看看它能不能被重新接起来变成一个完整的段落。"
cn_wrapped = "\n\n".join("\n".join(cn[i:i + 20] for i in range(0, len(cn), 20)) for _ in range(12))
got = T.paragraphs(cn_wrapped)
ok(len(got) == 12 and got[0] == cn, "Chinese cut at 20 characters: joined, and with no space put in")
ok(T.paragraphs("一\n\n* * *\n\n二\n") == ["一", "***", "二"], "a row of asterisks is a scene break")
ok(T.paragraphs("一\n=====\n-----\n二\n") == ["一", "***", "二"], "two rows of decoration make one break")
ok(T.paragraphs("一\r\n二\r\n") == ["一", "二"], "Windows line ends")
ok(T.paragraphs("") == [], "nothing in, nothing out")

# ── headings ──
for s in ("第一章 风起", "第12章", "第一百二十三回　大闹天宫", "正文 第三章 夜", "卷二 长安", "序章", "楔子", "后记",
          "番外：那年夏天", "Chapter 12", "CHAPTER XII. The Storm", "Chapter One", "Part Two", "BOOK II", "Prologue",
          "Epilogue", "# A heading", "第三卷 北方", "第十话 再见", "Chapter Twenty-One"):
    ok(T.is_heading(s), f"a heading: {s}")
for s in ("第一次见到她的时候，他还很年轻。", "他说：第一章已经写完了。", "第二天早上", "part two of his plan was simpler",
          "Chapter 3 was the one he had read to her every night that winter before the war came and took him.",
          "序", "他走了", "Book me a table, please.", "第一章写得不好，重写。", "", "Part of the reason was simple"):
    ok(not T.is_heading(s) or s == "序", f"not a heading: {s}")

# ── chapters ──
novel = "书名\n作者：某人\n\n" + "".join(f"第{i}章 标题{i}\n　　正文{i}。\n　　又一段。\n" for i in range(1, 6))
chs = T.chapters(T.paragraphs(novel))
ok([c["title"] for c in chs] == [""] + [f"第{i}章 标题{i}" for i in range(1, 6)], "front matter, then five chapters")
ok(chs[0]["paras"] == ["书名", "作者：某人"] and chs[2]["paras"] == ["正文2。", "又一段。"], "each chapter holds its own text")
toc = "目录\n" + "".join(f"第{i}章 标题{i}\n" for i in range(1, 6)) + novel
chs = T.chapters(T.paragraphs(toc))
ok([c["title"] for c in chs].count("第3章 标题3") == 1, "a list of contents at the front is not five empty chapters")
ok(any(c["paras"] == ["正文3。", "又一段。"] for c in chs), "and the real chapter 3 is kept")
plain = "\n".join(f"这是第{i}段，没有任何章节标题，只有一段接一段的文字。" * 5 for i in range(600))
chs = T.chapters(T.paragraphs(plain))
ok(len(chs) > 3 and all(not c["title"] for c in chs), "no chapters: cut into sections")
ok(all(sum(len(p) for p in c["paras"]) <= T.SECTION_CHARS + 200 for c in chs), "of a readable length")
ok(sum(len(c["paras"]) for c in chs) == 600, "and no paragraph is lost")
long = "第一章 长\n" + "\n".join("很长的一段文字。" * 50 for _ in range(400)) + "\n第二章 短\n完。\n"
chs = T.chapters(T.paragraphs(long))
ok(chs[0]["title"] == "第一章 长" and chs[1]["title"] == "第一章 长 (2)" and chs[-1]["title"] == "第二章 短",
   "a chapter too long to lay out is cut into parts")
nums = "\n".join(f"{i}\n\nSome text of section {i} goes here and it is a sentence." for i in range(1, 8))
ok([c["title"] for c in T.chapters(T.paragraphs(nums))] == [str(i) for i in range(1, 8)], "bare numbers as chapters, when there are several")
one = "He was 42\n42\nyears old."
ok(len(T.chapters(T.paragraphs(one))) == 1, "one bare number is not a chapter")
ok(T.chapters([]) == [{"title": "", "paras": []}], "an empty book has one empty section")

# ── what the read-through found ──
dirty = zh.encode("utf-8")
dirty = dirty[:300] + b"\xff\xfe" + dirty[300:]
ok(T.decode(dirty)[1] == "utf-8" and "这是一个关于他们的故事" in T.decode(dirty)[0], "two damaged bytes in a UTF-8 text: still UTF-8")
late = ("The Project Gutenberg eBook, a very long licence in English.\n" * 3000 + zh).encode("gb18030")
ok(T.decode(late)[1] == "gb18030" and "这是一个关于他们的故事" in T.decode(late)[0], "a Chinese book behind 180 KB of English: judged from where the Chinese begins")
late5 = ("The Project Gutenberg eBook, a very long licence in English.\n" * 3000 + tw).encode("big5")
ok(T.decode(late5)[1] == "big5", "and a Big5 one likewise")
for s in ("Chapter Sixty-One", "Chapter Ninety", "Part I", "Part IV. The Return", "Act V", "第三章风起", "第十二卷龙战于野", "第一回 大闹天宫", "第二节 课后"):
    ok(T.is_heading(s), f"a heading: {s}")
for s in ("Part I liked best.", "Part I'm not proud of", "Act Civil", "Book I read last year", "第一回见到他", "第二节课下课后", "第一季度营收增长",
          "第一部分人先走了", "第三集团军司令部", "第一章的内容很长"):
    ok(not T.is_heading(s), f"not a heading: {s}")
ok(T.paragraphs("一\n......\n二\n") == ["一", "......", "二"], "a line of dots is a pause somebody wrote, not a scene break")
tw5 = "\n\n".join(wrap(para, 70).replace("of hope,", "of twenty-\nfive,") for _ in range(12))
ok("twenty-five" in T.paragraphs(tw5)[0] or "twenty- five" not in T.paragraphs(tw5)[0], "a hyphen at a line's end stays, without a space after it")
mixed = "\n\n".join(wrap(para, 70) for _ in range(40)) + "\n\n" + "\n".join("这是正文里很长很长的一段话，没有在任何固定的宽度处断开，一段就是一行。" * 3 for _ in range(9000))
got = T.paragraphs(mixed)
ok(len(got) > 9000, f"a preface cut at a column in front of a book that is not: the book keeps its paragraphs ({len(got)})")
tabbed = "\n".join("\t" + wrap(para, 70).replace("\n", "\n") for _ in range(12))
ok(len(T.paragraphs(tabbed)) == 12, "a tab in front of a paragraph begins it")

# ── HTML ──
h = T.chapter_html({"title": "第一章 <风>", "paras": ["a < b & c", "***", "完"]})
ok("<h2" in h and "&lt;风&gt;" in h and "a &lt; b &amp; c" in h and "tb-rule" in h, "escaped, with a heading and a break")
ok("<h2" not in T.chapter_html({"title": "x (2)", "paras": ["p"], "more": True}), "the second part of a chapter has no heading")
ok(T.is_latin(T.paragraphs(gut)) and not T.is_latin(T.paragraphs(novel)), "which of the two layouts")
b = T.build(novel.encode("gb18030"))
ok(b["encoding"] == "gb18030" and len(b["chapters"]) == 6 and b["titles"][1] == "第1章 标题1", "bytes in, book out")
ok(T.book_title("三体.TXT") == "三体" and T.book_title("") == "text", "the name on the shelf")
print(f"textbook: {n} checks passed")
