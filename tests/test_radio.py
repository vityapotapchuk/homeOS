"""HomeOS Radio: playlist, playhead, admin rules, taking over the old radio, and the stream."""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests import helpers


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def tone(path, seconds=4, freq=440):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=%d:duration=%d" % (freq, seconds),
                    "-ac", "2", "-b:a", "128k", path], check=True)


class RadioTests(unittest.TestCase):
    def setUp(self):
        self.home = helpers.temp_home()
        import importlib
        import radio
        self.r = importlib.reload(radio)
        self.r.LEGACY_DATA = os.path.join(self.home, "old-radio", "data")
        self.cfg = os.path.join(self.home, ".config", "homeos")

    def tearDown(self):
        srv = self.r._servers.get("current")
        if srv:
            srv.shutdown()
            srv.server_close()
            self.r._servers["current"] = None
        shutil.rmtree(os.path.dirname(self.home), ignore_errors=True)

    # ---- pieces
    def test_signatures(self):
        d = self.home
        cases = {"a.mp3": b"ID3\x04" + b"\0" * 12, "b.mp3": b"\xff\xfb\x90" + b"\0" * 13, "c.wav": b"RIFF\0\0\0\0WAVEfmt ",
                 "d.ogg": b"OggS" + b"\0" * 12, "e.flac": b"fLaC" + b"\0" * 12, "f.m4a": b"\0\0\0\x20ftypM4A " + b"\0" * 4}
        for name, data in cases.items():
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
            self.assertTrue(self.r.audio_signature_ok(os.path.join(d, name)), name)
        with open(os.path.join(d, "x.mp3"), "wb") as f:
            f.write(b"<html>not audio</html>")
        self.assertFalse(self.r.audio_signature_ok(os.path.join(d, "x.mp3")))

    def test_links_must_be_public(self):
        for url in ("ftp://example.com/a", "http://127.0.0.1/a", "http://localhost/a", "http://10.1.2.3/a",
                    "http://192.168.1.5/a", "http://[::1]/a", "file:///etc/passwd"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    self.r._public_url(url)

    def test_playhead(self):
        store = self.r.Store(os.path.join(self.home, "t.db"))
        for i, d in enumerate((10, 20, 30)):
            store.add("T%d" % i, "f%d.mp3" % i, d)
        st = self.r.Station(store, self.home, self.home)
        st.reload()
        st.epoch = time.time() - 15            # 15 s in: the second track, 5 s into it
        np = st.now_playing()
        self.assertEqual((np["title"], round(np["position"])), ("T1", 5))
        self.assertEqual(np["next"], "T2")
        st.epoch = time.time() - 65            # one full round (60 s) and 5 s
        self.assertEqual(st.now_playing()["title"], "T0")
        st.skip()
        self.assertEqual(st.now_playing()["title"], "T1")
        store.update(2, visible=0)
        st.reload()
        self.assertEqual([t["title"] for t in st.order], ["T0", "T2"])

    def test_takes_over_the_old_radio_once(self):
        old = self.r.LEGACY_DATA
        os.makedirs(os.path.join(old, "uploads"))
        db = sqlite3.connect(os.path.join(old, "radio.db"))
        db.execute(self.r.SCHEMA)
        for i, (title, vis, status) in enumerate((("First", 1, "ready"), ("Hidden", 0, "ready"), ("Broken", 1, "failed"))):
            fn = "f%d.mp3" % i
            with open(os.path.join(old, "uploads", fn), "wb") as f:
                f.write(b"ID3" + b"\0" * 100)
            db.execute("INSERT INTO tracks (title, filename, status, duration, position, visible) VALUES (?,?,?,?,?,?)",
                       (title, fn, status, 100 + i, i, vis))
        db.commit()
        db.close()
        radio = self.r.Radio(self.cfg)
        tracks = radio.store.all()
        self.assertEqual([(t["title"], t["visible"]) for t in tracks], [("First", True), ("Hidden", False)])
        for t in tracks:
            self.assertTrue(os.path.isfile(os.path.join(radio.uploads, t["filename"])))
        self.assertTrue(radio.settings().get("public"), "the old radio answered on the network; so does this one")
        self.assertTrue(os.path.isfile(os.path.join(old, "uploads", "f0.mp3")), "the old files stay")
        radio2 = self.r.Radio(self.cfg)
        self.assertEqual(len(radio2.store.all()), 2, "taken over twice")

    # ---- the HTTP service
    def start(self):
        port = free_port()
        self.r._servers["port"] = port
        radio = self.r.Radio(self.cfg)
        self.r.rebind(radio)
        return radio, "http://127.0.0.1:%d" % port

    def call(self, base, method, path, body=None, token=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + token} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_admin_needs_the_token(self):
        radio, base = self.start()
        self.assertEqual(self.call(base, "GET", "/api/now-playing")[0], 200)
        for method, path in (("GET", "/api/tracks"), ("POST", "/api/tracks/skip"), ("POST", "/api/server/public"),
                             ("DELETE", "/api/tracks/1"), ("PATCH", "/api/tracks/reorder")):
            self.assertEqual(self.call(base, method, path, {} if method != "GET" else None)[0], 401, path)
            self.assertEqual(self.call(base, method, path, {} if method != "GET" else None, "wrong")[0], 401, path)
        self.assertEqual(self.call(base, "GET", "/api/tracks", token=radio.token)[0], 200)
        self.assertEqual(os.stat(os.path.join(radio.dir, "token")).st_mode & 0o777, 0o600)

    def test_import_only_from_incoming(self):
        radio, base = self.start()
        code, out = self.call(base, "POST", "/api/tracks/import", {"file": "../token", "title": "x"}, radio.token)
        self.assertEqual(code, 400)
        self.assertTrue(os.path.exists(os.path.join(radio.dir, "token")))

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_upload_play_skip_reorder_delete(self):
        radio, base = self.start()
        for n, f in enumerate((440, 660)):
            tone(os.path.join(radio.incoming, "t%d.mp3" % n), 4, f)
            code, out = self.call(base, "POST", "/api/tracks/import", {"file": "t%d.mp3" % n, "title": "Tone %d" % f}, radio.token)
            self.assertEqual(code, 201, out)
            self.assertAlmostEqual(out["track"]["duration"], 4, delta=0.3)
        got = bytearray()

        def listen():
            with urllib.request.urlopen(base + "/api/stream", timeout=10) as r:
                end = time.time() + 3
                while time.time() < end:
                    got.extend(r.read(4096))
        t = threading.Thread(target=listen)
        t.start()
        time.sleep(1)
        self.assertEqual(self.call(base, "GET", "/api/now-playing")[1]["nowPlaying"]["listeners"], 1)
        t.join()
        self.assertGreater(len(got), 128000 // 8 * 2, "less than two seconds of audio in three")
        self.assertEqual(self.call(base, "POST", "/api/tracks/skip", {}, radio.token)[1]["nowPlaying"]["title"], "Tone 660")
        ids = [x["id"] for x in radio.store.all()]
        self.call(base, "PATCH", "/api/tracks/reorder", {"order": ids[::-1]}, radio.token)
        self.assertEqual([x["title"] for x in radio.store.all()], ["Tone 660", "Tone 440"])
        self.assertEqual(self.call(base, "DELETE", "/api/tracks/%d" % ids[0], token=radio.token)[0], 200)
        self.assertEqual(len(os.listdir(radio.uploads)), 1)


if __name__ == "__main__":
    unittest.main()
