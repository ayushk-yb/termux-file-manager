"""Sandbox and filename tests -- the security core."""

import os
import tempfile
import unittest

from termuxfm.errors import Forbidden, InvalidName, InvalidRequest, NotFound
from termuxfm.safepath import Sandbox, sanitize_name, split_relpath


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="termuxfm-sandbox-")
        self.root = os.path.join(self.tmp, "Server")
        os.makedirs(os.path.join(self.root, "media", "tv"))
        self.sandbox = Sandbox(self.root)

    def test_resolves_inside_root(self):
        self.assertEqual(
            self.sandbox.resolve("media/tv"),
            os.path.realpath(os.path.join(self.root, "media", "tv")),
        )

    def test_resolves_nonexistent_child_for_creation(self):
        target = self.sandbox.resolve("media/tv/New Show")
        self.assertTrue(target.startswith(self.sandbox.root + os.sep))

    def test_traversal_variants_all_rejected(self):
        for attempt in [
            "..", "../", "../../", "../etc", "media/../..",
            "media/tv/../../../..", "./../x", "a/b/../../../c",
            "//..//", "media/./../../root",
        ]:
            with self.subTest(attempt=attempt):
                with self.assertRaises(Forbidden):
                    self.sandbox.resolve(attempt)

    def test_absolute_paths_rejected(self):
        for attempt in ["/", "/etc/passwd", "/data/data/com.termux/files/home",
                        "\\windows"]:
            with self.subTest(attempt=attempt):
                with self.assertRaises(Forbidden):
                    self.sandbox.resolve(attempt)

    def test_nul_byte_rejected(self):
        with self.assertRaises(InvalidRequest):
            self.sandbox.resolve("media/tv\x00/etc")

    def test_dot_segments_are_harmless(self):
        self.assertEqual(self.sandbox.resolve("./media/./tv/"),
                         self.sandbox.resolve("media/tv"))

    def test_empty_path_is_root(self):
        self.assertEqual(self.sandbox.resolve(""), self.sandbox.root)
        self.assertEqual(self.sandbox.resolve(None), self.sandbox.root)

    def test_root_can_be_refused_as_target(self):
        with self.assertRaises(Forbidden):
            self.sandbox.resolve("", allow_root=False)

    def test_must_exist(self):
        with self.assertRaises(NotFound):
            self.sandbox.resolve("nope/at/all", must_exist=True)

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_symlink_escaping_root_is_rejected(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "secret"), "w") as fh:
            fh.write("nope")
        os.symlink(outside, os.path.join(self.root, "escape"))
        with self.assertRaises(Forbidden):
            self.sandbox.resolve("escape")
        with self.assertRaises(Forbidden):
            self.sandbox.resolve("escape/secret")

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_symlink_inside_root_is_allowed(self):
        os.symlink(os.path.join(self.root, "media"),
                   os.path.join(self.root, "shortcut"))
        self.assertEqual(self.sandbox.resolve("shortcut"),
                         self.sandbox.resolve("media"))

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_symlink_to_filesystem_root_is_rejected(self):
        os.symlink("/", os.path.join(self.root, "slash"))
        with self.assertRaises(Forbidden):
            self.sandbox.resolve("slash/etc/passwd")

    def test_sibling_prefix_cannot_masquerade(self):
        # "Server-evil" shares a textual prefix with "Server" but is not inside.
        sibling = os.path.join(self.tmp, "Server-evil")
        os.makedirs(sibling)
        self.assertFalse(self.sandbox.contains(sibling))
        self.assertFalse(self.sandbox.contains(self.sandbox.root + "-evil"))

    def test_relpath_roundtrip(self):
        absolute = self.sandbox.resolve("media/tv")
        self.assertEqual(self.sandbox.relpath(absolute), "media/tv")
        self.assertEqual(self.sandbox.relpath(self.sandbox.root), "")

    def test_relpath_refuses_outside(self):
        with self.assertRaises(Forbidden):
            self.sandbox.relpath("/etc")

    def test_is_descendant(self):
        self.assertTrue(self.sandbox.is_descendant("/a/b", "/a/b/c"))
        self.assertTrue(self.sandbox.is_descendant("/a/b", "/a/b"))
        self.assertFalse(self.sandbox.is_descendant("/a/b", "/a/bc"))

    def test_resolve_parent_validates_name(self):
        with self.assertRaises(InvalidName):
            self.sandbox.resolve_parent("media", "../escape")
        parent, clean, child = self.sandbox.resolve_parent("media", "Season 01")
        self.assertEqual(clean, "Season 01")
        self.assertTrue(child.startswith(parent + os.sep))


class SplitRelpathTests(unittest.TestCase):
    def test_drops_empty_and_dot(self):
        self.assertEqual(split_relpath("a//b/./c"), ["a", "b", "c"])

    def test_rejects_parent(self):
        with self.assertRaises(Forbidden):
            split_relpath("a/../b")

    def test_rejects_non_text(self):
        with self.assertRaises(InvalidRequest):
            split_relpath(42)


class SanitizeNameTests(unittest.TestCase):
    def test_accepts_normal_names(self):
        for name in ["Season 01", "S01E01.mkv", "Show Name (2019)",
                     "a.b.c.tar.gz", "-dash", "_under", "#hash", "100%"]:
            with self.subTest(name=name):
                self.assertEqual(sanitize_name(name), name)

    def test_accepts_unicode(self):
        for name in ["Пример.mkv", "日本語.mkv", "emoji 🎬.mkv", "café.txt"]:
            with self.subTest(name=name):
                self.assertEqual(sanitize_name(name), name)

    def test_normalises_to_nfc(self):
        # macOS hands over NFD; Android's storage stores NFC.
        self.assertEqual(sanitize_name("café.txt"), "café.txt")

    def test_rejects_separators_and_traversal(self):
        for name in ["a/b", "../x", "..", ".", "", "/", "a\\b"]:
            with self.subTest(name=name):
                with self.assertRaises(InvalidName):
                    sanitize_name(name)

    def test_rejects_android_reserved_characters(self):
        for char in '"*:<>?|\\':
            with self.subTest(char=char):
                with self.assertRaises(InvalidName):
                    sanitize_name("bad%schar" % char)

    def test_rejects_control_characters(self):
        for name in ["bell\x07", "nul\x00", "nl\n", "tab\t", "del\x7f"]:
            with self.subTest(name=name):
                with self.assertRaises(InvalidName):
                    sanitize_name(name)

    def test_rejects_trailing_dot_or_space(self):
        for name in ["trailing.", "trailing ", " leading", "dots.."]:
            with self.subTest(name=name):
                with self.assertRaises(InvalidName):
                    sanitize_name(name)

    def test_length_cap_is_measured_in_bytes(self):
        self.assertEqual(len(sanitize_name("a" * 255)), 255)
        with self.assertRaises(InvalidName):
            sanitize_name("a" * 256)
        # 100 four-byte emoji = 400 bytes, over the cap despite being 100 chars.
        with self.assertRaises(InvalidName):
            sanitize_name("🎬" * 100)


if __name__ == "__main__":
    unittest.main()
