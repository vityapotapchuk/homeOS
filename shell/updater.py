"""Updating HomeOS from Settings.

install.sh records where HomeOS came from in /opt/homeos/source: the git remote
and branch, and the commit that is installed. "check" asks that remote for the
newest commit (as the HomeOS user, into a cache folder of its own); "apply" asks
the root helper bin/homeos-update to fetch it as root into /var/lib/homeos/src
and run install.sh from there.
"""

import os
import re
import subprocess

SOURCE_FILE = "/opt/homeos/source"
LOG = "/var/log/homeos-update.log"
UNIT = "homeos-update"
HELPER = "/opt/homeos/bin/homeos-update"      # root-owned; install.sh lets the user run it with sudo
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "homeos", "update-check.git")


def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
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
    if not re.match(r"^(https://|ssh://|git@)", info.get("REMOTE", "")):
        return None
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", info.get("BRANCH", "")):
        return None
    return info


NO_SOURCE = ("This copy of HomeOS does not know where its updates come from. Run  sudo ./install.sh  once "
             "from the console in a git copy of HomeOS.")


def check():
    info = _source()
    if not info:
        return {"ok": False, "error": NO_SOURCE}
    if not os.path.isdir(CACHE):
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        rc, _, err = _run(["git", "init", "--quiet", "--bare", CACHE])
        if rc != 0:
            return {"ok": False, "error": "Could not prepare the update check. " + err[-200:]}
    git = ["git", "-C", CACHE]
    rc, _, err = _run(git + ["fetch", "--quiet", "--depth", "1", info["REMOTE"], "refs/heads/" + info["BRANCH"]], timeout=60)
    if rc != 0:
        return {"ok": False, "error": "Could not reach the update server. " + err[-200:]}
    rc, head, _ = _run(git + ["rev-parse", "FETCH_HEAD"])
    if rc != 0:
        return {"ok": False, "error": "The update server sent nothing for " + info["BRANCH"] + "."}
    _, summary, _ = _run(git + ["log", "-1", "--format=%s", "FETCH_HEAD"])
    return {"ok": True, "available": head != info.get("COMMIT", ""), "summary": summary, "branch": info["BRANCH"]}


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
