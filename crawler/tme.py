"""Reading a public channel through Telegram's own web preview (t.me/s/<name>).

No account, no username lookup, no FloodWait: it is a web page. tgarr reads
followed channels this way FIRST and keeps the account for what only the
account can do - handing over a file.

What the page gives, per post: its id and time, the text, and for each
attachment its kind; a file's name and (rounded) size; a video's running
time. What it does not give: the file's identity (file_unique_id), its data
centre, anything needed to download it - and nothing at all for channels
Telegram does not preview (most channels that carry films, and every group).
For those the page redirects, and the caller falls back to the account.

Nothing here touches the network except fetch_page(); parse_page() is pure
and tested in test_tme.py.
"""
import html as _html
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
USERNAME_RX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

_WRAP_SPLIT_RX = re.compile(r'(?=<div class="tgme_widget_message_wrap)')
_POST_ID_RX = re.compile(r'data-post="([^"/]+)/(\d+)"')
_TIME_RX = re.compile(r'<a class="tgme_widget_message_date"[^>]*>\s*<time[^>]+datetime="([^"]+)"')
_TIME_ANY_RX = re.compile(r'<time[^>]+datetime="([^"]+)"[^>]*class="time"|<time[^>]+class="time"[^>]+datetime="([^"]+)"')
_TEXT_RX = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*(?:<div class="tgme_widget_message_footer|<div class="tgme_widget_message_reply|<div class="media_not_supported_cont|<a class="tgme_widget_message_|$)', re.S)
_TEXT_LOOSE_RX = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TAG_RX = re.compile(r"<[^>]+>")
_BR_RX = re.compile(r"<br\s*/?>", re.I)
# one attachment = one <a …> whose class names the kind and whose href names the message
_ITEM_RX = re.compile(
    r'<a\b[^>]*\bclass="(?P<cls>[^"]*\btgme_widget_message_'
    r'(?P<kind>photo_wrap|video_player|document_wrap|voice_player|roundvideo_player)\b[^"]*)"[^>]*>', re.S)
_HREF_ID_RX = re.compile(r'href="https?://t\.me/[^"/]+/(\d+)(?:\?[^"]*)?"')
_DOC_TITLE_RX = re.compile(r'tgme_widget_message_document_title[^>]*>(.*?)</div>', re.S)
_DOC_EXTRA_RX = re.compile(r'tgme_widget_message_document_extra[^>]*>(.*?)</div>', re.S)
_DURATION_RX = re.compile(r'<time[^>]*class="[^"]*(?:message_video_duration|tgme_widget_message_voice_duration)[^"]*"[^>]*>([^<]+)</time>')
_VIDEO_SRC_RX = re.compile(r'<video[^>]+src="https?://')
_WRAP_SIZE_RX = re.compile(r'tgme_widget_message_video_wrap[^>]*style="[^"]*width:(\d+)px;padding-top:([\d.]+)%')
_OG_TITLE_RX = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"')
_COUNTER_RX = re.compile(
    r'<span class="counter_value">([^<]+)</span>\s*<span class="counter_type">([^<]+)</span>')
_HEADER_COUNTER_RX = re.compile(r'tgme_header_counter[^>]*>([^<]+)<')
_BEFORE_RX = re.compile(r'data-before="(\d+)"')
_SIZE_RX = re.compile(r"([\d.,]+)\s*([KMGT]?B)\b", re.I)
_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
_KIND = {"photo_wrap": "photo", "video_player": "video", "document_wrap": "document",
         "voice_player": "audio", "roundvideo_player": "video"}


def text_of(fragment: str) -> str:
    """The plain text of a piece of the page."""
    s = _BR_RX.sub("\n", fragment or "")
    s = _TAG_RX.sub("", s)
    return _html.unescape(s).replace("\xa0", " ").strip()


def parse_size(s: str):
    """'421.8 KB' -> bytes (rounded as the page rounds it); None if not a size."""
    m = _SIZE_RX.search(s or "")
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", ".")) * _UNITS[m.group(2).upper()])
    except (ValueError, KeyError):
        return None


def parse_duration(s: str):
    """'1:02:03' / '6:03' -> seconds; None if not a duration."""
    try:
        parts = [int(p) for p in (s or "").strip().split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec or None


def parse_count(s: str):
    """'15.7K' / '1.2M' / '3 204' -> int; None if not a number."""
    t = (s or "").strip().replace("\xa0", "").replace(" ", "").replace(",", "")
    m = re.match(r"^([\d.]+)([KkMm]?)$", t)
    if not m:
        return None
    try:
        return int(float(m.group(1)) * {"": 1, "k": 1000, "m": 1000000}[m.group(2).lower()])
    except ValueError:
        return None


def _when(s: str):
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def parse_page(page: str) -> dict:
    """{'title', 'members', 'before', 'posts': [{'id', 'date', 'text', 'items': [...]}]}

    An item is one attachment: {'id', 'kind', 'file_name', 'file_size',
    'duration', 'width', 'height', 'playable'}. Its id is the id of the
    message that carries it - in an album every attachment is a message of
    its own, and the page says which. A post without attachments has no items.
    """
    out = {"title": None, "members": None, "before": None, "posts": []}
    m = _OG_TITLE_RX.search(page)
    if m:
        out["title"] = _html.unescape(m.group(1)).strip() or None
    for val, typ in _COUNTER_RX.findall(page):
        if typ.strip().lower() in ("subscribers", "subscriber", "members", "member"):
            out["members"] = parse_count(val)
            break
    if out["members"] is None:
        m = _HEADER_COUNTER_RX.search(page)
        if m:
            out["members"] = parse_count(m.group(1).split()[0] if m.group(1).split() else "")
    m = _BEFORE_RX.search(page)
    if m:
        out["before"] = int(m.group(1))

    for chunk in _WRAP_SPLIT_RX.split(page)[1:]:
        pm = _POST_ID_RX.search(chunk)
        if not pm:
            continue
        post_id = int(pm.group(2))
        tm = _TIME_RX.search(chunk)
        when = _when(tm.group(1)) if tm else None
        if when is None:
            ta = _TIME_ANY_RX.search(chunk)
            if ta:
                when = _when(ta.group(1) or ta.group(2))
        tx = _TEXT_RX.search(chunk) or _TEXT_LOOSE_RX.search(chunk)
        text = text_of(tx.group(1)) if tx else ""
        items = []
        marks = list(_ITEM_RX.finditer(chunk))
        for i, im in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(chunk)
            seg = chunk[im.start():end]
            hid = _HREF_ID_RX.search(im.group(0))
            item = {"id": int(hid.group(1)) if hid else post_id, "kind": _KIND[im.group("kind")],
                    "file_name": None, "file_size": None, "duration": None,
                    "width": None, "height": None, "playable": False}
            if item["kind"] == "document":
                t = _DOC_TITLE_RX.search(seg)
                e = _DOC_EXTRA_RX.search(seg)
                item["file_name"] = (text_of(t.group(1)) or None) if t else None
                item["file_size"] = parse_size(text_of(e.group(1))) if e else None
            elif item["kind"] in ("video", "audio"):
                d = _DURATION_RX.search(seg)
                item["duration"] = parse_duration(d.group(1)) if d else None
                item["playable"] = bool(_VIDEO_SRC_RX.search(seg))
                w = _WRAP_SIZE_RX.search(seg)
                if w:
                    try:
                        item["width"] = int(w.group(1))
                        item["height"] = int(round(item["width"] * float(w.group(2)) / 100)) or None
                    except ValueError:
                        pass
            items.append(item)
        # one attachment per message id (a video also carries a thumbnail link to itself)
        seen, uniq = set(), []
        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            uniq.append(it)
        out["posts"].append({"id": post_id, "date": when, "text": text, "items": uniq})
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch_page(username: str, before=None, timeout: int = 25):
    """One preview page. Returns (state, html):

    'ok'         the page, with posts
    'nopreview'  Telegram does not preview this channel (it redirects), or the
                 page came back without a single post
    'slowdown'   asked to back off (HTTP 429 / 5xx)
    'error'      anything else
    Blocking: call it in a thread.
    """
    if not USERNAME_RX.match(username or ""):
        return "error", ""
    url = f"https://t.me/s/{username}" + (f"?before={int(before)}" if before else "")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
        with _opener.open(req, timeout=timeout) as r:
            body = r.read(4 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return "nopreview", ""
        return ("slowdown" if e.code == 429 or e.code >= 500 else "error"), ""
    except Exception:
        return "error", ""
    if 'data-post="' not in body:
        return "nopreview", body
    return "ok", body
