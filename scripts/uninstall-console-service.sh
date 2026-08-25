#!/bin/sh
set -eu

LABEL="dev.personal-software-factory.observer"
SERVICE_HOME=${CONSOLE_SERVICE_HOME:-$HOME}
LAUNCHCTL=${CONSOLE_SERVICE_LAUNCHCTL:-/bin/launchctl}
DOMAIN="gui/$(id -u)"
PLIST="$SERVICE_HOME/Library/LaunchAgents/$LABEL.plist"

usage() {
  echo "usage: uninstall-console-service.sh --preview|--apply" >&2
  exit 2
}

[ "$#" -eq 1 ] || usage
MODE=$1
[ "$MODE" = "--preview" ] || [ "$MODE" = "--apply" ] || usage

if [ "$MODE" = "--preview" ]; then
  echo "Would stop $LABEL and remove $PLIST"
  echo "Observer Hooks and SQLite evidence would be preserved."
  exit 0
fi

"$LAUNCHCTL" bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo "Removed $LABEL; Observer Hooks and SQLite evidence were preserved."
