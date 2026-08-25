from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from observer.config import load_projects
from observer.event_store import EventRecord, EventStore, VerificationRecord
from observer.git_snapshot import capture_git_snapshot
from observer.project_identity import identify_project
from observer.redact import redact_text
from observer.verification import classify_verification


@dataclass(frozen=True)
class ObserverSettings:
    registry_path: Path
    db_path: Path
    buffer_dir: Path
    git_executable: str | None = None


@dataclass(frozen=True)
class IngestResult:
    status: str
    event_id: str | None = None


_EVENT_TYPES = {
    "SessionStart": "session_started",
    "UserPromptSubmit": "turn_started",
    "Stop": "turn_stopped",
    "SessionEnd": "session_ended",
}


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing {key}")
    return value


def ingest_hook(raw: Mapping[str, Any], settings: ObserverSettings) -> IngestResult:
    session_id = _required_string(raw, "session_id")
    cwd = Path(_required_string(raw, "cwd"))
    hook_name = _required_string(raw, "hook_event_name")
    projects = load_projects(settings.registry_path)
    identity = identify_project(cwd, projects, git_executable=settings.git_executable)
    if identity is None:
        return IngestResult("ignored_unregistered")
    turn_id = raw.get("turn_id") if isinstance(raw.get("turn_id"), str) else None
    tool_use_id = raw.get("tool_use_id") if isinstance(raw.get("tool_use_id"), str) else ""
    key_material = f"1|{session_id}|{turn_id or ''}|{hook_name}|{tool_use_id}"
    dedupe = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-observer:{dedupe}"))
    payload: dict[str, Any] = {
        "cwd": str(cwd.resolve(strict=False)),
        "model": raw.get("model") if isinstance(raw.get("model"), str) else None,
        "permissionMode": raw.get("permission_mode") if isinstance(raw.get("permission_mode"), str) else None,
    }
    event_type = _EVENT_TYPES.get(hook_name)
    verification_record = None
    if hook_name == "UserPromptSubmit":
        redacted = redact_text(raw.get("prompt", "") if isinstance(raw.get("prompt"), str) else "")
        payload.update(promptSummary=redacted.text, promptSha256=redacted.sha256, promptRedacted=redacted.was_redacted)
    elif hook_name == "Stop":
        redacted = redact_text(raw.get("last_assistant_message", "") if isinstance(raw.get("last_assistant_message"), str) else "")
        payload.update(assistantClaimSummary=redacted.text, assistantClaimSha256=redacted.sha256)
    elif hook_name == "PostToolUse":
        tool_name = raw.get("tool_name") if isinstance(raw.get("tool_name"), str) else "unknown"
        observation = classify_verification(tool_name, raw.get("tool_input"), raw.get("tool_response"))
        if tool_name == "apply_patch":
            event_type = "code_change_observed"
        elif observation:
            event_type = "verification_observed"
            verification_record = VerificationRecord(
                observation.kind, observation.command_class, observation.exit_code, observation.status,
            )
            payload.update(kind=observation.kind, commandClass=observation.command_class, status=observation.status)
        else:
            event_type = "tool_completed_metadata"
        payload["toolName"] = tool_name
    if event_type is None:
        return IngestResult("ignored_unsupported")
    snapshot = capture_git_snapshot(cwd, git_executable=settings.git_executable)
    store = EventStore(settings.db_path, settings.buffer_dir)
    record = EventRecord(
        event_id=event_id,
        deduplication_key=dedupe,
        project_id=identity.project_id,
        thread_id=session_id,
        turn_id=turn_id,
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        payload_version=1,
        redacted_payload=payload,
        git_snapshot=snapshot,
        verification=verification_record,
    )
    try:
        store.initialize()
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise
        store.buffer_event(record)
        return IngestResult("buffered", event_id)
    result = store.append_event(record)
    return IngestResult("duplicate" if result.duplicate else ("buffered" if result.buffered else "recorded"), event_id)
