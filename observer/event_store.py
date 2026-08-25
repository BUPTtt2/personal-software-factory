from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from observer.git_snapshot import GitSnapshot


@dataclass(frozen=True)
class VerificationRecord:
    kind: str
    command_class: str
    exit_code: int | None
    status: str


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    deduplication_key: str
    project_id: str
    thread_id: str
    turn_id: str | None
    event_type: str
    occurred_at: datetime
    payload_version: int
    redacted_payload: dict[str, Any]
    git_snapshot: GitSnapshot | None = None
    verification: VerificationRecord | None = None


@dataclass(frozen=True)
class AppendResult:
    duplicate: bool
    buffered: bool = False


@dataclass(frozen=True)
class ReplayResult:
    replayed: int
    duplicates: int
    failed: int


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
  thread_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  cwd TEXT,
  status TEXT,
  model TEXT,
  permission_mode TEXT,
  transcript_ref TEXT
);
CREATE TABLE IF NOT EXISTS turns (
  thread_id TEXT NOT NULL REFERENCES threads(thread_id),
  turn_id TEXT NOT NULL,
  started_at TEXT,
  stopped_at TEXT,
  status TEXT,
  prompt_summary TEXT,
  assistant_claim_summary TEXT,
  PRIMARY KEY (thread_id, turn_id)
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  deduplication_key TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  thread_id TEXT NOT NULL REFERENCES threads(thread_id),
  turn_id TEXT,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload_version INTEGER NOT NULL,
  redacted_payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS git_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
  repo_root TEXT,
  common_git_dir TEXT,
  branch TEXT,
  head_sha TEXT,
  dirty INTEGER,
  changed_paths TEXT NOT NULL,
  available INTEGER NOT NULL,
  error_code TEXT,
  captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_records (
  verification_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
  thread_id TEXT NOT NULL,
  turn_id TEXT,
  kind TEXT NOT NULL,
  command_class TEXT NOT NULL,
  exit_code INTEGER,
  status TEXT NOT NULL,
  observed_at TEXT NOT NULL
);
"""

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL,
  status TEXT NOT NULL,
  thread_id TEXT,
  judgement TEXT,
  reason TEXT,
  next_action TEXT,
  requires_user INTEGER,
  error_code TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS agent_runs_project_status_created
ON agent_runs(project_id,status,created_at);
"""

_SCHEMA_V3 = """
ALTER TABLE agent_runs RENAME TO agent_runs_v2;
CREATE TABLE agent_runs (
  run_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL,
  project_id TEXT NOT NULL,
  status TEXT NOT NULL,
  thread_id TEXT,
  judgement TEXT,
  reason TEXT,
  next_action TEXT,
  requires_user INTEGER,
  error_code TEXT,
  evidence_event_at TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  UNIQUE(project_id,idempotency_key)
);
INSERT INTO agent_runs(
  run_id,idempotency_key,project_id,status,thread_id,judgement,reason,next_action,
  requires_user,error_code,created_at,started_at,completed_at
)
SELECT
  run_id,idempotency_key,project_id,status,thread_id,judgement,reason,next_action,
  requires_user,error_code,created_at,started_at,completed_at
FROM agent_runs_v2;
DROP TABLE agent_runs_v2;
UPDATE agent_runs
SET status='failed',error_code='server_restarted',completed_at=COALESCE(completed_at,created_at)
WHERE status IN ('queued','running');
CREATE UNIQUE INDEX agent_runs_one_active_per_project
ON agent_runs(project_id) WHERE status IN ('queued','running');
CREATE INDEX agent_runs_project_status_created
ON agent_runs(project_id,status,created_at);
"""

_SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS project_intents (
  project_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  acceptance_criteria TEXT NOT NULL,
  constraints TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id,version)
);
CREATE INDEX IF NOT EXISTS project_intents_project_version
ON project_intents(project_id,version DESC);
CREATE TABLE IF NOT EXISTS action_items (
  action_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  intent_version INTEGER NOT NULL,
  title TEXT NOT NULL,
  why_now TEXT NOT NULL,
  completion_evidence TEXT NOT NULL,
  actor TEXT NOT NULL,
  status TEXT NOT NULL,
  source TEXT NOT NULL,
  evidence_cursor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS action_items_one_current_per_project
ON action_items(project_id) WHERE status IN ('proposed','ready','in_progress');
CREATE INDEX IF NOT EXISTS action_items_project_updated
ON action_items(project_id,updated_at DESC);
CREATE TABLE IF NOT EXISTS decision_requests (
  decision_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  intent_version INTEGER NOT NULL,
  question TEXT NOT NULL,
  reason TEXT NOT NULL,
  options TEXT NOT NULL,
  status TEXT NOT NULL,
  resolution TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS decision_requests_project_created
ON decision_requests(project_id,created_at DESC);
"""

_SCHEMA_V6_FROM_V4 = """
UPDATE project_intents
SET status=CASE WHEN version=(
  SELECT MAX(candidate.version) FROM project_intents AS candidate
  WHERE candidate.project_id=project_intents.project_id
) THEN 'active' ELSE 'superseded' END;
CREATE UNIQUE INDEX IF NOT EXISTS project_intents_one_active_per_project
ON project_intents(project_id) WHERE status='active';
DROP INDEX IF EXISTS action_items_one_current_per_project;
DROP INDEX IF EXISTS action_items_project_updated;
ALTER TABLE action_items RENAME TO action_items_v4;
CREATE TABLE action_items (
  action_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  intent_version INTEGER NOT NULL,
  title TEXT NOT NULL,
  why_now TEXT NOT NULL,
  completion_evidence TEXT NOT NULL,
  actor TEXT NOT NULL,
  status TEXT NOT NULL,
  source TEXT NOT NULL,
  evidence_cursor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(project_id,intent_version) REFERENCES project_intents(project_id,version)
);
INSERT INTO action_items SELECT * FROM action_items_v4;
DROP TABLE action_items_v4;
CREATE UNIQUE INDEX action_items_one_current_per_project
ON action_items(project_id) WHERE status IN ('proposed','ready','in_progress');
CREATE INDEX action_items_project_updated ON action_items(project_id,updated_at DESC);
DROP INDEX IF EXISTS decision_requests_project_created;
ALTER TABLE decision_requests RENAME TO decision_requests_v4;
CREATE TABLE decision_requests (
  decision_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  intent_version INTEGER NOT NULL,
  question TEXT NOT NULL,
  reason TEXT NOT NULL,
  options TEXT NOT NULL,
  status TEXT NOT NULL,
  resolution TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  FOREIGN KEY(project_id,intent_version) REFERENCES project_intents(project_id,version)
);
INSERT INTO decision_requests SELECT * FROM decision_requests_v4;
DROP TABLE decision_requests_v4;
CREATE INDEX decision_requests_project_created ON decision_requests(project_id,created_at DESC);
CREATE TABLE action_resolutions (
  resolution_id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL UNIQUE REFERENCES action_items(action_id),
  project_id TEXT NOT NULL,
  intent_version INTEGER NOT NULL,
  resolution_kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  verification_event_id TEXT UNIQUE REFERENCES events(event_id),
  resolved_at TEXT NOT NULL,
  FOREIGN KEY(project_id,intent_version) REFERENCES project_intents(project_id,version)
);
"""

_SCHEMA_V6_FROM_V5 = """
DROP INDEX IF EXISTS project_intents_project_version;
ALTER TABLE project_intents RENAME TO project_intents_v5;
CREATE TABLE project_intents (
  project_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  acceptance_criteria TEXT NOT NULL,
  constraints TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','superseded')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id,version)
);
INSERT INTO project_intents SELECT project_id,version,outcome,acceptance_criteria,constraints,
  'active',created_at,updated_at FROM project_intents_v5;
INSERT OR IGNORE INTO project_intents(
  project_id,version,outcome,acceptance_criteria,constraints,status,created_at,updated_at
)
SELECT project_id,intent_version,'Historical intent version','["Historical record preserved"]','[]',
  'superseded',MIN(created_at),MAX(updated_at)
FROM action_items GROUP BY project_id,intent_version;
INSERT OR IGNORE INTO project_intents(
  project_id,version,outcome,acceptance_criteria,constraints,status,created_at,updated_at
)
SELECT project_id,intent_version,'Historical intent version','["Historical record preserved"]','[]',
  'superseded',MIN(created_at),MAX(created_at)
FROM decision_requests GROUP BY project_id,intent_version;
DROP TABLE project_intents_v5;
CREATE UNIQUE INDEX project_intents_one_active_per_project
ON project_intents(project_id) WHERE status='active';
DROP INDEX IF EXISTS action_items_one_current_per_project;
DROP INDEX IF EXISTS action_items_project_updated;
ALTER TABLE action_items RENAME TO action_items_v5;
CREATE TABLE action_items (
  action_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  intent_version INTEGER NOT NULL,
  title TEXT NOT NULL,
  why_now TEXT NOT NULL,
  completion_evidence TEXT NOT NULL,
  actor TEXT NOT NULL,
  status TEXT NOT NULL,
  source TEXT NOT NULL,
  evidence_cursor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(project_id,intent_version) REFERENCES project_intents(project_id,version)
);
INSERT INTO action_items SELECT * FROM action_items_v5;
DROP TABLE action_items_v5;
CREATE UNIQUE INDEX action_items_one_current_per_project
ON action_items(project_id) WHERE status IN ('proposed','ready','in_progress');
CREATE INDEX action_items_project_updated ON action_items(project_id,updated_at DESC);
DROP INDEX IF EXISTS decision_requests_project_created;
ALTER TABLE decision_requests RENAME TO decision_requests_v5;
CREATE TABLE decision_requests (
  decision_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  intent_version INTEGER NOT NULL,
  question TEXT NOT NULL,
  reason TEXT NOT NULL,
  options TEXT NOT NULL,
  status TEXT NOT NULL,
  resolution TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  FOREIGN KEY(project_id,intent_version) REFERENCES project_intents(project_id,version)
);
INSERT INTO decision_requests SELECT * FROM decision_requests_v5;
DROP TABLE decision_requests_v5;
CREATE INDEX decision_requests_project_created ON decision_requests(project_id,created_at DESC);
CREATE TABLE action_resolutions (
  resolution_id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL UNIQUE REFERENCES action_items(action_id),
  project_id TEXT NOT NULL,
  intent_version INTEGER NOT NULL,
  resolution_kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  verification_event_id TEXT UNIQUE REFERENCES events(event_id),
  resolved_at TEXT NOT NULL,
  FOREIGN KEY(project_id,intent_version) REFERENCES project_intents(project_id,version)
);
"""

_SCHEMA_V7 = """
ALTER TABLE action_resolutions RENAME TO action_resolutions_v6;
CREATE TEMP TABLE migration_untrusted_verified_actions (
  action_id TEXT PRIMARY KEY,
  reason TEXT NOT NULL
);
INSERT INTO migration_untrusted_verified_actions(action_id,reason)
SELECT action_items.action_id,
  CASE WHEN EXISTS(
    SELECT 1 FROM action_resolutions_v6
    WHERE action_resolutions_v6.action_id=action_items.action_id
      AND action_resolutions_v6.resolution_kind='user_confirmation'
  ) THEN 'legacy user confirmation lacked a trusted decision chain'
  ELSE 'legacy verified action lacked an auditable resolution' END
FROM action_items
WHERE action_items.status='verified'
  AND NOT EXISTS(
    SELECT 1 FROM action_resolutions_v6
    WHERE action_resolutions_v6.action_id=action_items.action_id
      AND action_resolutions_v6.resolution_kind='trusted_verification'
      AND action_resolutions_v6.verification_event_id IS NOT NULL
  );
CREATE TABLE action_resolutions (
  resolution_id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL UNIQUE REFERENCES action_items(action_id),
  project_id TEXT NOT NULL,
  intent_version INTEGER NOT NULL,
  resolution_kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  authority TEXT NOT NULL,
  reason TEXT NOT NULL,
  verification_event_id TEXT UNIQUE REFERENCES events(event_id),
  resolved_at TEXT NOT NULL,
  FOREIGN KEY(project_id,intent_version) REFERENCES project_intents(project_id,version)
);
INSERT INTO action_resolutions(
  resolution_id,action_id,project_id,intent_version,resolution_kind,actor,authority,reason,
  verification_event_id,resolved_at
)
SELECT resolution_id,action_id,project_id,intent_version,resolution_kind,actor,
  CASE WHEN resolution_kind='trusted_verification' AND verification_event_id IS NOT NULL
    THEN 'trusted_receipt' ELSE 'legacy_unverified' END,
  'legacy_record',verification_event_id,resolved_at
FROM action_resolutions_v6
WHERE action_id NOT IN (SELECT action_id FROM migration_untrusted_verified_actions);
DROP TABLE action_resolutions_v6;
UPDATE action_items SET status='cancelled'
WHERE action_id IN (SELECT action_id FROM migration_untrusted_verified_actions);
INSERT INTO action_resolutions(
  resolution_id,action_id,project_id,intent_version,resolution_kind,actor,authority,reason,
  verification_event_id,resolved_at
)
SELECT lower(hex(randomblob(16))),action_id,project_id,intent_version,'migration_cancellation','migration',
  'legacy_unverified',migration_untrusted_verified_actions.reason,NULL,updated_at
FROM action_items JOIN migration_untrusted_verified_actions USING(action_id);
DROP TABLE migration_untrusted_verified_actions;
"""

_SCHEMA_V8 = """
ALTER TABLE project_intents
ADD COLUMN provenance TEXT NOT NULL DEFAULT 'legacy_unverified'
CHECK(provenance IN ('legacy_unverified','local_user'));
ALTER TABLE action_items
ADD COLUMN provenance TEXT NOT NULL DEFAULT 'legacy_unverified'
CHECK(provenance IN ('legacy_unverified','local_user','policy'));
ALTER TABLE decision_requests
ADD COLUMN provenance TEXT NOT NULL DEFAULT 'legacy_unverified'
CHECK(provenance IN ('legacy_unverified','local_user'));
CREATE TEMP TABLE migration_v8_untrusted_verified_actions (
  action_id TEXT PRIMARY KEY,
  reason TEXT NOT NULL
);
INSERT INTO migration_v8_untrusted_verified_actions(action_id,reason)
SELECT action_items.action_id,
  CASE WHEN EXISTS(
    SELECT 1 FROM action_resolutions
    WHERE action_resolutions.action_id=action_items.action_id
      AND action_resolutions.resolution_kind='user_confirmation'
  ) THEN 'legacy user confirmation lacked a trusted decision chain'
  ELSE 'legacy verified action lacked an auditable resolution' END
FROM action_items
WHERE action_items.status='verified'
  AND NOT EXISTS(
    SELECT 1 FROM action_resolutions
    WHERE action_resolutions.action_id=action_items.action_id
      AND action_resolutions.resolution_kind='trusted_verification'
      AND action_resolutions.authority='trusted_receipt'
      AND action_resolutions.verification_event_id IS NOT NULL
  );
ALTER TABLE action_resolutions RENAME TO action_resolutions_v7;
CREATE TABLE action_resolutions (
  resolution_id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL UNIQUE REFERENCES action_items(action_id),
  project_id TEXT NOT NULL,
  intent_version INTEGER NOT NULL,
  resolution_kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  authority TEXT NOT NULL,
  reason TEXT NOT NULL,
  verification_event_id TEXT UNIQUE REFERENCES events(event_id),
  decision_id TEXT UNIQUE REFERENCES decision_requests(decision_id),
  resolved_at TEXT NOT NULL,
  FOREIGN KEY(project_id,intent_version) REFERENCES project_intents(project_id,version)
);
INSERT INTO action_resolutions(
  resolution_id,action_id,project_id,intent_version,resolution_kind,actor,authority,reason,
  verification_event_id,decision_id,resolved_at
)
SELECT resolution_id,action_id,project_id,intent_version,resolution_kind,actor,authority,reason,
  verification_event_id,NULL,resolved_at
FROM action_resolutions_v7
WHERE action_id NOT IN (SELECT action_id FROM migration_v8_untrusted_verified_actions);
DROP TABLE action_resolutions_v7;
UPDATE action_items SET status='cancelled'
WHERE action_id IN (SELECT action_id FROM migration_v8_untrusted_verified_actions);
INSERT INTO action_resolutions(
  resolution_id,action_id,project_id,intent_version,resolution_kind,actor,authority,reason,
  verification_event_id,decision_id,resolved_at
)
SELECT lower(hex(randomblob(16))),action_id,project_id,intent_version,'migration_cancellation','migration',
  'legacy_unverified',migration_v8_untrusted_verified_actions.reason,NULL,NULL,updated_at
FROM action_items JOIN migration_v8_untrusted_verified_actions USING(action_id);
DROP TABLE migration_v8_untrusted_verified_actions;
"""

_LATEST_SCHEMA_VERSION = 8


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class EventStore:
    def __init__(self, db_path: Path, buffer_dir: Path):
        self.db_path = db_path
        self.buffer_dir = buffer_dir

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=0.05)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=50")
        return connection

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            with connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
                row = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
                version = int(row[0]) if row else 0
                if version > _LATEST_SCHEMA_VERSION:
                    raise RuntimeError(f"unsupported schema version: {version}")
                connection.executescript(_SCHEMA_V1)
                if version == 0:
                    connection.execute("INSERT INTO schema_meta(version) VALUES (1)")
                    version = 1
                if version < 2:
                    connection.executescript(_SCHEMA_V2)
                    connection.execute("UPDATE schema_meta SET version=2")
                    version = 2
                if version < 3:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        "DROP INDEX IF EXISTS agent_runs_project_status_created;\n"
                        f"{_SCHEMA_V3}\n"
                        "UPDATE schema_meta SET version=3;\n"
                        "COMMIT;"
                    )
                    version = 3
                if version < 4:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        f"{_SCHEMA_V4}\n"
                        "UPDATE schema_meta SET version=4;\n"
                        "COMMIT;"
                    )
                    version = 4
                if version < 5:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        f"{_SCHEMA_V6_FROM_V4}\n"
                        "UPDATE schema_meta SET version=6;\n"
                        "COMMIT;"
                    )
                    version = 6
                if version < 6:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        f"{_SCHEMA_V6_FROM_V5}\n"
                        "UPDATE schema_meta SET version=6;\n"
                        "COMMIT;"
                    )
                    version = 6
                if version < 7:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        f"{_SCHEMA_V7}\n"
                        "UPDATE schema_meta SET version=7;\n"
                        "COMMIT;"
                    )
                    version = 7
                if version < 8:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        f"{_SCHEMA_V8}\n"
                        "UPDATE schema_meta SET version=8;\n"
                        "COMMIT;"
                    )

    def append_event(self, record: EventRecord) -> AppendResult:
        try:
            with closing(self.connect()) as connection:
                with connection:
                    exists = connection.execute(
                        "SELECT 1 FROM events WHERE deduplication_key=?", (record.deduplication_key,),
                    ).fetchone()
                    if exists:
                        return AppendResult(duplicate=True)
                    timestamp = _iso(record.occurred_at)
                    connection.execute(
                        "INSERT INTO projects(project_id,first_seen_at,last_seen_at) VALUES(?,?,?) "
                        "ON CONFLICT(project_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                        (record.project_id, timestamp, timestamp),
                    )
                    connection.execute(
                        "INSERT INTO threads(thread_id,project_id,first_seen_at,last_seen_at,status) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(thread_id) DO UPDATE SET "
                        "last_seen_at=MAX(threads.last_seen_at,excluded.last_seen_at),"
                        "status=CASE WHEN excluded.last_seen_at>=threads.last_seen_at "
                        "THEN excluded.status ELSE threads.status END",
                        (
                            record.thread_id,
                            record.project_id,
                            timestamp,
                            timestamp,
                            "ended" if record.event_type == "session_ended" else
                            "stopped" if record.event_type in {"turn_stopped", "verification_receipt"} else
                            "working",
                        ),
                    )
                    if record.turn_id:
                        connection.execute(
                            "INSERT INTO turns(thread_id,turn_id,started_at,status) VALUES(?,?,?,?) "
                            "ON CONFLICT(thread_id,turn_id) DO NOTHING",
                            (record.thread_id, record.turn_id, timestamp, "working"),
                        )
                        if record.event_type == "turn_started":
                            connection.execute(
                                "UPDATE turns SET prompt_summary=? WHERE thread_id=? AND turn_id=?",
                                (record.redacted_payload.get("promptSummary"), record.thread_id, record.turn_id),
                            )
                        elif record.event_type == "code_change_observed":
                            connection.execute(
                                "UPDATE turns SET status=CASE WHEN status='verification_failed' THEN status "
                                "ELSE 'evidence_incomplete' END WHERE thread_id=? AND turn_id=?",
                                (record.thread_id, record.turn_id),
                            )
                        elif record.event_type == "verification_observed" and record.verification:
                            if record.verification.status == "failed":
                                connection.execute(
                                    "UPDATE turns SET status='verification_failed' WHERE thread_id=? AND turn_id=?",
                                    (record.thread_id, record.turn_id),
                                )
                            elif record.verification.status == "passed":
                                connection.execute(
                                    "UPDATE turns SET status='verification_passed' "
                                    "WHERE thread_id=? AND turn_id=? AND status='evidence_incomplete'",
                                    (record.thread_id, record.turn_id),
                                )
                        elif record.event_type == "turn_stopped":
                            connection.execute(
                                "UPDATE turns SET stopped_at=?,status=CASE WHEN status IN "
                                "('evidence_incomplete','verification_passed','verification_failed','awaiting_user') "
                                "THEN status ELSE 'stopped' END,assistant_claim_summary=? "
                                "WHERE thread_id=? AND turn_id=?",
                                (timestamp, record.redacted_payload.get("assistantClaimSummary"),
                                 record.thread_id, record.turn_id),
                            )
                    connection.execute(
                        "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                        (record.event_id, record.deduplication_key, record.project_id, record.thread_id,
                         record.turn_id, record.event_type, timestamp, record.payload_version,
                         _json(record.redacted_payload)),
                    )
                    if record.git_snapshot and self._should_store_snapshot(
                        connection, record.project_id, record.git_snapshot, record.occurred_at,
                    ):
                        snap = record.git_snapshot
                        connection.execute(
                            "INSERT INTO git_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (str(uuid.uuid4()), record.event_id, snap.repo_root, snap.common_git_dir,
                             snap.branch, snap.head_sha, None if snap.dirty is None else int(snap.dirty),
                             _json(snap.changed_paths), int(snap.available), snap.error_code, timestamp),
                        )
                    if record.verification:
                        item = record.verification
                        connection.execute(
                            "INSERT INTO verification_records VALUES(?,?,?,?,?,?,?,?,?)",
                            (str(uuid.uuid4()), record.event_id, record.thread_id, record.turn_id,
                             item.kind, item.command_class, item.exit_code, item.status, timestamp),
                        )
            return AppendResult(duplicate=False)
        except sqlite3.IntegrityError as exc:
            if "deduplication_key" in str(exc):
                return AppendResult(duplicate=True)
            raise
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            self._buffer(record)
            return AppendResult(duplicate=False, buffered=True)

    def _should_store_snapshot(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        snapshot: GitSnapshot,
        captured_at: datetime,
    ) -> bool:
        row = connection.execute(
            "SELECT git_snapshots.repo_root,git_snapshots.common_git_dir,git_snapshots.branch,"
            "git_snapshots.head_sha,git_snapshots.dirty,git_snapshots.changed_paths,"
            "git_snapshots.available,git_snapshots.error_code,git_snapshots.captured_at "
            "FROM git_snapshots JOIN events ON events.event_id=git_snapshots.event_id "
            "WHERE events.project_id=? "
            "ORDER BY git_snapshots.captured_at DESC,git_snapshots.snapshot_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if not row:
            return True
        previous_signature = tuple(row[:8])
        current_signature = (
            snapshot.repo_root,
            snapshot.common_git_dir,
            snapshot.branch,
            snapshot.head_sha,
            None if snapshot.dirty is None else int(snapshot.dirty),
            _json(snapshot.changed_paths),
            int(snapshot.available),
            snapshot.error_code,
        )
        if previous_signature != current_signature:
            return True
        previous_at = datetime.fromisoformat(row[8])
        if previous_at.tzinfo is None:
            previous_at = previous_at.replace(tzinfo=timezone.utc)
        current_at = captured_at.astimezone(timezone.utc)
        return current_at < previous_at.astimezone(timezone.utc) or \
            current_at - previous_at.astimezone(timezone.utc) >= timedelta(minutes=30)

    def _buffer(self, record: EventRecord) -> None:
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(record)
        payload["occurred_at"] = _iso(record.occurred_at)
        path = self.buffer_dir / f"{record.event_id}.json"
        if path.exists():
            return
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def buffer_event(self, record: EventRecord) -> None:
        self._buffer(record)

    def replay_buffer(self) -> ReplayResult:
        replayed = duplicates = failed = 0
        for path in sorted(self.buffer_dir.glob("*.json")) if self.buffer_dir.exists() else ():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                snap = GitSnapshot(**payload["git_snapshot"]) if payload.get("git_snapshot") else None
                verification = VerificationRecord(**payload["verification"]) if payload.get("verification") else None
                payload["occurred_at"] = datetime.fromisoformat(payload["occurred_at"])
                payload["git_snapshot"] = snap
                payload["verification"] = verification
                result = self.append_event(EventRecord(**payload))
                duplicates += int(result.duplicate)
                replayed += int(not result.duplicate and not result.buffered)
                if not result.buffered:
                    path.unlink()
            except Exception:
                failed += 1
        return ReplayResult(replayed, duplicates, failed)

    def list_recent(self, limit: int = 20) -> list[sqlite3.Row]:
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            return list(connection.execute(
                "SELECT * FROM events ORDER BY occurred_at DESC LIMIT ?", (limit,),
            ))

    def get_turn(self, thread_id: str, turn_id: str) -> sqlite3.Row | None:
        with closing(self.connect()) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT * FROM turns WHERE thread_id=? AND turn_id=?", (thread_id, turn_id),
            ).fetchone()
