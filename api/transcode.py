"""Playing what a browser cannot play: converted while it is watched.

A phone or a tablet plays mp4 and little else. A DVD or Blu-ray image, an avi, an old mpeg, a
wmv, an mkv with sound the browser has no decoder for - downloaded, they are files nobody on
that device can open. Here ffmpeg reads the file as it is and hands the browser a stream it
does play (H.264 and AAC in fragmented MP4), starting anywhere: moving the slider starts
ffmpeg again from that second. Nothing is written to the disk and the file is never changed.

    needs(name)            'always' | 'maybe' | ''   - can a browser play this as it is?
    openings(path)         the ways ffmpeg may open it, in the order to try
    parse_probe(text)      what ffprobe said, as {duration, width, height, vcodec, acodec, pix}
    command(info, t, cap)  the ffmpeg command that plays from second t, no taller than cap

Only probe() runs anything; the rest is plain and checked by test_transcode.py.
"""
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


def can_copy(info: dict, cap: int) -> bool:
    """May the picture be passed on untouched? Only what every browser decodes: 8-bit H.264
    from a plain file, and not taller than asked for. (A disc's picture is MPEG-2 or
    interlaced; it is always made anew.)"""
    return (info.get("kind") == "file" and info.get("vcodec") == "h264" and info.get("pix") in ("yuv420p", "yuvj420p")
            and (not cap or (info.get("height") or 0) <= cap))


def command(info: dict, start: float = 0.0, cap: int = 0) -> list:
    """ffmpeg, from `start` seconds in, as fragmented MP4 on its standard output."""
    start = max(0.0, min(float(start or 0), max(0.0, float(info.get("duration") or 0) - 1)))
    cap = cap if cap in HEIGHTS else 0
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += list(info["input"]) + ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if can_copy(info, cap):
        cmd += ["-c:v", "copy"]
    else:
        filters = (["yadif=deint=interlaced"] if info.get("kind") == "dvd" else []) \
            + ([f"scale=-2:'min({cap},ih)'"] if cap else [])
        if filters:
            cmd += ["-vf", ",".join(filters)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-g", "60"]
    cmd += ["-c:a", "aac", "-ac", "2", "-b:a", "160k",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
    return cmd
