"""Filesystem operation tests, including the Android-specific paths."""

import errno
import os
import unittest
from unittest import mock

from termuxfm import fsops
from tests.harness import ServerTestCase


class ListingTests(ServerTestCase):
    def test_lists_root(self):
        self.assertEqual(self.names(), ["downloads", "media", "notes.txt"])

    def test_directories_lead_regardless_of_order(self):
        listing = self.client.get("/api/list?sort=name&order=desc").json()
        kinds = [e["is_dir"] for e in listing["entries"]]
        self.assertEqual(kinds, sorted(kinds, reverse=True))

    def test_reports_size_and_mtime(self):
        listing = self.client.get(
            "/api/list?path=downloads/torrents/Show.Name").json()
        by_name = {e["name"]: e for e in listing["entries"]}
        self.assertEqual(by_name["S01E01.mkv"]["size"], 4096)
        self.assertEqual(by_name["S01E02.mkv"]["size"], 8192)
        self.assertGreater(by_name["S01E01.mkv"]["mtime"], 0)
        self.assertEqual(by_name["S01E01.mkv"]["kind"], "video")

    def test_sort_by_size_and_date(self):
        base = "downloads/torrents/Show.Name"
        asc = self.client.get("/api/list?path=%s&sort=size" % base).json()
        self.assertEqual([e["name"] for e in asc["entries"]],
                         ["S01E01.mkv", "S01E02.mkv"])
        desc = self.client.get(
            "/api/list?path=%s&sort=size&order=desc" % base).json()
        self.assertEqual([e["name"] for e in desc["entries"]],
                         ["S01E02.mkv", "S01E01.mkv"])
        self.assertEqual(
            self.client.get("/api/list?path=%s&sort=date" % base).status, 200)

    def test_natural_order_for_season_folders(self):
        for name in ["Season 1", "Season 2", "Season 10", "Season 20"]:
            self.client.post("/api/mkdir", {"path": "media/tv", "name": name})
        self.assertEqual(self.names("media/tv"),
                         ["Season 1", "Season 2", "Season 10", "Season 20"])

    def test_unknown_sort_rejected(self):
        self.assertEqual(self.client.get("/api/list?sort=owner").status, 400)
        self.assertEqual(self.client.get("/api/list?order=sideways").status, 400)

    def test_listing_a_file_is_a_400(self):
        self.assertEqual(self.client.get("/api/list?path=notes.txt").status, 400)

    def test_missing_path_is_404(self):
        self.assertEqual(self.client.get("/api/list?path=ghost").status, 404)

    def test_stat_single_entry(self):
        payload = self.client.get("/api/stat?path=notes.txt").json()
        self.assertEqual(payload["name"], "notes.txt")
        self.assertEqual(payload["kind"], "text")
        self.assertFalse(payload["is_dir"])


class MkdirTests(ServerTestCase):
    def test_creates_nested_structure(self):
        self.assertEqual(self.client.post(
            "/api/mkdir", {"path": "media/tv", "name": "Show Name"}).status, 201)
        self.assertEqual(self.client.post(
            "/api/mkdir",
            {"path": "media/tv/Show Name", "name": "Season 01"}).status, 201)
        self.assertTrue(os.path.isdir(
            self.fx.path("media", "tv", "Show Name", "Season 01")))

    def test_duplicate_is_409(self):
        self.client.post("/api/mkdir", {"path": "media", "name": "dup"})
        result = self.client.post("/api/mkdir", {"path": "media", "name": "dup"})
        self.assertEqual(result.status, 409)

    def test_invalid_names_are_422(self):
        for name in ["../escape", "a/b", "", ".", "bad:name", "trailing.",
                     "nul\x00byte"]:
            with self.subTest(name=name):
                result = self.client.post("/api/mkdir",
                                          {"path": "media", "name": name})
                self.assertIn(result.status, (400, 422))

    def test_missing_name_is_400(self):
        self.assertEqual(
            self.client.post("/api/mkdir", {"path": "media"}).status, 400)

    def test_creating_inside_a_file_fails(self):
        result = self.client.post("/api/mkdir",
                                  {"path": "notes.txt", "name": "x"})
        self.assertEqual(result.status, 400)


class RenameTests(ServerTestCase):
    def test_rename_file(self):
        result = self.client.post(
            "/api/rename", {"path": "notes.txt", "name": "readme.txt"})
        self.assertEqual(result.status, 200)
        self.assertEqual(self.names(), ["downloads", "media", "readme.txt"])

    def test_rename_folder(self):
        result = self.client.post("/api/rename",
                                  {"path": "downloads/torrents/Show.Name",
                                   "name": "Show Name"})
        self.assertEqual(result.status, 200)
        self.assertEqual(self.names("downloads/torrents"), ["Show Name"])

    def test_rename_onto_existing_is_409(self):
        self.client.post("/api/mkdir", {"path": "", "name": "taken"})
        result = self.client.post("/api/rename",
                                  {"path": "notes.txt", "name": "taken"})
        self.assertEqual(result.status, 409)

    def test_rename_to_same_name_is_a_noop(self):
        result = self.client.post("/api/rename",
                                  {"path": "notes.txt", "name": "notes.txt"})
        self.assertEqual(result.status, 200)
        self.assertTrue(os.path.exists(self.fx.path("notes.txt")))

    def test_cannot_rename_root(self):
        self.assertEqual(
            self.client.post("/api/rename", {"path": "", "name": "x"}).status,
            403)

    def test_rename_cannot_move_via_name(self):
        result = self.client.post("/api/rename",
                                  {"path": "notes.txt", "name": "../notes.txt"})
        self.assertEqual(result.status, 422)

    def test_unicode_rename(self):
        result = self.client.post("/api/rename",
                                  {"path": "notes.txt", "name": "日本語 🎬.txt"})
        self.assertEqual(result.status, 200)
        self.assertIn("日本語 🎬.txt", self.names())


class CopyTests(ServerTestCase):
    def test_copy_file(self):
        job = self.client.wait_job(self.client.post(
            "/api/copy", {"paths": ["notes.txt"], "dest": "media"}))
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["errors"], [])
        self.assertIn("notes.txt", self.names("media"))
        # The original survives.
        self.assertIn("notes.txt", self.names())

    def test_copy_folder_recursively(self):
        job = self.client.wait_job(self.client.post(
            "/api/copy",
            {"paths": ["downloads/torrents/Show.Name"], "dest": "media/tv"}))
        self.assertEqual(job["state"], "done")
        self.assertEqual(sorted(self.names("media/tv/Show.Name")),
                         ["S01E01.mkv", "S01E02.mkv"])
        self.assertEqual(job["done_bytes"], 4096 + 8192)

    def test_copy_reports_byte_progress_totals(self):
        job = self.client.wait_job(self.client.post(
            "/api/copy",
            {"paths": ["downloads/torrents/Show.Name"], "dest": "media"}))
        self.assertEqual(job["total_bytes"], 4096 + 8192)
        self.assertEqual(job["total_items"], 2)

    def test_copy_conflict_fail(self):
        self.client.wait_job(self.client.post(
            "/api/copy", {"paths": ["notes.txt"], "dest": "media"}))
        job = self.client.wait_job(self.client.post(
            "/api/copy", {"paths": ["notes.txt"], "dest": "media"}))
        self.assertEqual(job["error_count"], 1)
        self.assertIn("already exists", job["errors"][0]["message"])

    def test_copy_conflict_rename(self):
        for _ in range(2):
            self.client.wait_job(self.client.post(
                "/api/copy", {"paths": ["notes.txt"], "dest": "media",
                              "conflict": "rename"}))
        self.assertIn("notes (2).txt", self.names("media"))

    def test_copy_conflict_overwrite(self):
        self.client.wait_job(self.client.post(
            "/api/copy", {"paths": ["notes.txt"], "dest": "media"}))
        with open(self.fx.path("notes.txt"), "w") as fh:
            fh.write("replaced content")
        job = self.client.wait_job(self.client.post(
            "/api/copy", {"paths": ["notes.txt"], "dest": "media",
                          "conflict": "overwrite"}))
        self.assertEqual(job["error_count"], 0)
        with open(self.fx.path("media", "notes.txt")) as fh:
            self.assertEqual(fh.read(), "replaced content")

    def test_copy_into_own_subtree_refused(self):
        result = self.client.post(
            "/api/copy", {"paths": ["downloads"], "dest": "downloads/torrents"})
        self.assertEqual(result.status, 400)
        self.assertIn("into itself", result.json()["message"])

    def test_copy_unknown_conflict_mode(self):
        self.assertEqual(self.client.post(
            "/api/copy", {"paths": ["notes.txt"], "dest": "media",
                          "conflict": "yolo"}).status, 400)

    def test_copy_requires_paths(self):
        for body in [{}, {"paths": [], "dest": ""}, {"paths": "x"}]:
            with self.subTest(body=body):
                status = self.client.post("/api/copy", body).status
                self.assertIn(status, (400, 404))

    def test_copy_to_missing_destination_is_404(self):
        self.assertEqual(self.client.post(
            "/api/copy", {"paths": ["notes.txt"], "dest": "ghost"}).status, 404)

    def test_copy_leaves_no_partial_file_on_failure(self):
        target = self.fx.path("media")
        with mock.patch("termuxfm.fsops._copy_file",
                        side_effect=OSError(errno.ENOSPC, "No space")):
            job = self.client.wait_job(self.client.post(
                "/api/copy", {"paths": ["notes.txt"], "dest": "media"}))
        self.assertEqual(job["error_count"], 1)
        self.assertNotIn("notes.txt", os.listdir(target))


class MoveTests(ServerTestCase):
    def test_move_file(self):
        job = self.client.wait_job(self.client.post(
            "/api/move", {"paths": ["notes.txt"], "dest": "media"}))
        self.assertEqual(job["state"], "done")
        self.assertNotIn("notes.txt", self.names())
        self.assertIn("notes.txt", self.names("media"))

    def test_move_folder_the_jellyfin_way(self):
        """The primary use case: reshape a torrent folder into Jellyfin's tree."""
        self.client.post("/api/mkdir", {"path": "media/tv", "name": "Show Name"})
        self.client.post("/api/mkdir",
                         {"path": "media/tv/Show Name", "name": "Season 01"})
        episodes = ["downloads/torrents/Show.Name/S01E01.mkv",
                    "downloads/torrents/Show.Name/S01E02.mkv"]
        job = self.client.wait_job(self.client.post(
            "/api/move", {"paths": episodes,
                          "dest": "media/tv/Show Name/Season 01"}))
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["errors"], [])
        self.assertEqual(sorted(self.names("media/tv/Show Name/Season 01")),
                         ["S01E01.mkv", "S01E02.mkv"])
        self.assertEqual(self.names("downloads/torrents/Show.Name"), [])

    def test_cross_device_fallback(self):
        """EXDEV is what Termux home -> /storage/emulated/0 actually raises."""
        real_rename = os.rename
        calls = []

        def fake_rename(src, dst, *a, **kw):
            calls.append((src, dst))
            raise OSError(errno.EXDEV, "Cross-device link")

        with mock.patch("termuxfm.fsops.os.rename", side_effect=fake_rename):
            job = self.client.wait_job(self.client.post(
                "/api/move", {"paths": ["notes.txt"], "dest": "media"}))
        self.assertTrue(calls, "os.rename should have been attempted first")
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["errors"], [])
        # Copy+unlink fallback: present at the destination, gone from the source.
        self.assertTrue(os.path.exists(self.fx.path("media", "notes.txt")))
        self.assertFalse(os.path.exists(self.fx.path("notes.txt")))
        self.assertIs(os.rename, real_rename)

    def test_cross_device_fallback_for_folders(self):
        with mock.patch("termuxfm.fsops.os.rename",
                        side_effect=OSError(errno.EXDEV, "Cross-device link")):
            job = self.client.wait_job(self.client.post(
                "/api/move", {"paths": ["downloads/torrents/Show.Name"],
                              "dest": "media/tv"}))
        self.assertEqual(job["state"], "done")
        self.assertEqual(sorted(self.names("media/tv/Show.Name")),
                         ["S01E01.mkv", "S01E02.mkv"])
        self.assertFalse(os.path.exists(
            self.fx.path("downloads", "torrents", "Show.Name")))

    def test_unexpected_errno_is_not_swallowed(self):
        with mock.patch("termuxfm.fsops.os.rename",
                        side_effect=OSError(errno.EIO, "I/O error")):
            job = self.client.wait_job(self.client.post(
                "/api/move", {"paths": ["notes.txt"], "dest": "media"}))
        self.assertEqual(job["error_count"], 1)
        self.assertTrue(os.path.exists(self.fx.path("notes.txt")))

    def test_move_conflict_fail(self):
        self.client.wait_job(self.client.post(
            "/api/copy", {"paths": ["notes.txt"], "dest": "media"}))
        job = self.client.wait_job(self.client.post(
            "/api/move", {"paths": ["notes.txt"], "dest": "media"}))
        self.assertEqual(job["error_count"], 1)
        self.assertTrue(os.path.exists(self.fx.path("notes.txt")))

    def test_move_conflict_rename(self):
        self.client.wait_job(self.client.post(
            "/api/copy", {"paths": ["notes.txt"], "dest": "media"}))
        job = self.client.wait_job(self.client.post(
            "/api/move", {"paths": ["notes.txt"], "dest": "media",
                          "conflict": "rename"}))
        self.assertEqual(job["state"], "done")
        self.assertIn("notes (2).txt", self.names("media"))

    def test_move_into_itself_refused(self):
        result = self.client.post(
            "/api/move", {"paths": ["downloads"], "dest": "downloads/torrents"})
        self.assertEqual(result.status, 400)

    def test_move_to_same_place_is_reported(self):
        job = self.client.wait_job(self.client.post(
            "/api/move", {"paths": ["notes.txt"], "dest": ""}))
        self.assertEqual(job["error_count"], 1)
        self.assertTrue(os.path.exists(self.fx.path("notes.txt")))


class DeleteTests(ServerTestCase):
    def test_delete_requires_confirmation(self):
        result = self.client.post("/api/delete", {"paths": ["notes.txt"]})
        self.assertEqual(result.status, 400)
        self.assertIn("permanent", result.json()["message"])
        self.assertTrue(os.path.exists(self.fx.path("notes.txt")))

    def test_delete_file(self):
        job = self.client.wait_job(self.client.post(
            "/api/delete", {"paths": ["notes.txt"], "confirm": True}))
        self.assertEqual(job["state"], "done")
        self.assertFalse(os.path.exists(self.fx.path("notes.txt")))

    def test_delete_folder_recursively(self):
        job = self.client.wait_job(self.client.post(
            "/api/delete", {"paths": ["downloads/torrents"], "confirm": True}))
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["errors"], [])
        self.assertFalse(os.path.exists(self.fx.path("downloads", "torrents")))
        self.assertTrue(os.path.isdir(self.fx.path("downloads")))

    def test_cannot_delete_root(self):
        self.assertEqual(self.client.post(
            "/api/delete", {"paths": [""], "confirm": True}).status, 403)

    def test_delete_missing_is_404(self):
        self.assertEqual(self.client.post(
            "/api/delete", {"paths": ["ghost"], "confirm": True}).status, 404)

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_delete_removes_link_not_target(self):
        outside_dir = os.path.join(self.fx.tmp, "outside")
        os.makedirs(outside_dir)
        victim = os.path.join(outside_dir, "keepme")
        with open(victim, "w") as fh:
            fh.write("must survive")
        os.makedirs(self.fx.path("linkdir"))
        os.symlink(outside_dir, self.fx.path("linkdir", "link"))
        job = self.client.wait_job(self.client.post(
            "/api/delete", {"paths": ["linkdir"], "confirm": True}))
        self.assertEqual(job["state"], "done")
        self.assertFalse(os.path.exists(self.fx.path("linkdir")))
        # The recursive delete walked *into* nothing: the target is untouched.
        self.assertTrue(os.path.exists(victim))


class SearchTests(ServerTestCase):
    def test_finds_by_substring(self):
        job = self.client.wait_job(self.client.post(
            "/api/search", {"path": "", "query": "s01e"}))
        names = sorted(e["name"] for e in job["result"]["entries"])
        self.assertEqual(names, ["S01E01.mkv", "S01E02.mkv"])

    def test_is_case_insensitive(self):
        job = self.client.wait_job(self.client.post(
            "/api/search", {"path": "", "query": "SHOW"}))
        self.assertEqual([e["name"] for e in job["result"]["entries"]],
                         ["Show.Name"])

    def test_scoped_to_subtree(self):
        job = self.client.wait_job(self.client.post(
            "/api/search", {"path": "media", "query": "notes"}))
        self.assertEqual(job["result"]["entries"], [])

    def test_results_carry_parent_paths(self):
        job = self.client.wait_job(self.client.post(
            "/api/search", {"path": "", "query": "S01E01"}))
        entry = job["result"]["entries"][0]
        self.assertEqual(entry["parent"], "downloads/torrents/Show.Name")
        self.assertEqual(entry["path"],
                         "downloads/torrents/Show.Name/S01E01.mkv")

    def test_empty_query_rejected(self):
        self.assertEqual(self.client.post(
            "/api/search", {"path": "", "query": "   "}).status, 400)

    def test_search_outside_root_rejected(self):
        self.assertEqual(self.client.post(
            "/api/search", {"path": "../..", "query": "x"}).status, 403)


class DuTests(ServerTestCase):
    def test_recursive_size(self):
        job = self.client.wait_job(self.client.post("/api/du", {"path": ""}))
        result = job["result"]
        self.assertEqual(result["bytes"], 4096 + 8192 + len("hello from termuxfm\n"))
        self.assertEqual(result["files"], 3)
        self.assertGreaterEqual(result["dirs"], 5)

    def test_subtree_size(self):
        job = self.client.wait_job(self.client.post(
            "/api/du", {"path": "downloads/torrents/Show.Name"}))
        self.assertEqual(job["result"]["bytes"], 4096 + 8192)

    def test_second_call_is_cached(self):
        self.client.wait_job(self.client.post(
            "/api/du", {"path": "downloads/torrents/Show.Name"}))
        job = self.client.wait_job(self.client.post(
            "/api/du", {"path": "downloads/torrents/Show.Name"}))
        self.assertTrue(job["result"]["cached"])

    def test_missing_path_is_404(self):
        self.assertEqual(self.client.post("/api/du", {"path": "ghost"}).status,
                         404)


class UniqueNameTests(unittest.TestCase):
    def test_numbering(self):
        import tempfile

        tmp = tempfile.mkdtemp()
        first = os.path.join(tmp, "a.mkv")
        open(first, "w").close()
        second = fsops.unique_name(first)
        self.assertEqual(os.path.basename(second), "a (2).mkv")
        open(second, "w").close()
        third = fsops.unique_name(first)
        self.assertEqual(os.path.basename(third), "a (3).mkv")

    def test_no_extension(self):
        import tempfile

        tmp = tempfile.mkdtemp()
        target = os.path.join(tmp, "folder")
        os.makedirs(target)
        self.assertEqual(os.path.basename(fsops.unique_name(target)),
                         "folder (2)")


class ClassifyTests(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "a.mkv": "video", "a.mp4": "video", "a.mp3": "audio",
            "a.flac": "audio", "a.jpg": "image", "a.txt": "text",
            "a.srt": "text", "a.zip": "archive", "a.pdf": "document",
            "a.bin": "file", "noext": "file",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(fsops.classify(name, False), expected)
        self.assertEqual(fsops.classify("anything", True), "dir")

    def test_case_insensitive_extensions(self):
        self.assertEqual(fsops.classify("MOVIE.MKV", False), "video")


if __name__ == "__main__":
    unittest.main()
