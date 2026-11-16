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
ok(T.needs("film.mp4") == "maybe" and T.needs("a.webm") == "maybe" and T.needs("b.m4v") == "maybe",
   "mp4 and webm too, since AV1 and HEVC live in them: played as they are, converted if this device cannot")
ok(T.needs("song.mp3") == "" and T.needs("noext") == "" and T.needs("") == "" and T.needs(None) == "",
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

# ---- on the graphics card ------------------------------------------------------------------------
V = ("vaapi", "/dev/dri/renderD128", "vbr")
Q = ("vaapi", "/dev/dri/renderD128", "cqp")
c = T.command(D, 0, 0, V)
ok(c[c.index("-vaapi_device") + 1] == "/dev/dri/renderD128" and c.index("-vaapi_device") < c.index("-i"), "the card is named before the film")
ok(c[c.index("-vf") + 1] == "yadif=deint=interlaced,format=nv12,hwupload" and c[c.index("-c:v") + 1] == "h264_vaapi",
   "a disc: its combing taken out in software, then handed to the card's encoder")
ok("libx264" not in c and "-crf" not in c and "-pix_fmt" not in c and "-preset" not in c, "nothing of the software encoder is left in the command")
ok(c[c.index("-c:a") + 1] == "aac" and c[-3:] == ["-f", "mp4", "pipe:1"], "the sound and the way out are as ever")
c = T.command(D, 3600.5, 480, V)
ok(c[c.index("-vf") + 1] == "yadif=deint=interlaced,scale=-2:'min(480,ih)',format=nv12,hwupload", "made smaller in software, before the card")
ok(c.index("-vaapi_device") < c.index("-ss") < c.index("-i"), "and from an hour in: card, place, film")
N = ("nvenc", "", "vbr")
c = T.command(M, 10, 720, N)
ok(c[c.index("-c:v") + 1] == "h264_nvenc" and c[c.index("-vf") + 1] == "scale=-2:'min(720,ih)',format=yuv420p" and "-vaapi_device" not in c,
   "an NVIDIA card: no device to name, the picture as it takes it")
c = T.command(M, 10, 0, V)
ok(c[c.index("-c:v") + 1] == "copy" and "-vaapi_device" not in c and "-vf" not in c, "a picture that is passed on untouched is not sent to any card")
ok(T.command(dict(M, vcodec="hevc"), 0, 0, V)[T.command(dict(M, vcodec="hevc"), 0, 0, V).index("-c:v") + 1] == "h264_vaapi", "HEVC is made anew - on the card")
ok(T.command(dict(M, pix="yuv420p10le"), 0, 0, N)[T.command(dict(M, pix="yuv420p10le"), 0, 0, N).index("-vf") + 1] == "format=yuv420p", "10-bit: read in software, brought to 8-bit, then the card")
for hw in (("", ""), None, ("nonsense", "x"), (), ("", "", "")):
    try:
        c = T.command(dict(M, vcodec="hevc"), 0, 0, hw)
        ok(c[c.index("-c:v") + 1] == "libx264" and "-vaapi_device" not in c, f"no card, or one that is none ({hw!r}): software, as before")
    except Exception as e:
        ok(False, f"{hw!r}: {e}")
ok(all(isinstance(x, str) for x in T.command(D, 12.3, 720, V) + T.command(D, 12.3, 720, N)), "every argument is a string still")

# a size to aim at: a card told only "this quality" made a gigabyte of two minutes of film grain
ok(T.hw_rate(1080) == (8000, 12000) and T.hw_rate(720) == (5000, 7500) and T.hw_rate(480) == (2500, 4000) and T.hw_rate(2160) == (14000, 20000),
   "so much for a picture so tall")
ok(T.hw_rate(0) == (14000, 20000) and T.hw_rate(None) == (14000, 20000), "a height that is not known is taken for a tall one")
c = T.command(dict(M, vcodec="hevc"), 0, 0, V)
ok(c[c.index("-b:v") + 1] == "8000k" and c[c.index("-maxrate") + 1] == "12000k" and c[c.index("-rc_mode") + 1] == "VBR" and "-qp" not in c, "a 1080p film on the card: aimed at 8, never over 12 Mbit/s")
c = T.command(dict(M, vcodec="hevc"), 0, 480, V)
ok(c[c.index("-b:v") + 1] == "2500k", "made 480p: the size of a 480p picture")
c = T.command(dict(M, vcodec="hevc", height=360), 0, 720, N)
ok(c[c.index("-b:v") + 1] == "2500k" and c[c.index("-maxrate") + 1] == "4000k" and c[c.index("-bufsize") + 1] == "8000k", "a small film asked for at 720p stays small, and so does its size")
c = T.command(D, 0, 0, V)
ok(c[c.index("-b:v") + 1] == "2500k", "a DVD is 480 lines")
c = T.command(dict(M, vcodec="hevc"), 0, 0, Q)
ok(c[c.index("-rc_mode") + 1] == "CQP" and c[c.index("-qp") + 1] == "25" and "-b:v" not in c and "-maxrate" not in c,
   "a card that cannot hold a size (an older Intel one): one fixed quality")
c = T.command(dict(M, vcodec="hevc"), 0, 0, ("vaapi", "/dev/dri/renderD128"))
ok(c[c.index("-rc_mode") + 1] == "CQP", "not said how: the way every card knows")

t = T.hw_trial("vaapi", "/dev/dri/renderD128")
ok(t[t.index("-vaapi_device") + 1] == "/dev/dri/renderD128" and "testsrc2=size=640x360:rate=25:duration=1" in t and t[-3:] == ["-f", "null", "-"] and "h264_vaapi" in t,
   "the trial: one second of a test picture through the card, thrown away")
ok("h264_nvenc" in T.hw_trial("nvenc") and "-vaapi_device" not in T.hw_trial("nvenc"), "and for an NVIDIA card")
asked = []
ok(T.hw_find(run=lambda cmd: asked.append(cmd) or 1, devices=["/dev/dri/renderD128", "/dev/dri/renderD129"]) == ("", "", "") and len(asked) == 5,
   "no trial succeeds: no card - each device both ways, and NVIDIA, were tried")
ok(T.hw_find(run=lambda cmd: 0 if "/dev/dri/renderD129" in cmd else 1, devices=["/dev/dri/renderD128", "/dev/dri/renderD129"]) == ("vaapi", "/dev/dri/renderD129", "vbr"),
   "the second device is the one that works")
ok(T.hw_find(run=lambda cmd: 0 if "CQP" in cmd else 1, devices=["/dev/dri/renderD128"]) == ("vaapi", "/dev/dri/renderD128", "cqp"),
   "a card that takes the trial only at a fixed quality is used that way")
ok(T.hw_find(run=lambda cmd: 0 if "h264_nvenc" in cmd else 1, devices=[]) == ("nvenc", "", "vbr"), "no device at all, but an NVIDIA card")


def crashes(cmd):
    raise OSError("the driver fell over")


ok(T.hw_find(run=crashes, devices=["/dev/dri/renderD128"]) == ("", "", ""), "a driver that crashes is a no, not an error")
ok(T.hw_find(run=lambda cmd: 0, devices=["/dev/dri/renderD128"]) == ("vaapi", "/dev/dri/renderD128", "vbr"), "VAAPI is tried before NVIDIA, holding the size before not")
ok("VBR" in T.hw_trial("vaapi", "/dev/x", "vbr") and "CQP" in T.hw_trial("vaapi", "/dev/x", "cqp"), "the trial is made the way the films will be")
ok(T.hw_devices("/nonexistent/renderD*") == [], "a container that was given no card")
ok(set(T.HW_NAMES) == {"vaapi", "nvenc", ""}, "a name for each, and for none")

print(f"test_transcode: {n} checks passed")
