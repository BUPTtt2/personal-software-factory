from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from observer.action_policy import CURRENT_ACTION_STATUSES, CURRENT_ACTION_STATUS_SQL
from observer.console_model import ProjectSnapshot, current_evidence_cursor
from observer.redact import detect_sensitive_output


_ACTION_ACTORS = frozenset({"codex", "user", "external"})
_ACTION_STATUSES = frozenset({
    "proposed", "ready", "in_progress", "blocked", "verified", "cancelled",
})
_ACTION_SOURCES = frozenset({"policy", "supervisor", "user"})
_COMPLETION_EVIDENCE = frozenset({
    "trusted_verification_current", "project_activity_observed", "recovered_state_verified",
    "user_confirms_acceptance",
})
_DECISION_STATUSES = frozenset({"open", "resolved", "withdrawn"})
_TRUSTED_RECEIPT_KINDS = {
    "trusted_verification_current": "test",
    "recovered_state_verified": "check",
}
_POLICY_ACTION_TEXT = frozenset({
    ("Verify", "Missing proof"), ("Verify", "Current proof"), ("Review", "Proof is current"),
    ("等待当前工作收口", "当前线程仍在工作"),
    ("处理验证失败", "当前可信验证未通过"),
    ("补齐可信验证", "变更尚无当前可信验证"),
    ("恢复并核验状态", "线程状态需要恢复核验"),
    ("确认验收完成", "当前可信验证已通过"),
    ("核对验收标准并确认完成", "当前可信验证已通过；其余验收标准需要你核对"),
})
_CANCELLATION_SAFETY_CODES = frozenset({
    "manual_cancellation", "policy_state_superseded", "evidence_advanced", "intent_missing",
})
_BODY_MARKERS = re.compile(
    r"```|(?:^|\s)(?:def|class|function|const|let|var|import|from|export)\s+"
    r"|(?:system|user|assistant)\s+prompt\b|tool\s+(?:output|result)\b|\benv(?:ironment)?\s*=",
    re.IGNORECASE,
)


class IntentVersionConflict(RuntimeError):
    """Raised when an intent update does not name the current version."""


class CurrentActionConflict(RuntimeError):
    """Raised when a project would have more than one current action."""


class ActionStateConflict(RuntimeError):
    """Raised when a user confirmation does not name a current action."""


class ActionConfirmationRejected(RuntimeError):
    """Raised when an Action cannot establish local-user confirmation."""


class EvidenceAdvancedConflict(RuntimeError):
    """Raised when facts changed after a confirmation Action was created."""


@dataclass(frozen=True)
class ProjectIntent:
    project_id: str
    outcome: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    status: str
    version: int
    provenance: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ActionItem:
    action_id: str
    project_id: str
    intent_version: int
    title: str
    why_now: str
    completion_evidence: str
    actor: str
    status: str
    source: str
    evidence_cursor: str
    provenance: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DecisionRequest:
    decision_id: str
    project_id: str
    intent_version: int
    question: str
    reason: str
    options: tuple[str, ...]
    status: str
    resolution: str | None
    provenance: str
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class _PolicyAction:
    title: str
    why_now: str
    completion_evidence: str
    actor: str


_WORKING_ACTION = _PolicyAction(
    "等待当前工作收口", "当前线程仍在工作", "project_activity_observed", "external",
)
_FAILED_VERIFICATION_ACTION = _PolicyAction(
    "处理验证失败", "当前可信验证未通过", "project_activity_observed", "codex",
)
_MISSING_VERIFICATION_ACTION = _PolicyAction(
    "补齐可信验证", "变更尚无当前可信验证", "trusted_verification_current", "external",
)
_RECOVERY_ACTION = _PolicyAction(
    "恢复并核验状态", "线程状态需要恢复核验", "recovered_state_verified", "external",
)
_USER_CONFIRMATION_ACTION = _PolicyAction(
    "核对验收标准并确认完成", "当前可信验证已通过；其余验收标准需要你核对",
    "user_confirms_acceptance", "user",
)


class ProjectWorkStore:
    """Persist bounded project-work inputs, never raw Codex content.

    Intent and decision provenance records the local-user entry route; it does
    not classify the semantics of short natural-language product text. Policy
    actions are limited to fixed templates, and supervisors have no creation
    route. Transcripts, prompts, answers, source payloads, and tool outputs
    belong outside this store.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=0.25)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=250")
        return connection

    def get_intent(self, project_id: str) -> ProjectIntent | None:
        project_id = _text(project_id, "project_id", 128)
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM project_intents WHERE project_id=? AND status='active'",
                (project_id,),
            ).fetchone()
        return _intent_from_row(row) if row else None

    def is_intent_achieved(self, project_id: str, intent_version: int) -> bool:
        project_id = _text(project_id, "project_id", 128)
        intent_version = _version(intent_version, "intent_version")
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM action_items JOIN action_resolutions USING(action_id) "
                "WHERE action_items.project_id=? AND action_items.intent_version=? "
                "AND action_items.status='verified' "
                "AND action_items.completion_evidence='user_confirms_acceptance' "
                "AND action_resolutions.resolution_kind='user_confirmation' "
                "AND action_resolutions.decision_id IS NOT NULL LIMIT 1",
                (project_id, intent_version),
            ).fetchone()
        return row is not None

    def save_intent(
        self,
        project_id: str,
        outcome: str,
        acceptance_criteria: Iterable[str],
        constraints: Iterable[str],
        expected_version: int,
        updated_at: datetime,
    ) -> ProjectIntent:
        project_id = _text(project_id, "project_id", 128)
        outcome = _text(outcome, "outcome", 240)
        criteria = _text_items(acceptance_criteria, "acceptance_criteria", 1, 7)
        limits = _text_items(constraints, "constraints", 0, 7)
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        timestamp = _timestamp(updated_at)
        with closing(self.connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM project_intents WHERE project_id=? AND status='active'", (project_id,),
                ).fetchone()
                current_version = int(row["version"]) if row else 0
                if expected_version != current_version:
                    raise IntentVersionConflict(
                        f"expected version {expected_version}, current version is {current_version}",
                    )
                version = current_version + 1
                if row:
                    connection.execute(
                        "UPDATE project_intents SET status='superseded',updated_at=? "
                        "WHERE project_id=? AND version=? AND status='active'",
                        (timestamp, project_id, current_version),
                    )
                connection.execute(
                    "INSERT INTO project_intents("
                    "project_id,version,outcome,acceptance_criteria,constraints,status,created_at,updated_at,"
                    "provenance) VALUES(?,?,?,?,?,?,?,?,?)",
                    (project_id, version, outcome, _json(criteria), _json(limits), "active", timestamp, timestamp,
                     "local_user"),
                )
                row = connection.execute(
                    "SELECT * FROM project_intents WHERE project_id=? AND version=?", (project_id, version),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _intent_from_row(row)

    def create_action(
        self,
        project_id: str,
        intent_version: int,
        title: str,
        why_now: str,
        completion_evidence: str,
        actor: str,
        status: str,
        source: str,
        evidence_cursor: str,
        created_at: datetime,
    ) -> ActionItem:
        project_id = _text(project_id, "project_id", 128)
        intent_version = _version(intent_version, "intent_version")
        title = _text(title, "title", 120)
        why_now = _text(why_now, "why_now", 240)
        completion_evidence = _enum(
            completion_evidence, "completion_evidence", _COMPLETION_EVIDENCE,
        )
        actor = _enum(actor, "actor", _ACTION_ACTORS)
        status = _enum(status, "status", _ACTION_STATUSES)
        if status == "verified":
            raise ValueError("verified actions require an audited resolution")
        source = _enum(source, "source", _ACTION_SOURCES)
        if source == "supervisor":
            raise PermissionError("supervisor cannot create actions")
        provenance = "policy" if source == "policy" else "local_user"
        if source == "policy":
            if (title, why_now) not in _POLICY_ACTION_TEXT:
                raise ValueError("policy actions require a fixed template")
        evidence_cursor = _text(evidence_cursor, "evidence_cursor", 240)
        evidence_cursor = _evidence_cursor(evidence_cursor)
        timestamp = _timestamp(created_at)
        action_id = str(uuid.uuid4())
        with closing(self.connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_current_intent(connection, project_id, intent_version)
                try:
                    connection.execute(
                        "INSERT INTO action_items("
                        "action_id,project_id,intent_version,title,why_now,completion_evidence,actor,status,"
                        "source,evidence_cursor,created_at,updated_at,provenance"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (action_id, project_id, intent_version, title, why_now, completion_evidence, actor,
                         status, source, evidence_cursor, timestamp, timestamp, provenance),
                    )
                    row = connection.execute(
                        "SELECT * FROM action_items WHERE action_id=?", (action_id,),
                    ).fetchone()
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            except sqlite3.IntegrityError as exc:
                if "action_items.project_id" in str(exc):
                    raise CurrentActionConflict(project_id) from exc
                raise
        return _action_from_row(row)

    def list_actions(self, project_id: str) -> list[ActionItem]:
        project_id = _text(project_id, "project_id", 128)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM action_items WHERE project_id=? ORDER BY "
                f"CASE WHEN action_items.status IN {CURRENT_ACTION_STATUS_SQL} THEN 0 ELSE 1 END, "
                "action_items.updated_at DESC, action_items.action_id DESC",
                (project_id,),
            ).fetchall()
        return [_action_from_row(row) for row in rows]

    def list_current_intent_actions(self, project_id: str) -> list[ActionItem]:
        project_id = _text(project_id, "project_id", 128)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT action_items.* FROM action_items JOIN project_intents "
                "ON project_intents.project_id=action_items.project_id "
                "AND project_intents.version=action_items.intent_version "
                "AND project_intents.status='active' WHERE action_items.project_id=? ORDER BY "
                f"CASE WHEN action_items.status IN {CURRENT_ACTION_STATUS_SQL} THEN 0 ELSE 1 END, "
                "action_items.updated_at DESC,action_items.action_id DESC",
                (project_id,),
            ).fetchall()
        return [_action_from_row(row) for row in rows]

    def get_action(self, action_id: str) -> ActionItem | None:
        action_id = _text(action_id, "action_id", 128)
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM action_items WHERE action_id=?", (action_id,),
            ).fetchone()
        return _action_from_row(row) if row else None

    def resolve_action(
        self, action_id: str, verification_event_id: str, resolved_at: datetime,
    ) -> ActionItem:
        action_id = _text(action_id, "action_id", 128)
        verification_event_id = _text(verification_event_id, "verification_event_id", 128)
        timestamp = _timestamp(resolved_at)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM action_items WHERE action_id=?", (action_id,),
                ).fetchone()
                if not existing:
                    raise KeyError(action_id)
                action = _action_from_row(existing)
                self._require_trusted_verification(connection, action, verification_event_id)
                connection.execute(
                    "UPDATE action_items SET status='verified',updated_at=? WHERE action_id=?",
                    (timestamp, action_id),
                )
                connection.execute(
                    "INSERT INTO action_resolutions("
                    "resolution_id,action_id,project_id,intent_version,resolution_kind,actor,authority,reason,"
                    "verification_event_id,decision_id,resolved_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,NULL,?)",
                    (str(uuid.uuid4()), action.action_id, action.project_id, action.intent_version,
                     "trusted_verification", "verification", "trusted_receipt", "passed_receipt",
                     verification_event_id, timestamp),
                )
                row = connection.execute(
                    "SELECT * FROM action_items WHERE action_id=?", (action_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _action_from_row(row)

    def confirm_action(self, action_id: str, decision_id: str, confirmed_at: datetime) -> ActionItem:
        action_id = _text(action_id, "action_id", 128)
        decision_id = _text(decision_id, "decision_id", 128)
        timestamp = _timestamp(confirmed_at)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM action_items WHERE action_id=?", (action_id,),
                ).fetchone()
                if not existing:
                    raise KeyError(action_id)
                action = _action_from_row(existing)
                decision_row = connection.execute(
                    "SELECT * FROM decision_requests WHERE decision_id=?", (decision_id,),
                ).fetchone()
                if not decision_row:
                    raise KeyError(decision_id)
                decision = _decision_from_row(decision_row)
                if (
                    action.actor != "user"
                    or action.source not in {"policy", "user"}
                    or action.provenance not in {"policy", "local_user"}
                    or action.completion_evidence != "user_confirms_acceptance"
                ):
                    raise PermissionError("only user acceptance actions can be confirmed")
                self._require_open_action(action)
                _require_current_intent(connection, action.project_id, action.intent_version)
                if current_evidence_cursor(connection, action.project_id) != action.evidence_cursor:
                    raise EvidenceAdvancedConflict(action_id)
                if decision.project_id != action.project_id or decision.intent_version != action.intent_version:
                    raise ValueError("decision does not belong to the action intent")
                if decision.provenance != "local_user" or decision.status != "resolved":
                    raise ValueError("decision is not a resolved local user decision")
                if decision.resolution not in decision.options or decision.resolution != action.title:
                    raise ValueError("decision resolution does not match the action")
                connection.execute(
                    "UPDATE action_items SET status='verified',updated_at=? WHERE action_id=?",
                    (timestamp, action_id),
                )
                connection.execute(
                    "INSERT INTO action_resolutions("
                    "resolution_id,action_id,project_id,intent_version,resolution_kind,actor,authority,reason,"
                    "verification_event_id,decision_id,resolved_at"
                    ") VALUES(?,?,?,?,?,?,?,?,NULL,?,?)",
                    (str(uuid.uuid4()), action.action_id, action.project_id, action.intent_version,
                     "user_confirmation", "user", "resolved_local_user_decision", "decision_option_confirmed",
                     decision.decision_id, timestamp),
                )
                row = connection.execute(
                    "SELECT * FROM action_items WHERE action_id=?", (action_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _action_from_row(row)

    def confirm_current_action_from_decision(
        self,
        project_id: str,
        action_id: str,
        expected_intent_version: int,
        confirmed_at: datetime,
    ) -> tuple[ProjectIntent, ActionItem]:
        project_id = _text(project_id, "project_id", 128)
        action_id = _text(action_id, "action_id", 128)
        expected_intent_version = _version(expected_intent_version, "expected_intent_version")
        timestamp = _timestamp(confirmed_at)
        decision_id = str(uuid.uuid4())
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                intent_row = connection.execute(
                    "SELECT * FROM project_intents WHERE project_id=? AND status='active'", (project_id,),
                ).fetchone()
                if not intent_row or int(intent_row["version"]) != expected_intent_version:
                    current_version = int(intent_row["version"]) if intent_row else 0
                    raise IntentVersionConflict(
                        f"intent version {expected_intent_version} is not current for {project_id}; "
                        f"current is {current_version}",
                    )
                action_row = connection.execute(
                    "SELECT * FROM action_items WHERE action_id=?", (action_id,),
                ).fetchone()
                if not action_row:
                    raise KeyError(action_id)
                action = _action_from_row(action_row)
                if action.project_id != project_id or action.intent_version != expected_intent_version:
                    raise KeyError(action_id)
                if action.status not in CURRENT_ACTION_STATUSES:
                    raise ActionStateConflict(action_id)
                if (
                    action.actor != "user"
                    or action.source not in {"policy", "user"}
                    or action.provenance not in {"policy", "local_user"}
                    or action.completion_evidence != "user_confirms_acceptance"
                ):
                    raise ActionConfirmationRejected(action_id)
                if current_evidence_cursor(connection, project_id) != action.evidence_cursor:
                    raise EvidenceAdvancedConflict(action_id)
                connection.execute(
                    "INSERT INTO decision_requests("
                    "decision_id,project_id,intent_version,question,reason,options,status,resolution,"
                    "created_at,resolved_at,provenance"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, project_id, expected_intent_version, "确认验收完成", "当前可信验证已通过",
                     _json((action.title, "暂不确认")), "resolved", action.title,
                     timestamp, timestamp, "local_user"),
                )
                cursor = connection.execute(
                    "UPDATE action_items SET status='verified',updated_at=? "
                    "WHERE action_id=? AND status IN ('proposed','ready','in_progress')",
                    (timestamp, action_id),
                )
                if cursor.rowcount != 1:
                    raise ActionStateConflict(action_id)
                connection.execute(
                    "INSERT INTO action_resolutions("
                    "resolution_id,action_id,project_id,intent_version,resolution_kind,actor,authority,reason,"
                    "verification_event_id,decision_id,resolved_at"
                    ") VALUES(?,?,?,?,?,?,?,?,NULL,?,?)",
                    (str(uuid.uuid4()), action_id, project_id, expected_intent_version,
                     "user_confirmation", "user", "resolved_local_user_decision",
                     "decision_option_confirmed", decision_id, timestamp),
                )
                confirmed_row = connection.execute(
                    "SELECT * FROM action_items WHERE action_id=?", (action_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _intent_from_row(intent_row), _action_from_row(confirmed_row)

    def cancel_action(
        self, action_id: str, cancelled_at: datetime, safety_code: str = "manual_cancellation",
    ) -> ActionItem:
        action_id = _text(action_id, "action_id", 128)
        timestamp = _timestamp(cancelled_at)
        safety_code = _enum(safety_code, "safety_code", _CANCELLATION_SAFETY_CODES)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                action_row = connection.execute(
                    "SELECT * FROM action_items WHERE action_id=?", (action_id,),
                ).fetchone()
                if not action_row:
                    raise KeyError(action_id)
                action = _action_from_row(action_row)
                cursor = connection.execute(
                    "UPDATE action_items SET status='cancelled',updated_at=? "
                    "WHERE action_id=? AND status NOT IN ('verified','cancelled')",
                    (timestamp, action_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(action_id)
                connection.execute(
                    "INSERT INTO action_resolutions("
                    "resolution_id,action_id,project_id,intent_version,resolution_kind,actor,authority,reason,"
                    "verification_event_id,decision_id,resolved_at"
                    ") VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?)",
                    (str(uuid.uuid4()), action.action_id, action.project_id, action.intent_version,
                     "safety_cancellation", "policy", "safety_code", safety_code, timestamp),
                )
                row = connection.execute(
                    "SELECT * FROM action_items WHERE action_id=?", (action_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _action_from_row(row)

    @staticmethod
    def _require_open_action(action: ActionItem) -> None:
        if action.status not in CURRENT_ACTION_STATUSES | {"blocked"}:
            raise ValueError("action is already resolved")

    def _require_trusted_verification(
        self, connection: sqlite3.Connection, action: ActionItem, event_id: str,
    ) -> None:
        if action.actor == "codex" or action.source == "supervisor":
            raise PermissionError("agent and supervisor actions cannot be directly verified")
        if action.actor == "user":
            raise PermissionError("user action requires a user confirmation")
        expected_kind = _TRUSTED_RECEIPT_KINDS.get(action.completion_evidence)
        if not expected_kind:
            raise ValueError("completion_evidence cannot be resolved by a verification receipt")
        self._require_open_action(action)
        row = connection.execute(
            "SELECT verification_records.kind,verification_records.status,verification_records.observed_at,"
            "events.project_id,events.event_type,events.occurred_at "
            "FROM verification_records JOIN events USING(event_id) WHERE events.event_id=?",
            (event_id,),
        ).fetchone()
        if not row or row["project_id"] != action.project_id or row["event_type"] != "verification_receipt":
            raise ValueError("verification receipt is not available for this project")
        if row["status"] != "passed" or row["kind"] != expected_kind:
            raise ValueError("verification receipt does not satisfy completion_evidence")
        intent = connection.execute(
            "SELECT updated_at FROM project_intents WHERE project_id=? AND status='active'",
            (action.project_id,),
        ).fetchone()
        latest_change = connection.execute(
            "SELECT MAX(occurred_at) AS occurred_at FROM events "
            "WHERE project_id=? AND event_type='code_change_observed'", (action.project_id,),
        ).fetchone()
        if row["observed_at"] <= action.created_at or row["occurred_at"] <= intent["updated_at"]:
            raise ValueError("verification receipt is older than the action or current intent")
        if latest_change["occurred_at"] and row["occurred_at"] < latest_change["occurred_at"]:
            raise ValueError("verification receipt predates the latest code change")
        if connection.execute(
            "SELECT 1 FROM action_resolutions WHERE verification_event_id=?", (event_id,),
        ).fetchone():
            raise ValueError("verification receipt is already used")

    def create_decision_request(
        self,
        project_id: str,
        intent_version: int,
        question: str,
        reason: str,
        options: Iterable[str],
        status: str,
        resolution: str | None,
        created_at: datetime,
    ) -> DecisionRequest:
        project_id = _text(project_id, "project_id", 128)
        intent_version = _version(intent_version, "intent_version")
        question = _text(question, "question", 240)
        reason = _text(reason, "reason", 240)
        option_items = _text_items(options, "options", 2, 3)
        status = _enum(status, "status", _DECISION_STATUSES)
        resolution = _optional_text(resolution, "resolution", 240)
        if status != "open" or resolution is not None:
            raise ValueError("decision requests must be created open and unresolved")
        timestamp = _timestamp(created_at)
        decision_id = str(uuid.uuid4())
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _require_current_intent(connection, project_id, intent_version)
                connection.execute(
                    "INSERT INTO decision_requests("
                    "decision_id,project_id,intent_version,question,reason,options,status,resolution,"
                    "created_at,resolved_at,provenance"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, project_id, intent_version, question, reason, _json(option_items), status,
                     resolution, timestamp, None, "local_user"),
                )
                row = connection.execute(
                    "SELECT * FROM decision_requests WHERE decision_id=?", (decision_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _decision_from_row(row)

    def list_decision_requests(self, project_id: str) -> list[DecisionRequest]:
        project_id = _text(project_id, "project_id", 128)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM decision_requests WHERE project_id=? ORDER BY created_at DESC, decision_id DESC",
                (project_id,),
            ).fetchall()
        return [_decision_from_row(row) for row in rows]

    def get_decision_request(self, decision_id: str) -> DecisionRequest | None:
        decision_id = _text(decision_id, "decision_id", 128)
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM decision_requests WHERE decision_id=?", (decision_id,),
            ).fetchone()
        return _decision_from_row(row) if row else None

    def resolve_user_decision(
        self, decision_id: str, resolution: str, resolved_at: datetime,
    ) -> DecisionRequest:
        decision_id = _text(decision_id, "decision_id", 128)
        resolution = _text(resolution, "resolution", 240)
        timestamp = _timestamp(resolved_at)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM decision_requests WHERE decision_id=?", (decision_id,),
                ).fetchone()
                if not existing:
                    raise KeyError(decision_id)
                decision = _decision_from_row(existing)
                if decision.status != "open":
                    raise ValueError("decision is already resolved")
                if decision.provenance != "local_user":
                    raise PermissionError("legacy decisions cannot establish user authority")
                if resolution not in decision.options:
                    raise ValueError("resolution must match a decision option")
                cursor = connection.execute(
                    "UPDATE decision_requests SET status='resolved',resolution=?,resolved_at=? "
                    "WHERE decision_id=? AND status='open'",
                    (resolution, timestamp, decision_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("decision is already resolved")
                row = connection.execute(
                    "SELECT * FROM decision_requests WHERE decision_id=?", (decision_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _decision_from_row(row)

    def resolve_decision_request(
        self, decision_id: str, resolution: str, resolved_at: datetime,
    ) -> DecisionRequest:
        return self.resolve_user_decision(decision_id, resolution, resolved_at)


class ProjectWorkEvaluator:
    """Derive one bounded action from the current safe project-state facts."""

    def __init__(self, store: ProjectWorkStore):
        self.store = store

    def refresh(
        self, project_id: str, snapshot: ProjectSnapshot, now: datetime,
    ) -> ActionItem | None:
        project_id = _text(project_id, "project_id", 128)
        if snapshot.project_id != project_id:
            raise ValueError("snapshot does not belong to project")
        evidence_cursor = _evidence_cursor(snapshot.evidence_cursor)
        intent = self.store.get_intent(project_id)
        current = self._current_action(project_id)
        if not intent:
            if current:
                self.store.cancel_action(current.action_id, now, "intent_missing")
            return None

        if current and current.intent_version != intent.version:
            self.store.cancel_action(current.action_id, now, "policy_state_superseded")
            current = None

        if self.store.is_intent_achieved(project_id, intent.version):
            if current:
                self.store.cancel_action(current.action_id, now, "policy_state_superseded")
            return None

        if self._is_current_trusted_pass(snapshot) and current and current.evidence_cursor != evidence_cursor:
            current = self._resolve_matching_verification(current, now)

        desired = self._desired_action(snapshot)
        if current and self._matches(current, desired, intent.version, evidence_cursor):
            return current
        if current and not desired:
            return current
        if current:
            reason = (
                "evidence_advanced"
                if current.evidence_cursor != evidence_cursor
                else "policy_state_superseded"
            )
            self.store.cancel_action(current.action_id, now, reason)
        if not desired:
            return None
        return self.store.create_action(
            project_id, intent.version, desired.title, desired.why_now, desired.completion_evidence,
            desired.actor, "ready", "policy", evidence_cursor, now,
        )

    def _current_action(self, project_id: str) -> ActionItem | None:
        return next(
            (action for action in self.store.list_actions(project_id) if action.status in CURRENT_ACTION_STATUSES),
            None,
        )

    def _resolve_matching_verification(self, action: ActionItem, now: datetime) -> ActionItem | None:
        expected_kind = _TRUSTED_RECEIPT_KINDS.get(action.completion_evidence)
        if not expected_kind:
            return action
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                "SELECT events.event_id FROM events JOIN verification_records USING(event_id) "
                "WHERE events.project_id=? AND events.event_type='verification_receipt' "
                "AND verification_records.kind=? AND verification_records.status='passed' "
                "AND verification_records.observed_at>? "
                "AND events.event_id NOT IN (SELECT verification_event_id FROM action_resolutions "
                "WHERE verification_event_id IS NOT NULL) "
                "ORDER BY verification_records.observed_at DESC,events.event_id DESC LIMIT 1",
                (action.project_id, expected_kind, action.created_at),
            ).fetchone()
        if not row:
            return action
        self.store.resolve_action(action.action_id, row["event_id"], now)
        return None

    @staticmethod
    def _matches(
        action: ActionItem,
        desired: _PolicyAction | None,
        intent_version: int,
        evidence_cursor: str,
    ) -> bool:
        return bool(desired) and (
            action.intent_version == intent_version
            and action.evidence_cursor == evidence_cursor
            and action.title == desired.title
            and action.why_now == desired.why_now
            and action.completion_evidence == desired.completion_evidence
            and action.actor == desired.actor
        )

    @staticmethod
    def _is_current_trusted_pass(snapshot: ProjectSnapshot) -> bool:
        summary = snapshot.work_summary
        return (
            summary.verification_status == "passed"
            and summary.verification_freshness == "current"
        )

    @staticmethod
    def _desired_action(snapshot: ProjectSnapshot) -> _PolicyAction | None:
        summary = snapshot.work_summary
        if _count(summary.active_threads, "active_threads") > 0 or summary.stage == "工作中":
            return _WORKING_ACTION
        if summary.verification_status == "failed" or summary.stage == "验证失败":
            return _FAILED_VERIFICATION_ACTION
        if summary.changed_paths and not ProjectWorkEvaluator._is_current_trusted_pass(snapshot):
            return _MISSING_VERIFICATION_ACTION
        if _count(summary.interrupted_threads, "interrupted_threads") > 0 or summary.stage == "需要恢复核验":
            return _RECOVERY_ACTION
        if summary.stage == "尚无活动" or snapshot.activity_status == "none":
            return None
        if ProjectWorkEvaluator._is_current_trusted_pass(snapshot):
            return _USER_CONFIRMATION_ACTION
        return None


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must be non-empty text up to {maximum} characters")
    if "\n" in normalized or "\r" in normalized or detect_sensitive_output(normalized):
        raise ValueError(f"{name} cannot contain sensitive or body content")
    if _BODY_MARKERS.search(normalized):
        raise ValueError(f"{name} cannot contain prompt, source, or tool content")
    return normalized


def _optional_text(value: object, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, name, maximum)


def _text_items(value: Iterable[str], name: str, minimum: int, maximum: int) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a sequence of text")
    try:
        items = tuple(_text(item, name, 240) for item in value)
    except TypeError as exc:
        raise ValueError(f"{name} must be a sequence of text") from exc
    if not minimum <= len(items) <= maximum:
        raise ValueError(f"{name} must contain {minimum} to {maximum} items")
    return items


def _enum(value: object, name: str, values: Iterable[str]) -> str:
    normalized = _text(value, name, 64)
    if normalized not in values:
        raise ValueError(f"{name} is invalid")
    return normalized


def _version(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _evidence_cursor(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("evidence_cursor must be a lowercase SHA-256 digest")
    return value


def _require_current_intent(
    connection: sqlite3.Connection, project_id: str, intent_version: int,
) -> None:
    row = connection.execute(
        "SELECT version FROM project_intents WHERE project_id=? AND status='active'", (project_id,),
    ).fetchone()
    if not row or int(row["version"]) != intent_version:
        current = int(row["version"]) if row else 0
        raise IntentVersionConflict(
            f"intent version {intent_version} is not current for {project_id}; current is {current}",
        )


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _intent_from_row(row: sqlite3.Row) -> ProjectIntent:
    return ProjectIntent(
        project_id=row["project_id"], outcome=row["outcome"],
        acceptance_criteria=tuple(json.loads(row["acceptance_criteria"])),
        constraints=tuple(json.loads(row["constraints"])), status=row["status"], version=row["version"],
        provenance=row["provenance"], created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _action_from_row(row: sqlite3.Row) -> ActionItem:
    return ActionItem(
        action_id=row["action_id"], project_id=row["project_id"], intent_version=row["intent_version"],
        title=row["title"], why_now=row["why_now"], completion_evidence=row["completion_evidence"],
        actor=row["actor"], status=row["status"], source=row["source"],
        evidence_cursor=row["evidence_cursor"], provenance=row["provenance"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _decision_from_row(row: sqlite3.Row) -> DecisionRequest:
    return DecisionRequest(
        decision_id=row["decision_id"], project_id=row["project_id"], intent_version=row["intent_version"],
        question=row["question"], reason=row["reason"], options=tuple(json.loads(row["options"])),
        status=row["status"], resolution=row["resolution"], provenance=row["provenance"], created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )
