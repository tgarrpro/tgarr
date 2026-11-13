"""Checks for the household's own files. Run: python test_localfiles.py"""
import localfiles as L

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


# ---- what kind of file ---------------------------------------------------------
ok(L.classify("Trip.MP4") == ("video", "video/mp4"), "a film, whatever the capitals")
ok(L.classify("song.flac")[0] == "audio" and L.classify("IMG_1.HEIC")[0] == "photo", "music and photos")
ok(L.classify("book.epub") == ("document", "application/epub+zip"), "a book")
ok(L.classify("setup.exe") == ("document", "application/octet-stream"), "an upload is taken whatever it is")
ok(L.classify("setup.exe", known_only=True) is None, "media and documents only, when asked so")
ok(L.classify("noext", known_only=True) is None and L.classify("noext")[0] == "document", "no ending at all")
for junk in (".DS_Store", "Thumbs.db", "film.mkv.part", "~$letter.docx", "x.crdownload", ""):
    ok(L.classify(junk) is None, f"left out: {junk!r}")
ok(L.classify("/some/where/clip.webm")[0] == "video", "only the name counts, not the folders")

# ---- names -------------------------------------------------------------------------
ok(L.safe_name("../../etc/passwd") == "passwd", "an upload cannot climb out of its folder")
ok(L.safe_name("C:\\Users\\me\\a.jpg") == "a.jpg", "nor with the other kind of slash")
ok(L.safe_name('we:ird*na?me.mp3') == "we_ird_na_me.mp3", "characters a file system refuses")
ok(L.safe_name("") == "file" and L.safe_name("...") == "file", "nothing usable: a plain name")
ok(L.safe_name("全家福.jpg") == "全家福.jpg", "other alphabets pass untouched")
long = L.safe_name("长" * 200 + ".mkv")
ok(len(long.encode("utf-8")) <= 200 and long.endswith(".mkv"), "a long name is cut by bytes, its ending kept")
have = {"a.jpg", "a (2).jpg"}
ok(L.free_name(have.__contains__, "b.jpg") == "b.jpg", "a free name stays")
ok(L.free_name(have.__contains__, "a.jpg") == "a (3).jpg", "a taken one is numbered, past those taken too")

# ---- the made-up chats ----------------------------------------------------------------
ok(L.chat_id("mine") == L.chat_id("mine") and L.chat_id("mine") != L.chat_id("external/nas"), "one id per source, always the same")
ok(all(L.chat_id(r) <= L.LOCAL_CHAT_MAX for r in ("mine", "external/a", "external/照片")), "never an id Telegram could use")
ok(L.is_local(L.chat_id("mine")) and not L.is_local(-1001234567890) and not L.is_local(777000) and not L.is_local(None),
   "told apart from real chats")
ok(L.unique_id("mine/a.jpg") == L.unique_id("mine/a.jpg") != L.unique_id("mine/b.jpg"), "one id per file")
ok(L.title("mine") == "My files", "the name people see")

print(f"ok - {n} checks")
