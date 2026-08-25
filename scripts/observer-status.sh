#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OBSERVER_ROOT=${CODEX_OBSERVER_ROOT:-$(dirname -- "$SCRIPT_DIR")}
cd "$OBSERVER_ROOT"

for candidate in "${CODEX_OBSERVER_PYTHON:-}" "$(command -v python3 || true)" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    exec "$candidate" -m observer.cli status --root "$OBSERVER_ROOT" "$@"
  fi
done
echo "No usable Python 3.11+ runtime found." >&2
exit 1
