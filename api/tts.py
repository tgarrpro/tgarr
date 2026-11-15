"""Reading aloud with a voice of this machine's own.

A browser reads with whatever voices it has: Edge has Microsoft's natural ones, Chrome on most
systems a robot. Here the server makes the audio - Kokoro, a small open model, on the CPU - so
a browser, phone or television without a good voice of its own hears a natural one. It reads
Chinese and English, each with a voice of that language.

Nothing is in the image but the code. The voice (365 MB) is fetched when somebody asks for it
in Settings, the model for hard Chinese pronunciations (589 MB) only if that is asked for too;
each file is checked against a known SHA-256 before it is used. Without the voice the readers
go on using the browser's own, as before.

    status()                what is installed, what is being fetched
    install(what)           fetch 'voice' or 'pronunciation' (in a thread; see status)
    remove(what)            delete it again
    say(text) -> bytes      a WAV of the text, from the cache when it was said before
"""
import hashlib
import io
import logging
import os
import shutil
import tarfile
import threading
import time
import urllib.request
import wave
import zipfile

import tts_text

log = logging.getLogger("tgarr")

ROOT = os.environ.get("TGARR_TTS_DIR", "/downloads/.tts")
MODELS = os.path.join(ROOT, "models")
CACHE = os.path.join(ROOT, "cache")
CACHE_MAX_BYTES = int(os.environ.get("TGARR_TTS_CACHE_MB", "300")) * 1024 * 1024
IDLE_UNLOAD_S = 15 * 60
TEXT_MAX = 600

VOICE_DIR = os.path.join(MODELS, "kokoro-multi-lang-v1_1")
# Who reads: (key, what the reader's menu calls it, the model's number for the speaker). The
# first of each is used when nothing was chosen. Of the model's hundred Chinese speakers 3-57
# are women and 58-102 men; its English ones are three women, so a man's voice for English
# is the Chinese one going on ("same").
VOICES = {
    "zh": (("f1", "Woman 1 (higher)", 3), ("f2", "Woman 2", 4), ("f3", "Woman 3 (lower)", 57),
           ("m1", "Man 1", 58), ("m2", "Man 2 (deep)", 60), ("m3", "Man 3 (lighter)", 61)),
    "en": (("us1", "Woman, American", 0), ("us2", "Woman, American (another)", 1), ("uk", "Woman, British", 2),
           ("same", "The voice chosen for Chinese", None)),
}
_SID = {lang: {k: sid for k, _label, sid in vs} for lang, vs in VOICES.items()}


def speaker(chinese: bool, zh: str = "", en: str = "") -> int:
    """The model's speaker for a piece, from what the reader chose (anything else: the first)."""
    z = _SID["zh"].get(zh, VOICES["zh"][0][2])
    if chinese:
        return z
    e = _SID["en"].get(en, VOICES["en"][0][2]) if en in _SID["en"] else VOICES["en"][0][2]
    return z if e is None else e
G2PW_DIR = os.path.join(MODELS, "G2PWModel")
BERT_VOCAB = os.path.join(MODELS, "bert-base-chinese-vocab.txt")
STANDIN_LEXICON = os.path.join(MODELS, "lexicon-with-stand-ins.txt")

# (url, bytes, sha256, where it goes, how it is unpacked)
FILES = {
    "voice": (
        # the full model: the smaller one of the same name (int8) is slower than speech on a CPU
        ("https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-multi-lang-v1_1.tar.bz2", 364855331,
         "b78e1ac42b852e839cceaafbe88f66806bf5860992d1d7b9a3f2ad1198427aed", VOICE_DIR, "tar"),
    ),
    "pronunciation": (
        ("https://huggingface.co/YC-Chen/g2pW/resolve/main/G2PWModel-v2-onnx.zip", 589075404,
         "699f3c1fd7fb0e2c2d49ed2486826fd5bff233fee7759350a91c3b49aedc4ed2", G2PW_DIR, "zip"),
        ("https://huggingface.co/bert-base-chinese/resolve/main/vocab.txt", 109540,
         "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c", BERT_VOCAB, "file"),
    ),
}
SIZES = {k: sum(f[1] for f in v) for k, v in FILES.items()}
READY_MARK = {"voice": os.path.join(VOICE_DIR, ".ready"), "pronunciation": os.path.join(G2PW_DIR, ".ready")}

_state = {"voice": {"doing": False, "done": 0, "error": ""}, "pronunciation": {"doing": False, "done": 0, "error": ""}}
_lock = threading.Lock()                # one sentence at a time: the model uses every core it is given
_engine = {"tts": None, "lex": None, "reader": None, "words": None, "used": 0.0, "key": ""}


def installed(what: str) -> bool:
    return os.path.exists(READY_MARK[what])


def available() -> bool:
    """Can this machine read aloud by itself right now?"""
    if not installed("voice"):
        return False
    try:
        import sherpa_onnx  # noqa: F401
        return True
    except Exception:
        return False


def status() -> dict:
    out = {"available": available(), "loaded": _engine["tts"] is not None}
    for what in FILES:
        s = _state[what]
        out[what] = {"installed": installed(what), "installing": s["doing"], "bytes": SIZES[what], "done": s["done"], "error": s["error"]}
    try:
        n = b = 0
        for r, _d, fs in os.walk(CACHE):
            for f in fs:
                n += 1
                b += os.path.getsize(os.path.join(r, f))
        out["cache"] = {"clips": n, "bytes": b}
    except OSError:
        out["cache"] = {"clips": 0, "bytes": 0}
    return out


# ───────────────────────── fetching ─────────────────────────

def _inside(base: str, name: str) -> bool:
    return os.path.realpath(os.path.join(base, name)).startswith(os.path.realpath(base) + os.sep)


def _fetch(what: str, url: str, size: int, sha256: str, dest: str, how: str) -> None:
    os.makedirs(MODELS, exist_ok=True)
    part = dest + ".part"
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "tgarr"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            _state[what]["done"] += len(chunk)
    if os.path.getsize(part) != size or h.hexdigest() != sha256:
        os.unlink(part)
        raise RuntimeError(f"{os.path.basename(url)} is not the file that was expected (it may have been replaced where it is kept); not installed")
    if how == "file":
        os.replace(part, dest)
        return
    tmp = dest + ".unpacking"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        if how == "tar":
            with tarfile.open(part) as t:
                for m in t.getmembers():
                    if not (m.isfile() or m.isdir()) or not _inside(tmp, m.name):      # no links, nothing outside
                        continue
                    t.extract(m, tmp)
        else:
            with zipfile.ZipFile(part) as z:
                for m in z.infolist():
                    if _inside(tmp, m.filename):
                        z.extract(m, tmp)
        inner = [x for x in os.listdir(tmp)]
        src = os.path.join(tmp, inner[0]) if len(inner) == 1 and os.path.isdir(os.path.join(tmp, inner[0])) else tmp
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(src, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.unlink(part)
        except OSError:
            pass


def _install(what: str) -> None:
    s = _state[what]
    try:
        for url, size, sha256, dest, how in FILES[what]:
            _fetch(what, url, size, sha256, dest, how)
        with open(READY_MARK[what], "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        log.info("[tts] %s installed", what)
    except Exception as e:
        s["error"] = str(e)[:300]
        log.warning("[tts] installing %s: %s", what, e)
    finally:
        s["doing"] = False
        unload()


def install(what: str) -> bool:
    if what not in FILES or _state[what]["doing"] or installed(what):
        return False
    _state[what].update(doing=True, done=0, error="")
    threading.Thread(target=_install, args=(what,), daemon=True).start()
    return True


def remove(what: str) -> None:
    if what not in FILES or _state[what]["doing"]:
        return
    unload()
    for _url, _size, _sha, dest, _how in FILES[what]:
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        else:
            try:
                os.unlink(dest)
            except OSError:
                pass
    if what == "voice":
        shutil.rmtree(CACHE, ignore_errors=True)
        try:
            os.unlink(STANDIN_LEXICON)
        except OSError:
            pass


# ───────────────────────── the voice ─────────────────────────

def unload() -> None:
    with _lock:
        _engine.update(tts=None, lex=None, reader=None, words=None, key="")


def unload_if_idle() -> None:
    """Called now and then: a model nobody has used for a quarter of an hour is let go
    (the voice takes about 600 MB of memory, the pronunciation model about a gigabyte)."""
    if _engine["tts"] is not None and time.time() - _engine["used"] > IDLE_UNLOAD_S:
        unload()
        log.info("[tts] voice put away after a quarter of an hour unused")


def _load(pronunciation: bool) -> None:
    import sherpa_onnx
    key = "p" if (pronunciation and installed("pronunciation")) else "-"
    if _engine["tts"] is not None and _engine["key"] == key:
        return
    zh = os.path.join(VOICE_DIR, "lexicon-zh.txt")
    lex = tts_text.Lexicon.read(zh)
    with open(STANDIN_LEXICON + ".new", "w", encoding="utf-8") as g:
        g.write(lex.lexicon_text(zh))
    os.replace(STANDIN_LEXICON + ".new", STANDIN_LEXICON)
    rule = ",".join(p for p in (os.path.join(VOICE_DIR, n) for n in ("phone-zh.fst", "date-zh.fst", "number-zh.fst")) if os.path.exists(p))
    threads = max(1, min(4, (os.cpu_count() or 2) - 1))
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=os.path.join(VOICE_DIR, "model.onnx"), voices=os.path.join(VOICE_DIR, "voices.bin"),
                tokens=os.path.join(VOICE_DIR, "tokens.txt"), lexicon=os.path.join(VOICE_DIR, "lexicon-us-en.txt") + "," + STANDIN_LEXICON,
                data_dir=os.path.join(VOICE_DIR, "espeak-ng-data"), dict_dir=os.path.join(VOICE_DIR, "dict")),
            num_threads=threads, provider="cpu"),
        rule_fsts=rule, max_num_sentences=1)
    t = time.time()
    tts = sherpa_onnx.OfflineTts(cfg)
    words = None
    try:
        import jieba
        import jieba.posseg
        jieba.setLogLevel(logging.WARNING)
        jieba.initialize()

        def words(text, _cut=jieba.posseg.cut):
            return [(w, flag) for w, flag in _cut(text, HMM=False)]
        words("好。")                               # the splitter loads its tables on first use
    except Exception as e:                       # without the word splitter every entry of the voice's list counts
        log.warning("[tts] no word splitter: %s", e)
    reader = None
    if key == "p":
        try:
            import g2pw_lite
            reader = g2pw_lite.Reader(G2PW_DIR, BERT_VOCAB, threads=min(2, threads))
        except Exception as e:
            log.warning("[tts] the pronunciation model could not be loaded: %s", e)
    _engine.update(tts=tts, lex=lex, reader=reader, words=words, key=key)
    log.info("[tts] voice ready in %.1fs (%d threads%s)", time.time() - t, threads, ", with the pronunciation model" if reader else "")


def _model_of(lex, reader):
    if reader is None:
        return None

    def ask(text):
        said = lex.said(text)
        where = [i for i, ch in enumerate(text) if said[i] and ch in tts_text.POLYPHONIC]
        got = reader(text, where) if where else {}
        return [got.get(i) for i in range(len(text))]
    return ask


def _wav(samples, rate: int) -> bytes:
    import numpy as np
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _cache_path(text: str, key: str, sid: int) -> str:
    h = hashlib.sha1(("k1|" + key + "|" + str(sid) + "|" + text).encode("utf-8")).hexdigest()
    return os.path.join(CACHE, h[:2], h + ".wav")


def say(text: str, pronunciation: bool = True, zh: str = "", en: str = "") -> bytes:
    """The text as a WAV (24 kHz, one channel). Raises RuntimeError when it cannot be said."""
    text = (text or "").strip()[:TEXT_MAX]
    if not text:
        raise RuntimeError("nothing to say")
    if not available():
        raise RuntimeError("no voice is installed")
    key = "p" if (pronunciation and installed("pronunciation")) else "-"
    sid = speaker(bool(tts_text.HAN.search(text)), zh, en)
    path = _cache_path(text, key, sid)
    try:
        with open(path, "rb") as f:
            data = f.read()
        os.utime(path, None)
        return data
    except OSError:
        pass
    with _lock:
        _load(pronunciation)
        e = _engine
        e["used"] = time.time()
        prepared = tts_text.prepare(text, e["lex"], e["words"], _model_of(e["lex"], e["reader"]))
        if not prepared:
            raise RuntimeError("nothing in it to say")
        audio = e["tts"].generate(prepared, sid=sid, speed=1.0)
        if len(audio.samples) < audio.sample_rate // 20:
            raise RuntimeError("the voice made no sound for this")
        data = _wav(audio.samples, audio.sample_rate)
        e["used"] = time.time()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
    except OSError:
        pass
    return data


def trim_cache() -> int:
    """Keep what was said lately; the oldest go when the cache is over its size. Returns
    the number of clips removed."""
    files, total = [], 0
    for r, _d, fs in os.walk(CACHE):
        for f in fs:
            p = os.path.join(r, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
            total += st.st_size
    files.sort()
    n = 0
    while files and total > CACHE_MAX_BYTES:
        _m, size, p = files.pop(0)
        try:
            os.unlink(p)
            total -= size
            n += 1
        except OSError:
            pass
    return n
