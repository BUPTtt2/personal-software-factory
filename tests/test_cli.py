import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from observer.cli import main


class CliTests(unittest.TestCase):
    def test_malformed_hook_input_is_advisory_and_safe(self):
        root = Path(tempfile.mkdtemp())
        state_root = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {"SOFTWARE_FACTORY_HOME": str(state_root)}), \
                patch("sys.stdin", io.StringIO("not-json")), redirect_stderr(io.StringIO()):
            result = main(["ingest", "--stdin", "--root", str(root)])
        self.assertEqual(result, 0)
        health = state_root / "logs/observer-health.log"
        self.assertTrue(health.exists())
        self.assertNotIn("not-json", health.read_text(encoding="utf-8"))

    def test_serve_passes_safe_defaults_and_options(self):
        root = Path(tempfile.mkdtemp())
        state_root = Path(tempfile.mkdtemp())
        registry = state_root / "projects.json"
        with patch("observer.web.serve") as serve:
            result = main([
                "serve", "--root", str(root), "--port", "9876", "--open",
                "--codex", "/opt/codex", "--state-root", str(state_root),
                "--registry", str(registry),
            ])
        self.assertEqual(result, 0)
        serve.assert_called_once_with(
            root=root.resolve(strict=False),
            host="127.0.0.1",
            port=9876,
            open_browser=True,
            codex_executable="/opt/codex",
            state_root=state_root.resolve(strict=False),
            registry_path=registry.resolve(strict=False),
        )

    def test_verify_returns_real_runner_exit_code(self):
        root = Path(tempfile.mkdtemp())
        cwd = root / "project"
        cwd.mkdir()
        with patch("observer.verification_runner.VerificationRunner.run") as run:
            run.return_value = SimpleNamespace(
                event_id="event", project_id="demo", kind="test",
                command_class="python_unittest", status="failed", exit_code=7,
            )
            with redirect_stdout(io.StringIO()):
                result = main([
                    "verify", "--root", str(root), "--cwd", str(cwd), "--",
                    "python3", "-m", "unittest",
                ])
        self.assertEqual(result, 7)
        run.assert_called_once_with(cwd.resolve(strict=False), ["python3", "-m", "unittest"])


if __name__ == "__main__":
    unittest.main()
