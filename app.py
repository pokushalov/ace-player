#!/usr/bin/env python3
"""Ace Player - a local web controller for AceStream + VLC on macOS.

Paste an acestream:// link, a content ID or a stream URL; the app starts the AceStream
engine (Docker) when needed, opens the stream in VLC, and charts network throughput.

Run:  python3 app.py     (or ./web.sh)       Open: http://localhost:8888
Standard library only. Listens on loopback.

How it fits together
--------------------
* The UI (static/) is a single page that polls GET /api/state once a second and sends
  commands with POST /api/{play,stop,engine,history}.
* A background thread (`monitor`) samples network counters and refreshes cached engine/VLC
  state, so request handlers never block on slow external commands (docker, netstat, pgrep).
* Playing a stream is asynchronous: /api/play starts the engine and returns, then a worker
  thread waits for peers and launches VLC. `now_playing["phase"]` tells the page where it is.
* All shared state lives in module-level globals guarded by one lock (`lock`). Never run a
  subprocess or network call while holding it.
* Security model: loopback only, plus Host/Origin checks on every request (see Handler).
  There is no authentication - anyone with local access to the Mac can use the app.
"""
import argparse
import fcntl
import json
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Always loopback: the app has no authentication, so it must never listen on the LAN.
HOST = "127.0.0.1"
# Preferred port for the UI. If it is taken, bind_server() falls back to a random free one.
PORT = int(os.environ.get("ACE_PORT", "8888"))
ENGINE_PORT = int(os.environ.get("ACE_ENGINE_PORT", "6878"))
# 127.0.0.1 rather than "localhost": avoids an IPv6 (::1) lookup the engine may not answer on.
ENGINE = f"http://127.0.0.1:{ENGINE_PORT}"
CONTAINER_NAME = "acestream-engine"
# A third-party build of the (proprietary) AceStream engine - see the License section of the README.
# The UI displays this name, so keep it in sync if you change the image.
IMAGE = "vstavrinov/acestream-engine:latest"
IMAGE_URL = "https://hub.docker.com/r/" + IMAGE.split(":")[0]
HISTORY_SECONDS = 300  # how much network history the chart keeps (one sample per second)

# Static files are served from an explicit whitelist, never by joining a request path onto
# a directory, so path traversal is impossible by construction.
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

# Where to look for `docker` when it is not on PATH (e.g. the app was started from a GUI launcher
# with a minimal environment). Order: Intel Homebrew/Docker Desktop symlink, Apple Silicon
# Homebrew, the binary inside Docker.app.
DOCKER_CANDIDATES = [
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
    "/Applications/Docker.app/Contents/Resources/bin/docker",
]
VLC_APPS = ["/Applications/VLC.app", str(Path.home() / "Applications" / "VLC.app")]

# The library lives outside the repo so `git pull` / reinstalling never touches user data.
DATA_DIR = Path(os.environ.get("ACE_DATA_DIR") or Path.home() / "Library" / "Application Support" / "AcePlayer")
HISTORY_FILE = DATA_DIR / "history.json"
HISTORY_MAX = 50       # unpinned entries kept; pinned entries are never pruned
NAME_MAX = 80          # cap for user-given names (keeps the file and the UI tidy)

# What the user may paste. A content ID is exactly 40 hex characters, optionally wrapped as
# acestream://ID or as an engine URL (which is rewritten to OUR engine, see normalize()).
ID_RE = re.compile(r"^(?:acestream://+|https?://[^/\s]+/ace/getstream\?id=)?([0-9a-fA-F]{40})$")
# Any other stream (HLS, a torrent URL, ...) is passed straight to VLC. http(s) only: no file://,
# no custom schemes that could make VLC open something local.
URL_RE = re.compile(r"^https?://\S+$")

# -- shared state ----------------------------------------------------------------
# Everything below is read and written by several threads (HTTP handlers, `monitor`,
# `play_worker`) and is guarded by `lock`.
lock = threading.Lock()
samples = deque(maxlen=HISTORY_SECONDS)   # [{t, down, up}] network throughput in Mbit/s
iface_name = os.environ.get("ACE_IFACE", "")   # interface being charted; "" = pick automatically
engine_state = {"running": None, "error": "", "version": ""}   # running is None until first check
stream_state = {}                         # last engine report for the current stream (peers, status)

# What the user is watching right now.
#   phase: idle | connecting (waiting for peers) | playing (VLC launched) | error
#   vlc:   whether a VLC process exists (refreshed by `monitor`)
#   token: generation counter. Every Play/Stop bumps it; a `play_worker` that sees a different
#          token knows it was superseded and must not launch VLC any more.
now_playing = {"url": "", "hash": "", "phase": "idle", "since": 0, "error": "", "vlc": False, "token": 0}

history = []           # [{key, source, name, pinned, last_played, plays}]
history_rev = 0        # bumped on every change so the page knows when to refetch


# -- library (history / pins / names) ------------------------------------------

# The library file may be written by more than one running instance (e.g. two copies started by
# accident, or an old one left over). Each process keeps its own in-memory copy, so a naive
# "write my whole list to disk" would silently wipe the other's changes. Instead every write is a
# merge: read the file, combine it with memory, write the result - under a file lock.
#
# Merge rule, per entry (keyed by `key`): the version with the newest `updated` wins. A delete
# leaves a "tombstone" (key -> deletion time) so it beats older copies of the entry that other
# processes still hold; a newer re-add beats the tombstone. `plays` takes the larger count.
tombstones = {}        # key -> time the entry was deleted
_disk_mtime = 0        # st_mtime_ns of history.json when we last read or wrote it
TOMBSTONE_TTL = 30 * 86400   # forget deletions after 30 days so the file does not grow forever


def _clean_entry(e):
    """Validate one stored entry; returns a normalized dict or None for junk."""
    if not (isinstance(e, dict) and isinstance(e.get("key"), str) and isinstance(e.get("source"), str)):
        return None
    last_played = float(e.get("last_played", 0) or 0)
    return {
        "key": e["key"], "source": e["source"],
        "name": str(e.get("name", ""))[:NAME_MAX],
        "pinned": bool(e.get("pinned", False)),
        "last_played": last_played,
        "plays": int(e.get("plays", 0) or 0),
        # Files written before merging existed have no `updated`; last_played is the best guess.
        "updated": float(e.get("updated", last_played) or 0),
    }


def _read_disk():
    """Read the library file. Returns (items, tombstones).

    A missing or corrupt file just means an empty library, and junk entries are dropped, so a bad
    file can never crash the app on startup. Also accepts the original format (a bare list).
    """
    try:
        raw = json.loads(HISTORY_FILE.read_text())
    except (OSError, ValueError):
        return [], {}
    if isinstance(raw, list):
        raw_items, raw_dead = raw, {}
    elif isinstance(raw, dict):
        raw_items, raw_dead = raw.get("items", []), raw.get("deleted", {})
    else:
        return [], {}
    items = [c for c in (_clean_entry(e) for e in raw_items if isinstance(e, dict)) if c]
    dead = {k: float(v) for k, v in raw_dead.items() if isinstance(k, str) and isinstance(v, (int, float))} \
        if isinstance(raw_dead, dict) else {}
    return items, dead


def load_history():
    """The stored entries only (see _read_disk)."""
    return _read_disk()[0]


def merge_library(items_a, dead_a, items_b, dead_b, now=None):
    """Combine two copies of the library into one. Pure function: no I/O, no shared state.

    Returns (items, tombstones). See the comment above for the rule. Also applies the size cap
    (HISTORY_MAX unpinned entries) and drops tombstones older than TOMBSTONE_TTL.
    """
    now = time.time() if now is None else now
    merged = {}
    for e in items_a + items_b:
        cur = merged.get(e["key"])
        if cur is None:
            merged[e["key"]] = dict(e)
        elif e["updated"] > cur["updated"]:
            merged[e["key"]] = dict(e, plays=max(cur["plays"], e["plays"]))
        else:
            cur["plays"] = max(cur["plays"], e["plays"])

    dead = dict(dead_a)
    for k, t in dead_b.items():
        dead[k] = max(dead.get(k, 0), t)
    for k in list(merged):
        if dead.get(k, 0) > merged[k]["updated"]:
            del merged[k]       # deleted after the newest version we know of
        else:
            dead.pop(k, None)   # re-added (or edited) after the deletion: the tombstone is obsolete
    dead = {k: t for k, t in dead.items() if now - t < TOMBSTONE_TTL}

    unpinned = sorted((e for e in merged.values() if not e["pinned"]), key=lambda e: e["last_played"], reverse=True)
    for old in unpinned[HISTORY_MAX:]:
        del merged[old["key"]]
    return list(merged.values()), dead


def save_history():
    """Merge memory with the file, write the result, and adopt it. Caller holds `lock`.

    The whole read-merge-write runs under an exclusive flock on a sidecar lock file, so two
    processes cannot interleave and lose each other's changes. The write goes to a temp file that
    is renamed into place, so a crash mid-write cannot leave an unparseable history.json.
    A failure to persist must never break playback, so I/O errors are swallowed.
    """
    global _disk_mtime
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "history.lock", "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)   # released when the file is closed
            disk_items, disk_dead = _read_disk()
            items, dead = merge_library(history, tombstones, disk_items, disk_dead)
            tmp = HISTORY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"items": items, "deleted": dead}, indent=2))
            os.replace(tmp, HISTORY_FILE)
            history[:] = items                      # adopt what the other instance added
            tombstones.clear()
            tombstones.update(dead)
            _disk_mtime = HISTORY_FILE.stat().st_mtime_ns
    except OSError:
        pass


def sync_from_disk():
    """Pick up changes another running instance wrote, so both stay in step without a restart.

    Called every second by `monitor`. It is just a stat() unless the file changed, and our own
    writes are recognised by their mtime so we do not re-merge them.
    """
    global history_rev, _disk_mtime
    try:
        mtime = HISTORY_FILE.stat().st_mtime_ns
    except OSError:
        return
    with lock:
        if mtime == _disk_mtime:
            return
        _disk_mtime = mtime
        disk_items, disk_dead = _read_disk()
        items, dead = merge_library(history, tombstones, disk_items, disk_dead)
        by_key = lambda e: e["key"]
        if sorted(items, key=by_key) != sorted(history, key=by_key) or dead != tombstones:
            history[:] = items
            tombstones.clear()
            tombstones.update(dead)
            history_rev += 1    # tell the page to refetch the library


def load_library():
    """Startup: load entries and tombstones from disk into memory."""
    global _disk_mtime
    items, dead = _read_disk()
    history[:] = items
    tombstones.clear()
    tombstones.update(dead)
    try:
        _disk_mtime = HISTORY_FILE.stat().st_mtime_ns
    except OSError:
        _disk_mtime = 0


def record_play(key, source):
    """Add a stream to the library or bump an existing entry, then prune old unpinned ones.

    `key` identifies the stream (content ID, or the URL for plain http streams) so replaying the
    same stream updates one entry instead of creating duplicates. `source` is what gets shown
    and replayed (acestream://ID or the URL).
    """
    global history_rev
    with lock:
        now = time.time()
        entry = next((e for e in history if e["key"] == key), None)
        if entry is None:
            entry = {"key": key, "source": source, "name": "", "pinned": False,
                     "last_played": 0, "plays": 0, "updated": 0}
            history.append(entry)
        entry["last_played"] = entry["updated"] = now
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
        now = time.time()
        if action == "clear":
            for e in history:
                if not e["pinned"]:
                    tombstones[e["key"]] = now
            history[:] = [e for e in history if e["pinned"]]
        else:
            entry = next((e for e in history if e["key"] == key), None)
            if entry is None:
                return False
            if action == "rename":
                entry["name"] = " ".join(str(value or "").split())[:NAME_MAX]
                entry["updated"] = now
            elif action == "pin":
                entry["pinned"] = bool(value)
                entry["updated"] = now
            elif action == "delete":
                history.remove(entry)
                tombstones[key] = now   # so the delete is not undone by a stale copy elsewhere
            else:
                return False
        history_rev += 1
        save_history()
    return True


def sorted_history():
    """Library as the page shows it. Copies the entries so callers cannot mutate shared state."""
    with lock:
        items = [dict(e) for e in history]
    # Pinned first, then most recently played.
    return sorted(items, key=lambda e: (not e["pinned"], -e["last_played"]))


# -- input ---------------------------------------------------------------------

def normalize(raw: str):
    """Return (stream_url, content_id_or_None) for user input, or raise ValueError.

    A content ID (bare, acestream://ID, or an engine URL pasted from elsewhere) is always
    rewritten to OUR engine's getstream URL, so a pasted URL can never point VLC at another host
    by pretending to be an engine link. Plain http(s) URLs pass through with no content ID.
    """
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
    """Path to the docker CLI, or None. See DOCKER_CANDIDATES for why PATH alone is not enough."""
    return shutil.which("docker") or next((c for c in DOCKER_CANDIDATES if os.path.exists(c)), None)


def docker(*args, timeout=30):
    """Run `docker <args>`. Returns (exit_code, stdout, stderr); never raises.

    Synthetic exit codes: 127 = docker not installed, 124 = timed out (same as the shell's convention).
    """
    exe = find_docker()
    if not exe:
        return 127, "", "Docker not found. Install Docker Desktop (see ./install.sh)."
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "Docker timed out."
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def vlc_binary():
    """Path to the VLC executable inside VLC.app, or None if VLC is not in a known location."""
    for app in VLC_APPS:
        exe = Path(app) / "Contents" / "MacOS" / "VLC"
        if exe.exists():
            return str(exe)
    return None


def vlc_installed():
    """True if VLC can be launched. Falls back to asking Launch Services (`open -Ra`), which also
    finds VLC installed in a non-standard place."""
    return vlc_binary() is not None or subprocess.run(["open", "-Ra", "VLC"], capture_output=True).returncode == 0


def vlc_running():
    """True if any VLC process exists. Exact name match (-x) so e.g. a `vlc-something` helper does not count."""
    return subprocess.run(["pgrep", "-x", "VLC"], capture_output=True).returncode == 0


def launch_vlc(url):
    """Open `url` in VLC.

    Flags: a 15 s network buffer smooths out peer-to-peer jitter; --http-reconnect retries if the
    engine is not serving yet; --repeat keeps a live stream going if VLC thinks it ended.
    start_new_session detaches VLC so closing this app (Ctrl+C) does not kill the player.
    """
    exe = vlc_binary()
    if exe:
        subprocess.Popen(
            [exe, "--network-caching=15000", "--http-reconnect", "--repeat", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    else:
        # Non-standard install location: let Launch Services find it (no custom flags possible).
        subprocess.Popen(["open", "-a", "VLC", url])


def stop_vlc_playback():
    """Stop playback but leave VLC itself open. Returns an error string or ''.

    Uses AppleScript, so macOS asks once whether Python may control VLC (Automation permission).
    If that is denied, the error text surfaces in the UI instead of failing silently.
    """
    if not vlc_running():
        return ""
    r = subprocess.run(["osascript", "-e", 'tell application "VLC" to stop'], capture_output=True, text=True)
    return "" if r.returncode == 0 else (r.stderr.strip() or "osascript failed")


# -- engine --------------------------------------------------------------------

def engine_running():
    """True if the engine container is up. False also when Docker itself is down."""
    code, out, _ = docker("ps", "--format", "{{.Names}}")
    return code == 0 and CONTAINER_NAME in out.splitlines()


def start_engine():
    """Start (or create) the engine container. Returns (ok, message).

    Three cases: already running (no-op), exists but stopped (docker start), or first run
    (docker run, which also pulls the image).
    """
    # `docker info` fails fast with a clear reason when Docker Desktop is not running, which beats
    # a confusing error from `docker run` further down.
    code, _, err = docker("info")
    if code != 0:
        return False, err if code == 127 else "Docker is not running. Start Docker Desktop and try again."
    if engine_running():
        return True, "already running"
    code, out, _ = docker("ps", "-a", "--format", "{{.Names}}")
    if CONTAINER_NAME in out.splitlines():
        code, _, err = docker("start", CONTAINER_NAME)
    else:
        # The image is amd64-only, so pin the platform (emulated on Apple Silicon). Publish the port on
        # loopback only: the engine has no authentication and must not be reachable from the LAN.
        # --restart unless-stopped brings it back after a Docker/Mac restart.
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
    """GET a path on the engine's HTTP API and return the parsed JSON. Raises on any failure."""
    with urllib.request.urlopen(ENGINE + path, timeout=timeout) as resp:
        return json.loads(resp.read())


def wait_for_engine(timeout=30):
    """Poll until the engine answers (a freshly started container needs a few seconds)."""
    for _ in range(timeout):
        try:
            engine_get("/webui/api/service?method=get_version", timeout=1)
            return True
        except Exception:
            time.sleep(1)
    return False


def stream_status(content_id):
    """The engine's report for one stream: status, peers, speed... ({} if it has none yet).
    `content_id` is always 40 hex characters here (validated by normalize), so it is URL-safe."""
    return engine_get(f"/webui/api/service?method=get_stream_status&id={content_id}").get("result", {}) or {}


def prewarm_stream(url):
    """Make the engine register the stream, then drop the connection.

    The engine only starts looking for peers once someone requests the stream. Requesting it
    here, before VLC, lets us watch the peer count and open VLC when there is data to play.
    """
    try:
        with urllib.request.urlopen(url, timeout=2) as conn:
            conn.read(64)
    except Exception:
        pass  # a timeout / short read is expected


def wait_for_peers(content_id, token, timeout=15):
    """Wait until the engine reports peers or data; give up after `timeout` s (VLC keeps retrying).

    Also returns early when `token` is stale, i.e. the user pressed Stop or started another stream.
    """
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
    """Update now_playing only if `token` is still current, so a stale worker cannot overwrite
    the state of a newer Play/Stop."""
    with lock:
        if now_playing["token"] == token:
            now_playing.update(fields)


def play_worker(url, content_id, token):
    """Background half of Play: warm up the stream, wait for peers, then launch VLC.

    Runs in its own thread because waiting for peers can take ~15 s and must not block the request.
    """
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
    """Handle a Play request. Returns (ok, payload, http_status); payload carries `url` or `error`.

    Validation and engine startup happen synchronously so errors reach the user immediately;
    waiting for peers and launching VLC are handed to `play_worker`.
    """
    try:
        url, content_id = normalize(source)
    except ValueError as e:
        return False, {"error": str(e)}, 400
    if not vlc_installed():
        return False, {"error": "VLC not found. Install it with: brew install --cask vlc"}, 400
    if content_id:
        # Plain http(s) streams do not need the engine, so only start it for content IDs.
        ok, msg = start_engine()
        if not ok:
            return False, {"error": f"Could not start the engine: {msg}"}, 502
        if not wait_for_engine():
            return False, {"error": "The engine did not respond within 30 s."}, 504
    with lock:
        # New token: any worker still running for a previous stream will notice and bail out.
        token = now_playing["token"] + 1
        now_playing.update(url=url, hash=content_id or "", phase="connecting", since=time.time(), error="", token=token)
        stream_state.clear()
    record_play(content_id or url, f"acestream://{content_id}" if content_id else url)
    threading.Thread(target=play_worker, args=(url, content_id, token), daemon=True).start()
    return True, {"url": url}, 200


def stop_playback():
    """Stop the current stream. Returns an error string ('' on success).

    Bumping the token first cancels a `play_worker` that is still waiting for peers.
    """
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
    """Parse `netstat -ibn` into {iface: (rx_bytes, tx_bytes)} for physical en* interfaces.

    Only enN interfaces are considered: lo0 is loopback, utun*/bridge* are tunnels and virtual
    switches that would double-count traffic already seen on the physical interface.
    """
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
    """Current byte counters per physical interface (see parse_netstat)."""
    # -n: no name resolution (without it netstat blocks on DNS for seconds).
    return parse_netstat(subprocess.run(["netstat", "-ibn"], capture_output=True, text=True).stdout)


def monitor():
    """Background loop, once per second: network sample + cached engine/VLC/stream state.

    All slow external calls live here so HTTP handlers only read cached values. Cheap checks (netstat,
    pgrep) run every second; `docker ps` and the engine API run every third tick (`tick % 3`).
    Every step is wrapped so one failing command (Docker down, engine restarting) never kills the loop.
    """
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
                    # Rate = counter delta over the real elapsed time (sleep/netstat jitter makes the
                    # interval slightly more than 1 s). max(.., 0) guards against a counter reset.
                    dt = max(t - prev_t, 0.001)
                    down = max(cur[0] - prev[0], 0) * 8 / dt / 1e6
                    up = max(cur[1] - prev[1], 0) * 8 / dt / 1e6
                    with lock:
                        samples.append({"t": time.time(), "down": round(down, 3), "up": round(up, 3)})
                prev, prev_t = cur, t
        except Exception:
            prev = None
        try:
            sync_from_disk()    # another instance may have changed the library
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
    """The JSON the page polls. `since` is the timestamp of the newest network sample the page
    already has, so each poll carries only the new ones instead of the whole history. The
    internal `token` is deliberately not exposed."""
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
    """Routes: GET / and /static/* (UI), GET /api/state, GET /api/history, and POST
    /api/{play,stop,engine,history}. Anything else is a 404."""

    def log_message(self, fmt, *args):
        pass  # silence the per-request access log; the page polls every second

    def _send(self, code, body, ctype="application/json"):
        # no-store: state changes every second, never let the browser cache it.
        # nosniff: stops the browser from guessing a different content type than we declared.
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
        """The only Host values a legitimate request to this server can carry."""
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self):
        # DNS rebinding: a hostile site can point its own domain at 127.0.0.1, which makes the browser
        # treat our API as same-origin. Such requests carry the attacker's domain in Host, so rejecting
        # unknown Host values closes that hole.
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
        # Only our own page may drive playback. Browsers always attach Origin to cross-site POSTs, so
        # requiring it to be one of our own origins blocks CSRF (another tab/site posting to
        # http://localhost:8888). Non-browser clients such as curl simply have to send it too.
        origins = {f"http://{h}" for h in self._hosts()}
        if not self._host_ok() or self.headers.get("Origin") not in origins:
            return self._json(403, {"ok": False, "error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 65536:  # commands are tiny; refuse to buffer anything big
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


def port_in_use(port):
    """True if something is already listening on 127.0.0.1:`port`.

    Probing with a connect catches listeners that bind() alone can miss on macOS, such as a server
    bound to all interfaces (0.0.0.0) by another program.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, port)) == 0


def bind_server():
    """Create the HTTP server on PORT, or on a random free port if PORT is taken.

    Order: the preferred port, then up to 50 random ports in 8000-9999 (easy to remember, unlikely
    to need firewall rules), then port 0, which lets the OS pick any free port - so this always
    succeeds. Binding is the real test (a probe alone could race with another program starting),
    so a failed bind just moves on to the next candidate.
    """
    candidates = [PORT] + random.sample([p for p in range(8000, 10000) if p != PORT], 50) + [0]
    for port in candidates:
        if port and port_in_use(port):
            continue
        try:
            return ThreadingHTTPServer((HOST, port), Handler)
        except OSError:
            continue  # lost a race for this port; try the next one
    raise RuntimeError("could not bind any port")  # unreachable in practice: port 0 always works


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ace Player - local web UI for AceStream + VLC")
    parser.add_argument("--open", action="store_true", help="open the UI in the default browser once it is up")
    args = parser.parse_args(argv)

    # Load the library before the server accepts requests.
    load_library()
    threading.Thread(target=monitor, daemon=True).start()
    server = bind_server()
    port = server.server_address[1]
    url = f"http://localhost:{port}"
    if port != PORT:
        print(f"Port {PORT} is busy, using {port} instead.", flush=True)
    # flush=True: when stdout is a pipe or file (nohup, launchd, ./web.sh | tee) Python buffers
    # output, and the user would never see which port we ended up on.
    print(f"Ace Player running at {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if args.open:
        # The socket is already listening (created in bind_server), so the browser can connect
        # right away; requests queue until serve_forever() starts below.
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
