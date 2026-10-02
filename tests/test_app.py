import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

ID = "a" * 40
ENGINE_URL = f"{app.ENGINE}/ace/getstream?id={ID}"

NETSTAT = """\
Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                         25873     0  105157673    25873     0  105157673     0
en0        1500  <Link#14>   9e:d1:4d:6f:69:28  9562695     0 13523437139  3988735     0  633866221     0
en0        1500  192.168.100/2 192.168.101.204  9562695     - 13523437139  3988735     -  633866221     -
en5        1500  <Link#9>    9e:d1:4d:6f:69:29        0     0          0        0     0          0     0
utun5      1380  <Link#20>                          500     0       9999      500     0       9999     0
"""


class NormalizeTests(unittest.TestCase):
    """User input -> stream URL. The security-relevant part: only content IDs and http(s) URLs get through."""

    def test_bare_id(self):
        self.assertEqual(app.normalize(ID), (ENGINE_URL, ID))

    def test_acestream_scheme(self):
        self.assertEqual(app.normalize(f"acestream://{ID}"), (ENGINE_URL, ID))
        self.assertEqual(app.normalize(f"acestream:////{ID}"), (ENGINE_URL, ID))

    def test_id_is_lowercased_and_trimmed(self):
        self.assertEqual(app.normalize(f"  {ID.upper()}\n"), (ENGINE_URL, ID))

    def test_engine_url_is_rewritten_to_local_engine(self):
        self.assertEqual(app.normalize(f"http://192.168.1.5:6878/ace/getstream?id={ID}"), (ENGINE_URL, ID))

    def test_http_url_passthrough(self):
        self.assertEqual(app.normalize("https://example.com/live.m3u8"), ("https://example.com/live.m3u8", None))

    def test_rejects_garbage(self):
        for bad in ["", "garbage", "acestream://short", "file:///etc/passwd", "ftp://x/y", "http://a b", "-" + ID]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.normalize(bad)


class NetstatTests(unittest.TestCase):
    """`netstat -ibn` parsing. The sample text mirrors real macOS output, including a VPN tunnel (utun)
    and duplicate per-address rows that must not be counted."""

    def test_parses_only_physical_link_rows(self):
        self.assertEqual(app.parse_netstat(NETSTAT), {"en0": (13523437139, 633866221), "en5": (0, 0)})

    def test_empty_output(self):
        self.assertEqual(app.parse_netstat(""), {})


class LibraryTests(unittest.TestCase):
    """History / pin / rename / prune. Each test gets a throwaway data dir so the real library is never touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (app.DATA_DIR, app.HISTORY_FILE, list(app.history), dict(app.tombstones), app._disk_mtime)
        app.DATA_DIR = Path(self.tmp.name)
        app.HISTORY_FILE = app.DATA_DIR / "history.json"
        app.history[:] = []
        app.tombstones.clear()
        app._disk_mtime = 0

    def tearDown(self):
        app.DATA_DIR, app.HISTORY_FILE = self._saved[:2]
        app.history[:] = self._saved[2]
        app.tombstones.clear()
        app.tombstones.update(self._saved[3])
        app._disk_mtime = self._saved[4]
        self.tmp.cleanup()

    def other_instance(self, code):
        """Run `code` in a separate Python process sharing this test's data dir: a second Ace Player."""
        env = dict(os.environ, ACE_DATA_DIR=self.tmp.name)
        script = "import app; app.load_library(); " + code
        subprocess.run([sys.executable, "-c", script], cwd=str(Path(app.__file__).parent), env=env, check=True)

    def test_record_and_count(self):
        app.record_play(ID, f"acestream://{ID}")
        app.record_play(ID, f"acestream://{ID}")
        self.assertEqual(len(app.history), 1)
        self.assertEqual(app.history[0]["plays"], 2)

    def test_pin_rename_delete(self):
        app.record_play(ID, f"acestream://{ID}")
        self.assertTrue(app.update_history("rename", ID, "  My   channel \n"))
        self.assertTrue(app.update_history("pin", ID, True))
        self.assertEqual((app.history[0]["name"], app.history[0]["pinned"]), ("My channel", True))
        self.assertTrue(app.update_history("delete", ID))
        self.assertEqual(app.history, [])

    def test_unknown_entry_or_action(self):
        self.assertFalse(app.update_history("rename", "nope", "x"))
        app.record_play(ID, "s")
        self.assertFalse(app.update_history("explode", ID))

    def test_name_is_capped(self):
        app.record_play(ID, "s")
        app.update_history("rename", ID, "x" * 500)
        self.assertEqual(len(app.history[0]["name"]), app.NAME_MAX)

    def test_pinned_survive_pruning_and_order(self):
        app.record_play("pinned", "pinned")
        app.update_history("pin", "pinned", True)
        for i in range(app.HISTORY_MAX + 5):
            app.record_play(f"k{i}", f"k{i}")
        keys = [e["key"] for e in app.history]
        self.assertIn("pinned", keys)
        self.assertEqual(len(keys), app.HISTORY_MAX + 1)
        self.assertEqual(app.sorted_history()[0]["key"], "pinned")

    def test_clear_keeps_pinned(self):
        app.record_play("a", "a")
        app.record_play("b", "b")
        app.update_history("pin", "b", True)
        app.update_history("clear", "")
        self.assertEqual([e["key"] for e in app.history], ["b"])

    def test_persists_and_reloads(self):
        app.record_play(ID, f"acestream://{ID}")
        app.update_history("rename", ID, "News")
        self.assertEqual(app.load_history()[0]["name"], "News")

    def test_legacy_list_format_still_loads(self):
        app.HISTORY_FILE.write_text(json.dumps([{"key": "k", "source": "s", "last_played": 5, "plays": 2}]))
        entry = app.load_history()[0]
        self.assertEqual((entry["key"], entry["plays"], entry["updated"]), ("k", 2, 5.0))

    # -- two running instances must not wipe each other's changes ------------------

    def test_stale_instance_does_not_overwrite_the_other(self):
        app.record_play("m1", "m1")
        self.other_instance("app.record_play('s1', 's1')")      # the other instance adds s1
        app.record_play("m2", "m2")                              # we still only knew m1: used to drop s1
        self.assertEqual({e["key"] for e in app.history}, {"m1", "s1", "m2"})
        self.assertEqual({e["key"] for e in app.load_history()}, {"m1", "s1", "m2"})

    def test_sync_from_disk_picks_up_other_instance(self):
        app.record_play("m1", "m1")
        rev = app.history_rev
        self.other_instance("app.record_play('s1', 's1'); app.update_history('pin', 's1', True)")
        app.sync_from_disk()
        pinned = {e["key"] for e in app.history if e["pinned"]}
        self.assertEqual(pinned, {"s1"})
        self.assertGreater(app.history_rev, rev)
        rev = app.history_rev
        app.sync_from_disk()                                     # nothing new: must not bump again
        self.assertEqual(app.history_rev, rev)

    def test_delete_is_not_undone_by_stale_instance(self):
        app.record_play("k", "k")
        self.other_instance("app.update_history('delete', 'k')")
        app.record_play("m2", "m2")                              # our memory still has k
        self.assertEqual({e["key"] for e in app.history}, {"m2"})

    def test_corrupt_file_is_ignored(self):
        app.HISTORY_FILE.write_text("{not json")
        self.assertEqual(app.load_history(), [])
        app.HISTORY_FILE.write_text(json.dumps([{"key": 1}, "x", {"key": "k", "source": "s"}]))
        self.assertEqual([e["key"] for e in app.load_history()], ["k"])


def entry(key, updated, **kw):
    return {"key": key, "source": key, "name": "", "pinned": False, "last_played": updated,
            "plays": 1, "updated": updated, **kw}


class MergeTests(unittest.TestCase):
    """merge_library is the pure core of multi-instance safety: no files, no threads."""

    def test_newest_version_wins(self):
        items, _ = app.merge_library([entry("k", 1, name="old")], {}, [entry("k", 2, name="new")], {})
        self.assertEqual([e["name"] for e in items], ["new"])

    def test_play_count_takes_the_larger(self):
        items, _ = app.merge_library([entry("k", 2, plays=3)], {}, [entry("k", 1, plays=7)], {})
        self.assertEqual(items[0]["plays"], 7)

    def test_union_of_disjoint_entries(self):
        items, _ = app.merge_library([entry("a", 1)], {}, [entry("b", 1)], {})
        self.assertEqual({e["key"] for e in items}, {"a", "b"})

    def test_tombstone_beats_older_copy(self):
        items, dead = app.merge_library([], {"k": 10}, [entry("k", 5)], {}, now=11)  # `now` close to the tombstone
        self.assertEqual(items, [])
        self.assertIn("k", dead)

    def test_newer_readd_beats_tombstone(self):
        items, dead = app.merge_library([entry("k", 20)], {}, [], {"k": 10})
        self.assertEqual([e["key"] for e in items], ["k"])
        self.assertNotIn("k", dead)

    def test_old_tombstones_expire(self):
        _, dead = app.merge_library([], {"k": 1}, [], {}, now=1 + app.TOMBSTONE_TTL + 1)
        self.assertEqual(dead, {})

    def test_size_cap_keeps_pinned(self):
        many = [entry(f"k{i}", i + 1) for i in range(app.HISTORY_MAX + 3)]
        items, _ = app.merge_library(many, {}, [entry("old-pinned", 0.5, pinned=True)], {})
        keys = {e["key"] for e in items}
        self.assertIn("old-pinned", keys)
        self.assertEqual(len(keys), app.HISTORY_MAX + 1)


class PortTests(unittest.TestCase):
    """Startup port selection: keep the preferred port when free, otherwise fall back to another one."""

    def setUp(self):
        self._saved_port = app.PORT

    def tearDown(self):
        app.PORT = self._saved_port

    def test_uses_preferred_port_when_free(self):
        with socket.socket() as s:  # find a free port, then release it
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        app.PORT = free
        server = app.bind_server()
        try:
            self.assertEqual(server.server_address[1], free)
        finally:
            server.server_close()

    def test_falls_back_when_preferred_port_is_taken(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            app.PORT = busy.getsockname()[1]
            server = app.bind_server()
            try:
                self.assertNotEqual(server.server_address[1], app.PORT)
                self.assertTrue(server.server_address[1] > 0)
            finally:
                server.server_close()

    def test_port_in_use_probe(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            self.assertTrue(app.port_in_use(s.getsockname()[1]))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        self.assertFalse(app.port_in_use(free))


class HttpTests(unittest.TestCase):
    """The HTTP layer against a real server on a random port: static files, the state API, and above all
    the Host/Origin checks that stop other websites from driving VLC or the engine."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None, host=None, origin=None):
        host = host or f"127.0.0.1:{self.port}"
        headers = {"Host": host}
        if origin is not None:
            headers["Origin"] = origin
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def own_origin(self):
        return f"http://127.0.0.1:{self.port}"

    def test_static_files_served(self):
        for path in ["/", "/static/style.css", "/static/app.js"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 200)

    def test_state_shape(self):
        status, data = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(data)
        self.assertTrue({"engine", "now", "stream", "net", "history_rev"} <= set(state))
        self.assertEqual(state["engine"]["image"], app.IMAGE)
        self.assertTrue(state["engine"]["image_url"].startswith("https://hub.docker.com/r/"))

    def test_localhost_host_is_allowed(self):
        self.assertEqual(self.request("GET", "/api/state", host=f"localhost:{self.port}")[0], 200)

    def test_foreign_host_is_rejected(self):
        self.assertEqual(self.request("GET", "/api/state", host="evil.com")[0], 403)

    def test_post_requires_own_origin(self):
        for origin in [None, "http://evil.com"]:
            with self.subTest(origin=origin):
                status, _ = self.request("POST", "/api/play", {"source": ID}, origin=origin)
                self.assertEqual(status, 403)

    def test_post_rejects_foreign_host_even_with_own_origin(self):
        status, _ = self.request("POST", "/api/stop", {}, host="evil.com", origin=self.own_origin())
        self.assertEqual(status, 403)

    def test_play_rejects_bad_input(self):
        status, data = self.request("POST", "/api/play", {"source": "garbage"}, origin=self.own_origin())
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(data)["ok"])

    def test_history_unknown_entry(self):
        status, _ = self.request("POST", "/api/history", {"action": "pin", "key": "nope"}, origin=self.own_origin())
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
