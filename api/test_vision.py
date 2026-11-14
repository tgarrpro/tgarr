"""The parts of vision.py that need no models: the verdict and how a picture is prepared."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["TGARR_VISION_DIR"] = tempfile.mkdtemp()
import vision  # noqa: E402

n = 0


def check(what, got, want):
    global n
    n += 1
    assert got == want, f"{what}: {got!r}, wanted {want!r}"


# the photo model's word counts whatever the picture is
check("adult photo", vision.verdict(0.9, 0.1, 0.0) >= vision.ADULT_AT, True)
check("plain photo", vision.verdict(0.1, 0.1, 0.0) >= vision.ADULT_AT, False)
# the drawing model's word counts only for a drawing
check("explicit drawing", vision.verdict(0.05, 0.9, 0.8) >= vision.ADULT_AT, True)
check("harmless drawing", vision.verdict(0.05, 0.9, 0.1) >= vision.ADULT_AT, False)
check("r18 said of a photograph is ignored", vision.verdict(0.05, 0.2, 0.95) >= vision.ADULT_AT, False)
check("a drawing the photo model flags", vision.verdict(0.7, 0.9, 0.1) >= vision.ADULT_AT, True)

# nothing installed: says so, starts nothing by itself
check("not installed", vision.installed(), False)
check("not available", vision.available(), False)
st = vision.status()
check("status", (st["installed"], st["installing"], st["bytes"]), (False, False, vision.SIZE))
check("three models", sorted(vision.FILES), ["adult", "drawn", "rating"])
check("every model has a checksum", all(len(f[2]) == 64 and f[1] > 0 for f in vision.FILES.values()), True)
try:
    vision.look("/nowhere.jpg")
    check("look without models", "answered", "refused")
except RuntimeError:
    n += 1

try:
    import numpy as np
    from PIL import Image
except ImportError:
    np = None
if np is not None:
    x = vision.pixels(Image.new("L", (50, 20), 255))
    check("shape", x.shape, (1, 3, 384, 384))
    check("white is 1", float(x.max()), 1.0)
    check("black is -1", float(vision.pixels(Image.new("RGBA", (9, 9), (0, 0, 0, 255))).min()), -1.0)
    check("float32", str(x.dtype), "float32")

print(f"test_vision: {n} checks passed")
