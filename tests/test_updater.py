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
            self.helper = f.read()
        with open(os.path.join(ROOT, "bin", "homeos-update-job"), encoding="utf-8") as f:
            self.job = f.read()

    def test_installs_only_from_the_root_copy(self):
        runs = re.findall(r'"\$[A-Z_]+/install\.sh"', self.job)
        self.assertEqual(runs, ['"$CLONE/install.sh"'])
        self.assertIn("CLONE=/var/lib/homeos/src", self.job)
        self.assertIn("chown -R root:root", self.job)
        code = [l for l in self.helper.splitlines() if not l.lstrip().startswith("#") and "echo" not in l]
        self.assertFalse([l for l in code if "install.sh" in l], "the helper itself runs install.sh")

    def test_reads_nothing_from_the_users_folder(self):
        both = self.helper + self.job
        self.assertNotIn("SRC)", both)
        self.assertNotIn("runuser", both)
        self.assertIn("core.hooksPath=/dev/null", self.job)

    def test_no_script_on_the_systemd_command_line(self):
        # systemd-run replaces $NAME in its command line, which broke the inline job in 0.19.0
        cmd = self.helper[self.helper.index("exec systemd-run"):]
        self.assertNotIn("bash -c", cmd)
        self.assertIn("/opt/homeos/bin/homeos-update-job", cmd)
        for line in cmd.splitlines():
            for var in re.findall(r"\$\{?([A-Za-z_]+)", line):
                self.assertIn(var, ("UNIT", "OWNER", "REMOTE", "BRANCH", "FLAGS", "LOG"), line)

    def test_checks_its_inputs(self):
        self.assertIn('stat -c %u "$SOURCE"', self.helper)
        self.assertIn("--web|--web-only|--no-autostart|--lite|--no-chromium", self.helper)

    def test_shell_syntax(self):
        for name in ("homeos-update", "homeos-update-job"):
            subprocess.run(["bash", "-n", os.path.join(ROOT, "bin", name)], check=True)


@unittest.skipUnless(os.geteuid() == 0, "the update job runs as root")
class JobRunTests(unittest.TestCase):
    """Runs the real job against a local repository, with its folders moved into a sandbox."""

    def setUp(self):
        self.box = tempfile.mkdtemp(prefix="homeos-job-")
        self.remote = os.path.join(self.box, "remote")
        os.makedirs(self.remote)
        git("init", "-q", "-b", "main", cwd=self.remote)
        with open(os.path.join(self.remote, "install.sh"), "w") as f:
            f.write('#!/bin/bash\necho "from=$(dirname "$0") user=$HOMEOS_USER args=$*" > "%s/marker"\n' % self.box)
        os.chmod(os.path.join(self.remote, "install.sh"), 0o755)
        git("add", ".", cwd=self.remote)
        git("-c", "commit.gpgsign=false", "commit", "-qm", "v1", cwd=self.remote)
        with open(os.path.join(ROOT, "bin", "homeos-update-job"), encoding="utf-8") as f:
            job = f.read()
        state = os.path.join(self.box, "state")
        job = job.replace("/var/lib/homeos/src", state + "/src").replace("/var/lib/homeos", state) \
                 .replace("/etc/homeos/git-credentials", self.box + "/no-credentials")
        self.job = os.path.join(self.box, "job.sh")
        with open(self.job, "w") as f:
            f.write(job)

    def tearDown(self):
        shutil.rmtree(self.box, ignore_errors=True)

    def run_job(self):
        env = dict(os.environ, HOMEOS_OWNER="someone", HOMEOS_REMOTE=self.remote, HOMEOS_BRANCH="main",
                   HOMEOS_FLAGS="--web")
        p = subprocess.run(["bash", self.job], env=env, capture_output=True, text=True)
        self.assertIn("HOMEOS_UPDATE_EXIT=0", p.stdout, p.stdout + p.stderr)
        with open(os.path.join(self.box, "marker")) as f:
            return f.read()

    def test_first_and_second_update(self):
        out = self.run_job()
        self.assertIn("from=%s/state/src" % self.box, out)
        self.assertIn("user=someone args=--no-packages --web", out)
        git("-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "v2", cwd=self.remote)
        self.run_job()
        head = subprocess.run(["git", "-C", os.path.join(self.box, "state", "src"), "log", "-1", "--format=%s"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, "v2")
        self.assertEqual(os.stat(os.path.join(self.box, "state", "src")).st_uid, 0)


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
