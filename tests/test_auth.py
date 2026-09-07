"""Authentication, session and CSRF tests."""

import unittest

from termuxfm.auth import (AuthManager, SESSION_COOKIE, hash_password,
                           verify_password)
from termuxfm.config import Config
from termuxfm.errors import Forbidden, TooManyRequests, Unauthorized
from tests.harness import PASSWORD, USERNAME, Client, ServerTestCase


def make_cfg(**kw):
    cfg = Config(path="/nonexistent")
    digest, salt, _ = hash_password("hunter22", rounds=1000)
    cfg.password_hash, cfg.password_salt, cfg.pbkdf2_rounds = digest, salt, 1000
    cfg.username = "admin"
    for key, value in kw.items():
        setattr(cfg, key, value)
    return cfg


class PasswordTests(unittest.TestCase):
    def test_roundtrip(self):
        cfg = make_cfg()
        self.assertTrue(verify_password("hunter22", cfg))
        self.assertFalse(verify_password("hunter23", cfg))
        self.assertFalse(verify_password("", cfg))

    def test_salt_is_random(self):
        first = hash_password("same", rounds=1000)
        second = hash_password("same", rounds=1000)
        self.assertNotEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])

    def test_no_password_configured(self):
        cfg = Config(path="/nonexistent")
        self.assertFalse(cfg.has_password)
        self.assertFalse(verify_password("anything", cfg))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.auth = AuthManager(self.cfg)

    def test_login_and_lookup(self):
        session = self.auth.login("admin", "hunter22", ip="10.0.0.1")
        self.assertIs(self.auth.get(session.token), session)
        self.assertNotEqual(session.token, session.csrf)

    def test_wrong_credentials(self):
        with self.assertRaises(Unauthorized):
            self.auth.login("admin", "nope", ip="10.0.0.1")
        with self.assertRaises(Unauthorized):
            self.auth.login("root", "hunter22", ip="10.0.0.1")

    def test_logout_invalidates_server_side(self):
        session = self.auth.login("admin", "hunter22", ip="10.0.0.1")
        self.assertTrue(self.auth.logout(session.token))
        self.assertIsNone(self.auth.get(session.token))
        with self.assertRaises(Unauthorized):
            self.auth.require(session.token)

    def test_unknown_token_rejected(self):
        self.assertIsNone(self.auth.get("made-up"))
        self.assertIsNone(self.auth.get(None))

    def test_session_expires(self):
        clock = [1000.0]
        auth = AuthManager(make_cfg(session_ttl=60), clock=lambda: clock[0])
        session = auth.login("admin", "hunter22", ip="1.1.1.1")
        clock[0] += 61
        self.assertIsNone(auth.get(session.token))

    def test_csrf_required(self):
        session = self.auth.login("admin", "hunter22", ip="10.0.0.1")
        self.auth.require_csrf(session, session.csrf)
        for bad in [None, "", "wrong", session.token]:
            with self.subTest(bad=bad):
                with self.assertRaises(Forbidden):
                    self.auth.require_csrf(session, bad)

    def test_rate_limit_after_five_failures(self):
        for _ in range(5):
            with self.assertRaises(Unauthorized):
                self.auth.login("admin", "bad", ip="9.9.9.9")
        with self.assertRaises(TooManyRequests):
            self.auth.login("admin", "hunter22", ip="9.9.9.9")

    def test_rate_limit_is_per_ip(self):
        for _ in range(5):
            with self.assertRaises(Unauthorized):
                self.auth.login("admin", "bad", ip="9.9.9.9")
        # A different client is unaffected.
        self.assertTrue(self.auth.login("admin", "hunter22", ip="8.8.8.8"))

    def test_successful_login_clears_failures(self):
        for _ in range(3):
            with self.assertRaises(Unauthorized):
                self.auth.login("admin", "bad", ip="7.7.7.7")
        self.auth.login("admin", "hunter22", ip="7.7.7.7")
        for _ in range(5):
            with self.assertRaises(Unauthorized):
                self.auth.login("admin", "bad", ip="7.7.7.7")

    def test_cookie_flags(self):
        session = self.auth.login("admin", "hunter22", ip="10.0.0.1")
        header = self.auth.cookie_header(session)
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Strict", header)
        self.assertIn("Path=/", header)
        self.assertIn(session.token, header)


class HttpAuthTests(ServerTestCase):
    UNAUTH_GETS = ["/api/list", "/api/me", "/api/stat?path=notes.txt",
                   "/api/download?path=notes.txt", "/api/preview?path=notes.txt",
                   "/api/zip?path=media", "/api/job"]
    UNAUTH_POSTS = ["/api/mkdir", "/api/rename", "/api/delete", "/api/copy",
                    "/api/move", "/api/search", "/api/du", "/api/job/cancel"]

    def test_every_endpoint_requires_a_session(self):
        anon = Client(self.fx.port)
        for path in self.UNAUTH_GETS:
            with self.subTest(path=path):
                self.assertEqual(anon.get(path).status, 401)
        for path in self.UNAUTH_POSTS:
            with self.subTest(path=path):
                self.assertEqual(anon.post(path, {}).status, 401)

    def test_upload_requires_a_session(self):
        anon = Client(self.fx.port)
        result = anon.request("PUT", "/api/upload?dir=", raw_body=b"x",
                              headers={"X-Filename": "eA"})
        self.assertEqual(result.status, 401)

    def test_login_sets_httponly_cookie(self):
        client = Client(self.fx.port)
        result = client.login()
        self.assertEqual(result.status, 200)
        self.assertIn("HttpOnly", result.headers["Set-Cookie"])
        self.assertIn(SESSION_COOKIE, result.headers["Set-Cookie"])

    def test_bad_password_over_http(self):
        client = Client(self.fx.port)
        result = client.post("/api/login",
                             {"username": USERNAME, "password": "wrong"})
        self.assertEqual(result.status, 401)
        self.assertEqual(result.json()["error"], "unauthorized")

    def test_login_does_not_leak_which_field_was_wrong(self):
        client = Client(self.fx.port)
        wrong_user = client.post("/api/login",
                                 {"username": "nobody", "password": PASSWORD})
        wrong_pass = client.post("/api/login",
                                 {"username": USERNAME, "password": "nope"})
        self.assertEqual(wrong_user.json()["message"],
                         wrong_pass.json()["message"])

    def test_mutations_require_csrf(self):
        for path, body in [
            ("/api/mkdir", {"path": "", "name": "csrf-test"}),
            ("/api/rename", {"path": "notes.txt", "name": "x.txt"}),
            ("/api/delete", {"paths": ["notes.txt"], "confirm": True}),
            ("/api/copy", {"paths": ["notes.txt"], "dest": "media"}),
            ("/api/move", {"paths": ["notes.txt"], "dest": "media"}),
        ]:
            with self.subTest(path=path):
                result = self.client.request("POST", path, body, csrf=False)
                self.assertEqual(result.status, 403)
                self.assertEqual(result.json()["error"], "forbidden")

    def test_wrong_csrf_token_rejected(self):
        result = self.client.request(
            "POST", "/api/mkdir", {"path": "", "name": "x"},
            headers={"X-CSRF-Token": "not-the-token"}, csrf=False)
        self.assertEqual(result.status, 403)

    def test_logout_then_blocked(self):
        self.assertEqual(self.client.post("/api/logout").status, 200)
        self.assertEqual(self.client.get("/api/list").status, 401)

    def test_me_reports_identity(self):
        payload = self.client.get("/api/me").json()
        self.assertEqual(payload["username"], USERNAME)
        self.assertEqual(payload["root"], "Server")
        self.assertIn("max_upload_bytes", payload)

    def test_static_login_page_is_public(self):
        anon = Client(self.fx.port)
        result = anon.get("/")
        self.assertEqual(result.status, 200)
        self.assertIn("Content-Security-Policy", result.headers)


if __name__ == "__main__":
    unittest.main()
