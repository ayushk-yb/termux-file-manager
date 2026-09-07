"""Disk-footprint tests: log volume and orphaned upload temp files.

An always-on phone must not quietly fill its own storage, so the two paths
that can grow without bound are pinned down here.
"""

import base64
import os
import time
import unittest

from termuxfm import transfer
from termuxfm.fsops import PART_PREFIX
from tests.harness import ServerFixture, Client, ServerTestCase


class AccessLogTests(ServerTestCase):
    """Successful requests must not each cost a line of storage."""

    def setUp(self):
        super().setUp()
        self.lines = []
        self.fx.app.log = self.lines.append

    def test_successful_requests_are_not_logged(self):
        for _ in range(30):
            self.assertEqual(self.client.get("/api/list").status, 200)
            self.assertEqual(self.client.get("/api/job").status, 200)
        self.assertEqual(self.lines, [])

    def test_range_requests_are_not_logged(self):
        # Seeking around one video is thousands of 206s; none may be logged.
        for offset in range(0, 4096, 256):
            result = self.client.get(
                "/api/download?path=downloads/torrents/Show.Name/S01E01.mkv",
                headers={"Range": "bytes=%d-%d" % (offset, offset + 63)})
            self.assertEqual(result.status, 206)
        self.assertEqual(self.lines, [])

    def test_errors_are_always_logged(self):
        anon = Client(self.fx.port)
        anon.get("/api/list")                       # 401
        self.client.get("/api/list?path=../../etc")  # 403
        self.client.get("/api/nope")                 # 404
        self.assertEqual(len(self.lines), 3)
        self.assertTrue(any("401" in line for line in self.lines))
        self.assertTrue(any("403" in line for line in self.lines))
        self.assertTrue(any("404" in line for line in self.lines))

    def test_access_log_can_be_enabled(self):
        self.fx.app.access_log = True
        self.client.get("/api/list")
        self.assertTrue(any("/api/list" in line for line in self.lines))

    def test_login_is_recorded_even_when_quiet(self):
        Client(self.fx.port).login()
        self.assertTrue(any("login ok" in line for line in self.lines))


class PartFileVisibilityTests(ServerTestCase):
    def test_temp_files_are_hidden_from_listings(self):
        with open(self.fx.path(PART_PREFIX + "deadbeef"), "wb") as fh:
            fh.write(b"partial")
        self.assertNotIn(PART_PREFIX + "deadbeef", self.names())

    def test_temp_files_are_hidden_from_search(self):
        with open(self.fx.path("media", PART_PREFIX + "cafe"), "wb") as fh:
            fh.write(b"partial")
        job = self.client.wait_job(self.client.post(
            "/api/search", {"path": "", "query": "termuxfm-part"}))
        self.assertEqual(job["result"]["entries"], [])

    def test_a_successful_upload_leaves_no_temp_file(self):
        name = base64.urlsafe_b64encode(b"clean.bin").decode().rstrip("=")
        result = self.client.request("PUT", "/api/upload?dir=media",
                                     raw_body=b"x" * 4096,
                                     headers={"X-Filename": name})
        self.assertEqual(result.status, 201)
        leftovers = [n for n in os.listdir(self.fx.path("media"))
                     if n.startswith(PART_PREFIX)]
        self.assertEqual(leftovers, [])


class SweepTests(unittest.TestCase):
    """Reclaiming temp files orphaned by Android's low-memory killer."""

    def setUp(self):
        self.fx = ServerFixture({"media": {"tv": {}}, "keep.mkv": b"data"})
        self.addCleanup(self.fx.stop)

    def part(self, *parts, age=0, size=1024):
        path = self.fx.path(*parts)
        with open(path, "wb") as fh:
            fh.write(b"\0" * size)
        if age:
            old = time.time() - age
            os.utime(path, (old, old))
        return path

    def test_removes_old_orphans_recursively(self):
        shallow = self.part(PART_PREFIX + "aaa", age=7 * 3600, size=2048)
        nested = self.part("media", "tv", PART_PREFIX + "bbb",
                           age=48 * 3600, size=4096)
        result = transfer.sweep_partials(self.fx.root)
        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["freed"], 2048 + 4096)
        self.assertFalse(os.path.exists(shallow))
        self.assertFalse(os.path.exists(nested))

    def test_keeps_recent_temp_files(self):
        """A concurrent upload in flight must never be deleted."""
        fresh = self.part(PART_PREFIX + "inflight", age=0)
        result = transfer.sweep_partials(self.fx.root)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(os.path.exists(fresh))

    def test_never_touches_real_files(self):
        self.part("media", "movie.mkv", age=99 * 3600)
        transfer.sweep_partials(self.fx.root)
        self.assertTrue(os.path.exists(self.fx.path("media", "movie.mkv")))
        self.assertTrue(os.path.exists(self.fx.path("keep.mkv")))

    def test_honours_a_custom_age(self):
        self.part(PART_PREFIX + "ccc", age=120)
        self.assertEqual(
            transfer.sweep_partials(self.fx.root, max_age=60)["removed"], 1)

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_does_not_follow_symlinks_out_of_the_tree(self):
        outside = os.path.join(self.fx.tmp, "outside")
        os.makedirs(outside)
        victim = os.path.join(outside, PART_PREFIX + "victim")
        with open(victim, "wb") as fh:
            fh.write(b"not ours")
        old = time.time() - 99 * 3600
        os.utime(victim, (old, old))
        os.symlink(outside, self.fx.path("link"))
        transfer.sweep_partials(self.fx.root)
        self.assertTrue(os.path.exists(victim))

    def test_scan_is_bounded(self):
        result = transfer.sweep_partials(self.fx.root, limit=1)
        self.assertLessEqual(result["scanned"], 1)


if __name__ == "__main__":
    unittest.main()
