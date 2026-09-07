#!/data/data/com.termux/files/usr/bin/bash
#
# Removes TermuxFM. Your files under the server root are never touched.

set -euo pipefail

APP_NAME="termuxfm"
SERVICE_NAME="filemanager"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '\033[32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[33m  !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: ./scripts/uninstall-termux.sh [--purge]

  --purge   also delete the config file (username and password hash)

Files under the server root are never deleted by this script.
EOF
      exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[ -n "${PREFIX:-}" ] || die "PREFIX is not set; run this inside Termux."

SERVICE_DIR="$PREFIX/var/service/$SERVICE_NAME"
OPT_DIR="$PREFIX/opt/$APP_NAME"
BIN_PATH="$PREFIX/bin/$SERVICE_NAME"
LOG_DIR="$PREFIX/var/log/$SERVICE_NAME"
CONFIG_DIR="$HOME/.config/$APP_NAME"

bold "Uninstalling TermuxFM"

if command -v sv >/dev/null 2>&1 && [ -d "$SERVICE_DIR" ]; then
  # Stop the supervised process before the service directory disappears,
  # otherwise runit keeps restarting it.
  sv force-down "$SERVICE_NAME" >/dev/null 2>&1 || true
  sleep 1
  ok "service stopped"
fi

if [ -d "$SERVICE_DIR" ]; then
  rm -rf "$SERVICE_DIR"
  ok "removed $SERVICE_DIR"
fi

for target in "$BIN_PATH" "$OPT_DIR" "$LOG_DIR"; do
  if [ -e "$target" ]; then
    rm -rf "$target"
    ok "removed $target"
  fi
done

if [ "$PURGE" = "1" ]; then
  if [ -d "$CONFIG_DIR" ]; then
    rm -rf "$CONFIG_DIR"
    ok "removed $CONFIG_DIR"
  fi
else
  if [ -d "$CONFIG_DIR" ]; then
    info "kept your config at $CONFIG_DIR (use --purge to delete it)"
  fi
fi

printf '\n'
bold "Done. Your files were not touched."
printf '\n'
warn "Remove this line from ~/.termux/boot/start-server.sh if it is there:"
info "    sv up $SERVICE_NAME"
