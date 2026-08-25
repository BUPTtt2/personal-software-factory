import io
import json
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from observer.mcp_tools import FactoryServiceClient, call_tool, list_tool_definitions


PROJECT = {
    "project_id": "demo",
    "display_name": "Demo",
    "judgement": "等待验证",
    "reason": "存在代码变化",
    "next_action": "运行可信验证",
    "activity_status": "recent",
    "intent_status": "active",
    "outcome_summary": "Deliver a verified loop",
    "last_event_at": "2026-08-25T01:00:00+00:00",
    "current_action": {
        "action_id": "action-1",
        "title": "运行可信验证",
        "completion_evidence": "trusted_verification_current",
        "actor": "codex",
        "status": "ready",
    },
    "agent_activity": {"status": "not_run", "error_code": None},
    "evidence": [
        {"source": "Observer", "summary": "发现 1 次近期活动"},
        {"source": "Git", "summary": "工作区存在变更"},
        {"source": "Verifier", "summary": "尚无当前验证"},
    ],
    "work_summary": {
        "stage": "验证待完成",
        "active_threads": 0,
        "interrupted_threads": 0,
        "branch": "main",
        "head": "a" * 40,
        "changed_paths": ["src/app.py"],
        "verification_status": "unknown",
        "verification_freshness": "unknown",
        "verification_observed_at": None,
    },
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/overview":
            return self.respond(200, {"schemaVersion": 1, "projects": [PROJECT]})
        if self.path == "/api/projects/demo":
            return self.respond(200, PROJECT)
        if self.path == "/api/projects/busy":
            return self.respond(503, {"error": "database_busy"})
        if self.path == "/api/projects/wrong":
            return self.respond(200, PROJECT)
        if self.path == "/api/projects/leaky":
            return self.respond(200, PROJECT | {
                "project_id": "leaky", "reason": "token=" + "sk-" + "live-example-secret-value",
            })
        if self.path == "/api/projects/huge":
            return self.respond(200, PROJECT | {
                "project_id": "huge", "reason": "x" * 1_100_000,
            })
        if self.path == "/api/health":
            return self.respond(200, {"schemaVersion": 1, "status": "ok"})
        return self.respond(404, {"error": "project_not_found"})

    def respond(self, status, value):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        return


class McpToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.project_root = self.directory / "demo"
        self.project_root.mkdir()
        self.registry = self.directory / "projects.json"
        self.registry.write_text(json.dumps({"projects": [{
            "id": "demo",
            "displayName": "Demo",
            "roots": [str(self.project_root)],
            "gitRemotes": [],
            "enabled": True,
        }]}), encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = FactoryServiceClient(self.base_url, self.registry)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_lists_exactly_five_read_only_tools(self):
        definitions = list_tool_definitions()
        self.assertEqual([item["name"] for item in definitions], [
            "factory_list_projects",
            "factory_get_current_project",
            "factory_get_project_status",
            "factory_get_evidence_summary",
            "factory_open_console",
        ])
        self.assertTrue(all(item["annotations"]["readOnlyHint"] for item in definitions))
        current = next(item for item in definitions if item["name"] == "factory_get_current_project")
        self.assertEqual(current["inputSchema"]["required"], ["cwd"])

    def test_project_status_returns_bounded_projection(self):
        result = call_tool("factory_get_project_status", {"projectId": "demo"}, self.client)
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["projectId"], "demo")
        self.assertEqual(result.structured_content["currentAction"]["title"], "运行可信验证")
        rendered = json.dumps(result.structured_content, ensure_ascii=False)
        self.assertNotIn(str(self.project_root), rendered)
        self.assertNotIn("changed_paths", rendered)

    def test_evidence_summary_contains_only_bounded_sources(self):
        result = call_tool("factory_get_evidence_summary", {"projectId": "demo"}, self.client)
        self.assertEqual([item["source"] for item in result.structured_content["evidence"]], [
            "Observer", "Git", "Verifier",
        ])
        self.assertEqual(result.structured_content["verificationStatus"], "unknown")
        self.assertEqual(result.structured_content["changedPathCount"], 1)
        self.assertNotIn("src/app.py", json.dumps(result.structured_content))

    def test_current_project_uses_registry_without_returning_path(self):
        result = call_tool(
            "factory_get_current_project", {"cwd": str(self.project_root / "src")}, self.client,
        )
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, {
            "projectId": "demo", "displayName": "Demo", "matchedBy": "root",
        })

    def test_unregistered_current_project_is_recoverable(self):
        result = call_tool(
            "factory_get_current_project", {"cwd": str(self.directory / "other")}, self.client,
        )
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"], "project_not_registered")

    def test_current_project_requires_explicit_task_cwd(self):
        result = call_tool("factory_get_current_project", {}, self.client)
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"], "invalid_arguments")

    def test_current_project_rejects_sensitive_registry_text(self):
        self.registry.write_text(json.dumps({"projects": [{
            "id": "demo",
            "displayName": "token=" + "sk-" + "live-example-secret-value",
            "roots": [str(self.project_root)],
            "gitRemotes": [],
            "enabled": True,
        }]}), encoding="utf-8")
        result = call_tool(
            "factory_get_current_project", {"cwd": str(self.project_root)}, self.client,
        )
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"], "service_unavailable")

    def test_project_projection_rejects_wrong_identity_sensitive_text_and_oversized_body(self):
        for project_id in ("wrong", "leaky", "huge"):
            with self.subTest(project_id=project_id):
                result = call_tool(
                    "factory_get_project_status", {"projectId": project_id}, self.client,
                )
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"], "service_unavailable")

    def test_http_error_body_is_read_with_a_hard_limit(self):
        class TrackingBody(io.BytesIO):
            requested = None

            def read(self, size=-1):
                self.requested = size
                return super().read(size)

        body = TrackingBody(b'{"error":"' + b"x" * 1_100_000 + b'"}')
        error = urllib.error.HTTPError(
            self.base_url, 503, "busy", {"Content-Type": "application/json"}, body,
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError):
                self.client.get("/api/overview")
        self.assertEqual(body.requested, 1_048_577)

    def test_console_tool_checks_health(self):
        result = call_tool("factory_open_console", {"projectId": "demo"}, self.client)
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, {
            "url": f"{self.base_url}/?project=demo", "healthy": True, "projectId": "demo",
        })

    def test_service_failure_and_database_busy_are_recoverable(self):
        offline = FactoryServiceClient("http://127.0.0.1:1", self.registry, timeout=0.05)
        unavailable = call_tool("factory_list_projects", {}, offline)
        self.assertTrue(unavailable.is_error)
        self.assertEqual(unavailable.structured_content["error"], "service_unavailable")
        busy = call_tool("factory_get_project_status", {"projectId": "busy"}, self.client)
        self.assertTrue(busy.is_error)
        self.assertEqual(busy.structured_content["error"], "database_busy")

    def test_unknown_tool_and_unexpected_arguments_fail_closed(self):
        unknown = call_tool("factory_delete_project", {}, self.client)
        self.assertEqual(unknown.structured_content["error"], "unknown_tool")
        extra = call_tool("factory_list_projects", {"includePaths": True}, self.client)
        self.assertEqual(extra.structured_content["error"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
