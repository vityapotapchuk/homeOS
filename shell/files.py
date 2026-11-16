"""The Files app: the HomeOS user's home folder.

Every path comes from the browser as a path relative to the home folder and
is resolved here; anything that resolves outside it, symlinks included, is
refused. Files are sent with a sandboxing Content-Security-Policy, so an HTML
or SVG file opened from here cannot run script on the HomeOS page.
"""

import json
import mimetypes
import os
import shutil
import stat
import tempfile
from urllib.parse import parse_qs, quote

ROOT = os.path.realpath(os.path.expanduser("~"))
MAX_UPLOAD = 4 * 1024 ** 3          # 4 GB per file
INLINE = ("image/", "video/", "audio/", "text/plain", "application/pdf")


class Refused(Exception):
    pass


def _resolve(rel, must_exist=True):
    rel = str(rel or "").replace("\\", "/").strip("/")
    if "\0" in rel:
        raise Refused("Bad path.")
    full = os.path.realpath(os.path.join(ROOT, rel))
    if full != ROOT and not full.startswith(ROOT + os.sep):
        raise Refused("That is outside your home folder.")
    if must_exist and not os.path.exists(full):
        raise Refused("That file or folder is gone.")
    return full


def _rel(full):
    return "" if full == ROOT else os.path.relpath(full, ROOT)


def _name_ok(name):
    name = str(name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name or len(name) > 255:
        raise Refused("That name cannot be used.")
    return name


def _free_name(folder, name):
    """name, or 'name (2).ext' and so on when name is taken."""
    base, ext = os.path.splitext(name)
    candidate, n = name, 1
    while os.path.lexists(os.path.join(folder, candidate)):
        n += 1
        candidate = "%s (%d)%s" % (base, n, ext)
    return candidate


def _kind(name, is_dir):
    if is_dir:
        return "folder"
    mime = mimetypes.guess_type(name)[0] or ""
    for prefix, kind in (("image/", "image"), ("video/", "video"), ("audio/", "audio"), ("text/", "text")):
        if mime.startswith(prefix):
            return kind
    if name.lower().endswith((".py", ".sh", ".json", ".md", ".log", ".conf", ".cfg", ".ini", ".yaml", ".yml", ".toml", ".js", ".css", ".env")):
        return "text"
    if mime == "application/pdf":
        return "pdf"
    if name.lower().endswith((".zip", ".tar", ".gz", ".xz", ".7z", ".bz2", ".deb")):
        return "archive"
    return "file"


def listing(rel, hidden=False):
    try:
        folder = _resolve(rel)
        if not os.path.isdir(folder):
            raise Refused("That is not a folder.")
        entries = []
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith(".") and not hidden:
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue            # a broken link
                is_dir = stat.S_ISDIR(st.st_mode)
                entries.append({"name": e.name, "dir": is_dir, "kind": _kind(e.name, is_dir),
                                "size": None if is_dir else st.st_size, "mtime": int(st.st_mtime)})
    except Refused as e:
        return {"ok": False, "error": str(e)}
    except PermissionError:
        return {"ok": False, "error": "You are not allowed to open that folder."}
    except OSError as e:
        return {"ok": False, "error": e.strerror or str(e)}
    entries.sort(key=lambda x: (not x["dir"], x["name"].casefold()))      # folders first, then A to Z
    du = shutil.disk_usage(folder)
    return {"ok": True, "path": _rel(folder), "entries": entries, "free": du.free}


def make_folder(rel, name):
    try:
        parent = _resolve(rel)
        name = _name_ok(name)
        target = os.path.join(parent, name)
        if os.path.lexists(target):
            raise Refused("Something with that name is already there.")
        os.mkdir(target)
    except Refused as e:
        return {"ok": False, "error": str(e)}
    except OSError as e:
        return {"ok": False, "error": e.strerror or str(e)}
    return {"ok": True}


def rename(rel, name):
    try:
        src = _resolve(rel)
        if src == ROOT:
            raise Refused("The home folder cannot be renamed.")
        name = _name_ok(name)
        dst = os.path.join(os.path.dirname(src), name)
        if os.path.lexists(dst):
            raise Refused("Something with that name is already there.")
        os.rename(src, dst)
    except Refused as e:
        return {"ok": False, "error": str(e)}
    except OSError as e:
        return {"ok": False, "error": e.strerror or str(e)}
    return {"ok": True}


def delete(rel):
    try:
        rel = str(rel or "").replace("\\", "/").strip("/")
        parts = rel.split("/")
        # every part must be a real name: no "", "." or "..", so the target is
        # always one entry inside a folder that is itself inside the home folder
        if not rel or any(p in ("", ".", "..") for p in parts):
            raise Refused("Nothing to delete.")
        name = _name_ok(parts[-1])
        parent = _resolve("/".join(parts[:-1]))
        # the link itself is removed, never what it points to
        full = os.path.join(parent, name)
        if os.path.normpath(full) != full or not full.startswith(ROOT + os.sep) or not os.path.lexists(full):
            raise Refused("Nothing to delete.")
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
    except Refused as e:
        return {"ok": False, "error": str(e)}
    except OSError as e:
        return {"ok": False, "error": e.strerror or str(e)}
    return {"ok": True}


def _json(handler, code, data):
    body = json.dumps(data).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def serve(handler, query):
    """GET: sends a file, to show it (inline) or to save it (dl=1)."""
    q = parse_qs(query)
    try:
        full = _resolve(q.get("path", [""])[0])
        if not os.path.isfile(full):
            raise Refused("That is not a file.")
        f = open(full, "rb")
    except Refused as e:
        return _json(handler, 404, {"ok": False, "error": str(e)})
    except OSError as e:
        return _json(handler, 403, {"ok": False, "error": e.strerror or str(e)})
    with f:
        size = os.fstat(f.fileno()).st_size
        name = os.path.basename(full)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if _kind(name, False) == "text":
            mime = "text/plain; charset=utf-8"           # shown as text, never run
        inline = q.get("dl", ["0"])[0] != "1" and mime.startswith(INLINE)
        handler.send_response(200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(size))
        handler.send_header("Content-Disposition", "%s; filename*=UTF-8''%s" % ("inline" if inline else "attachment", quote(name)))
        handler.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'; media-src 'self'")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        try:
            shutil.copyfileobj(f, handler.wfile, 256 * 1024)
        except OSError:
            pass                # the viewer left


def receive(handler, query):
    """POST with the raw file as the body: ?path=<folder>&name=<file name>."""
    q = parse_qs(query)
    try:
        n = int(handler.headers.get("Content-Length", "-1"))
    except ValueError:
        n = -1
    if n < 0 or n > MAX_UPLOAD:
        handler.close_connection = True
        return _json(handler, 413, {"ok": False, "error": "The file is too large (4 GB at most)."})
    tmp = None
    try:
        folder = _resolve(q.get("path", [""])[0])
        if not os.path.isdir(folder):
            raise Refused("That is not a folder.")
        name = _name_ok(q.get("name", [""])[0])
        if shutil.disk_usage(folder).free < n + 64 * 1024 ** 2:
            raise Refused("There is not enough free space.")
        fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=folder)
        with os.fdopen(fd, "wb") as out:
            left = n
            while left > 0:
                chunk = handler.rfile.read(min(left, 256 * 1024))
                if not chunk:
                    raise Refused("The upload was cut off.")
                out.write(chunk)
                left -= len(chunk)
        final = _free_name(folder, name)
        os.chmod(tmp, 0o644)
        os.rename(tmp, os.path.join(folder, final))
        tmp = None
    except Refused as e:
        handler.close_connection = True
        return _json(handler, 400, {"ok": False, "error": str(e)})
    except OSError as e:
        handler.close_connection = True
        return _json(handler, 500, {"ok": False, "error": e.strerror or str(e)})
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return _json(handler, 200, {"ok": True, "name": final})
