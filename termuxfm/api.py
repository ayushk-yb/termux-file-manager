"""HTTP routes.

Read-only endpoints are ``GET`` and need no CSRF token.  Everything that
mutates the filesystem is ``POST``/``PUT`` and must echo the session's CSRF
token in ``X-CSRF-Token``; combined with ``SameSite=Strict`` on the cookie, a
page on another origin cannot drive this API even from the same browser.

Long-running work (copy, move, delete, search, recursive size) returns ``202``
with a job id instead of blocking the request.
"""

import os
from urllib.parse import quote

from . import fsops, transfer, ziputil
from .auth import CSRF_HEADER, SESSION_COOKIE
from .errors import ApiError, Forbidden, InvalidRequest, NotFound
from .httpd import FILE_CSP, Response, StreamResponse, json_response

STATIC_FILES = {
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


# -- helpers ------------------------------------------------------------

def _session(app, req):
    return app.auth.require(req.cookie(SESSION_COOKIE))


def _mutating_session(app, req):
    """Authenticate *and* verify CSRF for a state-changing request."""
    session = _session(app, req)
    app.auth.require_csrf(session, req.header(CSRF_HEADER))
    return session


def _job_response(job, status=202):
    return json_response({"job": job.snapshot()}, status)


def _disposition(name, inline=False):
    """RFC 6266 header that survives non-ASCII names in every browser."""
    ascii_fallback = name.encode("ascii", "replace").decode("ascii")
    ascii_fallback = ascii_fallback.replace('"', "_").replace("\\", "_")
    return '%s; filename="%s"; filename*=UTF-8\'\'%s' % (
        "inline" if inline else "attachment",
        ascii_fallback,
        quote(name, safe=""),
    )


def _conflict_mode(data, default=fsops.CONFLICT_FAIL):
    mode = data.get("conflict", default)
    if mode not in fsops.CONFLICT_MODES:
        raise InvalidRequest(
            "conflict must be one of %s" % ", ".join(fsops.CONFLICT_MODES)
        )
    return mode


def register(app):
    """Attach every route to ``app``."""

    # -- session ------------------------------------------------------

    @app.route("GET", "/api/ping")
    def ping(app, req):
        return json_response({"ok": True, "app": "termuxfm"})

    @app.route("POST", "/api/login")
    def login(app, req):
        data = req.json()
        username = data.get("username") or ""
        password = data.get("password") or ""
        if not isinstance(username, str) or not isinstance(password, str):
            raise InvalidRequest("username and password must be text")
        session = app.auth.login(username, password, ip=req.client_ip)
        app.log("login ok for %r from %s" % (session.username, req.client_ip))
        return json_response(
            {"username": session.username, "csrf": session.csrf,
             "root": os.path.basename(app.sandbox.root)},
            headers=[("Set-Cookie", app.auth.cookie_header(session))],
        )

    @app.route("POST", "/api/logout")
    def logout(app, req):
        token = req.cookie(SESSION_COOKIE)
        session = app.auth.get(token)
        if session is not None:
            app.auth.require_csrf(session, req.header(CSRF_HEADER))
            app.auth.logout(token)
        return json_response(
            {"ok": True},
            headers=[("Set-Cookie", app.auth.clear_cookie_header())],
        )

    @app.route("GET", "/api/me")
    def me(app, req):
        session = _session(app, req)
        return json_response({
            "username": session.username,
            "csrf": session.csrf,
            "root": os.path.basename(app.sandbox.root),
            "max_upload_bytes": app.cfg.max_upload_bytes,
            "version": __import__("termuxfm").__version__,
        })

    # -- browsing -----------------------------------------------------

    @app.route("GET", "/api/list")
    def list_dir(app, req):
        _session(app, req)
        sort = req.q("sort", "name")
        order = req.q("order", "asc")
        if sort not in ("name", "size", "date", "type"):
            raise InvalidRequest("Unknown sort field")
        if order not in ("asc", "desc"):
            raise InvalidRequest("order must be asc or desc")
        return json_response(fsops.listdir(app.sandbox, req.q("path", ""),
                                           sort, order))

    @app.route("GET", "/api/stat")
    def stat_one(app, req):
        _session(app, req)
        return json_response(fsops.stat_entry(app.sandbox, req.q("path", "")))

    # -- mutations ----------------------------------------------------

    @app.route("POST", "/api/mkdir")
    def mkdir(app, req):
        _mutating_session(app, req)
        data = req.json()
        name = data.get("name")
        if not isinstance(name, str):
            raise InvalidRequest("name is required")
        return json_response(
            fsops.mkdir(app.sandbox, data.get("path", ""), name), 201
        )

    @app.route("POST", "/api/rename")
    def rename(app, req):
        _mutating_session(app, req)
        data = req.json()
        path = data.get("path")
        name = data.get("name")
        if not isinstance(path, str) or not isinstance(name, str):
            raise InvalidRequest("path and name are required")
        return json_response(fsops.rename(app.sandbox, path, name))

    @app.route("POST", "/api/copy")
    def copy(app, req):
        _mutating_session(app, req)
        data = req.json()
        sources = req.str_list(data, "paths")
        dest = data.get("dest", "")
        if not isinstance(dest, str):
            raise InvalidRequest("dest must be text")
        runner = fsops.copy(app.sandbox, sources, dest, _conflict_mode(data))
        label = "Copying %d item%s" % (len(sources), "" if len(sources) == 1 else "s")
        return _job_response(app.jobs.submit("copy", label, runner))

    @app.route("POST", "/api/move")
    def move(app, req):
        _mutating_session(app, req)
        data = req.json()
        sources = req.str_list(data, "paths")
        dest = data.get("dest", "")
        if not isinstance(dest, str):
            raise InvalidRequest("dest must be text")
        runner = fsops.move(app.sandbox, sources, dest, _conflict_mode(data))
        label = "Moving %d item%s" % (len(sources), "" if len(sources) == 1 else "s")
        return _job_response(app.jobs.submit("move", label, runner))

    @app.route("POST", "/api/delete")
    def delete(app, req):
        _mutating_session(app, req)
        data = req.json()
        paths = req.str_list(data, "paths")
        # The UI requires the folder name to be typed; this is the server-side
        # half of that contract, so a stray API call cannot wipe a tree.
        if not data.get("confirm"):
            raise InvalidRequest(
                "Deletion is permanent; resend with \"confirm\": true"
            )
        runner = fsops.delete(app.sandbox, paths)
        label = "Deleting %d item%s" % (len(paths), "" if len(paths) == 1 else "s")
        return _job_response(app.jobs.submit("delete", label, runner))

    # -- scans --------------------------------------------------------

    @app.route("POST", "/api/search")
    def search(app, req):
        _mutating_session(app, req)
        data = req.json()
        query = data.get("query")
        if not isinstance(query, str):
            raise InvalidRequest("query is required")
        runner = fsops.search(app.sandbox, data.get("path", ""), query)
        return _job_response(
            app.jobs.submit("search", "Searching for %r" % query, runner,
                            lane="scan")
        )

    @app.route("POST", "/api/du")
    def du(app, req):
        _mutating_session(app, req)
        data = req.json()
        path = data.get("path", "")
        if not isinstance(path, str):
            raise InvalidRequest("path must be text")
        target = app.sandbox.resolve(path, must_exist=True)

        def runner(job):
            return fsops.du(app.sandbox, path, job=job)

        label = "Measuring %s" % (os.path.basename(target) or "root")
        return _job_response(
            app.jobs.submit("du", label, runner, lane="scan")
        )

    # -- jobs ---------------------------------------------------------

    @app.route("GET", "/api/job")
    def job_status(app, req):
        _session(app, req)
        job_id = req.q("id")
        if not job_id:
            return json_response({"jobs": app.jobs.active()})
        job = app.jobs.get(job_id)
        if job is None:
            raise NotFound("No such job (it may have expired)")
        return json_response({"job": job.snapshot()})

    @app.route("POST", "/api/job/cancel")
    def job_cancel(app, req):
        _mutating_session(app, req)
        job_id = req.json().get("id")
        if not isinstance(job_id, str) or not app.jobs.cancel(job_id):
            raise NotFound("No such job")
        return json_response({"ok": True})

    # -- upload -------------------------------------------------------

    @app.route("PUT", "/api/upload")
    def upload(app, req):
        _mutating_session(app, req)
        dir_rel = req.q("dir", "")
        filename = transfer.decode_header_name(req.header("X-Filename"))
        rel_header = req.header("X-Rel-Path")
        rel_path = (transfer.decode_header_name(rel_header, what="relative path")
                    if rel_header else None)
        conflict = req.q("conflict", fsops.CONFLICT_RENAME)
        if conflict not in fsops.CONFLICT_MODES:
            raise InvalidRequest("Unknown conflict mode")
        length = req.content_length
        if length is None:
            raise InvalidRequest("Content-Length is required for uploads")
        target = transfer.prepare_upload_target(
            app.sandbox, dir_rel, filename, rel_path, conflict
        )
        written = transfer.receive_upload(
            req.handler.rfile, target, length, app.cfg.max_upload_bytes
        )
        req.mark_body_read()
        fsops.du_cache.clear()
        app.log("uploaded %s (%d bytes)" % (app.sandbox.relpath(target), written))
        return json_response({
            "path": app.sandbox.relpath(target),
            "name": os.path.basename(target),
            "size": written,
        }, 201)

    # -- download / preview / zip -------------------------------------

    def _file_stream(app, req, *, inline):
        path = app.sandbox.resolve(req.q("path", ""), must_exist=True,
                                   allow_root=False)
        if os.path.islink(path):
            raise Forbidden("Refusing to serve a symbolic link")
        if os.path.isdir(path):
            raise InvalidRequest("That is a folder; use /api/zip")
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise ApiError.from_oserror(exc, what="download")
        name = os.path.basename(path)
        content_type = transfer.content_type_for(name)
        if inline and content_type not in transfer.INLINE_MIME:
            # Anything that could execute in the browser is downloaded, never
            # rendered on this origin.
            inline = False
        rng = transfer.parse_range(req.header("Range"), size)
        headers = [
            ("Accept-Ranges", "bytes"),
            ("Content-Disposition", _disposition(name, inline)),
            ("Content-Security-Policy", FILE_CSP),
            ("Cache-Control", "private, max-age=0, must-revalidate"),
        ]
        if rng is None:
            start, end = 0, size - 1 if size else 0
            length = size
            status = 200
        else:
            start, end = rng
            length = end - start + 1
            status = 206
            headers.append(("Content-Range", "bytes %d-%d/%d" % (start, end, size)))

        def writer(out):
            transfer.send_file_range(path, out, start, length)

        return StreamResponse(writer, status=status, content_type=content_type,
                              length=length, headers=headers)

    @app.route("GET", "/api/download")
    def download(app, req):
        _session(app, req)
        return _file_stream(app, req, inline=False)

    @app.route("GET", "/api/preview")
    def preview(app, req):
        _session(app, req)
        path = app.sandbox.resolve(req.q("path", ""), must_exist=True,
                                   allow_root=False)
        if os.path.isdir(path) or os.path.islink(path):
            raise InvalidRequest("No preview for that entry")
        name = os.path.basename(path)
        size = os.path.getsize(path)
        kind, mime = transfer.preview_kind(name, size)
        if kind is None:
            raise InvalidRequest("No preview available for %r" % name)
        if kind == "text":
            if transfer.looks_binary(path):
                raise InvalidRequest("File looks binary; not previewing as text")
            with open(path, "rb") as fh:
                blob = fh.read(transfer.TEXT_PREVIEW_LIMIT)
            text = blob.decode("utf-8", "replace")
            return Response(200, text, "text/plain; charset=utf-8", [
                ("Content-Disposition", _disposition(name, True)),
                ("Content-Security-Policy", FILE_CSP),
                ("X-Preview-Truncated", "1" if size > len(blob) else "0"),
            ])
        return _file_stream(app, req, inline=True)

    @app.route("GET", "/api/zip")
    def zip_download(app, req):
        _session(app, req)
        path = app.sandbox.resolve(req.q("path", ""), must_exist=True)
        if os.path.islink(path):
            raise Forbidden("Refusing to archive a symbolic link")
        base = os.path.basename(path) or "Server"
        compress = req.q("compress") == "1"

        def writer(out):
            ziputil.write_zip(out, path, compress=compress)

        return StreamResponse(
            writer,
            content_type="application/zip",
            chunked=True,
            headers=[
                ("Content-Disposition", _disposition(base + ".zip")),
                ("Content-Security-Policy", FILE_CSP),
                ("Cache-Control", "no-store"),
            ],
        )

    return app
