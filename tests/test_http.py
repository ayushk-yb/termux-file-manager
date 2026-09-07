"""Routing, status codes, jobs and response-header tests."""

import json
import threading
import unittest

from tests.harness import Client, ServerTestCase


class RoutingTests(ServerTestCase):
    def test_unknown_path_is_404(self):
        self.assertEqual(self.client.get("/api/nope").status, 404)
        self.assertEqual(self.client.get("/random").status, 404)

    def test_wrong_method_is_405(self):
        self.assertEqual(self.client.post("/api/list", {}).status, 405)
        self.assertEqual(self.client.get("/api/mkdir").status, 405)

    def test_ping_is_public(self):
        anon = Client(self.fx.port)
        result = anon.get("/api/ping")
        self.assertEqual(result.status, 200)
        self.assertTrue(result.json()["ok"])

    def test_options_is_answered(self):
        result = self.client.request("OPTIONS", "/api/list")
        self.assertEqual(result.status, 204)

    def test_malformed_json_is_400(self):
        result = self.client.request("POST", "/api/mkdir",
                                     raw_body=b"{not json",
                                     headers={"Content-Type": "application/json"})
        self.assertEqual(result.status, 400)

    def test_json_array_body_is_400(self):
        result = self.client.request("POST", "/api/mkdir", raw_body=b"[1,2]",
                                     headers={"Content-Type": "application/json"})
        self.assertEqual(result.status, 400)

    def test_oversized_json_body_is_413(self):
        blob = json.dumps({"name": "x" * (2 * 1024 * 1024)}).encode()
        result = self.client.request("POST", "/api/mkdir", raw_body=blob)
        self.assertEqual(result.status, 413)

    def test_errors_are_json_with_a_code(self):
        payload = self.client.get("/api/list?path=ghost").json()
        self.assertEqual(payload["error"], "not_found")
        self.assertIn("message", payload)

    def test_security_headers_on_every_response(self):
        for path in ["/", "/api/ping", "/api/list"]:
            with self.subTest(path=path):
                headers = self.client.get(path).headers
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_app_csp_allows_only_self(self):
        csp = self.client.get("/").headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-eval", csp)

    def test_server_banner_hides_python_version(self):
        banner = self.client.get("/api/ping").headers["Server"]
        self.assertIn("TermuxFM", banner)
        self.assertNotIn("Python", banner)

    def test_keep_alive_is_http11(self):
        # One connection, two requests: proves HTTP/1.1 framing is correct.
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.fx.port, timeout=10)
        for _ in range(2):
            conn.request("GET", "/api/ping")
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.version, 11)
        conn.close()


class StaticAssetTests(ServerTestCase):
    def test_index_is_served_at_root(self):
        result = self.client.get("/")
        self.assertEqual(result.status, 200)
        self.assertIn("text/html", result.headers["Content-Type"])

    def test_assets_are_served(self):
        for path, kind in [("/app.js", "javascript"), ("/style.css", "css")]:
            with self.subTest(path=path):
                result = self.client.get(path)
                self.assertEqual(result.status, 200)
                self.assertIn(kind, result.headers["Content-Type"])

    def test_static_serving_is_an_allowlist(self):
        """The web root is not a file server: only known assets are reachable."""
        for path in ["/../termuxfm/cli.py", "/..%2fsetup.py", "/harness.py",
                     "/../../etc/passwd", "/web/index.html"]:
            with self.subTest(path=path):
                self.assertIn(self.client.get(path).status, (400, 403, 404))


class JobTests(ServerTestCase):
    def test_job_lifecycle(self):
        started = self.client.post(
            "/api/copy", {"paths": ["notes.txt"], "dest": "media"})
        self.assertEqual(started.status, 202)
        job_id = started.json()["job"]["id"]
        snapshot = self.client.wait_job(started)
        self.assertEqual(snapshot["state"], "done")
        self.assertEqual(snapshot["kind"], "copy")
        self.assertGreater(snapshot["elapsed"], -1)
        # Still queryable after completion.
        self.assertEqual(
            self.client.get("/api/job?id=" + job_id).json()["job"]["id"], job_id)

    def test_unknown_job_is_404(self):
        self.assertEqual(self.client.get("/api/job?id=deadbeef").status, 404)
        self.assertEqual(
            self.client.post("/api/job/cancel", {"id": "deadbeef"}).status, 404)

    def test_job_list_without_id(self):
        result = self.client.get("/api/job")
        self.assertEqual(result.status, 200)
        self.assertIn("jobs", result.json())

    def test_cancellation_stops_the_worker(self):
        # A tree big enough that cancellation lands mid-copy.
        for index in range(40):
            with open(self.fx.path("media", "movies", "f%02d.bin" % index),
                      "wb") as fh:
                fh.write(b"x" * (512 * 1024))
        started = self.client.post(
            "/api/copy", {"paths": ["media/movies"], "dest": "media/tv"})
        job_id = started.json()["job"]["id"]
        self.assertEqual(
            self.client.post("/api/job/cancel", {"id": job_id}).status, 200)
        snapshot = self.client.wait_job(started)
        self.assertEqual(snapshot["state"], "cancelled")

    def test_job_progress_fields_are_present(self):
        started = self.client.post(
            "/api/copy", {"paths": ["downloads/torrents/Show.Name"],
                          "dest": "media"})
        snapshot = self.client.wait_job(started)
        for field in ["total_bytes", "done_bytes", "total_items", "done_items",
                      "state", "errors", "label", "kind"]:
            self.assertIn(field, snapshot)

    def test_concurrent_requests_are_served(self):
        """Several browser tabs polling at once must not deadlock the server."""
        results = []
        lock = threading.Lock()

        def hammer():
            client = Client(self.fx.port)
            client.cookie = self.client.cookie
            status = client.get("/api/list").status
            with lock:
                results.append(status)

        threads = [threading.Thread(target=hammer) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(results, [200] * 12)


class FilenameEdgeCaseTests(ServerTestCase):
    """Names that break naive file managers."""

    NAMES = [
        "spaces everywhere.mkv",
        "quote'single.mkv",
        "semi;colon.mkv",
        "amp&ersand.mkv",
        "hash#tag.mkv",
        "question?mark.mkv",          # rejected on create, see below
        "percent%20encoded.mkv",
        "plus+plus.mkv",
        "dash-dash_under.mkv",
        "[brackets].mkv",
        "(parens).mkv",
        "日本語.mkv",
        "emoji 🎬.mkv",
        "Ünïcödé.mkv",
        "..leading-dots.mkv",
        "a" * 200 + ".mkv",
    ]

    def test_create_list_download_and_delete_each_name(self):
        from urllib.parse import quote

        for name in self.NAMES:
            with self.subTest(name=name):
                created = self.client.post("/api/mkdir",
                                           {"path": "media/tv", "name": name})
                if ":" in name or "?" in name or "*" in name:
                    # Android's storage layer would reject these outright.
                    self.assertEqual(created.status, 422)
                    continue
                self.assertEqual(created.status, 201)
                self.assertIn(name, self.names("media/tv"))
                listed = self.client.get(
                    "/api/list?path=" + quote("media/tv/" + name))
                self.assertEqual(listed.status, 200)
                deleted = self.client.wait_job(self.client.post(
                    "/api/delete",
                    {"paths": ["media/tv/" + name], "confirm": True}))
                self.assertEqual(deleted["state"], "done")

    def test_upload_and_download_roundtrip_with_odd_names(self):
        import base64
        from urllib.parse import quote

        for name in ["quote'.txt", "amp&.txt", "hash#.txt", "日本語.txt",
                     "emoji 🎬.txt", "percent%.txt", "plus+.txt"]:
            with self.subTest(name=name):
                header = base64.urlsafe_b64encode(
                    name.encode()).decode().rstrip("=")
                upload = self.client.request(
                    "PUT", "/api/upload?dir=media&conflict=fail",
                    raw_body=b"payload-" + name.encode(),
                    headers={"X-Filename": header})
                self.assertEqual(upload.status, 201)
                stored = upload.json()["path"]
                download = self.client.get("/api/download?path=" + quote(stored))
                self.assertEqual(download.status, 200)
                self.assertEqual(download.body, b"payload-" + name.encode())


if __name__ == "__main__":
    unittest.main()
