"""The Files API must never touch anything outside the home folder."""
import importlib
import os
import shutil
import unittest

from tests import helpers


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.home = helpers.temp_home()
        self.outside = os.path.join(os.path.dirname(self.home), "outside")
        os.mkdir(self.outside)
        import files
        self.files = importlib.reload(files)            # ROOT is read when the module loads
        self.assertEqual(self.files.ROOT, os.path.realpath(self.home))
        os.makedirs(os.path.join(self.home, "docs", "sub"))
        for p in ("docs/a.txt", "docs/sub/b.txt", "keep.txt"):
            with open(os.path.join(self.home, p), "w") as f:
                f.write("x")
        with open(os.path.join(self.outside, "secret.txt"), "w") as f:
            f.write("outside")

    def tearDown(self):
        shutil.rmtree(os.path.dirname(self.home), ignore_errors=True)

    def assertHomeIntact(self):
        for p in ("docs/a.txt", "docs/sub/b.txt", "keep.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.home, p)), p)
        self.assertTrue(os.path.exists(os.path.join(self.outside, "secret.txt")))

    def test_delete_refuses_dot_paths(self):
        for rel in ("", "/", ".", "./", "..", "../", "docs/..", "docs/../..", "docs/.", "./docs",
                    "docs//a.txt", "../" + os.path.basename(self.outside), "docs/sub/../..", "\\..", "..\\"):
            with self.subTest(rel=rel):
                r = self.files.delete(rel)
                self.assertFalse(r["ok"], rel)
                self.assertHomeIntact()

    def test_delete_file_and_folder(self):
        self.assertTrue(self.files.delete("docs/a.txt")["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "docs/a.txt")))
        self.assertTrue(self.files.delete("docs/sub")["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "docs/sub")))
        self.assertTrue(os.path.exists(os.path.join(self.home, "keep.txt")))

    def test_delete_link_not_its_target(self):
        os.symlink(self.outside, os.path.join(self.home, "link-out"))
        self.assertTrue(self.files.delete("link-out")["ok"])
        self.assertFalse(os.path.lexists(os.path.join(self.home, "link-out")))
        self.assertTrue(os.path.exists(os.path.join(self.outside, "secret.txt")))

    def test_delete_through_linked_folder_refused(self):
        os.symlink(self.outside, os.path.join(self.home, "link-out"))
        r = self.files.delete("link-out/secret.txt")
        self.assertFalse(r["ok"])
        self.assertTrue(os.path.exists(os.path.join(self.outside, "secret.txt")))

    def test_other_operations_stay_inside(self):
        self.assertFalse(self.files.listing("..")["ok"])
        self.assertFalse(self.files.rename("", "x")["ok"])
        self.assertFalse(self.files.rename("keep.txt", "../x")["ok"])
        self.assertFalse(self.files.make_folder("..", "x")["ok"])
        self.assertFalse(self.files.make_folder("", "..")["ok"])
        self.assertHomeIntact()


if __name__ == "__main__":
    unittest.main()
