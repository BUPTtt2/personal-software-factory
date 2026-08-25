import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from observer.verification_runner import VerificationRunner, VerificationRejected


class VerificationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.project = self.root / "demo"
        self.project.mkdir()
        (self.root / "config").mkdir()
        (self.root / "config/projects.yaml").write_text(json.dumps({"projects": [{
            "id": "demo",
            "displayName": "Demo",
            "roots": [str(self.project)],
            "gitRemotes": [],
            "enabled": True,
        }]}), encoding="utf-8")
        self.runner = VerificationRunner(self.root)

    def test_records_real_success_and_failure_exit_codes_without_command_text(self):
        (self.project / "test_ok.py").write_text(
            "import unittest\nclass Ok(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        success = self.runner.run(
            self.project,
            [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_ok.py"],
        )
        failure = self.runner.run(
            self.project,
            [sys.executable, "-m", "unittest", "module_that_does_not_exist_for_observer"],
        )

        self.assertEqual((success.exit_code, success.status), (0, "passed"))
        self.assertNotEqual(failure.exit_code, 0)
        self.assertEqual(failure.status, "failed")
        with closing(sqlite3.connect(self.root / "data/factory.sqlite")) as connection:
            rows = connection.execute(
                "SELECT verification_records.exit_code,verification_records.status,events.event_type,"
                "events.redacted_payload,threads.status "
                "FROM verification_records JOIN events USING(event_id) "
                "JOIN threads ON threads.thread_id=events.thread_id ORDER BY events.occurred_at"
            ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [(0, "passed"), (failure.exit_code, "failed")])
        self.assertTrue(all(row[2] == "verification_receipt" for row in rows))
        self.assertTrue(all(row[4] == "stopped" for row in rows))
        rendered = "".join(row[3] for row in rows)
        self.assertNotIn("module_that_does_not_exist_for_observer", rendered)
        self.assertNotIn(sys.executable, rendered)

    def test_rejects_unknown_command_before_execution_and_outside_project(self):
        execute = Mock()
        runner = VerificationRunner(self.root, executor=execute)
        with self.assertRaises(VerificationRejected):
            runner.run(self.project, ["git", "push"])
        with self.assertRaises(VerificationRejected):
            runner.run(self.root / "outside", [sys.executable, "-m", "unittest"])
        execute.assert_not_called()
        self.assertFalse((self.root / "data/factory.sqlite").exists())

    def test_rejects_project_local_executable_and_canonicalizes_trusted_python(self):
        execute = Mock(return_value=type("Result", (), {"returncode": 0})())
        runner = VerificationRunner(self.root, executor=execute)
        with self.assertRaises(VerificationRejected):
            runner.run(self.project, ["./python3", "-m", "unittest"])
        runner.run(self.project, ["python3", "-m", "unittest"])
        executed = execute.call_args.args[0]
        self.assertTrue(Path(executed[0]).is_absolute())
        self.assertNotEqual(executed[0], str(self.project / "python3"))

    def test_external_json_registry_is_used_without_legacy_layout(self):
        state = Path(tempfile.mkdtemp())
        registry = state / "config/projects.json"
        registry.parent.mkdir()
        registry.write_text((self.root / "config/projects.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        execute = Mock(return_value=type("Result", (), {"returncode": 0})())
        result = VerificationRunner(
            state, executor=execute, registry_path=registry,
        ).run(self.project, ["python3", "-m", "unittest"])
        self.assertEqual(result.project_id, "demo")
        self.assertTrue((state / "data/factory.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
