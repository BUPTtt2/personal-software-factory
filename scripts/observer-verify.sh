#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=${CODEX_OBSERVER_ROOT:-$(dirname -- "$SCRIPT_DIR")}
PYTHON=${CODEX_OBSERVER_PYTHON:-$(command -v python3 || true)}

if [ "$#" -lt 2 ]; then
  echo "usage: observer-verify.sh PROJECT_CWD COMMAND [ARG ...]" >&2
  exit 2
fi

PROJECT_CWD=$1
shift
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || { echo "Python 3.11+ is required." >&2; exit 1; }
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1 || { echo "Python 3.11+ is required." >&2; exit 1; }
exec "$PYTHON" -m observer.cli verify --root "$ROOT" --cwd "$PROJECT_CWD" -- "$@"
