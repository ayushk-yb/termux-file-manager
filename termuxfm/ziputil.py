"""Streaming ZIP of a subtree.

``zipfile.ZipFile`` supports an unseekable output stream (it emits data
descriptors instead of back-patching local headers), which is what lets a whole
folder be zipped straight into the HTTP response without a temp file and
without knowing the total size in advance.

Stored (uncompressed) is the default: the payload is almost always already
compressed media, and a phone CPU spent on deflate for a 40 GB folder is a
waste of both time and battery.
"""

import os
import zipfile

from .fsops import CHUNK

MAX_ENTRIES = 20000


def iter_tree(root):
    """Yield ``(abspath, arcname)`` for every regular file under ``root``.

    Symlinks are skipped: they cannot exist on shared storage, and following one
    is exactly how an archive would end up containing files from outside the
    sandbox.
    """
    root = root.rstrip(os.sep)
    base = os.path.basename(root) or "archive"
    if os.path.isfile(root) and not os.path.islink(root):
        yield root, base
        return
    count = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                rel = os.path.relpath(entry.path, root)
                arcname = os.path.join(base, rel).replace(os.sep, "/")
                if entry.is_dir():
                    stack.append(entry.path)
                    continue
                if not entry.is_file():
                    continue
            except OSError:
                continue
            count += 1
            if count > MAX_ENTRIES:
                return
            yield entry.path, arcname


def write_zip(out, root, *, compress=False):
    """Write a ZIP of ``root`` into the file-like ``out``. Returns bytes read."""
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    total = 0
    with zipfile.ZipFile(out, "w", compression=method,
                         allowZip64=True) as archive:
        for path, arcname in iter_tree(root):
            try:
                info = zipfile.ZipInfo.from_file(path, arcname)
            except OSError:
                continue
            info.compress_type = method
            try:
                with open(path, "rb", buffering=0) as src, \
                        archive.open(info, "w") as dst:
                    while True:
                        buf = src.read(CHUNK)
                        if not buf:
                            break
                        dst.write(buf)
                        total += len(buf)
            except OSError:
                # A file removed mid-archive should not abort the download.
                continue
    return total
