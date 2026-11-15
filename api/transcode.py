"""Playing what a browser cannot play: converted while it is watched.

A phone or a tablet plays mp4 and little else. A DVD or Blu-ray image, an avi, an old mpeg, a
wmv, an mkv with sound the browser has no decoder for - downloaded, they are files nobody on
that device can open. Here ffmpeg reads the file as it is and hands the browser a stream it
does play (H.264 and AAC in fragmented MP4), starting anywhere: moving the slider starts
ffmpeg again from that second. Nothing is written to the disk and the file is never changed.

    needs(name)            'always' | 'maybe' | ''   - can a browser play this as it is?
    openings(path)         the ways ffmpeg may open it, in the order to try
    parse_probe(text)      what ffprobe said, as {duration, width, height, vcodec, acodec, pix}
    command(info, t, cap, hw)  the ffmpeg command that plays from second t, no taller than cap
    hw_find()              the graphics card that can do the encoding, if there is one

The graphics card. Making H.264 in software takes two processor cores for one 1080p film on
an ordinary desktop processor, and more than a small server has. A card's encoder does it
for a third of one core. So where the container was given a card - an Intel or AMD one
through /dev/dri (VAAPI), an NVIDIA one through its container runtime (NVENC) - and a
one-second trial on it succeeds, the picture is encoded there. Reading the film and any
resizing stay in software: that part works for every file, and the saving is in the encoding.
(Measured on an i7-7700 with its own graphics, 1080p with film grain: 199% of a core in
software, 35% with the card encoding.)

Only probe() and hw_find() run anything; the rest is plain and checked by test_transcode.py.
"""
import glob
import json
import shutil
import subprocess

# what no browser plays / what some play and some do not / everything else plays as it is
ALWAYS = frozenset("iso vob avi wmv flv mpg mpeg rmvb rm 3gp ts m2ts mts divx asf ogv".split())
MAYBE = frozenset("mkv mov".split())
HEIGHTS = (480, 720, 1080)             # what the player offers; 0 is "as it is"
FFMPEG, FFPROBE = "ffmpeg", "ffprobe"


class NotAFilm(Exception):
    """The file holds nothing that can be played (a disc image of software, a broken file)."""


def available() -> bool:
    return bool(shutil.which(FFMPEG) and shutil.which(FFPROBE))


def ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def needs(name: str) -> str:
    e = ext(name)
    return "always" if e in ALWAYS else "maybe" if e in MAYBE else ""


def openings(path: str) -> list:
    """[(kind, [ffmpeg's arguments that name the input])]. A disc image is tried as a DVD
    (its longest title), then as a Blu-ray; anything else is a file."""
    if ext(path) == "iso":
        return [("dvd", ["-f", "dvdvideo", "-i", path]), ("bluray", ["-i", "bluray:" + path])]
    return [("file", ["-i", path])]


def parse_probe(text) -> dict:
    """From ffprobe's JSON. Raises NotAFilm when there is no picture in it or no length."""
    try:
        d = json.loads(text) if isinstance(text, (str, bytes)) else text
        streams = d.get("streams") or []
    except (ValueError, AttributeError, TypeError):
        raise NotAFilm("unreadable")
    v = next((s for s in streams if s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") != 1), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        duration = float((d.get("format") or {}).get("duration") or (v or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if not v or duration <= 0:
        raise NotAFilm("no picture in it")
    return {"duration": round(duration, 2), "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
            "vcodec": str(v.get("codec_name") or ""), "pix": str(v.get("pix_fmt") or ""),
            "acodec": str((a or {}).get("codec_name") or "")}


def probe(path: str, timeout: int = 90) -> dict:
    """What the file is: parse_probe()'s answer plus `kind` and `input`. Raises NotAFilm."""
    for kind, inp in openings(path):
        try:
            r = subprocess.run([FFPROBE, "-v", "error"] + inp + ["-show_entries",
                               "format=duration:stream=codec_type,codec_name,width,height,pix_fmt,duration:stream_disposition=attached_pic",
                               "-of", "json"], capture_output=True, timeout=timeout)
            info = parse_probe(r.stdout)
        except (NotAFilm, subprocess.TimeoutExpired, OSError):
            continue
        info.update(kind=kind, input=inp)
        return info
    raise NotAFilm("not a film disc" if ext(path) == "iso" else "nothing in it that can be played")


HW_KINDS = ("vaapi", "nvenc")
HW_NAMES = {"vaapi": "the graphics card (VAAPI)", "nvenc": "the NVIDIA card (NVENC)", "": "software"}


def hw_devices(pattern: str = "/dev/dri/renderD*") -> list:
    """The cards the container was given, as VAAPI names them."""
    return sorted(glob.glob(pattern))


def hw_rate(height: int) -> tuple:
    """(what to aim at, the most it may be), in kbit/s, for a picture so tall. A card's
    encoder told only "this quality" spends whatever a picture asks for - film grain asked
    for 80 Mbit/s, a gigabyte for two minutes. So it is given a size to aim at: generous for
    a home network, and several times what the same film takes when made in software."""
    h = int(height or 0)
    if h <= 0 or h > 1200:
        return 14000, 20000
    if h > 800:
        return 8000, 12000
    if h > 540:
        return 5000, 7500
    return 2500, 4000


def hw_encode(kind: str, height: int = 0, rc: str = "") -> list:
    """The arguments that make H.264 on the card, for a picture so tall - which must
    already be what the card takes (see hw_upload).
    rc: how the size is held. 'vbr' aims at hw_rate(). Not every card can: an Intel one of
    before 2019 with the free driver knows 'cqp' only - one fixed quality, whatever it costs
    (a real film at 720p: 4 Mbit/s, where software makes 2.4). hw_find() tries 'vbr' first."""
    aim, most = hw_rate(height)
    if kind == "vaapi":
        if rc == "vbr":
            return ["-c:v", "h264_vaapi", "-rc_mode", "VBR", "-b:v", f"{aim}k", "-maxrate", f"{most}k", "-g", "60"]
        return ["-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", "25", "-g", "60"]
    if kind == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "24", "-b:v", f"{aim}k", "-maxrate", f"{most}k",
                "-bufsize", f"{most * 2}k", "-g", "60"]
    raise ValueError(kind)


def hw_upload(kind: str) -> list:
    """The last filters before the card's encoder: the picture in the form it takes."""
    return ["format=nv12", "hwupload"] if kind == "vaapi" else ["format=yuv420p"]


def hw_trial(kind: str, device: str = "", rc: str = "") -> list:
    """One second of a test picture through the card's encoder, thrown away: does it work
    here, in this container, with this driver, holding the size this way?"""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if kind == "vaapi":
        cmd += ["-vaapi_device", device]
    cmd += ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=1", "-vf", ",".join(hw_upload(kind))]
    return cmd + hw_encode(kind, 360, rc) + ["-f", "null", "-"]


def hw_find(run=None, devices=None, timeout: int = 25) -> tuple:
    """(kind, device, rc) of the first card whose trial succeeds, or ('', '', ''). Each card
    is tried holding the size ('vbr') and then at a fixed quality ('cqp'). A driver that
    crashes, a card that is busy, a device that is no card: all are 'no'."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, timeout=timeout).returncode)
    tries = [("vaapi", d, rc) for d in (hw_devices() if devices is None else devices) for rc in ("vbr", "cqp")] + [("nvenc", "", "vbr")]
    for kind, device, rc in tries:
        try:
            if run(hw_trial(kind, device, rc)) == 0:
                return kind, device, rc
        except Exception:
            continue
    return "", "", ""


def can_copy(info: dict, cap: int) -> bool:
    """May the picture be passed on untouched? Only what every browser decodes: 8-bit H.264
    from a plain file, and not taller than asked for. (A disc's picture is MPEG-2 or
    interlaced; it is always made anew.)"""
    return (info.get("kind") == "file" and info.get("vcodec") == "h264" and info.get("pix") in ("yuv420p", "yuvj420p")
            and (not cap or (info.get("height") or 0) <= cap))


def command(info: dict, start: float = 0.0, cap: int = 0, hw: tuple = ("", "", "")) -> list:
    """ffmpeg, from `start` seconds in, as fragmented MP4 on its standard output.
    hw: (kind, device, rc) from hw_find() - the picture is then encoded on that card."""
    start = max(0.0, min(float(start or 0), max(0.0, float(info.get("duration") or 0) - 1)))
    cap = cap if cap in HEIGHTS else 0
    hw = tuple(hw or ()) + ("", "", "")
    kind, device, rc = hw[0], hw[1], hw[2]
    kind = kind if kind in HW_KINDS else ""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if kind == "vaapi" and not can_copy(info, cap):
        cmd += ["-vaapi_device", device]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += list(info["input"]) + ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if can_copy(info, cap):
        cmd += ["-c:v", "copy"]
    else:
        filters = (["yadif=deint=interlaced"] if info.get("kind") == "dvd" else []) \
            + ([f"scale=-2:'min({cap},ih)'"] if cap else [])
        if kind:
            tall = int(info.get("height") or 0)
            cmd += ["-vf", ",".join(filters + hw_upload(kind))] + hw_encode(kind, min(tall, cap) if (cap and tall) else (cap or tall), rc)
        else:
            if filters:
                cmd += ["-vf", ",".join(filters)]
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-g", "60"]
    cmd += ["-c:a", "aac", "-ac", "2", "-b:a", "160k",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
    return cmd
