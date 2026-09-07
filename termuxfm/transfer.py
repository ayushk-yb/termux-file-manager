"""Streaming uploads and downloads.  Nothing here ever holds a file in RAM.

Uploads are **one raw PUT per file** rather than multipart form data.  That is
deliberate: ``cgi.FieldStorage`` was removed in Python 3.13 and buffered whole
uploads anyway, and ``XMLHttpRequest.upload.onprogress`` (which needs a plain
body) is still the only way to get real upload progress in a browser.

The file name travels in a base64url ``X-Filename`` header instead of the query
string, so names containing ``#``, ``?``, ``%`` or non-ASCII text cannot be
mangled by URL parsing or by header charset rules.
"""

import base64
import errno
import os
import secrets
import time

from .errors import (ApiError, InvalidRequest, PayloadTooLarge,
                     RangeNotSatisfiable)
from .fsops import CHUNK, PART_PREFIX, apply_conflict, classify
from .safepath import sanitize_name, split_relpath

TEXT_PREVIEW_LIMIT = 1024 * 1024

#: Android kills background processes.  A partial upload interrupted that way
#: leaves a temp file behind that nothing would ever remove, so anything older
#: than this is considered abandoned and reclaimed.
PART_MAX_AGE = 6 * 3600

#: Only these are ever served inline.  Everything else -- HTML above all -- is
#: forced to download, so a file stored here can never execute as script on the
#: app's own origin.
INLINE_MIME = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
    "image/avif", "image/x-icon",
    "video/mp4", "video/webm", "video/ogg", "video/x-matroska", "video/quicktime",
    "audio/mpeg", "audio/mp4", "audio/ogg", "audio/flac", "audio/wav",
    "audio/x-wav", "audio/aac", "audio/opus", "audio/x-m4a",
    "text/plain",
}

_EXT_MIME = {
    ".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4",
    ".webm": "video/webm", ".mov": "video/quicktime", ".ogv": "video/ogg",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".opus": "audio/opus", ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".bmp": "image/bmp", ".ico": "image/x-icon",
}


def content_type_for(name):
    import mimetypes

    ext = os.path.splitext(name)[1].lower()
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    guessed = mimetypes.guess_type(name)[0]
    if guessed:
        return guessed
    return "application/octet-stream"


def decode_header_name(raw, *, what="file name"):
    """Decode a base64url header value into text."""
    if raw is None:
        raise InvalidRequest("Missing %s header" % what)
    raw = raw.strip()
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError, TypeError):
        raise InvalidRequest("Malformed %s header (expected base64url UTF-8)" % what)


# -- upload -------------------------------------------------------------

def prepare_upload_target(sandbox, dir_rel, filename, rel_path=None,
                          conflict="rename"):
    """Resolve (and create) the destination directory, returning the file path.

    ``rel_path`` carries the browser's ``webkitRelativePath`` for folder
    uploads; every segment is sanitised independently.
    """
    parent = sandbox.resolve(dir_rel, must_exist=True)
    if not os.path.isdir(parent):
        raise InvalidRequest("Upload destination is not a folder")

    if rel_path:
        segments = split_relpath(rel_path)
        # The final segment is the file itself; the rest are directories.
        for segment in segments[:-1]:
            clean = sanitize_name(segment, what="folder name")
            parent = os.path.join(parent, clean)
            if not sandbox.contains(os.path.realpath(parent)):
                from .errors import Forbidden
                raise Forbidden("Path escapes the configured root")
            try:
                os.mkdir(parent)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise ApiError.from_oserror(exc, what="folder creation")
                if not os.path.isdir(parent):
                    from .errors import Conflict
                    raise Conflict("%r exists and is not a folder" % clean)
        if segments:
            filename = segments[-1]

    clean = sanitize_name(filename, what="file name")
    target = os.path.join(parent, clean)
    return apply_conflict(target, conflict)


def receive_upload(stream, target, expected_length, max_bytes, *, on_chunk=None):
    """Stream a request body to ``target`` via a same-directory temp file.

    The temp file lives beside the target so the final ``os.replace`` is on the
    same volume: atomic and instant even for a 40 GB file.
    """
    if expected_length is not None and expected_length > max_bytes:
        raise PayloadTooLarge(
            "Upload is %d bytes; the limit is %d" % (expected_length, max_bytes)
        )
    directory = os.path.dirname(target)
    tmp = os.path.join(directory, PART_PREFIX + secrets.token_hex(8))
    written = 0
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb", buffering=0) as out:
            remaining = expected_length
            while True:
                if remaining is not None and remaining <= 0:
                    break
                want = CHUNK if remaining is None else min(CHUNK, remaining)
                buf = stream.read(want)
                if not buf:
                    break
                written += len(buf)
                if written > max_bytes:
                    raise PayloadTooLarge(
                        "Upload exceeded the %d byte limit" % max_bytes
                    )
                out.write(buf)
                if remaining is not None:
                    remaining -= len(buf)
                if on_chunk is not None:
                    on_chunk(len(buf))
        if expected_length is not None and written != expected_length:
            raise InvalidRequest(
                "Upload truncated: got %d of %d bytes" % (written, expected_length)
            )
        os.replace(tmp, target)
    except BaseException:
        # Aborted transfer, disk full, client vanished: leave nothing behind.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return written


# -- download -----------------------------------------------------------

def parse_range(header, size):
    """Parse a single-range ``Range`` header. Returns ``(start, end)`` or None."""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):].strip()
    if "," in spec:
        # Multipart ranges are legal but no browser needs them for media.
        spec = spec.split(",")[0].strip()
    if "-" not in spec:
        raise RangeNotSatisfiable("Malformed Range header")
    first, _, last = spec.partition("-")
    try:
        if not first:
            # Suffix range: the final N bytes.
            length = int(last)
            if length <= 0:
                raise RangeNotSatisfiable("Malformed Range header")
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        raise RangeNotSatisfiable("Malformed Range header")
    if size == 0:
        raise RangeNotSatisfiable("File is empty")
    if start > end or start >= size:
        raise RangeNotSatisfiable("Range %s is outside the file" % spec)
    return start, min(end, size - 1)


def send_file_range(path, out, start, length):
    """Copy ``length`` bytes from ``path`` at ``start`` into ``out``.

    ``os.sendfile`` avoids copying through user space when the kernel allows it
    (Bionic exposes ``sendfile64``); the read/write loop covers everything else,
    including FUSE-backed files that refuse sendfile.
    """
    with open(path, "rb", buffering=0) as fh:
        fh.seek(start)
        remaining = length
        out_fd = None
        if hasattr(os, "sendfile"):
            try:
                out_fd = out.fileno()
            except (AttributeError, OSError, ValueError):
                out_fd = None
        if out_fd is not None:
            offset = start
            while remaining > 0:
                try:
                    sent = os.sendfile(out_fd, fh.fileno(), offset,
                                       min(remaining, 8 * 1024 * 1024))
                except (OSError, AttributeError):
                    break          # fall through to the portable loop
                if sent == 0:
                    return length - remaining
                offset += sent
                remaining -= sent
            if remaining <= 0:
                return length
            fh.seek(offset)
        while remaining > 0:
            buf = fh.read(min(CHUNK, remaining))
            if not buf:
                break
            out.write(buf)
            remaining -= len(buf)
        return length - remaining


#: Never previewed inline, whatever their MIME type says.  Serving these as
#: ``text/plain`` would be safe in practice (``nosniff`` plus a sandbox CSP),
#: but keeping markup off this origin structurally is one less thing to get
#: wrong later.
NEVER_INLINE_EXT = {".html", ".htm", ".xhtml", ".xht", ".svg", ".svgz",
                    ".xml", ".mhtml"}


def preview_kind(name, size):
    """Decide how the browser should render a preview, if at all."""
    kind = classify(name, False)
    mime = content_type_for(name)
    if os.path.splitext(name)[1].lower() in NEVER_INLINE_EXT:
        return None, mime
    if kind in ("image", "video", "audio") and mime in INLINE_MIME:
        return kind, mime
    if kind == "text":
        # Oversized text is truncated to the cap rather than refused: the head
        # of a big log is exactly what you want to see.
        return "text", "text/plain; charset=utf-8"
    return None, mime


def sweep_partials(root, max_age=PART_MAX_AGE, limit=200000):
    """Reclaim orphaned upload temp files under ``root``.

    Uses ``scandir`` only (no reads) and never follows a symlink.  Returns a
    summary so the caller can log it -- silently deleting nothing is the normal
    case and should stay quiet.
    """
    cutoff = time.time() - max_age
    removed, freed, scanned = 0, 0, 0
    stack = [root]
    while stack and scanned < limit:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    scanned += 1
                    if scanned >= limit:
                        break
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.name.startswith(PART_PREFIX):
                            continue
                        st = entry.stat(follow_symlinks=False)
                        if st.st_mtime > cutoff:
                            continue          # an upload may be in flight
                        size = st.st_size
                        os.unlink(entry.path)
                        removed += 1
                        freed += size
                    except OSError:
                        continue
        except OSError:
            continue
    return {"removed": removed, "freed": freed, "scanned": scanned}


def looks_binary(path, probe=8192):
    """A NUL byte in the first few KB means 'do not render as text'."""
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(probe)
    except OSError as exc:
        raise ApiError.from_oserror(exc, what="preview")
