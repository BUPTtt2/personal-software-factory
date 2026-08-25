from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from observer.config import ConfigError, ProjectConfig, load_projects
from observer.project_identity import identify_project
from observer.redact import detect_sensitive_output


_PROJECT_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SAFE_SERVICE_ERRORS = {
    "database_busy", "project_not_found", "service_unavailable",
}
_MAX_RESPONSE_BYTES = 1_048_576
_PRIVATE_PATH = re.compile(
    r"(?:^|[\\s\"'])(?:" + re.escape("/" + "Users" + "/") + r"|/home/|[A-Za-z]:\\\\)",
)


@dataclass(frozen=True)
class ToolResult:
    content: tuple[dict[str, object], ...]
    structured_content: dict[str, object]
    is_error: bool = False

    def to_mcp_result(self) -> dict[str, object]:
        value: dict[str, object] = {
            "content": list(self.content),
            "structuredContent": self.structured_content,
        }
        if self.is_error:
            value["isError"] = True
        return value


class _ServiceError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FactoryServiceClient:
    base_url: str
    registry_path: Path
    timeout: float = 2.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("factory service must use loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid factory service URL")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

    def get(self, path: str) -> dict[str, object]:
        request = urllib.request.Request(
            f"{self.base_url}{path}", headers={"Accept": "application/json"}, method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise _ServiceError("service_unavailable")
                content_type = response.headers.get_content_type()
                length = response.headers.get("Content-Length")
                if content_type != "application/json" or (length and int(length) > _MAX_RESPONSE_BYTES):
                    raise _ServiceError("service_unavailable")
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise _ServiceError("service_unavailable")
                value = json.loads(body)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(_MAX_RESPONSE_BYTES + 1)
                content_type = (exc.headers.get("Content-Type", "").split(";", 1)[0].strip().lower())
                if len(body) > _MAX_RESPONSE_BYTES or content_type != "application/json":
                    code = None
                else:
                    payload = json.loads(body)
                    code = payload.get("error") if isinstance(payload, dict) else None
            except (OSError, ValueError):
                code = None
            finally:
                exc.close()
            raise _ServiceError(code if code in _SAFE_SERVICE_ERRORS else "service_unavailable") from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise _ServiceError("service_unavailable") from exc
        if not isinstance(value, dict):
            raise _ServiceError("service_unavailable")
        return value


def list_tool_definitions() -> tuple[dict[str, object], ...]:
    definitions = (
        (
            "factory_list_projects",
            "List registered software projects and whether each needs attention.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        (
            "factory_get_current_project",
            "Identify which registered project contains the current working directory.",
            {
                "type": "object",
                "properties": {"cwd": {"type": "string", "maxLength": 4096}},
                "required": ["cwd"],
                "additionalProperties": False,
            },
        ),
        (
            "factory_get_project_status",
            "Get the current goal, single action, activity and agent state for a project.",
            {
                "type": "object",
                "properties": {"projectId": {"type": "string", "maxLength": 64}},
                "required": ["projectId"],
                "additionalProperties": False,
            },
        ),
        (
            "factory_get_evidence_summary",
            "Get bounded Observer, Git and Verifier evidence summaries for a project.",
            {
                "type": "object",
                "properties": {"projectId": {"type": "string", "maxLength": 64}},
                "required": ["projectId"],
                "additionalProperties": False,
            },
        ),
        (
            "factory_open_console",
            "Return the healthy localhost console URL for optional project inspection.",
            {
                "type": "object",
                "properties": {"projectId": {"type": "string", "maxLength": 64}},
                "additionalProperties": False,
            },
        ),
    )
    return tuple({
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    } for name, description, schema in definitions)


def call_tool(
    name: str,
    arguments: dict[str, object],
    client: FactoryServiceClient,
) -> ToolResult:
    handlers: dict[str, tuple[set[str], set[str], Callable[[dict[str, object]], dict[str, object]]]] = {
        "factory_list_projects": (set(), set(), lambda _: _list_projects(client)),
        "factory_get_current_project": ({"cwd"}, {"cwd"}, lambda value: _current_project(client, value)),
        "factory_get_project_status": ({"projectId"}, {"projectId"}, lambda value: _project_status(client, value)),
        "factory_get_evidence_summary": ({"projectId"}, {"projectId"}, lambda value: _evidence_summary(client, value)),
        "factory_open_console": ({"projectId"}, set(), lambda value: _open_console(client, value)),
    }
    if name not in handlers:
        return _error("unknown_tool")
    if not isinstance(arguments, dict):
        return _error("invalid_arguments")
    allowed, required, handler = handlers[name]
    if not set(arguments).issubset(allowed) or not required.issubset(arguments):
        return _error("invalid_arguments")
    try:
        value = handler(arguments)
    except _ServiceError as exc:
        return _error(exc.code)
    except (ConfigError, OSError, TypeError, ValueError):
        return _error("invalid_arguments")
    return _success(value)


def _list_projects(client: FactoryServiceClient) -> dict[str, object]:
    payload = client.get("/api/overview")
    raw_projects = payload.get("projects")
    if not isinstance(raw_projects, list):
        raise _ServiceError("service_unavailable")
    projects = []
    for item in raw_projects:
        if not isinstance(item, dict):
            raise _ServiceError("service_unavailable")
        projects.append({
            "projectId": _required_text(item, "project_id", 64),
            "displayName": _required_text(item, "display_name", 120),
            "activityStatus": _optional_text(item.get("activity_status"), 40),
            "needsAttention": bool(item.get("current_action") or item.get("open_decision")),
            "lastEventAt": _optional_text(item.get("last_event_at"), 80),
        })
    return {"projects": projects}


def _current_project(client: FactoryServiceClient, arguments: dict[str, object]) -> dict[str, object]:
    cwd_value = arguments.get("cwd")
    if isinstance(cwd_value, str) and 0 < len(cwd_value) <= 4096:
        cwd = Path(cwd_value)
    else:
        raise ValueError("invalid cwd")
    projects = tuple(project for project in load_projects(client.registry_path) if project.enabled)
    identity = identify_project(cwd, projects)
    if identity is None:
        raise _ServiceError("project_not_registered")
    project = next(item for item in projects if item.project_id == identity.project_id)
    return {
        "projectId": _public_text(project.project_id, 64, identifier=True),
        "displayName": _public_text(project.display_name, 120),
        "matchedBy": identity.matched_by,
    }


def _project_status(client: FactoryServiceClient, arguments: dict[str, object]) -> dict[str, object]:
    project_id = _argument_project_id(arguments, required=True)
    item = client.get(f"/api/projects/{urllib.parse.quote(project_id, safe='')}")
    if _required_text(item, "project_id", 64) != project_id:
        raise _ServiceError("service_unavailable")
    action = item.get("current_action")
    safe_action = None
    if isinstance(action, dict):
        safe_action = {
            "actionId": _required_text(action, "action_id", 128),
            "title": _required_text(action, "title", 240),
            "completionEvidence": _required_text(action, "completion_evidence", 120),
            "actor": _required_text(action, "actor", 40),
            "status": _required_text(action, "status", 40),
        }
    agent = item.get("agent_activity") if isinstance(item.get("agent_activity"), dict) else {}
    return {
        "projectId": _required_text(item, "project_id", 64),
        "displayName": _required_text(item, "display_name", 120),
        "judgement": _required_text(item, "judgement", 240),
        "reason": _required_text(item, "reason", 1200),
        "nextAction": _required_text(item, "next_action", 240),
        "activityStatus": _optional_text(item.get("activity_status"), 40),
        "intentStatus": _optional_text(item.get("intent_status"), 40),
        "outcomeSummary": _optional_text(item.get("outcome_summary"), 240),
        "currentAction": safe_action,
        "agentActivity": {
            "status": _optional_text(agent.get("status"), 40),
            "errorCode": _optional_text(agent.get("error_code"), 80),
        },
        "lastEventAt": _optional_text(item.get("last_event_at"), 80),
    }


def _evidence_summary(client: FactoryServiceClient, arguments: dict[str, object]) -> dict[str, object]:
    project_id = _argument_project_id(arguments, required=True)
    item = client.get(f"/api/projects/{urllib.parse.quote(project_id, safe='')}")
    if _required_text(item, "project_id", 64) != project_id:
        raise _ServiceError("service_unavailable")
    raw_evidence = item.get("evidence")
    if not isinstance(raw_evidence, list):
        raise _ServiceError("service_unavailable")
    evidence = []
    for value in raw_evidence[:3]:
        if not isinstance(value, dict):
            raise _ServiceError("service_unavailable")
        evidence.append({
            "source": _required_text(value, "source", 40),
            "summary": _required_text(value, "summary", 400),
        })
    work = item.get("work_summary") if isinstance(item.get("work_summary"), dict) else {}
    changed_paths = work.get("changed_paths") if isinstance(work.get("changed_paths"), list) else []
    return {
        "projectId": _required_text(item, "project_id", 64),
        "evidence": evidence,
        "verificationStatus": _optional_text(work.get("verification_status"), 40),
        "verificationFreshness": _optional_text(work.get("verification_freshness"), 40),
        "verificationObservedAt": _optional_text(work.get("verification_observed_at"), 80),
        "changedPathCount": min(len(changed_paths), 10000),
        "lastEventAt": _optional_text(item.get("last_event_at"), 80),
    }


def _open_console(client: FactoryServiceClient, arguments: dict[str, object]) -> dict[str, object]:
    project_id = _argument_project_id(arguments, required=False)
    health = client.get("/api/health")
    healthy = health.get("status") in {"ok", "healthy"}
    url = f"{client.base_url}/"
    if project_id:
        url += f"?project={urllib.parse.quote(project_id, safe='')}"
    return {"url": url, "healthy": healthy, "projectId": project_id}


def _argument_project_id(arguments: dict[str, object], required: bool) -> str | None:
    value = arguments.get("projectId")
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _PROJECT_ID.fullmatch(value):
        raise ValueError("invalid project id")
    return value


def _required_text(value: dict[str, object], key: str, maximum: int) -> str:
    result = value.get(key)
    return _public_text(result, maximum)


def _optional_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str) or len(value) > maximum
        or detect_sensitive_output(value) or _PRIVATE_PATH.search(value)
    ):
        raise _ServiceError("service_unavailable")
    return value


def _public_text(value: object, maximum: int, identifier: bool = False) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > maximum
        or detect_sensitive_output(value) or _PRIVATE_PATH.search(value)
        or (identifier and not _PROJECT_ID.fullmatch(value))
    ):
        raise _ServiceError("service_unavailable")
    return value


def _success(value: dict[str, object]) -> ToolResult:
    return ToolResult(
        content=({"type": "text", "text": json.dumps(value, ensure_ascii=False, separators=(",", ":"))},),
        structured_content=value,
    )


def _error(code: str) -> ToolResult:
    value = {"error": code}
    return ToolResult(
        content=({"type": "text", "text": json.dumps(value, separators=(",", ":"))},),
        structured_content=value,
        is_error=True,
    )
