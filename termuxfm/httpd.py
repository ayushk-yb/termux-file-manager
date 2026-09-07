"""The HTTP server: a threaded stdlib server with a tiny explicit router.

``ThreadingHTTPServer`` is a thread per connection, which is the right shape
here -- a handful of browser connections, each mostly blocked on disk or socket
I/O, so the GIL is released where it matters.  A semaphore caps the thread count
so a buggy or hostile client cannot fork-bomb a phone.

``protocol_version`` must be set to HTTP/1.1 explicitly; without it the stdlib
speaks 1.0, which rules out keep-alive (making a file listing of 500 rows a
storm of connections) and chunked responses (needed for streaming ZIP).
"""

import io
import json
import os
import sys
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .errors import (ApiError, InvalidRequest, MethodNotAllowed, NotFound,
                     PayloadTooLarge)

MAX_THREADS = 32
MAX_JSON_BODY = 1024 * 1024
#: An oversized JSON body is drained up to this size before the 413 is sent, so
#: the client actually receives the status instead of a broken pipe.
DRAIN_LIMIT = 8 * 1024 * 1024
MAX_HEADER_COUNT = 64

BASE_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
)

APP_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
)

#: Applied to anything served *from the managed tree*.  Combined with the
#: attachment disposition for non-media, a stored HTML file can never run as
#: script on this origin.
FILE_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"


class Response:
    def __init__(self, status=200, body=b"", content_type="application/json",
                 headers=None, close=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = list(headers or [])
        self.close = close


class StreamResponse:
    """A response whose body is produced by a callback writing to a stream."""

    def __init__(self, writer, *, status=200, content_type="application/octet-stream",
                 length=None, headers=None, chunked=False):
        self.writer = writer
        self.status = status
        self.content_type = content_type
        self.length = length
        self.headers = list(headers or [])
        self.chunked = chunked


def json_response(payload, status=200, headers=None):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return Response(status, body, "application/json; charset=utf-8", headers)


class ChunkedWriter(io.RawIOBase):
    """Wraps a socket stream in HTTP/1.1 chunked transfer encoding.

    Subclasses ``RawIOBase`` so that ``zipfile`` detects an unseekable stream
    (its ``tell()`` raises ``OSError``) and switches to data descriptors.
    """

    def __init__(self, raw):
        self._raw = raw
        self.total = 0

    def writable(self):
        return True

    def seekable(self):
        return False

    def write(self, data):
        data = bytes(data)
        if not data:
            return 0
        self._raw.write(b"%X\r\n" % len(data))
        self._raw.write(data)
        self._raw.write(b"\r\n")
        self.total += len(data)
        return len(data)

    def finish(self):
        self._raw.write(b"0\r\n\r\n")


class Request:
    def __init__(self, handler):
        self.handler = handler
        self.method = handler.command
        parsed = urlparse(handler.path)
        self.path = unquote(parsed.path)
        self.raw_query = parsed.query
        self._query = parse_qs(parsed.query, keep_blank_values=True)
        self.headers = handler.headers
        self.client_ip = handler.client_address[0] if handler.client_address else "?"
        self._json = None
        self._body_read = False

    # -- parameters -----------------------------------------------------

    def q(self, name, default=None):
        values = self._query.get(name)
        if not values:
            return default
        return values[0]

    def q_all(self, name):
        return self._query.get(name, [])

    def header(self, name, default=None):
        return self.headers.get(name, default)

    @property
    def content_length(self):
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            raise InvalidRequest("Malformed Content-Length")
        if value < 0:
            raise InvalidRequest("Negative Content-Length")
        return value

    def json(self):
        if self._json is not None:
            return self._json
        length = self.content_length
        if length is None:
            raise InvalidRequest("A JSON body with Content-Length is required")
        if length > MAX_JSON_BODY:
            if length <= DRAIN_LIMIT:
                remaining = length
                while remaining > 0:
                    chunk = self.handler.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                self._body_read = True
            raise PayloadTooLarge("Request body is too large")
        raw = self.handler.rfile.read(length) if length else b"{}"
        self._body_read = True
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidRequest("Malformed JSON body: %s" % exc)
        if not isinstance(data, dict):
            raise InvalidRequest("JSON body must be an object")
        self._json = data
        return data

    def mark_body_read(self):
        """Tell the error path that the request body was fully consumed."""
        self._body_read = True

    def cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:                       # noqa: BLE001 -- malformed jar
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def str_list(self, data, key):
        """Read a list-of-strings field from a JSON body."""
        value = data.get(key)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value:
            raise InvalidRequest("%r must be a non-empty list of paths" % key)
        if len(value) > 5000:
            raise InvalidRequest("Too many paths in one request")
        for item in value:
            if not isinstance(item, str):
                raise InvalidRequest("%r must contain only strings" % key)
        return value


class App:
    """Shared server state, handed to every route."""

    def __init__(self, cfg, sandbox, auth, jobs, webroot, access_log=False):
        self.cfg = cfg
        self.sandbox = sandbox
        self.auth = auth
        self.jobs = jobs
        self.webroot = os.path.realpath(webroot)
        # Off by default: one video seek session plus UI polling is tens of
        # thousands of requests, and a phone should not spend its storage
        # recording that nothing went wrong.
        self.access_log = access_log
        self.started = time.time()
        self.routes = {}

    def log(self, message):
        sys.stdout.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        sys.stdout.flush()

    def route(self, method, path):
        def register(fn):
            self.routes[(method, path)] = fn
            return fn
        return register


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TermuxFM/%s" % __version__
    sys_version = ""
    app = None

    # -- plumbing -------------------------------------------------------

    def log_message(self, fmt, *args):
        self.app.log("%s %s" % (self.client_address[0], fmt % args))

    def log_error(self, fmt, *args):
        self.app.log("%s error: %s" % (self.client_address[0], fmt % args))

    def log_request(self, code="-", size="-"):
        """Log a completed request.

        Successful requests are silent unless --access-log is set: a listing
        refresh, a job poll and every Range request of a video would otherwise
        each cost a line on the phone's storage.  Client and server errors are
        always recorded, because those are what you troubleshoot.
        """
        status = code.value if hasattr(code, "value") else code
        if not self.app.access_log:
            try:
                if int(status) < 400:
                    return
            except (TypeError, ValueError):
                pass
        super().log_request(code, size)

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._send(Response(204, b"", None,
                            [("Allow", "GET, HEAD, POST, PUT, OPTIONS")]))

    def _dispatch(self):
        req = None
        try:
            if len(self.headers) > MAX_HEADER_COUNT:
                raise InvalidRequest("Too many headers")
            req = Request(self)
            handler = self.app.routes.get((self.command, req.path))
            if handler is None:
                if self.command == "HEAD":
                    handler = self.app.routes.get(("GET", req.path))
                if handler is None:
                    if any(m for (m, p) in self.app.routes if p == req.path):
                        raise MethodNotAllowed("%s is not allowed here" % self.command)
                    handler = _serve_static
            result = handler(self.app, req)
            self._send(result, head_only=(self.command == "HEAD"))
        except ApiError as exc:
            self._send_error(exc, req)
        except (BrokenPipeError, ConnectionResetError):
            # The browser cancelled a download or navigated away.
            self.close_connection = True
        except Exception as exc:                # noqa: BLE001 -- last resort
            self.app.log("unhandled %s on %s: %s\n%s" % (
                exc.__class__.__name__, self.path, exc, traceback.format_exc()))
            self._send_error(ApiError("Internal server error"), req)

    def _send_error(self, exc, req=None):
        # If a request body was never consumed, the stream is out of sync and
        # the connection cannot be reused.
        close = True
        if req is not None and req.method in ("GET", "HEAD"):
            close = False
        elif req is not None and getattr(req, "_body_read", False):
            close = False
        payload = exc.to_dict()
        try:
            self._send(json_response(payload, exc.status), close=close)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send(self, result, *, head_only=False, close=False):
        if isinstance(result, StreamResponse):
            return self._send_stream(result, head_only=head_only)
        if result is None:
            result = json_response({"ok": True})
        self.send_response(result.status)
        for key, value in BASE_HEADERS:
            self.send_header(key, value)
        if result.content_type:
            self.send_header("Content-Type", result.content_type)
        for key, value in result.headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(result.body)))
        if close or result.close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if not head_only and result.body:
            self.wfile.write(result.body)

    def _send_stream(self, result, *, head_only=False):
        self.send_response(result.status)
        for key, value in BASE_HEADERS:
            self.send_header(key, value)
        if result.content_type:
            self.send_header("Content-Type", result.content_type)
        for key, value in result.headers:
            self.send_header(key, value)
        if result.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(result.length or 0))
        self.end_headers()
        if head_only:
            return
        if result.chunked:
            writer = ChunkedWriter(self.wfile)
            try:
                result.writer(writer)
                writer.finish()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as exc:            # noqa: BLE001
                # Headers are already out; all we can do is cut the connection
                # so the client sees a truncated (invalid) response.
                self.app.log("stream failed midway: %s" % exc)
                self.close_connection = True
        else:
            try:
                result.writer(self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True


def _serve_static(app, req):
    """Serve the bundled web UI.  Only an explicit allowlist is reachable."""
    from .api import STATIC_FILES

    path = req.path
    if path == "/":
        path = "/index.html"
    entry = STATIC_FILES.get(path)
    if entry is None:
        raise NotFound("Not found")
    filename, content_type = entry
    full = os.path.join(app.webroot, filename)
    try:
        st = os.stat(full)
    except OSError:
        raise NotFound("Missing web asset %s" % filename)
    # A validator derived from mtime+size means "no-cache" actually revalidates
    # (and a 304 saves re-sending the whole UI on every page load).
    etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
    headers = [
        ("Cache-Control", "no-cache"),
        ("ETag", etag),
        ("Content-Security-Policy", APP_CSP),
    ]
    if req.header("If-None-Match") == etag:
        return Response(304, b"", None, headers)
    with open(full, "rb") as fh:
        body = fh.read()
    return Response(200, body, content_type, headers)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        # Per instance, not per class: a phone should not spawn unbounded
        # threads, and two servers in one process must not share the budget.
        self._slots = threading.Semaphore(MAX_THREADS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(timeout=5):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def serve_forever(app, host, port):
    handler = type("BoundHandler", (Handler,), {"app": app})
    # Keep long uploads alive but drop half-open sockets.
    handler.timeout = 300
    httpd = Server((host, port), handler)
    httpd.serve_forever(poll_interval=0.5)
