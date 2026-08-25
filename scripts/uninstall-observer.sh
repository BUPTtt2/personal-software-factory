#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OBSERVER_ROOT=${CODEX_OBSERVER_ROOT:-$(dirname -- "$SCRIPT_DIR")}

find_python() {
  for candidate in "${CODEX_OBSERVER_PYTHON:-}" "$(command -v python3 || true)" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

PYTHON=$(find_python) || { echo "No usable Python 3.11+ runtime found." >&2; exit 1; }
CODEX_OBSERVER_ROOT="$OBSERVER_ROOT" CODEX_OBSERVER_PYTHON="$PYTHON" exec "$PYTHON" "$OBSERVER_ROOT/observer/hook_install.py" uninstall "$@"
