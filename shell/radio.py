#!/usr/bin/env python3
"""HomeOS Radio: a looping radio station on the Pi.

Everyone who listens hears the same moment of the same track, like a real
radio. One ffmpeg process encodes the playlist to a 128 kbit/s MP3 stream and
its output is shared by every listener. The encoder runs only while someone
listens; a virtual playhead keeps the station's time while nobody does, so a
listener who tunes in later joins in the middle of the right track.

Runs as its own service (homeos-radio.service) for the HomeOS user:

    python3 radio.py --config ~/.config/homeos [--port 8081]

Data lives in <config>/radio/: radio.db (SQLite), uploads/, incoming/, the
admin token (mode 600) and radio.json (whether the stream is shared on the
home network). Python standard library only, plus ffmpeg/ffprobe and, to add
tracks from a link, yt-dlp.

HTTP:
  GET  /api/stream, /api/now-playing               anyone who can reach the port
  GET  /api/tracks                                 admin
  POST /api/tracks/import   {file, title}          admin: a file HomeOS put in incoming/
  POST /api/tracks/from-url {url, title}           admin: yt-dlp
  PATCH /api/tracks/reorder {order}                admin
  PATCH /api/tracks/<id>    {title?, visible?}     admin
  DELETE /api/tracks/<id>                          admin
  POST /api/tracks/skip, /api/server/restart       admin
  POST /api/server/public   {on}                   admin
Admin means: from this Pi itself (127.0.0.1) and with the bearer token.
"""

import argparse
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8081
BITRATE = 128000                       # bit/s of the stream
BURST = 3 * BITRATE // 8               # bytes a new listener gets at once (3 s), so playback starts quickly
IDLE_STOP = 15                         # seconds without listeners before the encoder stops
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus")
LEGACY_DATA = os.path.expanduser("~/radio-server/data")       # an earlier stand-alone radio server, if any
FALLBACK_DURATION = 180.0


LOG_FILE = None                        # <config>/radio/radio.log, so HomeOS can show what went wrong


def log(*a):
    line = " ".join(str(x) for x in a)
    print("radio:", line, flush=True)
    if LOG_FILE:
        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 256 * 1024:
                os.replace(LOG_FILE, LOG_FILE + ".1")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        except OSError:
            pass


# --------------------------------------------------------------- the playlist

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    filename    TEXT,
    source_url  TEXT,
    status      TEXT NOT NULL DEFAULT 'ready',
    error       TEXT,
    duration    REAL,
    position    INTEGER NOT NULL DEFAULT 0,
    visible     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with self._db() as db:
            db.execute(SCHEMA)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _out(row):
        return {"id": row["id"], "title": row["title"], "filename": row["filename"],
                "sourceUrl": row["source_url"], "status": row["status"], "error": row["error"],
                "duration": row["duration"], "position": row["position"], "visible": bool(row["visible"]),
                "createdAt": row["created_at"]}

    def all(self):
        with self.lock, self._db() as db:
            return [self._out(r) for r in db.execute("SELECT * FROM tracks ORDER BY position, id")]

    def playlist(self):
        return [t for t in self.all() if t["visible"] and t["status"] == "ready" and t["filename"]]

    def get(self, tid):
        with self.lock, self._db() as db:
            r = db.execute("SELECT * FROM tracks WHERE id = ?", (tid,)).fetchone()
            return self._out(r) if r else None

    def add(self, title, filename=None, duration=None, source_url=None, status="ready"):
        with self.lock, self._db() as db:
            pos = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM tracks").fetchone()[0]
            cur = db.execute("INSERT INTO tracks (title, filename, duration, source_url, status, position) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (title, filename, duration, source_url, status, pos))
            return cur.lastrowid

    def update(self, tid, **fields):
        allowed = {"title", "filename", "duration", "status", "error", "visible"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        with self.lock, self._db() as db:
            db.execute("UPDATE tracks SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in fields),
                       (*fields.values(), tid))

    def delete(self, tid):
        with self.lock, self._db() as db:
            db.execute("DELETE FROM tracks WHERE id = ?", (tid,))

    def reorder(self, order):
        with self.lock, self._db() as db:
            for i, tid in enumerate(order):
                db.execute("UPDATE tracks SET position = ? WHERE id = ?", (i, tid))

    def count(self):
        with self.lock, self._db() as db:
            return db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]


def probe_duration(path):
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=30)
        return float(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def audio_signature_ok(path):
    """The first bytes must look like audio of the kind the name says."""
    try:
        with open(path, "rb") as f:
            b = f.read(16)
    except OSError:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        return b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0)
    if ext == ".wav":
        return b[:4] == b"RIFF" and b[8:12] == b"WAVE"
    if ext in (".m4a", ".aac"):
        return b[4:8] == b"ftyp" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xF6) == 0xF0)
    if ext in (".ogg", ".opus"):
        return b[:4] == b"OggS"
    if ext == ".flac":
        return b[:4] == b"fLaC"
    return False


# --------------------------------------------------------------- the station

class Station:
    """The virtual playhead, the encoder and the listeners."""

    def __init__(self, store, uploads, work):
        self.store, self.uploads, self.work = store, uploads, work
        self.lock = threading.Lock()
        self.order = []                 # the playlist in playing order
        self.epoch = time.time()        # when the first track of `order` started
        self.listeners = set()
        self.burst = bytearray()
        self.proc = None
        self.gen = 0                    # bumps whenever the encoder must start over
        self.wake = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="radio-encoder").start()

    # ---- the playhead
    def reload(self, start_index=0):
        tracks = self.store.playlist()
        with self.lock:
            if tracks:
                start_index %= len(tracks)
                tracks = tracks[start_index:] + tracks[:start_index]
            self.order = tracks
            self.epoch = time.time()
        log("playlist: %d track(s)" % len(tracks) + (', starting with "%s"' % tracks[0]["title"] if tracks else ""))
        self.restart()

    def _where(self, at=None):
        """(index in order, seconds into that track) at time `at`."""
        if not self.order:
            return None, 0.0
        durs = [t["duration"] or FALLBACK_DURATION for t in self.order]
        pos = ((at or time.time()) - self.epoch) % sum(durs)
        for i, d in enumerate(durs):
            if pos < d:
                return i, pos
            pos -= d
        return 0, 0.0

    def now_playing(self):
        with self.lock:
            i, into = self._where()
            if i is None:
                return None
            t = self.order[i]
            return {"title": t["title"], "id": t["id"], "index": i, "listeners": len(self.listeners),
                    "position": round(into, 1), "duration": t["duration"] or FALLBACK_DURATION,
                    "next": self.order[(i + 1) % len(self.order)]["title"]}

    def skip(self):
        with self.lock:
            i, _ = self._where()
            if i is None:
                return
            # rotate so the next track starts now
            nxt = (i + 1) % len(self.order)
            self.order = self.order[nxt:] + self.order[:nxt]
            self.epoch = time.time()
        self.restart()

    def restart(self):
        with self.lock:
            self.gen += 1
            p = self.proc
        if p:
            try:
                p.kill()
            except OSError:
                pass
        self.wake.set()

    # ---- listeners
    def listen(self):
        q = queue.Queue(maxsize=256)
        with self.lock:
            if self.burst:
                q.put_nowait(bytes(self.burst))
            self.listeners.add(q)
        self.wake.set()
        return q

    def leave(self, q):
        with self.lock:
            self.listeners.discard(q)

    def _send(self, chunk):
        with self.lock:
            self.burst += chunk
            del self.burst[:-BURST]
            for q in list(self.listeners):
                try:
                    q.put_nowait(chunk)
                except queue.Full:                  # a listener that cannot keep up is let go
                    self.listeners.discard(q)
                    try:
                        q.put_nowait(None)
                    except queue.Full:
                        pass

    # ---- the encoder
    def _concat(self):
        """An ffconcat list from where the playhead is now, round the playlist twice."""
        with self.lock:
            i, into = self._where()
            if i is None:
                return None
            order = self.order[i:] + self.order[:i]
        lines = ["ffconcat version 1.0"]
        for n, t in enumerate(order * 2):
            path = os.path.join(self.uploads, t["filename"])
            lines.append("file '%s'" % path.replace("'", "'\\''"))
            if n == 0 and into > 1:
                lines.append("inpoint %.2f" % into)
        path = os.path.join(self.work, "playlist.ffconcat")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(path + ".tmp", path)
        return path

    def _run(self):
        idle_since = None
        while True:
            self.wake.wait(1.0)
            self.wake.clear()
            with self.lock:
                listening = bool(self.listeners)
                has_tracks = bool(self.order)
            if not listening:
                idle_since = idle_since or time.time()
                continue
            idle_since = None
            if not has_tracks:
                continue
            playlist = self._concat()
            if not playlist:
                continue
            with self.lock:
                gen = self.gen
            started = time.time()
            try:
                p = subprocess.Popen(
                    ["ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-f", "concat", "-safe", "0", "-i", playlist,
                     "-vn", "-acodec", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-f", "mp3", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
            except OSError as e:
                log("cannot start ffmpeg:", e)
                time.sleep(5)
                continue
            with self.lock:
                self.proc = p
            last_listener = time.time()
            while True:
                chunk = p.stdout.read1(16384) if hasattr(p.stdout, "read1") else p.stdout.read(16384)
                if not chunk:
                    break
                self._send(chunk)
                with self.lock:
                    stale = gen != self.gen
                    if self.listeners:
                        last_listener = time.time()
                if stale or time.time() - last_listener > IDLE_STOP:
                    break
            try:
                p.kill()
            except OSError:
                pass
            err = p.stderr.read().decode("utf-8", "replace").strip() if p.stderr else ""
            p.wait()
            p.stdout.close()
            p.stderr.close()
            with self.lock:
                self.proc = None
            if gen == self.gen and time.time() - started < 2:
                log("ffmpeg stopped at once:", err[-300:])
                time.sleep(3)              # a broken file must not spin the processor
            self.wake.set()


# --------------------------------------------------------------- adding tracks from a link

def _public_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Only http(s) links are supported.")
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(p.hostname, None)}
    except OSError:
        raise ValueError("Could not find that site.")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("That address is not on the internet.")


def ytdlp_path():
    for c in (shutil.which("yt-dlp"), os.path.expanduser("~/.local/bin/yt-dlp"), os.path.expanduser("~/bin/yt-dlp"),
              "/home/server/bin/yt-dlp"):
        if c and os.access(c, os.X_OK):
            return c
    return None


class Radio:
    def __init__(self, config_dir):
        self.dir = os.path.join(config_dir, "radio")
        self.uploads = os.path.join(self.dir, "uploads")
        self.incoming = os.path.join(self.dir, "incoming")
        for d in (self.dir, self.uploads, self.incoming):
            os.makedirs(d, exist_ok=True)
        os.chmod(self.dir, 0o700)
        global LOG_FILE
        LOG_FILE = os.path.join(self.dir, "radio.log")
        self.token = self._token()
        self.store = Store(os.path.join(self.dir, "radio.db"))
        try:
            self._migrate()
        except Exception as e:                  # a broken old playlist must not keep the radio off the air
            log("taking over the earlier radio failed: %r" % e)
        self.station = Station(self.store, self.uploads, self.dir)
        self.station.reload()

    def _token(self):
        path = os.path.join(self.dir, "token")
        try:
            with open(path, encoding="utf-8") as f:
                t = f.read().strip()
            if len(t) >= 32:
                return t
        except OSError:
            pass
        t = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(t)
        return t

    def settings(self):
        try:
            with open(os.path.join(self.dir, "radio.json"), encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self, **kw):
        d = self.settings()
        d.update(kw)
        path = os.path.join(self.dir, "radio.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(path + ".tmp", path)

    def _migrate(self):
        """Takes over the playlist and files of the earlier stand-alone radio, once."""
        mark = os.path.join(self.dir, "migrated")
        old_db = os.path.join(LEGACY_DATA, "radio.db")
        if os.path.exists(mark) or self.store.count() or not os.path.exists(old_db):
            return
        moved = 0
        try:
            db = sqlite3.connect("file:%s?mode=ro" % old_db, uri=True)
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM tracks WHERE status = 'ready' ORDER BY position, id").fetchall()
            db.close()
        except sqlite3.Error as e:
            log("could not read the earlier radio's playlist:", e)
            return
        for r in rows:
            name = os.path.basename(r["filename"] or "")
            src = os.path.join(LEGACY_DATA, "uploads", name)
            if not name or not os.path.isfile(src):
                continue
            shutil.copy2(src, os.path.join(self.uploads, name))
            tid = self.store.add(r["title"], name, r["duration"] or probe_duration(src), r["source_url"])
            self.store.update(tid, visible=int(r["visible"]))
            moved += 1
        with open(mark, "w") as f:
            f.write("%d tracks from %s\n" % (moved, LEGACY_DATA))
        if moved:
            self.save_settings(public=True)    # the earlier radio answered on the home network
        log("took over %d track(s) from the earlier radio" % moved)

    # ---- admin actions
    def import_file(self, name, title):
        name = os.path.basename(str(name or ""))
        src = os.path.join(self.incoming, name)
        if not name or not os.path.isfile(src):
            raise ValueError("The uploaded file is not there.")
        ext = os.path.splitext(name)[1].lower()
        if ext not in AUDIO_EXT or not audio_signature_ok(src):
            os.remove(src)
            raise ValueError("That is not an audio file the radio can play.")
        final = "%d-%s%s" % (time.time() * 1000, secrets.token_hex(6), ext)
        os.replace(src, os.path.join(self.uploads, final))
        dur = probe_duration(os.path.join(self.uploads, final))
        tid = self.store.add(str(title or "").strip()[:200] or "Untitled", final, dur)
        self.station.reload()
        return self.store.get(tid)

    def from_url(self, url, title):
        url = str(url or "").strip()
        _public_url(url)
        tool = ytdlp_path()
        if not tool:
            raise ValueError("yt-dlp is not installed on the Pi (sudo apt install yt-dlp).")
        tid = self.store.add(str(title or "").strip()[:200] or "Fetching…", source_url=url, status="pending")
        threading.Thread(target=self._fetch, args=(tid, url, str(title or "").strip(), tool), daemon=True).start()
        return self.store.get(tid)

    def _fetch(self, tid, url, title, tool):
        uid = "%d-%s" % (time.time() * 1000, secrets.token_hex(6))
        try:
            p = subprocess.run([tool, "--no-playlist", "--quiet", "--no-warnings", "-x", "--audio-format", "mp3",
                                "--audio-quality", "5", "--print", "after_move:%(title)s",
                                "-o", os.path.join(self.uploads, uid + ".%(ext)s"), url],
                               capture_output=True, text=True, timeout=15 * 60)
            path = os.path.join(self.uploads, uid + ".mp3")
            if p.returncode != 0 or not os.path.isfile(path):
                raise RuntimeError((p.stderr.strip().splitlines() or ["yt-dlp could not get the audio."])[-1][:300])
            if not self.store.get(tid):          # deleted while it was fetching
                os.remove(path)
                return
            name = title or (p.stdout.strip().splitlines() or ["Untitled"])[-1][:200]
            self.store.update(tid, title=name, filename=uid + ".mp3", duration=probe_duration(path),
                              status="ready", error=None)
            log('added "%s" from %s' % (name, url))
            self.station.reload()
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            for f in os.listdir(self.uploads):
                if f.startswith(uid):
                    try:
                        os.remove(os.path.join(self.uploads, f))
                    except OSError:
                        pass
            self.store.update(tid, status="failed", error=str(e)[:300])
            log("fetching %s failed: %s" % (url, e))

    def delete(self, tid):
        t = self.store.get(tid)
        if not t:
            raise LookupError
        self.store.delete(tid)
        if t["filename"]:
            try:
                os.remove(os.path.join(self.uploads, os.path.basename(t["filename"])))
            except OSError:
                pass
        self.station.reload()

    def patch(self, tid, body):
        t = self.store.get(tid)
        if not t:
            raise LookupError
        fields = {}
        if isinstance(body.get("title"), str) and body["title"].strip():
            fields["title"] = body["title"].strip()[:200]
        if isinstance(body.get("visible"), bool):
            fields["visible"] = int(body["visible"])
        self.store.update(tid, **fields)
        if "visible" in fields:
            self.station.reload()
        elif "title" in fields:
            with self.station.lock:
                for o in self.station.order:
                    if o["id"] == tid:
                        o["title"] = fields["title"]
        return self.store.get(tid)

    def reorder(self, order):
        if not isinstance(order, list) or not order or not all(isinstance(i, int) for i in order):
            raise ValueError("order must be a list of track ids")
        self.store.reorder(order)
        self.station.reload()
        return self.store.all()


# --------------------------------------------------------------- HTTP

def make_handler(radio):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "HomeOS-Radio"

        def log_message(self, *a):
            pass

        def _json(self, code, data):
            body = json.dumps(data).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _admin(self):
            local = self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
            got = self.headers.get("Authorization", "")
            got = got[7:] if got.startswith("Bearer ") else ""
            if local and got and hmac.compare_digest(got.encode(), radio.token.encode()):
                return True
            self._json(401, {"error": "Unauthorized"})
            return False

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n < 0 or n > 64 * 1024:
                    raise ValueError
                d = json.loads(self.rfile.read(n) or b"{}")
                return d if isinstance(d, dict) else {}
            except ValueError:
                return {}

        def _id(self, path):
            m = re.fullmatch(r"/api/tracks/(\d+)", path)
            return int(m.group(1)) if m else None

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/now-playing":
                return self._json(200, {"nowPlaying": radio.station.now_playing()})
            if path == "/api/stream":
                return self._stream()
            if path == "/api/tracks":
                if self._admin():
                    self._json(200, {"tracks": radio.store.all(), "public": bool(radio.settings().get("public")),
                                     "ytdlp": bool(ytdlp_path())})
                return
            self._json(404, {"error": "Not found"})

        def _stream(self):
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("icy-name", "HomeOS Radio")
            self.end_headers()
            q = radio.station.listen()
            try:
                while True:
                    try:
                        chunk = q.get(timeout=20)
                    except queue.Empty:
                        continue            # nothing is playing; keep the line open
                    if chunk is None:
                        break
                    self.wfile.write(chunk)
            except OSError:
                pass
            finally:
                radio.station.leave(q)

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            if not self._admin():
                return
            body = self._body()
            try:
                if path == "/api/tracks/import":
                    return self._json(201, {"track": radio.import_file(body.get("file"), body.get("title"))})
                if path == "/api/tracks/from-url":
                    return self._json(202, {"track": radio.from_url(body.get("url"), body.get("title"))})
                if path == "/api/tracks/skip":
                    radio.station.skip()
                    return self._json(200, {"nowPlaying": radio.station.now_playing()})
                if path == "/api/server/restart":
                    radio.station.reload()
                    return self._json(200, {"ok": True})
                if path == "/api/server/public":
                    radio.save_settings(public=body.get("on") is True)
                    self._json(200, {"ok": True, "public": body.get("on") is True})
                    threading.Thread(target=rebind, args=(radio,), daemon=True).start()
                    return
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            self._json(404, {"error": "Not found"})

        def do_PATCH(self):
            path = urllib.parse.urlsplit(self.path).path
            if not self._admin():
                return
            body = self._body()
            try:
                if path == "/api/tracks/reorder":
                    return self._json(200, {"tracks": radio.reorder(body.get("order"))})
                tid = self._id(path)
                if tid is not None:
                    return self._json(200, {"track": radio.patch(tid, body)})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except LookupError:
                return self._json(404, {"error": "Track not found"})
            self._json(404, {"error": "Not found"})

        def do_DELETE(self):
            path = urllib.parse.urlsplit(self.path).path
            if not self._admin():
                return
            tid = self._id(path)
            if tid is None:
                return self._json(404, {"error": "Not found"})
            try:
                radio.delete(tid)
            except LookupError:
                return self._json(404, {"error": "Track not found"})
            self._json(200, {"ok": True})

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass                    # a listener that hangs up is not an error


_servers = {"current": None, "port": DEFAULT_PORT}


def rebind(radio):
    """(Re)opens the port: everywhere when the stream is shared, else only on this Pi."""
    time.sleep(0.3)
    host = "0.0.0.0" if radio.settings().get("public") else "127.0.0.1"
    old = _servers["current"]
    if old and old.server_address[0] == host:
        return
    if old:
        old.shutdown()
        old.server_close()
    tries = 0
    while True:
        try:
            srv = Server((host, _servers["port"]), make_handler(radio))
            break
        except OSError as e:
            if tries % 10 == 0:
                log("port %d is taken (%s), probably by the old radio server; waiting for it" % (_servers["port"], e.strerror or e))
            tries += 1
            time.sleep(3)
    _servers["current"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("listening on http://%s:%d/" % (host, _servers["port"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="HomeOS Radio")
    ap.add_argument("--config", default=os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "homeos"))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args(argv)
    if not shutil.which("ffmpeg"):
        log("ffmpeg is missing: sudo apt install ffmpeg")
    _servers["port"] = a.port
    global LOG_FILE
    os.makedirs(os.path.join(a.config, "radio"), exist_ok=True)
    LOG_FILE = os.path.join(a.config, "radio", "radio.log")
    try:
        radio = Radio(a.config)
    except Exception:
        import traceback
        log("could not start:\n" + traceback.format_exc())
        raise
    log("started (HomeOS radio, port %d)" % a.port)
    rebind(radio)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    radio.station.restart()


if __name__ == "__main__":
    sys.exit(main())
