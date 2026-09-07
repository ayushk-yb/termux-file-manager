"""Password verification, sessions, CSRF tokens and login throttling.

Design notes:

* The password is stored only as ``pbkdf2_hmac('sha256', ...)`` with a random
  salt.  A plaintext password never appears in the source, in ``argv`` (which is
  world-readable through ``/proc``), or in the config file.
* Sessions live in memory.  Restarting the service logs everyone out, which on a
  single-user LAN box is a feature rather than a problem -- there is no session
  store on disk to leak.
* CSRF uses a double-submit token plus ``SameSite=Strict``: a mutating request
  must echo the session's token in ``X-CSRF-Token``.  All ``GET``/``HEAD``
  endpoints are side-effect free, so they need no token.
"""

import base64
import hashlib
import hmac
import secrets
import threading
import time

from .errors import Forbidden, TooManyRequests, Unauthorized

SESSION_COOKIE = "termuxfm_session"
CSRF_HEADER = "X-CSRF-Token"

_MAX_FAILURES = 5
_BASE_LOCKOUT = 2.0        # seconds; doubles per failure beyond the threshold
_MAX_LOCKOUT = 300.0
_MAX_SESSIONS = 64


def hash_password(password, *, salt=None, rounds=None):
    """Return ``(hash_b64, salt_b64, rounds)``."""
    from .config import DEFAULT_PBKDF2_ROUNDS

    rounds = rounds or DEFAULT_PBKDF2_ROUNDS
    salt_bytes = base64.b64decode(salt) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt_bytes, rounds
    )
    return (
        base64.b64encode(digest).decode("ascii"),
        base64.b64encode(salt_bytes).decode("ascii"),
        rounds,
    )


def verify_password(password, cfg):
    if not cfg.has_password:
        return False
    try:
        expected = base64.b64decode(cfg.password_hash)
        salt = base64.b64decode(cfg.password_salt)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, cfg.pbkdf2_rounds
    )
    return hmac.compare_digest(digest, expected)


class Session:
    __slots__ = ("token", "csrf", "username", "created", "last_seen")

    def __init__(self, username, now=None):
        self.token = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.username = username
        # The clock is supplied by the manager so expiry stays consistent (and
        # testable) rather than mixing injected and wall-clock time.
        self.created = time.time() if now is None else now
        self.last_seen = self.created


class AuthManager:
    """Thread-safe session table with per-IP login throttling."""

    def __init__(self, cfg, *, clock=time.time):
        self.cfg = cfg
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions = {}
        self._failures = {}      # ip -> [count, blocked_until]

    # -- throttling -----------------------------------------------------

    def _check_throttle(self, ip):
        entry = self._failures.get(ip)
        if not entry:
            return
        count, until = entry
        now = self._clock()
        if until > now:
            raise TooManyRequests(
                "Too many failed logins. Try again in %d seconds."
                % int(until - now + 0.5)
            )
        if count >= _MAX_FAILURES:
            # Window elapsed: keep the count but let one attempt through.
            self._failures[ip] = [count, 0.0]

    def _record_failure(self, ip):
        count = self._failures.get(ip, [0, 0.0])[0] + 1
        until = 0.0
        if count >= _MAX_FAILURES:
            backoff = min(
                _BASE_LOCKOUT * (2 ** (count - _MAX_FAILURES)), _MAX_LOCKOUT
            )
            until = self._clock() + backoff
        self._failures[ip] = [count, until]

    # -- login / logout -------------------------------------------------

    def login(self, username, password, *, ip="?"):
        with self._lock:
            self._check_throttle(ip)
        if not self.cfg.has_password:
            raise Unauthorized(
                "No password is configured. Run:  filemanager setup"
            )
        # Compare the username in constant time too, so the response time does
        # not reveal whether the account name was right.
        user_ok = hmac.compare_digest(
            (username or "").encode("utf-8"), self.cfg.username.encode("utf-8")
        )
        pass_ok = verify_password(password or "", self.cfg)
        if not (user_ok and pass_ok):
            with self._lock:
                self._record_failure(ip)
            raise Unauthorized("Invalid username or password")
        with self._lock:
            self._failures.pop(ip, None)
            self._reap_locked()
            if len(self._sessions) >= _MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
                self._sessions.pop(oldest.token, None)
            session = Session(self.cfg.username, self._clock())
            self._sessions[session.token] = session
            return session

    def logout(self, token):
        with self._lock:
            return self._sessions.pop(token, None) is not None

    # -- validation -----------------------------------------------------

    def _reap_locked(self):
        cutoff = self._clock() - self.cfg.session_ttl
        for token in [t for t, s in self._sessions.items() if s.last_seen < cutoff]:
            self._sessions.pop(token, None)

    def get(self, token):
        if not token:
            return None
        with self._lock:
            self._reap_locked()
            session = self._sessions.get(token)
            if session is not None:
                session.last_seen = self._clock()
            return session

    def require(self, token):
        session = self.get(token)
        if session is None:
            raise Unauthorized("Please log in")
        return session

    def require_csrf(self, session, supplied):
        if not supplied or not hmac.compare_digest(
            str(supplied), session.csrf
        ):
            raise Forbidden("Missing or invalid CSRF token")

    def cookie_header(self, session):
        return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (
            SESSION_COOKIE, session.token, self.cfg.session_ttl
        )

    def clear_cookie_header(self):
        return "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" % SESSION_COOKIE

    @property
    def session_count(self):
        with self._lock:
            return len(self._sessions)
