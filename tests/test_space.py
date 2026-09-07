"""Free-space reporting and pre-flight space checks."""

import base64
import os
import unittest
from unittest import mock

from termuxfm import fsops
from termuxfm.errors import ApiError
from tests.harness import ServerTestCase


class DiskUsageTests(ServerTestCase):
    def test_reports_plausible_figures(self):
        usage = fsops.disk_usage(self.fx.root)
        self.assertIsNotNone(usage)
        self.assertGreater(usage["total"], 0)
        self.assertGreaterEqual(usage["free"], 0)
        self.assertLessEqual(usage["free"], usage["total"])
        self.assertEqual(usage["used"], usage["total"] - usage["free"])
        self.assertGreaterEqual(usage["percent"], 0)
        self.assertLessEqual(usage["percent"], 100)

    def test_returns_none_when_the_platform_refuses(self):
        # A FUSE mount that will not report must not break the listing.
        with mock.patch("termuxfm.fsops.os.statvfs",
                        side_effect=OSError("not supported")):
            self.assertIsNone(fsops.disk_usage(self.fx.root))

    def test_listing_includes_disk_info(self):
        payload = self.client.get("/api/list").json()
        self.assertIn("disk", payload)
        self.assertGreater(payload["disk"]["total"], 0)

    def test_listing_survives_missing_disk_info(self):
        with mock.patch("termuxfm.fsops.os.statvfs", side_effect=OSError):
            payload = self.client.get("/api/list").json()
        self.assertIsNone(payload["disk"])
        self.assertEqual(len(payload["entries"]), 3)

    def test_me_includes_disk_info(self):
        self.assertIn("disk", self.client.get("/api/me").json())


def fake_statvfs(free_bytes, total_bytes=100 * 1024 ** 3):
    """A statvfs result with a chosen amount of free space."""
    block = 4096
    result = mock.Mock()
    result.f_frsize = block
    result.f_bsize = block
    result.f_blocks = total_bytes // block
    result.f_bavail = free_bytes // block
    return result


class CheckSpaceTests(unittest.TestCase):
    def test_passes_when_it_fits(self):
        with mock.patch("termuxfm.fsops.os.statvfs",
                        return_value=fake_statvfs(10 * 1024 ** 3)):
            self.assertIsNotNone(fsops.check_space("/x", 1024 ** 3))

    def test_raises_507_when_it_does_not_fit(self):
        with mock.patch("termuxfm.fsops.os.statvfs",
                        return_value=fake_statvfs(1024 ** 3)):
            with self.assertRaises(ApiError) as caught:
                fsops.check_space("/x", 8 * 1024 ** 3, what="copy")
        self.assertEqual(caught.exception.status, 507)
        self.assertEqual(caught.exception.code, "no_space")
        self.assertIn("8.0 GB", caught.exception.message)
        self.assertIn("copy", caught.exception.message)

    def test_reserves_a_margin(self):
        """A request that exactly fills the volume is still refused."""
        free = 100 * 1024 ** 2          # 100 MB free, 64 MB margin
        with mock.patch("termuxfm.fsops.os.statvfs",
                        return_value=fake_statvfs(free)):
            with self.assertRaises(ApiError):
                fsops.check_space("/x", free)
            self.assertIsNotNone(fsops.check_space("/x", 20 * 1024 ** 2))

    def test_allows_the_write_when_space_is_unknown(self):
        with mock.patch("termuxfm.fsops.os.statvfs", side_effect=OSError):
            self.assertIsNone(fsops.check_space("/x", 10 ** 15))

    def test_format_bytes(self):
        self.assertEqual(fsops.format_bytes(0), "0 B")
        self.assertEqual(fsops.format_bytes(512), "512 B")
        self.assertEqual(fsops.format_bytes(2048), "2.0 KB")
        self.assertEqual(fsops.format_bytes(5 * 1024 ** 3), "5.0 GB")


class UploadSpaceTests(ServerTestCase):
    def put(self, body, name="big.bin", directory="media"):
        header = base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
        return self.client.request("PUT", "/api/upload?dir=" + directory,
                                   raw_body=body,
                                   headers={"X-Filename": header})

    def test_upload_rejected_before_any_bytes_are_written(self):
        with mock.patch("termuxfm.fsops.os.statvfs",
                        return_value=fake_statvfs(1024)):
            result = self.put(b"x" * 8192)
        self.assertEqual(result.status, 507)
        self.assertEqual(result.json()["error"], "no_space")
        self.assertIn("upload", result.json()["message"])
        # Nothing was created, not even a temp file.
        self.assertEqual(sorted(os.listdir(self.fx.path("media"))),
                         ["movies", "tv"])

    def test_upload_allowed_when_there_is_room(self):
        self.assertEqual(self.put(b"x" * 8192).status, 201)


class CopySpaceTests(ServerTestCase):
    def test_copy_rejected_after_measuring(self):
        with mock.patch("termuxfm.fsops.os.statvfs",
                        return_value=fake_statvfs(4096)):
            job = self.client.wait_job(self.client.post(
                "/api/copy", {"paths": ["downloads/torrents/Show.Name"],
                              "dest": "media/tv"}))
        self.assertEqual(job["state"], "error")
        self.assertIn("Not enough space", job["message"])
        # The destination is untouched: no partial tree left behind.
        self.assertEqual(os.listdir(self.fx.path("media", "tv")), [])

    def test_copy_allowed_when_there_is_room(self):
        job = self.client.wait_job(self.client.post(
            "/api/copy", {"paths": ["downloads/torrents/Show.Name"],
                          "dest": "media/tv"}))
        self.assertEqual(job["state"], "done")
        self.assertEqual(sorted(self.names("media/tv/Show.Name")),
                         ["S01E01.mkv", "S01E02.mkv"])


if __name__ == "__main__":
    unittest.main()
