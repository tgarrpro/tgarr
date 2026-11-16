"""Where somebody stopped - kept on the server, so that it follows the person and not the browser.

Every player and reader of tgarr already keeps its place in the browser's own store, under a
key of its own ("tgarr.v.<id>" seconds of a film, "tgarr.read.<id>" chapter and fraction of a
book, "tgarr.pdf.<id>", "comic-<id>", "tgarr.a.<id>" seconds of a recording). That stays as it
is. What this adds sits in front of it, in one small script the pages carry (JS below):

  * when a page opens, what the server holds for this person and this file is put into the
    browser's store first - if it is newer than what the browser has - and the player then
    finds it where it always looked;
  * when the player writes its place, the same value is sent to the server (at most every few
    seconds, and once more when the page is left).

"This person" is whoever the request is for: the child whose PIN the browser has, else the
device (a browser without the PIN), else the owner. main.py decides that; nothing here does.

Besides the player's own value, a fraction 0..1 is kept where it can be told (how far into the
film, the book), and whether the thing was finished: what a list of lessons shows.
"""
import json
import re

# the keys the players use, and which of them mean the same thing (seconds into the same film)
KEYS = ("tgarr.v.", "tgarr.play.", "tgarr.read.", "tgarr.pdf.", "tgarr.a.", "comic-")
SAME = {"tgarr.v.": ("tgarr.play.",), "tgarr.play.": ("tgarr.v.",)}
VAL_MOST = 400                     # a place is a number or a small object, never more
_KEY_RX = re.compile(r"^(tgarr\.(?:v|play|read|pdf|a)\.|comic-)$")


def clean(body) -> dict:
    """What a browser sent, made safe to keep: {"key", "val", "frac", "done"} - or None when
    it is not a place at all."""
    if not isinstance(body, dict):
        return None
    key, val = body.get("key"), body.get("val")
    if not isinstance(key, str) or not _KEY_RX.match(key):
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        val = str(val)
    if val is not None and (not isinstance(val, str) or len(val) > VAL_MOST):
        return None
    frac = body.get("frac")
    if isinstance(frac, bool) or not isinstance(frac, (int, float)) or frac != frac:
        frac = None
    else:
        frac = max(0.0, min(1.0, float(frac)))
    done = body.get("done") is True
    if val is None and not done:
        return None                # nothing to remember
    if done:
        frac = 1.0
    total = body.get("total")
    if isinstance(total, bool) or not isinstance(total, (int, float)) or total != total or not 0 < total < 400000:
        total = None
    return {"key": key, "val": val, "frac": frac, "done": done, "total": None if total is None else float(total)}


def seed(row) -> str:
    """What the page is handed for one stored place: a JSON object for the script, or "null"."""
    if not row or row.get("val") is None:
        return "null"
    keys = [row["key"]] + list(SAME.get(row["key"], ()))
    return json.dumps({"keys": keys, "val": row["val"], "at": int(row["at"])}).replace("</", "<\\/")


def nav(course) -> str:
    """What the page is told when the file is a lesson of a course: where in the course it
    stands and what comes before and after ({"n", "total", "prev", "next", "back", "title",
    "word_next", "word_done"}), as JSON for the script - or "null"."""
    if not course:
        return "null"
    return json.dumps(course, ensure_ascii=False).replace("</", "<\\/")


# The script. __ID__ = the file, __SEED__ = seed() of what is stored, __NAV__ = nav() of the
# course it is a lesson of. It must run before the page's own script, which reads its place
# as soon as it starts.
JS = r"""<script>
(function(){
  var ID = __ID__, SEED = __SEED__, NAV = __NAV__, ls;
  // a lesson of a course: a small chip says which one it is and leads to the next; when the
  // lesson is done the chip grows into the way on
  var chip = null;
  function lessonChip(done){
    if (!NAV) return;
    if (!chip) {
      chip = document.createElement("div");
      chip.style.cssText = "position:fixed;z-index:2147483000;right:12px;top:64px;display:flex;align-items:center;gap:8px;padding:6px 8px 6px 12px;" +
        "border-radius:999px;background:rgba(8,10,16,.72);color:#fff;font:600 13px/1.2 system-ui,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;" +
        "box-shadow:0 6px 18px rgba(0,0,0,.35);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);max-width:calc(100vw - 24px)";
      var where = document.createElement("a");
      where.href = NAV.back; where.textContent = NAV.n + " / " + NAV.total; where.title = NAV.title;
      where.style.cssText = "color:#fff;text-decoration:none;opacity:.85;white-space:nowrap";
      chip.appendChild(where);
      if (NAV.next) {
        var go = document.createElement("a");
        go.href = "/watch/" + NAV.next; go.textContent = NAV.word_next + " \u203a"; go.id = "tgNextLesson";
        go.style.cssText = "color:#0b0d12;background:#fff;text-decoration:none;padding:6px 12px;border-radius:999px;white-space:nowrap";
        chip.appendChild(go);
      }
      (document.body || document.documentElement).appendChild(chip);
    }
    if (done) {
      var g = document.getElementById("tgNextLesson");
      chip.style.background = "#16a34a"; chip.style.fontSize = "15px"; chip.style.padding = "9px 10px 9px 16px";
      chip.firstChild.textContent = "\u2713 " + NAV.word_done + " \u00b7 " + NAV.n + " / " + NAV.total;
      if (g) { g.style.padding = "9px 16px"; g.style.fontWeight = "800"; }
    }
  }
  if (NAV) { if (document.body) lessonChip(false); else document.addEventListener("DOMContentLoaded", function(){ lessonChip(false); }); }
  try { ls = window.localStorage; if (!ls) return; } catch (e) { return; }
  var RX = /^(tgarr\.(?:v|play|read|pdf|a)\.|comic-)(\d+)$/;
  var set = Storage.prototype.setItem, del = Storage.prototype.removeItem;
  // the server's is used when this browser has none, or an older one
  try {
    if (SEED) SEED.keys.forEach(function(k){
      var mine = ls.getItem(k + ID), at = +ls.getItem(k + ID + ".at") || 0;
      if (mine === null || at <= SEED.at) { set.call(ls, k + ID, SEED.val); set.call(ls, k + ID + ".at", String(SEED.at)); }
    });
  } catch (e) {}
  var wait = null, last = 0, next = null;
  function frac(key, val){
    try {
      var m = document.querySelector("video, audio");
      // (a film that is being converted has no length the browser knows: no fraction for "tgarr.play.")
      if (m && m.duration > 0 && isFinite(m.duration) && (key === "tgarr.v." || key === "tgarr.a.")) return m.currentTime / m.duration;
      if (key === "tgarr.read.") {
        var o = JSON.parse(val), s = document.getElementById("slider"), n = s ? (+s.max || 0) + 1 : 0;
        if (n > 0) return ((+o.ch || 0) + (+o.frac || 0)) / n;
      }
      if (key === "comic-") { var n2 = document.querySelectorAll("main img").length; if (n2 > 1) return (+val || 0) / (n2 - 1); }
      if (key === "tgarr.pdf.") { var t = document.getElementById("ptotal"), o2 = JSON.parse(val), p = +(o2.page || o2.p || o2) || 0, tt = t ? +t.textContent : 0; if (tt > 1) return (p - 1) / (tt - 1); }
    } catch (e) {}
    return null;
  }
  function send(now){
    if (!next) return;
    var body = JSON.stringify(next); next = null; last = Date.now();
    try {
      if (now && navigator.sendBeacon) { navigator.sendBeacon("/api/progress/" + ID, new Blob([body], {type: "application/json"})); return; }
      fetch("/api/progress/" + ID, {method: "POST", headers: {"Content-Type": "application/json"}, body: body, keepalive: true}).catch(function(){});
    } catch (e) {}
  }
  function length(key){
    try { var m = document.querySelector("video, audio");
      if (m && m.duration > 0 && isFinite(m.duration) && (key === "tgarr.v." || key === "tgarr.a.")) return m.duration; } catch (e) {}
    return null;
  }
  function note(key, val, done){
    var f = done ? 1 : frac(key, val);
    next = {key: key, val: val, frac: f, total: length(key), done: !!done || (f !== null && f >= 0.985 && key !== "tgarr.read." ? true : false)};
    if (f !== null && key === "tgarr.read." && f >= 0.995) next.done = true;
    if (next.done) lessonChip(true);
    if (wait) return;
    wait = setTimeout(function(){ wait = null; send(false); }, Math.max(300, 8000 - (Date.now() - last)));
  }
  Storage.prototype.setItem = function(k, v){
    set.call(this, k, v);
    try { var m = this === ls && RX.exec(k); if (m && +m[2] === ID) { set.call(ls, k + ".at", String(Date.now())); note(m[1], String(v), false); } } catch (e) {}
  };
  // a player takes its place away when the film was seen to the end
  Storage.prototype.removeItem = function(k){
    del.call(this, k);
    try { var m = this === ls && RX.exec(k); if (m && +m[2] === ID) { del.call(ls, k + ".at"); note(m[1], null, true); } } catch (e) {}
  };
  addEventListener("pagehide", function(){ send(true); });
  document.addEventListener("visibilitychange", function(){ if (document.visibilityState === "hidden") send(true); });
})();
</script>
"""


def script(msg_id: int, row, course=None) -> str:
    return JS.replace("__ID__", str(int(msg_id))).replace("__NAV__", nav(course)).replace("__SEED__", seed(row))
