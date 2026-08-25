from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import platform
from pathlib import Path
from typing import Any


MARKER = "personal-software-factory-codex-observer-v1"


def _default_state_root() -> Path:
    configured = os.environ.get("SOFTWARE_FACTORY_HOME")
    if configured:
        return Path(configured).resolve()
    home = Path.home()
    if platform.system() == "Darwin":
        return (home / "Library/Application Support/PersonalSoftwareFactory").resolve()
    if platform.system() == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"), "PersonalSoftwareFactory").resolve()
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"), "personal-software-factory").resolve()


def _paths() -> tuple[Path, Path, Path, Path]:
    root = Path(os.environ.get("CODEX_OBSERVER_ROOT", Path(__file__).resolve().parents[1])).resolve()
    home = Path(os.environ.get("CODEX_OBSERVER_TEST_HOME", Path.home() / ".codex")).resolve()
    state_root = _default_state_root()
    registry = Path(os.environ.get(
        "CODEX_OBSERVER_REGISTRY", state_root / "config/projects.json",
    )).resolve()
    return root, state_root, registry, home / "hooks.json"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"refusing to overwrite malformed JSON: {path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
        raise ValueError(f"invalid hook document: {path}")
    value.setdefault("hooks", {})
    return value


def _is_observer_group(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = group.get("hooks")
    return isinstance(handlers, list) and any(
        isinstance(handler, dict) and MARKER in str(handler.get("statusMessage", ""))
        for handler in handlers
    )


def _without_observer(document: dict[str, Any]) -> dict[str, Any]:
    copied = json.loads(json.dumps(document))
    hooks = copied.setdefault("hooks", {})
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            raise ValueError(f"hook event must contain a list: {event}")
        remaining = [group for group in groups if not _is_observer_group(group)]
        if remaining:
            hooks[event] = remaining
        else:
            del hooks[event]
    return copied


def _render_template(
    root: Path, python: Path, state_root: Path, registry: Path,
) -> dict[str, Any]:
    raw = (root / "hooks/codex-observer-hooks.json").read_text(encoding="utf-8")
    raw = (
        raw.replace("__PYTHON__", str(python))
        .replace("__OBSERVER_ROOT__", str(root))
        .replace("__STATE_ROOT__", str(state_root))
        .replace("__REGISTRY__", str(registry))
    )
    return json.loads(raw)


def _merge(existing: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    merged = _without_observer(existing)
    hooks = merged.setdefault("hooks", {})
    for event, groups in template["hooks"].items():
        hooks.setdefault(event, []).extend(groups)
    return merged


def _write(target: Path, payload: dict[str, Any], state_root: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup_dir = state_root / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"hooks.json.{time.time_ns()}.bak"
        shutil.copy2(target, backup)
        os.chmod(backup, 0o600)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("install", "uninstall"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--purge-data", action="store_true")
    parser.add_argument("--confirm-purge", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.operation == "uninstall" and args.purge_data != args.confirm_purge:
        raise ValueError("data purge requires both --purge-data and --confirm-purge")
    root, state_root, registry, target = _paths()
    python = Path(os.environ.get("CODEX_OBSERVER_PYTHON", sys.executable)).resolve()
    existing = _load(target)
    if args.operation == "install":
        desired = _merge(existing, _render_template(root, python, state_root, registry))
    else:
        desired = _without_observer(existing)
    print(f"Observer hook target: {target}")
    print(json.dumps(desired, ensure_ascii=False, indent=2))
    if args.preview:
        print("Preview only; no files changed.")
        return 0
    if desired != existing:
        _write(target, desired, state_root)
    if args.operation == "install":
        print("Observer hook configuration written.")
        print("Activation is NOT verified yet.")
        print("Open /hooks in Codex, review the five Observer hooks, and trust the exact definitions.")
        print("Then run scripts/observer-status.sh and the live acceptance procedure.")
    else:
        if args.purge_data:
            shutil.rmtree(state_root / "data", ignore_errors=True)
            shutil.rmtree(state_root / "buffer", ignore_errors=True)
        print("Observer hooks removed; local evidence preserved." if not args.purge_data else "Observer hooks and local evidence removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
