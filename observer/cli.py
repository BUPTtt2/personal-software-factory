from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from observer.event_store import EventStore
from observer.config import load_projects
from observer.git_snapshot import resolve_git_executable
from observer.observer import ObserverSettings, ingest_hook
from observer.paths import RuntimePaths, resolve_runtime_paths
from observer.reconcile import AppServerClient, reconcile_stale_turns


def _health_error(root: Path, code: str) -> None:
    path = root / "logs/observer-health.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(), "code": code,
        }, sort_keys=True, separators=(",", ":")) + "\n")


def _settings(paths: RuntimePaths) -> ObserverSettings:
    return ObserverSettings(
        registry_path=paths.registry_path,
        db_path=paths.db_path,
        buffer_dir=paths.buffer_dir,
        git_executable=resolve_git_executable(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-observer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("ingest", "init-db", "replay-buffer", "status", "reconcile", "serve", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--root", type=Path, default=None)
        command.add_argument("--state-root", type=Path, default=None)
        command.add_argument("--registry", type=Path, default=None)
        if name == "ingest":
            command.add_argument("--stdin", action="store_true", required=True)
        if name == "reconcile":
            command.add_argument("--stale-minutes", type=int, default=30)
        if name == "status":
            command.add_argument("--json", action="store_true", dest="as_json")
        if name == "serve":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8765)
            command.add_argument("--open", action="store_true", dest="open_browser")
            command.add_argument("--codex", default=None)
        if name == "verify":
            command.add_argument("--cwd", type=Path, required=True)
            command.add_argument("verification_command", nargs=argparse.REMAINDER)
    return parser


def status_snapshot(paths: RuntimePaths) -> dict[str, object]:
    home = Path(os.environ.get("CODEX_OBSERVER_TEST_HOME", Path.home() / ".codex"))
    hooks_path = home / "hooks.json"
    hook_installed = False
    if hooks_path.exists():
        try:
            hook_installed = "personal-software-factory-codex-observer-v1" in hooks_path.read_text(encoding="utf-8")
        except OSError:
            pass
    db_path = paths.db_path
    event_count = 0
    last_event_at = None
    database_ready = db_path.exists()
    warnings: list[str] = []
    if database_ready:
        try:
            with closing(sqlite3.connect(db_path)) as connection:
                event_count = connection.execute("SELECT count(*) FROM events").fetchone()[0]
                row = connection.execute("SELECT max(occurred_at) FROM events").fetchone()
                last_event_at = row[0] if row else None
        except sqlite3.Error:
            database_ready = False
            warnings.append("database_unreadable")
    else:
        warnings.append("database_not_initialized")
    try:
        registered = sum(project.enabled for project in load_projects(paths.registry_path))
    except Exception:
        registered = 0
        warnings.append("registry_unreadable")
    buffered = len(list(paths.buffer_dir.glob("*.json"))) if paths.buffer_dir.exists() else 0
    return {
        "schemaVersion": 1,
        "hookInstalled": hook_installed,
        "hookTrust": "unknown_manual_check_required",
        "databaseReady": database_ready,
        "registeredProjects": registered,
        "eventCount": event_count,
        "bufferedEventCount": buffered,
        "lastEventAt": last_event_at,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code_root = (args.root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    legacy_registry = code_root / "config/projects.yaml"
    legacy_mode = bool(args.root and args.state_root is None and legacy_registry.exists())
    state_root = args.state_root.resolve(strict=False) if args.state_root else (
        code_root if legacy_mode else None
    )
    registry_path = args.registry.resolve(strict=False) if args.registry else None
    if registry_path is None and legacy_mode:
        registry_path = legacy_registry
    paths = resolve_runtime_paths(code_root, state_root, registry_path)
    try:
        if args.command == "ingest":
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                raise ValueError("hook input must be an object")
            ingest_hook(payload, _settings(paths))
        elif args.command == "init-db":
            store = EventStore(paths.db_path, paths.buffer_dir)
            store.initialize()
        elif args.command == "replay-buffer":
            EventStore(paths.db_path, paths.buffer_dir).replay_buffer()
        elif args.command == "status":
            value = status_snapshot(paths)
            if args.as_json:
                print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            else:
                print(f"Hook installed: {value['hookInstalled']}")
                print(f"Hook trust: {value['hookTrust']}")
                print(f"Database ready: {value['databaseReady']}")
                print(f"Registered projects: {value['registeredProjects']}")
                print(f"Events: {value['eventCount']} (buffered: {value['bufferedEventCount']})")
                print(f"Last event: {value['lastEventAt'] or 'none'}")
                if value["warnings"]:
                    print("Warnings: " + ", ".join(value["warnings"]))
        elif args.command == "reconcile":
            from datetime import timedelta
            codex = os.environ.get("CODEX_OBSERVER_CODEX") or shutil.which("codex") or "codex"
            store = EventStore(paths.db_path, paths.buffer_dir)
            store.initialize()
            with AppServerClient((codex, "app-server", "--listen", "stdio://")) as client:
                report = reconcile_stale_turns(
                    store, client, datetime.now(timezone.utc), timedelta(minutes=args.stale_minutes),
                )
            print(json.dumps({
                "examined": report.examined, "interrupted": report.interrupted,
                "stillActive": report.still_active, "unknown": report.unknown,
                "errors": report.errors,
            }, sort_keys=True, separators=(",", ":")))
        elif args.command == "serve":
            from observer.web import serve
            codex = args.codex or os.environ.get("CODEX_OBSERVER_CODEX") or shutil.which("codex") or "codex"
            serve(
                root=paths.code_root,
                host=args.host,
                port=args.port,
                open_browser=args.open_browser,
                codex_executable=codex,
                state_root=paths.state_root,
                registry_path=paths.registry_path,
            )
        elif args.command == "verify":
            from observer.verification_runner import VerificationRunner
            command = list(args.verification_command)
            if command[:1] == ["--"]:
                command = command[1:]
            if not command:
                raise ValueError("verification command is required")
            result = VerificationRunner(
                paths.state_root, registry_path=paths.registry_path,
            ).run(args.cwd.resolve(strict=False), command)
            print(json.dumps({
                "eventId": result.event_id,
                "projectId": result.project_id,
                "kind": result.kind,
                "commandClass": result.command_class,
                "status": result.status,
                "exitCode": result.exit_code,
            }, sort_keys=True, separators=(",", ":")))
            return result.exit_code
    except Exception as exc:
        _health_error(paths.state_root, type(exc).__name__)
        return 0 if args.command == "ingest" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
