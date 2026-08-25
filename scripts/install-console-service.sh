#!/bin/sh
set -eu

LABEL="dev.personal-software-factory.observer"
SERVICE_HOME=${CONSOLE_SERVICE_HOME:-$HOME}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=${CONSOLE_SERVICE_ROOT:-$(dirname -- "$SCRIPT_DIR")}
STATE_ROOT=${CONSOLE_SERVICE_STATE_ROOT:-${SOFTWARE_FACTORY_HOME:-$SERVICE_HOME/Library/Application Support/PersonalSoftwareFactory}}
PYTHON=${CONSOLE_SERVICE_PYTHON:-$(command -v python3 || true)}
CODEX=${CONSOLE_SERVICE_CODEX:-$(command -v codex || true)}
PORT=${CONSOLE_SERVICE_PORT:-8765}
LAUNCHCTL=${CONSOLE_SERVICE_LAUNCHCTL:-/bin/launchctl}
DOMAIN="gui/$(id -u)"
AGENT_DIR="$SERVICE_HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"
REGISTRY=${CONSOLE_SERVICE_REGISTRY:-$STATE_ROOT/config/projects.json}
LOG_DIR="$STATE_ROOT/logs"

usage() {
  echo "usage: install-console-service.sh --preview|--apply" >&2
  exit 2
}

[ "$#" -eq 1 ] || usage
MODE=$1
[ "$MODE" = "--preview" ] || [ "$MODE" = "--apply" ] || usage

if [ "$MODE" = "--preview" ]; then
  echo "Would install $LABEL at $PLIST"
  echo "Would serve http://127.0.0.1:$PORT/ from $ROOT"
  exit 0
fi

[ -x "$PYTHON" ] || { echo "Python is not executable: $PYTHON" >&2; exit 1; }
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1 || { echo "Python 3.11+ is required: $PYTHON" >&2; exit 1; }
[ -x "$CODEX" ] || { echo "Codex is not executable: $CODEX" >&2; exit 1; }
[ -f "$ROOT/observer/cli.py" ] || { echo "Observer root is invalid: $ROOT" >&2; exit 1; }

SERVICE_INFO=$("$LAUNCHCTL" print "$DOMAIN/$LABEL" 2>/dev/null || true)
if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  SERVICE_PID=$(printf '%s\n' "$SERVICE_INFO" | /usr/bin/awk '/pid = / { print $3; exit }')
  LISTEN_PIDS=$(/usr/sbin/lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN | /usr/bin/sort -u)
  if [ -z "$SERVICE_PID" ] || [ "$LISTEN_PIDS" != "$SERVICE_PID" ]; then
    echo "Port $PORT is already in use by another process." >&2
    exit 1
  fi
fi

mkdir -p "$AGENT_DIR" "$LOG_DIR"
TEMP_PLIST=$(mktemp "$AGENT_DIR/.observer-console.XXXXXX")
OLD_PLIST=""
if [ -f "$PLIST" ]; then
  OLD_PLIST=$(mktemp "$AGENT_DIR/.observer-console-backup.XXXXXX")
  cp "$PLIST" "$OLD_PLIST"
fi
trap 'rm -f "$TEMP_PLIST" ${OLD_PLIST:+"$OLD_PLIST"}' EXIT HUP INT TERM
cat >"$TEMP_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>observer.cli</string>
    <string>serve</string>
    <string>--root</string>
    <string>$ROOT</string>
    <string>--state-root</string>
    <string>$STATE_ROOT</string>
    <string>--registry</string>
    <string>$REGISTRY</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>$PORT</string>
    <string>--codex</string>
    <string>$CODEX</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/console-service.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/console-service-error.log</string>
</dict>
</plist>
EOF
chmod 600 "$TEMP_PLIST"
mv "$TEMP_PLIST" "$PLIST"
trap - EXIT HUP INT TERM

"$LAUNCHCTL" bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
if ! "$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST"; then
  rm -f "$PLIST"
  if [ -n "$OLD_PLIST" ] && [ -f "$OLD_PLIST" ]; then
    mv "$OLD_PLIST" "$PLIST"
    OLD_PLIST=""
    "$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
  fi
  echo "Failed to start $LABEL; previous configuration was restored." >&2
  exit 1
fi
rm -f ${OLD_PLIST:+"$OLD_PLIST"}
OLD_PLIST=""
echo "Installed $LABEL"
echo "Console: http://127.0.0.1:$PORT/"
