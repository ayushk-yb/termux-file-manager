"""Shared test harness: starts a real TermuxFM server on an ephemeral port.

The tests talk HTTP over a loopback socket rather than calling handlers
directly, so routing, status codes, cookies, CSRF, Range and chunked framing are
all exercised the way a browser would exercise them.
"""

import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest

from termuxfm.api import register
from termuxfm.auth import AuthManager, hash_password
from termuxfm.config import Config
from termuxfm.httpd import App, Handler, Server
from termuxfm.jobs import JobRegistry
from termuxfm.safepath import Sandbox

PASSWORD = "correct horse battery"
USERNAME = "tester"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ServerFixture:
    """A running server plus the temp tree it exposes."""

    def __init__(self, tree=None, **cfg_overrides):
        self.tmp = tempfile.mkdtemp(prefix="termuxfm-test-")
        self.root = os.path.join(self.tmp, "Server")
        os.makedirs(self.root)
        if tree:
            make_tree(self.root, tree)
        cfg = Config(path=os.path.join(self.tmp, "config.json"))
        cfg.root = self.root
        # 1000 rounds keeps the suite fast; production uses 600k.
        digest, salt, _ = hash_password(PASSWORD, rounds=1000)
        cfg.password_hash, cfg.password_salt, cfg.pbkdf2_rounds = digest, salt, 1000
        cfg.username = USERNAME
        for key, value in cfg_overrides.items():
            setattr(cfg, key, value)
        self.cfg = cfg
        self.sandbox = Sandbox(self.root)
        self.jobs = JobRegistry()
        self.auth = AuthManager(cfg)
        self.app = App(cfg, self.sandbox, self.auth, self.jobs,
                       os.path.join(REPO, "web"))
        self.app.log = lambda message: None      # keep test output clean
        register(self.app)
        self.port = free_port()
        handler = type("TestHandler", (Handler,), {"app": self.app})
        self.httpd = Server(("127.0.0.1", self.port), handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.root, *parts)


def make_tree(base, spec):
    """``{'dir': {'file.txt': b'data'}}`` -> real files on disk."""
    for name, value in spec.items():
        target = os.path.join(base, name)
        if isinstance(value, dict):
            os.makedirs(target, exist_ok=True)
            make_tree(target, value)
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            data = value.encode() if isinstance(value, str) else value
            with open(target, "wb") as fh:
                fh.write(data)


class Client:
    """Minimal cookie-aware HTTP client."""

    def __init__(self, port):
        self.port = port
        self.cookie = None
        self.csrf = None

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)

    def request(self, method, path, body=None, headers=None, csrf=True,
                raw_body=None):
        headers = dict(headers or {})
        if self.cookie:
            headers["Cookie"] = self.cookie
        if csrf and self.csrf and method in ("POST", "PUT", "DELETE"):
            headers.setdefault("X-CSRF-Token", self.csrf)
        payload = raw_body
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if payload is not None:
            headers["Content-Length"] = str(len(payload))
        conn = self._conn()
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            data = response.read()
            set_cookie = response.getheader("Set-Cookie")
            if set_cookie:
                self.cookie = set_cookie.split(";")[0]
            return Result(response.status, dict(response.getheaders()), data)
        finally:
            conn.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body if body is not None else {},
                            **kw)

    def login(self, username=USERNAME, password=PASSWORD):
        result = self.post("/api/login",
                           {"username": username, "password": password})
        if result.status == 200:
            self.csrf = result.json()["csrf"]
        return result

    def wait_job(self, result, timeout=30):
        """Block until a 202 job response finishes, returning its snapshot."""
        import time

        job_id = result.json()["job"]["id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = self.get("/api/job?id=" + job_id).json()["job"]
            if snapshot["done"]:
                return snapshot
            time.sleep(0.02)
        raise AssertionError("job %s did not finish in %ss" % (job_id, timeout))


class Result:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")


class ServerTestCase(unittest.TestCase):
    """Base case with a logged-in client and a small media-ish tree."""

    TREE = {
        "downloads": {
            "torrents": {
                "Show.Name": {
                    "S01E01.mkv": b"a" * 4096,
                    "S01E02.mkv": b"b" * 8192,
                },
            },
            "direct": {},
        },
        "media": {"movies": {}, "tv": {}},
        "notes.txt": "hello from termuxfm\n",
    }
    CFG = {}

    def setUp(self):
        self.fx = ServerFixture(self.TREE, **self.CFG)
        self.addCleanup(self.fx.stop)
        self.client = Client(self.fx.port)
        self.assertEqual(self.client.login().status, 200)

    def names(self, path=""):
        from urllib.parse import quote

        listing = self.client.get("/api/list?path=" + quote(path)).json()
        return [e["name"] for e in listing["entries"]]
