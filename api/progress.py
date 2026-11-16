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


# The script. __ID__ = the file, __SEED__ = seed() of what is stored. It must run before the
# page's own script, which reads its place as soon as it starts.
JS = r"""<script>
(function(){
  var ID = __ID__, SEED = __SEED__, ls;
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


def script(msg_id: int, row) -> str:
    return JS.replace("__ID__", str(int(msg_id))).replace("__SEED__", seed(row))
