"""Books opened without converting: FictionBook, Word, OpenDocument, Rich Text, Mobipocket,
a saved page. Each sample is made here, byte by byte, so nothing has to be downloaded."""
import base64
import io
import os
import struct
import tempfile
import zipfile

import ownbooks as B

n = 0
tmp = tempfile.mkdtemp()
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def ok(cond, what):
    global n
    n += 1
    if not cond:
        raise SystemExit(f"FAIL {n}: {what}")


def put(name, data):
    p = os.path.join(tmp, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def refused(fn, word=""):
    try:
        fn()
    except B.NotReadable as e:
        return word in str(e)
    return False


# ── pictures, and markup made safe ──
ok(B.picture_type(PNG) == "image/png" and B.picture_type(b"\xff\xd8\xff\xe0") == "image/jpeg", "a picture by its first bytes")
ok(B.picture_type(b"<svg onload=alert(1)>") is None and B.picture_type(b"<html>") is None, "an SVG or a page is not a picture")
c = B.clean('<p onclick="x()" style="color:red">a<script>alert(1)</script><b>b</b><iframe src=x></iframe>'
            '<a href="javascript:x">l</a><img src="http://evil/x.png" onerror=x></p><style>p{}</style>tail')
ok(c == "<p>a<b>b</b><span>l</span></p>tail", f"only harmless tags, no attributes: {c}")
ok(B.clean("<p>a<b>b</p>c") == "<p>a<b>b</b></p>c", "tags left open are closed")
ok(B.clean("</div></p>x") == "x", "an end without a beginning is dropped")
ok(B.clean("<center>x</center><p align=right>y</p>") == '<div class="tb-c">x</div><p class="tb-r">y</p>', "centred and right-set text stays so")
ok(B.clean('<img recindex="3">', lambda a: "../asset/i" + a["recindex"]) == '<img src="../asset/i3" alt="">', "a picture gets the address it is given")
ok(B.clean("a < b &amp; c") == "a &lt; b &amp; c", "text is escaped")
ok(B.clean('<td colspan="2" rowspan="900">x</td>') == '<td colspan="2">x</td>', "a table cell keeps a sane span")
ok(B.kind_of("War.FB2") == "fb2" and B.kind_of("a.epub") == "" and B.kind_of("noext") == "", "which files are read here")

# ── FictionBook ──
fb2 = f"""<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info><book-title>Война и мир</book-title><coverpage><image l:href="#cover.png"/></coverpage></title-info></description>
<body><title><p>Война и мир</p></title>
<section><title><p>Том первый</p></title>
  <section><title><p>Глава I</p></title><epigraph><p>Эпиграф</p><text-author>Автор</text-author></epigraph>
    <p>Первый <emphasis>абзац</emphasis> &lt;tag&gt;.</p><empty-line/><p>Второй <strong>абзац</strong>.</p>
    <image l:href="#pic.png"/><poem><stanza><v>Строка раз</v><v>Строка два</v></stanza></poem></section>
  <section><title><p>Глава II</p></title><p>Текст<a l:href="#n1" type="note">[1]</a>.</p><subtitle>* * *</subtitle><p>Ещё.</p></section>
</section></body>
<body name="notes"><section id="n1"><title><p>1</p></title><p>Сноска.</p></section></body>
<binary id="cover.png" content-type="image/png">{base64.b64encode(PNG).decode()}</binary>
<binary id="pic.png" content-type="image/png">{base64.b64encode(PNG).decode()}</binary>
<binary id="bad.png" content-type="image/png">{base64.b64encode(b"<svg/onload=1>").decode()}</binary>
</FictionBook>"""
b = B.build(put("w.fb2", fb2.encode("utf-8")), "fb2")
ok(b.title == "Война и мир", "FB2: the book's title")
ok(b.titles[:4] == ["Война и мир", "Том первый", "Глава I", "Глава II"], f"FB2: sections in order: {b.titles}")
ch = b.chapter(2)
ok("<i>абзац</i>" in ch and "&lt;tag&gt;" in ch and "<blockquote>" in ch and 'class="tb-v">Строка раз' in ch, "FB2: emphasis, epigraph, verse")
ok('<img src="../asset/pic.png"' in ch and b.asset("pic.png") == (PNG, "image/png"), "FB2: a picture, and its bytes")
ok(b.asset("bad.png") is None and b.asset("nope") is None, "FB2: what is not a picture is not served")
ok(b.cover == "cover.png" and not b.latin, "FB2: the cover; not a Latin-letter book")
ok("Сноска" in b.chapter(len(b) - 1), "FB2: the notes come last")
ru = '<?xml version="1.0" encoding="windows-1251"?><FictionBook><body><section><title><p>Глава</p></title><p>Привет.</p></section></body></FictionBook>'
ok("Привет" in B.build(put("r.fb2", ru.encode("cp1251")), "fb2").chapter(0), "FB2 saved in Windows-1251")
ok(refused(lambda: B.build(put("e.fb2", b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><FictionBook><body/></FictionBook>'), "fb2"), "entities"),
   "FB2 that declares entities is not opened")
ok(refused(lambda: B.build(put("x.fb2", b"not xml at all"), "fb2"), "damaged"), "FB2 that is not XML")

# ── Word ──
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def wp(text, style="", extra=""):
    ppr = f'<w:pPr>{f"<w:pStyle w:val={chr(34)}{style}{chr(34)}/>" if style else ""}{extra}</w:pPr>'
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


doc = (f'<?xml version="1.0"?><w:document {W}><w:body>'
       + wp("My Report", "Titel")
       + wp("Intro text.")
       + wp("First Part", "berschrift1")
       + '<w:p><w:r><w:rPr><w:b/><w:i w:val="0"/></w:rPr><w:t>bold</w:t></w:r><w:r><w:t xml:space="preserve"> and &lt;plain&gt;</w:t></w:r>'
         '<w:hyperlink r:id="rId9"><w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>2</w:t></w:r></w:hyperlink></w:p>'
       + wp("centred", "", '<w:jc w:val="center"/>')
       + wp("an item", "", '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
       + '<w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>'
       + '<w:p><w:r><w:drawing><a:blip r:embed="rId2"/></w:drawing></w:r></w:p>'
       + '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>cell B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
       + wp("Second Part", "berschrift1") + wp("More text.") + wp("Detail", "berschrift2") + wp("Deep text.")
       + "</w:body></w:document>")
styles = (f'<?xml version="1.0"?><w:styles {W}>'
          '<w:style w:type="paragraph" w:styleId="berschrift1"><w:name w:val="heading 1"/></w:style>'
          '<w:style w:type="paragraph" w:styleId="berschrift2"><w:name w:val="heading 2"/></w:style>'
          '<w:style w:type="paragraph" w:styleId="Titel"><w:name w:val="Title"/></w:style></w:styles>')
rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="http://evil/x.png" TargetMode="External"/>'
        '<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="http://x" TargetMode="External"/>'
        '</Relationships>')
core = '<?xml version="1.0"?><cp:coreProperties xmlns:cp="x" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>The Report</dc:title></cp:coreProperties>'
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("word/document.xml", doc)
    z.writestr("word/styles.xml", styles)
    z.writestr("word/_rels/document.xml.rels", rels)
    z.writestr("word/media/image1.png", PNG)
    z.writestr("docProps/core.xml", core)
b = B.build(put("r.docx", buf.getvalue()), "docx")
ok(b.title == "The Report", "DOCX: the title from its properties")
ok(b.titles == ["My Report", "First Part", "Second Part"], f"DOCX: cut at the highest headings, whatever the style is called: {b.titles}")
ch = b.chapter(1)
ok("<b>bold</b> and &lt;plain&gt;<sup>2</sup>" in ch, f"DOCX: bold, an explicit not-italic, superscript: {ch[:300]}")
ok('<p class="tb-c">centred</p>' in ch and '<p class="tb-li">• an item</p>' in ch, "DOCX: centred, a list item")
ok(ch.count("<img") == 1 and b.asset("image1.png") == (PNG, "image/png"), "DOCX: the picture inside is shown, the one on the internet is not")
ok("<table><tr><td><p>cell A</p></td><td><p>cell B</p></td></tr></table>" in ch, "DOCX: a table")
ok("Detail" in b.chapter(2) and "Deep text." in b.chapter(2), "DOCX: a lower heading stays inside its chapter")
ok(b.latin, "DOCX: a Latin-letter book")
ok(refused(lambda: B.build(put("old.docx", b"\xd0\xcf\x11\xe0 old doc"), "docx"), "Word"), "an old .doc under a .docx name")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {W}><w:body>'
               + "".join(wp(f"Chapter {i}") + wp("Some text of it.") for i in (1, 2, 3)) + "</w:body></w:document>")
b = B.build(put("plain.docx", buf.getvalue()), "docx")
ok(b.titles == ["Chapter 1", "Chapter 2", "Chapter 3"], f"DOCX without heading styles: lines that read as headings: {b.titles}")

# ── OpenDocument ──
O = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
     'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
     'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:xlink="http://www.w3.org/1999/xlink" '
     'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"')
content = (f'<?xml version="1.0"?><office:document-content {O}><office:automatic-styles>'
           '<style:style style:name="T1" style:family="text"><style:text-properties fo:font-weight="bold" fo:font-style="italic"/></style:style>'
           '<style:style style:name="P1" style:family="paragraph"><style:paragraph-properties fo:text-align="center"/></style:style>'
           '</office:automatic-styles><office:body><office:text>'
           '<text:table-of-content><text:index-body><text:p>第一章 ........ 1</text:p></text:index-body></text:table-of-content>'
           '<text:h text:outline-level="1">第一章</text:h>'
           '<text:p>正文<text:span text:style-name="T1">粗斜</text:span>后<text:s text:c="3"/>x<text:line-break/>y<text:note><text:note-body><text:p>注</text:p></text:note-body></text:note></text:p>'
           '<text:p text:style-name="P1">居中</text:p>'
           '<text:list><text:list-item><text:p>项目</text:p></text:list-item></text:list>'
           '<text:p><draw:frame><draw:image xlink:href="Pictures/a.png"/></draw:frame></text:p>'
           '<table:table><table:table-row><table:table-cell><text:p>格</text:p></table:table-cell></table:table-row></table:table>'
           '<text:h text:outline-level="1">第二章</text:h><text:p>完。</text:p>'
           '</office:text></office:body></office:document-content>')
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
    z.writestr("content.xml", content)
    z.writestr("Pictures/a.png", PNG)
    z.writestr("meta.xml", '<?xml version="1.0"?><m xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>文档</dc:title></m>')
b = B.build(put("d.odt", buf.getvalue()), "odt")
ok(b.title == "文档" and b.titles == ["第一章", "第二章"], f"ODT: title and chapters; its own list of contents is left out: {b.titles}")
ch = b.chapter(0)
ok("正文<b><i>粗斜</i></b>后   x<br>y</p>" in ch and "注" not in ch, f"ODT: a styled span, spaces, a line break; the footnote is not in the line: {ch[:200]}")
ok('<p class="tb-c">居中</p>' in ch and '<p class="tb-li">• 项目</p>' in ch and "<td><p>格</p></td>" in ch, "ODT: centred, a list, a table")
ok('<img src="../asset/a.png"' in ch and b.asset("a.png") == (PNG, "image/png"), "ODT: a picture")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("content.xml", f'<?xml version="1.0"?><office:document-content {O}><office:body><office:spreadsheet/></office:body></office:document-content>')
ok(refused(lambda: B.build(put("s.odt", buf.getvalue()), "odt"), "not a text"), "a spreadsheet under an .odt name")

# ── Rich Text ──
rtf = (rb"{\rtf1\ansi\ansicpg936\deff0{\fonttbl{\f0\fnil SimSun;}{\f1 Arial;}}{\colortbl;\red0\green0\blue0;}"
       rb"{\info{\title Hidden Title}{\author Nobody}}{\*\generator Riched20;}"
       rb"\pard Chapter 1\par Hello {\b bold} \'d6\'d0\'ce\'c4 \u20013? and \u-10179?\u-8704? end\par" + b"\r\n"
       rb"{\pict\pngblip 89504e470d0a1a0a}after picture\par \{braces\} \\ back\~slash\line second line\par"
       rb" Chapter 2\par dash\emdash quote\ldblquote x\rdblquote\tab tab\par}")
ps = B.rtf_paragraphs(rtf)
ok(ps[0] == "Chapter 1", f"RTF: first paragraph: {ps[:2]}")
ok(ps[1] == "Hello bold 中文 中 and 😀 end", f"RTF: code page bytes, a character by number, a pair of halves: {ps[1]!r}")
ok(not any("Hidden" in p or "SimSun" in p or "89504e" in p or "Riched" in p for p in ps), "RTF: fonts, properties and pictures are not text")
ok("after picture" in ps and "{braces} \\ back slash" in ps and "second line" in ps, f"RTF: escapes and a line break: {ps}")
ok("dash—quote“x” tab" in ps, "RTF: dashes and quotes")
b = B.build(put("a.rtf", rtf), "rtf")
ok(b.titles == ["Chapter 1", "Chapter 2"], f"RTF: chapters from lines that read as headings: {b.titles}")
ok(refused(lambda: B.build(put("b.rtf", b"plain text"), "rtf"), "Rich Text"), "not RTF")

# ── Mobipocket ──
ok(B.palmdoc(b"abc") == b"abc", "PalmDOC: plain bytes")
ok(B.palmdoc(b"\x02\xe4\xb8a") == b"\xe4\xb8a", "PalmDOC: bytes carried as they are")
ok(B.palmdoc(b"a\xe2") == b"a b", "PalmDOC: a space and a letter in one byte")
ok(B.palmdoc(b"abcd\x80\x23") == b"abcdabcdab", "PalmDOC: a copy that runs into itself (distance 4, length 6)")
ok(B.palmdoc(b"ab\x80") == b"ab", "PalmDOC: cut inside a copy does not raise")


def pack(data: bytes) -> bytes:
    """PalmDOC, the lazy way: every byte carried as it is."""
    out, i = bytearray(), 0
    while i < len(data):
        c = data[i]
        if c == 0 or 9 <= c <= 0x7f:
            out.append(c)
            i += 1
        else:
            j = i
            while j < len(data) and j - i < 8 and not (data[j] == 0 or 9 <= data[j] <= 0x7f):
                j += 1
            out.append(j - i)
            out += data[i:j]
            i = j
    return bytes(out)


def mobi(markup: bytes, crypt=0, comp=2, version=6, images=(PNG,), creator=b"BOOKMOBI", flags=3) -> bytes:
    recs_text = [markup[i:i + 4096] for i in range(0, len(markup), 4096)]
    title = "书名 Title".encode("utf-8")
    exth_recs = struct.pack(">LL", 503, 8 + len(title)) + title + struct.pack(">LLL", 201, 12, 0)
    exth = b"EXTH" + struct.pack(">LL", 12 + len(exth_recs), 2) + exth_recs
    exth += b"\0" * (-len(exth) % 4)
    mh = bytearray(232)
    mh[0:4] = b"MOBI"
    struct.pack_into(">LLL", mh, 4, 232, 2, 65001)
    struct.pack_into(">LL", mh, 0x54 - 16, 16 + 232 + len(exth), len(title))
    struct.pack_into(">L", mh, 0x68 - 16, version)
    struct.pack_into(">L", mh, 0x6c - 16, 1 + len(recs_text))
    struct.pack_into(">L", mh, 0x80 - 16, 0x40)
    struct.pack_into(">H", mh, 0xf2 - 16, flags)
    r0 = struct.pack(">HHLHHHH", comp, 0, len(markup), len(recs_text), 4096, crypt, 0) + bytes(mh) + exth + title + b"\0\0"
    body = []
    for t in recs_text:
        d = pack(t) if comp == 2 else t
        if flags & 1:
            d += b"\x00"
        if flags & 2:
            d += b"XY" + bytes([0x80 | 3])
        body.append(d)
    recs = [r0] + body + list(images) + [b"\xe9\x8e\x0d\x0a"]
    head = bytearray(78)
    head[0:4] = b"test"
    head[60:68] = creator
    struct.pack_into(">H", head, 76, len(recs))
    at = 78 + 8 * len(recs) + 2
    table = b""
    for i, r in enumerate(recs):
        table += struct.pack(">LL", at, 2 * i)
        at += len(r)
    return bytes(head) + table + b"\0\0" + b"".join(recs)


def page(toc=0, c1=0, c2=0):
    filler = "<p>这是很长的正文。Some text to fill the record with.</p>" * 120
    return (f'<html><head><guide><reference type="toc" title="Contents" filepos={toc:010d} /></guide></head><body>'
            f'<h1>Front Page</h1><p>by somebody</p><mbp:pagebreak/>'
            f'<p><a filepos={c1:010d}>第一章 开始</a></p><p><a filepos={c2:010d}>Chapter <i>Two</i></a></p><mbp:pagebreak/>'
            f'<p height="1em" width="2em">正文一<script>alert(1)</script><img recindex="00001" onerror="x()"><img recindex="00009"></p>{filler}<mbp:pagebreak/>'
            f'<h2>Not This Title</h2><p align="center">text two</p></body></html>').encode("utf-8")


p0 = page()
marks = [i + len(b"<mbp:pagebreak/>") for i in range(len(p0)) if p0.startswith(b"<mbp:pagebreak/>", i)]
markup = page(marks[0], marks[1], marks[2])
ok(len(markup) == len(p0), "the sample keeps its length when the positions are written in")
b = B.build(put("m.mobi", mobi(markup)), "mobi")
ok(b.title == "书名 Title", f"MOBI: the title from the book's own record: {b.title}")
ok(b.titles == ["Front Page", "", "第一章 开始", "Chapter Two"], f"MOBI: chapters named by the book's list of contents: {b.titles}")
ch = b.chapter(2)
ok("正文一" in ch and "alert" not in ch and "onerror" not in ch and "这是很长的正文" in ch, "MOBI: text across several records, script dropped")
ok(ch.count("<img") == 1 and '<img src="../asset/i1" alt="">' in ch, "MOBI: the picture that exists, by its number")
ok(b.asset("i1") == (PNG, "image/png") and b.asset("i2") is None and b.cover == "i1", "MOBI: picture bytes, and the cover")
ok('<p class="tb-c">text two</p>' in b.chapter(3), "MOBI: a centred paragraph")
ok(B.build(put("m1.mobi", mobi(markup, comp=1, flags=0)), "mobi").titles == b.titles, "MOBI not compressed, nothing appended to its records")
ok(refused(lambda: B.build(put("d.mobi", mobi(markup, crypt=2)), "mobi"), "protected"), "a protected book says so")
ok(refused(lambda: B.build(put("h.mobi", mobi(markup, comp=17480)), "mobi"), "HUFF"), "HUFF/CDIC says so")
shell = '<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title><link href="kindle:flow:0001?mime=text/css"/></head><body aid="0"></body></html>'
kf8 = (shell + '<h1>第一回</h1><p>正文一<img src="kindle:embed:0001?mime=image/png"/><img src="kindle:embed:000V?mime=image/png"/></p>'
       + shell + '<h2>Second</h2><p>text two</p>' + shell + '<p>no heading here</p>').encode("utf-8")
b = B.build(put("k.azw3", mobi(kf8 + b"p{color:red}", version=8)), "azw3")
ok(b.titles == ["第一回", "Second", ""], f"KF8: one chapter a page of the book, named by its heading: {b.titles}")
ok('<img src="../asset/i1" alt="">' in b.chapter(0) and b.chapter(0).count("<img") == 1, "KF8: a picture by its number in base 32")
ok("kindle:" not in b.chapter(0) and "<title" not in b.chapter(0) and "正文一" in b.chapter(0), "KF8: the shell of a page leaves nothing behind")
ok(B.kind_of("x.AZW3") == "azw3", "KF8 is read here")
ok(refused(lambda: B.build(put("z.mobi", b"PK\x03\x04" + b"\0" * 200), "mobi"), "not a Mobipocket"), "an EPUB renamed .mobi")
ok(refused(lambda: B.load(put("t.mobi", mobi(markup)[:300]), "mobi")), "a book cut short does not raise anything else")
long = ("<html><body>" + "".join(f"<h1>Part {i}</h1>" + "<p>word word word word word word.</p>" * 9000 for i in range(1, 4)) + "</body></html>").encode()
b = B.build(put("l.mobi", mobi(long, images=())), "mobi")
ok(len(b) > 3 and b.titles[0] == "Part 1" and "Part 2" in b.titles and "Part 3" in b.titles, f"MOBI without page breaks is cut at its headings and by length: {len(b)}")

# ── a saved page ──
pg = ('<html><head><meta charset="gb2312"><title>页面 标题</title><style>p{color:red}</style></head><body>'
      '<h1>第一节</h1><p>内容一<script>x()</script></p><h1>第二节</h1><p>内容二</p><img src="x.png"></body></html>').encode("gb18030")
b = B.build(put("p.html", pg), "html")
ok(b.title == "页面 标题" and b.titles == ["第一节", "第二节"], f"HTML: the declared characters, title, chapters: {b.title} {b.titles}")
ok("内容一" in b.chapter(0) and "x()" not in b.chapter(0) and "<img" not in b.chapter(1) and "color" not in b.chapter(0), "HTML: script, style and outside pictures are gone")

# ── the one way in ──
b1 = B.load(put("c.fb2", fb2.encode("utf-8")), "fb2")
ok(B.load(os.path.join(tmp, "c.fb2"), "fb2") is b1, "a book is kept for the next chapter")
t = B.load(put("n.txt", "第一章 一\n正文。\n第二章 二\n正文。\n".encode("gb18030")), "txt")
ok(t.titles == ["第一章 一", "第二章 二"] and "<h2" in t.chapter(0), "a .txt comes through the same door")
ok(refused(lambda: B.load(put("bad.docx", b"PK\x03\x04broken"), "docx")), "a broken zip is NotReadable, nothing else")
# ── compiled help ──
ok(B.kind_of("Manual.CHM") == "chm", "a .chm is read here")
ok(refused(lambda: B.load(put("fake.chm", b"<html>not a chm</html>"), "chm")), "what is not a .chm (or no program to open one) is NotReadable")
import unpack  # noqa: E402
BS = chr(92)
ok(unpack._literal("a[1]*?.jpg") == f"a{BS}[1{BS}]{BS}*{BS}?.jpg", "a name with pattern characters is asked for as that one file")
ok(unpack.magic(put("r.rar", unpack.RAR_MAGIC + bytes(2)), 6) == unpack.RAR_MAGIC and unpack.magic(os.path.join(tmp, "nowhere")) == b"",
   "an archive by its first bytes")
print(f"ownbooks: {n} checks passed")
