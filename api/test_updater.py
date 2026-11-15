"""Checks for 'Update now': what is told to the user for each way asking the updater can go.
Nothing is asked of anything. Run: python test_updater.py"""
import updater as U

n = 0


def ok(cond, what):
    global n
    assert cond, what
    n += 1


ok(U.verdict(200)[0] == "current", "it looked and found nothing newer")
ok(U.verdict(401)[0] == "refused" and U.verdict(403)[0] == "refused" and "docker compose up -d" in U.verdict(401)[1], "a wrong token: said so, with what to do")
ok(U.verdict(500)[0] == "failed" and U.BY_HAND in U.verdict(500)[1], "any other answer: the command to do it by hand")
ok(U.verdict(error="timeout")[0] == "asked" and "comes back by itself" in U.verdict(error="timeout")[1], "silence is the updater at work")
ok(U.verdict(error="refused")[0] == "away" and U.verdict(error="unknown host")[0] == "away" and "docker compose up -d" in U.verdict(error="refused")[1],
   "nothing listening, or no such container: the updater is not running")
ok(U.verdict(error="something odd")[0] == "failed" and "something odd" in U.verdict(error="something odd")[1] and U.verdict()[0] == "failed", "anything else is told as it is")
ok(all(isinstance(U.verdict(c, e)[1], str) and U.verdict(c, e)[1] for c, e in ((200, ""), (401, ""), (None, "timeout"), (None, "refused"), (None, ""))), "always something to say")
ok(U.beside("172.21.0.6", "172.21.0.5") and U.beside("10.89.3.4", "10.89.0.2"), "the updater beside this container, on the network they share")
ok(not U.beside("192.168.2.1", "172.21.0.5") and not U.beside("8.8.8.8", "172.21.0.5") and not U.beside("172.22.0.6", "172.21.0.5"),
   "an address on the home network, on the internet, or on another network of containers is not it")
ok(not U.beside("127.0.0.1", "127.0.0.1") and not U.beside("nonsense", "172.21.0.5") and not U.beside("fd00::1", "fd00::2") and not U.beside("", ""),
   "nor this container itself, nor anything that is no address")
U.URL, U.TOKEN = "", ""
ok(not U.configured() and U.ask()[0] == "none" and U.BY_HAND in U.ask()[1] and "docker-compose.yml" in U.ask()[1], "an install from an older compose file: no updater to ask, and how to get one")
U.URL, U.TOKEN = "http://127.0.0.1:9", "t"
ok(U.configured() and U.ask(timeout=3)[0] == "away", "a name that stands for an address not beside this container: nothing is sent, the updater is 'not running'")
sent = []
U.where_is = lambda url: ("172.21.0.6", "172.21.0.5")
U.urllib.request.urlopen = lambda req, timeout=0: sent.append(req.get_header("Authorization")) or (_ for _ in ()).throw(U.urllib.error.URLError(ConnectionRefusedError()))
ok(U.ask(timeout=3)[0] == "away" and sent == ["Bearer t"], "beside it: asked, with the token; nothing listening is 'not running'")
U.where_is = lambda url: ("192.168.2.1", "172.21.0.5")
ok(U.ask(timeout=3)[0] == "away" and len(sent) == 1, "the name answered for by somebody outside: the token is not sent")
import importlib
importlib.reload(U)
U.URL, U.TOKEN = "http://no-such-host.invalid:8080", "t"
ok(U.ask(timeout=5)[0] == "away", "a name that stands for nothing: not running")
print(f"test_updater: {n} checks passed")
