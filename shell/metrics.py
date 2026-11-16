"""System monitor: a day of temperature, processor and memory history.

A reading every three seconds is kept in memory for the last hour, so the
Monitor app can follow the Pi live. Once a minute their average joins the
day-long history, which is saved to the settings folder every five minutes. The desktop shell and web access may both be running; a file
lock makes sure only one of them samples, and the other reads the saved file.
"""

import fcntl
import json
import os
import shutil
import subprocess
import threading
import time

FAST = 3                    # seconds between live readings
RECENT = 3600 // FAST       # live readings kept (one hour)
STEP = 60                   # seconds between history samples
KEEP = 24 * 60              # history samples kept (one day)
SERVICES = (
    ("homeos-detect", "Motion detection"),
    ("homeos-radio", "Radio"),
    ("homeos-web", "Web access"),
)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def temperature():
    raw = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    try:
        return round(int(raw) / 1000, 1)
    except ValueError:
        return None


def memory_percent():
    info = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        try:
            info[key] = int(rest.split()[0])
        except (ValueError, IndexError):
            pass
    total, free = info.get("MemTotal"), info.get("MemAvailable")
    if not total or free is None:
        return None
    return round((total - free) * 100 / total, 1)


def _cpu_times():
    parts = _read("/proc/stat").split("\n", 1)[0].split()[1:]
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        return None
    if len(nums) < 4:
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return sum(nums), idle


def services():
    names = [name for name, _ in SERVICES]
    try:
        out = subprocess.run(["systemctl", "show", "-p", "Id,LoadState,ActiveState"] + names,
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = {}
    for block in out.strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        found[fields.get("Id", "").replace(".service", "")] = fields
    result = []
    for name, label in SERVICES:
        f = found.get(name, {})
        if f.get("LoadState") != "loaded":
            state = "not installed"
        else:
            state = f.get("ActiveState", "unknown")
        result.append({"name": name, "label": label, "state": state})
    return result


class History:
    def __init__(self, config_dir):
        self.path = os.path.join(config_dir, "metrics.json")
        self.lock_path = os.path.join(config_dir, "metrics.lock")
        self.lock = threading.Lock()
        self.points = []            # [unix time, temperature, cpu %, memory %], one a minute
        self.recent = []            # the same, one every FAST seconds
        self.sampling = False
        self._lock_file = None
        self._started = False

    def start(self):
        if not self._started:
            self._started = True
            threading.Thread(target=self._run, daemon=True, name="metrics").start()
        return self

    def _load(self):
        try:
            data = json.loads(_read(self.path) or "[]")
            cutoff = time.time() - KEEP * STEP
            return [p for p in data if isinstance(p, list) and len(p) == 4 and p[0] >= cutoff]
        except ValueError:
            return []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.points, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _own(self):
        """True when this process is the one that samples."""
        if self._lock_file:
            return True
        try:
            os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
            f = open(self.lock_path, "w")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self._lock_file = f
        with self.lock:
            self.points = self._load()
            self.sampling = True
        return True

    @staticmethod
    def _mean(samples, col):
        vals = [s[col] for s in samples if s[col] is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def _run(self):
        last = _cpu_times()
        ticks = minutes = 0
        per_minute = STEP // FAST
        while True:
            time.sleep(FAST)
            now = _cpu_times()
            cpu = None
            if last and now and now[0] > last[0]:
                cpu = round(100 * (1 - (now[1] - last[1]) / (now[0] - last[0])), 1)
            last = now
            with self.lock:
                self.recent.append([int(time.time()), temperature(), cpu, memory_percent()])
                del self.recent[:-RECENT]
                batch = self.recent[-per_minute:]
            ticks += 1
            if ticks % per_minute or not self._own():
                continue
            minutes += 1
            with self.lock:
                self.points.append([batch[-1][0]] + [self._mean(batch, c) for c in (1, 2, 3)])
                del self.points[:-KEEP]
                if minutes % 5 == 1:
                    self._save()

    def now(self):
        with self.lock:
            return {"ok": True, "sample": self.recent[-1] if self.recent else None}

    def read(self):
        with self.lock:
            recent = list(self.recent)
            points = list(self.points) if self.sampling else None
        if points is None:
            points = self._load()
        try:
            du = shutil.disk_usage("/")
            disk = {"total": du.total, "used": du.used}
        except OSError:
            disk = None
        try:
            load = [round(x, 2) for x in os.getloadavg()]
        except OSError:
            load = None
        return {"ok": True, "step": STEP, "points": points, "recent": recent, "disk": disk, "load": load,
                "cpus": os.cpu_count(), "services": services()}
