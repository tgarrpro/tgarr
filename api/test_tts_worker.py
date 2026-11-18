"""The voice in a process of its own (tts._ask_worker): whatever happens to that process, the API's
process goes on and the request gets a plain refusal (the reader then uses the browser's voice).
Runs without the voice model: the process starts, cannot load a model, and says so."""
import time

import tts

n = 0


def ok(cond, what):
    global n
    n += 1
    assert cond, what


def refused(fn, *a):
    try:
        fn(*a)
    except RuntimeError as e:
        return str(e)
    return None


def main():
    # no model here: the voice's process starts, answers with the reason, and the parent carries on
    why = refused(tts._ask_worker, "你好", False, "", "")
    ok(why is not None and "could not say this" in why, f"without a model the process answers with a reason, not a crash ({why})")
    p = tts._w["proc"]
    ok(p is not None and p.is_alive(), "and stays up for the next sentence")

    # a process that has gone (killed by the kernel for memory, say) is noticed and started again
    p.kill()
    p.join(5)
    why = refused(tts._ask_worker, "你好", False, "", "")
    ok(why is not None and tts._w["proc"] is not None and tts._w["proc"] is not p, "a dead process is replaced on the next sentence")

    # too little memory: the voice is not started at all
    tts.unload()
    real = tts.headroom_mb
    tts.headroom_mb = lambda: 300.0
    why = refused(tts._ask_worker, "你好", False, "", "")
    ok(why is not None and "not enough free memory" in why and tts._w["proc"] is None, "under NEED_VOICE_MB nothing is started")

    # enough for the voice but not for the pronunciation model: it starts without it
    tts.headroom_mb = lambda: float(tts.NEED_VOICE_MB + 100)
    refused(tts._ask_worker, "你好", True, "", "")
    ok(tts._w["key"] == "-", "the pronunciation model is left out when it would not fit")
    tts.unload()
    tts.headroom_mb = real

    # after a crash: lighter for half an hour
    tts._w["crash_at"] = time.time()
    ok(tts._light(), "a crash just now: light")
    refused(tts._ask_worker, "你好", True, "", "")
    ok(tts._w["key"] == "-", "and the pronunciation model is not loaded")
    tts._w["crash_at"] = time.time() - tts.LIGHT_AFTER_CRASH_S - 1
    ok(not tts._light(), "half an hour later: not light any more")

    tts.unload()
    ok(tts._w["proc"] is None, "unload ends the process")
    ok(tts.status()["loaded"] is False, "and status says so")
    print(f"ok - {n} checks")


if __name__ == "__main__":
    main()
