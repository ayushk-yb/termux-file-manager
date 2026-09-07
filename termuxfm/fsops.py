"""Filesystem operations, written for Android's shared storage.

Three things here differ from a textbook implementation, and all three are
because ``~/storage/shared`` is a FUSE/sdcardfs view rather than a POSIX
filesystem:

1. ``os.rename`` raises ``EXDEV`` between Termux-internal storage and shared
   storage, so every move falls back to copy-then-unlink.  A same-volume move
   stays a cheap rename.
2. ``chmod``/``chown``/``utime`` and symlinks are unsupported there.  Metadata
   copying is best-effort and never fails an operation.
3. ``stat`` is not free on FUSE, so a listing does exactly one ``scandir`` pass
   and reuses ``DirEntry.stat()``'s cached result.

``shutil.copytree``/``rmtree`` are deliberately unused: they offer neither
progress nor cancellation, both of which are mandatory for multi-GB media.
"""

import errno
import mimetypes
import os
import re
import stat
import time

from .errors import ApiError, Conflict, Forbidden, InvalidRequest
from .jobs import Cancelled
from .safepath import sanitize_name

CHUNK = 512 * 1024

CONFLICT_FAIL = "fail"
CONFLICT_RENAME = "rename"
CONFLICT_OVERWRITE = "overwrite"
CONFLICT_MODES = (CONFLICT_FAIL, CONFLICT_RENAME, CONFLICT_OVERWRITE)

SEARCH_MAX_RESULTS = 2000
SEARCH_MAX_DEPTH = 24
SEARCH_DEADLINE = 60.0

_VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".wmv", ".flv",
              ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv"}
_AUDIO_EXT = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav",
              ".wma", ".alac", ".aiff"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico",
              ".heic", ".heif", ".avif"}
_TEXT_EXT = {".txt", ".md", ".log", ".json", ".xml", ".yml", ".yaml", ".ini",
             ".conf", ".cfg", ".csv", ".tsv", ".sh", ".py", ".js", ".css",
             ".html", ".htm", ".nfo", ".srt", ".vtt", ".ass", ".sub", ".toml",
             ".env", ".rs", ".c", ".h", ".java", ".sql", ".diff", ".patch"}
_ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst",
                ".iso", ".tgz"}
_DOC_EXT = {".pdf", ".epub", ".mobi", ".doc", ".docx", ".xls", ".xlsx", ".ppt",
            ".pptx", ".odt", ".ods"}

_NUM_RE = re.compile(r"(\d+)")


def classify(name, is_dir):
    """Coarse kind used for sorting, icons and preview routing."""
    if is_dir:
        return "dir"
    ext = os.path.splitext(name)[1].lower()
    if ext in _VIDEO_EXT:
        return "video"
    if ext in _AUDIO_EXT:
        return "audio"
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _TEXT_EXT:
        return "text"
    if ext in _ARCHIVE_EXT:
        return "archive"
    if ext in _DOC_EXT:
        return "document"
    guess = mimetypes.guess_type(name)[0] or ""
    if guess.startswith("video/"):
        return "video"
    if guess.startswith("audio/"):
        return "audio"
    if guess.startswith("image/"):
        return "image"
    if guess.startswith("text/"):
        return "text"
    return "file"


def natural_key(name):
    """Sort key so 'Season 2' precedes 'Season 10'."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in _NUM_RE.split(name)
    ]


# -- listing ------------------------------------------------------------

_SORT_KEYS = {
    "name": lambda e: natural_key(e["name"]),
    "size": lambda e: e["size"],
    "date": lambda e: e["mtime"],
    "type": lambda e: (e["kind"], natural_key(e["name"])),
}


def listdir(sandbox, rel, sort="name", order="asc"):
    path = sandbox.resolve(rel, must_exist=True)
    if not os.path.isdir(path):
        raise InvalidRequest("Not a directory")
    entries = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    is_link = entry.is_symlink()
                    # One stat per entry, from scandir's cache.
                    st = entry.stat(follow_symlinks=not is_link)
                    is_dir = entry.is_dir(follow_symlinks=not is_link)
                except OSError:
                    # A file that vanished mid-listing, or a FUSE hiccup.
                    continue
                entries.append({
                    "name": entry.name,
                    "is_dir": bool(is_dir),
                    "is_link": bool(is_link),
                    "size": 0 if is_dir else int(st.st_size),
                    "mtime": int(st.st_mtime),
                    "kind": classify(entry.name, is_dir),
                })
    except OSError as exc:
        raise ApiError.from_oserror(exc, what="listing")

    key = _SORT_KEYS.get(sort, _SORT_KEYS["name"])
    reverse = order == "desc"
    entries.sort(key=key, reverse=reverse)
    # Directories always lead, regardless of sort direction: on a media server
    # you navigate far more often than you inspect file sizes.
    entries.sort(key=lambda e: not e["is_dir"])
    return {
        "path": sandbox.relpath(path),
        "sort": sort,
        "order": order,
        "entries": entries,
    }


def stat_entry(sandbox, rel):
    path = sandbox.resolve(rel, must_exist=True)
    st = os.lstat(path)
    is_link = stat.S_ISLNK(st.st_mode)
    if is_link:
        try:
            st = os.stat(path)
        except OSError:
            pass
    is_dir = stat.S_ISDIR(st.st_mode)
    name = os.path.basename(path) or "/"
    return {
        "name": name,
        "path": sandbox.relpath(path),
        "is_dir": is_dir,
        "is_link": is_link,
        "size": 0 if is_dir else int(st.st_size),
        "mtime": int(st.st_mtime),
        "kind": classify(name, is_dir),
    }


# -- create / rename ----------------------------------------------------

def mkdir(sandbox, rel, name):
    _, clean, target = sandbox.resolve_parent(rel, name, what="folder name")
    try:
        os.mkdir(target)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise Conflict("A file or folder named %r already exists" % clean)
        raise ApiError.from_oserror(exc, what="folder creation")
    return {"path": sandbox.relpath(target), "name": clean}


def rename(sandbox, rel, new_name):
    source = sandbox.resolve(rel, must_exist=True, allow_root=False)
    parent = os.path.dirname(source)
    clean = sanitize_name(new_name, what="name")
    target = os.path.join(parent, clean)
    if target == source:
        return {"path": sandbox.relpath(source), "name": clean}
    if not sandbox.contains(target):
        raise Forbidden("Path escapes the configured root")
    # Case-only rename on a case-insensitive volume: os.path.lexists() would
    # report the source itself, so compare normalised names first.
    if os.path.lexists(target) and clean.lower() != os.path.basename(source).lower():
        raise Conflict("A file or folder named %r already exists" % clean)
    try:
        os.rename(source, target)
    except OSError as exc:
        raise ApiError.from_oserror(exc, what="rename")
    return {"path": sandbox.relpath(target), "name": clean}


# -- conflict handling --------------------------------------------------

def unique_name(target):
    """``a.mkv`` -> ``a (2).mkv``, skipping names already taken."""
    directory, base = os.path.split(target)
    stem, ext = os.path.splitext(base)
    match = re.match(r"^(.*) \((\d+)\)$", stem)
    counter = 2
    if match:
        stem = match.group(1)
        counter = int(match.group(2)) + 1
    while counter < 10000:
        candidate = os.path.join(directory, "%s (%d)%s" % (stem, counter, ext))
        if not os.path.lexists(candidate):
            return candidate
        counter += 1
    raise Conflict("Could not find a free name for %r" % base)


def apply_conflict(target, mode):
    """Return the path to write, honouring the requested conflict policy."""
    if not os.path.lexists(target):
        return target
    if mode == CONFLICT_RENAME:
        return unique_name(target)
    if mode == CONFLICT_OVERWRITE:
        return target
    raise Conflict("%r already exists" % os.path.basename(target))


# -- measuring ----------------------------------------------------------

def measure(paths, job=None):
    """Count files/dirs/bytes in a set of trees using scandir only."""
    files = dirs = total = 0
    stack = list(paths)
    while stack:
        if job is not None:
            job.check_cancel()
        current = stack.pop()
        try:
            st = os.lstat(current)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            files += 1
            continue
        if stat.S_ISDIR(st.st_mode):
            dirs += 1
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        stack.append(entry.path)
            except OSError:
                pass
        else:
            files += 1
            total += st.st_size
    return {"files": files, "dirs": dirs, "bytes": total}


class _DuCache:
    """Recursive-size cache keyed by (path, directory mtime)."""

    LIMIT = 512

    def __init__(self):
        self._data = {}

    def get(self, path):
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return None
        hit = self._data.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        return None

    def clear(self):
        self._data.clear()

    def put(self, path, value):
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return
        if len(self._data) >= self.LIMIT:
            self._data.clear()
        self._data[path] = (mtime, value)


du_cache = _DuCache()


def du(sandbox, rel, job=None):
    path = sandbox.resolve(rel, must_exist=True)
    cached = du_cache.get(path)
    if cached is not None:
        return dict(cached, cached=True)
    result = measure([path], job=job)
    result["path"] = sandbox.relpath(path)
    du_cache.put(path, result)
    return dict(result, cached=False)


# -- copy ---------------------------------------------------------------

def _copy_file(src, dst, job, *, overwrite):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    with open(src, "rb", buffering=0) as fsrc:
        fd = os.open(dst, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", buffering=0) as fdst:
                while True:
                    if job is not None:
                        job.check_cancel()
                    buf = fsrc.read(CHUNK)
                    if not buf:
                        break
                    fdst.write(buf)
                    if job is not None:
                        job.advance(nbytes=len(buf))
        except BaseException:
            # Never leave a half-written file behind.
            try:
                os.unlink(dst)
            except OSError:
                pass
            raise
    try:
        st = os.stat(src)
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass          # shared storage often refuses this; harmless


def _copy_tree(src, dst, job, mode):
    """Recursively copy ``src`` to ``dst``, merging into an existing directory."""
    try:
        os.mkdir(dst)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        if not os.path.isdir(dst):
            raise Conflict("%r exists and is not a folder" % os.path.basename(dst))
        if mode == CONFLICT_FAIL:
            raise Conflict("%r already exists" % os.path.basename(dst))
    try:
        with os.scandir(src) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as exc:
        job.add_error(src, ApiError.from_oserror(exc, what="copy").message)
        return
    for entry in entries:
        job.check_cancel()
        child_dst = os.path.join(dst, entry.name)
        try:
            if entry.is_symlink():
                # Symlinks are never recreated: on shared storage they cannot
                # exist, and copying one could point outside the sandbox.
                job.add_error(entry.path, "Skipped symbolic link")
                continue
            if entry.is_dir():
                _copy_tree(entry.path, child_dst, job, mode)
            else:
                final = apply_conflict(child_dst, mode)
                _copy_file(entry.path, final, job,
                           overwrite=(final == child_dst
                                      and mode == CONFLICT_OVERWRITE))
                job.advance(items=1, current=entry.name)
        except Cancelled:
            raise
        except Conflict as exc:
            job.add_error(entry.path, exc.message)
        except OSError as exc:
            job.add_error(entry.path,
                          ApiError.from_oserror(exc, what="copy").message)


def _prepare_transfer(sandbox, sources, dest_rel, *, verb):
    dest = sandbox.resolve(dest_rel, must_exist=True)
    if not os.path.isdir(dest):
        raise InvalidRequest("Destination is not a folder")
    resolved = []
    for rel in sources:
        src = sandbox.resolve(rel, must_exist=True, allow_root=False)
        if os.path.isdir(src) and sandbox.is_descendant(src, dest):
            raise InvalidRequest(
                "Cannot %s %r into itself" % (verb, os.path.basename(src))
            )
        resolved.append(src)
    return dest, resolved


def copy(sandbox, sources, dest_rel, mode=CONFLICT_FAIL):
    """Build a job function that copies ``sources`` into ``dest_rel``."""
    if mode not in CONFLICT_MODES:
        raise InvalidRequest("Unknown conflict mode %r" % mode)
    dest, resolved = _prepare_transfer(sandbox, sources, dest_rel, verb="copy")

    def run(job):
        stats = measure(resolved, job=job)
        job.set_total(items=stats["files"], nbytes=stats["bytes"])
        copied = 0
        for src in resolved:
            job.check_cancel()
            target = os.path.join(dest, os.path.basename(src))
            job.advance(current=os.path.basename(src))
            try:
                if os.path.islink(src):
                    job.add_error(src, "Skipped symbolic link")
                    continue
                if os.path.isdir(src):
                    final = target
                    if os.path.lexists(target) and mode == CONFLICT_RENAME:
                        final = unique_name(target)
                    _copy_tree(src, final, job, mode)
                else:
                    final = apply_conflict(target, mode)
                    _copy_file(src, final, job,
                               overwrite=(final == target
                                          and mode == CONFLICT_OVERWRITE))
                    job.advance(items=1)
                copied += 1
            except Cancelled:
                raise
            except Conflict as exc:
                job.add_error(src, exc.message)
            except OSError as exc:
                job.add_error(src, ApiError.from_oserror(exc, what="copy").message)
        du_cache.clear()
        return {"copied": copied, "dest": sandbox.relpath(dest)}

    return run


# -- move ---------------------------------------------------------------

def move(sandbox, sources, dest_rel, mode=CONFLICT_FAIL):
    if mode not in CONFLICT_MODES:
        raise InvalidRequest("Unknown conflict mode %r" % mode)
    dest, resolved = _prepare_transfer(sandbox, sources, dest_rel, verb="move")

    def run(job):
        # Bytes are only counted for entries that need a real copy, which is
        # discovered per entry -- a same-volume move is a metadata operation.
        stats = measure(resolved, job=job)
        job.set_total(items=stats["files"], nbytes=stats["bytes"])
        moved = 0
        for src in resolved:
            job.check_cancel()
            base = os.path.basename(src)
            job.advance(current=base)
            target = os.path.join(dest, base)
            if os.path.realpath(src) == os.path.realpath(target):
                job.add_error(src, "Source and destination are the same")
                continue
            try:
                final = target
                if os.path.lexists(target):
                    if mode == CONFLICT_RENAME:
                        final = unique_name(target)
                    elif mode == CONFLICT_FAIL:
                        raise Conflict("%r already exists" % base)
                    elif os.path.isdir(target) != os.path.isdir(src):
                        raise Conflict(
                            "%r exists with a different type" % base
                        )
                _move_one(src, final, job, mode)
                moved += 1
            except Cancelled:
                raise
            except Conflict as exc:
                job.add_error(src, exc.message)
            except OSError as exc:
                job.add_error(src, ApiError.from_oserror(exc, what="move").message)
        du_cache.clear()
        return {"moved": moved, "dest": sandbox.relpath(dest)}

    return run


def _move_one(src, dst, job, mode):
    """Rename if possible; fall back to copy+delete across volumes."""
    if not (os.path.lexists(dst) and mode == CONFLICT_OVERWRITE):
        try:
            os.rename(src, dst)
            if not os.path.isdir(dst):
                try:
                    job.advance(nbytes=os.path.getsize(dst))
                except OSError:
                    pass
            return
        except OSError as exc:
            # EXDEV: Termux-internal <-> /storage/emulated/0.  EPERM/EACCES/
            # ENOTEMPTY also show up on Android's storage layer where a plain
            # rename is refused but a copy is allowed.
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES,
                                 errno.ENOTEMPTY, errno.EBUSY, errno.EINVAL):
                raise
    if os.path.isdir(src):
        _copy_tree(src, dst, job, mode)
        job.check_cancel()
        _delete_tree(src, job)
    else:
        _copy_file(src, dst, job, overwrite=True)
        job.advance(items=1)
        os.unlink(src)


# -- delete -------------------------------------------------------------

def _delete_tree(path, job):
    """Iterative post-order removal that never follows a symlink."""
    stack = [(path, False)]
    while stack:
        job.check_cancel()
        current, children_done = stack.pop()
        try:
            st = os.lstat(current)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                job.add_error(current, ApiError.from_oserror(
                    exc, what="delete").message)
            continue
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            if not children_done:
                stack.append((current, True))
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            stack.append((entry.path, False))
                except OSError as exc:
                    job.add_error(current, ApiError.from_oserror(
                        exc, what="delete").message)
                continue
            try:
                os.rmdir(current)
                job.advance(items=1, current=os.path.basename(current))
            except OSError as exc:
                job.add_error(current, ApiError.from_oserror(
                    exc, what="delete").message)
        else:
            try:
                size = 0 if stat.S_ISLNK(st.st_mode) else st.st_size
                os.unlink(current)
                job.advance(items=1, nbytes=size,
                            current=os.path.basename(current))
            except OSError as exc:
                job.add_error(current, ApiError.from_oserror(
                    exc, what="delete").message)


def delete(sandbox, paths):
    """Build a job function that permanently deletes ``paths``.

    There is no trash directory: deletion here is irreversible by design, and
    the UI requires the folder's name to be typed before calling this.
    """
    resolved = [
        sandbox.resolve(rel, must_exist=True, allow_root=False) for rel in paths
    ]

    def run(job):
        stats = measure(resolved, job=job)
        job.set_total(items=stats["files"] + stats["dirs"],
                      nbytes=stats["bytes"])
        for path in resolved:
            job.check_cancel()
            _delete_tree(path, job)
        du_cache.clear()
        return {"deleted": len(resolved)}

    return run


# -- search -------------------------------------------------------------

def search(sandbox, rel, query, *, limit=SEARCH_MAX_RESULTS):
    base = sandbox.resolve(rel, must_exist=True)
    needle = (query or "").strip().lower()
    if len(needle) < 1:
        raise InvalidRequest("Search text is required")

    def run(job):
        deadline = time.time() + SEARCH_DEADLINE
        results = []
        truncated = False
        stack = [(base, 0)]
        scanned = 0
        while stack:
            job.check_cancel()
            if time.time() > deadline or len(results) >= limit:
                truncated = True
                break
            current, depth = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        scanned += 1
                        if scanned % 500 == 0:
                            job.check_cancel()
                            job.advance(items=0,
                                        current=sandbox.relpath(current))
                        try:
                            is_link = entry.is_symlink()
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        if needle in entry.name.lower():
                            try:
                                st = entry.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            results.append({
                                "name": entry.name,
                                "path": sandbox.relpath(entry.path),
                                "parent": sandbox.relpath(current),
                                "is_dir": bool(is_dir),
                                "is_link": bool(is_link),
                                "size": 0 if is_dir else int(st.st_size),
                                "mtime": int(st.st_mtime),
                                "kind": classify(entry.name, is_dir),
                            })
                            if len(results) >= limit:
                                truncated = True
                                break
                        # Symlinked directories are not descended into: that is
                        # how a walk would otherwise leave the sandbox or loop.
                        if is_dir and not is_link and depth < SEARCH_MAX_DEPTH:
                            stack.append((entry.path, depth + 1))
            except OSError:
                continue
        job.set_total(items=len(results))
        results.sort(key=lambda r: (not r["is_dir"], natural_key(r["name"])))
        return {
            "query": query,
            "base": sandbox.relpath(base),
            "truncated": truncated,
            "scanned": scanned,
            "entries": results,
        }

    return run
