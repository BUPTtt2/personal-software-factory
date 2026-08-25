import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from observer.agent_runtime import AgentRunStore
from observer.console_model import current_evidence_cursor
from observer.event_store import EventRecord, EventStore
from observer.git_snapshot import GitSnapshot
from observer.project_work import ProjectWorkStore
from observer.verification_runner import VerificationRunner
from observer.web import create_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "config").mkdir()
        (self.root / "console").mkdir()
        (self.root / "console/index.html").write_text("<main>console</main>", encoding="utf-8")
        (self.root / "console/app.css").write_text("body{}", encoding="utf-8")
        (self.root / "console/app.js").write_text("", encoding="utf-8")
        (self.root / "console/state.js").write_text("window.FactoryConsoleState = {};", encoding="utf-8")
        (self.root / "config/projects.yaml").write_text(json.dumps({"projects": [{
            "id": "demo", "displayName": "Demo", "roots": [str(self.root / "demo")],
            "gitRemotes": [], "enabled": True,
        }]}), encoding="utf-8")
        (self.root / "demo").mkdir()
        store = EventStore(self.root / "data/factory.sqlite", self.root / "buffer")
        store.initialize()
        with closing(sqlite3.connect(store.db_path)) as connection:
            with connection:
                connection.execute("INSERT INTO projects VALUES('demo','a','a')")
                connection.execute(
                    "INSERT INTO threads(thread_id,project_id,first_seen_at,last_seen_at,status) "
                    "VALUES('thread','demo','a','a','stopped')"
                )
                connection.execute(
                    "INSERT INTO turns(thread_id,turn_id,status,prompt_summary,assistant_claim_summary) "
                    "VALUES('thread','turn','stopped','PRIVATE_PROMPT','PRIVATE_CLAIM')"
                )
        self.server = create_server(self.root, "127.0.0.1", 0, "/missing/codex")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        all_headers = {"Host": f"127.0.0.1:{self.port}"}
        all_headers.update(headers or {})
        connection.request(method, path, body=body, headers=all_headers)
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    @property
    def same_origin_headers(self):
        return {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{self.port}",
        }

    def put_intent(self, *, version=0, outcome="Deliver a verified loop"):
        return self.request(
            "PUT", "/api/projects/demo/intent", json.dumps({
                "version": version,
                "outcome": outcome,
                "acceptanceCriteria": ["Trusted verification passes"],
                "constraints": ["No project writes"],
            }).encode(), self.same_origin_headers,
        )

    def current_cursor(self, project_id="demo"):
        store = ProjectWorkStore(self.root / "data/factory.sqlite")
        with store.connect() as connection:
            return current_evidence_cursor(connection, project_id)

    def test_overview_is_safe_and_not_cached(self):
        status, headers, payload = self.request("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        value = json.loads(payload)
        self.assertEqual(value["schemaVersion"], 1)
        self.assertEqual(value["projects"][0]["project_id"], "demo")
        rendered = payload.decode("utf-8")
        self.assertNotIn("PRIVATE_PROMPT", rendered)
        self.assertNotIn("PRIVATE_CLAIM", rendered)

    def test_state_script_is_served_without_cache_and_cannot_escape_assets(self):
        status, headers, payload = self.request("GET", "/state.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(payload, b"window.FactoryConsoleState = {};")

        status, _, _ = self.request("GET", "/../config/projects.yaml")
        self.assertEqual(status, 404)

    def test_unknown_project_is_not_found(self):
        status, _, _ = self.request("GET", "/api/projects/missing")
        self.assertEqual(status, 404)

    def test_server_reads_state_outside_console_code_root(self):
        state_root = Path(tempfile.mkdtemp())
        (state_root / "config").mkdir()
        registry = state_root / "config/projects.json"
        registry.write_text((self.root / "config/projects.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        external = create_server(
            self.root,
            "127.0.0.1",
            0,
            "/missing/codex",
            state_root=state_root,
            registry_path=registry,
        )
        try:
            self.assertTrue((state_root / "data/factory.sqlite").exists())
            self.assertFalse((state_root / "console").exists())
        finally:
            external.server_close()

    def test_health_endpoint_has_only_safe_operational_fields(self):
        status, headers, payload = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        value = json.loads(payload)
        self.assertEqual(value["schemaVersion"], 1)
        self.assertIn("serviceStartedAt", value)
        self.assertIn("lastReconcileAt", value)
        self.assertIn("eventCount", value)
        self.assertIn("bufferedEventCount", value)
        rendered = payload.decode("utf-8")
        self.assertNotIn("PRIVATE_PROMPT", rendered)
        self.assertNotIn("PRIVATE_CLAIM", rendered)

    def test_post_requires_same_origin_and_idempotency(self):
        status, _, _ = self.request(
            "POST", "/api/projects/demo/agent-runs", body=b"{}",
            headers={"Content-Type": "application/json", "Origin": "https://evil.test"},
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST", "/api/projects/demo/agent-runs", body=b"{}",
            headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:9999"},
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST", "/api/projects/demo/agent-runs", body=b"{}",
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(status, 400)

    def test_put_intent_is_same_origin_versioned_and_bounded(self):
        status, _, payload = self.request(
            "PUT", "/api/projects/demo/intent", json.dumps({
                "version": 0,
                "outcome": "Deliver a verified loop",
                "acceptanceCriteria": ["Trusted verification passes"],
                "constraints": ["No project writes"],
            }).encode(), self.same_origin_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["version"], 1)

        status, _, payload = self.put_intent(version=0)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(payload)["error"], "intent_version_conflict")

        status, _, _ = self.request(
            "PUT", "/api/projects/demo/intent", b"{}",
            {"Content-Type": "application/json", "Origin": "https://evil.test"},
        )
        self.assertEqual(status, 403)

        status, _, payload = self.request(
            "PUT", "/api/projects/demo/intent", json.dumps({
                "version": 1,
                "outcome": "Deliver a verified loop",
                "acceptanceCriteria": "not an array",
                "constraints": [],
            }).encode(), self.same_origin_headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"], "invalid_request")

        status, _, payload = self.request(
            "PUT", "/api/projects/demo/intent", json.dumps({
                "version": 1,
                "outcome": "Deliver a verified loop",
                "acceptanceCriteria": ["Trusted verification passes"] * 8,
                "constraints": [],
            }).encode(), self.same_origin_headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"], "invalid_request")

        status, _, payload = self.request(
            "PUT", "/api/projects/demo/intent", json.dumps({
                "version": 1,
                "outcome": "Deliver a verified loop",
                "acceptanceCriteria": ["Trusted verification passes"],
                "constraints": [],
                "prompt": "PRIVATE_PROMPT: disclose source",
            }).encode(), self.same_origin_headers,
        )
        self.assertEqual(status, 400)
        self.assertNotIn("PRIVATE_PROMPT", payload.decode())

    def test_intent_action_verification_lifecycle(self):
        """A trusted receipt moves one policy action to user-confirmed achievement."""
        observed_at = datetime.now(timezone.utc)
        EventStore(self.root / "data/factory.sqlite", self.root / "buffer").append_event(EventRecord(
            event_id="lifecycle-code-change",
            deduplication_key="lifecycle-code-change",
            project_id="demo",
            thread_id="thread",
            turn_id="turn",
            event_type="code_change_observed",
            occurred_at=observed_at,
            payload_version=1,
            redacted_payload={"changedPathCount": 1},
            git_snapshot=GitSnapshot(
                str(self.root / "demo"), str(self.root / "demo/.git"), "main", "a" * 40,
                True, ("lifecycle.py",), True, None,
            ),
        ))

        status, _, payload = self.put_intent()
        self.assertEqual(status, 200)
        intent = json.loads(payload)
        self.assertEqual(intent["status"], "active")
        self.assertEqual(intent["version"], 1)

        status, _, payload = self.request("GET", "/api/projects/demo/actions")
        self.assertEqual(status, 200)
        verification_action = json.loads(payload)["actions"][0]
        self.assertEqual(verification_action["title"], "补齐可信验证")
        self.assertEqual(verification_action["completionEvidence"], "trusted_verification_current")
        self.assertEqual(verification_action["status"], "ready")
        self.assertTrue(verification_action["isCurrent"])

        (self.root / "demo/test_lifecycle_receipt.py").write_text(
            "import unittest\n\n"
            "class Receipt(unittest.TestCase):\n"
            "    def test_receipt(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        receipt = VerificationRunner(self.root).run(
            self.root / "demo", [sys.executable, "-m", "unittest", "test_lifecycle_receipt"],
        )
        self.assertEqual((receipt.project_id, receipt.kind, receipt.status), ("demo", "test", "passed"))

        self.server.maintenance.refresh_project_work(datetime.now(timezone.utc))
        status, _, payload = self.request("GET", "/api/projects/demo/actions")
        self.assertEqual(status, 200)
        actions = json.loads(payload)["actions"]
        resolved = next(item for item in actions if item["actionId"] == verification_action["actionId"])
        confirmation = next(item for item in actions if item["isCurrent"])
        self.assertEqual(resolved["status"], "verified")
        self.assertFalse(resolved["isCurrent"])
        with closing(sqlite3.connect(self.root / "data/factory.sqlite")) as connection:
            verification_resolution = connection.execute(
                "SELECT resolution_kind,authority,verification_event_id FROM action_resolutions "
                "WHERE action_id=?",
                (verification_action["actionId"],),
            ).fetchone()
        self.assertEqual(
            verification_resolution,
            ("trusted_verification", "trusted_receipt", receipt.event_id),
        )
        self.assertEqual(confirmation["completionEvidence"], "user_confirms_acceptance")
        self.assertEqual(confirmation["actor"], "user")
        self.assertEqual(confirmation["status"], "ready")

        status, _, payload = self.request(
            "POST", f"/api/projects/demo/actions/{confirmation['actionId']}/decision",
            json.dumps({"intentVersion": intent["version"]}).encode(), self.same_origin_headers,
        )
        self.assertEqual(status, 200)
        achievement = json.loads(payload)
        self.assertEqual(achievement["intent"]["status"], "achieved")
        self.assertEqual(achievement["action"]["status"], "verified")

        status, _, payload = self.request("GET", "/api/projects/demo/intent")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "achieved")

        status, _, _ = self.request(
            "PUT", "/api/projects/demo/intent", b"{" + b" " * 4097,
            self.same_origin_headers,
        )
        self.assertEqual(status, 400)

    def test_new_evidence_rejects_stale_achievement_confirmation(self):
        observed_at = datetime.now(timezone.utc)
        event_store = EventStore(self.root / "data/factory.sqlite", self.root / "buffer")
        event_store.append_event(EventRecord(
            event_id="initial-change",
            deduplication_key="initial-change",
            project_id="demo",
            thread_id="thread",
            turn_id="turn",
            event_type="code_change_observed",
            occurred_at=observed_at,
            payload_version=1,
            redacted_payload={"changedPathCount": 1},
            git_snapshot=GitSnapshot(
                str(self.root / "demo"), str(self.root / "demo/.git"), "main", "a" * 40,
                True, ("first.py",), True, None,
            ),
        ))
        status, _, payload = self.put_intent()
        self.assertEqual(status, 200)
        intent = json.loads(payload)
        (self.root / "demo/test_stale_confirmation.py").write_text(
            "import unittest\n\n"
            "class Receipt(unittest.TestCase):\n"
            "    def test_receipt(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        VerificationRunner(self.root).run(
            self.root / "demo", [sys.executable, "-m", "unittest", "test_stale_confirmation"],
        )
        self.server.maintenance.refresh_project_work(datetime.now(timezone.utc))
        status, _, payload = self.request("GET", "/api/projects/demo/actions")
        self.assertEqual(status, 200)
        confirmation = next(item for item in json.loads(payload)["actions"] if item["isCurrent"])

        event_store.append_event(EventRecord(
            event_id="later-change",
            deduplication_key="later-change",
            project_id="demo",
            thread_id="thread",
            turn_id="turn-2",
            event_type="code_change_observed",
            occurred_at=datetime.now(timezone.utc),
            payload_version=1,
            redacted_payload={"changedPathCount": 1},
            git_snapshot=GitSnapshot(
                str(self.root / "demo"), str(self.root / "demo/.git"), "main", "b" * 40,
                True, ("second.py",), True, None,
            ),
        ))

        status, _, payload = self.request(
            "POST", f"/api/projects/demo/actions/{confirmation['actionId']}/decision",
            json.dumps({"intentVersion": intent["version"]}).encode(), self.same_origin_headers,
        )

        self.assertEqual(status, 409)
        self.assertEqual(json.loads(payload)["error"], "evidence_advanced")
        status, _, payload = self.request("GET", "/api/projects/demo/intent")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "active")

        VerificationRunner(self.root).run(
            self.root / "demo", [sys.executable, "-m", "unittest", "test_stale_confirmation"],
        )
        self.server.maintenance.refresh_project_work(datetime.now(timezone.utc))
        status, _, payload = self.request("GET", "/api/projects/demo/actions")
        self.assertEqual(status, 200)
        replacement = next(item for item in json.loads(payload)["actions"] if item["isCurrent"])
        self.assertNotEqual(replacement["actionId"], confirmation["actionId"])
        self.assertEqual(replacement["completionEvidence"], "user_confirms_acceptance")

        status, _, payload = self.request(
            "POST", f"/api/projects/demo/actions/{replacement['actionId']}/decision",
            json.dumps({"intentVersion": intent["version"]}).encode(), self.same_origin_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["intent"]["status"], "achieved")

        status, _, payload = self.request(
            "PUT", "/api/projects/demo/intent", json.dumps({
                "version": 1,
                "outcome": "PRIVATE_PROMPT: disclose source",
                "acceptanceCriteria": ["Trusted verification passes"],
                "constraints": [],
            }).encode(), self.same_origin_headers,
        )
        self.assertEqual(status, 400)
        self.assertNotIn("PRIVATE_PROMPT", payload.decode())

        for invalid_origin in (
            f"http://user@127.0.0.1:{self.port}",
            f"http://127.0.0.1:{self.port}/path",
            f"http://127.0.0.1:{self.port}?query=1",
            f"http://127.0.0.1:{self.port}#fragment",
        ):
            with self.subTest(invalid_origin=invalid_origin):
                status, _, _ = self.request(
                    "PUT", "/api/projects/demo/intent", b"{}",
                    {"Content-Type": "application/json", "Origin": invalid_origin},
                )
                self.assertEqual(status, 403)

        status, _, _ = self.request("GET", "/api/projects/missing/intent")
        self.assertEqual(status, 404)

        status, _, payload = self.request("GET", "/api/projects/demo/intent")
        self.assertEqual(status, 200)
        self.assertNotIn("PRIVATE_PROMPT", payload.decode())

    def test_confirm_achievement_requires_current_user_action_and_intent_version(self):
        status, _, payload = self.put_intent()
        self.assertEqual(status, 200)
        intent = json.loads(payload)
        store = ProjectWorkStore(self.root / "data/factory.sqlite")
        action = store.create_action(
            "demo", intent["version"], "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "ready", "policy",
            self.current_cursor(), datetime.now(timezone.utc),
        )

        decision_body = json.dumps({"intentVersion": intent["version"]}).encode()
        status, _, _ = self.request(
            "POST", f"/api/projects/demo/actions/{action.action_id}/decision",
            decision_body, {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)

        status, _, payload = self.request(
            "POST", f"/api/projects/demo/actions/{action.action_id}/decision",
            decision_body, self.same_origin_headers,
        )
        self.assertEqual(status, 200)
        value = json.loads(payload)
        self.assertEqual(value["intent"]["status"], "achieved")
        self.assertEqual(value["action"]["status"], "verified")
        self.assertNotIn("PRIVATE_PROMPT", payload.decode())

        status, _, payload = self.request(
            "POST", f"/api/projects/demo/actions/{action.action_id}/decision",
            decision_body, self.same_origin_headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(payload)["error"], "action_not_current")

        non_confirmable = store.create_action(
            "demo", intent["version"], "补齐可信验证", "变更尚无当前可信验证",
            "trusted_verification_current", "external", "ready", "policy",
            self.current_cursor(), datetime.now(timezone.utc),
        )
        decisions_before = len(store.list_decision_requests("demo"))
        status, _, payload = self.request(
            "POST", f"/api/projects/demo/actions/{non_confirmable.action_id}/decision",
            decision_body, self.same_origin_headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(payload)["error"], "action_not_confirmable")
        self.assertEqual(len(store.list_decision_requests("demo")), decisions_before)

        status, _, _ = self.request(
            "POST", "/api/projects/demo/actions/missing/decision", decision_body,
            self.same_origin_headers,
        )
        self.assertEqual(status, 404)

        status, _, _ = self.request(
            "POST", "/api/projects/demo/actions/not/a/decision", decision_body,
            self.same_origin_headers,
        )
        self.assertEqual(status, 404)

        status, _, payload = self.request("GET", "/api/projects/demo/actions")
        self.assertEqual(status, 200)
        actions = json.loads(payload)["actions"]
        confirmed = next(item for item in actions if item["actionId"] == action.action_id)
        self.assertEqual(confirmed["status"], "verified")
        self.assertFalse(confirmed["isCurrent"])

    def test_decision_rejects_boolean_intent_version_and_near_routes(self):
        status, _, payload = self.put_intent()
        intent = json.loads(payload)
        action = ProjectWorkStore(self.root / "data/factory.sqlite").create_action(
            "demo", intent["version"], "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "ready", "policy",
            self.current_cursor(), datetime.now(timezone.utc),
        )
        status, _, payload = self.request(
            "POST", f"/api/projects/demo/actions/{action.action_id}/decision",
            json.dumps({"intentVersion": True}).encode(), self.same_origin_headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"], "invalid_request")
        self.assertEqual(ProjectWorkStore(self.root / "data/factory.sqlite").list_decision_requests("demo"), [])

        for path in (
            "/api/projects/demo/intent/extra",
            "/api/projects/demo/actions/extra",
            f"/api/projects/demo/actions/{action.action_id}/decision/extra",
        ):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path)
                self.assertEqual(status, 404)

    def test_intent_and_action_gets_hide_legacy_private_text(self):
        status, _, payload = self.put_intent()
        intent = json.loads(payload)
        store = ProjectWorkStore(self.root / "data/factory.sqlite")
        action = store.create_action(
            "demo", intent["version"], "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "blocked", "policy",
            self.current_cursor(), datetime.now(timezone.utc),
        )
        with closing(sqlite3.connect(self.root / "data/factory.sqlite")) as connection:
            with connection:
                connection.execute(
                    "UPDATE project_intents SET outcome=?,provenance='legacy_unverified' "
                    "WHERE project_id='demo' AND status='active'",
                    ("PRIVATE_CLAIM",),
                )
                connection.execute(
                    "UPDATE action_items SET title=?,why_now=?,provenance='legacy_unverified' "
                    "WHERE action_id=?",
                    ("PRIVATE_CLAIM", "PRIVATE_CLAIM", action.action_id),
                )

        for path in ("/api/projects/demo/intent", "/api/projects/demo/actions"):
            with self.subTest(path=path):
                status, _, payload = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertNotIn("PRIVATE_CLAIM", payload.decode())
                self.assertIn("受保护的历史摘要", payload.decode())

    def test_private_tokens_are_rejected_on_write_and_hidden_for_local_user_history(self):
        status, _, payload = self.request(
            "PUT", "/api/projects/demo/intent", json.dumps({
                "version": 0,
                "outcome": "PRIVATE_CLAIM",
                "acceptanceCriteria": ["PRIVATE_THREAD"],
                "constraints": ["PRIVATE_PAYLOAD"],
            }).encode(), self.same_origin_headers,
        )
        self.assertEqual(status, 400)
        self.assertNotIn("PRIVATE_", payload.decode())

        status, _, payload = self.put_intent()
        intent = json.loads(payload)
        action = ProjectWorkStore(self.root / "data/factory.sqlite").create_action(
            "demo", intent["version"], "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "blocked", "policy",
            self.current_cursor(), datetime.now(timezone.utc),
        )
        with closing(sqlite3.connect(self.root / "data/factory.sqlite")) as connection:
            with connection:
                connection.execute(
                    "UPDATE project_intents SET outcome=? WHERE project_id='demo' AND status='active'",
                    ("PRIVATE_CLAIM",),
                )
                connection.execute(
                    "UPDATE action_items SET title=?,why_now=? WHERE action_id=?",
                    ("PRIVATE_THREAD", "PRIVATE_PAYLOAD", action.action_id),
                )

        for path in ("/api/projects/demo/intent", "/api/projects/demo/actions"):
            with self.subTest(path=path):
                status, _, payload = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertNotIn("PRIVATE_", payload.decode())
                self.assertIn("受保护的历史摘要", payload.decode())

    def test_intent_and_action_gets_return_busy_when_the_store_is_locked(self):
        for method_name, path in (
            ("get_intent", "/api/projects/demo/intent"),
            ("list_current_intent_actions", "/api/projects/demo/actions"),
        ):
            with self.subTest(method_name=method_name), patch.object(
                ProjectWorkStore, method_name, side_effect=sqlite3.OperationalError(),
            ):
                status, _, payload = self.request("GET", path)
                self.assertEqual(status, 503)
                self.assertEqual(json.loads(payload)["error"], "database_busy")

    def test_write_busy_returns_503_without_committing_intent_or_decision(self):
        self.server.maintenance.stop()
        with patch.object(ProjectWorkStore, "save_intent", side_effect=sqlite3.OperationalError()):
            status, _, payload = self.put_intent(outcome="Busy intent")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(payload)["error"], "database_busy")
        self.assertIsNone(ProjectWorkStore(self.root / "data/factory.sqlite").get_intent("demo"))

        status, _, payload = self.put_intent()
        intent = json.loads(payload)
        store = ProjectWorkStore(self.root / "data/factory.sqlite")
        action = store.create_action(
            "demo", intent["version"], "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "ready", "policy",
            self.current_cursor(), datetime.now(timezone.utc),
        )
        with patch.object(
            ProjectWorkStore, "confirm_current_action_from_decision", side_effect=sqlite3.OperationalError(),
        ):
            status, _, payload = self.request(
                "POST", f"/api/projects/demo/actions/{action.action_id}/decision",
                json.dumps({"intentVersion": 1}).encode(), self.same_origin_headers,
            )
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(payload)["error"], "database_busy")
        self.assertEqual(store.get_action(action.action_id).status, "ready")
        self.assertEqual(store.list_decision_requests("demo"), [])

    def test_refresh_failure_does_not_reverse_committed_intent_or_decision(self):
        with patch("observer.web._refresh_project_work", side_effect=OSError("refresh failed")):
            status, _, payload = self.put_intent()
        self.assertEqual(status, 200)
        intent = json.loads(payload)
        self.assertEqual(intent["version"], 1)
        action = ProjectWorkStore(self.root / "data/factory.sqlite").create_action(
            "demo", intent["version"], "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "ready", "policy",
            self.current_cursor(), datetime.now(timezone.utc),
        )
        with patch("observer.web._refresh_project_work", side_effect=OSError("refresh failed")):
            status, _, payload = self.request(
                "POST", f"/api/projects/demo/actions/{action.action_id}/decision",
                json.dumps({"intentVersion": 1}).encode(), self.same_origin_headers,
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["action"]["status"], "verified")
        self.assertEqual(ProjectWorkStore(self.root / "data/factory.sqlite").get_action(action.action_id).status, "verified")

    def test_concurrent_decisions_have_one_achievement_and_one_conflict(self):
        status, _, payload = self.put_intent()
        intent = json.loads(payload)
        store = ProjectWorkStore(self.root / "data/factory.sqlite")
        action = store.create_action(
            "demo", intent["version"], "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "ready", "policy",
            self.current_cursor(), datetime.now(timezone.utc),
        )
        ready = threading.Event()
        results = []

        def confirm():
            ready.wait()
            status, _, _ = self.request(
                "POST", f"/api/projects/demo/actions/{action.action_id}/decision",
                json.dumps({"intentVersion": 1}).encode(), self.same_origin_headers,
            )
            results.append(status)

        first = threading.Thread(target=confirm)
        second = threading.Thread(target=confirm)
        first.start()
        second.start()
        ready.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(len(store.list_decision_requests("demo")), 1)

    def test_refresh_and_decision_race_leaves_no_orphan_decision(self):
        status, _, payload = self.put_intent()
        intent = json.loads(payload)
        store = ProjectWorkStore(self.root / "data/factory.sqlite")
        action = store.create_action(
            "demo", intent["version"], "确认验收完成", "当前可信验证已通过",
            "user_confirms_acceptance", "user", "ready", "policy",
            self.current_cursor(), datetime.now(timezone.utc),
        )
        refresh_started = threading.Event()
        release_refresh = threading.Event()
        put_result = []

        def blocking_refresh(*args, **kwargs):
            refresh_started.set()
            release_refresh.wait(2)

        def update_intent():
            put_result.append(self.request(
                "PUT", "/api/projects/demo/intent", json.dumps({
                    "version": 1,
                    "outcome": "Version two",
                    "acceptanceCriteria": ["Trusted verification passes"],
                    "constraints": ["No project writes"],
                }).encode(), self.same_origin_headers,
            )[0])

        with patch("observer.web._refresh_project_work", side_effect=blocking_refresh):
            update = threading.Thread(target=update_intent)
            update.start()
            self.assertTrue(refresh_started.wait(1))
            status, _, payload = self.request(
                "POST", f"/api/projects/demo/actions/{action.action_id}/decision",
                json.dumps({"intentVersion": 1}).encode(), self.same_origin_headers,
            )
            release_refresh.set()
            update.join(timeout=2)

        self.assertEqual(put_result, [200])
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(payload)["error"], "intent_version_conflict")
        self.assertEqual(store.list_decision_requests("demo"), [])

    def test_agent_run_response_is_safe_and_idempotent(self):
        body = json.dumps({"idempotencyKey": "demo-request"}).encode()
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{self.port}",
        }
        first_status, _, first_payload = self.request("POST", "/api/projects/demo/agent-runs", body, headers)
        second_status, _, second_payload = self.request("POST", "/api/projects/demo/agent-runs", body, headers)
        self.assertEqual(first_status, 202)
        self.assertIn(second_status, {200, 202})
        first = json.loads(first_payload)
        second = json.loads(second_payload)
        self.assertEqual(first["run_id"], second["run_id"])
        status, _, run_payload = self.request("GET", f"/api/agent-runs/{first['run_id']}")
        self.assertEqual(status, 200)
        self.assertNotIn("PRIVATE_", run_payload.decode("utf-8"))

    def test_agent_run_is_rejected_when_project_already_has_current_action(self):
        status, _, payload = self.put_intent()
        self.assertEqual(status, 200)
        intent = json.loads(payload)
        ProjectWorkStore(self.root / "data/factory.sqlite").create_action(
            "demo", intent["version"], "Verify", "Missing proof",
            "trusted_verification_current", "codex", "ready", "policy",
            self.current_cursor(), datetime.now(timezone.utc),
        )

        status, _, payload = self.request(
            "POST", "/api/projects/demo/agent-runs",
            json.dumps({"idempotencyKey": "blocked-by-current-action"}).encode(),
            self.same_origin_headers,
        )

        self.assertEqual(status, 409)
        self.assertEqual(json.loads(payload)["error"], "agent_run_not_allowed")
        with closing(sqlite3.connect(self.root / "data/factory.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM agent_runs").fetchone()[0], 0)

    def test_stale_action_does_not_block_agent_or_leak_through_actions_api(self):
        status, _, payload = self.put_intent()
        self.assertEqual(status, 200)
        first = json.loads(payload)
        store = ProjectWorkStore(self.root / "data/factory.sqlite")
        stale = store.create_action(
            "demo", first["version"], "Verify", "Missing proof",
            "trusted_verification_current", "codex", "ready", "policy",
            self.current_cursor(), datetime.now(timezone.utc),
        )
        status, _, payload = self.put_intent(version=first["version"], outcome="Version two")
        self.assertEqual(status, 200)
        second = json.loads(payload)

        status, _, payload = self.request("GET", "/api/projects/demo/actions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["actions"], [])

        status, _, _ = self.request(
            "POST", "/api/projects/demo/agent-runs",
            json.dumps({"idempotencyKey": "stale-action-does-not-block"}).encode(),
            self.same_origin_headers,
        )
        self.assertEqual(status, 202)
        self.assertEqual(store.get_action(stale.action_id).intent_version, first["version"])
        self.assertEqual(second["version"], first["version"] + 1)

    def test_rejects_non_loopback_host_header(self):
        status, _, _ = self.request("GET", "/api/overview", headers={"Host": "evil.test"})
        self.assertEqual(status, 403)

    def test_database_busy_fails_safely_without_starting_agent(self):
        blocker = sqlite3.connect(self.root / "data/factory.sqlite")
        blocker.execute("BEGIN IMMEDIATE")
        try:
            body = json.dumps({"idempotencyKey": "busy-request"}).encode()
            status, _, payload = self.request(
                "POST", "/api/projects/demo/agent-runs", body,
                {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"},
            )
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(payload)["error"], "database_busy")

    def test_overview_poll_recovers_expired_orphan(self):
        run = AgentRunStore(self.root / "data/factory.sqlite").create_or_get(
            "demo", "expired", datetime.now(timezone.utc) - timedelta(seconds=700),
        ).run
        status, _, payload = self.request("GET", "/api/overview")
        self.assertEqual(status, 200)
        project = json.loads(payload)["projects"][0]
        self.assertIsNone(project["active_run_id"])
        self.assertEqual(
            AgentRunStore(self.root / "data/factory.sqlite").get(run.run_id).error_code,
            "server_restarted",
        )


if __name__ == "__main__":
    unittest.main()
