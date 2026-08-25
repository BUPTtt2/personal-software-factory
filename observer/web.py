from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from observer.action_policy import is_current_action_status
from observer.agent_runtime import AgentRunNotAllowed, AgentRunStore, CodexSupervisorRunner
from observer.config import ProjectConfig, load_projects
from observer.console_model import ProjectSnapshot, build_overview
from observer.event_store import EventStore
from observer.maintenance import MaintenanceLoop
from observer.project_work import (
    ActionConfirmationRejected,
    EvidenceAdvancedConflict,
    ActionItem,
    ActionStateConflict,
    IntentVersionConflict,
    ProjectIntent,
    ProjectWorkEvaluator,
    ProjectWorkStore,
)
from observer.redact import detect_sensitive_output


_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_MAX_BODY_BYTES = 4096
_PUBLIC_TEXT_FALLBACK = "受保护的历史摘要"
_PUBLIC_BODY_MARKERS = re.compile(
    r"```|(?:^|\s)(?:def|class|function|const|let|var|import|from|export)\s+"
    r"|(?:system|user|assistant)\s+prompt\b|tool\s+(?:output|result)\b|\benv(?:ironment)?\s*=",
    re.IGNORECASE,
)


class ConsoleServer(ThreadingHTTPServer):
    maintenance: MaintenanceLoop | None = None

    def server_close(self) -> None:
        if self.maintenance:
            self.maintenance.stop()
        super().server_close()


def create_server(
    root: Path,
    host: str,
    port: int,
    codex_executable: str,
    *,
    state_root: Path | None = None,
    registry_path: Path | None = None,
) -> ConsoleServer:
    code_root = root.resolve(strict=False)
    state = (state_root or code_root).resolve(strict=False)
    registry = (registry_path or state / "config/projects.yaml").resolve(strict=False)
    db_path = state / "data/factory.sqlite"
    buffer_dir = state / "buffer"
    event_store = EventStore(db_path, buffer_dir)
    event_store.initialize()
    work_store = ProjectWorkStore(db_path)
    work_evaluator = ProjectWorkEvaluator(work_store)

    def refresh_project_work(now: datetime) -> None:
        for snapshot in build_overview(
            state, now, registry_path=registry, db_path=db_path,
        ):
            work_evaluator.refresh(snapshot.project_id, snapshot, now)

    handler = _handler_factory(code_root, state, registry, codex_executable)
    server = ConsoleServer((host, port), handler)
    AgentRunStore(db_path).recover_abandoned(datetime.now(timezone.utc))
    server.maintenance = MaintenanceLoop(
        event_store,
        (codex_executable, "app-server", "--listen", "stdio://"),
        interval_seconds=30.0,
        stale_after=timedelta(minutes=30),
        refresh_project_work=refresh_project_work,
    )
    server.maintenance.start()
    return server


def serve(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    codex_executable: str = "codex",
    *,
    state_root: Path | None = None,
    registry_path: Path | None = None,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("console host must be loopback")
    server = create_server(
        root, host, port, codex_executable,
        state_root=state_root, registry_path=registry_path,
    )
    url = f"http://{host}:{server.server_port}/"
    if open_browser:
        webbrowser.open(url)
    print(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _handler_factory(
    code_root: Path,
    state_root: Path,
    registry_path: Path,
    codex_executable: str,
):
    db_path = state_root / "data/factory.sqlite"
    buffer_dir = state_root / "buffer"

    def overview(now: datetime | None = None) -> tuple[ProjectSnapshot, ...]:
        return build_overview(
            state_root, now, registry_path=registry_path, db_path=db_path,
        )

    class ConsoleHandler(BaseHTTPRequestHandler):
        server_version = "SoftwareFactoryConsole/1"

        def do_GET(self) -> None:
            if not self._host_allowed():
                return self._json_error(HTTPStatus.FORBIDDEN, "invalid_host")
            path = urlsplit(self.path).path
            if path == "/api/overview":
                self._recover_stale_runs()
                return self._json_response(HTTPStatus.OK, {
                    "schemaVersion": 1,
                    "projects": [item.to_public_dict() for item in overview()],
                })
            if path == "/api/health":
                return self._json_response(HTTPStatus.OK, self._health_snapshot())
            intent_project_id = _project_id_from_path(path, "/intent")
            if intent_project_id is not None:
                if not _project_for(registry_path, intent_project_id):
                    return self._json_error(HTTPStatus.NOT_FOUND, "project_not_found")
                try:
                    store = ProjectWorkStore(db_path)
                    intent = store.get_intent(intent_project_id)
                    if not intent:
                        return self._json_error(HTTPStatus.NOT_FOUND, "intent_not_found")
                    return self._json_response(
                        HTTPStatus.OK,
                        _intent_public(
                            intent,
                            achieved=store.is_intent_achieved(intent.project_id, intent.version),
                        ),
                    )
                except sqlite3.OperationalError:
                    return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "database_busy")
            actions_project_id = _project_id_from_path(path, "/actions")
            if actions_project_id is not None:
                if not _project_for(registry_path, actions_project_id):
                    return self._json_error(HTTPStatus.NOT_FOUND, "project_not_found")
                try:
                    store = ProjectWorkStore(db_path)
                    return self._json_response(HTTPStatus.OK, {
                        "actions": [
                            _action_public(action)
                            for action in store.list_current_intent_actions(actions_project_id)
                        ],
                    })
                except sqlite3.OperationalError:
                    return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "database_busy")
            if path.startswith("/api/projects/"):
                self._recover_stale_runs()
                project_id = unquote(path.removeprefix("/api/projects/"))
                if "/" in project_id:
                    return self._json_error(HTTPStatus.NOT_FOUND, "not_found")
                snapshot = _snapshot_for(overview(), project_id)
                if not snapshot:
                    return self._json_error(HTTPStatus.NOT_FOUND, "project_not_found")
                return self._json_response(HTTPStatus.OK, snapshot.to_public_dict())
            if path.startswith("/api/agent-runs/"):
                self._recover_stale_runs()
                run_id = unquote(path.removeprefix("/api/agent-runs/"))
                run = AgentRunStore(db_path).get(run_id)
                if not run:
                    return self._json_error(HTTPStatus.NOT_FOUND, "run_not_found")
                return self._json_response(HTTPStatus.OK, run.to_public_dict())
            return self._serve_asset(path)

        def do_PUT(self) -> None:
            if not self._host_allowed():
                return self._json_error(HTTPStatus.FORBIDDEN, "invalid_host")
            if not self._origin_allowed(require_origin=True):
                return self._json_error(HTTPStatus.FORBIDDEN, "invalid_origin")
            project_id = _project_id_from_path(urlsplit(self.path).path, "/intent")
            if project_id is None:
                return self._json_error(HTTPStatus.NOT_FOUND, "not_found")
            if not _project_for(registry_path, project_id):
                return self._json_error(HTTPStatus.NOT_FOUND, "project_not_found")
            try:
                value = self._read_bounded_json_body({
                    "version", "outcome", "acceptanceCriteria", "constraints",
                })
                intent = ProjectWorkStore(db_path).save_intent(
                    project_id,
                    value["outcome"],
                    value["acceptanceCriteria"],
                    value["constraints"],
                    value["version"],
                    datetime.now(timezone.utc),
                )
            except IntentVersionConflict:
                return self._json_error(HTTPStatus.CONFLICT, "intent_version_conflict")
            except sqlite3.OperationalError:
                return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "database_busy")
            except (KeyError, PermissionError, ValueError, json.JSONDecodeError):
                return self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request")
            self._refresh_project_work_after_write(datetime.now(timezone.utc))
            return self._json_response(HTTPStatus.OK, _intent_public(intent, achieved=False))

        def do_POST(self) -> None:
            if not self._host_allowed():
                return self._json_error(HTTPStatus.FORBIDDEN, "invalid_host")
            path = urlsplit(self.path).path
            action_route = _action_decision_route(path)
            if action_route:
                if not self._origin_allowed(require_origin=True):
                    return self._json_error(HTTPStatus.FORBIDDEN, "invalid_origin")
                return self._confirm_project_action(*action_route)
            if not self._origin_allowed():
                return self._json_error(HTTPStatus.FORBIDDEN, "invalid_origin")
            prefix = "/api/projects/"
            suffix = "/agent-runs"
            if not path.startswith(prefix) or not path.endswith(suffix):
                return self._json_error(HTTPStatus.NOT_FOUND, "not_found")
            project_id = unquote(path[len(prefix):-len(suffix)]).strip("/")
            project = _project_for(registry_path, project_id)
            snapshot = _snapshot_for(overview(), project_id)
            if not project or not snapshot:
                return self._json_error(HTTPStatus.NOT_FOUND, "project_not_found")
            try:
                value = self._read_json_body()
                key = value.get("idempotencyKey")
                if not isinstance(key, str) or not key.strip() or len(key) > 128:
                    raise ValueError("invalid idempotency key")
            except (ValueError, json.JSONDecodeError):
                return self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request")
            store = AgentRunStore(db_path)
            try:
                store.recover_abandoned(datetime.now(timezone.utc))
                existing = store.get_by_idempotency_key(project_id, key.strip())
            except sqlite3.OperationalError:
                return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "database_busy")
            if existing:
                status = HTTPStatus.ACCEPTED if existing.status in {"queued", "running"} else HTTPStatus.OK
                return self._json_response(status, existing.to_public_dict())
            if not snapshot.can_run_agent and not snapshot.active_run_id:
                return self._json_error(HTTPStatus.CONFLICT, "agent_run_not_allowed")
            try:
                created = store.create_or_get(project_id, key.strip(), datetime.now(timezone.utc))
            except AgentRunNotAllowed:
                return self._json_error(HTTPStatus.CONFLICT, "agent_run_not_allowed")
            except sqlite3.OperationalError:
                return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "database_busy")
            if created.created:
                runner = CodexSupervisorRunner(
                    code_root,
                    store,
                    codex_executable,
                    code_root / "config/agent-supervisor-output.schema.json",
                )
                threading.Thread(
                    target=runner.run,
                    args=(created.run.run_id, project, snapshot),
                    daemon=True,
                ).start()
            status = HTTPStatus.ACCEPTED if created.run.status in {"queued", "running"} else HTTPStatus.OK
            return self._json_response(status, created.run.to_public_dict())

        def _confirm_project_action(self, project_id: str, action_id: str) -> None:
            if not _project_for(registry_path, project_id):
                return self._json_error(HTTPStatus.NOT_FOUND, "project_not_found")
            try:
                value = self._read_bounded_json_body({"intentVersion"})
                now = datetime.now(timezone.utc)
                intent, confirmed = ProjectWorkStore(
                    db_path,
                ).confirm_current_action_from_decision(
                    project_id, action_id, value["intentVersion"], now,
                )
            except IntentVersionConflict:
                return self._json_error(HTTPStatus.CONFLICT, "intent_version_conflict")
            except ActionStateConflict:
                return self._json_error(HTTPStatus.CONFLICT, "action_not_current")
            except ActionConfirmationRejected:
                return self._json_error(HTTPStatus.CONFLICT, "action_not_confirmable")
            except EvidenceAdvancedConflict:
                self._refresh_project_work_after_write(datetime.now(timezone.utc))
                return self._json_error(HTTPStatus.CONFLICT, "evidence_advanced")
            except KeyError:
                return self._json_error(HTTPStatus.NOT_FOUND, "action_not_found")
            except sqlite3.OperationalError:
                return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "database_busy")
            except (PermissionError, ValueError, json.JSONDecodeError):
                return self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request")
            self._refresh_project_work_after_write(now)
            return self._json_response(HTTPStatus.OK, {
                "intent": _intent_public(intent, achieved=True),
                "action": _action_public(confirmed),
            })

        def _refresh_project_work_after_write(self, now: datetime) -> None:
            try:
                _refresh_project_work(db_path, overview, now)
            except Exception as exc:
                maintenance = getattr(self.server, "maintenance", None)
                if maintenance:
                    maintenance.record_project_work_refresh_failure(now, exc)

        def _read_json_body(self) -> dict[str, object]:
            raw_length = self.headers.get("Content-Length", "0")
            length = int(raw_length)
            if length < 0 or length > _MAX_BODY_BYTES:
                raise ValueError("request too large")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("request must be object")
            return value

        def _read_bounded_json_body(self, allowed_fields: set[str]) -> dict[str, object]:
            value = self._read_json_body()
            if set(value) != allowed_fields:
                raise ValueError("unexpected request fields")
            return value

        def _recover_stale_runs(self) -> None:
            try:
                AgentRunStore(db_path).recover_abandoned(
                    datetime.now(timezone.utc)
                )
            except sqlite3.OperationalError:
                return

        def _health_snapshot(self) -> dict[str, object]:
            event_count = 0
            try:
                with sqlite3.connect(db_path) as connection:
                    event_count = int(connection.execute("SELECT count(*) FROM events").fetchone()[0])
            except sqlite3.Error:
                pass
            maintenance = getattr(self.server, "maintenance", None)
            state = maintenance.health_snapshot() if maintenance else {
                "status": "degraded",
                "serviceStartedAt": None,
                "lastReconcileAt": None,
                "lastReport": {},
                "errorCode": "maintenance_unavailable",
            }
            return {
                "schemaVersion": 1,
                **state,
                "eventCount": event_count,
                "bufferedEventCount": len(list(buffer_dir.glob("*.json")))
                if buffer_dir.exists() else 0,
            }

        def _host_allowed(self) -> bool:
            raw_host = self.headers.get("Host", "")
            host = raw_host.rsplit(":", 1)[0] if not raw_host.startswith("[") else raw_host.split("]", 1)[0] + "]"
            return host in _ALLOWED_HOSTS

        def _origin_allowed(self, require_origin: bool = False) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return not require_origin
            try:
                parsed = urlsplit(origin)
                request_host = urlsplit(f"http://{self.headers.get('Host', '')}")
                origin_port = parsed.port or 80
                request_port = request_host.port or 80
                return (
                    parsed.scheme == "http"
                    and parsed.hostname == request_host.hostname
                    and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                    and origin_port == request_port
                    and parsed.username is None
                    and parsed.password is None
                    and not parsed.path
                    and not parsed.query
                    and not parsed.fragment
                )
            except ValueError:
                return False

        def _serve_asset(self, path: str) -> None:
            names = {
                "/": "index.html",
                "/index.html": "index.html",
                "/app.css": "app.css",
                "/app.js": "app.js",
                "/state.js": "state.js",
            }
            name = names.get(path)
            if not name:
                return self._json_error(HTTPStatus.NOT_FOUND, "not_found")
            asset = code_root / "console" / name
            try:
                payload = asset.read_bytes()
            except OSError:
                return self._json_error(HTTPStatus.NOT_FOUND, "asset_not_found")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def _json_response(self, status: HTTPStatus, value: object) -> None:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _json_error(self, status: HTTPStatus, code: str) -> None:
            self._json_response(status, {"error": code})

        def log_message(self, format: str, *args: object) -> None:
            return

    return ConsoleHandler


def _project_for(registry_path: Path, project_id: str) -> ProjectConfig | None:
    return next(
        (project for project in load_projects(registry_path)
         if project.enabled and project.project_id == project_id),
        None,
    )


def _snapshot_for(
    snapshots: tuple[ProjectSnapshot, ...], project_id: str,
) -> ProjectSnapshot | None:
    return next((item for item in snapshots if item.project_id == project_id), None)


def _project_id_from_path(path: str, suffix: str) -> str | None:
    prefix = "/api/projects/"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    encoded_project_id = path[len(prefix):-len(suffix)]
    if not encoded_project_id or "/" in encoded_project_id:
        return None
    project_id = unquote(encoded_project_id)
    return project_id if project_id and "/" not in project_id else None


def _action_decision_route(path: str) -> tuple[str, str] | None:
    parts = path.split("/")
    if len(parts) != 7 or parts[:3] != ["", "api", "projects"]:
        return None
    if parts[4] != "actions" or parts[6] != "decision" or not parts[3] or not parts[5]:
        return None
    project_id, action_id = unquote(parts[3]), unquote(parts[5])
    if "/" in project_id or "/" in action_id:
        return None
    return project_id, action_id


def _refresh_project_work(
    db_path: Path,
    overview: Callable[[datetime | None], tuple[ProjectSnapshot, ...]],
    now: datetime,
) -> None:
    evaluator = ProjectWorkEvaluator(ProjectWorkStore(db_path))
    for snapshot in overview(now):
        evaluator.refresh(snapshot.project_id, snapshot, now)


def _intent_public(intent: ProjectIntent, achieved: bool | None = None) -> dict[str, object]:
    return {
        "projectId": intent.project_id,
        "outcome": _safe_public_text(intent.outcome, intent.provenance),
        "acceptanceCriteria": [
            _safe_public_text(item, intent.provenance) for item in intent.acceptance_criteria
        ],
        "constraints": [_safe_public_text(item, intent.provenance) for item in intent.constraints],
        "status": "achieved" if achieved else "active",
        "version": intent.version,
        "updatedAt": _safe_public_text(intent.updated_at, intent.provenance),
    }


def _action_public(action: ActionItem) -> dict[str, object]:
    return {
        "actionId": _safe_public_text(action.action_id, action.provenance),
        "intentVersion": action.intent_version,
        "title": _safe_public_text(action.title, action.provenance),
        "whyNow": _safe_public_text(action.why_now, action.provenance),
        "completionEvidence": _safe_public_text(action.completion_evidence, action.provenance),
        "actor": _safe_public_text(action.actor, action.provenance),
        "status": _safe_public_text(action.status, action.provenance),
        "isCurrent": is_current_action_status(action.status),
        "createdAt": _safe_public_text(action.created_at, action.provenance),
        "updatedAt": _safe_public_text(action.updated_at, action.provenance),
    }


def _safe_public_text(value: str, provenance: str) -> str:
    if (
        provenance not in {"local_user", "policy"}
        or not isinstance(value, str)
        or not value.strip()
        or len(value) > 240
        or "\n" in value
        or "\r" in value
        or detect_sensitive_output(value)
        or _PUBLIC_BODY_MARKERS.search(value)
    ):
        return _PUBLIC_TEXT_FALLBACK
    return value
