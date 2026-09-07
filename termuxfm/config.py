"""Configuration: CLI flag > environment variable > config file > default.

The config file holds the password *hash* only.  It is written 0600 and the
server refuses to start if the mode is looser than that, because on Android
every app-private file still lives under a shared Termux UID.
"""

import json
import os
import secrets
import stat

DEFAULT_ROOT = "~/storage/shared/Server"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_MAX_UPLOAD = 16 * 1024 ** 3          # 16 GiB
DEFAULT_SESSION_TTL = 7 * 24 * 3600          # 7 days
DEFAULT_PBKDF2_ROUNDS = 600_000

_ENV_PREFIX = "TERMUXFM_"


def default_config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "termuxfm", "config.json")


class Config:
    """In-memory view of the effective configuration."""

    FIELDS = {
        "root": str,
        "host": str,
        "port": int,
        "username": str,
        "password_hash": str,
        "password_salt": str,
        "pbkdf2_rounds": int,
        "max_upload_bytes": int,
        "session_ttl": int,
    }

    def __init__(self, path=None, data=None):
        self.path = path or default_config_path()
        self.root = DEFAULT_ROOT
        self.host = DEFAULT_HOST
        self.port = DEFAULT_PORT
        self.username = "admin"
        self.password_hash = ""
        self.password_salt = ""
        self.pbkdf2_rounds = DEFAULT_PBKDF2_ROUNDS
        self.max_upload_bytes = DEFAULT_MAX_UPLOAD
        self.session_ttl = DEFAULT_SESSION_TTL
        if data:
            self.apply(data)

    # -- loading --------------------------------------------------------

    def apply(self, data):
        for key, caster in self.FIELDS.items():
            if key in data and data[key] is not None:
                setattr(self, key, caster(data[key]))
        return self

    @classmethod
    def load(cls, path=None, *, check_mode=True):
        path = path or default_config_path()
        cfg = cls(path=path)
        if os.path.exists(path):
            if check_mode:
                mode = stat.S_IMODE(os.stat(path).st_mode)
                if mode & (stat.S_IRWXG | stat.S_IRWXO):
                    raise SystemExit(
                        "Refusing to start: %s is readable by other users "
                        "(mode %o).\nFix it with:  chmod 600 %s" % (path, mode, path)
                    )
            with open(path, "r", encoding="utf-8") as fh:
                try:
                    cfg.apply(json.load(fh))
                except ValueError as exc:
                    raise SystemExit("Malformed config %s: %s" % (path, exc))
        cfg.apply_env()
        return cfg

    def apply_env(self):
        env_map = {
            "root": "ROOT",
            "host": "HOST",
            "port": "PORT",
            "username": "USERNAME",
            "max_upload_bytes": "MAX_UPLOAD",
            "session_ttl": "SESSION_TTL",
        }
        for key, suffix in env_map.items():
            raw = os.environ.get(_ENV_PREFIX + suffix)
            if raw:
                setattr(self, key, self.FIELDS[key](raw))
        return self

    def apply_args(self, args):
        """Overlay parsed CLI arguments (highest precedence)."""
        for key in ("root", "host", "port", "username", "max_upload_bytes",
                    "session_ttl"):
            value = getattr(args, key, None)
            if value is not None:
                setattr(self, key, value)
        return self

    # -- saving ---------------------------------------------------------

    def save(self):
        payload = {key: getattr(self, key) for key in self.FIELDS}
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp-%s" % secrets.token_hex(4)
        # Create 0600 from the outset -- never a window where it is readable.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return self.path

    @property
    def has_password(self):
        return bool(self.password_hash and self.password_salt)

    def expanded_root(self):
        return os.path.realpath(os.path.expanduser(self.root))
