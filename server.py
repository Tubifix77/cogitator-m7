"""Cogitator M-VII host bridge.

Serves cogitator.html on localhost (which Ollama's default CORS rules accept)
and exposes read-only hardware telemetry the browser cannot read by itself.

    python server.py               -> starts, wakes Ollama if needed, opens the browser
    python server.py --no-browser  -> same, without opening a browser tab
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import psutil

ROOT = Path(__file__).resolve().parent
VAULTS = ROOT / "vaults"
VAULT_EXTS = {".txt", ".md", ".log", ".dat", ".doc"}
ASSET_EXTS = {".webp", ".png", ".jpg", ".jpeg"}  # images the page may load from assets/
PORT = int(os.environ.get("COGITATOR_PORT", "8077"))
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# psutil needs the same Process objects across calls for cpu_percent to mean anything.
_proc_cache: dict[int, psutil.Process] = {}
_proc_lock = threading.Lock()
psutil.cpu_percent(None)


def gpu_readings():
    fields = "name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw"
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW,
        ).stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None
    name, util, temp, used, total, power = [v.strip() for v in out.split(",")]

    def num(v):
        try:
            return float(v)
        except ValueError:
            return None

    return {"name": name, "util": num(util), "temp": num(temp),
            "mem_used_mb": num(used), "mem_total_mb": num(total), "power_w": num(power)}


def disks():
    result = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        if u.total < 50 * 2**30:  # skip small removable / virtual drives
            continue
        result.append({"name": part.mountpoint.rstrip("\\/"), "used": u.used, "total": u.total})
    return result


def telemetry():
    vm = psutil.virtual_memory()
    return {"gpu": gpu_readings(), "cpu": psutil.cpu_percent(None),
            "ram": {"used": vm.used, "total": vm.total}, "disks": disks()}


def processes(limit=8):
    rows = []
    with _proc_lock:
        live = set()
        for p in psutil.process_iter(["pid", "name", "memory_info"]):
            pid = p.info["pid"]
            if pid == 0 or not p.info["memory_info"]:
                continue
            live.add(pid)
            proc = _proc_cache.setdefault(pid, p)
            try:
                cpu = proc.cpu_percent(None) / psutil.cpu_count()
            except psutil.Error:
                continue
            rows.append({"pid": pid, "name": p.info["name"], "cpu": round(cpu, 1),
                         "mem_mb": round(p.info["memory_info"].rss / 2**20)})
        for pid in list(_proc_cache):
            if pid not in live:
                del _proc_cache[pid]
    rows.sort(key=lambda r: r["mem_mb"], reverse=True)
    return rows[:limit]


def vault_list():
    if not VAULTS.is_dir():
        return []
    return sorted(f.name for f in VAULTS.iterdir() if f.is_file() and f.suffix.lower() in VAULT_EXTS)


def vault_read(name):
    path = VAULTS / Path(name).name  # basename only: no escaping the vault
    if path.suffix.lower() not in VAULT_EXTS or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


# ~3k tokens: leaves the 8k context room for persona, augury, chat history and the reply.
ARCHIVE_BUDGET_CHARS = 12_000


def vault_archive(budget=ARCHIVE_BUDGET_CHARS):
    """All vault records the servo-brain can hold at once; the rest are named as sealed."""
    records, sealed, used = [], [], 0
    for name in vault_list():
        text = (vault_read(name) or "").strip()
        if used + len(text) <= budget:
            records.append({"name": name, "content": text})
            used += len(text)
        else:
            sealed.append(name)
    return {"records": records, "sealed": sealed, "chars": used, "budget": budget}


# Lifecycle: the backend lives exactly as long as a console page is open.
# Every open page polls /api/telemetry with its client id (the heartbeat) and sends
# /api/bye from `pagehide` when its tab closes. Once no page is left, the backend
# unloads the models the pages used from VRAM and exits.
GRACE_S = 10       # after the last page leaves: a reload (F5) re-registers within this
STALE_S = 120      # no heartbeat this long = page is gone (crash); hidden tabs may poll only once a minute
STARTUP_S = 300    # no page ever connected (browser didn't open): give up after this

_clients: dict[str, float] = {}   # client id -> last heartbeat (monotonic)
_models_used: set[str] = set()
_ever_connected = False
_empty_since: float | None = None
_life_lock = threading.Lock()
_powering_down = threading.Event()
_started = time.monotonic()


def heartbeat(client, model):
    global _ever_connected, _empty_since
    if not client:
        return
    with _life_lock:
        _clients[client] = time.monotonic()
        _ever_connected = True
        _empty_since = None
        if model:
            _models_used.add(model)


def bye(client):
    with _life_lock:
        _clients.pop(client, None)


def ollama_request(path, payload=None, timeout=5):
    req = urllib.request.Request(
        f"http://localhost:11434{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.load(res)


def unload_models():
    """Evict from VRAM every model a console page used (other users' models are left alone)."""
    with _life_lock:
        used = set(_models_used)
    try:
        loaded = {m["name"] for m in ollama_request("/api/ps")["models"]}
    except (OSError, ValueError, KeyError):
        return
    for name in sorted(used & loaded):
        try:
            ollama_request("/api/generate", {"model": name, "keep_alive": 0}, timeout=10)
            print(f"Servo-brain {name} unloaded from VRAM.")
        except (OSError, ValueError):
            print(f"WARNING: could not unload {name}.")


def power_down(server, reason):
    if _powering_down.is_set():
        return
    _powering_down.set()
    print(f"Powering down: {reason}")
    unload_models()
    threading.Thread(target=server.shutdown, daemon=True).start()


def watchdog(server):
    global _empty_since
    while not _powering_down.wait(1):
        now = time.monotonic()
        with _life_lock:
            for cid, seen in list(_clients.items()):
                if now - seen > STALE_S:
                    del _clients[cid]
            if _clients:
                continue
            if not _ever_connected:
                if now - _started < STARTUP_S:
                    continue
                reason = f"no console page connected within {STARTUP_S} s"
            else:
                _empty_since = _empty_since or now
                if now - _empty_since < GRACE_S:
                    continue
                reason = "last console page closed"
        power_down(server, reason)


def hook_console_close():
    """Closing the console window: unload models before Windows kills the process (~5 s budget)."""
    if sys.platform != "win32":
        return
    import ctypes
    HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def on_event(event):
        if event in (2, 5, 6):  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
            print("Console closing - unloading servo-brain...")
            unload_models()
            return 1
        return 0  # Ctrl+C / Ctrl+Break: fall through to Python's KeyboardInterrupt

    hook_console_close.handler = HandlerRoutine(on_event)  # keep a reference alive
    ctypes.windll.kernel32.SetConsoleCtrlHandler(hook_console_close.handler, True)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path in ("/", "/cogitator.html"):
            self.path = "/cogitator.html"
            return super().do_GET()
        if path.startswith("/assets/"):
            name = Path(unquote(path[len("/assets/"):])).name  # basename only: no escaping assets/
            if Path(name).suffix.lower() in ASSET_EXTS and (ROOT / "assets" / name).is_file():
                self.path = f"/assets/{name}"
                return super().do_GET()
            return self.send_json({"error": "not found"}, 404)
        if path == "/api/telemetry":
            q = parse_qs(url.query)
            heartbeat(q.get("client", [""])[0], q.get("model", [""])[0])
            return self.send_json(telemetry())
        if path == "/api/processes":
            return self.send_json(processes())
        if path == "/api/vaults":
            return self.send_json(vault_list())
        if path == "/api/archive":
            return self.send_json(vault_archive())
        if path.startswith("/api/vaults/"):
            text = vault_read(unquote(path[len("/api/vaults/"):]))
            if text is None:
                return self.send_json({"error": "not found"}, 404)
            return self.send_json({"content": text})
        # Nothing else in the project folder (server source, .claude/, notes) is served.
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        # Only the console page itself may end the session: a foreign website posting
        # to localhost carries its own Origin and is refused.
        if self.headers.get("Origin") not in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self.send_json({"error": "forbidden"}, 403)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            data = {}
        path = urlparse(self.path).path
        if path == "/api/bye":
            bye(data.get("client"))
            return self.send_json({"ok": True})
        if path == "/api/shutdown":
            self.send_json({"ok": True})
            return threading.Thread(target=power_down, args=(self.server, "POWER DOWN rite from the console")).start()
        return self.send_json({"error": "not found"}, 404)

    def do_HEAD(self):
        self.send_json({"error": "method not allowed"}, 405)

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass  # page reloaded or closed mid-poll; nothing to report

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/"):  # telemetry polling would flood the log
            super().log_message(fmt, *args)


class CogitatorServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port, so a double
    # launch would silently run two servers. Claim the port exclusively instead.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def ollama_alive():
    try:
        with urllib.request.urlopen("http://localhost:11434/api/version", timeout=2):
            return True
    except OSError:
        return False


def ensure_ollama():
    """Start Ollama in the background if it isn't running (it normally lives in the tray)."""
    if ollama_alive():
        print("Servo-brain link: Ollama is running.")
        return
    exe = shutil.which("ollama")
    if not exe:
        print("WARNING: Ollama is not running and not installed: https://ollama.com/download")
        return
    print("Ollama not running - starting 'ollama serve' in the background...")
    flags = (NO_WINDOW | 0x00000008) if sys.platform == "win32" else 0  # + DETACHED_PROCESS
    subprocess.Popen([exe, "serve"], creationflags=flags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(0.5)
        if ollama_alive():
            print("Servo-brain link: Ollama is up.")
            return
    print("WARNING: Ollama did not answer within 10 s; the console will show the link as UNRESPONSIVE.")


if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    open_browser = "--no-browser" not in sys.argv
    try:
        server = CogitatorServer(("127.0.0.1", PORT), partial(Handler, directory=str(ROOT)))
    except OSError:
        # Port taken: almost always a cogitator that is already running. Just show it.
        print(f"Port {PORT} is already in use - assuming the cogitator is already running.")
        if open_browser:
            webbrowser.open(url)
        sys.exit(0)

    ensure_ollama()
    hook_console_close()
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    print(f"Cogitator M-VII online: {url}")
    print("Shuts down by itself when the console tab is closed (or close this window / Ctrl+C).")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Ctrl+C received.")
    finally:
        if not _powering_down.is_set():
            unload_models()
        server.server_close()
        print("Cogitator M-VII powered down. The Machine Spirit rests.")
