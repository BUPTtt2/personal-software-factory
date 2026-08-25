import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class McpServerTests(unittest.TestCase):
    def run_server(self, messages):
        directory = Path(tempfile.mkdtemp())
        registry = directory / "projects.json"
        registry.write_text('{"projects":[]}', encoding="utf-8")
        environment = os.environ.copy()
        environment.update({
            "SOFTWARE_FACTORY_HOME": str(directory),
            "SOFTWARE_FACTORY_REGISTRY": str(registry),
            "SOFTWARE_FACTORY_URL": "http://127.0.0.1:1",
        })
        process = subprocess.run(
            [sys.executable, "-m", "observer.mcp_server"],
            input="\n".join(json.dumps(item) if isinstance(item, dict) else item for item in messages) + "\n",
            text=True,
            capture_output=True,
            timeout=5,
            env=environment,
            check=False,
        )
        responses = [json.loads(line) for line in process.stdout.splitlines() if line]
        return process, responses

    def test_initialize_and_tools_list_use_real_stdio_process(self):
        process, responses = self.run_server([
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["result"]["serverInfo"], {
            "name": "personal-software-factory", "version": "0.1.0",
        })
        self.assertEqual([item["name"] for item in responses[1]["result"]["tools"]], [
            "factory_list_projects", "factory_get_current_project",
            "factory_get_project_status", "factory_get_evidence_summary",
            "factory_open_console",
        ])

    def test_tools_call_returns_recoverable_service_error(self):
        process, responses = self.run_server([{
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "factory_list_projects", "arguments": {}},
        }])
        self.assertEqual(process.returncode, 0)
        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"], {"error": "service_unavailable"})

    def test_unknown_method_and_malformed_json_return_json_rpc_errors(self):
        process, responses = self.run_server([
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {}},
            "{not-json",
        ])
        self.assertEqual(process.returncode, 0)
        self.assertEqual(responses[0]["error"]["code"], -32601)
        self.assertEqual(responses[1]["error"]["code"], -32700)
        self.assertIsNone(responses[1]["id"])

    def test_ping_and_invalid_call_params_are_bounded(self):
        process, responses = self.run_server([
            {"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": []},
        ])
        self.assertEqual(process.returncode, 0)
        self.assertEqual(responses[0]["result"], {})
        self.assertEqual(responses[1]["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
