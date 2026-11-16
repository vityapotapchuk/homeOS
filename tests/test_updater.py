"""Updates: what the root helper trusts, and the update check."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from tests import helpers

ROOT = os.path.dirname(helpers.SHELL)


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                            GIT_COMMITTER_EMAIL="t@t"))


class HelperScriptTests(unittest.TestCase):
    """bin/homeos-update runs as root: it must never run code from a folder the user can write to."""

    def setUp(self):
        with open(os.path.join(ROOT, "bin", "homeos-update"), encoding="utf-8") as f:
            self.text = f.read()

    def test_installs_only_from_the_root_copy(self):
        runs = re.findall(r'"\$[A-Z_]+/install\.sh"', self.text)
        self.assertEqual(runs, ['"$CLONE/install.sh"'])
        self.assertIn("CLONE=/var/lib/homeos/src", self.text)
        self.assertIn("chown -R root:root", self.text)

    def test_reads_nothing_from_the_users_folder(self):
        self.assertNotIn("SRC)", self.text)          # the user's folder is not even read
        self.assertNotIn("runuser", self.text)
        self.assertIn("core.hooksPath=/dev/null", self.text)

    def test_checks_its_inputs(self):
        self.assertIn('stat -c %u "$SOURCE"', self.text)
        self.assertIn("--web|--web-only|--no-autostart|--lite|--no-chromium", self.text)

    def test_shell_syntax(self):
        subprocess.run(["bash", "-n", os.path.join(ROOT, "bin", "homeos-update")], check=True)


class CheckTests(unittest.TestCase):
    def setUp(self):
        helpers.temp_home()
        import importlib
        import updater
        self.u = importlib.reload(updater)
        self.tmp = tempfile.mkdtemp(prefix="homeos-upd-")
        self.remote = os.path.join(self.tmp, "remote")
        os.makedirs(self.remote)
        git("init", "-q", "-b", "main", cwd=self.remote)
        git("commit", "-q", "--allow-empty", "-m", "HomeOS 1.0", cwd=self.remote)
        self.first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.remote, capture_output=True,
                                    text=True).stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def source(self, **kw):
        path = os.path.join(self.tmp, "source")
        with open(path, "w") as f:
            for k, v in kw.items():
                f.write("%s=%s\n" % (k, v))
        self.u.SOURCE_FILE = path

    def test_source_needs_remote_and_branch(self):
        self.source(SRC="/x", REMOTE="", BRANCH="main")
        self.assertIsNone(self.u._source())
        self.source(REMOTE="https://example.com/r.git", BRANCH="main; rm -rf /")
        self.assertIsNone(self.u._source())
        self.source(REMOTE="/home/someone/repo", BRANCH="main")
        self.assertIsNone(self.u._source())
        self.source(REMOTE="https://example.com/r.git", BRANCH="main")
        self.assertIsNotNone(self.u._source())

    def test_check_sees_a_new_commit(self):
        real = self.u._source
        self.u._source = lambda: {"REMOTE": self.remote, "BRANCH": "main", "COMMIT": self.first}
        try:
            r = self.u.check()
            self.assertTrue(r["ok"], r)
            self.assertFalse(r["available"])
            git("commit", "-q", "--allow-empty", "-m", "HomeOS 1.1: fixes", cwd=self.remote)
            r = self.u.check()
            self.assertTrue(r["available"])
            self.assertEqual(r["summary"], "HomeOS 1.1: fixes")
        finally:
            self.u._source = real


if __name__ == "__main__":
    unittest.main()
