"""Books this reader opens by itself, without converting them to a PDF first.

    .txt                 textbook.py
    .fb2                 FictionBook: one XML file, pictures inside it
    .docx  .odt          a zip of XML (Word, LibreOffice)
    .rtf                 text and paragraphs (type faces, pictures and tables are let go)
    .html  .htm          a saved page: its text and structure (pictures kept beside it are not in the file)
    .pptx                slides: a chapter a slide - its title, its text, its pictures and tables
    .xlsx                sheets: a table a sheet, as the cells read (numbers, dates, text), no charts
    .chm                 Windows' compiled help: its pages in the order of its contents, its pictures
                         (unpacked by 7-Zip, see unpack.py)
    .mobi  .azw  .prc    Mobipocket, the old Kindle format, when it is not protected
    .azw3                KF8, the newer one: its pages of XHTML, its pictures (its style sheets are let go)

Each becomes the same thing: a title, chapter titles, chapters as HTML, pictures by name. The
e-book reader shows that as it shows an EPUB - type you can size, pages that fit the screen,
Read aloud - where calibre made pictures of pages out of them, after a 1.5 GB download.

What a book file says is never trusted: its markup is rebuilt from a short list of harmless
tags (`clean`), every other tag and every attribute is dropped, and a picture is served only
when its first bytes say it is one.

`NotReadable` carries a sentence for the person reading: a protected book, a kind of
compression not handled here. The caller may then fall back on converting."""
import base64
import bisect
import codecs
import html
import os
import re
import struct
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

import textbook

KINDS = ("txt", "fb2", "docx", "odt", "rtf", "mobi", "azw", "azw3", "prc", "html", "htm", "xhtml", "chm",
         "pptx", "ppsx", "xlsx", "xlsm")
XML_MOST = 80 * 1024 * 1024         # one XML part, unpacked
TEXT_MOST = 120 * 1024 * 1024       # the text of one book
_CACHE: dict = {}                   # path -> (mtime, Book)
_CACHE_MOST = 3

CSS = (textbook.CSS +
       "p.tb-c,h4.tb-c{text-indent:0;text-align:center}p.tb-r{text-indent:0;text-align:right}"
       "p.tb-v{text-indent:0;margin:.15em 0 .15em 2em}div.tb-verse{margin:1em 0}"
       "p.tb-li{text-indent:0;margin:.3em 0 .3em 1.5em}"
       "div.tb-wide{overflow-x:auto}table.tb-sheet{font-size:.85em;margin:1em 0}"
       "table.tb-sheet td{white-space:nowrap;max-width:26em;overflow:hidden;text-overflow:ellipsis}"
       "table{border-collapse:collapse;margin:1em auto}td,th{border:1px solid rgba(120,140,170,.45);"
       "padding:.25em .6em;vertical-align:top}td p,th p{text-indent:0;margin:.2em 0}")


class NotReadable(Exception):
    """This book cannot be opened here. The message is shown as it is."""


class Book:
    def __init__(self, title, titles, chapters, latin=False, assets=None, cover=None):
        self.title = title or None
        self.titles = titles
        self._chapters = chapters       # HTML, or what textbook.chapter_html takes
        self.latin = latin
        self._assets = assets or {}     # name -> ("bytes", b) | ("zip", path, member) | ("file", path, offset, length)
        self.cover = cover              # the name of the cover among the assets, if any

    def __len__(self):
        return len(self._chapters)

    def chapter(self, n: int) -> str:
        c = self._chapters[n]
        return textbook.chapter_html(c) if isinstance(c, dict) else c

    def asset(self, name: str):
        """(bytes, media type) of a picture of the book, or None."""
        src = self._assets.get(name)
        if not src:
            return None
        try:
            if src[0] == "bytes":
                data = src[1]
            elif src[0] == "7z":
                import unpack
                data = unpack.seven_read(src[1], src[2], 40 * 1024 * 1024)
            elif src[0] == "zip":
                with zipfile.ZipFile(src[1]) as zf:
                    if zf.getinfo(src[2]).file_size > 40 * 1024 * 1024:
                        return None
                    data = zf.read(src[2])
            else:
                with open(src[1], "rb") as f:
                    f.seek(src[2])
                    data = f.read(src[3])
        except (OSError, KeyError, zipfile.BadZipFile):
            return None
        kind = picture_type(data)
        return (data, kind) if kind else None


def picture_type(data: bytes):
    """The media type of a picture a browser shows, told from its first bytes; else None
    (an SVG or a page of HTML handed in as a "picture" could carry script)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM" and len(data) > 26:
        return "image/bmp"
    return None


# ── markup made safe ──────────────────────────────────────────────────────────────────
_KEEP = {"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "b", "i", "em", "strong", "u", "s", "sup", "sub",
         "blockquote", "ul", "ol", "li", "dl", "dt", "dd", "div", "span", "table", "thead", "tbody", "tr", "td", "th",
         "pre", "code", "small", "big", "img", "cite"}
_SAME = {"strike": "s", "del": "s", "tt": "code", "font": "span", "center": "div", "a": "span", "section": "div",
         "article": "div", "figure": "div", "figcaption": "p", "caption": "p", "ins": "span", "q": "span"}
_VOID = {"br", "hr", "img"}
_DROP_WITH_TEXT = {"script", "style", "head", "title", "iframe", "object", "embed", "svg", "math", "form", "guide",
                   "select", "textarea", "noscript", "template", "audio", "video", "canvas", "applet", "frameset"}


class _Cleaner(HTMLParser):
    def __init__(self, picture):
        super().__init__(convert_charrefs=True)
        self.out, self.open, self.drop, self.picture = [], [], 0, picture

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_WITH_TEXT:
            self.drop += 1
            return
        if self.drop:
            return
        was = tag
        tag = _SAME.get(tag, tag)
        if tag not in _KEEP:
            return
        a = dict(attrs)
        if tag == "img":
            src = self.picture(a) if self.picture else None
            if src:
                self.out.append(f'<img src="{html.escape(src, quote=True)}" alt="">')
            return
        extra = ""
        if was == "center" or (a.get("align") or "").lower() == "center":
            extra = ' class="tb-c"'
        elif (a.get("align") or "").lower() == "right":
            extra = ' class="tb-r"'
        if tag in ("td", "th"):
            for k in ("colspan", "rowspan"):
                if (a.get(k) or "").isdigit() and 1 < int(a[k]) <= 50:
                    extra += f' {k}="{int(a[k])}"'
        self.out.append(f"<{tag}{extra}>")
        if tag not in _VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in _DROP_WITH_TEXT:
            self.drop -= 1
        elif not self.drop:
            t = _SAME.get(tag, tag)
            if t in _KEEP and t not in _VOID and self.open and self.open[-1] == t:
                self.open.pop()
                self.out.append(f"</{t}>")

    def handle_endtag(self, tag):
        if tag in _DROP_WITH_TEXT:
            self.drop = max(0, self.drop - 1)
            return
        if self.drop:
            return
        tag = _SAME.get(tag, tag)
        if tag in self.open:
            while self.open:
                t = self.open.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.drop:
            self.out.append(html.escape(data, quote=False))


def clean(markup: str, picture=None) -> str:
    """Somebody else's HTML as markup that can do nothing: the tags of a short list, no
    attribute but what is written here. `picture(attrs)` gives the address of an <img>."""
    c = _Cleaner(picture)
    try:
        c.feed(markup)
        c.close()
    except Exception:
        pass
    while c.open:
        c.out.append(f"</{c.open.pop()}>")
    return "".join(c.out)


def _plain(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


# ── blocks into chapters ──────────────────────────────────────────────────────────────
def _from_blocks(blocks: list) -> tuple:
    """blocks: (heading level or 0, html, plain text). Returns (titles, chapters as HTML).
    The book is cut at its highest headings; one without any at lines that read as headings
    ("Chapter 3", "第三章"); one without those either, and a chapter too long, by length."""
    levels = sorted({b[0] for b in blocks if b[0]})
    if not levels:
        blocks = [(1 if (len(t) <= 80 and textbook.is_heading(t)) else 0,
                   f'<h2 class="tb-h">{html.escape(t.strip())}</h2>' if (len(t) <= 80 and textbook.is_heading(t)) else h, t)
                  for _, h, t in blocks]
        levels = [1] if sum(1 for b in blocks if b[0]) >= 2 else []
        if not levels:
            blocks = [(0, h if not h.startswith('<h2 class="tb-h">') else f"<p>{html.escape(t.strip())}</p>", t)
                      for _, h, t in blocks]
    cut_at = 0
    for lv in levels:
        cut_at = lv
        if sum(1 for b in blocks if b[0] and b[0] <= lv) >= 2:
            break
    parts, cur_title, cur = [], "", []
    for lv, h, t in blocks:
        if cut_at and lv and lv <= cut_at:
            if cur:
                parts.append((cur_title, cur))
            cur_title, cur = re.sub(r"\s+", " ", t).strip()[:120], [(lv, h, t)]
        else:
            cur.append((lv, h, t))
    if cur:
        parts.append((cur_title, cur))
    titles, chapters = [], []
    for title, bl in parts:
        if not any(t.strip() or "<img" in h for _, h, t in bl):
            continue
        size, run, n = 0, [], 0
        most = textbook.CHAPTER_MOST if sum(len(t) for _, _, t in bl) > textbook.CHAPTER_MOST else 10 ** 12
        for lv, h, t in bl:
            if run and size + len(t) > (textbook.SECTION_CHARS if most < 10 ** 12 else most):
                titles.append(title if n == 0 else (f"{title} ({n + 1})" if title else ""))
                chapters.append("\n".join(run))
                run, size, n = [], 0, n + 1
            run.append(h)
            size += len(t)
        if run:
            titles.append(title if n == 0 else (f"{title} ({n + 1})" if title else ""))
            chapters.append("\n".join(run))
    if not chapters:
        titles, chapters = [""], ["<p></p>"]
    return titles, chapters


def _latin(blocks: list) -> bool:
    return textbook.is_latin([t for _, _, t in blocks[:600]])


def _xml(data: bytes):
    if len(data) > XML_MOST:
        raise NotReadable("this document is too large to open here")
    if b"<!ENTITY" in data[:20000] or b"<!entity" in data[:20000]:
        raise NotReadable("this document declares entities of its own and is not opened")
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise NotReadable(f"the document inside is damaged ({e})")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _zip_part(zf, name: str) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise NotReadable("this is not the kind of document its name says (a part is missing)")
    if info.file_size > XML_MOST:
        raise NotReadable("this document is too large to open here")
    return zf.read(name)


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)[:120] or "x"


# ── FictionBook ───────────────────────────────────────────────────────────────────────
_FB2_INLINE = {"emphasis": "i", "strong": "b", "sub": "sub", "sup": "sup", "strikethrough": "s", "code": "code"}


def _fb2(data: bytes) -> Book:
    root = _xml(data)
    assets, cover = {}, None
    total = 0
    for el in root:
        if _local(el.tag) == "binary" and el.get("id") and el.text:
            try:
                raw = base64.b64decode(el.text, validate=False)
            except Exception:
                continue
            total += len(raw)
            if total > 150 * 1024 * 1024:
                break
            assets[_safe_name(el.get("id"))] = ("bytes", raw)

    def href(el):
        for k, v in el.attrib.items():
            if _local(k) == "href" and v.startswith("#"):
                name = _safe_name(v[1:])
                return name if name in assets else None
        return None

    def inline(el) -> str:
        out = [html.escape(el.text or "", quote=False)]
        for ch in el:
            tag = _local(ch.tag)
            if tag == "image":
                name = href(ch)
                if name:
                    out.append(f'<img src="../asset/{name}" alt="">')
            elif tag in _FB2_INLINE:
                out.append(f"<{_FB2_INLINE[tag]}>{inline(ch)}</{_FB2_INLINE[tag]}>")
            else:
                out.append(inline(ch))
            out.append(html.escape(ch.tail or "", quote=False))
        return "".join(out)

    def text_of(el) -> str:
        return re.sub(r"\s+", " ", "".join(el.itertext())).strip()

    def blocks_of(el, out: list):
        for ch in el:
            tag = _local(ch.tag)
            if tag == "p":
                out.append((0, f"<p>{inline(ch)}</p>", text_of(ch)))
            elif tag == "empty-line":
                out.append((0, '<p class="tb-c">&nbsp;</p>', ""))
            elif tag == "subtitle":
                out.append((0, f'<h4 class="tb-c">{inline(ch)}</h4>', text_of(ch)))
            elif tag == "image":
                name = href(ch)
                if name:
                    out.append((0, f'<p class="tb-c"><img src="../asset/{name}" alt=""></p>', ""))
            elif tag in ("epigraph", "cite", "annotation"):
                inner = []
                blocks_of(ch, inner)
                out.append((0, "<blockquote>" + "".join(h for _, h, _ in inner) + "</blockquote>",
                            " ".join(t for _, _, t in inner)))
            elif tag == "poem":
                inner = []
                for st in ch:
                    stag = _local(st.tag)
                    if stag == "stanza":
                        inner.append('<div class="tb-verse">' + "".join(
                            f'<p class="tb-v">{inline(v)}</p>' for v in st if _local(v.tag) == "v") + "</div>")
                    elif stag == "title":
                        inner.append(f'<h4 class="tb-c">{html.escape(text_of(st))}</h4>')
                    elif stag == "text-author":
                        inner.append(f'<p class="tb-r">{inline(st)}</p>')
                out.append((0, "".join(inner), text_of(ch)))
            elif tag == "text-author":
                out.append((0, f'<p class="tb-r">{inline(ch)}</p>', text_of(ch)))
            elif tag == "table":
                rows = []
                for tr in ch:
                    if _local(tr.tag) == "tr":
                        rows.append("<tr>" + "".join(f"<{_local(c.tag)}>{inline(c)}</{_local(c.tag)}>"
                                                     for c in tr if _local(c.tag) in ("td", "th")) + "</tr>")
                out.append((0, "<table>" + "".join(rows) + "</table>", text_of(ch)))

    titles, chapters, all_blocks = [], [], []

    def emit(title: str, blocks: list):
        if not title and not any(t or "<img" in h for _, h, t in blocks):
            return
        head = [(1, f'<h2 class="tb-h">{html.escape(title)}</h2>', title)] if title else []
        t2, c2 = _from_blocks(head + blocks)
        titles.extend(t2)
        chapters.extend(c2)
        all_blocks.extend(blocks)

    def section(el, fallback=""):
        title = ""
        for ch in el:
            if _local(ch.tag) == "title":
                title = text_of(ch)
                break
        own = []
        blocks_of(el, own)
        subs = [ch for ch in el if _local(ch.tag) == "section"]
        if own or not subs or title:
            emit(title or fallback, own)
        for s in subs:
            section(s)

    book_title = None
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "book-title" and book_title is None:
            book_title = text_of(el)
        elif tag == "coverpage" and cover is None:
            for im in el:
                cover = cover or href(im)
        elif tag == "body":
            break
    n_body = 0
    for el in root:
        if _local(el.tag) != "body":
            continue
        n_body += 1
        notes = bool(el.get("name"))
        subs = [ch for ch in el if _local(ch.tag) == "section"]
        if notes or not subs:
            section(el, "Notes" if notes else "")
            continue
        front = []
        blocks_of(el, front)
        head = next((text_of(ch) for ch in el if _local(ch.tag) == "title"), "")
        if head or front:
            emit(head, front)
        for s in subs:
            section(s)
    if not n_body:
        raise NotReadable("this file has no book in it")
    if not chapters:
        titles, chapters = [""], ["<p></p>"]
    return Book(book_title, titles, chapters, _latin(all_blocks), assets, cover)


# ── Word ──────────────────────────────────────────────────────────────────────────────
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _on(el) -> bool:
    return el is not None and (el.get(_W + "val") or "true").lower() not in ("0", "false", "off", "none")


def _docx(path: str) -> Book:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise NotReadable("this is not a Word document (an old .doc renamed, or a damaged file)")
    with zf:
        root = _xml(_zip_part(zf, "word/document.xml"))
        names = set(zf.namelist())
        heading = {}                  # style id -> level
        if "word/styles.xml" in names:
            for st in _xml(_zip_part(zf, "word/styles.xml")).iter(_W + "style"):
                nm = st.find(_W + "name")
                name = (nm.get(_W + "val") if nm is not None else "") or ""
                m = re.match(r"heading (\d)$", name, re.I)
                sid = st.get(_W + "styleId") or ""
                if m:
                    heading[sid] = int(m.group(1))
                elif name.lower() == "title":
                    heading[sid] = 1
        rels = {}
        if "word/_rels/document.xml.rels" in names:
            for r in _xml(_zip_part(zf, "word/_rels/document.xml.rels")):
                if (r.get("Type") or "").endswith("/image") and (r.get("TargetMode") or "") != "External":
                    target = os.path.normpath("word/" + (r.get("Target") or "")).replace("\\", "/").lstrip("/")
                    if (r.get("Target") or "").startswith("/"):
                        target = r.get("Target").lstrip("/")
                    if target in names:
                        rels[r.get("Id")] = target
        title = None
        if "docProps/core.xml" in names:
            try:
                for el in _xml(_zip_part(zf, "docProps/core.xml")):
                    if _local(el.tag) == "title" and (el.text or "").strip():
                        title = el.text.strip()[:200]
            except NotReadable:
                pass
    assets = {}

    def run(r) -> str:
        pr = r.find(_W + "rPr")
        open_, close = "", ""
        if pr is not None:
            va = pr.find(_W + "vertAlign")
            va = (va.get(_W + "val") if va is not None else "") or ""
            for tag, on in (("b", _on(pr.find(_W + "b"))), ("i", _on(pr.find(_W + "i"))),
                            ("u", _on(pr.find(_W + "u"))), ("s", _on(pr.find(_W + "strike"))),
                            ("sup", va == "superscript"), ("sub", va == "subscript")):
                if on:
                    open_ += f"<{tag}>"
                    close = f"</{tag}>" + close
        out = []
        for ch in r:
            tag = _local(ch.tag)
            if tag == "t":
                out.append(html.escape(ch.text or "", quote=False))
            elif tag == "tab":
                out.append("\u2003")
            elif tag in ("br", "cr"):
                out.append("<br>")
            elif tag == "noBreakHyphen":
                out.append("-")
            elif tag in ("drawing", "pict", "object"):
                for el in ch.iter():
                    rid = el.get(_R_ID + "embed") or (el.get(_R_ID + "id") if _local(el.tag) == "imagedata" else None)
                    if rid and rid in rels:
                        name = _safe_name(rels[rid].rsplit("/", 1)[-1])
                        assets[name] = ("zip", path, rels[rid])
                        out.append(f'<img src="../asset/{name}" alt="">')
        body = "".join(out)
        return (open_ + body + close) if body else ""

    def runs(el) -> str:
        out = []
        for ch in el:
            tag = _local(ch.tag)
            if tag == "r":
                out.append(run(ch))
            elif tag in ("hyperlink", "ins", "smartTag", "fldSimple", "sdtContent", "sdt", "customXml"):
                out.append(runs(ch))
        return "".join(out)

    def para(p):
        pr = p.find(_W + "pPr")
        lv, cls, bullet = 0, "", ""
        if pr is not None:
            st = pr.find(_W + "pStyle")
            lv = heading.get(st.get(_W + "val") if st is not None else "", 0)
            ol = pr.find(_W + "outlineLvl")
            if not lv and ol is not None and (ol.get(_W + "val") or "").isdigit() and int(ol.get(_W + "val")) < 6:
                lv = int(ol.get(_W + "val")) + 1
            jc = pr.find(_W + "jc")
            jc = (jc.get(_W + "val") if jc is not None else "") or ""
            cls = "tb-c" if jc == "center" else "tb-r" if jc in ("right", "end") else ""
            if pr.find(_W + "numPr") is not None and not lv:
                cls, bullet = "tb-li", "• "
        inner = runs(p)
        text = _plain(inner).strip()
        if not text and "<img" not in inner:
            return (0, "", "")
        if lv:
            return (lv, f'<h{min(lv + 1, 5)} class="tb-h">{inner}</h{min(lv + 1, 5)}>', text)
        if "<img" in inner and not text:
            cls = "tb-c"
        return (0, f'<p{" class=" + chr(34) + cls + chr(34) if cls else ""}>{bullet}{inner}</p>', text)

    def table(t) -> tuple:
        rows, texts = [], []
        for tr in t.iter(_W + "tr"):
            cells = []
            for tc in tr.findall(_W + "tc"):
                ps = [para(p) for p in tc.findall(_W + "p")]
                cells.append("<td>" + "".join(h for _, h, _ in ps) + "</td>")
                texts.extend(x for _, _, x in ps)
            rows.append("<tr>" + "".join(cells) + "</tr>")
        return (0, "<table>" + "".join(rows) + "</table>", " ".join(texts))

    blocks = []

    def walk(el):
        for ch in el:
            tag = _local(ch.tag)
            if tag == "p":
                b = para(ch)
                if b[1]:
                    blocks.append(b)
            elif tag == "tbl":
                blocks.append(table(ch))
            elif tag in ("sdt", "sdtContent", "customXml", "ins"):
                walk(ch)

    body = root.find(_W + "body")
    if body is None:
        raise NotReadable("this Word document has no text in it")
    walk(body)
    titles, chapters = _from_blocks(blocks)
    return Book(title, titles, chapters, _latin(blocks), assets)


# ── OpenDocument ──────────────────────────────────────────────────────────────────────
_ODF = "urn:oasis:names:tc:opendocument:xmlns:"
_T, _ST, _DR, _TB = "{" + _ODF + "text:1.0}", "{" + _ODF + "style:1.0}", "{" + _ODF + "drawing:1.0}", "{" + _ODF + "table:1.0}"
_FO = "{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}"
_XL = "{http://www.w3.org/1999/xlink}"


def _odt(path: str) -> Book:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise NotReadable("this is not an OpenDocument text (a damaged file)")
    with zf:
        root = _xml(_zip_part(zf, "content.xml"))
        names = set(zf.namelist())
        title = None
        if "meta.xml" in names:
            try:
                for el in _xml(_zip_part(zf, "meta.xml")).iter():
                    if _local(el.tag) == "title" and (el.text or "").strip():
                        title = el.text.strip()[:200]
            except NotReadable:
                pass
    look = {}                         # style name -> (tags, class)
    for st in root.iter(_ST + "style"):
        tags, cls = [], ""
        tp = st.find(_ST + "text-properties")
        if tp is not None:
            if tp.get(_FO + "font-weight") == "bold":
                tags.append("b")
            if tp.get(_FO + "font-style") == "italic":
                tags.append("i")
            if (tp.get(_ST + "text-underline-style") or "none") != "none":
                tags.append("u")
            pos = tp.get(_ST + "text-position") or ""
            if pos.startswith("super"):
                tags.append("sup")
            elif pos.startswith("sub"):
                tags.append("sub")
        pp = st.find(_ST + "paragraph-properties")
        if pp is not None:
            al = pp.get(_FO + "text-align") or ""
            cls = "tb-c" if al == "center" else "tb-r" if al in ("end", "right") else ""
        look[st.get(_ST + "name")] = (tags, cls)
    assets = {}

    def inline(el) -> str:
        out = [html.escape(el.text or "", quote=False)]
        for ch in el:
            tag = ch.tag
            if tag == _T + "span":
                tags = look.get(ch.get(_T + "style-name"), ([], ""))[0]
                out.append("".join(f"<{t}>" for t in tags) + inline(ch) + "".join(f"</{t}>" for t in reversed(tags)))
            elif tag == _T + "s":
                c = ch.get(_T + "c") or "1"
                out.append("\u00a0" * min(int(c) if c.isdigit() else 1, 40))
            elif tag == _T + "tab":
                out.append("\u2003")
            elif tag == _T + "line-break":
                out.append("<br>")
            elif tag == _DR + "frame":
                for im in ch.iter(_DR + "image"):
                    ref = (im.get(_XL + "href") or "").lstrip("./")
                    if ref in names:
                        name = _safe_name(ref.rsplit("/", 1)[-1])
                        assets[name] = ("zip", path, ref)
                        out.append(f'<img src="../asset/{name}" alt="">')
                        break
            elif tag in (_T + "note", _T + "bookmark", _T + "bookmark-start", _T + "bookmark-end", _T + "soft-page-break"):
                pass
            else:
                out.append(inline(ch))
            out.append(html.escape(ch.tail or "", quote=False))
        return "".join(out)

    blocks = []

    def para(el, cls="", bullet=""):
        inner = inline(el)
        text = _plain(inner).strip()
        if not text and "<img" not in inner:
            return None
        cls = cls or look.get(el.get(_T + "style-name"), ([], ""))[1]
        if "<img" in inner and not text:
            cls = "tb-c"
        return (0, f'<p{" class=" + chr(34) + cls + chr(34) if cls else ""}>{bullet}{inner}</p>', text)

    def walk(el, in_list=False):
        for ch in el:
            tag = ch.tag
            if tag == _T + "h":
                lv = ch.get(_T + "outline-level") or "1"
                lv = min(int(lv), 6) if lv.isdigit() else 1
                inner = inline(ch)
                if _plain(inner).strip():
                    blocks.append((lv, f'<h{min(lv + 1, 5)} class="tb-h">{inner}</h{min(lv + 1, 5)}>', _plain(inner).strip()))
            elif tag == _T + "p":
                b = para(ch, "tb-li" if in_list else "", "• " if in_list else "")
                if b:
                    blocks.append(b)
            elif tag == _T + "list":
                for item in ch:
                    walk(item, True)
            elif tag == _TB + "table":
                rows, texts = [], []
                for tr in ch.iter(_TB + "table-row"):
                    cells = []
                    for tc in tr.findall(_TB + "table-cell"):
                        ps = [para(p) for p in tc.findall(_T + "p")]
                        cells.append("<td>" + "".join(b[1] for b in ps if b) + "</td>")
                        texts.extend(b[2] for b in ps if b)
                    rows.append("<tr>" + "".join(cells) + "</tr>")
                blocks.append((0, "<table>" + "".join(rows) + "</table>", " ".join(texts)))
            elif tag == _T + "section":
                walk(ch, in_list)
            # text:table-of-content and the indexes are left out: the reader has its own

    text_el = None
    for el in root.iter():
        if _local(el.tag) == "text" and el.tag.startswith("{" + _ODF + "office:1.0}"):
            text_el = el
            break
    if text_el is None:
        raise NotReadable("this OpenDocument file is not a text (a spreadsheet or a drawing), or it is protected")
    walk(text_el)
    titles, chapters = _from_blocks(blocks)
    return Book(title, titles, chapters, _latin(blocks), assets)


# ── Rich Text ─────────────────────────────────────────────────────────────────────────
_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header", "footer", "headerl", "headerr",
             "headerf", "footerl", "footerr", "footerf", "footnote", "generator", "datastore", "themedata",
             "colorschememapping", "latentstyles", "listtable", "listoverridetable", "rsidtbl", "xmlnstbl", "mmathPr",
             "pgdsctbl", "fldinst", "nonshppict", "shp", "shpinst", "bkmkstart", "bkmkend", "filetbl", "revtbl",
             "template", "private", "falt", "panose", "wgrffmtfilter", "xe", "tc", "annotation", "atnid", "atnauthor"}
_RTF_CHAR = {"emdash": "\u2014", "endash": "\u2013", "lquote": "\u2018", "rquote": "\u2019", "ldblquote": "\u201c",
             "rdblquote": "\u201d", "bullet": "\u2022", "tab": "\u2003", "emspace": "\u2003", "enspace": "\u2002",
             "cell": " \u2003", "line": "\n"}
_RTF_PAR = {"par", "sect", "page", "row"}
_RTF_TOKEN = re.compile(rb"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z'])|([{}])|([^\\{}]+)", re.S)


def rtf_paragraphs(data: bytes) -> list:
    """The paragraphs of an RTF document as plain text."""
    if not data.lstrip()[:5].startswith(b"{\\rtf"):
        raise NotReadable("this is not a Rich Text document")
    codec = "cp1252"
    paras, cur, pend = [], [], bytearray()
    stack, skip, uc, to_skip, star = [], False, 1, 0, False

    def flush():
        if pend:
            cur.append(pend.decode(codec, "replace"))
            pend.clear()

    def end_par():
        flush()
        text = "".join(cur).strip()
        cur.clear()
        if text:
            paras.extend(x.strip() for x in text.split("\n") if x.strip())

    for m in _RTF_TOKEN.finditer(data):
        word, num, hx, sym, brace, text = m.groups()
        if brace == b"{":
            flush()
            stack.append((skip, uc))
            star = False
            continue
        if brace == b"}":
            flush()
            if stack:
                skip, uc = stack.pop()
            star = False
            continue
        if sym is not None:
            if sym == b"*":
                star = True
            elif not skip:
                if sym in (b"\\", b"{", b"}"):
                    if to_skip:
                        to_skip -= 1
                    else:
                        pend.extend(sym)
                elif sym == b"~":
                    flush()
                    cur.append("\u00a0")
                elif sym == b"_":
                    pend.extend(b"-")
                elif sym in (b"\n", b"\r"):
                    end_par()
            continue
        if word is not None:
            w = word.decode("ascii")
            if star or w in _RTF_SKIP:
                if star and w not in ("shppict",) or w in _RTF_SKIP:
                    skip = True
                star = False
                if skip:
                    continue
            star = False
            if w == "ansicpg" and num:
                try:
                    codecs.lookup("cp" + num.decode())
                    codec = "cp" + num.decode()
                except LookupError:
                    pass
            elif w == "uc" and num:
                uc = max(0, min(int(num), 8))
            elif skip:
                continue
            elif w == "u" and num:
                flush()
                n = int(num)
                n = n + 65536 if n < 0 else n
                if 0xd800 <= n <= 0xdbff:
                    cur.append(chr(n))          # the first half of a pair: joined below
                elif 0xdc00 <= n <= 0xdfff and cur and len(cur[-1]) == 1 and 0xd800 <= ord(cur[-1]) <= 0xdbff:
                    hi = ord(cur.pop())
                    cur.append(chr(0x10000 + ((hi - 0xd800) << 10) + (n - 0xdc00)))
                elif n < 0x110000 and not 0xd800 <= n <= 0xdfff:
                    cur.append(chr(n))
                to_skip = uc
            elif w in _RTF_PAR:
                end_par()
            elif w in _RTF_CHAR:
                flush()
                cur.append(_RTF_CHAR[w])
            continue
        if skip:
            continue
        if hx is not None:
            if to_skip:
                to_skip -= 1
            else:
                pend.append(int(hx, 16))
            continue
        if text is not None:
            t = text.replace(b"\r", b"").replace(b"\n", b"")
            if to_skip:
                cut = min(to_skip, len(t))
                t, to_skip = t[cut:], to_skip - cut
            pend.extend(t)
    end_par()
    return [p for p in ("".join(c for c in x if not 0xd800 <= ord(c) <= 0xdfff) for x in paras) if p]


def _rtf(data: bytes) -> Book:
    paras = rtf_paragraphs(data)
    blocks = [(0, f"<p>{html.escape(p, quote=False)}</p>", p) for p in paras]
    titles, chapters = _from_blocks(blocks)
    return Book(None, titles, chapters, _latin(blocks))


# ── Mobipocket ────────────────────────────────────────────────────────────────────────
def palmdoc(data: bytes) -> bytes:
    """Undo PalmDOC compression (a byte-wise LZ77 with an 11-bit distance)."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        i += 1
        if c == 0 or 9 <= c <= 0x7f:
            out.append(c)
        elif c <= 8:
            out += data[i:i + c]
            i += c
        elif c >= 0xc0:
            out.append(0x20)
            out.append(c ^ 0x80)
        else:
            if i >= n:
                break
            v = ((c << 8) | data[i]) & 0x3fff
            i += 1
            dist, length = v >> 3, (v & 7) + 3
            if dist == 0 or dist > len(out):
                continue
            for _ in range(length):
                out.append(out[-dist])
    return bytes(out)


def _trailing(data: bytes, flags: int) -> bytes:
    """A text record without what is appended to it (the extras a set bit of `flags` announces)."""
    for bit in range(15, 0, -1):
        if flags & (1 << bit) and data:
            size = 0
            for b in data[-4:]:
                if b & 0x80:
                    size = 0
                size = (size << 7) | (b & 0x7f)
            if 0 < size <= len(data):
                data = data[:-size]
    if flags & 1 and data:
        size = (data[-1] & 3) + 1
        data = data[:-size]
    return data


_MOBI_BREAK = re.compile(rb"<\s*/?\s*mbp:pagebreak[^>]*>", re.I)
_MOBI_BIG = 150 * 1024


def _mobi(path: str) -> Book:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(78)
        if len(head) < 78 or head[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
            raise NotReadable("this is not a Mobipocket book (it may be another format under this name)")
        count = struct.unpack(">H", head[76:78])[0]
        table = f.read(count * 8)
        if count < 2 or len(table) < count * 8:
            raise NotReadable("this book is cut short")
        offs = [struct.unpack(">L", table[i * 8:i * 8 + 4])[0] for i in range(count)] + [size]
        if any(offs[i] > offs[i + 1] for i in range(count)) or offs[0] < 78:
            raise NotReadable("this book is damaged (its list of parts is out of order)")

        def rec(i: int) -> bytes:
            f.seek(offs[i])
            return f.read(offs[i + 1] - offs[i])

        r0 = rec(0)
        if len(r0) < 16:
            raise NotReadable("this book is cut short")
        comp, _, text_len, n_text, _, crypt = struct.unpack(">HHLHHH", r0[:14])
        if crypt:
            raise NotReadable("this book is protected (DRM), and only the reader it was bought for can open it")
        if comp == 17480:
            raise NotReadable("this book is packed in a way not read here (HUFF/CDIC)")
        if comp not in (1, 2):
            raise NotReadable("this book is packed in a way not known here")
        codec, flags, first_img, title, cover, version = "cp1252", 0, None, None, None, 0
        kf8, fdst, flow_end = False, None, None
        if head[60:68] == b"BOOKMOBI" and r0[16:20] == b"MOBI" and len(r0) >= 0x74:
            mlen = struct.unpack(">L", r0[20:24])[0]
            if struct.unpack(">L", r0[28:32])[0] == 65001:
                codec = "utf-8"
            version = struct.unpack(">L", r0[0x68:0x6c])[0]
            first_img = struct.unpack(">L", r0[0x6c:0x70])[0]
            name_off, name_len = struct.unpack(">LL", r0[0x54:0x5c])
            if 0 < name_len < 1000 and name_off + name_len <= len(r0):
                title = r0[name_off:name_off + name_len].decode(codec, "replace").strip() or None
            if mlen >= 0xe4 and version >= 5 and len(r0) >= 0xf4:
                flags = struct.unpack(">H", r0[0xf2:0xf4])[0]
            kf8 = version >= 8
            if kf8 and len(r0) >= 0xc4:
                fdst = struct.unpack(">L", r0[0xc0:0xc4])[0]
            exth_at = 16 + mlen
            if len(r0) >= 0x84 and struct.unpack(">L", r0[0x80:0x84])[0] & 0x40 and r0[exth_at:exth_at + 4] == b"EXTH":
                n = struct.unpack(">L", r0[exth_at + 8:exth_at + 12])[0]
                p = exth_at + 12
                for _ in range(min(n, 500)):
                    if p + 8 > len(r0):
                        break
                    kind, ln = struct.unpack(">LL", r0[p:p + 8])
                    if ln < 8:
                        break
                    val = r0[p + 8:p + ln]
                    if kind == 503 and val:
                        title = val.decode(codec, "replace").strip() or title
                    elif kind == 201 and len(val) == 4:
                        cover = struct.unpack(">L", val)[0]
                    p += ln
        if n_text < 1 or n_text >= count:
            raise NotReadable("this book is damaged (it names more text than it holds)")
        if text_len > TEXT_MOST:
            raise NotReadable("this book is too large to open here")
        parts, got = [], 0
        for i in range(1, n_text + 1):
            d = _trailing(rec(i), flags)
            d = palmdoc(d) if comp == 2 else d
            parts.append(d)
            got += len(d)
            if got > TEXT_MOST:
                raise NotReadable("this book is too large to open here")
        raw = b"".join(parts)[:text_len] if text_len else b"".join(parts)
        if kf8 and fdst is not None and n_text < fdst < count:
            # a KF8 book holds several "flows" one after the other: the pages first, then
            # style sheets and drawings. This record says where the first one ends.
            fd = rec(fdst)
            if fd[:4] == b"FDST" and len(fd) >= 20 and struct.unpack(">L", fd[8:12])[0] >= 1:
                a, b = struct.unpack(">LL", fd[12:20])
                if a == 0 and 0 < b <= len(raw):
                    flow_end = b
        # the pictures: every record after the text that begins like one
        assets = {}
        if first_img and n_text < first_img < count:
            for i in range(first_img, count):
                f.seek(offs[i])
                if picture_type(f.read(32) + b"\0" * 26):
                    assets[f"i{i - first_img + 1}"] = ("file", path, offs[i], offs[i + 1] - offs[i])
    # where the pages break; a book without breaks, and a piece too long, is cut at headings
    if kf8:
        # each page of the book is a file of XHTML: an empty shell, then what belongs in it
        raw = raw[:flow_end] if flow_end else raw
        starts = sorted({0} | {m.start() for m in re.finditer(rb"<html[\s>]", raw, re.I)})
    else:
        starts = [0] + [m.end() for m in _MOBI_BREAK.finditer(raw)]
    cuts = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(raw)
        if e - s <= _MOBI_BIG:
            cuts.append(s)
            continue
        inner = [s] + [m.start() for m in re.finditer(rb"<h[12][\s>]", raw[s:e], re.I) if m.start() > 0]
        inner = [s] + [s + x for x in inner[1:]]
        for j, a in enumerate(inner):
            b = inner[j + 1] if j + 1 < len(inner) else e
            cuts.append(a)
            while b - a > _MOBI_BIG:
                at = raw.find(b"</p>", a + _MOBI_BIG // 2, b)
                if at < 0:
                    break
                a = at + 4
                cuts.append(a)
    cuts = sorted(set(cuts))
    # titles: the book's own list of contents says where chapters begin (byte positions)
    named = {}
    g = re.search(rb"<reference[^>]*type=[\"']?toc[\"']?[^>]*filepos=[\"']?(\d+)|<reference[^>]*filepos=[\"']?(\d+)[^>]*type=[\"']?toc",
                  raw, re.I)
    if g:
        toc_at = int(g.group(1) or g.group(2))
        k = bisect.bisect_right(cuts, toc_at) - 1
        toc_end = cuts[k + 1] if k + 1 < len(cuts) else len(raw)
        for a in re.finditer(rb"<a\b[^>]*filepos=[\"']?(\d+)[^>]*>(.*?)</a>", raw[toc_at:toc_end][:600000], re.I | re.S):
            label = re.sub(r"\s+", " ", _plain(a.group(2).decode(codec, "replace"))).strip()[:120]
            if label:
                named.setdefault(bisect.bisect_right(cuts, int(a.group(1))) - 1, label)

    def picture(a):
        v = a.get("recindex") or ""
        if v.isdigit() and f"i{int(v)}" in assets:
            return f"../asset/i{int(v)}"
        m = re.match(r"kindle:embed:([0-9A-Va-v]{1,6})", a.get("src") or "")       # KF8: the number in base 32
        if m and f"i{int(m.group(1), 32)}" in assets:
            return f"../asset/i{int(m.group(1), 32)}"
        return None

    titles, chapters, sample = [], [], []
    for k, s in enumerate(cuts):
        e = cuts[k + 1] if k + 1 < len(cuts) else len(raw)
        piece = raw[s:e].decode(codec, "replace")
        piece = re.sub(r"<\s*/?\s*mbp:pagebreak[^>]*>", "", piece, flags=re.I)
        piece = re.sub(r"</?(?:html|body)\b[^>]*>", "", piece, flags=re.I)
        body = clean(piece, picture)
        text = _plain(body).strip()
        if not text and "<img" not in body:
            continue
        name = named.get(k, "")
        if not name:
            h = re.search(r"<h[1-3]\b[^>]*>(.*?)</h[1-3]>", body, re.S)
            if h:
                name = re.sub(r"\s+", " ", _plain(h.group(1))).strip()[:120]
        titles.append(name)
        chapters.append(body)
        if len(sample) < 40:
            sample.append(text[:2000])
    if not chapters:
        raise NotReadable("this book has no text in it")
    cover_name = f"i{cover + 1}" if cover is not None and f"i{cover + 1}" in assets else None
    return Book(title, titles, chapters, textbook.is_latin(sample), assets, cover_name)


# ── a saved page ──────────────────────────────────────────────────────────────────────
_HTML_CUT = re.compile(r"(?=<h[1-6]>)|(?<=</h[1-6]>)|(?<=</p>)|(?<=</table>)|(?<=</blockquote>)|(?<=</pre>)"
                       r"|(?<=</ul>)|(?<=</ol>)")


def _html(data: bytes) -> Book:
    m = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", data[:4096], re.I)
    text = None
    if m and data[:3] != b"\xef\xbb\xbf" and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        try:
            name = m.group(1).decode("ascii").lower()
            text = data.decode("gb18030" if name in ("gb2312", "gbk") else name)
        except (LookupError, UnicodeDecodeError):
            text = None
    if text is None:
        text = textbook.decode(data)[0]
    t = re.search(r"<title[^>]*>(.*?)</title>", text[:20000], re.I | re.S)
    title = re.sub(r"\s+", " ", html.unescape(t.group(1))).strip()[:200] if t else None
    body = clean(text)
    # cut before every heading and after every paragraph, so chapters can be made of the pieces
    blocks = []
    for piece in _HTML_CUT.split(body):
        if not piece or not piece.strip():
            continue
        h = re.match(r"\s*<h([1-6])>", piece)
        plain = _plain(piece).strip()
        if not plain and "<img" not in piece:
            continue
        blocks.append((int(h.group(1)) if h else 0, piece, re.sub(r"\s+", " ", plain)))
    titles, chapters = _from_blocks(blocks)
    return Book(title, titles, chapters, _latin(blocks))


# ── Windows' compiled help ────────────────────────────────────────────────────────────
_CHM_MOST = 400 * 1024 * 1024
_CHM_PAGE = re.compile(r"\.(html?|xhtml)$", re.I)
_CHM_PICTURE = re.compile(r"\.(jpe?g|png|gif|bmp|webp)$", re.I)


def _decode_page(data: bytes) -> str:
    m = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", data[:4096], re.I)
    if m and data[:3] != b"\xef\xbb\xbf" and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        try:
            name = m.group(1).decode("ascii").lower()
            return data.decode("gb18030" if name in ("gb2312", "gbk") else name)
        except (LookupError, UnicodeDecodeError):
            pass
    return textbook.decode(data)[0]


def _page_blocks(body: str) -> list:
    blocks = []
    for piece in _HTML_CUT.split(body):
        if not piece or not piece.strip():
            continue
        h = re.match(r"\s*<h([1-6])>", piece)
        plain = _plain(piece).strip()
        if plain or "<img" in piece:
            blocks.append((int(h.group(1)) if h else 0, piece, re.sub(r"\s+", " ", plain)))
    return blocks


def _chm(path: str) -> Book:
    import shutil
    import tempfile
    import unpack
    if not unpack.have("7zz"):
        raise NotReadable("the program that opens .chm files is not part of this install yet")
    if unpack.magic(path, 4) != unpack.CHM_MAGIC:
        raise NotReadable("this is not a compiled help file (.chm)")
    try:
        listing = unpack.seven_list(path)
    except (OSError, ValueError) as e:
        raise NotReadable(f"this .chm could not be opened ({e})")
    if sum(sz for _, sz in listing) > _CHM_MOST:
        raise NotReadable("this .chm is too large to open here")
    members = {n.lstrip("/").lower(): n.lstrip("/") for n, _ in listing}
    tmp = tempfile.mkdtemp(prefix="chm-")
    try:
        try:
            unpack.seven_extract(path, tmp)
        except (OSError, ValueError) as e:
            raise NotReadable(f"this .chm could not be unpacked ({e})")
        on_disk = {}
        for base, _, files in os.walk(tmp):
            for f in files:
                full = os.path.join(base, f)
                if not os.path.islink(full):
                    on_disk[os.path.relpath(full, tmp).replace("\\", "/").lower()] = full

        def read(rel: str):
            full = on_disk.get(rel.lstrip("/").lower())
            if not full or os.path.getsize(full) > XML_MOST:
                return None
            with open(full, "rb") as f:
                return f.read()

        # the order of the pages and their names: the contents file of the help
        order, seen = [], set()
        for rel in sorted(on_disk):
            if rel.endswith(".hhc"):
                toc = _decode_page(read(rel) or b"")
                for obj in re.finditer(r"<object\b[^>]*>(.*?)</object>", toc, re.I | re.S):
                    name = re.search(r"<param\s+name=[\"']?Name[\"']?\s+value=\"([^\"]*)\"", obj.group(1), re.I)
                    local = re.search(r"<param\s+name=[\"']?Local[\"']?\s+value=\"([^\"]*)\"", obj.group(1), re.I)
                    if not local:
                        continue
                    target = os.path.normpath(os.path.join(os.path.dirname(rel), html.unescape(local.group(1)).split("#")[0])
                                              ).replace("\\", "/").lstrip("/").lower()
                    if target in on_disk and target not in seen and _CHM_PAGE.search(target):
                        seen.add(target)
                        order.append((target, html.unescape(name.group(1)).strip()[:120] if name else ""))
                break
        for rel in sorted(on_disk):          # pages the contents do not name come after, in order of name
            if _CHM_PAGE.search(rel) and rel not in seen:
                order.append((rel, ""))
        if not order:
            raise NotReadable("this .chm has no pages in it")
        assets, numbers = {}, {}
        titles, chapters, sample, book_title = [], [], [], None
        for rel, name in order[:5000]:
            data = read(rel)
            if not data:
                continue
            text = _decode_page(data)
            if book_title is None:
                t = re.search(r"<title[^>]*>(.*?)</title>", text[:20000], re.I | re.S)
                book_title = re.sub(r"\s+", " ", html.unescape(t.group(1))).strip()[:200] if t else ""

            def picture(a, rel=rel):
                src = (a.get("src") or "").split("?")[0].split("#")[0]
                if not src or re.match(r"[a-z][a-z0-9+.\-]*:", src, re.I):
                    return None
                target = os.path.normpath(os.path.join(os.path.dirname(rel), src.replace("\\", "/"))).replace("\\", "/").lstrip("/").lower()
                if target not in members or not _CHM_PICTURE.search(target):
                    return None
                if target not in numbers:
                    numbers[target] = f"c{len(numbers) + 1}"
                    assets[numbers[target]] = ("7z", path, members[target])
                return "../asset/" + numbers[target]

            body = clean(text, picture)
            blocks = _page_blocks(body)
            if not blocks:
                continue
            if not name:
                h = next((t for lv, _, t in blocks if lv), "")
                name = h[:120]
            if sum(len(t) for _, _, t in blocks) > textbook.CHAPTER_MOST:
                t2, c2 = _from_blocks([(0, h, t) for _, h, t in blocks])
                titles.extend([name] + [f"{name} ({k + 2})" if name else "" for k in range(len(c2) - 1)])
                chapters.extend(c2)
            else:
                titles.append(name)
                chapters.append("".join(h for _, h, _ in blocks))
            if len(sample) < 40:
                sample.append(" ".join(t for _, _, t in blocks)[:2000])
        if not chapters:
            raise NotReadable("this .chm has no text in it")
        return Book(book_title or None, titles, chapters, textbook.is_latin(sample), assets)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── slides and sheets ─────────────────────────────────────────────────────────────────
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
SLIDES_MOST = 2000
SHEET_ROWS_MOST = 3000          # of one sheet; what lies below is said to be left out
SHEET_COLS_MOST = 60
SHEET_ROWS_A_PAGE = 120


def _rels(zf, names, part: str) -> dict:
    """What a part of an Office file points at inside the file: id -> (kind, path)."""
    d, f = part.rsplit("/", 1) if "/" in part else ("", part)
    rp = (d + "/" if d else "") + "_rels/" + f + ".rels"
    out = {}
    if rp not in names:
        return out
    for r in _xml(_zip_part(zf, rp)):
        t = r.get("Target") or ""
        if (r.get("TargetMode") or "") == "External" or not t:
            continue
        path = t.lstrip("/") if t.startswith("/") else os.path.normpath((d + "/" if d else "") + t).replace("\\", "/")
        out[r.get("Id")] = ((r.get("Type") or "").rsplit("/", 1)[-1], path.lstrip("/"))
    return out


def _core_title(zf, names):
    if "docProps/core.xml" in names:
        try:
            for el in _xml(_zip_part(zf, "docProps/core.xml")):
                if _local(el.tag) == "title" and (el.text or "").strip():
                    return el.text.strip()[:200]
        except NotReadable:
            pass
    return None


def _a_para(p) -> str:
    """One paragraph of a slide's text."""
    out = []
    for ch in p:
        tag = _local(ch.tag)
        if tag in ("r", "fld"):
            t = ch.find(_A + "t")
            text = html.escape((t.text if t is not None else "") or "", quote=False)
            pr = ch.find(_A + "rPr")
            if text and pr is not None:
                if pr.get("b") in ("1", "true"):
                    text = f"<b>{text}</b>"
                if pr.get("i") in ("1", "true"):
                    text = f"<i>{text}</i>"
            out.append(text)
        elif tag == "br":
            out.append("<br>")
    return "".join(out)


def _pptx(path: str) -> Book:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise NotReadable("this is not a presentation of the newer kind (an old .ppt renamed, or a damaged file)")
    assets, titles, chapters, sample = {}, [], [], []
    with zf:
        names = set(zf.namelist())
        pres = _xml(_zip_part(zf, "ppt/presentation.xml"))
        rels = _rels(zf, names, "ppt/presentation.xml")
        order = []
        for sid in pres.iter(_P + "sldId"):
            got = rels.get(sid.get(_R_ID + "id"))
            if got and got[0] == "slide" and got[1] in names:
                order.append(got[1])
        if not order:
            order = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                           key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        if not order:
            raise NotReadable("this presentation has no slides in it")
        title = _core_title(zf, names)
        for num, part in enumerate(order[:SLIDES_MOST], 1):
            root = _xml(_zip_part(zf, part))
            srels = _rels(zf, names, part)
            head, body = "", []

            def shapes(el):
                nonlocal head
                for sh in el:
                    tag = _local(sh.tag)
                    if tag == "grpSp":
                        shapes(sh)
                    elif tag == "sp":
                        ph = sh.find(f"{_P}nvSpPr/{_P}nvPr/{_P}ph")
                        kind = ph.get("type") if ph is not None else ""
                        if kind in ("sldNum", "dt", "ftr", "hdr"):
                            continue
                        paras = [x for x in (_a_para(p) for p in sh.iter(_A + "p")) if _plain(x).strip()]
                        if kind in ("title", "ctrTitle") and not head and paras:
                            head = re.sub(r"\s+", " ", _plain(" ".join(paras))).strip()[:120]
                        else:
                            cls = ' class="tb-c"' if kind == "subTitle" else ' class="tb-li"'
                            body.extend(f"<p{cls}>{x}</p>" for x in paras)
                    elif tag == "pic":
                        for bl in sh.iter(_A + "blip"):
                            got = srels.get(bl.get(_R_ID + "embed"))
                            if got and got[1] in names:
                                name = _safe_name(got[1].rsplit("/", 1)[-1])
                                assets[name] = ("zip", path, got[1])
                                body.append(f'<p class="tb-c"><img src="../asset/{name}" alt=""></p>')
                            break
                    elif tag == "graphicFrame":
                        for tbl in sh.iter(_A + "tbl"):
                            rows = ["<tr>" + "".join(
                                "<td>" + "<br>".join(x for x in (_a_para(p) for p in tc.iter(_A + "p")) if x) + "</td>"
                                for tc in tr.findall(_A + "tc")) + "</tr>" for tr in tbl.findall(_A + "tr")]
                            body.append("<table>" + "".join(rows) + "</table>")

            tree = root.find(f"{_P}cSld/{_P}spTree")
            if tree is not None:
                shapes(tree)
            shown = head or f"Slide {num}"
            titles.append(f"{num}. {head}" if head else f"Slide {num}")
            chapters.append(f'<h2 class="tb-h">{html.escape(shown)}</h2>' + "\n".join(body))
            if len(sample) < 60:
                sample.append(_plain(shown + " " + " ".join(body))[:1500])
    return Book(title, titles, chapters, textbook.is_latin(sample), assets)


_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59))


def _sheet_number(v: str, is_date: bool, epoch_1904: bool) -> str:
    try:
        x = float(v)
    except ValueError:
        return v
    if is_date and 0 <= x < 2958466:
        import datetime
        base = datetime.datetime(1904, 1, 1) if epoch_1904 else datetime.datetime(1899, 12, 30)
        t = base + datetime.timedelta(days=x)
        t = (t + datetime.timedelta(microseconds=500000)).replace(microsecond=0)
        if x < 1:
            return t.strftime("%H:%M:%S")
        return t.strftime("%Y-%m-%d") if (t.hour, t.minute, t.second) == (0, 0, 0) else t.strftime("%Y-%m-%d %H:%M:%S")
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return format(x, ".10g")


def _xlsx(path: str) -> Book:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise NotReadable("this is not a spreadsheet of the newer kind (an old .xls renamed, or a damaged file)")
    titles, chapters, sample = [], [], []
    with zf:
        names = set(zf.namelist())
        wb = _xml(_zip_part(zf, "xl/workbook.xml"))
        rels = _rels(zf, names, "xl/workbook.xml")
        title = _core_title(zf, names)
        epoch_1904 = any(el.get("date1904") in ("1", "true") for el in wb.iter(_S + "workbookPr"))
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in _xml(_zip_part(zf, "xl/sharedStrings.xml")):
                bits = []
                for ch in si:
                    if ch.tag == _S + "t":
                        bits.append(ch.text or "")
                    elif ch.tag == _S + "r":
                        bits.extend(t.text or "" for t in ch.findall(_S + "t"))
                shared.append("".join(bits))
        dates = []                    # for each cell style: does it show a date?
        if "xl/styles.xml" in names:
            st = _xml(_zip_part(zf, "xl/styles.xml"))
            custom = {}
            for nf in st.iter(_S + "numFmt"):
                code = re.sub(r"\"[^\"]*\"|\[[^\]]*\]|\\.", "", nf.get("formatCode") or "")
                custom[nf.get("numFmtId")] = bool(re.search(r"[ymdhs]", code, re.I)) and "general" not in code.lower()
            xfs = st.find(_S + "cellXfs")
            for xf in (xfs if xfs is not None else []):
                fid = xf.get("numFmtId") or "0"
                dates.append(custom.get(fid, fid.isdigit() and int(fid) in _DATE_FORMATS))
        sheets = []
        for sh in wb.iter(_S + "sheet"):
            got = rels.get(sh.get(_R_ID + "id"))
            if got and got[1] in names and got[0] == "worksheet":
                sheets.append((sh.get("name") or f"Sheet {len(sheets) + 1}", got[1]))
        if not sheets:
            raise NotReadable("this spreadsheet has no sheets of cells in it")
        for name, part in sheets[:200]:
            root = _xml(_zip_part(zf, part))
            rows, more = [], False
            for row in root.iter(_S + "row"):
                if len(rows) >= SHEET_ROWS_MOST:
                    more = True
                    break
                cells = {}
                for k, c in enumerate(row.findall(_S + "c")):
                    ref = re.match(r"([A-Z]{1,3})", c.get("r") or "")
                    col = k
                    if ref:
                        col = 0
                        for letter in ref.group(1):
                            col = col * 26 + (ord(letter) - 64)
                        col -= 1
                    if col >= SHEET_COLS_MOST:
                        continue
                    kind = c.get("t") or "n"
                    v = c.find(_S + "v")
                    raw = (v.text if v is not None else "") or ""
                    if kind == "s":
                        text = shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else ""
                    elif kind == "inlineStr":
                        text = "".join(t.text or "" for t in c.iter(_S + "t"))
                    elif kind == "b":
                        text = "TRUE" if raw == "1" else "FALSE"
                    elif kind in ("str", "e") or not raw:
                        text = raw
                    else:
                        style = c.get("s") or ""
                        text = _sheet_number(raw, style.isdigit() and int(style) < len(dates) and dates[int(style)], epoch_1904)
                    if text.strip():
                        cells[col] = text.strip()[:500]
                if cells:
                    rows.append(cells)
            if not rows:
                continue
            width = max(max(r) for r in rows) + 1
            for at in range(0, len(rows), SHEET_ROWS_A_PAGE):
                part_rows = rows[at:at + SHEET_ROWS_A_PAGE]
                body = "".join("<tr>" + "".join(f"<td>{html.escape(r.get(c, ''), quote=False)}</td>" for c in range(width)) + "</tr>"
                               for r in part_rows)
                n = at // SHEET_ROWS_A_PAGE
                titles.append(name if n == 0 else f"{name} ({n + 1})")
                note = ""
                if more and at + SHEET_ROWS_A_PAGE >= len(rows):
                    note = f'<p class="tb-c">This sheet goes on: only its first {SHEET_ROWS_MOST} rows are shown here.</p>'
                chapters.append(f'<h2 class="tb-h">{html.escape(titles[-1])}</h2><div class="tb-wide"><table class="tb-sheet">{body}</table></div>{note}')
            if len(sample) < 60:
                sample.append(" ".join(" ".join(r.values()) for r in rows[:40])[:2000])
    if not chapters:
        raise NotReadable("this spreadsheet is empty")
    return Book(title, titles, chapters, textbook.is_latin(sample), {})


# ── one way in ────────────────────────────────────────────────────────────────────────
def kind_of(name: str) -> str:
    """'fb2' for 'War and Peace.FB2'; '' for what is not read here."""
    e = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    return e if e in KINDS else ""


def build(path: str, kind: str) -> Book:
    if kind == "txt":
        b = textbook.load(path)
        return Book(None, b["titles"], b["chapters"], b["latin"])
    if kind in ("mobi", "azw", "azw3", "prc"):
        return _mobi(path)
    if kind == "chm":
        return _chm(path)
    if kind == "docx":
        return _docx(path)
    if kind in ("pptx", "ppsx"):
        return _pptx(path)
    if kind in ("xlsx", "xlsm"):
        return _xlsx(path)
    if kind == "odt":
        return _odt(path)
    if os.path.getsize(path) > XML_MOST * 2:
        raise NotReadable("this document is too large to open here")
    with open(path, "rb") as f:
        data = f.read()
    if kind == "fb2":
        return _fb2(data)
    if kind == "rtf":
        return _rtf(data)
    if kind in ("html", "htm", "xhtml"):
        return _html(data)
    raise NotReadable("not a kind of book read here")


def load(path: str, kind: str) -> Book:
    """The book of a file, kept for the next chapter asked for. Raises NotReadable."""
    mtime = os.path.getmtime(path)
    hit = _CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        book = build(path, kind)
    except NotReadable:
        raise
    except (struct.error, IndexError, ValueError, KeyError, zipfile.BadZipFile, RecursionError, MemoryError) as e:
        raise NotReadable(f"this book is damaged ({type(e).__name__})")
    while len(_CACHE) >= _CACHE_MOST:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[path] = (mtime, book)
    return book
