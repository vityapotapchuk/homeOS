"""Shared set-up: the shell/ folder on the import path, and a private home folder."""
import os
import sys
import tempfile

SHELL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shell")
if SHELL not in sys.path:
    sys.path.insert(0, SHELL)


def temp_home():
    """A fresh home folder; HOME points at it so modules that read ~ use it.
    It sits inside a sandbox folder of its own, so even a broken path check that
    reaches the folder above home only finds the sandbox."""
    box = tempfile.mkdtemp(prefix="homeos-test-")
    d = os.path.join(box, "home")
    os.mkdir(d)
    os.environ["HOME"] = d
    return d
