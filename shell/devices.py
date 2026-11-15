"""Battery and Bluetooth for HomeOS.

Battery: a Raspberry Pi 5 has none of its own. A UPS board or battery HAT
whose driver registers with the kernel shows up in /sys/class/power_supply;
that is what is read here. With no such board, battery() returns None and
the indicator stays hidden.

Bluetooth: BlueZ through bluetoothctl, which the desktop user may use.
"""

import glob
import os
import re
import subprocess
import threading
import time

MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# --------------------------------------------------------------- battery

def battery():
    for base in sorted(glob.glob("/sys/class/power_supply/*")):
        if _read(os.path.join(base, "type")) not in ("Battery", "UPS"):
            continue
        if _read(os.path.join(base, "scope")) == "Device":
            continue            # the battery of a connected mouse or keyboard, not of the Pi
        try:
            level = max(0, min(100, int(_read(os.path.join(base, "capacity")))))
        except ValueError:
            continue
        status = _read(os.path.join(base, "status")) or "Unknown"
        return {"level": level, "charging": status in ("Charging", "Full"), "status": status}
    return None


# --------------------------------------------------------------- bluetooth

def _ctl(*args, timeout=10):
    try:
        p = subprocess.run(["bluetoothctl"] + list(args), capture_output=True, text=True, timeout=timeout + 5)
        return p.returncode, p.stdout + p.stderr
    except FileNotFoundError:
        return 127, "bluetoothctl is not installed"
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def _devices(kind=None):
    rc, out = _ctl("devices", *([kind] if kind else []))
    found = {}
    for line in out.splitlines():
        m = re.match(r"^Device ([0-9A-F:]{17}) (.*)$", line.strip())
        if m:
            found[m.group(1)] = m.group(2).strip()
    return found


_cache = {"at": 0.0, "on": None}
_lock = threading.Lock()


def powered(cached=True):
    """True/False, or None when there is no Bluetooth adapter."""
    with _lock:
        if cached and time.time() - _cache["at"] < 10:
            return _cache["on"]
    rc, out = _ctl("show", timeout=4)
    if rc != 0 or "Controller" not in out:
        on = None
    else:
        on = bool(re.search(r"^\s*Powered: yes", out, re.M))
    with _lock:
        _cache.update(at=time.time(), on=on)
    return on


def bt_state():
    on = powered(cached=False)
    if on is None:
        return {"ok": True, "available": False}
    paired, connected = _devices("Paired"), _devices("Connected")
    known = _devices()
    devices = []
    for mac, name in known.items():
        if name.replace("-", ":") == mac:
            name = "Unnamed device"
        devices.append({"mac": mac, "name": name, "paired": mac in paired, "connected": mac in connected})
    devices.sort(key=lambda d: (d["name"].casefold(), d["mac"]))
    return {"ok": True, "available": True, "on": on, "devices": devices}


def bt_power(on):
    if on:
        subprocess.run(["rfkill", "unblock", "bluetooth"], capture_output=True, timeout=5)
    rc, out = _ctl("power", "on" if on else "off")
    with _lock:
        _cache["at"] = 0.0
    if rc != 0 or "succeeded" not in out:
        return {"ok": False, "error": "Could not switch Bluetooth " + ("on" if on else "off") + ". " + out.strip()[-160:]}
    return bt_state()


def bt_scan():
    if not powered(cached=False):
        return {"ok": False, "error": "Turn Bluetooth on first."}
    _ctl("--timeout", "8", "scan", "on", timeout=12)
    return bt_state()


def bt_device(action, mac):
    mac = str(mac or "").upper()
    if not MAC.match(mac):
        return {"ok": False, "error": "Unknown device."}
    if action == "connect":
        if mac not in _devices("Paired"):
            rc, out = _ctl("--timeout", "25", "pair", mac, timeout=30)
            if "Pairing successful" not in out and "AlreadyExists" not in out:
                return {"ok": False, "error": "Pairing failed. Put the device in pairing mode and try again."}
            _ctl("trust", mac)
        rc, out = _ctl("--timeout", "20", "connect", mac, timeout=25)
        if "Connection successful" not in out:
            return {"ok": False, "error": "Could not connect. Is the device on and nearby?"}
    elif action == "disconnect":
        _ctl("disconnect", mac)
    elif action == "forget":
        _ctl("remove", mac)
    else:
        return {"ok": False, "error": "Unknown action."}
    return bt_state()
