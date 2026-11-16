"""The Motion switch: the exact sudo command the installer allows, and its answers."""
import os
import re
import subprocess
import unittest
from unittest import mock

from tests import helpers

ROOT = os.path.dirname(helpers.SHELL)


class DetectPowerTests(unittest.TestCase):
    def setUp(self):
        helpers.temp_home()
        import apps
        self.apps = apps

    def run_power(self, on, state="inactive", rc=0):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, rc, "", "")
        with mock.patch.object(self.apps, "detect_service", return_value=state), \
             mock.patch.object(self.apps.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(self.apps.shutil, "which", return_value="/usr/bin/systemctl"):
            return self.apps.detect_power(on), calls

    def test_on_and_off_commands(self):
        r, calls = self.run_power(True)
        self.assertEqual(r, {"ok": True, "on": True})
        self.assertEqual(calls, [["sudo", "-n", "/usr/bin/systemctl", "enable", "--now", "homeos-detect.service"]])
        r, calls = self.run_power(False, state="active")
        self.assertEqual(r, {"ok": True, "on": False})
        self.assertEqual(calls[0][3:], ["disable", "--now", "homeos-detect.service"])

    def test_commands_match_the_sudo_rule(self):
        with open(os.path.join(ROOT, "install.sh"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn('echo "$TARGET_USER ALL=(root) NOPASSWD: $ctl $verb --now homeos-detect.service"', text)
        self.assertIn("for ctl in /usr/bin/systemctl /bin/systemctl", text)
        self.assertIn("for verb in enable disable", text)

    def test_not_installed(self):
        r, calls = self.run_power(True, state="not installed")
        self.assertFalse(r["ok"])
        self.assertEqual(calls, [])

    def test_sudo_refused(self):
        r, _ = self.run_power(True, rc=1)
        self.assertFalse(r["ok"])
        self.assertIn("install.sh", r["error"])

    def test_state_when_switched_off_does_not_wait(self):
        with mock.patch.object(self.apps, "detect_service", return_value="inactive"), \
             mock.patch.object(self.apps, "_port_busy", return_value=False), \
             mock.patch.object(self.apps, "_get_json", side_effect=AssertionError("asked a stopped service")):
            r = self.apps.detect_state("http://127.0.0.1:9")
        self.assertEqual(r["service"], "inactive")
        self.assertFalse(r["ok"])

    def test_another_program_holds_the_port(self):
        # the Motion service is off, but an old detector still answers on its port
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        base = "http://127.0.0.1:%d" % srv.getsockname()[1]
        try:
            for state in ("inactive", "failed"):
                with mock.patch.object(self.apps, "detect_service", return_value=state), \
                     mock.patch.object(self.apps, "_get_json", side_effect=AssertionError("asked the other program")):
                    r = self.apps.detect_state(base)
                self.assertFalse(r["ok"])
                self.assertTrue(r["other"])
                self.assertIn("port 8084", r["error"])
        finally:
            srv.close()
        with mock.patch.object(self.apps, "detect_service", return_value="inactive"):
            self.assertNotIn("other", self.apps.detect_state(base))

    def test_installers_switch_off_the_old_detector(self):
        for name in ("install.sh",):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                text = f.read()
            self.assertIn("esp32cam-detect.service", text, name)
            self.assertIn('systemctl --user -M "$TARGET_USER@" disable --now "$old"', text, name)


class MotionInterfaceTests(unittest.TestCase):
    """Both Motion controls use one switch, so they never disagree."""

    def setUp(self):
        with open(os.path.join(helpers.SHELL, "ui", "app.js"), encoding="utf-8") as f:
            self.js = f.read()

    def test_one_switch(self):
        self.assertIn('$("det-power").addEventListener("click", motionToggle);', self.js)
        self.assertIn('$("d-motion").addEventListener("click", motionToggle);', self.js)
        self.assertEqual(len(re.findall(r'api\("/api/detect/power"', self.js)), 1)


if __name__ == "__main__":
    unittest.main()
