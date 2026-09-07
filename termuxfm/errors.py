"""Typed application errors that map cleanly onto HTTP status codes.

Every failure path in TermuxFM raises one of these instead of leaking a raw
OSError to the client.  ``ApiError.from_oserror`` translates errno values --
including the ones that are specific to Android's FUSE-backed shared storage --
into messages a person can act on.
"""

import errno as _errno


class ApiError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message=None, *, status=None, code=None, extra=None):
        super().__init__(message or self.__class__.__doc__ or self.code)
        self.message = message or "Internal error"
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.extra = extra or {}

    def to_dict(self):
        d = {"error": self.code, "message": self.message}
        d.update(self.extra)
        return d

    @staticmethod
    def from_oserror(exc, *, what="operation"):
        """Map an OSError onto the closest ApiError."""
        e = exc.errno
        if e == _errno.ENOENT:
            return NotFound("No such file or directory")
        if e == _errno.EEXIST:
            return Conflict("Already exists")
        if e in (_errno.EACCES, _errno.EPERM):
            return Forbidden("Permission denied by Android")
        if e == _errno.ENOSPC:
            return ApiError("Storage is full", status=507, code="no_space")
        if e == _errno.EDQUOT:
            return ApiError("Storage quota exceeded", status=507, code="no_space")
        if e == _errno.ENOTEMPTY:
            return Conflict("Directory is not empty")
        if e == _errno.EISDIR:
            return InvalidRequest("Target is a directory")
        if e == _errno.ENOTDIR:
            return InvalidRequest("Target is not a directory")
        if e == _errno.ENAMETOOLONG:
            return InvalidName("Name is too long for this filesystem")
        if e == _errno.EINVAL:
            # Android's sdcardfs/FUSE rejects some names outright.
            return InvalidName(
                "This filesystem rejected the name (Android shared storage "
                "disallows characters like \" * : < > ? \\ | )"
            )
        if e == _errno.EXDEV:
            return ApiError("Cross-volume %s failed" % what, status=500, code="cross_device")
        if e == _errno.ELOOP:
            return Forbidden("Too many symbolic links")
        if e == _errno.EROFS:
            return Forbidden("Filesystem is read-only")
        return ApiError("%s failed: %s" % (what.capitalize(), exc.strerror or exc), status=500)


class InvalidRequest(ApiError):
    status = 400
    code = "bad_request"


class Unauthorized(ApiError):
    status = 401
    code = "unauthorized"


class Forbidden(ApiError):
    status = 403
    code = "forbidden"


class NotFound(ApiError):
    status = 404
    code = "not_found"


class MethodNotAllowed(ApiError):
    status = 405
    code = "method_not_allowed"


class Conflict(ApiError):
    status = 409
    code = "conflict"


class PayloadTooLarge(ApiError):
    status = 413
    code = "too_large"


class InvalidName(ApiError):
    status = 422
    code = "invalid_name"


class RangeNotSatisfiable(ApiError):
    status = 416
    code = "bad_range"


class TooManyRequests(ApiError):
    status = 429
    code = "too_many_requests"
