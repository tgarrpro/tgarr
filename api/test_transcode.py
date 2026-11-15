"""Checks for playing what a browser cannot: which files, how they are opened, the command.
Nothing is run. Run: python test_transcode.py"""
import json

import transcode as T

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


# ---- which files ------------------------------------------------------------------------------
for name in ("Film.iso", "a.VOB", "old.avi", "x.wmv", "clip.flv", "tv.mpg", "b.mpeg", "c.rmvb", "cam.3gp", "rec.ts", "bd.m2ts", "d.divx"):
    ok(T.needs(name) == "always", f"{name}: no browser plays it")
ok(T.needs("film.mkv") == "maybe" and T.needs("clip.MOV") == "maybe", "mkv and mov: some browsers do, with some sound")
ok(T.needs("film.mp4") == "" and T.needs("a.webm") == "" and T.needs("b.m4v") == "" and T.needs("noext") == "" and T.needs("") == "" and T.needs(None) == "",
   "what plays as it is; and what is no file name")

# ---- how a file is opened -------------------------------------------------------------------------
ok(T.openings("/d/Film.iso") == [("dvd", ["-f", "dvdvideo", "-i", "/d/Film.iso"]), ("bluray", ["-i", "bluray:/d/Film.iso"])], "a disc image: as a DVD, then as a Blu-ray")
ok(T.openings("/d/film.mkv") == [("file", ["-i", "/d/film.mkv"])], "anything else is a file")

# ---- what ffprobe said ----------------------------------------------------------------------------
dvd = {"streams": [{"codec_type": "video", "codec_name": "mpeg2video", "width": 720, "height": 480, "pix_fmt": "yuv420p"},
                   {"codec_type": "audio", "codec_name": "ac3"}], "format": {"duration": "5412.933333"}}
i = T.parse_probe(json.dumps(dvd))
ok(i == {"duration": 5412.93, "width": 720, "height": 480, "vcodec": "mpeg2video", "pix": "yuv420p", "acodec": "ac3"}, f"a DVD: {i}")
i = T.parse_probe({"streams": [{"codec_type": "audio", "codec_name": "aac"},
                               {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
                               {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "pix_fmt": "yuv420p10le", "duration": "60.5"}],
                   "format": {}})
ok(i["vcodec"] == "h264" and i["height"] == 1080 and i["duration"] == 60.5 and i["acodec"] == "aac" and i["pix"] == "yuv420p10le",
   "the film's picture, not its cover; the length from the picture when the file names none")
ok(T.parse_probe({"streams": [{"codec_type": "video", "codec_name": "h264"}], "format": {"duration": "9"}})["acodec"] == "", "a silent film is a film")
for bad, why in (("not json", "no answer"), ("{}", "nothing in it"), (json.dumps({"streams": [{"codec_type": "audio"}], "format": {"duration": "9"}}), "sound only"),
                 (json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "0"}}), "no length"),
                 (json.dumps({"streams": [{"codec_type": "video", "disposition": {"attached_pic": 1}}], "format": {"duration": "9"}}), "a cover only"), (None, "nothing")):
    try:
        T.parse_probe(bad)
        ok(False, f"{why}: taken for a film")
    except T.NotAFilm:
        ok(True, why)

# ---- the command -------------------------------------------------------------------------------------
D = dict(T.parse_probe(dvd), kind="dvd", input=["-f", "dvdvideo", "-i", "/d/Film.iso"])
c = T.command(D)
ok(c[0] == "ffmpeg" and "-ss" not in c and c[c.index("-f"):c.index("-f") + 4] == ["-f", "dvdvideo", "-i", "/d/Film.iso"], "from the start: no seeking, the disc opened as a disc")
ok(c[-3:] == ["-f", "mp4", "pipe:1"] and "frag_keyframe+empty_moov+default_base_moof" in c, "out as a stream a browser plays while it arrives")
ok(c[c.index("-c:v") + 1] == "libx264" and c[c.index("-vf") + 1] == "yadif=deint=interlaced" and c[c.index("-c:a") + 1] == "aac" and c[c.index("-ac") + 1] == "2",
   "a disc's picture is made anew, its combing taken out; the sound becomes stereo AAC")
ok("-nostdin" in c and "-sn" in c and c[c.index("-map") + 1] == "0:v:0" and "0:a:0?" in c, "one picture, one sound if there is any, no subtitles, and it never waits for a key")
c = T.command(D, 3600.5, 480)
ok(c[c.index("-ss") + 1] == "3600.50" and c.index("-ss") < c.index("-i"), "from an hour in: the seek comes before the input (fast)")
ok(c[c.index("-vf") + 1] == "yadif=deint=interlaced,scale=-2:'min(480,ih)'", "no taller than asked, never made larger")
ok(T.command(D, 999999)[T.command(D, 999999).index("-ss") + 1] == "5411.93" and "-ss" not in T.command(D, -5), "a place past the end is the end; before the start is the start")
ok("scale" not in " ".join(T.command(D, 0, 333)), "a height that is not offered is 'as it is'")
M = {"duration": 100.0, "width": 1920, "height": 1080, "vcodec": "h264", "pix": "yuv420p", "acodec": "dts", "kind": "file", "input": ["-i", "/d/f.mkv"]}
c = T.command(M, 10)
ok(c[c.index("-c:v") + 1] == "copy" and "-vf" not in c and c[c.index("-c:a") + 1] == "aac", "an mkv with H.264 inside: the picture passed on untouched, only the sound converted")
ok(T.command(M, 0, 720)[T.command(M, 0, 720).index("-c:v") + 1] == "libx264", "asked for smaller than it is: made anew")
ok(T.command(M, 0, 1080)[T.command(M, 0, 1080).index("-c:v") + 1] == "copy", "asked for its own height: untouched")
ok(T.command(dict(M, pix="yuv420p10le"))[T.command(dict(M, pix="yuv420p10le")).index("-c:v") + 1] == "libx264", "10-bit H.264: browsers do not decode it")
ok(T.command(dict(M, vcodec="hevc"))[T.command(dict(M, vcodec="hevc")).index("-c:v") + 1] == "libx264", "HEVC: made anew")
ok(not T.can_copy(dict(M, kind="bluray"), 0), "a Blu-ray's picture is not passed on (it may be interlaced, and its stream is cut in pieces)")
ok(all(isinstance(x, str) for x in T.command(M, 12.3456, 720)), "every argument is a string: nothing goes through a shell")

print(f"test_transcode: {n} checks passed")
