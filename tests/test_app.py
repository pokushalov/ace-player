import http.client
import json
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
    def test_parses_only_physical_link_rows(self):
        self.assertEqual(app.parse_netstat(NETSTAT), {"en0": (13523437139, 633866221), "en5": (0, 0)})

    def test_empty_output(self):
        self.assertEqual(app.parse_netstat(""), {})


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (app.DATA_DIR, app.HISTORY_FILE, list(app.history))
        app.DATA_DIR = Path(self.tmp.name)
        app.HISTORY_FILE = app.DATA_DIR / "history.json"
        app.history[:] = []

    def tearDown(self):
        app.DATA_DIR, app.HISTORY_FILE = self._saved[:2]
        app.history[:] = self._saved[2]
        self.tmp.cleanup()

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

    def test_corrupt_file_is_ignored(self):
        app.HISTORY_FILE.write_text("{not json")
        self.assertEqual(app.load_history(), [])
        app.HISTORY_FILE.write_text(json.dumps([{"key": 1}, "x", {"key": "k", "source": "s"}]))
        self.assertEqual([e["key"] for e in app.load_history()], ["k"])


class HttpTests(unittest.TestCase):
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
