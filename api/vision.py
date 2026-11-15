"""Looking at pictures: is it adult, is it drawn.

Until now "adult" was a property of a channel, decided from its name and what its owner said.
A channel that calls itself harmless and is not, or one adult picture in a family chat, went
straight through - also into the Media Center, which is the one place that must not show it.
Here the picture itself is looked at, by two small open models on this machine's CPU:

    adult      a photo classifier (AdamCodd/vit-base-nsfw-detector, Apache-2.0), 88 MB
    drawn      drawing or photograph (deepghs/anime_real_cls, OpenRAIL), 17 MB

Where adult starts was measured on a real library (643 pictures of all-ages channels, each
verdict checked by its owner, and 100 left over from adult channels): at 0.5 the photo model
called 24 harmless pictures adult - album covers, paintings, a letter B - at 0.9 four, while
of the 30 it rightly found 27 stayed. So ADULT_AT is 0.9: what it says is then nearly always
true, and it misses some. A third model, rating how explicit a drawing is
(deepghs/anime_rating), was tried and left out: on book covers it was wrong every time.
Drawings are therefore told apart but not judged; an explicit drawing passes.

Pixels only - never a file name or a caption. Nothing leaves this machine. The models are
not in the image: they are fetched when asked for (105 MB) and checked against a known
SHA-256. What they cannot do: tell a person's age. Sexual material involving children is
kept out by names and groups (usenet_rules, the registry's lists), not by these models.

    status() / install() / remove()     as tts.py
    look(path) -> {"adult": 0..1, "drawn": 0..1}
"""
import hashlib
import logging
import os
import threading
import time
import urllib.request

log = logging.getLogger("tgarr")

ROOT = os.environ.get("TGARR_VISION_DIR", "/downloads/.vision")
VERSION = 2                       # of the verdict: raise it when models or the rule change, and everything is looked at again
ADULT_AT = 0.9                    # main.py's SQL carries the same number (test_vision checks)
IDLE_UNLOAD_S = 10 * 60

# name: (url, bytes, sha256)
FILES = {
    "adult": ("https://huggingface.co/AdamCodd/vit-base-nsfw-detector/resolve/main/onnx/model_quantized.onnx", 88500985,
              "432763a6899ebc418c55f784b98f90565e5fc694c778d2ffcb0294b12f6a7404"),
    "drawn": ("https://huggingface.co/deepghs/anime_real_cls/resolve/main/mobilenetv3_v1.4_dist/model.onnx", 16821153,
              "c02d68cc7f56dbcbcd4f41a10df190385c1f963f22c4eefb8d162869800ed3b0"),
}
SIZE = sum(f[1] for f in FILES.values())
LABELS = {"adult": ("sfw", "nsfw"), "drawn": ("anime", "real")}
READY = os.path.join(ROOT, ".ready")

_state = {"doing": False, "done": 0, "error": ""}
_lock = threading.Lock()
_models = {"sessions": None, "used": 0.0}


def _path(name: str) -> str:
    return os.path.join(ROOT, name + ".onnx")


def installed() -> bool:
    return os.path.exists(READY)


def available() -> bool:
    if not installed():
        return False
    try:
        import onnxruntime  # noqa: F401
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def status() -> dict:
    return {"installed": installed(), "available": available(), "installing": _state["doing"], "bytes": SIZE, "done": _state["done"],
            "error": _state["error"], "loaded": _models["sessions"] is not None, "version": VERSION}


def _install() -> None:
    try:
        os.makedirs(ROOT, exist_ok=True)
        for name, (url, size, sha256) in FILES.items():
            part = _path(name) + ".part"
            h = hashlib.sha256()
            req = urllib.request.Request(url, headers={"User-Agent": "tgarr"})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    _state["done"] += len(chunk)
            if os.path.getsize(part) != size or h.hexdigest() != sha256:
                os.unlink(part)
                raise RuntimeError(f"the '{name}' model is not the file that was expected (it may have been replaced where it is kept); not installed")
            os.replace(part, _path(name))
        with open(READY, "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        log.info("[vision] models installed")
    except Exception as e:
        _state["error"] = str(e)[:300]
        log.warning("[vision] installing: %s", e)
    finally:
        _state["doing"] = False


def install() -> bool:
    if _state["doing"] or installed():
        return False
    _state.update(doing=True, done=0, error="")
    threading.Thread(target=_install, daemon=True).start()
    return True


def remove() -> None:
    if _state["doing"]:
        return
    unload()
    for name in list(FILES) + ["rating", ".ready"]:        # "rating": left by version 1
        try:
            os.unlink(READY if name == ".ready" else _path(name))
        except OSError:
            pass


def unload() -> None:
    with _lock:
        _models["sessions"] = None


def unload_if_idle() -> None:
    if _models["sessions"] is not None and time.time() - _models["used"] > IDLE_UNLOAD_S:
        unload()


def _load(threads: int):
    if _models["sessions"] is None:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, threads)
        _models["sessions"] = {n: ort.InferenceSession(_path(n), sess_options=so, providers=["CPUExecutionProvider"]) for n in FILES}
    return _models["sessions"]


def pixels(img, size: int = 384):
    """A picture as all three models want it: RGB, 384 x 384, -1..1, channels first."""
    import numpy as np
    from PIL import Image
    img = img.convert("RGB").resize((size, size), Image.BICUBIC)
    return ((np.asarray(img, dtype=np.float32) / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None]


def _probs(session, x) -> list:
    """The model's answer as probabilities. Some models give scores to be turned into them,
    some (the deepghs ones) give probabilities already - taking those through softmax again
    presses everything towards the middle."""
    import numpy as np
    out = session.run(None, {session.get_inputs()[0].name: x})[0][0]
    if out.min() >= 0 and abs(float(out.sum()) - 1.0) < 0.01:
        return out.tolist()
    e = np.exp(out - out.max())
    return (e / e.sum()).tolist()


def look(path: str, threads: int = 2) -> dict:
    """{"adult": 0..1, "drawn": 0..1} for the picture at `path`; ADULT_AT and above is adult.
    Raises when it cannot be opened or no models are installed."""
    if not available():
        raise RuntimeError("no models are installed")
    from PIL import Image
    with Image.open(path) as img:
        img.load()
        x = pixels(img)
    with _lock:
        s = _load(threads)
        drawn = _probs(s["drawn"], x)[LABELS["drawn"].index("anime")]
        adult = _probs(s["adult"], x)[LABELS["adult"].index("nsfw")]
        _models["used"] = time.time()
    return {"adult": round(adult, 4), "drawn": round(drawn, 4)}
