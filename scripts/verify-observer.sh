#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OBSERVER_ROOT=$(dirname -- "$SCRIPT_DIR")

PYTHON=""
for candidate in "${CODEX_OBSERVER_PYTHON:-}" "$(command -v python3 || true)" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PYTHON=$candidate
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "No usable Python 3.11+ runtime found." >&2
  exit 1
fi

cd "$OBSERVER_ROOT"
"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" -m observer.cli init-db --root "$OBSERVER_ROOT"
"$PYTHON" -m observer.cli status --root "$OBSERVER_ROOT" --json
CODEX_OBSERVER_PYTHON="$PYTHON" "$OBSERVER_ROOT/scripts/install-observer.sh" --preview
