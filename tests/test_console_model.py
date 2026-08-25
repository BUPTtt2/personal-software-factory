import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from observer.console_model import build_overview
from observer.event_store import EventStore
from observer.project_work import ProjectWorkStore


class ConsoleModelTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "config").mkdir()
        projects = []
        for project_id in ("working", "incomplete", "interrupted", "stopped", "empty"):
            projects.append({
                "id": project_id,
                "displayName": project_id.title(),
                "roots": [f"/tmp/{project_id}"],
                "gitRemotes": [],
                "enabled": True,
            })
        (self.root / "config/projects.yaml").write_text(
            json.dumps({"projects": projects}), encoding="utf-8",
        )
        self.store = EventStore(self.root / "data/factory.sqlite", self.root / "buffer")
        self.store.initialize()
        self._insert_project_state("working", "working")
        self._insert_project_state("incomplete", "stopped", turn_status="evidence_incomplete")
        self._insert_project_state("interrupted", "interrupted", turn_status="interrupted")
        self._insert_project_state("stopped", "ended", turn_status="stopped")

    def _insert_project_state(self, project_id, thread_status, turn_status="working"):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                "INSERT INTO projects(project_id,first_seen_at,last_seen_at) VALUES(?,?,?)",
                (project_id, "2026-08-13T08:00:00+00:00", "2026-08-13T09:00:00+00:00"),
                )
                connection.execute(
                "INSERT INTO threads(thread_id,project_id,first_seen_at,last_seen_at,status) "
                "VALUES(?,?,?,?,?)",
                (f"thread-{project_id}", project_id, "2026-08-13T08:00:00+00:00",
                 "2026-08-13T09:00:00+00:00", thread_status),
                )
                connection.execute(
                "INSERT INTO turns(thread_id,turn_id,started_at,stopped_at,status,prompt_summary,assistant_claim_summary) "
                "VALUES(?,?,?,?,?,?,?)",
                (f"thread-{project_id}", f"turn-{project_id}", "2026-08-13T08:00:00+00:00",
                 "2026-08-13T09:00:00+00:00", turn_status, "PRIVATE_PROMPT", "PRIVATE_CLAIM"),
                )
                connection.execute(
                "INSERT INTO events(event_id,deduplication_key,project_id,thread_id,turn_id,event_type,"
                "occurred_at,payload_version,redacted_payload) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"event-{project_id}", f"dedupe-{project_id}", project_id, f"thread-{project_id}",
                 f"turn-{project_id}", "turn_stopped", "2026-08-13T09:00:00+00:00", 1,
                 '{"private":"PRIVATE_PAYLOAD"}'),
                )

    def snapshots(self):
        return {
            item.project_id: item for item in build_overview(
                self.root, now=datetime(2026, 8, 13, 9, 15, tzinfo=timezone.utc),
            )
        }

    def test_prioritizes_real_project_states(self):
        snapshots = self.snapshots()
        self.assertEqual([item.source for item in snapshots["working"].evidence], ["Observer", "Git", "Verifier"])
        self.assertIn("没有 Git 快照", snapshots["working"].evidence[1].summary)
        self.assertFalse(snapshots["working"].can_run_agent)
        self.assertIn("正在", snapshots["working"].judgement)
        self.assertTrue(snapshots["incomplete"].can_run_agent)
        self.assertIn("验证", snapshots["incomplete"].judgement)
        self.assertIn("恢复", snapshots["interrupted"].next_action)
        self.assertIn("收口", snapshots["stopped"].judgement)
        self.assertIn("没有新事件", snapshots["empty"].judgement)

    def test_public_snapshot_excludes_stored_text_and_payloads(self):
        rendered = json.dumps([asdict(item) for item in build_overview(self.root)], ensure_ascii=False)
        self.assertNotIn("PRIVATE_PROMPT", rendered)
        self.assertNotIn("PRIVATE_CLAIM", rendered)
        self.assertNotIn("PRIVATE_PAYLOAD", rendered)
        self.assertNotIn("prompt_summary", rendered)
        self.assertNotIn("assistant_claim_summary", rendered)
        self.assertNotIn("redacted_payload", rendered)

    def test_public_work_summary_is_bounded_and_actionable(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO git_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    ("summary-snapshot", "event-stopped", "/tmp/stopped", "/tmp/stopped/.git",
                     "codex/summary", "1234567890abcdef", 1,
                     '["a.py","b.py","c.py","d.py"]', 1, None,
                     "2026-08-13T09:00:00+00:00"),
                )
        summary = self.snapshots()["stopped"].to_public_dict()["work_summary"]
        self.assertEqual(summary["branch"], "codex/summary")
        self.assertEqual(summary["head"], "12345678")
        self.assertEqual(summary["changed_paths"], ("a.py", "b.py", "c.py"))
        self.assertIn(summary["stage"], {"等待验证", "验证失败", "已收口", "需要恢复核验", "工作中"})
        self.assertEqual(summary["verification_freshness"], "unknown")

    def test_overview_exposes_goal_and_one_safe_current_action(self):
        now = datetime(2026, 8, 13, 9, 5, tzinfo=timezone.utc)
        store = ProjectWorkStore(self.store.db_path)
        intent = store.save_intent(
            "incomplete", "Deliver a verified loop", ["Trusted verification passes"],
            ["No project writes"], 0, now,
        )
        store.create_action(
            "incomplete", intent.version, "补齐可信验证", "变更尚无当前可信验证",
            "trusted_verification_current", "external", "ready", "policy",
            self.snapshots()["incomplete"].evidence_cursor, now,
        )

        project = self.snapshots()["incomplete"]

        self.assertEqual(project.intent_status, "active")
        self.assertEqual(project.outcome_summary, "Deliver a verified loop")
        self.assertEqual(project.current_action.title, "补齐可信验证")
        self.assertEqual(project.current_action.actor, "external")
        self.assertEqual(project.current_action.completion_evidence, "trusted_verification_current")
        self.assertFalse(project.can_confirm_achievement)
        self.assertIsNone(project.open_decision)

    def test_overview_only_authorizes_server_confirmable_achievement_action(self):
        now = datetime(2026, 8, 13, 9, 5, tzinfo=timezone.utc)
        store = ProjectWorkStore(self.store.db_path)
        intent = store.save_intent(
            "empty", "Deliver a verified loop", ["User accepts the result"], [], 0, now,
        )
        store.create_action(
            "empty", intent.version, "确认验收完成", "当前可信验证已通过",
            "project_activity_observed", "user", "ready", "policy",
            self.snapshots()["empty"].evidence_cursor, now,
        )
        self.assertFalse(self.snapshots()["empty"].can_confirm_achievement)

        current = self.snapshots()["empty"].current_action
        store.cancel_action(current.action_id, now, "manual_cancellation")
        store.create_action(
            "empty", intent.version, "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "ready", "policy",
            self.snapshots()["empty"].evidence_cursor, now,
        )

        self.assertTrue(self.snapshots()["empty"].can_confirm_achievement)

    def test_overview_exposes_one_safe_open_decision(self):
        now = datetime(2026, 8, 13, 9, 5, tzinfo=timezone.utc)
        store = ProjectWorkStore(self.store.db_path)
        intent = store.save_intent(
            "empty", "Choose a safe release path", ["User approves one path"], [], 0, now,
        )
        store.create_decision_request(
            "empty", intent.version, "是否允许生产写入", "这会扩大当前权限边界",
            ["保持只读", "明确授权"], "open", None, now,
        )

        project = self.snapshots()["empty"]

        self.assertEqual(project.intent_status, "active")
        self.assertEqual(project.open_decision.question, "是否允许生产写入")
        self.assertEqual(project.open_decision.options, ("保持只读", "明确授权"))

    def test_overview_derives_achievement_from_audited_user_confirmation(self):
        now = datetime(2026, 8, 13, 9, 5, tzinfo=timezone.utc)
        store = ProjectWorkStore(self.store.db_path)
        intent = store.save_intent(
            "empty", "Deliver a verified loop", ["User accepts the result"], [], 0, now,
        )
        action = store.create_action(
            "empty", intent.version, "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "ready", "policy",
            self.snapshots()["empty"].evidence_cursor, now,
        )
        store.confirm_current_action_from_decision("empty", action.action_id, intent.version, now)

        project = self.snapshots()["empty"]

        self.assertEqual(project.intent_status, "achieved")
        self.assertIsNone(project.current_action)

    def test_overview_goal_fields_hide_legacy_private_text(self):
        now = datetime(2026, 8, 13, 9, 5, tzinfo=timezone.utc)
        ProjectWorkStore(self.store.db_path).save_intent(
            "empty", "Deliver a verified loop", ["Trusted result"], [], 0, now,
        )
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE project_intents SET outcome='PRIVATE_PAYLOAD' "
                    "WHERE project_id='empty' AND status='active'",
                )

        rendered = json.dumps(self.snapshots()["empty"].to_public_dict(), ensure_ascii=False)

        self.assertNotIn("PRIVATE_PAYLOAD", rendered)
        self.assertIn("受保护的历史摘要", rendered)

    def test_missing_database_keeps_registered_projects_visible(self):
        (self.root / "data/factory.sqlite").unlink()
        snapshots = self.snapshots()
        self.assertEqual(set(snapshots), {"working", "incomplete", "interrupted", "stopped", "empty"})
        self.assertTrue(all(item.source == "policy" for item in snapshots.values()))

    def test_database_read_failure_is_unknown_not_missing_intent(self):
        with patch(
            "observer.console_model._read_project_facts",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            snapshots = self.snapshots()

        self.assertTrue(all(item.intent_status == "unknown" for item in snapshots.values()))

    def test_agent_copy_is_bounded_for_the_primary_surface(self):
        cursor = self.snapshots()["empty"].evidence_cursor
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO agent_runs(run_id,idempotency_key,project_id,status,judgement,reason,next_action,"
                    "requires_user,evidence_event_at,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    ("agent-run", "agent-copy", "empty", "completed", "判" * 180, "因" * 600,
                     "步" * 400, 0, cursor, "2026-08-13T10:00:00+00:00", "2026-08-13T10:01:00+00:00"),
                )
        snapshot = self.snapshots()["empty"]
        self.assertLessEqual(len(snapshot.judgement), 100)
        self.assertLessEqual(len(snapshot.reason), 300)
        self.assertLessEqual(len(snapshot.next_action), 180)

    def test_public_snapshot_explains_activity_and_latest_agent_run(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO agent_runs(run_id,idempotency_key,project_id,status,thread_id,judgement,reason,"
                    "next_action,requires_user,error_code,evidence_event_at,created_at,started_at,completed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("completed-run", "completed-key", "stopped", "completed", "PRIVATE_THREAD",
                     "PRIVATE_JUDGEMENT", "PRIVATE_REASON", "PRIVATE_ACTION", 0, None, "cursor-1",
                     "2026-08-13T09:01:00+00:00", "2026-08-13T09:02:00+00:00",
                     "2026-08-13T09:03:00+00:00"),
                )
                connection.execute(
                    "INSERT INTO agent_runs(run_id,idempotency_key,project_id,status,error_code,created_at,completed_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("failed-run", "failed-key", "empty", "failed", "agent_timeout",
                     "2026-08-13T09:04:00+00:00", "2026-08-13T09:05:00+00:00"),
                )
        snapshots = {
            item.project_id: item for item in build_overview(
                self.root, now=datetime(2026, 8, 13, 9, 15, tzinfo=timezone.utc),
            )
        }
        stopped = snapshots["stopped"].to_public_dict()
        empty = snapshots["empty"].to_public_dict()
        self.assertEqual(stopped["activity_status"], "recent")
        self.assertEqual(stopped["agent_activity"]["status"], "completed")
        self.assertEqual(stopped["agent_activity"]["completed_at"], "2026-08-13T09:03:00+00:00")
        self.assertEqual(empty["activity_status"], "none")
        self.assertEqual(empty["agent_activity"]["status"], "failed")
        self.assertEqual(empty["agent_activity"]["error_code"], "agent_timeout")
        rendered = json.dumps((stopped, empty), ensure_ascii=False)
        self.assertNotIn("PRIVATE_THREAD", rendered)
        self.assertNotIn("PRIVATE_JUDGEMENT", rendered)
        self.assertNotIn("PRIVATE_REASON", rendered)

    def test_new_observer_fact_invalidates_older_agent_judgement(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO agent_runs(run_id,idempotency_key,project_id,status,judgement,reason,next_action,"
                    "requires_user,evidence_event_at,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    ("old-run", "old-agent", "working", "completed", "OLD_AGENT", "old", "old", 0,
                     "2026-08-13T08:00:00+00:00", "2026-08-13T08:01:00+00:00",
                     "2026-08-13T08:02:00+00:00"),
                )
        snapshot = self.snapshots()["working"]
        self.assertEqual(snapshot.source, "policy")
        self.assertNotIn("OLD_AGENT", snapshot.judgement)

    def test_latest_verification_replaces_older_failure(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO events(event_id,deduplication_key,project_id,thread_id,turn_id,event_type,"
                    "occurred_at,payload_version,redacted_payload) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("verification-old", "verification-old", "stopped", "thread-stopped", "turn-stopped",
                     "post_tool", "2026-08-13T08:30:00+00:00", 1, "{}"),
                )
                connection.execute(
                    "INSERT INTO verification_records VALUES(?,?,?,?,?,?,?,?,?)",
                    ("verify-old", "verification-old", "thread-stopped", "turn-stopped", "test", "test",
                     1, "failed", "2026-08-13T08:30:00+00:00"),
                )
                connection.execute(
                    "INSERT INTO events(event_id,deduplication_key,project_id,thread_id,turn_id,event_type,"
                    "occurred_at,payload_version,redacted_payload) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("verification-new", "verification-new", "stopped", "thread-stopped", "turn-stopped",
                     "post_tool", "2026-08-13T08:45:00+00:00", 1, "{}"),
                )
                connection.execute(
                    "INSERT INTO verification_records VALUES(?,?,?,?,?,?,?,?,?)",
                    ("verify-new", "verification-new", "thread-stopped", "turn-stopped", "test", "test",
                     0, "passed", "2026-08-13T08:45:00+00:00"),
                )
        snapshot = self.snapshots()["stopped"]
        verifier = next(item.summary for item in snapshot.evidence if item.source == "Verifier")
        self.assertIn("通过", verifier)
        self.assertNotIn("失败", verifier)
        self.assertNotIn("失败", snapshot.judgement)

    def test_project_verification_receipt_is_current_until_new_code_change(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO events(event_id,deduplication_key,project_id,thread_id,turn_id,event_type,"
                    "occurred_at,payload_version,redacted_payload) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("receipt-current", "receipt-current", "stopped", "thread-stopped", None,
                     "verification_receipt", "2026-08-13T09:10:00+00:00", 1, "{}"),
                )
                connection.execute(
                    "INSERT INTO verification_records VALUES(?,?,?,?,?,?,?,?,?)",
                    ("receipt-record", "receipt-current", "thread-stopped", None, "test",
                     "python_unittest", 0, "passed", "2026-08-13T09:10:00+00:00"),
                )
        current = self.snapshots()["stopped"]
        verifier = next(item.summary for item in current.evidence if item.source == "Verifier")
        self.assertIn("当前通过", verifier)

        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO events(event_id,deduplication_key,project_id,thread_id,turn_id,event_type,"
                    "occurred_at,payload_version,redacted_payload) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("code-after-receipt", "code-after-receipt", "stopped", "thread-stopped",
                     "turn-stopped", "code_change_observed", "2026-08-13T09:12:00+00:00", 1, "{}"),
                )
        stale = self.snapshots()["stopped"]
        verifier = next(item.summary for item in stale.evidence if item.source == "Verifier")
        self.assertIn("已过期", verifier)
        self.assertIn("验证", stale.judgement)

    def test_any_working_thread_blocks_agent_even_when_latest_thread_stopped(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO threads(thread_id,project_id,first_seen_at,last_seen_at,status) "
                    "VALUES(?,?,?,?,?)",
                    ("thread-stopped-newer", "working", "2026-08-13T09:30:00+00:00",
                     "2026-08-13T10:00:00+00:00", "stopped"),
                )
        snapshot = self.snapshots()["working"]
        self.assertFalse(snapshot.can_run_agent)
        self.assertIn("正在", snapshot.judgement)

    def test_stale_working_thread_becomes_recovery_unknown_in_console(self):
        snapshot = {
            item.project_id: item for item in build_overview(
                self.root, now=datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
            )
        }["working"]

        self.assertTrue(snapshot.can_run_agent)
        self.assertNotIn("正在工作", snapshot.judgement)
        self.assertIn("无法确定", snapshot.judgement)

    def test_any_interrupted_thread_requires_recovery_when_latest_stopped(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO threads(thread_id,project_id,first_seen_at,last_seen_at,status) "
                    "VALUES(?,?,?,?,?)",
                    ("thread-interrupted-older", "stopped", "2026-08-13T07:00:00+00:00",
                     "2026-08-13T08:00:00+00:00", "interrupted"),
                )
        snapshot = self.snapshots()["stopped"]
        self.assertIn("恢复", snapshot.next_action)
        self.assertNotIn("收口", snapshot.judgement)

    def test_same_timestamp_new_event_invalidates_agent_result(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO agent_runs(run_id,idempotency_key,project_id,status,judgement,reason,next_action,"
                    "requires_user,evidence_event_at,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    ("cursor-run", "cursor-agent", "stopped", "completed", "STALE_AGENT", "old", "old", 0,
                     "2026-08-13T09:00:00+00:00|1", "2026-08-13T09:01:00+00:00",
                     "2026-08-13T09:02:00+00:00"),
                )
                connection.execute(
                    "INSERT INTO events(event_id,deduplication_key,project_id,thread_id,turn_id,event_type,"
                    "occurred_at,payload_version,redacted_payload) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("same-time-event", "same-time-event", "stopped", "thread-stopped", "turn-stopped",
                     "post_tool", "2026-08-13T09:00:00+00:00", 1, "{}"),
                )
        snapshot = self.snapshots()["stopped"]
        self.assertEqual(snapshot.source, "policy")
        self.assertNotIn("STALE_AGENT", snapshot.judgement)

    def test_dirty_snapshot_without_current_pass_requires_verification(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO git_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    ("dirty-snapshot", "event-stopped", "/tmp/stopped", "/tmp/stopped/.git", "main",
                     "abc", 1, '["changed.py"]', 1, None, "2026-08-13T09:00:00+00:00"),
                )
        snapshot = self.snapshots()["stopped"]
        self.assertIn("验证", snapshot.judgement)
        self.assertNotIn("收口", snapshot.judgement)
        self.assertIn("未收口变更", snapshot.evidence[1].summary)

    def test_reconcile_status_change_invalidates_agent_result_without_new_event(self):
        cursor = self.snapshots()["stopped"].evidence_cursor
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO agent_runs(run_id,idempotency_key,project_id,status,judgement,reason,next_action,"
                    "requires_user,evidence_event_at,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    ("reconcile-run", "reconcile-agent", "stopped", "completed", "OLD_AGENT", "old", "old", 0,
                     cursor, "2026-08-13T10:00:00+00:00", "2026-08-13T10:01:00+00:00"),
                )
                connection.execute("UPDATE threads SET status='working' WHERE thread_id='thread-stopped'")
                connection.execute("UPDATE turns SET status='working' WHERE thread_id='thread-stopped'")
        snapshot = self.snapshots()["stopped"]
        self.assertEqual(snapshot.source, "policy")
        self.assertNotIn("OLD_AGENT", snapshot.judgement)


if __name__ == "__main__":
    unittest.main()
