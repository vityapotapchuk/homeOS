"""Bambu Lab printer (A1, A1 mini, P1P, P1S) for HomeOS, over the local network.

Status and commands go through the printer's own MQTT broker (TLS, port 8883,
user "bblp", password = the LAN access code shown on the printer). The camera of
the A1 and P1 series is a TLS stream of JPEG pictures on port 6000; it is passed
on to the browser as MJPEG. The picture of the model being printed comes from
the print file itself (a .3mf holds a rendered picture of every plate), read
from the printer's storage over FTPS (implicit TLS, port 990).

Since mid-2025 firmware, the printer obeys commands from other programs only in
LAN Only mode with Developer Mode switched on; it reports its status either way.

Only the Python standard library is used. The connection to the printer opens
when someone looks at it and closes ten minutes after the last look.
"""

import ftplib
import hashlib
import json
import os
import queue
import re
import socket
import ssl
import struct
import threading
import time
import zipfile

MQTT_PORT = 8883
CAMERA_PORT = 6000
FTP_PORT = 990
MAX_FILE = 400 * 1024 * 1024
IDLE_CLOSE = 600
SPEEDS = {"1": "Silent", "2": "Standard", "3": "Sport", "4": "Ludicrous"}


def _tls():
    # the printer has its own self-signed certificate on the home network
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def serial_from_certificate(host):
    """The printer's certificate is issued to its serial number: read it from there."""
    with socket.create_connection((host, MQTT_PORT), timeout=6) as raw:
        with _tls().wrap_socket(raw) as s:
            der = s.getpeercert(binary_form=True) or b""
    names = []
    i = 0
    while True:
        i = der.find(b"\x06\x03\x55\x04\x03", i)       # OID 2.5.4.3, commonName
        if i < 0:
            break
        j = i + 5
        if j + 2 <= len(der) and der[j] in (0x0C, 0x13, 0x16):     # UTF8, Printable, IA5 string
            n = der[j + 1]
            names.append(der[j + 2:j + 2 + n].decode("ascii", "replace"))
        i = j
    for name in names:
        if re.fullmatch(r"[0-9A-Z]{10,24}", name):
            return name
    return None


# --------------------------------------------------------------- a small MQTT 3.1.1 client

def _len(n):
    out = b""
    while True:
        b, n = n % 128, n // 128
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _str(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


class MQTT:
    def __init__(self, host, password, client_id):
        raw = socket.create_connection((host, MQTT_PORT), timeout=10)
        self.sock = _tls().wrap_socket(raw)
        self.lock = threading.Lock()
        body = _str("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60) + _str(client_id) + _str("bblp") + _str(password)
        self.sock.sendall(b"\x10" + _len(len(body)) + body)
        kind, data = self.read()
        if kind != 2 or len(data) < 2:
            raise OSError("the printer did not answer")
        if data[1] != 0:
            raise PermissionError("the printer refused the access code")
        self.next_id = 1

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OSError("connection closed")
            buf += chunk
        return buf

    def read(self):
        head = self._recv(1)[0]
        n, mult = 0, 1
        while True:
            b = self._recv(1)[0]
            n += (b & 0x7F) * mult
            mult *= 128
            if not b & 0x80:
                break
        return head >> 4, self._recv(n) if n else b""

    def send(self, data):
        with self.lock:
            self.sock.sendall(data)

    def subscribe(self, topic):
        self.next_id += 1
        body = struct.pack("!H", self.next_id) + _str(topic) + b"\x00"
        self.send(b"\x82" + _len(len(body)) + body)

    def publish(self, topic, payload):
        body = _str(topic) + payload.encode("utf-8")
        self.send(b"\x30" + _len(len(body)) + body)

    def ping(self):
        self.send(b"\xC0\x00")

    def close(self):
        try:
            self.send(b"\xE0\x00")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _merge(into, new):
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            _merge(into[k], v)
        else:
            into[k] = v


# --------------------------------------------------------------- the printer

class Printer:
    def __init__(self, config_dir):
        self.path = os.path.join(config_dir, "printer.json")
        self.lock = threading.Lock()
        self.cfg = self._load()
        self.mtime = self._mtime()
        self.data = {}
        self.online = False
        self.error = ""
        self.last_report = 0.0
        self.last_look = 0.0
        self.mqtt = None
        self.thread = None
        self.wake = threading.Event()
        self.camera = CameraHub(self)
        self.model = ModelPicture(self, os.path.dirname(self.path))

    # ---- settings (kept in their own file, readable by this user only: it holds the access code)
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return {k: str(d.get(k) or "") for k in ("host", "code", "serial")}
        except (OSError, ValueError):
            return {"host": "", "code": "", "serial": ""}

    def _mtime(self):
        try:
            return os.stat(self.path).st_mtime
        except OSError:
            return 0

    def _reload_if_changed(self):
        mt = self._mtime()
        if mt != self.mtime:
            self.mtime = mt
            cfg = self._load()
            if cfg != self.cfg:
                with self.lock:
                    self.cfg, self.data, self.online, self.error = cfg, {}, False, ""
                self._drop()

    def configured(self):
        return bool(self.cfg["host"] and self.cfg["code"])

    def public_config(self):
        return {"ok": True, "host": self.cfg["host"], "serial": self.cfg["serial"], "has_code": bool(self.cfg["code"])}

    def set_config(self, host, code, serial):
        host = str(host or "").strip().replace(",", ".")[:100]
        if host and not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
            return {"ok": False, "error": "That does not look like an address. Use the IP shown on the printer."}
        cfg = dict(self.cfg, host=host)
        if code:
            code = str(code).strip()
            if not re.fullmatch(r"[A-Za-z0-9]{4,32}", code):
                return {"ok": False, "error": "The access code is the 8 letters and digits shown on the printer."}
            cfg["code"] = code
        cfg["serial"] = re.sub(r"[^0-9A-Za-z]", "", str(serial or ""))[:32].upper()
        steps = []
        if host and cfg["code"]:
            self._drop()                      # free our own connection, the printer allows only a few
            test = diagnose(host, cfg["code"], cfg["serial"])
            steps = test["steps"]
            if test["serial"]:
                cfg["serial"] = test["serial"]
            if not test["ok"]:
                return {"ok": False, "error": steps[-1]["text"], "steps": steps}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(self.path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            os.replace(self.path + ".tmp", self.path)
            self.mtime = self._mtime()
        except OSError as e:
            return {"ok": False, "error": "Could not save: %s" % e}
        with self.lock:
            self.cfg = cfg
            self.data = {}
            self.online = False
            self.error = ""
        self._drop()
        return dict(self.public_config(), steps=steps)

    # ---- the MQTT connection
    def _drop(self):
        m, self.mqtt = self.mqtt, None
        if m:
            m.close()
        self.wake.set()

    def _ensure(self):
        self.last_look = time.time()
        if self.configured() and (self.thread is None or not self.thread.is_alive()):
            self.thread = threading.Thread(target=self._run, daemon=True, name="printer")
            self.thread.start()

    def _run(self):
        while self.configured() and time.time() - self.last_look < IDLE_CLOSE:
            cfg = dict(self.cfg)
            try:
                if not cfg["serial"]:
                    cfg["serial"] = serial_from_certificate(cfg["host"]) or ""
                    if not cfg["serial"]:
                        raise OSError("the serial number is unknown; type it in the printer settings")
                    self.cfg["serial"] = cfg["serial"]
                m = MQTT(cfg["host"], cfg["code"], "homeos-%d" % os.getpid())
                self.mqtt = m
                m.subscribe("device/%s/report" % cfg["serial"])
                self.command({"pushing": {"sequence_id": "0", "command": "pushall"}})
                m.sock.settimeout(20)
                last_ping = time.time()
                while self.mqtt is m and time.time() - self.last_look < IDLE_CLOSE:
                    try:
                        kind, body = m.read()
                    except socket.timeout:
                        kind, body = None, b""
                    if kind == 3 and len(body) > 2:
                        tl = struct.unpack("!H", body[:2])[0]
                        try:
                            msg = json.loads(body[2 + tl:].decode("utf-8", "replace"))
                        except ValueError:
                            msg = None
                        if isinstance(msg, dict):
                            with self.lock:
                                for part in ("print", "system"):
                                    if isinstance(msg.get(part), dict):
                                        _merge(self.data.setdefault(part, {}), msg[part])
                                self.online = True
                                self.error = ""
                                self.last_report = time.time()
                    if time.time() - last_ping > 30:
                        m.ping()
                        last_ping = time.time()
                m.close()
            except PermissionError as e:
                self.error = "The printer refused the access code. Check it on the printer (WLAN > Access Code)."
            except ssl.SSLError as e:
                self.error = "The secure connection to the printer failed (%s)." % (e.reason or e)
            except (OSError, ValueError) as e:
                self.error = "The printer is not answering (%s)." % e
            with self.lock:
                self.online = False
            self.mqtt = None
            self.wake.wait(10)
            self.wake.clear()

    def command(self, payload):
        m = self.mqtt
        if not m:
            return False
        try:
            m.publish("device/%s/request" % self.cfg["serial"], json.dumps(payload))
            return True
        except OSError:
            self._drop()
            return False

    # ---- what HomeOS pages ask for
    def state(self):
        self._reload_if_changed()
        if not self.configured():
            return {"ok": True, "configured": False}
        self._ensure()
        with self.lock:
            p = dict(self.data.get("print", {}))
            online = self.online and time.time() - self.last_report < 120
        num = lambda v, d=None: v if isinstance(v, (int, float)) else (float(v) if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v) else d)
        lights = p.get("lights_report") or []
        light = next((l.get("mode") for l in lights if isinstance(l, dict) and l.get("node") == "chamber_light"), None)
        fan = num(p.get("cooling_fan_speed"))
        return {
            "ok": True, "configured": True, "online": online, "error": "" if online else self.error,
            "state": p.get("gcode_state") or ("OFFLINE" if not online else "IDLE"),
            "file": p.get("subtask_name") or p.get("gcode_file") or "",
            "percent": num(p.get("mc_percent"), 0), "remaining": num(p.get("mc_remaining_time"), 0),
            "layer": num(p.get("layer_num"), 0), "layers": num(p.get("total_layer_num"), 0),
            "nozzle": num(p.get("nozzle_temper")), "nozzle_target": num(p.get("nozzle_target_temper")),
            "bed": num(p.get("bed_temper")), "bed_target": num(p.get("bed_target_temper")),
            "chamber": num(p.get("chamber_temper")),
            "fan": round(fan / 15 * 100) if fan is not None else None,
            "light": light, "speed": str(p.get("spd_lvl") or ""), "wifi": p.get("wifi_signal") or "",
            "print_error": p.get("print_error") or 0,
            "model": self.model.status(p, online),
            "camera_error": self.camera.error,
            "printer_model": model_name(self.cfg.get("serial")),
        }

    def act(self, action, value=None):
        if not self.configured():
            return {"ok": False, "error": "Set up the printer first."}
        self._ensure()
        if not self.mqtt:
            return {"ok": False, "error": self.error or "Connecting to the printer… try again in a moment."}
        if action in ("pause", "resume", "stop"):
            payload = {"print": {"sequence_id": "0", "command": action}}
        elif action in ("light_on", "light_off"):
            payload = {"system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light",
                                  "led_mode": "on" if action == "light_on" else "off", "led_on_time": 500,
                                  "led_off_time": 500, "loop_times": 0, "interval_time": 0}}
        elif action == "speed" and str(value) in SPEEDS:
            payload = {"print": {"sequence_id": "0", "command": "print_speed", "param": str(value)}}
        else:
            return {"ok": False, "error": "Unknown command."}
        if not self.command(payload):
            return {"ok": False, "error": "The printer is not connected."}
        return {"ok": True}


# --------------------------------------------------------------- camera (A1 / P1: JPEG pictures over TLS, port 6000)

# --------------------------------------------------------------- which printer it is

# the first three characters of the serial number name the model
MODELS = {"00M": "X1 Carbon", "00W": "X1", "03W": "X1E", "01S": "P1P", "01P": "P1S",
          "030": "A1 mini", "039": "A1", "094": "H2D", "22E": "P2S"}
VIDEO_MODELS = ("X1 Carbon", "X1", "X1E", "H2D", "P2S")     # these stream RTSPS video on port 322


def model_name(serial):
    return MODELS.get(str(serial or "")[:3].upper(), "")


# --------------------------------------------------------------- the model being printed

class _FTPS(ftplib.FTP_TLS):
    """FTP over implicit TLS, as the printer speaks it. The printer wants the data
    connection to reuse the TLS session of the control connection."""

    def connect(self, host="", port=FTP_PORT, timeout=15, source_address=None):
        self.host, self.port, self.timeout = host, port, timeout
        raw = socket.create_connection((host, port), timeout)
        self.af = raw.family
        self.sock = self.context.wrap_socket(raw, server_hostname=host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def fetch(self, path, out, limit):
        self.voidcmd("TYPE I")
        conn = self.transfercmd("RETR " + path)
        got = 0
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                got += len(chunk)
                if got > limit:
                    raise OSError("the print file is too big")
                out.write(chunk)
            try:
                conn.unwrap()
            except (OSError, ValueError):
                pass
        finally:
            conn.close()
        self.voidresp()


def _plate_png(zpath, plate):
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        for want in ("Metadata/plate_%d.png" % plate, "Metadata/top_%d.png" % plate):
            if want in names:
                return z.read(want)
        pics = sorted(n for n in names if re.fullmatch(r"Metadata/(plate|top)_\d+\.png", n))
        return z.read(pics[0]) if pics else None


class ModelPicture:
    """Keeps a picture of the model in the current print, read once per print file."""

    def __init__(self, printer, cache_dir):
        self.printer = printer
        self.png = os.path.join(cache_dir, "printer-model.png")
        self.meta = os.path.join(cache_dir, "printer-model.json")
        self.lock = threading.Lock()
        self.busy = False
        self.key = ""
        self.result = "none"                  # none, loading, ok, missing, error
        self.why = ""
        self.tried = 0.0
        try:
            with open(self.meta, encoding="utf-8") as f:
                m = json.load(f)
            if os.path.exists(self.png):
                self.key, self.result = m.get("key", ""), "ok"
        except (OSError, ValueError):
            pass

    @staticmethod
    def _job(p):
        gfile = str(p.get("gcode_file") or "")
        task = str(p.get("subtask_name") or "")
        if not gfile and not task:
            return None
        m = re.search(r"plate_(\d+)", gfile)
        plate = int(m.group(1)) if m else int(p.get("plate_idx") or 1) if str(p.get("plate_idx") or "").isdigit() else 1
        return gfile, task, plate

    def status(self, p, online):
        job = self._job(p)
        if not job:
            return {"status": "none"}
        key = "%s|%s|%d" % job
        with self.lock:
            stale = key != self.key or (self.result == "error" and time.time() - self.tried > 120)
            if stale and online and not self.busy:
                self.key, self.result, self.why, self.busy, self.tried = key, "loading", "", True, time.time()
                threading.Thread(target=self._fetch, args=(key,) + job, daemon=True, name="printer-model").start()
            if key != self.key:
                return {"status": "none"}
            return {"status": self.result, "v": hashlib.sha1(key.encode()).hexdigest()[:12], "why": self.why}

    def _candidates(self, gfile, task):
        out = []
        if gfile.lower().endswith(".3mf"):
            base = gfile.rsplit("/", 1)[-1]
            out += [gfile if gfile.startswith("/") else "/" + gfile, "/" + base, "/cache/" + base]
            if gfile.startswith("/sdcard/"):
                out.append(gfile[len("/sdcard"):])
        name = re.sub(r"(\.gcode)?(\.3mf)?$", "", task, flags=re.I)
        if name:
            for folder in ("/", "/cache/", "/model/"):
                out += [folder + name + ".gcode.3mf", folder + name + ".3mf"]
        seen, uniq = set(), []
        for c in out:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq

    def _fetch(self, key, gfile, task, plate):
        cfg = dict(self.printer.cfg)
        tmp = self.png + ".3mf.part"
        result, why = "missing", "The print file is not on the printer's storage (prints from the cloud may keep it elsewhere)."
        ftp = None
        try:
            ftp = _FTPS(context=_tls(), timeout=15)
            ftp.connect(cfg["host"], FTP_PORT)
            ftp.login("bblp", cfg["code"])
            ftp.prot_p()
            for path in self._candidates(gfile, task):
                try:
                    ftp.voidcmd("TYPE I")
                    size = ftp.size(path)
                except ftplib.error_perm:
                    continue
                if size and size > MAX_FILE:
                    why = "The print file is too big to read."
                    continue
                with open(tmp, "wb") as f:
                    ftp.fetch(path, f, MAX_FILE)
                png = _plate_png(tmp, plate)
                if png:
                    with open(self.png + ".tmp", "wb") as f:
                        f.write(png)
                    os.replace(self.png + ".tmp", self.png)
                    with open(self.meta, "w", encoding="utf-8") as f:
                        json.dump({"key": key, "file": path}, f)
                    result, why = "ok", ""
                    break
                why = "The print file has no picture of the model."
        except ftplib.error_perm as e:
            result, why = "error", "The printer did not let HomeOS read its files (%s)." % str(e)[:80]
        except (OSError, EOFError, ssl.SSLError, zipfile.BadZipFile, ftplib.Error) as e:
            result, why = "error", "Could not read the print file (%s)." % (str(e)[:80] or type(e).__name__)
        finally:
            if ftp is not None:
                try:
                    ftp.quit()
                except (OSError, EOFError, ftplib.Error):
                    ftp.close()
            try:
                os.remove(tmp)
            except OSError:
                pass
        with self.lock:
            if self.key == key:
                self.result, self.why = result, why
            self.busy = False

    def serve(self, handler):
        try:
            with open(self.png, "rb") as f:
                data = f.read()
        except OSError:
            return _fail(handler, 404, "No picture of the model yet.")
        handler.send_response(200)
        handler.send_header("Content-Type", "image/png")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "private, max-age=86400")
        handler.end_headers()
        handler.wfile.write(data)


class CameraHub:
    """One connection to the printer's camera, shared by every viewer.

    A1 and P1 printers send JPEG pictures over TLS on port 6000. X1, H2D and P2S
    printers stream video over RTSPS on port 322 instead; that needs ffmpeg on
    the Pi to turn it into pictures. The first kind that works is remembered."""

    def __init__(self, printer):
        self.printer = printer
        self.lock = threading.Lock()
        self.subs = []
        self.thread = None
        self.kind = None          # "jpeg" or "rtsp" once one has worked
        self.error = ""
        self.proc = None

    def _send(self, frame):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass
        return bool(subs)

    def _jpeg(self, cfg):
        """A1 / P1: returns True if pictures came."""
        try:
            raw = socket.create_connection((cfg["host"], CAMERA_PORT), timeout=8)
        except OSError as e:
            raise ConnectionError("port %d: %s" % (CAMERA_PORT, e.strerror or e))
        s = _tls().wrap_socket(raw)
        try:
            auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + cfg["code"].encode()[:32].ljust(32, b"\0")
            s.sendall(auth)
            s.settimeout(12)

            def recv(n):
                buf = b""
                while len(buf) < n:
                    chunk = s.recv(n - len(buf))
                    if not chunk:
                        raise ConnectionError("the printer closed the camera connection")
                    buf += chunk
                return buf

            got = False
            while True:
                try:
                    head = recv(16)
                except socket.timeout:
                    raise ConnectionError("no picture came from port %d" % CAMERA_PORT)
                size = struct.unpack("<I", head[:4])[0]
                if not 0 < size < 8 * 1024 * 1024:
                    raise ConnectionError("the camera sent something that is not a picture")
                frame = recv(size)
                got = True
                self.kind, self.error = "jpeg", ""
                if not self._send(frame):
                    return got
        finally:
            s.close()

    def _rtsp(self, cfg):
        """X1 / H2D / P2S: video over RTSPS, turned into pictures by ffmpeg."""
        import shutil
        import subprocess
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ConnectionError("this printer streams video (port 322); install ffmpeg on the Pi: sudo apt install ffmpeg")
        url = "rtsps://bblp:%s@%s:322/streaming/live/1" % (cfg["code"], cfg["host"])
        self.proc = p = subprocess.Popen(
            [ffmpeg, "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp", "-timeout", "10000000", "-i", url,
             "-an", "-vf", "scale='min(1280,iw)':-2", "-r", "8", "-q:v", "6", "-f", "mjpeg", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        buf, got = b"", False
        try:
            while True:
                chunk = p.stdout.read1(65536) if hasattr(p.stdout, "read1") else p.stdout.read(65536)
                if not chunk:
                    err = p.stderr.read().decode("utf-8", "replace").strip().splitlines()
                    raise ConnectionError("port 322: " + (err[-1][:120] if err else "the video stream ended"))
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                    if start < 0:
                        buf = buf[-1:]
                        break
                    if end < 0:
                        buf = buf[start:]
                        break
                    frame, buf = buf[start:end + 2], buf[end + 2:]
                    got = True
                    self.kind, self.error = "rtsp", ""
                    if not self._send(frame):
                        return got
                if len(buf) > 16 * 1024 * 1024:
                    buf = b""
        finally:
            try:
                p.kill()
            except OSError:
                pass
            p.wait()
            self.proc = None

    def _run(self):
        cfg = dict(self.printer.cfg)
        model = model_name(cfg.get("serial"))
        if self.kind:
            order = [self.kind]
        elif model in VIDEO_MODELS:
            order = ["rtsp"]
        elif model:
            order = ["jpeg"]
        else:
            order = ["jpeg", "rtsp"]
        errors = []
        for kind in order:
            try:
                if (self._jpeg if kind == "jpeg" else self._rtsp)(cfg):
                    errors = []
                    break
            except ssl.SSLError as e:
                errors.append("port %d closed the connection: the camera is off or another app is watching it"
                              % (CAMERA_PORT if kind == "jpeg" else 322) if "EOF" in str(e) else "secure connection failed: %s" % (e.reason or e))
            except (ConnectionError, OSError, struct.error, ValueError) as e:
                errors.append(str(e))
            with self.lock:
                if not self.subs:
                    break
        if errors:
            self.error = "The camera of the %s did not answer (%s). On the printer, switch on LAN Only Liveview " \
                         "(Settings > Network); close the camera in Bambu Studio and Bambu Handy." % (
                             ("Bambu Lab " + model) if model else "printer", "; ".join(errors))
        with self.lock:
            self.thread = None
            subs, self.subs = self.subs, []
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def serve(self, handler):
        if not self.printer.configured():
            return _fail(handler, 404, "The printer is not set up.")
        q = queue.Queue(maxsize=3)
        with self.lock:
            self.subs.append(q)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="printer-camera")
                self.thread.start()
        try:
            try:
                frame = q.get(timeout=15)
            except queue.Empty:
                frame = None
            if frame is None:
                return _fail(handler, 502, self.error or "The printer's camera is not answering. Is LAN Only Liveview on?")
            handler.send_response(200)
            handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=homeosprinter")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True
            while frame is not None:
                handler.wfile.write(b"--homeosprinter\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame) + frame + b"\r\n")
                handler.wfile.flush()
                frame = q.get(timeout=20)
        except (OSError, queue.Empty):
            pass
        finally:
            with self.lock:
                if q in self.subs:
                    self.subs.remove(q)


def _fail(handler, code, message):
    body = json.dumps({"ok": False, "error": message}).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


_printer = None


def get(config_dir):
    global _printer
    if _printer is None:
        _printer = Printer(config_dir)
    return _printer


# --------------------------------------------------------------- checking a connection step by step

def diagnose(host, code, serial=""):
    """Try each step the way HomeOS connects and say which one fails.
    Returns {"ok", "serial", "steps": [{"ok": bool|None, "text"}], "status"}."""
    steps = []
    add = lambda ok, text: steps.append({"ok": ok, "text": text})
    out = {"ok": False, "serial": "", "steps": steps, "status": None}
    host, code = str(host or "").strip(), str(code or "").strip()
    if not host or not code:
        add(False, "Type the printer's IP address and access code.")
        return out
    try:
        socket.create_connection((host, MQTT_PORT), timeout=6).close()
        add(True, "The printer answers at %s." % host)
    except OSError as e:
        add(False, "Nothing answers at %s port %d (%s). Check the IP on the printer, and that this Pi "
                   "is on the same home network as the printer (not a guest network)." % (host, MQTT_PORT, e.strerror or e))
        return out
    serial = re.sub(r"[^0-9A-Za-z]", "", str(serial or "")).upper()
    if not serial:
        try:
            serial = serial_from_certificate(host) or ""
        except (OSError, ssl.SSLError) as e:
            add(False, "The secure connection failed (%s)." % e)
            return out
        if not serial:
            add(False, "Could not read the serial number. Type it (printer: Settings > Device).")
            return out
        add(True, "Serial number %s." % serial)
    out["serial"] = serial
    try:
        m = MQTT(host, code, "homeos-test-%d" % (time.time() * 1000 % 100000))
    except PermissionError:
        add(False, "The printer refused the access code. Type it again from the printer's screen "
                   "(it changes when the refresh arrow next to it is pressed).")
        return out
    except (OSError, ssl.SSLError) as e:
        add(False, "Logging in failed (%s). The printer may already have too many programs connected: "
                   "close Bambu Studio / Handy on other devices and try again." % e)
        return out
    add(True, "Access code accepted.")
    got = None
    try:
        m.subscribe("device/%s/report" % serial)
        m.publish("device/%s/request" % serial, json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))
        m.sock.settimeout(4)
        end = time.time() + 12
        while time.time() < end and got is None:
            try:
                kind, body = m.read()
            except socket.timeout:
                continue
            if kind == 3 and len(body) > 2:
                tl = struct.unpack("!H", body[:2])[0]
                try:
                    msg = json.loads(body[2 + tl:].decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(msg, dict) and isinstance(msg.get("print"), dict):
                    got = msg["print"]
    except (OSError, ssl.SSLError):
        pass
    m.close()
    if got is None:
        add(None, "Logged in, but no status arrived yet. If the serial number above is not the one in "
                  "Settings > Device on the printer, type the right one.")
    else:
        add(True, "Status received: %s." % (got.get("gcode_state") or "ready").lower())
        out["status"] = got.get("gcode_state") or ""
    out["ok"] = True
    return out


# --------------------------------------------------------------- one-time setup from the console
#   python3 /opt/homeos/shell/printer.py 192.168.1.50 ACCESSCODE
# checks every step, says what went wrong, and saves the settings for HomeOS.

def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Connect HomeOS to a Bambu Lab printer")
    ap.add_argument("host")
    ap.add_argument("code")
    ap.add_argument("--serial", default="")
    ap.add_argument("--config", default=os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "homeos"))
    a = ap.parse_args(argv)
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        print("Run this without sudo, as the user HomeOS runs as.")
        return 2
    print("Printer %s" % a.host, flush=True)
    r = diagnose(a.host, a.code, a.serial)
    for st in r["steps"]:
        print("  %s %s" % ({True: "ok  ", False: "FAIL", None: "WARN"}[st["ok"]], st["text"]))
    if not r["ok"]:
        return 1
    p = Printer(a.config)
    s = p.set_config(a.host, a.code, r["serial"])
    if not s.get("ok"):
        print("  FAIL " + s.get("error", "could not save"))
        return 1
    print("  ok   Saved to %s; HomeOS picks it up by itself." % p.path)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
