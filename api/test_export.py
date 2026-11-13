"""Checks for "many files as one download". Run: python test_export.py"""
import datetime
import io
import os
import tempfile
import zipfile

import export as E

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


D = datetime.datetime(2026, 3, 9, 14, 5)

# ---- names ---------------------------------------------------------------------
ok(E.safe_name("a/b\\c:d*e?.jpg") == "a_b_c_d_e_.jpg", "characters no file system takes")
ok(E.safe_name("  .hidden.  ") == "hidden", "no leading or trailing dots and spaces")
ok(E.safe_name("", "chat") == "chat" and E.safe_name(None) == "file", "nothing left: the fallback")
long = E.safe_name("x" * 300 + ".mkv")
ok(len(long) == 150 and long.endswith(".mkv"), "a long name is cut, its ending kept")
ok(E.safe_name("全家福 2024.jpg") == "全家福 2024.jpg", "other alphabets pass untouched")
ok(E.item_name({"file_name": "Trip.mp4"}) == "Trip.mp4", "a file keeps its own name")
ok(E.item_name({"media_type": "photo", "posted_at": D, "tg_message_id": 812}) == "photo_2026-03-09_812.jpg",
   "a photo is named by day and place in the chat")
ok(E.item_name({"media_type": "video", "mime_type": "video/mp4", "posted_at": D, "tg_message_id": 5})
   == "video_2026-03-09_5.mp4", "the ending comes from the kind of file")
ok(E.item_name({"id": 9}) == "file_undated_9", "nothing known: still a usable name")

items = [{"folder": "Family", "file_name": "IMG_0001.jpg"}, {"folder": "Family", "file_name": "IMG_0001.jpg"},
         {"folder": "Family", "file_name": "img_0001.JPG"}, {"folder": "Work/Team", "file_name": "IMG_0001.jpg"},
         {"folder": None, "file_name": "a.txt"}]
names = [a for a, _ in E.arc_names(items)]
ok(names[0] == "Family/IMG_0001.jpg" and names[1] == "Family/IMG_0001 (2).jpg", "the second of a name is numbered")
ok(names[2] == "Family/img_0001 (3).JPG", "names differing only in capitals count as the same")
ok(names[3] == "Work_Team/IMG_0001.jpg", "another chat is another folder; a slash cannot make a sub-folder")
ok(names[4] == "chat/a.txt", "a chat without a name still gets a folder")
ok(len(set(x.lower() for x in names)) == len(names), "no two names collide")
ok(E.arc_names(items)[1][1] is items[1], "each name comes with its item")

# ---- the zip itself --------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    big = os.urandom(3 * (1 << 20) + 17)
    files = {"one.bin": b"hello", "two.bin": big, "empty.bin": b""}
    for k, v in files.items():
        with open(os.path.join(tmp, k), "wb") as f:
            f.write(v)
    pairs = [("A/one.bin", os.path.join(tmp, "one.bin")), ("A/gone.bin", os.path.join(tmp, "not-there.bin")),
             ("B/two.bin", os.path.join(tmp, "two.bin")), ("B/empty.bin", os.path.join(tmp, "empty.bin")),
             ("B/名字.bin", os.path.join(tmp, "one.bin"))]
    pieces = list(E.zip_stream(pairs, chunk=1 << 20))
    ok(len(pieces) >= 4, "it arrives in pieces, not as one block")
    ok(max(len(p) for p in pieces) < 2 * (1 << 20), "no piece is much larger than the block size")
    data = b"".join(pieces)
    zf = zipfile.ZipFile(io.BytesIO(data))
    ok(zf.testzip() is None, "every file inside passes its checksum")
    ok(zf.namelist() == ["A/one.bin", "B/two.bin", "B/empty.bin", "B/名字.bin"], "a missing file is left out, order kept")
    ok(zf.read("A/one.bin") == b"hello" and zf.read("B/two.bin") == big and zf.read("B/empty.bin") == b"",
       "contents are exact")
    ok(all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist()), "stored, not squeezed again")
    ok(len(data) < len(big) + 5 + 5 + 2000, "the zip is hardly larger than the files")
    ok(list(E.zip_stream([])) and zipfile.ZipFile(io.BytesIO(b"".join(E.zip_stream([])))).namelist() == [],
       "nothing to pack: still a valid, empty zip")

# ---- sizes as people read them ------------------------------------------------------
ok(E.human(0) == "" and E.human(None) == "", "nothing known: say nothing")
ok(E.human(900) == "900 B" and E.human(1536) == "1.5 KB", "small sizes")
ok(E.human(95 * (1 << 20)) == "95 MB" and E.human(int(4.25 * (1 << 30))) == "4.2 GB", "large sizes")

print(f"ok - {n} checks")
