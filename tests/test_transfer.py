"""Upload, download, Range and ZIP tests -- the streaming paths."""

import base64
import io
import os
import unittest
import zipfile
from unittest import mock

from termuxfm import transfer
from termuxfm.errors import InvalidRequest, PayloadTooLarge, RangeNotSatisfiable
from tests.harness import ServerTestCase


def b64(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class RangeParsingTests(unittest.TestCase):
    def test_full_and_partial(self):
        self.assertIsNone(transfer.parse_range(None, 1000))
        self.assertIsNone(transfer.parse_range("", 1000))
        self.assertEqual(transfer.parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(transfer.parse_range("bytes=500-", 1000), (500, 999))
        self.assertEqual(transfer.parse_range("bytes=-100", 1000), (900, 999))
        self.assertEqual(transfer.parse_range("bytes=0-99999", 1000), (0, 999))

    def test_first_range_of_a_multi_range_request(self):
        self.assertEqual(transfer.parse_range("bytes=0-9,20-29", 1000), (0, 9))

    def test_invalid_ranges(self):
        for header in ["bytes=2000-", "bytes=900-800", "bytes=abc-def",
                       "bytes=-0", "bytes=x"]:
            with self.subTest(header=header):
                with self.assertRaises(RangeNotSatisfiable):
                    transfer.parse_range(header, 1000)

    def test_range_on_empty_file(self):
        with self.assertRaises(RangeNotSatisfiable):
            transfer.parse_range("bytes=0-0", 0)

    def test_non_bytes_unit_is_ignored(self):
        self.assertIsNone(transfer.parse_range("items=0-5", 1000))


class HeaderDecodingTests(unittest.TestCase):
    def test_roundtrip_unicode(self):
        for name in ["plain.mkv", "Show — S01E01.mkv", "日本語.mkv", "emoji 🎬.txt",
                     "has#hash?and%percent.txt"]:
            with self.subTest(name=name):
                self.assertEqual(transfer.decode_header_name(b64(name)), name)

    def test_missing_and_malformed(self):
        with self.assertRaises(InvalidRequest):
            transfer.decode_header_name(None)
        with self.assertRaises(InvalidRequest):
            transfer.decode_header_name("!!!not base64!!!")


class UploadTests(ServerTestCase):
    def put(self, data, name="upload.bin", directory="", rel=None,
            conflict=None, headers=None):
        head = {"X-Filename": b64(name)}
        if rel:
            head["X-Rel-Path"] = b64(rel)
        head.update(headers or {})
        url = "/api/upload?dir=" + directory
        if conflict:
            url += "&conflict=" + conflict
        return self.client.request("PUT", url, raw_body=data, headers=head)

    def test_upload_file(self):
        result = self.put(b"x" * 5000, "movie.mkv", "media/movies")
        self.assertEqual(result.status, 201)
        self.assertEqual(result.json()["size"], 5000)
        self.assertEqual(
            os.path.getsize(self.fx.path("media", "movies", "movie.mkv")), 5000)

    def test_upload_multiple_files_into_one_folder(self):
        for index in range(3):
            self.assertEqual(
                self.put(b"data", "file%d.txt" % index, "media").status, 201)
        self.assertEqual(sorted(self.names("media")),
                         ["file0.txt", "file1.txt", "file2.txt", "movies", "tv"])

    def test_upload_preserves_unicode_name(self):
        name = "Show — S01E03 🎬.mkv"
        result = self.put(b"unicode", name, "media/tv")
        self.assertEqual(result.status, 201)
        self.assertEqual(result.json()["name"], name)
        self.assertIn(name, self.names("media/tv"))

    def test_folder_upload_creates_nested_directories(self):
        result = self.put(b"episode", rel="Show Name/Season 02/S02E01.mkv",
                          directory="media/tv")
        self.assertEqual(result.status, 201)
        self.assertEqual(result.json()["path"],
                         "media/tv/Show Name/Season 02/S02E01.mkv")
        self.assertTrue(os.path.isfile(
            self.fx.path("media", "tv", "Show Name", "Season 02", "S02E01.mkv")))

    def test_folder_upload_rejects_traversal_in_rel_path(self):
        for rel in ["../escape.txt", "a/../../escape.txt", "/abs.txt"]:
            with self.subTest(rel=rel):
                self.assertEqual(self.put(b"x", rel=rel).status, 403)

    def test_folder_upload_rejects_bad_segment(self):
        result = self.put(b"x", rel="bad:folder/file.txt", directory="media")
        self.assertEqual(result.status, 422)

    def test_default_conflict_mode_renames(self):
        self.put(b"first", "dupe.txt", "media")
        result = self.put(b"second", "dupe.txt", "media")
        self.assertEqual(result.status, 201)
        self.assertEqual(result.json()["name"], "dupe (2).txt")

    def test_conflict_fail_mode(self):
        self.put(b"first", "dupe.txt", "media")
        self.assertEqual(
            self.put(b"second", "dupe.txt", "media", conflict="fail").status, 409)

    def test_conflict_overwrite_mode(self):
        self.put(b"first", "dupe.txt", "media")
        result = self.put(b"second!", "dupe.txt", "media", conflict="overwrite")
        self.assertEqual(result.status, 201)
        with open(self.fx.path("media", "dupe.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"second!")

    def test_upload_rejects_invalid_filename(self):
        for name in ["../escape.txt", "bad:name.txt", "", "."]:
            with self.subTest(name=name):
                self.assertIn(self.put(b"x", name).status, (403, 422))

    def test_upload_outside_root_rejected(self):
        result = self.put(b"x", "evil.txt", directory="../..")
        self.assertEqual(result.status, 403)

    def test_upload_to_missing_folder_is_404(self):
        self.assertEqual(self.put(b"x", "a.txt", "ghost").status, 404)

    def test_upload_into_a_file_is_400(self):
        self.assertEqual(self.put(b"x", "a.txt", "notes.txt").status, 400)

    def test_size_limit_enforced_from_content_length(self):
        self.fx.cfg.max_upload_bytes = 1000
        result = self.put(b"x" * 2000, "big.bin", "media")
        self.assertEqual(result.status, 413)
        self.assertEqual(sorted(os.listdir(self.fx.path("media"))),
                         ["movies", "tv"])

    def test_missing_content_length_rejected(self):
        # Chunked request bodies are refused rather than read unbounded.
        result = self.client.request(
            "PUT", "/api/upload?dir=", raw_body=None,
            headers={"X-Filename": b64("a.txt"),
                     "Transfer-Encoding": "chunked"})
        self.assertEqual(result.status, 400)

    def test_missing_filename_header_rejected(self):
        result = self.client.request("PUT", "/api/upload?dir=", raw_body=b"x")
        self.assertEqual(result.status, 400)

    def test_no_part_file_left_after_a_failed_upload(self):
        self.fx.cfg.max_upload_bytes = 10
        self.put(b"x" * 100, "big.bin", "media")
        leftovers = [n for n in os.listdir(self.fx.path("media"))
                     if "termuxfm-part" in n]
        self.assertEqual(leftovers, [])

    def test_upload_streams_in_chunks_rather_than_one_read(self):
        """A 4 MB body must arrive as many bounded reads, never one big one."""
        sizes = []
        original = transfer.receive_upload

        def spy(stream, target, expected, max_bytes, **kw):
            class Counting:
                def read(self, want):
                    sizes.append(want)
                    return stream.read(want)
            return original(Counting(), target, expected, max_bytes, **kw)

        with mock.patch.object(transfer, "receive_upload", spy):
            with mock.patch("termuxfm.api.transfer.receive_upload", spy):
                result = self.put(b"z" * (4 * 1024 * 1024), "big.bin", "media")
        self.assertEqual(result.status, 201)
        self.assertGreaterEqual(len(sizes), 8)
        self.assertLessEqual(max(sizes), transfer.CHUNK)


class ReceiveUploadUnitTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.mkdtemp()

    def test_truncated_body_is_rejected_and_cleaned_up(self):
        target = os.path.join(self.tmp, "out.bin")
        with self.assertRaises(InvalidRequest):
            transfer.receive_upload(io.BytesIO(b"short"), target, 100, 1 << 30)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_limit_checked_before_reading(self):
        target = os.path.join(self.tmp, "out.bin")
        with self.assertRaises(PayloadTooLarge):
            transfer.receive_upload(io.BytesIO(b"x" * 10), target, 10, 5)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_atomic_replace_leaves_only_the_target(self):
        target = os.path.join(self.tmp, "out.bin")
        written = transfer.receive_upload(io.BytesIO(b"y" * 4096), target,
                                          4096, 1 << 30)
        self.assertEqual(written, 4096)
        self.assertEqual(os.listdir(self.tmp), ["out.bin"])


class DownloadTests(ServerTestCase):
    def test_download_file(self):
        result = self.client.get("/api/download?path=notes.txt")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"hello from termuxfm\n")
        self.assertEqual(result.headers["Accept-Ranges"], "bytes")

    def test_download_is_always_an_attachment(self):
        result = self.client.get("/api/download?path=notes.txt")
        self.assertTrue(
            result.headers["Content-Disposition"].startswith("attachment"))
        self.assertEqual(result.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", result.headers["Content-Security-Policy"])

    def test_download_unicode_filename_header(self):
        name = "Show — 🎬.txt"
        with open(self.fx.path(name), "w") as fh:
            fh.write("hi")
        from urllib.parse import quote

        result = self.client.get("/api/download?path=" + quote(name))
        self.assertEqual(result.status, 200)
        self.assertIn("filename*=UTF-8''", result.headers["Content-Disposition"])

    def test_range_request(self):
        result = self.client.get("/api/download?path=notes.txt",
                                 headers={"Range": "bytes=6-10"})
        self.assertEqual(result.status, 206)
        self.assertEqual(result.body, b"from ")
        self.assertEqual(result.headers["Content-Range"], "bytes 6-10/20")

    def test_suffix_range(self):
        result = self.client.get("/api/download?path=notes.txt",
                                 headers={"Range": "bytes=-8"})
        self.assertEqual(result.status, 206)
        self.assertEqual(result.body, b"ermuxfm\n")

    def test_unsatisfiable_range_is_416(self):
        result = self.client.get("/api/download?path=notes.txt",
                                 headers={"Range": "bytes=999-"})
        self.assertEqual(result.status, 416)

    def test_download_folder_is_rejected(self):
        self.assertEqual(self.client.get("/api/download?path=media").status, 400)

    def test_download_root_is_rejected(self):
        self.assertEqual(self.client.get("/api/download?path=").status, 403)

    def test_download_traversal_rejected(self):
        for path in ["../../etc/passwd", "..%2f..%2fetc", "/etc/passwd"]:
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.get("/api/download?path=" + path).status, 403)

    def test_download_missing_is_404(self):
        self.assertEqual(self.client.get("/api/download?path=ghost").status, 404)

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_symlink_download_refused(self):
        os.symlink("/etc/hosts", self.fx.path("hosts-link"))
        self.assertEqual(
            self.client.get("/api/download?path=hosts-link").status, 403)

    def test_head_request_reports_size_without_a_body(self):
        result = self.client.request("HEAD", "/api/download?path=notes.txt")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.headers["Content-Length"], "20")
        self.assertEqual(result.body, b"")


class LargeFileTests(ServerTestCase):
    """Multi-GB media must never be read into memory."""

    SIZE = 2 * 1024 ** 3 + 12345          # just over 2 GiB, sparse

    def setUp(self):
        super().setUp()
        self.big = self.fx.path("big.mkv")
        with open(self.big, "wb") as fh:
            fh.truncate(self.SIZE)
            fh.seek(self.SIZE - 5)
            fh.write(b"TAIL!")

    def test_listing_reports_the_full_size(self):
        listing = self.client.get("/api/list").json()
        entry = next(e for e in listing["entries"] if e["name"] == "big.mkv")
        self.assertEqual(entry["size"], self.SIZE)

    def test_range_past_2gb_boundary(self):
        """Reads beyond 2^31 must work -- a 32-bit offset bug shows up here."""
        start = self.SIZE - 5
        result = self.client.get(
            "/api/download?path=big.mkv",
            headers={"Range": "bytes=%d-%d" % (start, self.SIZE - 1)})
        self.assertEqual(result.status, 206)
        self.assertEqual(result.body, b"TAIL!")
        self.assertEqual(result.headers["Content-Range"],
                         "bytes %d-%d/%d" % (start, self.SIZE - 1, self.SIZE))

    def test_mid_file_seek(self):
        offset = 1024 ** 3          # 1 GiB in
        result = self.client.get(
            "/api/download?path=big.mkv",
            headers={"Range": "bytes=%d-%d" % (offset, offset + 15)})
        self.assertEqual(result.status, 206)
        self.assertEqual(len(result.body), 16)

    def test_content_length_is_exact_for_a_huge_file(self):
        result = self.client.request("HEAD", "/api/download?path=big.mkv")
        self.assertEqual(result.headers["Content-Length"], str(self.SIZE))


class PreviewTests(ServerTestCase):
    def test_text_preview_is_inline(self):
        result = self.client.get("/api/preview?path=notes.txt")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"hello from termuxfm\n")
        self.assertTrue(
            result.headers["Content-Disposition"].startswith("inline"))
        self.assertEqual(result.headers["X-Preview-Truncated"], "0")

    def test_text_preview_is_capped(self):
        big = "line\n" * 400000        # ~2 MB, over the 1 MB cap
        with open(self.fx.path("big.log"), "w") as fh:
            fh.write(big)
        result = self.client.get("/api/preview?path=big.log")
        self.assertEqual(result.status, 200)
        self.assertLessEqual(len(result.body), transfer.TEXT_PREVIEW_LIMIT)
        self.assertEqual(result.headers["X-Preview-Truncated"], "1")

    def test_binary_file_is_not_previewed_as_text(self):
        with open(self.fx.path("weird.txt"), "wb") as fh:
            fh.write(b"text\x00\x01binary")
        self.assertEqual(self.client.get("/api/preview?path=weird.txt").status,
                         400)

    def test_html_is_never_rendered_inline(self):
        """A stored HTML file must not execute on this origin."""
        with open(self.fx.path("evil.html"), "w") as fh:
            fh.write("<script>alert(document.cookie)</script>")
        result = self.client.get("/api/preview?path=evil.html")
        self.assertEqual(result.status, 400)
        # It is still downloadable, just never rendered on this origin.
        download = self.client.get("/api/download?path=evil.html")
        self.assertEqual(download.status, 200)
        self.assertTrue(
            download.headers["Content-Disposition"].startswith("attachment"))

    def test_svg_is_not_rendered_inline(self):
        with open(self.fx.path("x.svg"), "w") as fh:
            fh.write("<svg xmlns='http://www.w3.org/2000/svg'></svg>")
        self.assertEqual(self.client.get("/api/preview?path=x.svg").status, 400)

    def test_video_preview_supports_ranges(self):
        result = self.client.get(
            "/api/preview?path=downloads/torrents/Show.Name/S01E01.mkv",
            headers={"Range": "bytes=0-99"})
        self.assertEqual(result.status, 206)
        self.assertEqual(len(result.body), 100)
        self.assertEqual(result.headers["Content-Type"], "video/x-matroska")
        self.assertTrue(
            result.headers["Content-Disposition"].startswith("inline"))

    def test_no_preview_for_unknown_binary(self):
        with open(self.fx.path("blob.bin"), "wb") as fh:
            fh.write(b"\x00\x01\x02")
        self.assertEqual(self.client.get("/api/preview?path=blob.bin").status,
                         400)

    def test_preview_of_a_folder_is_rejected(self):
        self.assertEqual(self.client.get("/api/preview?path=media").status, 400)


class ZipTests(ServerTestCase):
    def test_zip_a_folder(self):
        result = self.client.get("/api/zip?path=downloads/torrents/Show.Name")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.headers["Content-Type"], "application/zip")
        self.assertIn("Show.Name.zip", result.headers["Content-Disposition"])
        archive = zipfile.ZipFile(io.BytesIO(result.body))
        self.assertIsNone(archive.testzip())
        self.assertEqual(sorted(archive.namelist()),
                         ["Show.Name/S01E01.mkv", "Show.Name/S01E02.mkv"])
        self.assertEqual(len(archive.read("Show.Name/S01E01.mkv")), 4096)

    def test_zip_is_chunked_because_the_size_is_unknown(self):
        result = self.client.get("/api/zip?path=media")
        self.assertEqual(result.status, 200)
        self.assertNotIn("Content-Length", result.headers)

    def test_zip_preserves_nested_structure(self):
        self.client.post("/api/mkdir", {"path": "media/tv", "name": "Show"})
        self.client.post("/api/mkdir",
                         {"path": "media/tv/Show", "name": "Season 01"})
        with open(self.fx.path("media", "tv", "Show", "Season 01", "e1.mkv"),
                  "wb") as fh:
            fh.write(b"ep")
        result = self.client.get("/api/zip?path=media/tv/Show")
        archive = zipfile.ZipFile(io.BytesIO(result.body))
        self.assertEqual(archive.namelist(), ["Show/Season 01/e1.mkv"])

    def test_zip_a_single_file(self):
        result = self.client.get("/api/zip?path=notes.txt")
        archive = zipfile.ZipFile(io.BytesIO(result.body))
        self.assertEqual(archive.namelist(), ["notes.txt"])

    def test_zip_traversal_rejected(self):
        self.assertEqual(self.client.get("/api/zip?path=../..").status, 403)

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_zip_skips_symlinks(self):
        os.symlink("/etc/hosts", self.fx.path("media", "link"))
        result = self.client.get("/api/zip?path=media")
        archive = zipfile.ZipFile(io.BytesIO(result.body))
        self.assertEqual([n for n in archive.namelist() if "link" in n], [])


if __name__ == "__main__":
    unittest.main()
