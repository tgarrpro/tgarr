"""Checks for the registry's web reader that need no network and no database.
Run: python test_webcrawl.py"""
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DB_DSN", "postgresql://unused")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tme_validate as V  # noqa: E402
import webcrawl as W  # noqa: E402

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


def post(i, when, kind=None, text="a film worth keeping, 1080p, with subtitles"):
    media = {"doc": '<div class="tgme_widget_message_document"></div>',
             "video": '<div class="tgme_widget_message_video_wrap"></div>',
             "photo": '<div class="tgme_widget_message_photo_wrap"></div>'}.get(kind, "")
    return (f'<div class="tgme_widget_message_wrap js-widget_message_wrap"><div data-post="chan/{i}">'
            f'{media}<div class="tgme_widget_message_text js-message_text" dir="auto">{text} {i}</div>'
            f'<time datetime="{when}"></time></div></div></div>')


def page(posts, members="12.5K", files="340"):
    return ('<meta property="og:title" content="Some Films">'
            '<div class="tgme_channel_info">'
            f'<div class="tgme_channel_info_counter"><span class="counter_value">{members}</span>'
            '<span class="counter_type">subscribers</span></div>'
            f'<div class="tgme_channel_info_counter"><span class="counter_value">{files}</span>'
            '<span class="counter_type">files</span></div>'
            '<div class="tgme_channel_info_description">films, daily @other_channel</div></div>'
            + "".join(posts))


# ---- what kind of page came back ---------------------------------------
alive = page([post(900, "2026-11-10T08:00:00+00:00", "doc")])
ok(W.page_kind(200, alive, V) == "alive", "a channel with a preview")
ok(W.page_kind(200, '<div class="tgme_page_title">Somebody</div>', V) == "no_preview",
   "a name that exists but shows no channel preview")
ok(W.page_kind(200, "<html><body>nothing here</body></html>", V) == "gone", "no such name")
for code in (429, 503, 403):
    ok(W.page_kind(code, "", V) == "slowdown", f"{code} means slow down")
ok(W.page_kind(None, "timed out", V) == "error", "a network failure")
ok(W.page_kind(500, "", V) == "error", "a server error")
ok(W.page_kind(200, "", V) == "error", "an empty answer")

ok(W.page_members('<div class="tgme_page_extra">12 345 subscribers</div>') == 12345, "followers on a plain page")
ok(W.page_members('<div class="tgme_page_extra">1 subscriber</div>') == 1, "one follower")
ok(W.page_members('<div class="tgme_page_extra">3 200 members, 15 online</div>') is None, "a group is not counted")
ok(W.page_members('<div class="tgme_page_extra">@BotFather</div>') is None, "a bot has none")
ok(W.page_members("") is None, "nothing to read")

# ---- what a page says about the channel --------------------------------
first = datetime(2024, 11, 10, tzinfo=timezone.utc)
s = W.summarize(V.parse_page(page([
    post(898, "2026-11-08T08:00:00+00:00", "photo"),
    post(899, "2026-11-09T08:00:00+00:00", "doc"),
    post(900, "2026-11-10T08:00:00+00:00"),
])), first, "sfw", V)
ok(s["title"] == "Some Films", "title")
ok(s["members"] == 12500 and s["files"] == 340, "counters")
ok(s["max_msg_id"] == 900, "newest post id")
ok(s["last_msg_at"] == datetime(2026, 11, 10, 8, tzinfo=timezone.utc), "newest post time")
ok(s["last_file_at"] == datetime(2026, 11, 9, 8, tzinfo=timezone.utc),
   "newest FILE post is the document, not the later text post")
ok(s["lifespan_days"] == 730, "age from the oldest post known earlier")
ok(s["post_rate"] == round(900 / 730, 3), "posts a day")
ok(s["gold"] is not None and s["gold"] > 0, "a score is worked out when the age is known")
ok("other_channel" in V.harvest_links(alive)[0], "names mentioned on the page are picked up")

# age unknown and the page is not the whole channel: no age, no score - the
# stored score must not be replaced by a guess
s = W.summarize(V.parse_page(page([post(900, "2026-11-10T08:00:00+00:00", "doc")])), None, "sfw", V)
ok(s["lifespan_days"] is None and s["gold"] is None and s["post_rate"] is None,
   "unknown age leaves age, rate and score alone")
ok(s["density"] is not None, "density needs no age")

# a small channel that fits on one page: its own oldest post gives the age
s = W.summarize(V.parse_page(page([
    post(3, "2026-01-01T00:00:00+00:00", "doc"), post(9, "2026-11-01T00:00:00+00:00", "doc")])),
    None, "sfw", V)
ok(s["lifespan_days"] == 304, "a channel that fits on one page dates itself")

# a clock that is wrong (or a page that lies) cannot put a post in the future
future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
s = W.summarize(V.parse_page(page([post(900, future, "video")])), first, "sfw", V)
ok(s["last_msg_at"] <= datetime.now(timezone.utc) and s["last_file_at"] <= datetime.now(timezone.utc),
   "no post from the future")

# nothing on the page
s = W.summarize(V.parse_page(page([])), first, "sfw", V)
ok(s["max_msg_id"] is None and s["last_msg_at"] is None and s["gold"] is None, "an empty preview changes nothing")

# ---- the bulk analysis survives a channel with nothing worth caching ----
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tme_validate.py"), encoding="utf-8").read()
ok("doc = None" in src, "process_channel always has a result to return")
ok("os.makedirs(CACHE_ROOT" not in src.split("def save_json")[0], "importing the analysis creates no folder")
ok(set(W._COUNTERS) <= set(W.SCHEMA.split()), "every counter has a column")

print(f"ok - {n} checks")
