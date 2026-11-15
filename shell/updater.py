"""Updating HomeOS from Settings.

install.sh records where HomeOS was installed from (/opt/homeos/source). When
that is a git checkout, "check" looks for newer commits and "apply" asks the
root helper bin/homeos-update to pull them and run install.sh again.
"""

import os
import subprocess

SOURCE_FILE = "/opt/homeos/source"
LOG = "/var/log/homeos-update.log"
UNIT = "homeos-update"
HELPER = "/opt/homeos/bin/homeos-update"      # root-owned; install.sh lets the user run it with sudo


def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _source():
    info = {}
    try:
        with open(SOURCE_FILE, encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.rstrip("\n").partition("=")
                if sep:
                    info[key] = value
    except OSError:
        return None
    src = info.get("SRC", "")
    if not src or not os.path.isdir(os.path.join(src, ".git")):
        return None
    return info


NO_SOURCE = ("This copy of HomeOS was not installed from a git folder, so it cannot update itself. "
             "Update from the console instead.")


def check():
    info = _source()
    if not info:
        return {"ok": False, "error": NO_SOURCE}
    git = ["git", "-C", info["SRC"]]
    rc, _, err = _run(git + ["fetch", "--quiet"], timeout=45)
    if rc != 0:
        return {"ok": False, "error": "Could not reach the update server. " + err[-200:]}
    rc, head, _ = _run(git + ["rev-parse", "@{u}"])
    if rc != 0:
        return {"ok": False, "error": "The source folder does not follow a remote branch."}
    _, summary, _ = _run(git + ["log", "-1", "--format=%s", "@{u}"])
    _, branch, _ = _run(git + ["rev-parse", "--abbrev-ref", "HEAD"])
    return {"ok": True, "available": head != info.get("COMMIT", ""), "summary": summary, "branch": branch}


def apply():
    info = _source()
    if not info:
        return {"ok": False, "error": NO_SOURCE}
    if status()["running"]:
        return {"ok": True}
    rc, _, err = _run(["sudo", "-n", HELPER], timeout=20)
    if rc != 0:
        return {"ok": False, "error": "Updates from here are not allowed yet. Run  sudo ./install.sh --web  once "
                                      "from the console in the HomeOS folder. " + err[-200:]}
    return {"ok": True}


def status():
    rc, state, _ = _run(["systemctl", "is-active", UNIT], timeout=5)
    running = state in ("active", "activating")
    tail, code = "", None
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            lines = f.read()[-4000:].splitlines()
        for line in lines:
            if line.startswith("HOMEOS_UPDATE_EXIT="):
                try:
                    code = int(line.split("=", 1)[1])
                except ValueError:
                    pass
        tail = "\n".join(x for x in lines if not x.startswith("HOMEOS_UPDATE_EXIT="))[-600:]
    except OSError:
        pass
    return {"ok": True, "running": running, "exit": None if running else code, "log": tail}
