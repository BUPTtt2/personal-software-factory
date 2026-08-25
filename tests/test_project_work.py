import hashlib
import inspect
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from observer.console_model import ProjectSnapshot, WorkSummary, current_evidence_cursor
from observer.event_store import EventRecord, EventStore, VerificationRecord
from observer.project_work import (
    CurrentActionConflict,
    IntentVersionConflict,
    ProjectWorkEvaluator,
    ProjectWorkStore,
)


class ProjectWorkStoreTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.events = EventStore(root / "factory.sqlite", root / "buffer")
        self.events.initialize()
        self.store = ProjectWorkStore(root / "factory.sqlite")
        self.now = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
        with self.store.connect() as connection:
            self.cursor = current_evidence_cursor(connection, "demo")
        self.next_cursor = hashlib.sha256(b"evidence-2").hexdigest()
        self.intent = self.store.save_intent(
            "demo", "Ship a verified loop", ["Tests pass"], ["Read only"], 0, self.now,
        )

    def receipt(
        self, event_id: str = "receipt-1", *, kind: str = "test", status: str = "passed",
        project_id: str = "demo",
    ) -> str:
        self.events.append_event(EventRecord(
            event_id=event_id, deduplication_key=event_id, project_id=project_id, thread_id="thread-1",
            turn_id=None, event_type="verification_receipt", occurred_at=self.now.replace(second=1), payload_version=1,
            redacted_payload={}, verification=VerificationRecord(kind, "python_unittest", 0, status),
        ))
        return event_id

    def test_save_intent_requires_current_version(self):
        first = self.store.save_intent(
            "second", "Ship a verified loop", ["Tests pass"], [], 0, self.now,
        )
        with self.assertRaises(IntentVersionConflict):
            self.store.save_intent(
                "second", "Overwrite", ["Looks done"], [], 0, self.now,
            )
        self.assertEqual(first.version, 1)

    def test_save_intent_preserves_immutable_history_with_one_active_row(self):
        first = self.intent
        second = self.store.save_intent(
            "demo", "Ship a browser-verified loop", ["Browser passes"], ["Read only"],
            first.version, self.now,
        )

        self.assertEqual(first.acceptance_criteria, ("Tests pass",))
        self.assertEqual(second.version, 2)
        self.assertEqual(second.status, "active")
        self.assertEqual(self.store.get_intent("demo"), second)
        with self.store.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM project_intents WHERE project_id='demo'").fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM project_intents WHERE project_id='demo' AND version=1"
                ).fetchone()[0],
                "superseded",
            )
        action = self.store.create_action(
            "demo", second.version, "Verify", "Current proof", "trusted_verification_current",
            "external", "ready", "policy", self.cursor, self.now,
        )
        self.assertEqual(action.intent_version, second.version)

    def test_database_allows_only_one_current_action(self):
        self.store.create_action(
            "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current", "codex",
            "ready", "policy", self.cursor, self.now,
        )
        with self.assertRaises(CurrentActionConflict):
            self.store.create_action(
                "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current", "codex",
                "ready", "policy", self.cursor, self.now,
            )

    def test_action_cannot_be_created_as_verified(self):
        with self.assertRaises(ValueError):
            self.store.create_action(
                "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current",
                "external", "verified", "policy", self.cursor, self.now,
            )

    def test_resolving_current_action_allows_next_action(self):
        first = self.store.create_action(
            "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current", "external",
            "in_progress", "policy", self.cursor, self.now,
        )
        resolved = self.store.resolve_action(
            first.action_id, self.receipt(), self.now,
        )
        second = self.store.create_action(
            "demo", self.intent.version, "Review", "Proof is current", "trusted_verification_current", "user",
            "ready", "user", self.next_cursor, self.now,
        )

        self.assertEqual(resolved.status, "verified")
        self.assertEqual(self.store.get_action(first.action_id), resolved)
        self.assertEqual(self.store.list_actions("demo"), [second, resolved])

    def test_validators_reject_blank_and_unknown_values(self):
        with self.assertRaises(ValueError):
            self.store.save_intent(" ", "Outcome", ["Criterion"], [], 0, self.now)
        with self.assertRaises(ValueError):
            self.store.create_action(
                "demo", 1, "Verify", "Missing proof", "unknown", "codex", "ready",
                "policy", self.cursor, self.now,
            )

    def test_actions_and_decisions_must_reference_the_current_intent(self):
        with self.assertRaises(IntentVersionConflict):
            self.store.create_action(
                "demo", self.intent.version + 1, "Verify", "Missing proof",
                "trusted_verification_current", "codex", "ready", "policy", self.cursor, self.now,
            )
        with self.assertRaises(IntentVersionConflict):
            self.store.create_decision_request(
                "demo", self.intent.version + 1, "Choose", "Reason", ["One", "Two"],
                "open", None, self.now,
            )
        with self.assertRaises(IntentVersionConflict):
            self.store.create_action(
                "missing", 1, "Verify", "Missing proof", "trusted_verification_current",
                "external", "ready", "policy", self.cursor, self.now,
            )

    def test_codex_and_supervisor_actions_cannot_be_directly_verified(self):
        codex_action = self.store.create_action(
            "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current",
            "codex", "in_progress", "policy", self.cursor, self.now,
        )
        with self.assertRaises(PermissionError):
            self.store.resolve_action(
                codex_action.action_id, self.receipt(), self.now,
            )
        self.store.cancel_action(codex_action.action_id, self.now)
        with self.assertRaises(PermissionError):
            self.store.create_action(
                "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current",
                "external", "in_progress", "supervisor", self.cursor, self.now,
            )

    def test_user_action_requires_explicit_confirmation_to_be_verified(self):
        self.assertTrue(hasattr(self.store, "resolve_user_decision"))
        action = self.store.create_action(
            "demo", self.intent.version, "Accept", "User must accept", "user_confirms_acceptance",
            "user", "in_progress", "user", self.cursor, self.now,
        )
        decision = self.store.create_decision_request(
            "demo", self.intent.version, "Accept this action?", "Explicit acceptance is required",
            ["Accept", "Reject"], "open", None, self.now,
        )
        self.store.resolve_user_decision(decision.decision_id, "Accept", self.now)
        resolved = self.store.confirm_action(action.action_id, decision.decision_id, self.now)
        self.assertEqual(resolved.status, "verified")
        with self.store.connect() as connection:
            self.assertEqual(
                tuple(connection.execute(
                    "SELECT resolution_kind,actor,intent_version,decision_id FROM action_resolutions "
                    "WHERE action_id=?", (action.action_id,),
                ).fetchone()),
                ("user_confirmation", "user", self.intent.version, decision.decision_id),
            )

    def test_current_user_confirmation_writes_decision_and_action_together(self):
        action = self.store.create_action(
            "demo", self.intent.version, "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "ready", "policy", self.cursor, self.now,
        )

        intent, confirmed = self.store.confirm_current_action_from_decision(
            "demo", action.action_id, self.intent.version, self.now,
        )

        self.assertEqual(intent.version, self.intent.version)
        self.assertEqual(confirmed.status, "verified")
        decisions = self.store.list_decision_requests("demo")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].status, "resolved")
        self.assertEqual(decisions[0].resolution, action.title)
        with self.store.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT decision_id FROM action_resolutions WHERE action_id=?", (action.action_id,),
                ).fetchone()[0],
                decisions[0].decision_id,
            )

    def test_stale_confirmation_leaves_no_orphan_decision(self):
        action = self.store.create_action(
            "demo", self.intent.version, "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "ready", "policy", self.cursor, self.now,
        )

        with self.assertRaises(IntentVersionConflict):
            self.store.confirm_current_action_from_decision(
                "demo", action.action_id, self.intent.version + 1, self.now,
            )

        self.assertEqual(self.store.list_decision_requests("demo"), [])
        self.assertEqual(self.store.get_action(action.action_id).status, "ready")

    def test_user_confirmation_requires_matching_resolved_decision(self):
        self.assertTrue(hasattr(self.store, "resolve_user_decision"))
        action = self.store.create_action(
            "demo", self.intent.version, "Accept", "User must accept", "user_confirms_acceptance",
            "user", "in_progress", "user", self.cursor, self.now,
        )
        decision = self.store.create_decision_request(
            "demo", self.intent.version, "Accept this action?", "Explicit acceptance is required",
            ["Accept", "Reject"], "open", None, self.now,
        )
        with self.assertRaises(ValueError):
            self.store.confirm_action(action.action_id, decision.decision_id, self.now)
        self.store.resolve_user_decision(decision.decision_id, "Reject", self.now)
        with self.assertRaises(ValueError):
            self.store.confirm_action(action.action_id, decision.decision_id, self.now)
        self.assertEqual(self.store.get_action(action.action_id).status, "in_progress")
        with self.store.connect() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM action_resolutions WHERE action_id=?", (action.action_id,),
            ).fetchone())

    def test_user_confirmation_requires_decision_from_the_same_project(self):
        action = self.store.create_action(
            "demo", self.intent.version, "Accept", "User must accept", "user_confirms_acceptance",
            "user", "in_progress", "user", self.cursor, self.now,
        )
        other_intent = self.store.save_intent(
            "other", "Ship another loop", ["Accepted"], [], 0, self.now,
        )
        decision = self.store.create_decision_request(
            "other", other_intent.version, "Accept this action?", "Explicit acceptance is required",
            ["Accept", "Reject"], "open", None, self.now,
        )
        self.store.resolve_user_decision(decision.decision_id, "Accept", self.now)

        with self.assertRaises(ValueError):
            self.store.confirm_action(action.action_id, decision.decision_id, self.now)
        self.assertEqual(self.store.get_action(action.action_id).status, "in_progress")

    def test_user_decision_resolution_must_be_an_option(self):
        self.assertTrue(hasattr(self.store, "resolve_user_decision"))
        decision = self.store.create_decision_request(
            "demo", self.intent.version, "Accept this action?", "Explicit acceptance is required",
            ["Accept", "Reject"], "open", None, self.now,
        )
        with self.assertRaises(ValueError):
            self.store.resolve_user_decision(decision.decision_id, "Other", self.now)
        self.assertEqual(self.store.get_decision_request(decision.decision_id).status, "open")

    def test_legacy_decision_resolution_name_uses_the_user_decision_contract(self):
        decision = self.store.create_decision_request(
            "demo", self.intent.version, "Accept this action?", "Explicit acceptance is required",
            ["Accept", "Reject"], "open", None, self.now,
        )

        with self.assertRaises(ValueError):
            self.store.resolve_decision_request(decision.decision_id, "Other", self.now)
        resolved = self.store.resolve_decision_request(decision.decision_id, "Accept", self.now)

        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.resolution, "Accept")

    def test_creation_interfaces_have_no_raw_codex_content_channel(self):
        raw_content = {"transcript": "hidden", "prompt": "hidden", "tool_output": "hidden"}

        with self.assertRaises(ValueError):
            self.store.save_intent(
                "demo", raw_content, ["Tests pass"], [], self.intent.version, self.now,
            )
        with self.assertRaises(ValueError):
            self.store.create_action(
                "demo", self.intent.version, raw_content, "Missing proof", "trusted_verification_current",
                "external", "blocked", "policy", self.cursor, self.now,
            )
        with self.assertRaises(ValueError):
            self.store.create_action(
                "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current",
                "external", "blocked", raw_content, self.cursor, self.now,
            )
        with self.assertRaises(ValueError):
            self.store.create_decision_request(
                "demo", self.intent.version, raw_content, "Reason", ["One", "Two"],
                "open", None, self.now,
            )
        with self.assertRaises(TypeError):
            self.store.save_intent(
                "demo", "Outcome", ["Tests pass"], [], self.intent.version, self.now,
                raw_content=raw_content,
            )
        with self.assertRaises(TypeError):
            self.store.create_action(
                "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current",
                "external", "blocked", "policy", self.cursor, self.now,
                raw_content=raw_content,
            )
        with self.assertRaises(TypeError):
            self.store.create_decision_request(
                "demo", self.intent.version, "Choose", "Reason", ["One", "Two"],
                "open", None, self.now, raw_content=raw_content,
            )

        self.assertEqual(self.intent.provenance, "local_user")
        policy_action = self.store.create_action(
            "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current",
            "codex", "blocked", "policy", self.cursor, self.now,
        )
        self.assertEqual(policy_action.provenance, "policy")
        with self.assertRaises(ValueError):
            self.store.create_action(
                "demo", self.intent.version, "Arbitrary", "Generated text", "trusted_verification_current",
                "codex", "blocked", "policy", self.next_cursor, self.now,
            )
        with self.assertRaises(PermissionError):
            self.store.create_action(
                "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current",
                "external", "blocked", "supervisor", self.next_cursor, self.now,
            )

    def test_write_interfaces_derive_and_persist_provenance(self):
        self.assertEqual(
            tuple(inspect.signature(self.store.save_intent).parameters),
            ("project_id", "outcome", "acceptance_criteria", "constraints", "expected_version", "updated_at"),
        )
        self.assertEqual(
            tuple(inspect.signature(self.store.create_action).parameters),
            ("project_id", "intent_version", "title", "why_now", "completion_evidence", "actor", "status",
             "source", "evidence_cursor", "created_at"),
        )
        self.assertEqual(
            tuple(inspect.signature(self.store.create_decision_request).parameters),
            ("project_id", "intent_version", "question", "reason", "options", "status", "resolution", "created_at"),
        )
        self.assertEqual(
            tuple(inspect.signature(self.store.confirm_action).parameters),
            ("action_id", "decision_id", "confirmed_at"),
        )

        self.assertEqual(self.intent.provenance, "local_user")
        policy_action = self.store.create_action(
            "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current",
            "codex", "blocked", "policy", self.cursor, self.now,
        )
        user_action = self.store.create_action(
            "demo", self.intent.version, "Accept", "User must accept", "user_confirms_acceptance",
            "user", "blocked", "user", self.next_cursor, self.now,
        )
        decision = self.store.create_decision_request(
            "demo", self.intent.version, "Choose", "Reason", ["One", "Two"], "open", None, self.now,
        )
        self.assertEqual(policy_action.provenance, "policy")
        self.assertEqual(user_action.provenance, "local_user")
        self.assertEqual(decision.provenance, "local_user")
        with self.store.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT provenance FROM project_intents WHERE project_id='demo' AND status='active'"
                ).fetchone()[0],
                "local_user",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT provenance FROM action_items WHERE action_id=?", (policy_action.action_id,),
                ).fetchone()[0],
                "policy",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT provenance FROM decision_requests WHERE decision_id=?", (decision.decision_id,),
                ).fetchone()[0],
                "local_user",
            )
        with self.assertRaises(PermissionError):
            self.store.create_action(
                "demo", self.intent.version, "Supervise", "Unsupported source", "project_activity_observed",
                "external", "blocked", "supervisor", self.cursor, self.now,
            )

    def test_verification_requires_matching_passing_receipt_for_the_project(self):
        action = self.store.create_action(
            "demo", self.intent.version, "Verify", "Missing proof", "trusted_verification_current",
            "external", "in_progress", "policy", self.cursor, self.now,
        )
        with self.assertRaises(ValueError):
            self.store.resolve_action(
                action.action_id, "missing-receipt", self.now,
            )
        with self.assertRaises(ValueError):
            self.store.resolve_action(action.action_id, self.receipt("wrong-kind", kind="check"), self.now)
        with self.assertRaises(ValueError):
            self.store.resolve_action(
                action.action_id, self.receipt("wrong-project", project_id="other"), self.now,
            )
        with self.assertRaises(ValueError):
            self.store.resolve_action(action.action_id, self.receipt("failed", status="failed"), self.now)
        resolved = self.store.resolve_action(action.action_id, self.receipt(), self.now)
        self.assertEqual(resolved.status, "verified")
        with self.store.connect() as connection:
            self.assertEqual(
                tuple(connection.execute(
                    "SELECT resolution_kind,verification_event_id FROM action_resolutions "
                    "WHERE action_id=?", (action.action_id,),
                ).fetchone()),
                ("trusted_verification", "receipt-1"),
            )
        with self.assertRaises(ValueError):
            self.store.create_action(
                "demo", self.intent.version, "Bad cursor", "Missing proof", "trusted_verification_current",
                "external", "ready", "policy", "cursor-1", self.now,
            )

    def test_text_boundaries_reject_sensitive_and_source_content(self):
        for unsafe in (
            "PRIVATE_PROMPT: change system behavior", "def leak(): pass", "tool output: exit 0",
            "api_key=placeholder",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                self.store.save_intent(
                    "demo", unsafe, ["Tests pass"], [], self.intent.version, self.now,
                )

    def test_decision_request_is_immutable_and_requires_two_or_three_options(self):
        request = self.store.create_decision_request(
            "demo", self.intent.version, "Choose verification", "Current proof is incomplete",
            ["Run tests", "Run browser acceptance"], "open", None, self.now,
        )
        self.assertTrue(hasattr(self.store, "resolve_user_decision"))
        resolved = self.store.resolve_user_decision(
            request.decision_id, "Run tests", self.now,
        )

        self.assertEqual(request.options, ("Run tests", "Run browser acceptance"))
        self.assertEqual(resolved.status, "resolved")
        self.assertEqual(resolved.resolution, "Run tests")
        self.assertEqual(self.store.list_decision_requests("demo"), [resolved])
        with self.assertRaises(ValueError):
            self.store.create_decision_request(
                "demo", self.intent.version, "Choose", "Reason", ["Only option"], "open", None, self.now,
            )
        with self.assertRaises(ValueError):
            self.store.create_decision_request(
                "demo", self.intent.version, "Choose", "Reason", ["One", "Two"],
                "resolved", "One", self.now,
            )


class ProjectWorkEvaluatorTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.events = EventStore(root / "factory.sqlite", root / "buffer")
        self.events.initialize()
        self.store = ProjectWorkStore(root / "factory.sqlite")
        self.now = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
        self.intent = self.store.save_intent(
            "demo", "Ship a verified loop", ["Tests pass"], ["Read only"], 0, self.now,
        )
        self.evaluator = ProjectWorkEvaluator(self.store)

    @staticmethod
    def cursor(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def snapshot(
        self,
        *,
        stage: str,
        verification: str = "unknown",
        freshness: str = "unknown",
        paths: tuple[str, ...] = (),
        active_threads: int = 0,
        interrupted_threads: int = 0,
        activity_status: str = "recent",
        cursor: str = "cursor-1",
    ) -> ProjectSnapshot:
        return ProjectSnapshot(
            project_id="demo", display_name="Demo", source="policy", judgement="safe",
            reason="safe", next_action="safe", can_run_agent=True, evidence=(), last_event_at=None,
            active_run_id=None,
            work_summary=WorkSummary(
                stage, active_threads, interrupted_threads, None, None, paths, verification, freshness, None,
            ),
            activity_status=activity_status, evidence_cursor=self.cursor(cursor),
        )

    def receipt(self, event_id: str = "receipt-1") -> None:
        self.events.append_event(EventRecord(
            event_id=event_id, deduplication_key=event_id, project_id="demo", thread_id="receipt-thread",
            turn_id=None, event_type="verification_receipt", occurred_at=self.now.replace(second=1),
            payload_version=1, redacted_payload={},
            verification=VerificationRecord("test", "python_unittest", 0, "passed"),
        ))

    def seed_verification_action(self, cursor: str):
        return self.store.create_action(
            "demo", self.intent.version, "补齐可信验证", "变更尚无当前可信验证",
            "trusted_verification_current", "external", "ready", "policy", self.cursor(cursor), self.now,
        )

    def test_working_project_waits_for_the_current_thread_to_close(self):
        action = self.evaluator.refresh(
            "demo", self.snapshot(stage="工作中", active_threads=1), self.now,
        )

        self.assertEqual(action.title, "等待当前工作收口")
        self.assertEqual(action.completion_evidence, "project_activity_observed")

    def test_failed_verification_requests_remediation(self):
        action = self.evaluator.refresh(
            "demo", self.snapshot(stage="验证失败", verification="failed", freshness="current"), self.now,
        )

        self.assertEqual(action.title, "处理验证失败")
        self.assertEqual(action.actor, "codex")

    def test_dirty_project_without_current_verification_gets_one_verification_action(self):
        action = self.evaluator.refresh(
            "demo", self.snapshot(stage="等待验证", verification="unknown", paths=("app.py",)), self.now,
        )

        self.assertEqual(action.title, "补齐可信验证")
        self.assertEqual(action.completion_evidence, "trusted_verification_current")

    def test_interrupted_or_unknown_project_requests_recovery_check(self):
        action = self.evaluator.refresh(
            "demo", self.snapshot(stage="需要恢复核验", interrupted_threads=1), self.now,
        )

        self.assertEqual(action.title, "恢复并核验状态")
        self.assertEqual(action.completion_evidence, "recovered_state_verified")

    def test_no_activity_does_not_invent_an_action(self):
        action = self.evaluator.refresh(
            "demo", self.snapshot(stage="尚无活动", activity_status="none"), self.now,
        )

        self.assertIsNone(action)
        self.assertEqual(self.store.list_actions("demo"), [])

    def test_no_activity_does_not_cancel_an_existing_current_action(self):
        current = self.store.create_action(
            "demo", self.intent.version, "Verify", "Missing proof",
            "trusted_verification_current", "codex", "ready", "policy",
            "0" * 64, self.now,
        )

        refreshed = self.evaluator.refresh(
            "demo", self.snapshot(stage="尚无活动", activity_status="none"), self.now,
        )

        self.assertEqual(refreshed.action_id, current.action_id)
        self.assertEqual(self.store.get_action(current.action_id).status, "ready")

    def test_current_passing_verification_verifies_previous_action_and_requests_user_confirmation(self):
        self.seed_verification_action("cursor-1")
        self.receipt()

        action = self.evaluator.refresh(
            "demo", self.snapshot(stage="已收口", verification="passed", freshness="current", cursor="cursor-2"),
            self.now,
        )

        self.assertEqual(self.store.list_actions("demo")[1].status, "verified")
        self.assertEqual(action.actor, "user")
        self.assertEqual(action.completion_evidence, "user_confirms_acceptance")

    def test_unchanged_evidence_cursor_keeps_the_current_action(self):
        first = self.evaluator.refresh(
            "demo", self.snapshot(stage="等待验证", paths=("app.py",)), self.now,
        )
        repeated = self.evaluator.refresh(
            "demo", self.snapshot(stage="等待验证", paths=("app.py",)), self.now,
        )

        self.assertEqual(repeated.action_id, first.action_id)
        self.assertEqual(len(self.store.list_actions("demo")), 1)

    def test_new_intent_version_replaces_semantically_matching_old_action(self):
        old_action = self.evaluator.refresh(
            "demo", self.snapshot(stage="等待验证", paths=("app.py",)), self.now,
        )
        new_intent = self.store.save_intent(
            "demo", "Ship version two", ["Tests still pass"], ["Read only"],
            self.intent.version, self.now.replace(second=1),
        )

        new_action = self.evaluator.refresh(
            "demo", self.snapshot(stage="等待验证", paths=("app.py",)),
            self.now.replace(second=2),
        )

        self.assertEqual(self.store.get_action(old_action.action_id).status, "cancelled")
        self.assertEqual(new_action.intent_version, new_intent.version)
        self.assertNotEqual(new_action.action_id, old_action.action_id)

    def test_advanced_evidence_cancels_an_obsolete_action_with_a_stable_safety_code(self):
        working = self.evaluator.refresh(
            "demo", self.snapshot(stage="工作中", active_threads=1, cursor="cursor-1"), self.now,
        )

        action = self.evaluator.refresh(
            "demo", self.snapshot(stage="验证失败", verification="failed", freshness="current", cursor="cursor-2"),
            self.now,
        )

        self.assertEqual(action.title, "处理验证失败")
        self.assertEqual(self.store.get_action(working.action_id).status, "cancelled")
        with self.store.connect() as connection:
            self.assertEqual(
                tuple(connection.execute(
                    "SELECT authority,reason FROM action_resolutions WHERE action_id=?", (working.action_id,),
                ).fetchone()),
                ("safety_code", "evidence_advanced"),
            )

    def test_generic_pass_requires_user_review_of_acceptance_criteria(self):
        self.seed_verification_action("cursor-1")
        self.receipt()

        action = self.evaluator.refresh(
            "demo", self.snapshot(
                stage="已收口", verification="passed", freshness="current", cursor="cursor-2",
            ), self.now,
        )

        self.assertEqual(action.actor, "user")
        self.assertIn("验收标准需要你核对", action.why_now)

    def test_new_evidence_replaces_a_semantically_identical_confirmation_action(self):
        self.seed_verification_action("cursor-1")
        self.receipt()
        first = self.evaluator.refresh(
            "demo", self.snapshot(
                stage="已收口", verification="passed", freshness="current", cursor="cursor-2",
            ), self.now,
        )

        replacement = self.evaluator.refresh(
            "demo", self.snapshot(
                stage="已收口", verification="passed", freshness="current", cursor="cursor-3",
            ), self.now.replace(second=2),
        )

        self.assertNotEqual(replacement.action_id, first.action_id)
        self.assertEqual(replacement.evidence_cursor, self.cursor("cursor-3"))
        self.assertEqual(self.store.get_action(first.action_id).status, "cancelled")


if __name__ == "__main__":
    unittest.main()
