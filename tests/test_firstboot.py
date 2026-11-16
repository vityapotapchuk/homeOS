"""First boot must not call itself finished when a step failed."""
import os
import re
import subprocess
import unittest

from tests import helpers

PATH = os.path.join(os.path.dirname(helpers.SHELL), "image", "firstboot.sh")


class FirstBootTests(unittest.TestCase):
    def setUp(self):
        with open(PATH, encoding="utf-8") as f:
            self.text = f.read()

    def test_syntax(self):
        subprocess.run(["bash", "-n", PATH], check=True)

    def test_failures_are_recorded(self):
        for step in ("install.sh did not finish", "the web password was not saved", "Tailscale did not accept the key"):
            self.assertRegex(self.text, r'\|\| fail "%s' % re.escape(step))

    def test_retries_before_finishing(self):
        retry = self.text.index('if [ -n "$FAILED" ] && [ "$ATTEMPT" -lt 3 ]')
        wipe = self.text.index("# ---- the secrets leave the card")
        first_done = self.text.index("firstboot.done")
        self.assertLess(retry, wipe, "secrets are wiped before a retry is decided")
        self.assertLess(retry, first_done, "marked finished before a retry is decided")
        self.assertIn("exit 1", self.text[retry:wipe])

    def test_installs_from_the_root_copy(self):
        self.assertIn('SRC_DIR=$STATE/src', self.text)
        self.assertIn('chown -R root:root "$SRC_DIR"', self.text)
        self.assertNotIn("$HOME_DIR/homeos-src/install.sh", self.text)


if __name__ == "__main__":
    unittest.main()
