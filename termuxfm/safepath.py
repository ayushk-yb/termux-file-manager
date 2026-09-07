"""The sandbox boundary.

Nothing else in TermuxFM is allowed to build a filesystem path from client
input.  Every request that names a file goes through :meth:`Sandbox.resolve`,
and every request that *creates* a name goes through :func:`sanitize_name`.

The containment check happens **after** ``os.path.realpath``, which is the part
that matters: a symlink pointing out of the root is rejected rather than
followed.  Android's shared storage cannot hold symlinks at all, but the root is
configurable and may point at Termux-internal storage, which can.
"""

import os
import stat
import unicodedata

from .errors import Forbidden, InvalidName, InvalidRequest, NotFound

#: Characters Android's sdcardfs/FUSE layer refuses in a file name.  Rejecting
#: them up front turns a confusing EINVAL from the kernel into a clear 422.
RESERVED_CHARS = frozenset('"*/:<>?\\|\x00')

#: Most filesystems cap a single path component at 255 *bytes*, not characters.
MAX_NAME_BYTES = 255


def sanitize_name(name, *, what="name"):
    """Validate a single path component destined for the filesystem.

    Returns the NFC-normalised name.  Raises :class:`InvalidName` rather than
    silently rewriting, so the user is never surprised by a file appearing under
    a name they did not choose.
    """
    if not isinstance(name, str):
        raise InvalidName("A %s must be text" % what)
    # Android's storage layer stores NFC; normalising here keeps a name typed on
    # macOS (which hands over NFD) from becoming a second, look-alike entry.
    name = unicodedata.normalize("NFC", name)
    if name in ("", ".", ".."):
        raise InvalidName("%r is not a usable %s" % (name, what))
    if name != name.strip():
        raise InvalidName("A %s cannot begin or end with whitespace" % what)
    if name.endswith("."):
        raise InvalidName("A %s cannot end with a dot" % what)
    bad = sorted(RESERVED_CHARS.intersection(name))
    if bad:
        raise InvalidName(
            "A %s cannot contain %s" % (what, " ".join(repr(c) for c in bad))
        )
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        raise InvalidName("A %s cannot contain control characters" % what)
    if len(name.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES:
        raise InvalidName(
            "A %s cannot be longer than %d bytes" % (what, MAX_NAME_BYTES)
        )
    return name


def split_relpath(rel):
    """Split client-supplied relative path text into safe components.

    Rejects absolute paths and any ``..`` component, wherever it appears.
    ``\\`` is *not* treated as a separator (this is POSIX), but it can never be
    created because :func:`sanitize_name` rejects it.
    """
    if rel is None:
        return []
    if not isinstance(rel, str):
        raise InvalidRequest("Path must be text")
    if "\x00" in rel:
        raise InvalidRequest("Path contains a NUL byte")
    if rel.startswith("/") or rel.startswith("\\"):
        raise Forbidden("Absolute paths are not allowed")
    parts = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise Forbidden("Path traversal is not allowed")
        parts.append(part)
    return parts


class Sandbox:
    """Confines all filesystem access to a single directory tree."""

    def __init__(self, root):
        root = os.path.realpath(os.path.expanduser(str(root)))
        if not os.path.isdir(root):
            raise ValueError("root directory does not exist: %s" % root)
        self.root = root
        # Compared as a prefix so that a sibling named "Server-evil" can never
        # masquerade as a child of "Server".
        self._prefix = root.rstrip(os.sep) + os.sep

    # -- containment ----------------------------------------------------

    def contains(self, abspath):
        return abspath == self.root or abspath.startswith(self._prefix)

    def resolve(self, rel, *, must_exist=False, allow_root=True):
        """Map client-supplied relative text onto an absolute path in the root."""
        parts = split_relpath(rel)
        if not parts and not allow_root:
            raise Forbidden("The root directory cannot be the target here")
        candidate = os.path.realpath(os.path.join(self.root, *parts))
        if not self.contains(candidate):
            # Either a traversal we did not catch textually, or a symlink whose
            # target lies outside the sandbox.
            raise Forbidden("Path escapes the configured root")
        if must_exist and not os.path.lexists(candidate):
            raise NotFound("No such file or directory")
        return candidate

    def resolve_parent(self, rel, name, *, what="name"):
        """Resolve ``rel`` as an existing directory and validate a new ``name``.

        Returns ``(parent_abspath, sanitized_name, child_abspath)``.
        """
        parent = self.resolve(rel, must_exist=True)
        if not os.path.isdir(parent):
            raise InvalidRequest("Parent is not a directory")
        clean = sanitize_name(name, what=what)
        child = os.path.join(parent, clean)
        # Defence in depth: sanitize_name already forbids separators.
        if not self.contains(os.path.realpath(child)):
            raise Forbidden("Path escapes the configured root")
        return parent, clean, child

    # -- presentation ---------------------------------------------------

    def relpath(self, abspath):
        """Render an absolute path inside the root as a client-facing path."""
        if abspath == self.root:
            return ""
        if not self.contains(abspath):
            raise Forbidden("Path escapes the configured root")
        return abspath[len(self._prefix):].replace(os.sep, "/")

    def is_descendant(self, ancestor, candidate):
        """True when ``candidate`` is inside ``ancestor`` (or is it)."""
        a = ancestor.rstrip(os.sep)
        return candidate == a or candidate.startswith(a + os.sep)


def lstat_kind(path):
    """Classify a path without following symlinks. Returns dir/file/link/other."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return "link"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "other"
