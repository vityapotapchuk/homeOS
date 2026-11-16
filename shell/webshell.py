#!/usr/bin/env python3
"""HomeOS web access - the home screen, settings and a terminal in a browser.

Meant to be reached over Tailscale:  http://<tailscale-address>:8790

Safety model
  * listens only on 127.0.0.1 and this device's Tailscale address (never on the
    LAN or the internet unless you change "bind" in the config);
  * nothing works without a password (set with:  homeos-web --set-password);
  * sessions are HttpOnly, SameSite=Strict cookies; every change needs the
    per-session token as well (CSRF protection);
  * the Host header must be an IP address or a known name (DNS-rebinding
    protection);
  * the terminal is a real shell of the user that runs this service. Anyone
    who knows the password and is in your tailnet gets that shell.

Standard library only.
"""

import argparse
import base64
import fcntl
import getpass
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import pty
import secrets
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sysapi  # noqa: E402
import apps  # noqa: E402

DEFAULT_PORT = 8790
COOKIE = "homeos_session"
SESSION_TTL = 12 * 3600
LOGIN_MAX_FAILS = 8
LOGIN_WINDOW = 300.0
MAX_TERMS = 4
TERM_BUFFER = 512 * 1024
MAX_BODY = 64 * 1024

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "frame-src http: https:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
}


# --------------------------------------------------------------- config

class WebConfig:
    def __init__(self, config_dir):
        self.path = os.path.join(config_dir, "web.json")
        self.data = {"port": DEFAULT_PORT, "bind": "tailscale", "hosts": [], "terminal": True, "password": None}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self.data.update(raw)
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return {"algo": "scrypt", "n": 16384, "r": 8, "p": 1, "salt": salt.hex(), "hash": digest.hex()}


def check_password(password, rec):
    try:
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(rec["salt"]),
                                n=int(rec["n"]), r=int(rec["r"]), p=int(rec["p"]), dklen=32)
        return hmac.compare_digest(digest, bytes.fromhex(rec["hash"]))
    except (KeyError, ValueError, TypeError):
        return False


# --------------------------------------------------------------- network

def tailscale_info():
    """Returns (ipv4 addresses, DNS names) of this device in the tailnet."""
    ips, names = [], []
    rc, out, _ = sysapi.run(["tailscale", "ip", "-4"], timeout=4)
    if rc == 0:
        ips = [x.strip() for x in out.splitlines() if x.strip()]
    else:
        rc, out, _ = sysapi.run(["ip", "-4", "-o", "addr", "show", "dev", "tailscale0"], timeout=4)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if "inet" in parts:
                    ips.append(parts[parts.index("inet") + 1].split("/")[0])
    rc, out, _ = sysapi.run(["tailscale", "status", "--json"], timeout=5)
    if rc == 0:
        try:
            me = json.loads(out).get("Self", {})
            if me.get("DNSName"):
                names.append(me["DNSName"].rstrip(".").lower())
                names.append(me["DNSName"].split(".")[0].lower())
            if me.get("HostName"):
                names.append(me["HostName"].lower())
        except ValueError:
            pass
    return ips, names


def is_ip(text):
    try:
        ipaddress.ip_address(text.strip("[]"))
        return True
    except ValueError:
        return False


# --------------------------------------------------------------- terminals

class Term:
    """One shell on a pseudo-terminal, with a replay buffer for reconnects."""

    def __init__(self, owner, cols, rows):
        self.id = secrets.token_urlsafe(9)
        self.owner = owner
        self.cond = threading.Condition()
        self.buf = bytearray()
        self.base = 0          # stream offset of buf[0]
        self.closed = False
        master, slave = pty.openpty()
        self.fd = master
        self.resize(cols, rows)
        shell = os.environ.get("SHELL") or "/bin/bash"
        if not os.path.exists(shell):
            shell = "/bin/sh"
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", HOMEOS_WEB="1")
        # setsid -c gives the shell its own session with the pty as controlling terminal
        self.proc = subprocess.Popen(["setsid", "-c", shell, "-l"], stdin=slave, stdout=slave, stderr=slave,
                                     cwd=os.path.expanduser("~"), env=env, close_fds=True)
        os.close(slave)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            with self.cond:
                if not data:
                    self.closed = True
                    self.cond.notify_all()
                    break
                self.buf += data
                extra = len(self.buf) - TERM_BUFFER
                if extra > 0:
                    del self.buf[:extra]
                    self.base += extra
                self.cond.notify_all()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def read_from(self, pos, timeout):
        """Returns (data, new position, closed). Blocks up to `timeout` seconds."""
        with self.cond:
            if pos < self.base:
                pos = self.base
            end = self.base + len(self.buf)
            if pos >= end and not self.closed:
                self.cond.wait(timeout)
                end = self.base + len(self.buf)
                if pos < self.base:
                    pos = self.base
            data = bytes(self.buf[pos - self.base:end - self.base]) if pos < end else b""
            return data, max(pos, end), self.closed

    def write(self, data):
        if not self.closed:
            try:
                os.write(self.fd, data)
            except OSError:
                pass

    def resize(self, cols, rows):
        cols = max(10, min(500, int(cols)))
        rows = max(4, min(200, int(rows)))
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def kill(self):
        try:
            os.killpg(self.proc.pid, signal.SIGHUP)
        except (OSError, ProcessLookupError):
            pass
        try:
            self.proc.terminate()
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        with self.cond:
            self.closed = True
            self.cond.notify_all()


class Terms:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}

    def open(self, owner, cols, rows):
        with self.lock:
            for tid in [t for t, term in self.items.items() if term.closed]:
                del self.items[tid]
            if len(self.items) >= MAX_TERMS:
                return None
            term = Term(owner, cols, rows)
            self.items[term.id] = term
            return term

    def get(self, tid, owner):
        with self.lock:
            term = self.items.get(tid)
        return term if term is not None and term.owner == owner else None

    def close(self, tid, owner):
        term = self.get(tid, owner)
        if term:
            term.kill()
            with self.lock:
                self.items.pop(tid, None)

    def close_owner(self, owner):
        with self.lock:
            mine = [t for t in self.items.values() if t.owner == owner]
        for term in mine:
            self.close(term.id, owner)

    def close_all(self):
        with self.lock:
            everything = list(self.items.values())
            self.items.clear()
        for term in everything:
            term.kill()


# --------------------------------------------------------------- server

class WebShell:
    def __init__(self, ui_dir, config_dir, bind=None, port=None):
        self.ui_dir = os.path.realpath(ui_dir)
        self.cfg = WebConfig(config_dir)
        self.settings = sysapi.Settings(config_dir)
        apps.start(config_dir)
        self.system = sysapi.System()
        self.port = int(port or self.cfg.data.get("port") or DEFAULT_PORT)
        self.bind_mode = bind or self.cfg.data.get("bind") or "tailscale"
        self.sessions = {}            # token -> {"exp": float, "csrf": str}
        self.lock = threading.Lock()
        self.fails = []
        self.terms = Terms()
        self.servers = {}             # address -> ThreadingHTTPServer
        self.names = {"localhost", socket.gethostname().lower()}
        self.names.update(str(h).lower() for h in self.cfg.data.get("hosts", []))

    # ---- listening
    def wanted_addresses(self):
        if self.bind_mode == "all":
            return ["0.0.0.0"]
        if isinstance(self.bind_mode, list):
            return ["127.0.0.1"] + [a for a in self.bind_mode if is_ip(a)]
        ips, names = tailscale_info()
        self.names.update(names)
        return ["127.0.0.1"] + ips

    def listen(self):
        """Opens listeners that are not open yet. Returns the open addresses."""
        for addr in self.wanted_addresses():
            if addr in self.servers:
                continue
            try:
                httpd = QuietServer((addr, self.port), make_handler(self))
            except OSError as e:
                print("cannot listen on %s:%d: %s" % (addr, self.port, e), flush=True)
                continue
            self.servers[addr] = httpd
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            print("listening on http://%s:%d/" % (addr, self.port), flush=True)
        return list(self.servers)

    def serve(self):
        if not self.listen():
            raise SystemExit("Could not open port %d on any address." % self.port)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.wait(30):
            self.listen()            # picks up Tailscale when it comes online later
            self.sweep()
        self.terms.close_all()

    # ---- sessions
    def sweep(self):
        now = time.time()
        with self.lock:
            dead = [t for t, s in self.sessions.items() if s["exp"] < now]
            for t in dead:
                del self.sessions[t]
        for t in dead:
            self.terms.close_owner(t)

    def _auth_key(self):
        """Names the sign-in in force: the password record, or "open" while no password
        is asked. web.json is read again when it changes (homeos-web --set-password,
        Settings > Sign-in), so a new password ends every session made with the old one."""
        try:
            mt = os.stat(self.cfg.path).st_mtime_ns
        except OSError:
            mt = None
        if mt != getattr(self, "_cfg_mtime", None):
            fresh = WebConfig(os.path.dirname(self.cfg.path)).data
            self.cfg.data["password"] = fresh.get("password")
            self.cfg.data["no_password"] = bool(fresh.get("no_password"))
            self._cfg_mtime = mt
        if self.cfg.data.get("no_password"):
            return "open"
        rec = self.cfg.data.get("password") or {}
        return hashlib.sha256(json.dumps(rec, sort_keys=True).encode("utf-8")).hexdigest()

    def session(self, token):
        if not token:
            return None
        key = self._auth_key()
        with self.lock:
            s = self.sessions.get(token)
            if not s:
                return None
            # a session made under an earlier password, or while no password was asked,
            # ends when a password is (newly) required
            if s["exp"] >= time.time() and (key == "open" or s.get("key") == key):
                return s
            del self.sessions[token]
        self.terms.close_owner(token)
        return None

    def login(self, password):
        """Returns (http status, session token or None)."""
        self._auth_key()                       # picks up --set-password and Settings > Sign-in
        rec = self.cfg.data.get("password")
        if self.cfg.data["no_password"]:      # switched off in Settings (for testing): anyone who reaches the Pi gets in
            return 200, self._new_session()
        if not rec:
            return 403, None
        now = time.monotonic()
        with self.lock:
            self.fails = [t for t in self.fails if now - t < LOGIN_WINDOW]
            if len(self.fails) >= LOGIN_MAX_FAILS:
                return 429, None
        if not check_password(password, rec):
            with self.lock:
                self.fails.append(now)
            time.sleep(0.6)
            return 401, None
        return 200, self._new_session()

    def _new_session(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[token] = {"exp": time.time() + SESSION_TTL, "csrf": secrets.token_urlsafe(24),
                                    "key": self._auth_key()}
        return token

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)
        self.terms.close_owner(token)

    def host_allowed(self, host_header):
        host = (host_header or "").strip().lower()
        if host.startswith("["):
            name = host.split("]")[0] + "]"
        else:
            name = host.rsplit(":", 1)[0] if ":" in host else host
        return bool(name) and (is_ip(name) or name in self.names)

    # ---- API
    def api_get(self, path):
        if path == "/api/boot":
            feats = self.system.features()
            feats["keyboard"] = False
            return {"info": self.system.info(), "features": feats, "settings": self.settings.get(),
                    "engines": sorted(sysapi.SEARCH_ENGINES),
                    "backgrounds": list(sysapi.BACKGROUNDS), "dev": False, "mode": "web",
                    "terminal": bool(self.cfg.data.get("terminal", True))}
        if path == "/api/web/password":
            return {"ok": True, "ask": not self.cfg.data.get("no_password"), "has_password": bool(self.cfg.data.get("password"))}
        if path == "/api/status":
            status = self.system.status()
            status["theme"] = self.settings.get()["theme"]
            return status
        if path == "/api/wifi":
            return self.system.wifi_list()
        return apps.api_get(path, self.settings)

    def api_post(self, path, body, token):
        s = self.system
        if path == "/api/settings":
            return {"ok": True, "settings": self.settings.update(body)}
        if path == "/api/web/password":
            ask = bool(body.get("ask"))
            if ask and not self.cfg.data.get("password"):
                return {"ok": False, "error": "No password is set. On the Pi run: homeos-web --set-password"}
            self.cfg.data["no_password"] = not ask
            self.cfg.save()
            return {"ok": True, "ask": ask}
        if path == "/api/wifi/connect":
            return s.wifi_connect(str(body.get("ssid", "")), str(body.get("password", "")))
        if path == "/api/volume":
            return s.set_volume(body.get("value", 0))
        if path == "/api/brightness":
            return s.set_brightness(body.get("value", 100))
        if path == "/api/power":
            return s.power(body.get("action"))
        res = apps.api_post(path, body, self.settings)
        if res is not None:
            return res
        if path.startswith("/api/term/"):
            if not self.cfg.data.get("terminal", True):
                return {"ok": False, "error": "The web terminal is turned off."}
            if path == "/api/term/open":
                term = self.terms.open(token, body.get("cols", 80), body.get("rows", 24))
                if term is None:
                    return {"ok": False, "error": "Too many open terminals. Close one first."}
                return {"ok": True, "id": term.id}
            term = self.terms.get(str(body.get("id", "")), token)
            if term is None:
                return {"ok": False, "error": "gone"}
            if path == "/api/term/input":
                term.write(str(body.get("data", "")).encode("utf-8"))
                return {"ok": True}
            if path == "/api/term/resize":
                term.resize(body.get("cols", 80), body.get("rows", 24))
                return {"ok": True}
            if path == "/api/term/close":
                self.terms.close(term.id, token)
                return {"ok": True}
        return None


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # a browser that closed the tab mid-response is not worth a traceback
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "HomeOS"
        sys_version = ""

        def log_message(self, *a):
            pass

        # ---- plumbing
        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, where):
            self._send(303, b"", "text/plain", {"Location": where})

        def _token(self):
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return None

        def _file(self, rel, replace=None):
            full = os.path.realpath(os.path.join(app.ui_dir, rel))
            if not full.startswith(app.ui_dir + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                data = f.read()
            if rel.endswith(".html"):
                data = data.replace(b"__HOMEOS_THEME__", app.settings.get()["theme"].encode())
            for k, v in (replace or {}).items():
                data = data.replace(k.encode(), v.encode())
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, data, ctype)

        def _guard(self):
            if not app.host_allowed(self.headers.get("Host")):
                self._send(403, b"This address is not allowed. Open the page by its Tailscale IP address, "
                                b"or allow the name with: homeos-web --allow-host NAME\n", "text/plain; charset=utf-8")
                return False
            return True

        # ---- GET
        def do_GET(self):
            if not self._guard():
                return
            url = urlparse(self.path)
            path = url.path
            token = self._token()
            sess = app.session(token)

            if path == "/api/public/weather":      # for the sign-in page; carries no town name
                return self._send(200, apps.weather_public(app.settings))
            if path.startswith("/api/"):
                if not sess:
                    return self._send(401, {"error": "login"})
                if path == "/api/term/stream":
                    return self._stream(parse_qs(url.query).get("id", [""])[0], token)
                if path in apps.SERVE_PATHS:
                    return apps.serve(self, path, url.query, app.settings)
                try:
                    res = app.api_get(path)
                except Exception as e:
                    return self._send(500, {"error": str(e)})
                return self._send(200, res) if res is not None else self._send(404, {"error": "not found"})

            if path in ("/login", "/login.html"):
                if sess:
                    return self._redirect("/")
                fresh = WebConfig(os.path.dirname(app.cfg.path)).data
                state = "open" if fresh.get("no_password") else "ready" if fresh.get("password") else "nopassword"
                return self._file("login.html", {"__HOMEOS_STATE__": state,
                                                 "__HOMEOS_BG__": app.settings.get().get("background", "plain")})
            if path in ("/", "/index.html"):
                if not sess:
                    return self._redirect("/login")
                return self._file("index.html", {"__HOMEOS_TOKEN__": sess["csrf"], "__HOMEOS_MODE__": "web"})
            if path in ("/terminal", "/terminal.html"):
                if not sess:
                    return self._redirect("/login")
                return self._file("terminal.html", {"__HOMEOS_TOKEN__": sess["csrf"],
                                                    "__HOMEOS_HOST__": socket.gethostname()})
            return self._file(path.lstrip("/"))

        def _stream(self, tid, token):
            term = app.terms.get(tid, token)
            if term is None:
                return self._send(404, {"error": "gone"})
            try:
                pos = int(self.headers.get("Last-Event-ID", "0"))
            except ValueError:
                pos = 0
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    data, pos, closed = term.read_from(pos, 15)
                    if data:
                        self.wfile.write(b"id: %d\ndata: %s\n\n" % (pos, base64.b64encode(data)))
                    elif closed:
                        self.wfile.write(b"event: ended\ndata: 0\n\n")
                        self.wfile.flush()
                        break
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    if not app.session(token):
                        break
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        # ---- POST
        def do_POST(self):
            if not self._guard():
                return
            url = urlparse(self.path)
            path = url.path
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower():
                return self._send(403, {"error": "cross-site request refused"})
            if path == "/api/files/upload":          # the body is the file, not JSON: check the session first
                if not app.session(self._token()):
                    self.close_connection = True
                    return self._send(401, {"error": "login"})
                got = self.headers.get("X-HomeOS-Token", "")
                if not hmac.compare_digest(got.encode(), app.session(self._token())["csrf"].encode()):
                    self.close_connection = True
                    return self._send(403, {"error": "forbidden"})
                return apps.files.receive(self, url.query)
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

            if path == "/api/login":
                code, token = app.login(str(body.get("password", "")))
                if code != 200:
                    msg = {401: "Wrong password.", 429: "Too many attempts. Wait five minutes.",
                           403: "No password is set on the Pi yet."}[code]
                    return self._send(code, {"ok": False, "error": msg})
                cookie = "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (COOKIE, token, SESSION_TTL)
                return self._send(200, {"ok": True}, extra={"Set-Cookie": cookie})

            token = self._token()
            sess = app.session(token)
            if not sess:
                return self._send(401, {"error": "login"})
            got = self.headers.get("X-HomeOS-Token", "")
            if not hmac.compare_digest(got.encode(), sess["csrf"].encode()):
                return self._send(403, {"error": "forbidden"})

            if path == "/api/logout":
                app.logout(token)
                gone = "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" % COOKIE
                return self._send(200, {"ok": True}, extra={"Set-Cookie": gone})
            try:
                res = app.api_post(path, body, token)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            return self._send(200, res) if res is not None else self._send(404, {"error": "not found"})

    return Handler


# --------------------------------------------------------------- command line

def main():
    ap = argparse.ArgumentParser(prog="homeos-web", description="HomeOS web access")
    ap.add_argument("--set-password", action="store_true", help="set or change the web password")
    ap.add_argument("--password-file", metavar="FILE", help="set the web password from the first line of FILE")
    ap.add_argument("--set-bind", choices=["tailscale", "all"], help="save where to listen: Tailscale only, or every network")
    ap.add_argument("--allow-host", metavar="NAME", help="also accept this host name in the address bar")
    ap.add_argument("--terminal", choices=["on", "off"], help="turn the web terminal on or off")
    ap.add_argument("--set-port", type=int, metavar="N", help="save the port to listen on")
    ap.add_argument("--no-password", action="store_true", help="let anyone who reaches the Pi in without a password")
    ap.add_argument("--ask-password", action="store_true", help="ask for the password again")
    ap.add_argument("--status", action="store_true", help="show the addresses and whether a password is set")
    ap.add_argument("--port", type=int, help="listen on this port for this run only")
    ap.add_argument("--bind", help="for this run only: tailscale (default), all, or one IP address")
    ap.add_argument("--config", default=sysapi.default_config_dir(), help=argparse.SUPPRESS)
    args = ap.parse_args()

    cfg = WebConfig(args.config)
    changed = False
    if args.no_password or args.ask_password:
        cfg.data["no_password"] = bool(args.no_password)
        changed = True
        print("The password is now", "OFF: anyone who reaches the Pi gets in." if args.no_password else "asked again.")
    if args.set_password:
        first = getpass.getpass("New password for HomeOS web access: ")
        if len(first) < 8:
            raise SystemExit("The password must be at least 8 characters. Nothing was changed.")
        if getpass.getpass("Repeat the password: ") != first:
            raise SystemExit("The passwords do not match. Nothing was changed.")
        cfg.data["password"] = hash_password(first)
        changed = True
        print("Password saved.")
    if args.password_file:
        with open(args.password_file, encoding="utf-8") as f:
            pw = (f.readline() or "").rstrip("\r\n")
        if len(pw) < 8:
            raise SystemExit("The password must be at least 8 characters. Nothing was changed.")
        cfg.data["password"] = hash_password(pw)
        changed = True
    if args.set_bind:
        cfg.data["bind"] = args.set_bind
        changed = True
    if args.allow_host:
        hosts = set(cfg.data.get("hosts", []))
        hosts.add(args.allow_host.strip().lower())
        cfg.data["hosts"] = sorted(hosts)
        changed = True
    if args.terminal:
        cfg.data["terminal"] = args.terminal == "on"
        changed = True
    if args.set_port:
        cfg.data["port"] = args.set_port
        changed = True
    if changed:
        cfg.save()
        if not args.status:
            if args.allow_host or args.terminal or args.set_port or args.set_bind or args.password_file:
                print("Saved. Restart to apply:  sudo systemctl restart homeos-web")
            return
    if args.status:
        ips, names = tailscale_info()
        port = cfg.data.get("port", DEFAULT_PORT)
        print("password set:", "yes" if cfg.data.get("password") else "NO (run: homeos-web --set-password)")
        print("terminal:", "on" if cfg.data.get("terminal", True) else "off")
        for ip in ips:
            print("address: http://%s:%d/" % (ip, port))
        for name in names[:1]:
            print("address: http://%s:%d/" % (name, port))
        if not ips:
            print("Tailscale address not found: is Tailscale running?  (tailscale status)")
        return

    here = os.path.dirname(os.path.abspath(__file__))
    bind = args.bind
    if bind and bind not in ("tailscale", "all"):
        bind = [bind]
    app = WebShell(os.path.join(here, "ui"), args.config, bind=bind, port=args.port)
    if not app.cfg.data.get("password"):
        print("No password set yet: the login page will say so. Run: homeos-web --set-password", flush=True)
    app.serve()


if __name__ == "__main__":
    main()
