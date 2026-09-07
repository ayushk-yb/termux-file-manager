"""Command line: ``serve``, ``setup``, ``passwd``, ``check``."""

import argparse
import getpass
import os
import socket
import sys

from . import __version__
from .auth import AuthManager, hash_password
from .config import (DEFAULT_HOST, DEFAULT_MAX_UPLOAD, DEFAULT_PORT,
                     DEFAULT_ROOT, DEFAULT_SESSION_TTL, Config,
                     default_config_path)
from .jobs import JobRegistry
from .safepath import Sandbox

WEBROOT_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"),
    os.path.join(sys.prefix, "opt", "termuxfm", "web"),
    "/data/data/com.termux/files/usr/opt/termuxfm/web",
)

_SUFFIXES = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}


def parse_size(text):
    """Accept ``16G``, ``500M``, ``1048576``."""
    text = str(text).strip().lower().rstrip("b")
    if not text:
        raise argparse.ArgumentTypeError("empty size")
    multiplier = 1
    if text[-1] in _SUFFIXES:
        multiplier = _SUFFIXES[text[-1]]
        text = text[:-1]
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a size: %r" % text)
    if value <= 0:
        raise argparse.ArgumentTypeError("size must be positive")
    return int(value * multiplier)


def find_webroot(explicit=None):
    if explicit:
        if not os.path.isdir(explicit):
            raise SystemExit("--webroot %s does not exist" % explicit)
        return explicit
    for candidate in WEBROOT_CANDIDATES:
        if os.path.isfile(os.path.join(candidate, "index.html")):
            return candidate
    raise SystemExit(
        "Could not find the web/ directory. Pass --webroot /path/to/web"
    )


def lan_address():
    """Best-effort LAN IP, for the startup banner only."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is sent; this just asks the routing table.
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="filemanager",
        description="TermuxFM -- a web file manager for native Termux.",
    )
    parser.add_argument("--version", action="version",
                        version="termuxfm %s" % __version__)
    sub = parser.add_subparsers(dest="command")

    def common(p):
        p.add_argument("--config", default=None,
                       help="config file (default: %s)" % default_config_path())
        p.add_argument("--root", default=None,
                       help="directory to expose (default: %s)" % DEFAULT_ROOT)

    serve = sub.add_parser("serve", help="run the server in the foreground")
    common(serve)
    serve.add_argument("--host", default=None,
                       help="bind address (default: %s)" % DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=None,
                       help="TCP port (default: %d)" % DEFAULT_PORT)
    serve.add_argument("--username", default=None, help="override the username")
    serve.add_argument("--max-upload", dest="max_upload_bytes", type=parse_size,
                       default=None,
                       help="per-file upload limit (default: %d)" % DEFAULT_MAX_UPLOAD)
    serve.add_argument("--session-ttl", dest="session_ttl", type=int, default=None,
                       help="session lifetime in seconds (default: %d)"
                            % DEFAULT_SESSION_TTL)
    serve.add_argument("--webroot", default=None, help="path to the web/ assets")

    setup = sub.add_parser("setup", help="create the config and set a password")
    common(setup)
    setup.add_argument("--host", default=None)
    setup.add_argument("--port", type=int, default=None)
    setup.add_argument("--username", default=None)
    setup.add_argument("--max-upload", dest="max_upload_bytes", type=parse_size,
                       default=None)
    setup.add_argument("--session-ttl", dest="session_ttl", type=int, default=None)
    setup.add_argument("--password-stdin", action="store_true",
                       help="read the password from stdin instead of prompting")
    setup.add_argument("--force", action="store_true",
                       help="overwrite an existing config")

    passwd = sub.add_parser("passwd", help="change the password")
    common(passwd)
    passwd.add_argument("--password-stdin", action="store_true")

    check = sub.add_parser("check", help="validate the configuration and exit")
    common(check)
    return parser


def _read_password(from_stdin, *, confirm=True):
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise SystemExit("No password on stdin")
        return password
    if not sys.stdin.isatty():
        raise SystemExit(
            "No terminal available for a password prompt.\n"
            "Use:  printf '%s' 'yourpassword' | filemanager setup --password-stdin"
        )
    while True:
        # getpass keeps the password out of the terminal and out of history.
        password = getpass.getpass("New password: ")
        if len(password) < 8:
            print("Please use at least 8 characters.", file=sys.stderr)
            continue
        if confirm and password != getpass.getpass("Repeat password: "):
            print("Passwords did not match; try again.", file=sys.stderr)
            continue
        return password


def cmd_setup(args):
    path = args.config or default_config_path()
    exists = os.path.exists(path)
    if exists and not args.force:
        cfg = Config.load(path, check_mode=False)
        if cfg.has_password:
            print("Config already exists at %s (use --force to replace it)." % path)
            return 0
    else:
        cfg = Config(path=path)
    cfg.apply_args(args)
    if args.root is None and not exists:
        cfg.root = DEFAULT_ROOT
    if sys.stdin.isatty() and not args.password_stdin and not args.username:
        typed = input("Username [%s]: " % cfg.username).strip()
        if typed:
            cfg.username = typed
    password = _read_password(args.password_stdin)
    digest, salt, rounds = hash_password(password)
    cfg.password_hash, cfg.password_salt, cfg.pbkdf2_rounds = digest, salt, rounds
    cfg.path = path
    cfg.save()
    print("Wrote %s (mode 600)." % path)
    print("Username: %s" % cfg.username)
    print("Root:     %s" % cfg.root)
    return 0


def cmd_passwd(args):
    path = args.config or default_config_path()
    if not os.path.exists(path):
        raise SystemExit("No config at %s -- run:  filemanager setup" % path)
    cfg = Config.load(path, check_mode=False)
    password = _read_password(args.password_stdin)
    digest, salt, rounds = hash_password(password)
    cfg.password_hash, cfg.password_salt, cfg.pbkdf2_rounds = digest, salt, rounds
    cfg.save()
    print("Password updated. Existing sessions end at the next restart.")
    return 0


def _load_for_serving(args):
    cfg = Config.load(args.config)
    cfg.apply_args(args)
    if not cfg.has_password:
        raise SystemExit(
            "No password configured.\nRun:  filemanager setup"
        )
    root = cfg.expanded_root()
    if not os.path.isdir(root):
        raise SystemExit(
            "Root directory does not exist: %s\n"
            "Create it, or pass --root. If ~/storage is missing, run:  "
            "termux-setup-storage" % root
        )
    return cfg, root


def cmd_check(args):
    cfg, root = _load_for_serving(args)
    print("config:   %s" % cfg.path)
    print("root:     %s" % root)
    print("bind:     %s:%d" % (cfg.host, cfg.port))
    print("username: %s" % cfg.username)
    print("upload limit: %.1f GiB" % (cfg.max_upload_bytes / 1024 ** 3))
    print("webroot:  %s" % find_webroot(getattr(args, "webroot", None)))
    print("OK")
    return 0


def cmd_serve(args):
    from .api import register
    from .httpd import App, serve_forever

    cfg, root = _load_for_serving(args)
    webroot = find_webroot(args.webroot)
    sandbox = Sandbox(root)
    jobs = JobRegistry()
    auth = AuthManager(cfg)
    app = App(cfg, sandbox, auth, jobs, webroot)
    jobs.set_logger(app.log)
    register(app)

    app.log("termuxfm %s starting" % __version__)
    app.log("root      %s" % sandbox.root)
    app.log("webroot   %s" % webroot)
    app.log("user      %s" % cfg.username)
    app.log("listening on http://%s:%d" % (cfg.host, cfg.port))
    if cfg.host in ("0.0.0.0", "::"):
        app.log("LAN URL   http://%s:%d" % (lan_address(), cfg.port))
    try:
        serve_forever(app, cfg.host, cfg.port)
    except KeyboardInterrupt:
        app.log("shutting down")
        return 0
    except OSError as exc:
        raise SystemExit(
            "Could not bind %s:%d -- %s\n"
            "Is another service using the port?  Try:  "
            "netstat -tlnp 2>/dev/null | grep %d"
            % (cfg.host, cfg.port, exc, cfg.port)
        )
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    handlers = {
        "serve": cmd_serve,
        "setup": cmd_setup,
        "passwd": cmd_passwd,
        "check": cmd_check,
    }
    return handlers[args.command](args)
