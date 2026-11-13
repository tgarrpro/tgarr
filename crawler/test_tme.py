"""The web-preview parser, on markup shaped like Telegram's own pages.

    python test_tme.py
"""
import sys

import tme

n = 0


def ok(cond, what):
    global n
    n += 1
    if not cond:
        sys.exit(f"FAIL: {what}")


def post(pid, inner, text="", when="2026-11-10T08:15:00+00:00", chan="somechan"):
    return (f'<div class="tgme_widget_message_wrap js-widget_message_wrap">'
            f'<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="{chan}/{pid}">'
            f'<div class="tgme_widget_message_bubble">{inner}'
            + (f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>' if text else '')
            + f'<div class="tgme_widget_message_footer compact js-message_footer"><div class="tgme_widget_message_info">'
              f'<span class="tgme_widget_message_views">1.2K</span>'
              f'<span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" '
              f'href="https://t.me/{chan}/{pid}"><time datetime="{when}" class="time">08:15</time></a></span>'
              f'</div></div></div></div></div>')


DOC = ('<a class="tgme_widget_message_document_wrap" href="https://t.me/somechan/{id}?single">'
       '<div class="tgme_widget_message_document_icon accent_bg"></div><div class="tgme_widget_message_document">'
       '<div class="tgme_widget_message_document_title accent_color" dir="auto">{name}</div>'
       '<div class="tgme_widget_message_document_extra" dir="auto">{size}</div></div></a>')
VIDEO_SMALL = ('<a class="tgme_widget_message_video_player js-message_video_player" href="https://t.me/somechan/{id}">'
               '<i class="tgme_widget_message_video_thumb" style="background-image:url(\'https://cdn4.cdn-telegram.org/file/x.jpg\')"></i>'
               '<div class="tgme_widget_message_video_wrap" style="width:1280px;padding-top:56.25%">'
               '<video src="https://cdn4.cdn-telegram.org/file/v.mp4?token=abc" class="tgme_widget_message_video"></video></div>'
               '<div class="message_video_play"></div><time class="message_video_duration js-message_video_duration">1:40</time></a>')
VIDEO_BIG = ('<a class="tgme_widget_message_video_player not_supported js-message_video_player" href="https://t.me/somechan/{id}">'
             '<i class="tgme_widget_message_video_thumb" style="background-image:url(\'https://cdn4.cdn-telegram.org/file/y.jpg\')"></i>'
             '<div class="tgme_widget_message_video_wrap" style="width:1920px;padding-top:56.25%"> </div>'
             '<div class="message_video_play"></div><time class="message_video_duration js-message_video_duration">1:52:03</time>'
             '<div class="message_media_not_supported_wrap"><div class="message_media_not_supported">'
             '<div class="message_media_not_supported_label">Media is too big</div></div></div></a>')
PHOTO = ('<a class="tgme_widget_message_photo_wrap grouped_media_wrap" href="https://t.me/somechan/{id}?single" '
         'style="background-image:url(\'https://cdn4.cdn-telegram.org/file/p{id}.jpg\')"></a>')

PAGE = ('<html><head><meta property="og:title" content="Some &amp; Channel"></head><body>'
        '<div class="tgme_channel_info_counters"><div class="tgme_channel_info_counter">'
        '<span class="counter_value">15.7K</span> <span class="counter_type">subscribers</span></div>'
        '<div class="tgme_channel_info_counter"><span class="counter_value">941</span> '
        '<span class="counter_type">photos</span></div></div>'
        '<a class="tme_messages_more" data-before="101" href="/s/somechan?before=101"></a>'
        + post(101, DOC.format(id=101, name="The.Book.2024.epub", size="2.3 MB"),
               "A book &amp; its <b>cover</b><br/>second line")
        + post(102, '<div class="tgme_widget_message_grouped_wrap"><div class="tgme_widget_message_grouped">'
                    + PHOTO.format(id=102) + PHOTO.format(id=103) + VIDEO_SMALL.format(id=104) + '</div></div>',
               "an album")
        + post(105, VIDEO_BIG.format(id=105), "Film.Title.2023.1080p")
        + post(106, "", "just words, nothing attached")
        + post(107, DOC.format(id=107, name="a.pdf", size="421.8 KB") + DOC.format(id=108, name="b.pdf", size="1,5 GB"))
        + '</body></html>')

p = tme.parse_page(PAGE)
ok(p["title"] == "Some & Channel", "channel title, entities decoded")
ok(p["members"] == 15700, "subscriber count, not the photo count")
ok(p["before"] == 101, "cursor for the next (older) page")
ok([x["id"] for x in p["posts"]] == [101, 102, 105, 106, 107], "five posts, in page order")
by = {x["id"]: x for x in p["posts"]}
ok(by[101]["text"] == "A book & its cover\nsecond line", "text: tags gone, <br> a newline, entities decoded")
ok(by[101]["date"].isoformat() == "2026-11-10T08:15:00+00:00", "post time, with its zone")
d = by[101]["items"]
ok(len(d) == 1 and d[0]["kind"] == "document" and d[0]["file_name"] == "The.Book.2024.epub"
   and d[0]["file_size"] == int(2.3 * 1024 ** 2), "a document: name and size")
a = by[102]["items"]
ok([(x["id"], x["kind"]) for x in a] == [(102, "photo"), (103, "photo"), (104, "video")],
   "an album: every attachment is its own message id")
ok(a[2]["duration"] == 100 and a[2]["playable"] and (a[2]["width"], a[2]["height"]) == (1280, 720),
   "a small video: running time, frame size, playable from the page")
b = by[105]["items"]
ok(len(b) == 1 and b[0]["kind"] == "video" and b[0]["duration"] == 6723 and not b[0]["playable"]
   and (b[0]["width"], b[0]["height"]) == (1920, 1080), "a big video: known to be a video, not playable here")
ok(by[106]["items"] == [] and by[106]["text"] == "just words, nothing attached", "a post without attachments")
c = by[107]["items"]
ok([(x["id"], x["file_name"]) for x in c] == [(107, "a.pdf"), (108, "b.pdf")], "two files in one post")
ok(c[0]["file_size"] == int(421.8 * 1024) and c[1]["file_size"] == int(1.5 * 1024 ** 3), "KB and a decimal comma")

# the small parsers
ok(tme.parse_size("20.3 MB") == int(20.3 * 1024 ** 2) and tme.parse_size("7 B") == 7 and tme.parse_size("soon") is None, "sizes")
ok(tme.parse_duration("0:20") == 20 and tme.parse_duration("1:02:03") == 3723 and tme.parse_duration("x") is None
   and tme.parse_duration("") is None, "durations")
ok(tme.parse_count("10M") == 10_000_000 and tme.parse_count("3 204") == 3204 and tme.parse_count("n/a") is None, "counts")

# a page without posts, and garbage, do not raise
ok(tme.parse_page("<html><body>nothing</body></html>")["posts"] == [], "a page without posts")
ok(tme.parse_page("")["posts"] == [] and tme.parse_page("<div class=\"tgme_widget_message_wrap\">broken")["posts"] == [],
   "empty and broken input")

# names that cannot be a username are never sent to the network
for bad in ("", "ab", "has space", "x/../y", "1abc", "a" * 40, "名字"):
    ok(tme.fetch_page(bad) == ("error", ""), f"not a username: {bad!r}")

print(f"ok - {n} checks")
