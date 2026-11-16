"""Web sign-in: passwords, sessions, and sessions ending when the password changes."""
import os
import shutil
import tempfile
import time
import unittest

from tests import helpers


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        helpers.temp_home()
        import webshell
        self.w = webshell
        self.cfg_dir = tempfile.mkdtemp(prefix="homeos-cfg-")
        self.set_password("first-password")
        self.app = webshell.WebShell(os.path.join(helpers.SHELL, "ui"), self.cfg_dir, bind="all", port=1)

    def tearDown(self):
        shutil.rmtree(self.cfg_dir, ignore_errors=True)

    def set_password(self, pw, open_=False):
        cfg = self.w.WebConfig(self.cfg_dir)
        cfg.data["password"] = self.w.hash_password(pw)
        cfg.data["no_password"] = open_
        cfg.save()
        st = os.stat(cfg.path)                      # make sure the change is seen even within one tick
        os.utime(cfg.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def test_hash_and_check(self):
        rec = self.w.hash_password("correct horse")
        self.assertEqual(rec["algo"], "scrypt")
        self.assertTrue(self.w.check_password("correct horse", rec))
        self.assertFalse(self.w.check_password("wrong", rec))

    def test_wrong_password_refused(self):
        code, token = self.app.login("nope")
        self.assertEqual(code, 401)
        self.assertIsNone(token)

    def test_session_ends_when_password_changes(self):
        code, token = self.app.login("first-password")
        self.assertEqual(code, 200)
        self.assertIsNotNone(self.app.session(token))
        self.set_password("second-password")
        self.assertIsNone(self.app.session(token), "an old session survived a new password")
        code, token2 = self.app.login("second-password")
        self.assertEqual(code, 200)
        self.assertIsNotNone(self.app.session(token2))

    def test_open_sessions_end_when_password_is_asked_again(self):
        self.set_password("first-password", open_=True)
        code, token = self.app.login("")
        self.assertEqual(code, 200)
        self.assertIsNotNone(self.app.session(token))
        self.set_password("first-password", open_=False)
        self.assertIsNone(self.app.session(token))

    def test_expired_session(self):
        code, token = self.app.login("first-password")
        self.app.sessions[token]["exp"] = time.time() - 1
        self.assertIsNone(self.app.session(token))

    def test_lockout_after_failures(self):
        for _ in range(self.w.LOGIN_MAX_FAILS):
            self.w.time.sleep = lambda s: None          # no waiting in tests
            self.app.login("nope")
        code, _ = self.app.login("first-password")
        self.assertEqual(code, 429)


if __name__ == "__main__":
    unittest.main()
