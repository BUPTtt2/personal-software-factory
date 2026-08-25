#!/bin/sh
set -eu

LABEL="dev.personal-software-factory.observer"
PORT=${CONSOLE_SERVICE_PORT:-8765}
LAUNCHCTL=${CONSOLE_SERVICE_LAUNCHCTL:-/bin/launchctl}
DOMAIN="gui/$(id -u)"

if "$LAUNCHCTL" print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "Service: loaded"
else
  echo "Service: not loaded"
  exit 1
fi

if /usr/bin/curl --silent --show-error --fail --max-time 2 "http://127.0.0.1:$PORT/api/health"; then
  echo
else
  echo "Health: unavailable" >&2
  exit 1
fi
