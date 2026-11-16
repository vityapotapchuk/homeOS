"""Built-in HomeOS apps: Camera and Radio.

Camera relays the MJPEG stream of a network camera (an ESP32-CAM by default),
so the picture is reachable from the Pi's own address and the camera only ever
sees one client. Radio talks to the HomeOS radio service (radio.py) on this Pi.

Used by both the desktop shell (sysapi.py) and web access (webshell.py).
"""

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlparse

import devices
import files
import metrics
import printer
import remote_browser
import updater

DEFAULT_CAMERA = "http://camera.local:81/stream"
DEFAULT_RADIO = "http://127.0.0.1:8081"
DEFAULT_DETECT = "http://127.0.0.1:8084"
WIDGETS = ("weather", "radio", "camera", "animation", "site", "map", "update", "printer")
WIDGETS_VERSION = 4
NEW_WIDGETS = {2: ("site", "map"), 3: ("update",), 4: ("printer",)}     # switched on for people who already chose their widgets
UA = {"User-Agent": "HomeOS"}


# --------------------------------------------------------------- camera

class StreamHub:
    """One connection to the camera, shared by every viewer.

    The ESP32-CAM firmware accepts very few stream clients at once. The hub
    connects when the first viewer arrives and disconnects after the last one
    leaves, so the camera rests when nobody is watching.
    """

    def __init__(self, url):
        self.url = url
        self.lock = threading.Lock()
        self.subs = []
        self.thread = None
        self.ctype = "multipart/x-mixed-replace; boundary=frame"

    def subscribe(self):
        q = queue.Queue(maxsize=8)
        with self.lock:
            self.subs.append(q)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="camera-hub")
                self.thread.start()
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def _send(self, item):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                pass            # a slow viewer misses a piece and keeps the connection
        return bool(subs)

    def _run(self):
        try:
            req = urllib.request.Request(self.url, headers=UA)
            with urllib.request.urlopen(req, timeout=8) as r:
                self.ctype = r.headers.get("Content-Type") or self.ctype
                while True:
                    chunk = r.read(4096)
                    if not chunk:
                        break
                    if not self._send(chunk):
                        break   # nobody is watching any more
        except Exception:
            pass
        with self.lock:
            self.thread = None
            subs, self.subs = self.subs, []
        for q in subs:          # tells the viewers that the picture ended
            try:
                q.put_nowait(None)
            except queue.Full:
                pass


_hubs = {}
_hubs_lock = threading.Lock()


def _hub(url):
    with _hubs_lock:
        if url not in _hubs:
            _hubs.clear()       # the address was changed in Settings
            _hubs[url] = StreamHub(url)
        return _hubs[url]


def _fail(handler, code, message):
    body = json.dumps({"ok": False, "error": message}).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _begin(handler, ctype):
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True


def serve_camera(handler, url):
    """Answers a GET with the live picture (an endless MJPEG response)."""
    if not url:
        return _fail(handler, 404, "No camera address is set.")
    hub = _hub(url)
    q = hub.subscribe()
    try:
        try:
            first = q.get(timeout=10)
        except queue.Empty:
            first = None
        if first is None:
            return _fail(handler, 502, "The camera is not answering.")
        _begin(handler, hub.ctype)
        handler.wfile.write(first)
        while True:
            chunk = q.get(timeout=15)
            if chunk is None:
                break
            handler.wfile.write(chunk)
    except (queue.Empty, OSError):
        pass                    # the camera went quiet or the viewer left
    finally:
        hub.unsubscribe(q)


# Settings the camera firmware accepts at /control (same list as handle_control()
# in esp32cam.ino); anything else is refused here and never reaches the camera.
CAMERA_VARS = {
    "framesize": ("qqvga", "qvga", "vga", "svga", "xga", "sxga", "uxga"),
    "quality": (10, 63),
    "brightness": (-2, 2),
    "contrast": (-2, 2),
    "saturation": (-2, 2),
    "fps": (1, 30),
    "vflip": bool,
    "hmirror": bool,
    "led": bool,
}
# framesize_t numbers of the esp32-camera driver, as /settings reports them
FRAMESIZES = {1: "qqvga", 5: "qvga", 8: "vga", 9: "svga", 10: "xga", 12: "sxga", 13: "uxga"}
CAMERA_OFF = "The camera is not answering."


def _camera_base(url):
    """The camera's control address: same host as the stream, port 80."""
    host = urlparse(url or "").hostname
    return "http://%s" % host if host else None


def _camera_get(url, path, timeout=5):
    base = _camera_base(url)
    if not base:
        raise OSError("no camera address")
    req = urllib.request.Request(base + path, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(8 * 1024 * 1024)


def camera_settings(url):
    try:
        raw = json.loads(_camera_get(url, "/settings"))
        if not isinstance(raw, dict):
            raise ValueError
    except (OSError, ValueError):
        return {"ok": False, "error": CAMERA_OFF}
    ms = raw.get("fps_limit_ms") or 0
    out = {key: raw.get(key) for key in ("quality", "brightness", "contrast", "saturation")}
    out.update({key: bool(raw.get(key)) for key in ("vflip", "hmirror", "led")})
    out["framesize"] = FRAMESIZES.get(raw.get("framesize"))
    out["fps"] = round(1000 / ms) if ms else 0
    return {"ok": True, "settings": out}


def camera_control(url, var, val):
    spec = CAMERA_VARS.get(var)
    if spec is None:
        return {"ok": False, "error": "Unknown camera setting."}
    if spec is bool:
        val = "1" if val in (True, 1, "1") else "0"
    elif isinstance(spec[0], str):
        if val not in spec:
            return {"ok": False, "error": "That value is not allowed."}
    else:
        try:
            val = str(max(spec[0], min(spec[1], int(val))))
        except (TypeError, ValueError):
            return {"ok": False, "error": "That value is not allowed."}
    try:
        _camera_get(url, "/control?var=%s&val=%s" % (var, val))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "The camera refused (%d)." % e.code}
    except OSError:
        return {"ok": False, "error": CAMERA_OFF}
    return {"ok": True}


def serve_capture(handler, url):
    """Answers a GET with one full-size photo."""
    try:
        data = _camera_get(url, "/capture", timeout=10)
    except OSError:
        return _fail(handler, 502, CAMERA_OFF)
    handler.send_response(200)
    handler.send_header("Content-Type", "image/jpeg")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Content-Disposition", 'attachment; filename="camera.jpg"')
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(data)


# --------------------------------------------------------------- radio

def _radio_dir():
    return os.path.join(_config_dir or os.path.expanduser("~/.config/homeos"), "radio")


def _radio_token():
    """The admin secret of the radio service (radio.py writes it, mode 600)."""
    try:
        with open(os.path.join(_radio_dir(), "token"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def radio_admin(base, method, path, body=None, timeout=10):
    """Calls an admin route of the radio service with its token."""
    token = _radio_token()
    if not token:
        return {"ok": False, "error": RADIO_OFF}
    data = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={**UA, "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read(4 * 1024 * 1024) or b"{}")
        return {"ok": True, **out}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read(65536) or b"{}").get("error")
        except ValueError:
            msg = None
        return {"ok": False, "error": msg or "The radio refused (%d)." % e.code}
    except (OSError, ValueError):
        return {"ok": False, "error": RADIO_OFF}


# ---- when the station does not answer: what is wrong, and the fixes HomeOS can make

RADIO_UNIT = "homeos-radio.service"
OLD_RADIO_UNIT = "radio-server.service"


def _unit(unit, user=False):
    cmd = ["systemctl"] + (["--user"] if user else []) + ["show", "-p", "LoadState,ActiveState,SubState,NRestarts", unit]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def _port_owner(port):
    """pid, command line and folder of the program listening on a TCP port (own user only)."""
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as f:
                next(f)
                for line in f:
                    p = line.split()
                    if p[3] == "0A" and int(p[1].rsplit(":", 1)[1], 16) == port:
                        inodes.add(p[9])
        except (OSError, StopIteration, IndexError, ValueError):
            pass
    if not inodes:
        return None
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            for fd in os.listdir("/proc/%s/fd" % pid):
                link = os.readlink("/proc/%s/fd/%s" % (pid, fd))
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    with open("/proc/%s/cmdline" % pid, "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                    try:
                        cwd = os.readlink("/proc/%s/cwd" % pid)
                    except OSError:
                        cwd = ""
                    return {"pid": int(pid), "cmd": cmd[:200], "cwd": cwd, "ours": "radio.py" in cmd}
        except OSError:
            continue
    return {"pid": None, "cmd": "", "cwd": "", "ours": False}     # someone else's program


def radio_status(base):
    port = urlparse(base).port or 8081
    svc = _unit(RADIO_UNIT)
    old_sys, old_user = _unit(OLD_RADIO_UNIT), _unit(OLD_RADIO_UNIT, user=True)
    owner = _port_owner(port)
    admin = radio_admin(base, "GET", "/api/tracks", timeout=4)
    log = []
    try:
        with open(os.path.join(_radio_dir(), "radio.log"), encoding="utf-8", errors="replace") as f:
            log = f.read()[-3000:].splitlines()[-12:]
    except OSError:
        pass
    old_running = old_sys.get("ActiveState") == "active" or old_user.get("ActiveState") == "active" \
        or bool(owner and not owner["ours"])
    if admin.get("ok"):
        reason = ""
    elif old_running:
        reason = "The old radio server still holds port %d, so the new one cannot start. Stop the old one." % port
    elif svc.get("LoadState") != "loaded":
        reason = "The radio service is not installed yet. Install the latest HomeOS update."
    elif svc.get("ActiveState") != "active":
        reason = "The radio service is %s." % (svc.get("SubState") or svc.get("ActiveState") or "stopped")
    elif not _radio_token():
        reason = "The radio's key is missing; restart the radio."
    else:
        reason = admin.get("error") or RADIO_OFF
    return {"ok": True, "working": bool(admin.get("ok")), "reason": reason,
            "service": svc.get("ActiveState", "unknown"), "sub": svc.get("SubState", ""),
            "restarts": svc.get("NRestarts", "0"), "old": old_running,
            "port": port, "owner": owner, "log": log,
            "ffmpeg": bool(shutil.which("ffmpeg")), "ytdlp": bool(shutil.which("yt-dlp"))}


def radio_fix(base, action):
    """stop-old: the old radio server of this user goes (its service and the program on the port);
    start: (re)starts the HomeOS radio service (install.sh allows this one sudo command)."""
    port = urlparse(base).port or 8081
    if action == "stop-old":
        for cmd in (["systemctl", "--user", "disable", "--now", OLD_RADIO_UNIT],
                    ["sudo", "-n", shutil.which("systemctl") or "/usr/bin/systemctl", "disable", "--now", OLD_RADIO_UNIT]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=20)
            except (OSError, subprocess.SubprocessError):
                pass
        owner = _port_owner(port)
        if owner and owner["pid"] and not owner["ours"]:
            try:
                os.kill(owner["pid"], 15)
            except OSError as e:
                return {"ok": False, "error": "Could not stop the program on port %d (%s)." % (port, e.strerror)}
        elif owner and not owner["pid"]:
            return {"ok": False, "error": "Port %d belongs to a program of another user. Stop it from the console: "
                                          "sudo systemctl disable --now %s" % (port, OLD_RADIO_UNIT)}
        return {"ok": True}
    if action == "start":
        ctl = shutil.which("systemctl") or "/usr/bin/systemctl"
        try:
            p = subprocess.run(["sudo", "-n", ctl, "restart", RADIO_UNIT], capture_output=True, text=True, timeout=25)
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "error": "Could not start the radio."}
        if p.returncode != 0:
            return {"ok": False, "error": "Starting the radio from here is not allowed yet; install the latest update. "
                                          + (p.stderr or "").strip()[-160:]}
        return {"ok": True}
    return {"ok": False, "error": "Unknown action."}


AUDIO_UPLOAD = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus")


def radio_upload(handler, query, settings):
    """POST with the raw audio file as the body: ?name=<file name>&title=<title>.
    The file goes to the radio's incoming folder; the radio checks and takes it."""
    q = parse_qs(query)
    name = os.path.basename(q.get("name", [""])[0])
    ext = os.path.splitext(name)[1].lower()
    try:
        n = int(handler.headers.get("Content-Length", "-1"))
    except ValueError:
        n = -1
    if ext not in AUDIO_UPLOAD:
        handler.close_connection = True
        return files._json(handler, 400, {"ok": False, "error": "Choose an audio file (MP3, WAV, M4A, OGG, FLAC)."})
    if n <= 0 or n > 300 * 1024 * 1024:
        handler.close_connection = True
        return files._json(handler, 413, {"ok": False, "error": "The file is too large (300 MB at most)."})
    folder = os.path.join(_radio_dir(), "incoming")
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, "up-%d-%s%s" % (time.time() * 1000, os.urandom(4).hex(), ext))
    try:
        with open(tmp, "wb") as out:
            left = n
            while left > 0:
                chunk = handler.rfile.read(min(left, 256 * 1024))
                if not chunk:
                    raise OSError("The upload was cut off.")
                out.write(chunk)
                left -= len(chunk)
    except OSError as e:
        handler.close_connection = True
        try:
            os.remove(tmp)
        except OSError:
            pass
        return files._json(handler, 400, {"ok": False, "error": str(e)})
    title = q.get("title", [""])[0] or os.path.splitext(name)[0]
    r = radio_admin(settings.get()["radio"], "POST", "/api/tracks/import",
                    {"file": os.path.basename(tmp), "title": title}, timeout=60)
    if not r.get("ok"):
        try:
            os.remove(tmp)
        except OSError:
            pass
    return files._json(handler, 200 if r.get("ok") else 400, r)


def radio_now(base):
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/now-playing", headers=UA)
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read(65536) or b"{}")
        return {"ok": True, "nowPlaying": data.get("nowPlaying")}
    except (OSError, ValueError):
        return {"ok": False, "error": RADIO_OFF}


def radio_skip(base):
    return radio_admin(base, "POST", "/api/tracks/skip")


def _relay(handler, url, ctype, error):
    """Passes an endless upstream response (audio, MJPEG) on to the browser."""
    try:
        req = urllib.request.Request(url, headers=UA)
        upstream = urllib.request.urlopen(req, timeout=8)
    except (OSError, ValueError):
        return _fail(handler, 502, error)
    try:
        _begin(handler, upstream.headers.get("Content-Type") or ctype)
        while True:
            chunk = upstream.read(4096)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except OSError:
        pass
    finally:
        upstream.close()


def serve_radio(handler, base):
    """Answers a GET with the live mp3 stream."""
    _relay(handler, base.rstrip("/") + "/api/stream", "audio/mpeg", RADIO_OFF)


# --------------------------------------------------------------- motion detection
# The detection service (dashboard.py from the esp32cam project) runs on this Pi
# on port 8084: it watches the camera, marks people and animals, records clips.

DETECT_OFF = "Motion detection is not running on this Pi."
RADIO_OFF = "The radio service is not running on this Pi."


def _get_json(url, timeout=4):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(4 * 1024 * 1024) or b"{}")


DETECT_UNIT = "homeos-detect.service"


def detect_service():
    """State of the detection service: active, inactive, failed ... or 'not installed'."""
    for s in metrics.services():
        if s["name"] == "homeos-detect":
            return s["state"]
    return "not installed"


def detect_power(on):
    """Switches the detection service on or off (and keeps it that way after a restart).
    install.sh allows exactly these two systemctl commands with sudo and no password."""
    if detect_service() == "not installed":
        return {"ok": False, "error": "Motion detection is not installed on this Pi (it is a separate service on port 8084)."}
    ctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    try:
        p = subprocess.run(["sudo", "-n", ctl, "enable" if on else "disable", "--now", DETECT_UNIT],
                           capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "error": "Could not switch motion detection."}
    if p.returncode != 0:
        return {"ok": False, "error": "Switching detection from here is not allowed yet. Run  sudo ./install.sh --web  "
                                      "once from the console in the HomeOS folder."}
    return {"ok": True, "on": bool(on)}


DETECT_OTHER = ("Another program is running motion detection on this Pi (port 8084), so the Motion "
                "switch cannot stop it. Stop that program, then try again.")


def _port_busy(base, timeout=0.3):
    """True when something listens at the detector's address (a quick connect, no HTTP)."""
    u = urllib.parse.urlsplit(base)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def detect_state(base):
    service = detect_service()
    if service in ("inactive", "failed") and _port_busy(base):
        # the Motion service is off, yet a detector answers: it is not ours to switch
        return {"ok": False, "error": DETECT_OTHER, "service": service, "other": True}
    try:
        if service not in ("active", "not installed"):
            raise OSError("switched off")       # do not wait for a service that is stopped
        d = _get_json(base.rstrip("/") + "/api/state")
    except (OSError, ValueError):
        return {"ok": False, "error": DETECT_OFF, "service": service}
    det = d.get("detector") or {}
    seen = sorted({str(x.get("cls")) for x in det.get("detections") or [] if isinstance(x, dict)})
    events = [{"t": e.get("t"), "type": e.get("type"), "classes": e.get("classes") or []}
              for e in (d.get("events") or [])[-8:] if isinstance(e, dict)]
    return {"ok": True, "service": service,
            "camera": bool((d.get("stream") or {}).get("connected")),
            "seen": seen,
            "recording": bool((d.get("recorder") or {}).get("writing")),
            "events": events[::-1]}


def detect_clips(base):
    try:
        rows = _get_json(base.rstrip("/") + "/api/clips").get("clips") or []
    except (OSError, ValueError):
        return {"ok": False, "error": DETECT_OFF}
    clips = []
    for r in rows[:40]:
        try:
            classes = json.loads(r.get("classes") or "[]")
        except (TypeError, ValueError):
            classes = []
        clips.append({"id": r.get("id"), "t": r.get("ts"), "classes": classes,
                      "duration": r.get("duration"), "size": r.get("size")})
    return {"ok": True, "clips": clips}


def detect_record(base):
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/clips/record", data=b"{}", method="POST",
                                     headers={**UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read(65536)
        return {"ok": True}
    except OSError:
        return {"ok": False, "error": DETECT_OFF}


def serve_clip(handler, base, clip_id):
    """Answers a GET with a recorded clip; Range is passed on so the player can seek."""
    try:
        url = "%s/api/clips/%d/video" % (base.rstrip("/"), int(clip_id))
    except (TypeError, ValueError):
        return _fail(handler, 404, "No such clip.")
    headers = dict(UA)
    if handler.headers.get("Range"):
        headers["Range"] = handler.headers["Range"]
    try:
        upstream = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15)
    except urllib.error.HTTPError as e:
        return _fail(handler, e.code if e.code in (404, 416) else 502, "No such clip.")
    except OSError:
        return _fail(handler, 502, DETECT_OFF)
    try:
        handler.send_response(upstream.status)
        handler.send_header("Content-Type", upstream.headers.get("Content-Type") or "video/mp4")
        for name in ("Content-Length", "Content-Range", "Accept-Ranges"):
            if upstream.headers.get(name):
                handler.send_header(name, upstream.headers[name])
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        while True:
            chunk = upstream.read(16384)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except OSError:
        pass
    finally:
        upstream.close()


# --------------------------------------------------------------- weather
# Open-Meteo: free, no key. Only the coordinates of the chosen town are sent.

WEATHER_TEXT = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}
_weather = {"key": None, "at": 0.0, "data": None}
_weather_lock = threading.Lock()


def _weather_kind(code):
    if code in (0, 1):
        return "sun"
    if code == 2:
        return "partly"
    if code in (3, 45, 48):
        return "cloud"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    return "rain"


def weather(place):
    if not place:
        return {"ok": False, "unset": True, "error": "Choose your town in Settings."}
    key = (place["lat"], place["lon"])
    with _weather_lock:
        if _weather["key"] == key and time.time() - _weather["at"] < 900:
            return _weather["data"]
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,is_day"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min"
           "&timezone=auto&forecast_days=4" % key)
    try:
        d = _get_json(url, timeout=8)
        cur, daily = d["current"], d["daily"]
        code = int(cur["weather_code"])
        days = []
        for i, day in enumerate(daily["time"][:4]):
            c = int(daily["weather_code"][i])
            days.append({"date": day, "kind": _weather_kind(c), "text": WEATHER_TEXT.get(c, ""),
                         "max": round(daily["temperature_2m_max"][i]), "min": round(daily["temperature_2m_min"][i])})
        data = {"ok": True, "place": place["name"], "temp": round(cur["temperature_2m"]),
                "feels": round(cur["apparent_temperature"]), "wind": round(cur["wind_speed_10m"]),
                "kind": _weather_kind(code), "text": WEATHER_TEXT.get(code, ""), "days": days,
                "fog": code in (45, 48), "night": cur.get("is_day", 1) == 0}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        with _weather_lock:
            if _weather["key"] == key and _weather["data"]:
                return _weather["data"]         # an older forecast beats none
        return {"ok": False, "error": "The weather service is not answering."}
    with _weather_lock:
        _weather.update(key=key, at=time.time(), data=data)
    return data


def weather_public(settings):
    """What the sign-in page may show before anyone has signed in: the forecast
    without the name of the town."""
    cfg = settings.get()
    if "weather" not in cfg["widgets"] or not cfg["weather"]:
        return {"ok": False}
    w = weather(cfg["weather"])
    if not w.get("ok"):
        return {"ok": False}
    return {"ok": True, **{k: w[k] for k in ("temp", "feels", "wind", "kind", "text", "days")}}


def weather_place(name, settings):
    name = str(name or "").strip()[:80]
    if not name:
        settings.update({"weather": None})
        return {"ok": True, "settings": settings.get()}
    try:
        d = _get_json("https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&format=json&name="
                      + quote(name), timeout=8)
        hit = (d.get("results") or [])[0]
        label = hit["name"] + (", " + hit["country"] if hit.get("country") else "")
        place = {"name": label, "lat": round(float(hit["latitude"]), 3), "lon": round(float(hit["longitude"]), 3)}
    except IndexError:
        return {"ok": False, "error": "That town was not found. Try another spelling."}
    except (OSError, ValueError, KeyError, TypeError):
        return {"ok": False, "error": "The weather service is not answering."}
    settings.update({"weather": place})
    return {"ok": True, "settings": settings.get()}


# --------------------------------------------------------------- map tiles
# OpenStreetMap tiles for the map widget, fetched by the Pi (the page may only load
# its own files) and kept for two weeks, as the OpenStreetMap tile policy asks.

TILE_URL = "https://tile.openstreetmap.org/%d/%d/%d.png"
TILE_KEEP = 14 * 86400


def serve_tile(handler, query):
    q = parse_qs(query)
    try:
        z, x, y = (int(q.get(k, ["-1"])[0]) for k in ("z", "x", "y"))
        if not (0 <= z <= 18 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise ValueError
    except ValueError:
        return _fail(handler, 404, "No such tile.")
    folder = os.path.join(_config_dir or os.path.expanduser("~/.config/homeos"), "tiles", str(z), str(x))
    path = os.path.join(folder, "%d.png" % y)
    data = None
    try:
        if time.time() - os.path.getmtime(path) < TILE_KEEP:
            with open(path, "rb") as f:
                data = f.read()
    except OSError:
        pass
    if data is None:
        try:
            req = urllib.request.Request(TILE_URL % (z, x, y), headers={"User-Agent": "HomeOS/0.11 (home dashboard on a Raspberry Pi)"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read(2 * 1024 * 1024)
            os.makedirs(folder, exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
        except OSError:
            try:
                with open(path, "rb") as f:          # an old tile beats none
                    data = f.read()
            except OSError:
                return _fail(handler, 502, "The map is not answering.")
    handler.send_response(200)
    handler.send_header("Content-Type", "image/png")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "private, max-age=86400")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(data)


# --------------------------------------------------------------- routes
# One table for the desktop shell and for web access.

_history = None
# --------------------------------------------------------------- a site's hero section
# For the featured-site widget: the headline, the line under it and the main picture
# of a site, read from its page (Open Graph tags, <h1>, <title>). Only for the user's
# own site tiles, kept for six hours.

_hero = {}
_hero_lock = threading.Lock()
HERO_TTL = 6 * 3600
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36 HomeOS",
              "Accept": "text/html,application/xhtml+xml,image/*;q=0.9,*/*;q=0.5"}


class _HeroParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.title, self.h1, self._in = {}, "", "", None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            if k and a.get("content") and k not in self.meta:
                self.meta[k] = a["content"].strip()
        elif tag in ("title", "h1") and not getattr(self, tag):
            self._in = tag

    def handle_endtag(self, tag):
        if tag == self._in:
            self._in = None

    def handle_data(self, data):
        if self._in == "title":
            self.title += data
        elif self._in == "h1":
            self.h1 += data


def _tile_url(settings, url):
    return any(t.get("url") == url for t in settings.get().get("tiles", []))


def site_hero(url):
    with _hero_lock:
        c = _hero.get(url)
        if c and time.time() - c["at"] < HERO_TTL:
            return c
    data = {"at": time.time(), "ok": False, "title": "", "text": "", "img": None, "ctype": ""}
    try:
        req = urllib.request.Request(url, headers=BROWSER_UA)
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read(2 * 1024 * 1024)
            charset = r.headers.get_content_charset() or "utf-8"
            base = r.geturl()
        p = _HeroParser()
        p.feed(raw.decode(charset, "replace"))
        m = p.meta
        squash = lambda t: re.sub(r"\s+", " ", t or "").strip()
        h1 = squash(p.h1)
        data["title"] = (h1 if 3 <= len(h1) <= 70 else "") or squash(m.get("og:title") or m.get("twitter:title") or p.title)[:90]
        data["text"] = squash(m.get("og:description") or m.get("twitter:description") or m.get("description"))[:180]
        img = m.get("og:image") or m.get("og:image:url") or m.get("twitter:image") or m.get("twitter:image:src")
        if img:
            img = urllib.parse.urljoin(base, img)
            if img.startswith(("http://", "https://")):
                with urllib.request.urlopen(urllib.request.Request(img, headers=BROWSER_UA), timeout=8) as r:
                    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
                    body = r.read(6 * 1024 * 1024 + 1)
                if ctype.startswith("image/") and ctype != "image/svg+xml" and len(body) <= 6 * 1024 * 1024:
                    data["img"], data["ctype"] = body, ctype
        data["ok"] = True
    except (OSError, ValueError, UnicodeError) as e:
        data["error"] = str(e)[:120]
    with _hero_lock:
        _hero[url] = data
    return data


def serve_site(handler, path, query, settings):
    url = parse_qs(query).get("u", [""])[0]
    if not _tile_url(settings, url):
        return _fail(handler, 404, "Not one of your sites.")
    h = site_hero(url)
    if path == "/api/site/hero":
        body = json.dumps({"ok": h["ok"], "title": h["title"], "text": h["text"], "image": bool(h["img"]),
                           "v": int(h["at"])}).encode("utf-8")
        ctype = "application/json; charset=utf-8"
    else:
        if not h["img"]:
            return _fail(handler, 404, "This site has no main picture.")
        body, ctype = h["img"], h["ctype"]
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "private, max-age=3600")
    handler.end_headers()
    handler.wfile.write(body)


_config_dir = None


def start(config_dir):
    """Starts background work (the system monitor's sampling)."""
    global _history, _config_dir
    _config_dir = config_dir
    if _history is None:
        _history = metrics.History(config_dir).start()


def _printer():
    return printer.get(_config_dir or os.path.expanduser("~/.config/homeos"))


def _rb():
    return remote_browser.get(_config_dir or os.path.expanduser("~/.config/homeos"))


def _rb_call(fn):
    try:
        res = fn()
        return res if isinstance(res, dict) else {"ok": True}
    except remote_browser.Unavailable as e:
        return {"ok": False, "error": str(e)}
    except (OSError, ValueError, TypeError) as e:
        return {"ok": False, "error": "The Pi browser had a problem: %s" % e}


SERVE_PATHS = ("/api/camera/stream", "/api/camera/capture", "/api/radio/stream",
               "/api/detect/stream", "/api/detect/clip", "/api/files/get", "/api/rb/stream", "/api/map/tile",
               "/api/printer/camera", "/api/printer/model", "/api/site/hero", "/api/site/hero-image")


def serve(handler, path, query, settings):
    """Streams and files. Returns False when the path is not one of them."""
    cfg = settings.get()
    if path == "/api/camera/stream":
        serve_camera(handler, cfg["camera"])
    elif path == "/api/camera/capture":
        serve_capture(handler, cfg["camera"])
    elif path == "/api/radio/stream":
        serve_radio(handler, cfg["radio"])
    elif path == "/api/detect/stream":
        _relay(handler, cfg["detect"].rstrip("/") + "/stream.mjpg",
               "multipart/x-mixed-replace; boundary=frame", DETECT_OFF)
    elif path == "/api/printer/camera":
        _printer().camera.serve(handler)
    elif path == "/api/printer/model":
        _printer().model.serve(handler)
    elif path == "/api/map/tile":
        serve_tile(handler, query)
    elif path in ("/api/site/hero", "/api/site/hero-image"):
        serve_site(handler, path, query, settings)
    elif path == "/api/rb/stream":
        _rb().stream(handler)
    elif path == "/api/files/get":
        files.serve(handler, query)
    elif path == "/api/detect/clip":
        serve_clip(handler, cfg["detect"], parse_qs(query).get("id", [""])[0])
    else:
        return False
    return True


def api_get(path, settings):
    cfg = settings.get()
    if path == "/api/radio/now":
        return radio_now(cfg["radio"])
    if path == "/api/radio/tracks":
        return radio_admin(cfg["radio"], "GET", "/api/tracks")
    if path == "/api/radio/status":
        return radio_status(cfg["radio"])
    if path == "/api/camera/settings":
        return camera_settings(cfg["camera"])
    if path == "/api/detect/state":
        return detect_state(cfg["detect"])
    if path == "/api/detect/clips":
        return detect_clips(cfg["detect"])
    if path == "/api/weather":
        return weather(cfg["weather"])
    if path == "/api/monitor":
        return _history.read() if _history else {"ok": False, "error": "The monitor is not running."}
    if path == "/api/printer":
        return _printer().state()
    if path == "/api/printer/config":
        return _printer().public_config()
    if path == "/api/rb/state":
        return _rb().state()
    if path == "/api/bt":
        return devices.bt_state()
    if path == "/api/monitor/now":
        return _history.now() if _history else {"ok": False}
    if path == "/api/update/status":
        return updater.status()
    return None


def api_post(path, body, settings):
    cfg = settings.get()
    if path == "/api/radio/skip":
        return radio_skip(cfg["radio"])
    if path.startswith("/api/radio/"):
        base = cfg["radio"]
        if path == "/api/radio/from-url":
            return radio_admin(base, "POST", "/api/tracks/from-url", {"url": body.get("url"), "title": body.get("title")})
        if path == "/api/radio/reorder":
            return radio_admin(base, "PATCH", "/api/tracks/reorder", {"order": body.get("order")})
        if path == "/api/radio/track":
            fields = {k: body[k] for k in ("title", "visible") if k in body}
            return radio_admin(base, "PATCH", "/api/tracks/%d" % int(body.get("id", 0)), fields)
        if path == "/api/radio/delete":
            return radio_admin(base, "DELETE", "/api/tracks/%d" % int(body.get("id", 0)))
        if path == "/api/radio/restart":
            return radio_admin(base, "POST", "/api/server/restart")
        if path == "/api/radio/fix":
            return radio_fix(base, str(body.get("action")))
        if path == "/api/radio/public":
            return radio_admin(base, "POST", "/api/server/public", {"on": body.get("on") is True})
    if path == "/api/camera/control":
        return camera_control(cfg["camera"], body.get("var"), body.get("val"))
    if path == "/api/detect/record":
        return detect_record(cfg["detect"])
    if path == "/api/printer/config":
        return _printer().set_config(body.get("host"), body.get("code"), body.get("serial"))
    if path == "/api/printer/cmd":
        return _printer().act(str(body.get("action")), body.get("value"))
    if path == "/api/web/check":
        url = str(body.get("url") or "")
        if urlparse(url).scheme not in ("http", "https"):
            return {"ok": False, "error": "Not a web address."}
        return {"ok": True, "framable": remote_browser.framable(url), "pi_browser": _rb().available()}
    if path == "/api/rb/open":
        url = str(body.get("url") or "")
        if url and urlparse(url).scheme not in ("http", "https"):
            return {"ok": False, "error": "Not a web address."}
        return _rb_call(lambda: _rb().open(url, body.get("w"), body.get("h"), body.get("scale")))
    if path == "/api/rb/go":
        return _rb_call(lambda: _rb().go(str(body.get("action"))))
    if path == "/api/rb/tap":
        return _rb_call(lambda: _rb().tap(body.get("x", 0), body.get("y", 0)))
    if path == "/api/rb/scroll":
        return _rb_call(lambda: _rb().scroll(body.get("x", 0.5), body.get("y", 0.5), body.get("dx", 0), body.get("dy", 0)))
    if path == "/api/rb/type":
        return _rb_call(lambda: _rb().type_text(body.get("text")))
    if path == "/api/rb/key":
        return _rb_call(lambda: _rb().key(str(body.get("key"))))
    if path == "/api/files/list":
        return files.listing(body.get("path"), body.get("hidden") is True)
    if path == "/api/files/mkdir":
        return files.make_folder(body.get("path"), body.get("name"))
    if path == "/api/files/rename":
        return files.rename(body.get("path"), body.get("name"))
    if path == "/api/files/delete":
        return files.delete(body.get("path"))
    if path == "/api/bt/power":
        return devices.bt_power(body.get("on") is True)
    if path == "/api/bt/scan":
        return devices.bt_scan()
    if path == "/api/bt/device":
        return devices.bt_device(body.get("action"), body.get("mac"))
    if path == "/api/detect/power":
        return detect_power(body.get("on") is True)
    if path == "/api/weather/place":
        return weather_place(body.get("name"), settings)
    if path == "/api/update/check":
        return updater.check()
    if path == "/api/update/apply":
        return updater.apply()
    return None
