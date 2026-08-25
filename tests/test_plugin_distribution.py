import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts/validate-plugin.py"
SKILL_VALIDATOR = ROOT / "scripts/validate-skill.py"


class PluginDistributionTests(unittest.TestCase):
    def test_skill_eval_corpus_covers_positive_negative_failure_and_overreach(self):
        value = json.loads((ROOT / "evals/software-factory-skill.json").read_text(encoding="utf-8"))
        counts = {}
        for case in value["cases"]:
            counts[case["category"]] = counts.get(case["category"], 0) + 1
            self.assertTrue(case["mustPreserve"])
        self.assertEqual(counts, {"positive": 5, "negative": 5, "failure": 3, "overreach": 3})

    def test_manifest_is_valid_and_all_declared_components_exist(self):
        manifest_path = ROOT / ".codex-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "personal-software-factory")
        self.assertRegex(
            manifest["version"], r"^0\.1\.0(?:\+codex\.[0-9A-Za-z.-]+)?$",
        )
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertNotIn("hooks", manifest)
        for relative in (manifest["skills"], manifest["mcpServers"]):
            self.assertTrue((ROOT / relative).exists())
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), str(ROOT)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_launcher_executes_real_stdio_server_from_any_cwd(self):
        launcher = ROOT / "bin/software-factory-mcp"
        self.assertTrue(os.access(launcher, os.X_OK))
        state = Path(tempfile.mkdtemp())
        (state / "observer").mkdir()
        (state / "observer/__init__.py").write_text("", encoding="utf-8")
        registry = state / "projects.json"
        registry.write_text('{"projects":[]}', encoding="utf-8")
        environment = os.environ.copy()
        environment.update({
            "SOFTWARE_FACTORY_PYTHON": sys.executable,
            "SOFTWARE_FACTORY_HOME": str(state),
            "SOFTWARE_FACTORY_REGISTRY": str(registry),
            "SOFTWARE_FACTORY_URL": "http://127.0.0.1:1",
        })
        request = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
        }) + "\n"
        result = subprocess.run(
            [str(launcher)], cwd=state, input=request, capture_output=True,
            text=True, timeout=5, env=environment, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["serverInfo"]["name"], "personal-software-factory")

    def test_skill_is_discoverable_and_keeps_mutations_out_of_scope(self):
        skill = ROOT / "skills/software-factory/SKILL.md"
        result = subprocess.run(
            [sys.executable, str(SKILL_VALIDATOR), str(skill.parent)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        body = skill.read_text(encoding="utf-8")
        for tool in (
            "factory_get_current_project", "factory_get_project_status",
            "factory_get_evidence_summary", "factory_open_console",
        ):
            self.assertIn(tool, body)
        for forbidden in ("自动修改代码", "自动合并", "自动部署"):
            self.assertNotIn(forbidden, body)

    def test_mcp_config_uses_relative_launcher(self):
        value = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(value, {"mcpServers": {"software-factory": {
            "command": "./bin/software-factory-mcp", "cwd": ".",
        }}})


if __name__ == "__main__":
    unittest.main()
