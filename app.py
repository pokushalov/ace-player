#!/usr/bin/env python3
"""Ace Player - a local web controller for AceStream + VLC on macOS.

Paste an acestream:// link, a content ID or a stream URL; the app starts the AceStream
engine (Docker) when needed, opens the stream in VLC, and charts network throughput.

Run:  python3 app.py     (or ./web.sh)       Open: http://localhost:8888
Standard library only. Listens on loopback.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("ACE_PORT", "8888"))
ENGINE_PORT = int(os.environ.get("ACE_ENGINE_PORT", "6878"))
ENGINE = f"http://127.0.0.1:{ENGINE_PORT}"
CONTAINER_NAME = "acestream-engine"
IMAGE = "vstavrinov/acestream-engine:latest"
IMAGE_URL = "https://hub.docker.com/r/" + IMAGE.split(":")[0]
HISTORY_SECONDS = 300
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

DOCKER_CANDIDATES = [
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
    "/Applications/Docker.app/Contents/Resources/bin/docker",
]
VLC_APPS = ["/Applications/VLC.app", str(Path.home() / "Applications" / "VLC.app")]
DATA_DIR = Path(os.environ.get("ACE_DATA_DIR") or Path.home() / "Library" / "Application Support" / "AcePlayer")
HISTORY_FILE = DATA_DIR / "history.json"
HISTORY_MAX = 50       # unpinned entries kept; pinned entries are never pruned
NAME_MAX = 80

ID_RE = re.compile(r"^(?:acestream://+|https?://[^/\s]+/ace/getstream\?id=)?([0-9a-fA-F]{40})$")
URL_RE = re.compile(r"^https?://\S+$")

lock = threading.Lock()
samples = deque(maxlen=HISTORY_SECONDS)
iface_name = os.environ.get("ACE_IFACE", "")
engine_state = {"running": None, "error": "", "version": ""}
stream_state = {}
# phase: idle | connecting (waiting for peers) | playing (VLC launched) | error
now_playing = {"url": "", "hash": "", "phase": "idle", "since": 0, "error": "", "vlc": False, "token": 0}


history = []           # [{key, source, name, pinned, last_played, plays}]
history_rev = 0        # bumped on every change so the page knows when to refetch


# -- library (history / pins / names) ------------------------------------------

def load_history():
    try:
        raw = json.loads(HISTORY_FILE.read_text())
    except (OSError, ValueError):
        return []
    items = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict) and isinstance(e.get("key"), str) and isinstance(e.get("source"), str):
            items.append({
                "key": e["key"], "source": e["source"],
                "name": str(e.get("name", ""))[:NAME_MAX],
                "pinned": bool(e.get("pinned", False)),
                "last_played": float(e.get("last_played", 0) or 0),
                "plays": int(e.get("plays", 0) or 0),
            })
    return items


def save_history():
    """Atomic write. Caller holds `lock`. A failure to persist must never break playback."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(history, indent=2))
        os.replace(tmp, HISTORY_FILE)
    except OSError:
        pass


def record_play(key, source):
    global history_rev
    with lock:
        entry = next((e for e in history if e["key"] == key), None)
        if entry is None:
            entry = {"key": key, "source": source, "name": "", "pinned": False, "last_played": 0, "plays": 0}
            history.append(entry)
        entry["last_played"] = time.time()
        entry["plays"] += 1
        recent = sorted((e for e in history if not e["pinned"]), key=lambda e: e["last_played"], reverse=True)
        for old in recent[HISTORY_MAX:]:
            history.remove(old)
        history_rev += 1
        save_history()


def update_history(action, key, value=None):
    """rename | pin | delete | clear. Returns False when the entry does not exist."""
    global history_rev
    with lock:
        if action == "clear":
            history[:] = [e for e in history if e["pinned"]]
        else:
            entry = next((e for e in history if e["key"] == key), None)
            if entry is None:
                return False
            if action == "rename":
                entry["name"] = " ".join(str(value or "").split())[:NAME_MAX]
            elif action == "pin":
                entry["pinned"] = bool(value)
            elif action == "delete":
                history.remove(entry)
            else:
                return False
        history_rev += 1
        save_history()
    return True


def sorted_history():
    with lock:
        items = [dict(e) for e in history]
    # Pinned first, then most recently played.
    return sorted(items, key=lambda e: (not e["pinned"], -e["last_played"]))


# -- input ---------------------------------------------------------------------

def normalize(raw: str):
    """Return (stream_url, content_id_or_None) for user input, or raise ValueError."""
    raw = raw.strip()
    m = ID_RE.match(raw)
    if m:
        content_id = m.group(1).lower()
        return f"{ENGINE}/ace/getstream?id={content_id}", content_id
    if URL_RE.match(raw):
        return raw, None
    raise ValueError("Expected an acestream:// link, a 40-character content ID, or an http(s) URL.")


# -- external tools ------------------------------------------------------------

def find_docker():
    return shutil.which("docker") or next((c for c in DOCKER_CANDIDATES if os.path.exists(c)), None)


def docker(*args, timeout=30):
    exe = find_docker()
    if not exe:
        return 127, "", "Docker not found. Install Docker Desktop (see ./install.sh)."
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "Docker timed out."
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def vlc_binary():
    for app in VLC_APPS:
        exe = Path(app) / "Contents" / "MacOS" / "VLC"
        if exe.exists():
            return str(exe)
    return None


def vlc_installed():
    return vlc_binary() is not None or subprocess.run(["open", "-Ra", "VLC"], capture_output=True).returncode == 0


def vlc_running():
    return subprocess.run(["pgrep", "-x", "VLC"], capture_output=True).returncode == 0


def launch_vlc(url):
    exe = vlc_binary()
    if exe:
        subprocess.Popen(
            [exe, "--network-caching=15000", "--http-reconnect", "--repeat", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    else:
        subprocess.Popen(["open", "-a", "VLC", url])


def stop_vlc_playback():
    """Stop playback but leave VLC itself open. Returns an error string or ''."""
    if not vlc_running():
        return ""
    r = subprocess.run(["osascript", "-e", 'tell application "VLC" to stop'], capture_output=True, text=True)
    return "" if r.returncode == 0 else (r.stderr.strip() or "osascript failed")


# -- engine --------------------------------------------------------------------

def engine_running():
    code, out, _ = docker("ps", "--format", "{{.Names}}")
    return code == 0 and CONTAINER_NAME in out.splitlines()


def start_engine():
    """Start (or create) the engine container. Returns (ok, message)."""
    code, _, err = docker("info")
    if code != 0:
        return False, err if code == 127 else "Docker is not running. Start Docker Desktop and try again."
    if engine_running():
        return True, "already running"
    code, out, _ = docker("ps", "-a", "--format", "{{.Names}}")
    if CONTAINER_NAME in out.splitlines():
        code, _, err = docker("start", CONTAINER_NAME)
    else:
        # The image is amd64-only (emulated on Apple Silicon). Bind to loopback: no need to expose it to the LAN.
        code, _, err = docker(
            "run", "-d", "--name", CONTAINER_NAME, "--restart", "unless-stopped",
            "--platform", "linux/amd64", "-p", f"127.0.0.1:{ENGINE_PORT}:{ENGINE_PORT}", IMAGE,
            timeout=900,  # first run pulls the image
        )
    return code == 0, err


def stop_engine():
    code, _, err = docker("stop", CONTAINER_NAME, timeout=60)
    return code == 0, err


def engine_get(path, timeout=3):
    with urllib.request.urlopen(ENGINE + path, timeout=timeout) as resp:
        return json.loads(resp.read())


def wait_for_engine(timeout=30):
    for _ in range(timeout):
        try:
            engine_get("/webui/api/service?method=get_version", timeout=1)
            return True
        except Exception:
            time.sleep(1)
    return False


def stream_status(content_id):
    return engine_get(f"/webui/api/service?method=get_stream_status&id={content_id}").get("result", {}) or {}


def prewarm_stream(url):
    """Make the engine register the stream, then drop the connection."""
    try:
        with urllib.request.urlopen(url, timeout=2) as conn:
            conn.read(64)
    except Exception:
        pass  # a timeout / short read is expected


def wait_for_peers(content_id, token, timeout=15):
    """Wait until the engine reports peers or data; give up after `timeout` s (VLC keeps retrying)."""
    for _ in range(timeout):
        with lock:
            if now_playing["token"] != token:
                return
        try:
            result = stream_status(content_id)
            if (result.get("peers") or 0) > 0 or result.get("status") == "dl":
                return
        except Exception:
            pass
        time.sleep(1)


# -- playback ------------------------------------------------------------------

def set_now(token, **fields):
    with lock:
        if now_playing["token"] == token:
            now_playing.update(fields)


def play_worker(url, content_id, token):
    if content_id:
        prewarm_stream(url)
        wait_for_peers(content_id, token)
    with lock:
        if now_playing["token"] != token:
            return  # superseded by Stop or another Play
    try:
        launch_vlc(url)
    except OSError as e:
        set_now(token, phase="error", error=f"Cannot launch VLC: {e}")
    else:
        set_now(token, phase="playing")


def start_playback(source):
    """Returns (ok, payload, http_status); payload carries `url` or `error`."""
    try:
        url, content_id = normalize(source)
    except ValueError as e:
        return False, {"error": str(e)}, 400
    if not vlc_installed():
        return False, {"error": "VLC not found. Install it with: brew install --cask vlc"}, 400
    if content_id:
        ok, msg = start_engine()
        if not ok:
            return False, {"error": f"Could not start the engine: {msg}"}, 502
        if not wait_for_engine():
            return False, {"error": "The engine did not respond within 30 s."}, 504
    with lock:
        token = now_playing["token"] + 1
        now_playing.update(url=url, hash=content_id or "", phase="connecting", since=time.time(), error="", token=token)
        stream_state.clear()
    record_play(content_id or url, f"acestream://{content_id}" if content_id else url)
    threading.Thread(target=play_worker, args=(url, content_id, token), daemon=True).start()
    return True, {"url": url}, 200


def stop_playback():
    with lock:
        content_id = now_playing["hash"]
        now_playing.update(url="", hash="", phase="idle", since=0, error="", token=now_playing["token"] + 1)
        stream_state.clear()
    if content_id:
        try:
            engine_get(f"/ace/stop?id={content_id}")  # best effort; closing VLC's connection also ends the session
        except Exception:
            pass
    return stop_vlc_playback()


# -- network sampling ----------------------------------------------------------

def parse_netstat(text):
    """Parse `netstat -ibn` into {iface: (rx_bytes, tx_bytes)} for physical en* interfaces."""
    counters = {}
    for line in text.splitlines():
        f = line.split()
        # Link-level rows only; Ibytes/Obytes sit at fixed offsets from the end.
        if len(f) >= 10 and re.match(r"^en\d+\*?$", f[0]) and "<Link#" in line:
            try:
                counters[f[0].rstrip("*")] = (int(f[-5]), int(f[-2]))
            except ValueError:
                continue
    return counters


def read_counters():
    # -n: no name resolution (without it netstat blocks on DNS for seconds).
    return parse_netstat(subprocess.run(["netstat", "-ibn"], capture_output=True, text=True).stdout)


def monitor():
    """Background loop, once per second: network sample + cached engine/VLC/stream state."""
    global iface_name
    prev = prev_t = None
    tick = 0
    while True:
        try:
            counters = read_counters()
            if not iface_name or iface_name not in counters:
                # The default route is often a VPN tunnel; the busiest en* is the real wire load.
                iface_name = max(counters, key=lambda k: sum(counters[k]), default="")
                prev = None
            t = time.monotonic()
            if iface_name in counters:
                cur = counters[iface_name]
                if prev is not None:
                    dt = max(t - prev_t, 0.001)
                    down = max(cur[0] - prev[0], 0) * 8 / dt / 1e6
                    up = max(cur[1] - prev[1], 0) * 8 / dt / 1e6
                    with lock:
                        samples.append({"t": time.time(), "down": round(down, 3), "up": round(up, 3)})
                prev, prev_t = cur, t
        except Exception:
            prev = None
        try:
            vlc = vlc_running()
            with lock:
                now_playing["vlc"] = vlc
                content_id = now_playing["hash"] if now_playing["phase"] in ("connecting", "playing") else ""
            if tick % 3 == 0:
                running = engine_running()
                with lock:
                    engine_state["running"] = running
                    need_version = running and not engine_state["version"]
                if need_version:
                    try:
                        version = engine_get("/webui/api/service?method=get_version", timeout=2)["result"]["version"]
                        with lock:
                            engine_state["version"] = str(version)
                    except Exception:
                        pass
            if content_id and tick % 3 == 0:
                try:
                    r = stream_status(content_id)
                    with lock:
                        stream_state.update(status=r.get("status"), peers=r.get("peers"), error=r.get("error"))
                except Exception:
                    pass
        except Exception:
            pass
        tick += 1
        time.sleep(1)


def snapshot(since):
    with lock:
        return {
            "engine": dict(engine_state, image=IMAGE, image_url=IMAGE_URL),
            "now": {k: v for k, v in now_playing.items() if k != "token"},
            "stream": dict(stream_state),
            "net": {"iface": iface_name, "samples": [s for s in samples if s["t"] > since]},
            "history_rev": history_rev,
        }


# -- http ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _hosts(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self):
        return self.headers.get("Host") in self._hosts()

    def do_GET(self):
        # Host check blocks DNS-rebinding reads of /api/state from other sites.
        if not self._host_ok():
            return self._json(403, {"error": "forbidden"})
        path, _, query = self.path.partition("?")
        if path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            return self._send(200, (STATIC_DIR / name).read_bytes(), ctype)
        if path == "/api/state":
            m = re.search(r"(?:^|&)since=([0-9.]+)", query)
            return self._json(200, snapshot(float(m.group(1)) if m else 0.0))
        if path == "/api/history":
            return self._json(200, {"items": sorted_history()})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        # Only our own page may drive playback: CSRF + DNS rebinding protection.
        origins = {f"http://{h}" for h in self._hosts()}
        if not self._host_ok() or self.headers.get("Origin") not in origins:
            return self._json(403, {"ok": False, "error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 65536:
                raise ValueError("body too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (ValueError, TypeError) as e:
            return self._json(400, {"ok": False, "error": str(e)})

        if self.path == "/api/play":
            ok, payload, code = start_playback(str(body.get("source", "")))
            return self._json(code, {"ok": ok, **payload})
        if self.path == "/api/stop":
            err = stop_playback()
            return self._json(500 if err else 200, {"ok": not err, "error": err})
        if self.path == "/api/history":
            ok = update_history(str(body.get("action", "")), str(body.get("key", "")), body.get("value"))
            return self._json(200 if ok else 404, {"ok": ok, "error": "" if ok else "no such entry or action"})
        if self.path == "/api/engine":
            action = body.get("action")
            if action == "start":
                ok, msg = start_engine()
            elif action == "stop":
                stop_playback()
                ok, msg = stop_engine()
            else:
                return self._json(400, {"ok": False, "error": "unknown action"})
            with lock:
                engine_state["running"] = engine_running() if ok else engine_state["running"]
            return self._json(200 if ok else 502, {"ok": ok, "error": "" if ok else msg})
        self._json(404, {"ok": False, "error": "not found"})


def main():
    history[:] = load_history()
    threading.Thread(target=monitor, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Ace Player running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
