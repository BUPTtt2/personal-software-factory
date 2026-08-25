import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from observer.event_store import EventRecord, EventStore, VerificationRecord
import observer.event_store as event_store_module
from observer.git_snapshot import GitSnapshot


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.store = EventStore(root / "factory.sqlite", root / "buffer")
        self.store.initialize()

    def event(self) -> EventRecord:
        return EventRecord(
            event_id="event-1", deduplication_key="dedupe-1", project_id="demo",
            thread_id="thread-1", turn_id="turn-1", event_type="verification_observed",
            occurred_at=datetime(2026, 8, 13, tzinfo=timezone.utc), payload_version=1,
            redacted_payload={"safe": "内容"},
            git_snapshot=GitSnapshot("/repo", "/repo/.git", "main", "abc", True, ("a.py",), True, None),
            verification=VerificationRecord("test", "python_unittest", 0, "passed"),
        )

    def test_initializes_wal_schema_without_sensitive_columns(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"projects", "threads", "turns", "events", "git_snapshots", "verification_records"} <= tables)
            columns = [row[1] for table in tables for row in connection.execute(f"PRAGMA table_info({table})")]
            self.assertFalse({"environment", "tool_output", "source_content"} & set(columns))

    def test_migrates_version_one_database_to_agent_runs(self):
        root = Path(tempfile.mkdtemp())
        store = EventStore(root / "factory.sqlite", root / "buffer")
        with closing(sqlite3.connect(store.db_path)) as connection:
            with connection:
                connection.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
                connection.execute("INSERT INTO schema_meta(version) VALUES (1)")
                connection.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO events(event_id) VALUES ('kept-event')")

        store.initialize()

        with closing(sqlite3.connect(store.db_path)) as connection:
            version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(version, 8)
            self.assertIn("agent_runs", tables)
            self.assertTrue({"project_intents", "action_items", "decision_requests"} <= tables)
            self.assertEqual(connection.execute("SELECT event_id FROM events").fetchone()[0], "kept-event")

    def test_migrates_v4_intent_history_without_losing_versions(self):
        root = Path(tempfile.mkdtemp())
        store = EventStore(root / "factory.sqlite", root / "buffer")
        with closing(sqlite3.connect(store.db_path)) as connection:
            connection.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
            connection.execute("INSERT INTO schema_meta(version) VALUES (4)")
            connection.executescript(event_store_module._SCHEMA_V4)
            connection.execute(
                "INSERT INTO project_intents VALUES "
                "('demo',1,'Old','[\"Old\"]','[]','active','earlier','earlier'),"
                "('demo',2,'Current','[\"Current\"]','[]','active','now','now')"
            )
            connection.commit()

        store.initialize()

        with closing(sqlite3.connect(store.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_meta").fetchone()[0], 8)
            self.assertEqual(
                connection.execute(
                    "SELECT version,outcome,status FROM project_intents WHERE project_id='demo' ORDER BY version"
                ).fetchall(),
                [(1, "Old", "superseded"), (2, "Current", "active")],
            )

    def test_migrates_v5_stale_action_reference_without_breaking_history_link(self):
        root = Path(tempfile.mkdtemp())
        store = EventStore(root / "factory.sqlite", root / "buffer")
        with closing(sqlite3.connect(store.db_path)) as connection:
            connection.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
            connection.execute("INSERT INTO schema_meta(version) VALUES (4)")
            connection.executescript(event_store_module._SCHEMA_V4)
            connection.execute(
                "INSERT INTO project_intents VALUES "
                "('demo',1,'Old','[\"Old\"]','[]','active','earlier','earlier'),"
                "('demo',2,'Current','[\"Current\"]','[]','active','now','now')"
            )
            connection.execute(
                "INSERT INTO action_items VALUES("
                "'action-1','demo',1,'Verify','Need proof','trusted_verification_current',"
                "'external','verified','policy','" + "a" * 64 + "','earlier','earlier')"
            )
            connection.executescript("""
                DROP INDEX IF EXISTS project_intents_project_version;
                ALTER TABLE project_intents RENAME TO project_intents_v4;
                CREATE TABLE project_intents (
                  project_id TEXT PRIMARY KEY, version INTEGER NOT NULL, outcome TEXT NOT NULL,
                  acceptance_criteria TEXT NOT NULL, constraints TEXT NOT NULL,
                  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO project_intents
                SELECT project_id,version,outcome,acceptance_criteria,constraints,status,created_at,updated_at
                FROM project_intents_v4 WHERE version=(
                  SELECT MAX(candidate.version) FROM project_intents_v4 AS candidate
                  WHERE candidate.project_id=project_intents_v4.project_id
                );
                DROP TABLE project_intents_v4;
            """)
            connection.execute("UPDATE schema_meta SET version=5")
            connection.commit()

        store.initialize()

        with closing(sqlite3.connect(store.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_meta").fetchone()[0], 8)
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM project_intents WHERE project_id='demo' AND version=1"
                ).fetchone()[0],
                "superseded",
            )
            self.assertEqual(
                connection.execute("SELECT intent_version FROM action_items WHERE action_id='action-1'").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT status FROM action_items WHERE action_id='action-1'").fetchone()[0],
                "cancelled",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT resolution_kind FROM action_resolutions WHERE action_id='action-1'"
                ).fetchone()[0],
                "migration_cancellation",
            )

    def test_migration_only_cancels_untrusted_legacy_verifications(self):
        root = Path(tempfile.mkdtemp())
        store = EventStore(root / "factory.sqlite", root / "buffer")
        with closing(sqlite3.connect(store.db_path)) as connection:
            connection.executescript(event_store_module._SCHEMA_V1)
            connection.execute("INSERT INTO schema_meta(version) VALUES (6)")
            connection.executescript(event_store_module._SCHEMA_V4)
            connection.executescript(event_store_module._SCHEMA_V6_FROM_V4)
            connection.execute(
                "INSERT INTO project_intents VALUES "
                "('demo',1,'Outcome','[\"Criterion\"]','[]','active','now','now')"
            )
            for action_id, status in (
                ("missing-resolution", "verified"),
                ("legacy-user-resolution", "verified"),
                ("trusted-resolution", "verified"),
                ("ordinary-cancellation", "cancelled"),
            ):
                connection.execute(
                    "INSERT INTO action_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (action_id, "demo", 1, "Accept", "Need proof", "user_confirms_acceptance", "user",
                     status, "user", "a" * 64, "now", "now"),
                )
            connection.execute(
                "INSERT INTO projects VALUES('demo','now','now')"
            )
            connection.execute(
                "INSERT INTO threads(thread_id,project_id,first_seen_at,last_seen_at) "
                "VALUES('thread-1','demo','now','now')"
            )
            connection.execute(
                "INSERT INTO events VALUES('receipt-1','dedupe-1','demo','thread-1',NULL,"
                "'verification_receipt','later',1,'{}')"
            )
            connection.execute(
                "INSERT INTO action_resolutions VALUES(?,?,?,?,?,?,?,?)",
                ("user-resolution", "legacy-user-resolution", "demo", 1, "user_confirmation", "user", None, "now"),
            )
            connection.execute(
                "INSERT INTO action_resolutions VALUES(?,?,?,?,?,?,?,?)",
                ("trusted-resolution", "trusted-resolution", "demo", 1, "trusted_verification", "verification",
                 "receipt-1", "later"),
            )
            connection.commit()

        store.initialize()

        with closing(sqlite3.connect(store.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_meta").fetchone()[0], 8)
            statuses = dict(connection.execute("SELECT action_id,status FROM action_items"))
            self.assertEqual(statuses["missing-resolution"], "cancelled")
            self.assertEqual(statuses["legacy-user-resolution"], "cancelled")
            self.assertEqual(statuses["trusted-resolution"], "verified")
            self.assertEqual(statuses["ordinary-cancellation"], "cancelled")
            resolutions = {
                row[0]: (row[1], row[2], row[3])
                for row in connection.execute(
                    "SELECT action_id,resolution_kind,authority,reason FROM action_resolutions"
                )
            }
            self.assertEqual(
                resolutions["missing-resolution"],
                ("migration_cancellation", "legacy_unverified", "legacy verified action lacked an auditable resolution"),
            )
            self.assertEqual(
                resolutions["legacy-user-resolution"],
                ("migration_cancellation", "legacy_unverified", "legacy user confirmation lacked a trusted decision chain"),
            )
            self.assertEqual(resolutions["trusted-resolution"][0], "trusted_verification")
            self.assertNotIn("ordinary-cancellation", resolutions)
            self.assertEqual(
                connection.execute(
                    "SELECT provenance FROM project_intents WHERE project_id='demo' AND version=1"
                ).fetchone()[0],
                "legacy_unverified",
            )

    def test_v8_downgrades_caller_asserted_v7_user_authority(self):
        root = Path(tempfile.mkdtemp())
        store = EventStore(root / "factory.sqlite", root / "buffer")
        with closing(sqlite3.connect(store.db_path)) as connection:
            connection.executescript(event_store_module._SCHEMA_V1)
            connection.execute("INSERT INTO schema_meta(version) VALUES (7)")
            connection.executescript(event_store_module._SCHEMA_V4)
            connection.executescript(event_store_module._SCHEMA_V6_FROM_V4)
            connection.executescript(event_store_module._SCHEMA_V7)
            connection.execute(
                "INSERT INTO project_intents VALUES "
                "('demo',1,'Outcome','[\"Criterion\"]','[]','active','now','now')"
            )
            connection.execute(
                "INSERT INTO action_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("caller-authority", "demo", 1, "Accept", "Need confirmation", "user_confirms_acceptance",
                 "user", "verified", "user", "a" * 64, "now", "now"),
            )
            connection.execute(
                "INSERT INTO action_resolutions VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("user-resolution", "caller-authority", "demo", 1, "user_confirmation", "user",
                 "same_origin_local_user", "local_user_confirmation", None, "now"),
            )
            connection.commit()

        store.initialize()

        with closing(sqlite3.connect(store.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM action_items WHERE action_id='caller-authority'"
                ).fetchone()[0],
                "cancelled",
            )
            self.assertEqual(
                tuple(connection.execute(
                    "SELECT resolution_kind,authority,reason FROM action_resolutions "
                    "WHERE action_id='caller-authority'"
                ).fetchone()),
                ("migration_cancellation", "legacy_unverified",
                 "legacy user confirmation lacked a trusted decision chain"),
            )

    def test_failed_v3_migration_rolls_back_cleanly(self):
        root = Path(tempfile.mkdtemp())
        store = EventStore(root / "factory.sqlite", root / "buffer")
        with closing(sqlite3.connect(store.db_path)) as connection:
            connection.executescript(event_store_module._SCHEMA_V1)
            connection.executescript(event_store_module._SCHEMA_V2)
            connection.execute("INSERT INTO schema_meta(version) VALUES (2)")
            connection.execute(
                "INSERT INTO agent_runs(run_id,idempotency_key,project_id,status,created_at) "
                "VALUES('kept-run','kept-key','demo','failed','now')"
            )
            connection.commit()
        with mock.patch.object(
            event_store_module, "_SCHEMA_V3", event_store_module._SCHEMA_V3 + "\nINVALID SQL;",
        ):
            with self.assertRaises(sqlite3.OperationalError):
                store.initialize()
        with closing(sqlite3.connect(store.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_meta").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT run_id FROM agent_runs").fetchone()[0], "kept-run")
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runs_v2'"
            ).fetchone())
        store.initialize()

    def test_append_is_atomic_and_idempotent(self):
        first = self.store.append_event(self.event())
        second = self.store.append_event(self.event())
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM git_snapshots").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM verification_records").fetchone()[0], 1)
            payload = connection.execute("SELECT redacted_payload FROM events").fetchone()[0]
            self.assertEqual(payload, json.dumps({"safe": "内容"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    def test_thread_lifecycle_tracks_turn_stop_restart_and_session_end(self):
        started = replace(
            self.event(), event_id="event-start", deduplication_key="dedupe-start",
            event_type="turn_started", verification=None,
        )
        stopped = replace(
            self.event(), event_id="event-stop", deduplication_key="dedupe-stop",
            event_type="turn_stopped", verification=None,
        )
        next_turn = replace(
            self.event(), event_id="event-next", deduplication_key="dedupe-next",
            turn_id="turn-2", event_type="turn_started", verification=None,
        )
        ended = replace(
            self.event(), event_id="event-end", deduplication_key="dedupe-end",
            turn_id=None, event_type="session_ended", verification=None,
        )

        self.store.append_event(started)
        self.store.append_event(stopped)
        self.assertEqual(self.thread_status(), "stopped")
        self.store.append_event(next_turn)
        self.assertEqual(self.thread_status(), "working")
        self.store.append_event(ended)
        self.assertEqual(self.thread_status(), "ended")

    def test_coalesces_identical_git_snapshots_until_heartbeat(self):
        first = self.event()
        duplicate_snapshot = replace(
            first,
            event_id="event-2",
            deduplication_key="dedupe-2",
            occurred_at=first.occurred_at + timedelta(minutes=1),
        )
        heartbeat = replace(
            first,
            event_id="event-3",
            deduplication_key="dedupe-3",
            occurred_at=first.occurred_at + timedelta(minutes=31),
        )
        changed = replace(
            first,
            event_id="event-4",
            deduplication_key="dedupe-4",
            occurred_at=first.occurred_at + timedelta(minutes=32),
            git_snapshot=GitSnapshot(
                "/repo", "/repo/.git", "main", "def", True, ("b.py",), True, None,
            ),
        )

        for record in (first, duplicate_snapshot, heartbeat, changed):
            self.store.append_event(record)

        with closing(sqlite3.connect(self.store.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM events").fetchone()[0], 4)
            rows = connection.execute(
                "SELECT git_snapshots.head_sha FROM git_snapshots "
                "ORDER BY git_snapshots.captured_at,git_snapshots.snapshot_id"
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual([row[0] for row in rows].count("abc"), 2)
        self.assertEqual(rows[-1][0], "def")

    def thread_status(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            return connection.execute(
                "SELECT status FROM threads WHERE thread_id='thread-1'"
            ).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
