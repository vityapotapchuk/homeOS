#!/usr/bin/env python3
"""HomeOS - local system API and static server for the shell UI.

Standard library only. Runs inside the GTK host (homeos_shell.py) or on its
own for previewing the UI on any computer:

    python3 sysapi.py --port 8137      (preview mode: never reboots or powers off)

The server listens on 127.0.0.1 only. Every /api/ call needs the per-run
token that is embedded in index.html, and the Host header is checked, so
web pages opened in the browser cannot call it.
"""

import argparse
import glob
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apps  # noqa: E402

VERSION = "0.20.0"
BACKGROUNDS = ("plain", "aurora", "sunset", "ocean", "forest")

SEARCH_ENGINES = {
    "duckduckgo": "https://duckduckgo.com/?q={}",
    "google": "https://www.google.com/search?q={}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={}",
}

DEFAULT_SETTINGS = {
    "theme": "dark",
    "background": "plain",
    "search": "duckduckgo",
    "homepage": "",
    "camera": apps.DEFAULT_CAMERA,      # MJPEG stream shown by the Camera app
    "radio": apps.DEFAULT_RADIO,        # the HomeOS radio service on this Pi
    "detect": apps.DEFAULT_DETECT,      # motion detection service on this Pi
    "weather": None,                    # {"name", "lat", "lon"} of the town for the weather widget
    "widgets": list(apps.WIDGETS),      # widgets shown on the home screen
    "widgets_version": apps.WIDGETS_VERSION,
    "tiles": [
        {"name": "Wikipedia", "url": "https://en.wikipedia.org"},
        {"name": "YouTube", "url": "https://www.youtube.com"},
        {"name": "GitHub", "url": "https://github.com"},
        {"name": "Raspberry Pi", "url": "https://www.raspberrypi.com/documentation/"},
    ],
}

MAX_BODY = 64 * 1024


# --------------------------------------------------------------- helpers

def run(cmd, timeout=8):
    """Run a command without a shell. Returns (code, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 126, "", str(e)


def run_priv(cmd, timeout=8):
    """Try as the user first, then with passwordless sudo if that fails."""
    res = run(cmd, timeout)
    if res[0] != 0 and shutil.which("sudo"):
        res2 = run(["sudo", "-n"] + cmd, timeout)
        if res2[0] == 0:
            return res2
    return res


def read_text(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip().strip("\x00")
    except OSError:
        return None


def split_nmcli(line):
    """Split one line of `nmcli -t` output (':' separated, '\\' escapes)."""
    parts, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_HOSTLIKE = re.compile(
    r"^(localhost|\d{1,3}(\.\d{1,3}){3}|[\w\-]+(\.[\w\-]+)+)(:\d+)?([/?#].*)?$"
)


def normalize_url(text, search="duckduckgo"):
    """Turn what the user typed into a URL: address, or a web search."""
    t = (text or "").strip()
    if not t:
        return None
    if t == "about:blank":
        return t
    if _SCHEME.match(t) and not _HOSTLIKE.match(t):
        scheme = t.split(":", 1)[0].lower()
        if scheme in ("http", "https"):
            return t
        # javascript:, file:, data: ... are never opened from the UI
        return SEARCH_ENGINES[search].format(quote(t))
    if " " not in t and _HOSTLIKE.match(t):
        host = t.split("/", 1)[0].split(":", 1)[0]
        local = host == "localhost" or re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host)
        return ("http://" if local else "https://") + t
    return SEARCH_ENGINES[search].format(quote(t))


# --------------------------------------------------------------- settings

class Settings:
    def __init__(self, config_dir):
        self.path = os.path.join(config_dir, "settings.json")
        self.lock = threading.Lock()
        self.data = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.mtime = None
        self._load()

    def _load(self):
        """(Re)reads the file when it changed: the desktop shell and the web
        access service share it."""
        try:
            mtime = os.stat(self.path).st_mtime_ns
            if mtime == self.mtime:
                return
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            clean = self._clean(raw)
            if isinstance(raw, dict) and isinstance(raw.get("widgets"), list):
                seen = raw.get("widgets_version", 1)
                added = {w for v, ws in apps.NEW_WIDGETS.items() if v > seen for w in ws}
                if added:
                    clean["widgets"] = [w for w in apps.WIDGETS if w in clean.get("widgets", []) or w in added]
            self.data.update(clean)
            self.mtime = mtime
        except (OSError, ValueError):
            pass

    @staticmethod
    def _clean(raw):
        out = {}
        if not isinstance(raw, dict):
            return out
        if raw.get("theme") in ("light", "dark"):
            out["theme"] = raw["theme"]
        if raw.get("background") in BACKGROUNDS:
            out["background"] = raw["background"]
        if raw.get("search") in SEARCH_ENGINES:
            out["search"] = raw["search"]
        if isinstance(raw.get("homepage"), str):
            hp = raw["homepage"].strip()[:500]
            out["homepage"] = hp if (not hp or urlparse(hp).scheme in ("http", "https")) else ""
        if "weather" in raw:
            w = raw["weather"]
            try:
                out["weather"] = {"name": str(w["name"])[:80], "lat": float(w["lat"]), "lon": float(w["lon"])}
            except (TypeError, KeyError, ValueError):
                out["weather"] = None
        if isinstance(raw.get("widgets"), list):
            out["widgets"] = [w for w in apps.WIDGETS if w in raw["widgets"]]
        for key in ("camera", "radio", "detect"):
            if isinstance(raw.get(key), str):
                url = raw[key].strip()[:500]
                if not url or urlparse(url).scheme in ("http", "https"):
                    out[key] = url or DEFAULT_SETTINGS[key]
        if isinstance(raw.get("tiles"), list):
            tiles = []
            for t in raw["tiles"][:40]:
                if not isinstance(t, dict):
                    continue
                name = str(t.get("name", "")).strip()[:40]
                url = normalize_url(str(t.get("url", ""))[:500])
                if name and url and urlparse(url).scheme in ("http", "https"):
                    tiles.append({"name": name, "url": url})
            out["tiles"] = tiles
        return out

    def get(self):
        with self.lock:
            self._load()
            return json.loads(json.dumps(self.data))

    def update(self, raw):
        with self.lock:
            self._load()
            self.data.update(self._clean(raw))
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            try:
                self.mtime = os.stat(self.path).st_mtime_ns
            except OSError:
                pass
            return json.loads(json.dumps(self.data))


# --------------------------------------------------------------- system

class System:
    """Reads and changes system state. Every method tolerates missing tools."""

    def __init__(self, dev=False):
        self.dev = dev
        self._wifi_cache = (0.0, None)

    # ---- info
    def info(self):
        osr = read_text("/etc/os-release") or ""
        m = re.search(r'^PRETTY_NAME="?([^"\n]*)"?', osr, re.M)
        return {
            "version": VERSION,
            "hostname": socket.gethostname(),
            "model": read_text("/proc/device-tree/model") or "",
            "os": m.group(1) if m else "",
            "kernel": os.uname().release,
            "arch": os.uname().machine,
        }

    def features(self):
        try:
            with open("/opt/homeos/edition", encoding="utf-8") as f:
                edition = f.read().strip() or "full"
        except OSError:
            edition = "full"
        return {
            "edition": edition,
            "wifi": bool(shutil.which("nmcli")),
            "volume": bool(shutil.which("wpctl")),
            "brightness": self._backlight() is not None,
            "keyboard": bool(shutil.which("squeekboard") and shutil.which("busctl")),
        }

    @staticmethod
    def ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
            return s.getsockname()[0]
        except OSError:
            return None
        finally:
            s.close()

    @staticmethod
    def temp():
        raw = read_text("/sys/class/thermal/thermal_zone0/temp")
        try:
            return round(int(raw) / 1000, 1)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def memory():
        raw = read_text("/proc/meminfo") or ""
        vals = dict(re.findall(r"^(\w+):\s+(\d+)", raw, re.M))
        try:
            total = int(vals["MemTotal"]) * 1024
            avail = int(vals["MemAvailable"]) * 1024
            return {"total": total, "used": total - avail}
        except (KeyError, ValueError):
            return None

    @staticmethod
    def disk():
        try:
            d = shutil.disk_usage("/")
            return {"total": d.total, "used": d.used}
        except OSError:
            return None

    @staticmethod
    def uptime():
        raw = read_text("/proc/uptime")
        try:
            return int(float(raw.split()[0]))
        except (AttributeError, ValueError, IndexError):
            return None

    def status(self):
        try:
            load = round(os.getloadavg()[0], 2)
        except OSError:
            load = None
        return {
            "ip": self.ip(),
            "temp": self.temp(),
            "memory": self.memory(),
            "disk": self.disk(),
            "uptime": self.uptime(),
            "load": load,
            "cpus": os.cpu_count(),
            "wifi": self.wifi_current(),
            "volume": self.get_volume(),
            "brightness": self.get_brightness(),
            "battery": apps.devices.battery(),
            "bluetooth": apps.devices.powered(),
        }

    # ---- Wi-Fi (NetworkManager)
    def wifi_current(self):
        now = time.time()
        if now - self._wifi_cache[0] < 10:
            return self._wifi_cache[1]
        cur = None
        rc, out, _ = run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi",
                          "list", "--rescan", "no"])
        if rc == 0:
            for line in out.splitlines():
                p = split_nmcli(line)
                if len(p) >= 3 and p[0] == "yes":
                    cur = {"ssid": p[1], "signal": int(p[2]) if p[2].isdigit() else 0}
                    break
        self._wifi_cache = (now, cur)
        return cur

    def wifi_list(self):
        rc, out, err = run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device",
                            "wifi", "list", "--rescan", "auto"], timeout=20)
        if rc != 0:
            return {"ok": False, "error": err or "nmcli failed", "networks": []}
        best = {}
        for line in out.splitlines():
            p = split_nmcli(line)
            if len(p) < 4 or not p[1]:
                continue
            net = {
                "ssid": p[1],
                "signal": int(p[2]) if p[2].isdigit() else 0,
                "secure": p[3] not in ("", "--"),
                "active": p[0].strip() == "*",
            }
            old = best.get(net["ssid"])
            if old is None or net["active"] or (net["signal"] > old["signal"] and not old["active"]):
                best[net["ssid"]] = net
        nets = sorted(best.values(), key=lambda n: (not n["active"], -n["signal"]))
        return {"ok": True, "networks": nets}

    def wifi_connect(self, ssid, password):
        if not ssid or len(ssid) > 64 or len(password or "") > 128:
            return {"ok": False, "error": "bad parameters"}
        cmd = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        rc, out, err = run_priv(cmd, timeout=45)
        self._wifi_cache = (0.0, None)
        return {"ok": rc == 0, "error": "" if rc == 0 else (err or out or "failed")}

    # ---- sound (PipeWire / WirePlumber)
    @staticmethod
    def get_volume():
        rc, out, _ = run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], timeout=3)
        m = re.search(r"(\d+(?:\.\d+)?)", out) if rc == 0 else None
        if not m:
            return None
        return {"value": min(100, round(float(m.group(1)) * 100)), "muted": "MUTED" in out}

    @staticmethod
    def set_volume(value):
        v = max(0, min(100, int(value)))
        run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"], timeout=3)
        rc, _, err = run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "%.2f" % (v / 100)], timeout=3)
        return {"ok": rc == 0, "error": err if rc else ""}

    # ---- display backlight
    @staticmethod
    def _backlight():
        for d in sorted(glob.glob("/sys/class/backlight/*")):
            if os.path.exists(os.path.join(d, "brightness")):
                return d
        return None

    def get_brightness(self):
        d = self._backlight()
        if not d:
            return None
        try:
            cur = int(read_text(os.path.join(d, "brightness")))
            mx = int(read_text(os.path.join(d, "max_brightness")))
            return round(cur * 100 / mx) if mx else None
        except (TypeError, ValueError):
            return None

    def set_brightness(self, value):
        d = self._backlight()
        if not d:
            return {"ok": False, "error": "no backlight"}
        try:
            mx = int(read_text(os.path.join(d, "max_brightness")))
        except (TypeError, ValueError):
            return {"ok": False, "error": "no backlight"}
        pct = max(5, min(100, int(value)))  # never fully dark: the screen is the only way back
        raw = str(max(1, round(mx * pct / 100)))
        path = os.path.join(d, "brightness")
        try:
            with open(path, "w") as f:
                f.write(raw)
            return {"ok": True, "error": ""}
        except OSError:
            if shutil.which("sudo"):
                try:
                    p = subprocess.run(["sudo", "-n", "tee", path], input=raw, text=True,
                                       capture_output=True, timeout=5)
                    if p.returncode == 0:
                        return {"ok": True, "error": ""}
                except (OSError, subprocess.TimeoutExpired):
                    pass
            return {"ok": False, "error": "permission denied"}

    # ---- on-screen keyboard (squeekboard over D-Bus)
    @staticmethod
    def toggle_keyboard(show=None):
        base = ["busctl", "--user"]
        if show is None:
            rc, out, _ = run(base + ["get-property", "sm.puri.OSK0", "/sm/puri/OSK0",
                                     "sm.puri.OSK0", "Visible"], timeout=3)
            show = not (rc == 0 and out.endswith("true"))
        rc, _, err = run(base + ["call", "sm.puri.OSK0", "/sm/puri/OSK0", "sm.puri.OSK0",
                                 "SetVisible", "b", "true" if show else "false"], timeout=3)
        return {"ok": rc == 0, "visible": bool(show) if rc == 0 else False, "error": err if rc else ""}

    # ---- power
    def power(self, action):
        if action not in ("reboot", "poweroff"):
            return {"ok": False, "error": "bad action"}
        if self.dev:
            print("[dev] power action skipped:", action, flush=True)
            return {"ok": True, "error": "", "dev": True}
        # answer the UI first, then act
        threading.Timer(0.8, lambda: run_priv(["systemctl", action], timeout=20)).start()
        return {"ok": True, "error": ""}


# --------------------------------------------------------------- host hooks

class DevHost:
    """Stand-in for the GTK window when the API runs on its own."""

    def apply_theme(self, theme):
        print("[dev] theme:", theme, flush=True)

    def open_browser(self, url):
        print("[dev] open browser:", url or "(new tab)", flush=True)

    def open_terminal(self):
        print("[dev] open terminal", flush=True)

    def quit_session(self):
        print("[dev] exit to console", flush=True)


# --------------------------------------------------------------- HTTP

class Server:
    def __init__(self, ui_dir, config_dir, host=None, dev=False, port=0):
        self.ui_dir = os.path.realpath(ui_dir)
        self.token = secrets.token_urlsafe(24)
        self.settings = Settings(config_dir)
        apps.start(config_dir)
        self.system = System(dev=dev)
        self.host = host or DevHost()
        self.theme = self.settings.get()["theme"]
        self.dev = dev
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d/" % self.port

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()

    def sync_theme(self):
        """Tells the window when the theme changed (here or from web access)."""
        theme = self.settings.get()["theme"]
        if theme != self.theme:
            self.theme = theme
            self.host.apply_theme(theme)
        return theme

    # ---- API routes
    def api_get(self, path):
        if path == "/api/boot":
            return {"info": self.system.info(), "features": self.system.features(),
                    "settings": self.settings.get(), "engines": sorted(SEARCH_ENGINES),
                    "backgrounds": list(BACKGROUNDS),
                    "dev": self.dev}
        if path == "/api/status":
            status = self.system.status()
            status["theme"] = self.sync_theme()
            return status
        if path == "/api/wifi":
            return self.system.wifi_list()
        return apps.api_get(path, self.settings)

    def api_post(self, path, body):
        res = apps.api_post(path, body, self.settings)
        if res is not None:
            return res
        s = self.system
        if path == "/api/settings":
            saved = self.settings.update(body)
            self.sync_theme()
            return {"ok": True, "settings": saved}
        if path == "/api/open":
            target = body.get("target")
            if target == "terminal":
                self.host.open_terminal()
                return {"ok": True}
            if target == "browser":
                cfg = self.settings.get()
                text = str(body.get("url") or "")[:2000]
                url = normalize_url(text, cfg["search"]) or (cfg["homepage"] or None)
                self.host.open_browser(url)
                return {"ok": True, "url": url}
            return {"ok": False, "error": "bad target"}
        if path == "/api/wifi/connect":
            return s.wifi_connect(str(body.get("ssid", "")), str(body.get("password", "")))
        if path == "/api/volume":
            return s.set_volume(body.get("value", 0))
        if path == "/api/brightness":
            return s.set_brightness(body.get("value", 100))
        if path == "/api/keyboard":
            return s.toggle_keyboard(body.get("show"))
        if path == "/api/power":
            action = body.get("action")
            if action == "exit":
                self.host.quit_session()
                return {"ok": True}
            return s.power(action)
        return None

    # ---- request handler
    def _handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "HomeOS"

            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json; charset=utf-8"):
                if not isinstance(body, bytes):
                    body = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _host_ok(self):
                # Blocks DNS-rebinding: only the literal loopback address is accepted.
                return self.headers.get("Host", "") == "127.0.0.1:%d" % srv.port

            def _token_ok(self):
                got = self.headers.get("X-HomeOS-Token", "")
                return hmac.compare_digest(got.encode(), srv.token.encode())

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, {"error": "forbidden"})
                url = urlparse(self.path)
                path = url.path
                if path in apps.SERVE_PATHS:
                    # <img> and <audio> cannot send a header, so the token comes in the address
                    got = parse_qs(url.query).get("t", [""])[0]
                    if not hmac.compare_digest(got.encode(), srv.token.encode()):
                        return self._send(403, {"error": "forbidden"})
                    return apps.serve(self, path, url.query, srv.settings)
                if path.startswith("/api/"):
                    if not self._token_ok():
                        return self._send(403, {"error": "forbidden"})
                    try:
                        res = srv.api_get(path)
                    except Exception as e:  # keep the shell alive whatever happens
                        return self._send(500, {"error": str(e)})
                    return self._send(200, res) if res is not None else self._send(404, {"error": "not found"})
                rel = "index.html" if path == "/" else path.lstrip("/")
                full = os.path.realpath(os.path.join(srv.ui_dir, rel))
                if not full.startswith(srv.ui_dir + os.sep) or not os.path.isfile(full):
                    return self._send(404, {"error": "not found"})
                with open(full, "rb") as f:
                    data = f.read()
                if rel == "index.html":
                    data = data.replace(b"__HOMEOS_TOKEN__", srv.token.encode())
                    data = data.replace(b"__HOMEOS_MODE__", b"local")
                    data = data.replace(b"__HOMEOS_THEME__", srv.settings.get()["theme"].encode())
                ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
                self._send(200, data, ctype)

            def do_POST(self):
                if not self._host_ok() or not self._token_ok():
                    return self._send(403, {"error": "forbidden"})
                url = urlparse(self.path)
                if url.path == "/api/files/upload":          # the body is the file, not JSON
                    return apps.files.receive(self, url.query)
                if url.path == "/api/radio/upload":
                    return apps.radio_upload(self, url.query, srv.settings)
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = -1
                if n < 0 or n > MAX_BODY:
                    return self._send(413, {"error": "too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError
                except ValueError:
                    return self._send(400, {"error": "bad json"})
                try:
                    res = srv.api_post(url.path, body)
                except Exception as e:
                    return self._send(500, {"error": str(e)})
                return self._send(200, res) if res is not None else self._send(404, {"error": "not found"})

        return Handler


def default_config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "homeos")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HomeOS shell API (standalone preview)")
    ap.add_argument("--port", type=int, default=8137)
    ap.add_argument("--config", default=default_config_dir())
    args = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    server = Server(os.path.join(here, "ui"), args.config, dev=True, port=args.port)
    print("HomeOS UI:", server.url, flush=True)
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
