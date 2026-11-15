"""Remote browser for the web version of HomeOS.

Most websites refuse to be shown inside another page, so a frame is not
enough. This runs a headless Chromium on the Pi and talks to it over the
Chrome DevTools Protocol: the page is sent to the viewer as a stream of JPEG
frames (MJPEG), and the viewer's taps, scrolling and typing are sent back as
input events. Every site works, because a real browser on the Pi loads it.

Only the Python standard library is used, including a small WebSocket client.
Chromium starts when the browser is first opened and stops after ten minutes
with nobody watching.
"""

import base64
import hashlib
import json
import os
import queue
import random
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlparse

PORT = 9229
IDLE_STOP = 600                 # seconds without a viewer before Chromium is stopped
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/131.0.0.0 Mobile Safari/537.36")
KEYS = {        # key name -> (code, Windows virtual key code)
    "Enter": ("Enter", 13), "Backspace": ("Backspace", 8), "Tab": ("Tab", 9), "Escape": ("Escape", 27),
    "Delete": ("Delete", 46), "ArrowLeft": ("ArrowLeft", 37), "ArrowUp": ("ArrowUp", 38),
    "ArrowRight": ("ArrowRight", 39), "ArrowDown": ("ArrowDown", 40), "Home": ("Home", 36),
    "End": ("End", 35), "PageUp": ("PageUp", 33), "PageDown": ("PageDown", 34),
}


class Unavailable(Exception):
    pass


def find_chromium():
    for name in (os.environ.get("HOMEOS_CHROMIUM"), "chromium", "chromium-browser", "google-chrome"):
        if name:
            path = name if os.path.isabs(name) else shutil.which(name)
            if path and os.access(path, os.X_OK):
                return path
    return None


# --------------------------------------------------------------- WebSocket (RFC 6455), client side only

class WebSocket:
    def __init__(self, url):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n") % (u.path or "/", u.hostname, u.port or 80, key)
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("WebSocket handshake failed")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if b" 101 " not in head.split(b"\r\n")[0] or accept.encode() not in head:
            raise OSError("WebSocket handshake refused")
        self.sock.settimeout(None)
        self.send_lock = threading.Lock()

    def send(self, text):
        data = text.encode("utf-8")
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            head = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.send_lock:
            self.sock.sendall(head + mask + masked)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise OSError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        """Returns one whole text message, or None when the connection closes."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, op, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x8:
                return None
            if op == 0x9:                       # ping
                with self.send_lock:
                    self.sock.sendall(struct.pack("!BB", 0x8A, 0x80) + os.urandom(4))
                continue
            if op in (0x0, 0x1, 0x2):
                parts.append(data)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------- the browser

class RemoteBrowser:
    def __init__(self, profile_dir):
        self.profile = profile_dir
        self.lock = threading.RLock()
        self.proc = None
        self.ws = None
        self.next_id = 0
        self.id_lock = threading.Lock()
        self.waiting = {}
        self.frame = None               # latest JPEG
        self.frame_no = 0
        self.frame_cond = threading.Condition()
        self.viewers = 0
        self.last_seen = time.time()
        self.size = (390, 700, 2.0, True)      # width, height, scale, mobile
        self.info = {"url": "", "title": "", "loading": False, "back": False, "forward": False}
        threading.Thread(target=self._idle_watch, daemon=True, name="rb-idle").start()

    # ---- process and connection
    def available(self):
        return find_chromium() is not None

    def _start(self):
        exe = find_chromium()
        if not exe:
            raise Unavailable("Chromium is not installed on this Pi. Install it from the console:  sudo apt install chromium")
        os.makedirs(self.profile, exist_ok=True)
        w, h, _, _ = self.size
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=%d" % PORT,
             "--user-data-dir=" + self.profile, "--no-first-run", "--no-default-browser-check",
             "--disable-gpu", "--hide-scrollbars", "--window-size=%d,%d" % (w, h)]
            + (["--no-sandbox"] if os.geteuid() == 0 else [])      # Chromium refuses to run as root otherwise
            + ["about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + 20
        target = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise Unavailable("Chromium stopped right after starting.")
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % PORT, timeout=2) as r:
                    pages = [t for t in json.loads(r.read()) if t.get("type") == "page"]
                if pages:
                    target = pages[0]["webSocketDebuggerUrl"]
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.3)
        if not target:
            self._stop()
            raise Unavailable("Chromium did not start in time.")
        self.ws = WebSocket(target)
        threading.Thread(target=self._reader, args=(self.ws,), daemon=True, name="rb-reader").start()
        for method in ("Page.enable", "Network.enable"):
            self._call(method)
        self._apply_size()
        self._call("Page.startScreencast", {"format": "jpeg", "quality": 70, "everyNthFrame": 1})

    def _stop(self):
        if self.ws:
            self.ws.close()
            self.ws = None
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
            self.proc = None
        self.frame = None

    def _ensure(self):
        with self.lock:
            if self.proc and self.proc.poll() is None and self.ws:
                return
            self._stop()
            self._start()

    def _idle_watch(self):
        while True:
            time.sleep(30)
            with self.lock:
                if self.proc and self.viewers == 0 and time.time() - self.last_seen > IDLE_STOP:
                    self._stop()

    # ---- protocol
    def _reader(self, ws):
        try:
            while True:
                text = ws.recv()
                if text is None:
                    break
                msg = json.loads(text)
                if "id" in msg:
                    q = self.waiting.pop(msg["id"], None)
                    if q:
                        q.put(msg)
                    continue
                self._event(msg.get("method"), msg.get("params") or {})
        except (OSError, ValueError):
            pass
        for q in list(self.waiting.values()):
            q.put({"error": {"message": "closed"}})
        self.waiting.clear()

    def _call(self, method, params=None, wait=True):
        if not self.ws:
            raise Unavailable("The browser is not running.")
        with self.id_lock:
            self.next_id += 1
            mid = self.next_id
        q = queue.Queue(maxsize=1) if wait else None
        if wait:
            self.waiting[mid] = q
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        if not wait:
            return None
        try:
            msg = q.get(timeout=15)
        except queue.Empty:
            self.waiting.pop(mid, None)
            raise Unavailable("The browser did not answer.")
        if "error" in msg:
            raise Unavailable(msg["error"].get("message", "browser error"))
        return msg.get("result") or {}

    def _event(self, method, p):
        if method == "Page.screencastFrame":
            try:
                self._call("Page.screencastFrameAck", {"sessionId": p["sessionId"]}, wait=False)
            except (Unavailable, OSError):
                pass
            with self.frame_cond:
                self.frame = base64.b64decode(p["data"])
                self.frame_no += 1
                self.frame_cond.notify_all()
        elif method == "Page.frameNavigated" and not p.get("frame", {}).get("parentId"):
            self.info["url"] = p["frame"].get("url", "")
            threading.Thread(target=self._refresh_info, daemon=True).start()
        elif method == "Page.frameStartedLoading":
            self.info["loading"] = True
        elif method in ("Page.loadEventFired", "Page.frameStoppedLoading"):
            self.info["loading"] = False
            threading.Thread(target=self._refresh_info, daemon=True).start()

    def _refresh_info(self):
        try:
            h = self._call("Page.getNavigationHistory")
            i, entries = h["currentIndex"], h["entries"]
            self.info.update(url=entries[i]["url"], title=entries[i].get("title", ""),
                             back=i > 0 and entries[i - 1]["url"] != "about:blank", forward=i < len(entries) - 1)
        except (Unavailable, OSError, KeyError, IndexError):
            pass

    def _apply_size(self):
        w, h, scale, mobile = self.size
        self._call("Emulation.setDeviceMetricsOverride",
                   {"width": w, "height": h, "deviceScaleFactor": scale, "mobile": mobile})
        self._call("Emulation.setTouchEmulationEnabled", {"enabled": mobile, "maxTouchPoints": 5 if mobile else 1})
        self._call("Network.setUserAgentOverride", {"userAgent": MOBILE_UA if mobile else ""})

    # ---- what the HomeOS pages call
    def open(self, url, width, height, scale):
        width = max(240, min(2560, int(width or 390)))
        height = max(240, min(2560, int(height or 700)))
        scale = max(1.0, min(2.0, float(scale or 1)))
        mobile = width < 700
        with self.lock:
            self._ensure()
            if (width, height, scale, mobile) != self.size:
                self.size = (width, height, scale, mobile)
                self._apply_size()
            if url:
                self._call("Page.navigate", {"url": url})
            self.last_seen = time.time()
        return self.state()

    def go(self, action):
        with self.lock:
            self._ensure()
            if action == "reload":
                self._call("Page.reload")
            elif action in ("back", "forward"):
                h = self._call("Page.getNavigationHistory")
                i = h["currentIndex"] + (-1 if action == "back" else 1)
                if 0 <= i < len(h["entries"]):
                    self._call("Page.navigateToHistoryEntry", {"entryId": h["entries"][i]["id"]})
        return self.state()

    def tap(self, x, y):
        w, h, _, mobile = self.size
        px, py = max(0.0, min(1.0, float(x))) * w, max(0.0, min(1.0, float(y))) * h
        with self.lock:
            self._ensure()
            if mobile:
                pt = [{"x": px, "y": py}]
                self._call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": pt})
                self._call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            else:
                for kind in ("mousePressed", "mouseReleased"):
                    self._call("Input.dispatchMouseEvent", {"type": kind, "x": px, "y": py, "button": "left", "clickCount": 1})

    def scroll(self, x, y, dx, dy):
        w, h, _, _ = self.size
        px, py = max(0.0, min(1.0, float(x))) * w, max(0.0, min(1.0, float(y))) * h
        with self.lock:
            self._ensure()
            self._call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": px, "y": py,
                                                    "deltaX": float(dx) * w, "deltaY": float(dy) * h})

    def type_text(self, text):
        text = str(text or "")[:2000]
        if text:
            with self.lock:
                self._ensure()
                self._call("Input.insertText", {"text": text})

    def key(self, name):
        if name not in KEYS:
            return
        code, vk = KEYS[name]
        with self.lock:
            self._ensure()
            for kind in ("rawKeyDown", "keyUp"):
                ev = {"type": kind, "key": name, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
                if kind == "rawKeyDown" and name == "Enter":
                    ev["text"] = "\r"
                self._call("Input.dispatchKeyEvent", ev)

    def state(self):
        return {"ok": True, "running": bool(self.proc and self.proc.poll() is None), **self.info}

    def stream(self, handler):
        """Answers a GET with the page as MJPEG, until the viewer leaves."""
        try:
            self._ensure()
        except Unavailable as e:
            body = json.dumps({"ok": False, "error": str(e)}).encode()
            handler.send_response(503)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        boundary = "homeosframe%d" % random.randint(1000, 9999)
        handler.send_response(200)
        handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + boundary)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        with self.lock:
            self.viewers += 1
        seen = -1
        try:
            while True:
                with self.frame_cond:
                    if self.frame_no == seen:
                        self.frame_cond.wait(timeout=20)
                    frame, seen = self.frame, self.frame_no
                if frame is None:
                    if not (self.proc and self.proc.poll() is None):
                        break
                    continue
                handler.wfile.write(("--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                     % (boundary, len(frame))).encode() + frame + b"\r\n")
                handler.wfile.flush()
        except OSError:
            pass                # the viewer left
        finally:
            with self.lock:
                self.viewers -= 1
                self.last_seen = time.time()


_browser = None


def get(config_dir):
    global _browser
    if _browser is None:
        _browser = RemoteBrowser(os.path.join(config_dir, "browser-profile"))
    return _browser


def framable(url):
    """Asks the site whether it may be shown inside another page (a frame)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
        with urllib.request.urlopen(req, timeout=6) as r:
            xfo = (r.headers.get("X-Frame-Options") or "").strip().lower()
            csp = " ".join(r.headers.get_all("Content-Security-Policy") or []).lower()
    except Exception:
        return False                    # unsure: the remote browser always works
    if xfo in ("deny", "sameorigin") or xfo.startswith("allow-from"):
        return False
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith("frame-ancestors"):
            return "*" in part.split()[1:]
    return True
