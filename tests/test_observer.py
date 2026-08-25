import json
import sqlite3
import subprocess
import shutil
import tempfile
import unittest
import sqlite3 as sqlite_module
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from observer.observer import ObserverSettings, ingest_hook


GIT = shutil.which("git") or "git"
FIXTURES = Path(__file__).parent / "fixtures/hooks"


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.repo = self.root / "repo"
        subprocess.run([GIT, "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run([GIT, "-C", str(self.repo), "config", "user.name", "Observer Test"], check=True)
        subprocess.run([GIT, "-C", str(self.repo), "config", "user.email", "observer@test.invalid"], check=True)
        (self.repo / "seed.txt").write_text("seed", encoding="utf-8")
        subprocess.run([GIT, "-C", str(self.repo), "add", "seed.txt"], check=True)
        subprocess.run([GIT, "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        config = self.root / "projects.yaml"
        config.write_text(json.dumps({"projects": [{
            "id": "demo", "displayName": "Demo", "roots": [str(self.repo)],
            "gitRemotes": [], "enabled": True,
        }]}), encoding="utf-8")
        self.settings = ObserverSettings(
            registry_path=config, db_path=self.root / "factory.sqlite",
            buffer_dir=self.root / "buffer", git_executable=GIT,
        )

    def fixture(self, name: str, cwd: Path | None = None) -> dict:
        text = (FIXTURES / name).read_text(encoding="utf-8")
        return json.loads(text.replace("__REGISTERED_CWD__", str(cwd or self.repo)))

    def database_bytes(self) -> bytes:
        return self.settings.db_path.read_bytes()

    def test_registered_prompt_is_redacted_and_idempotent(self):
        event = self.fixture("user_prompt_submit.json")
        first = ingest_hook(event, self.settings)
        second = ingest_hook(event, self.settings)
        self.assertEqual(first.status, "recorded")
        self.assertEqual(second.status, "duplicate")
        self.assertNotIn(b"fixture-secret", self.database_bytes())
        with closing(sqlite3.connect(self.settings.db_path)) as connection:
            payload = connection.execute("SELECT redacted_payload FROM events").fetchone()[0]
            self.assertIn("[REDACTED]", payload)

    def test_unregistered_project_is_ignored_without_database(self):
        event = self.fixture("user_prompt_submit.json", self.root / "elsewhere")
        (self.root / "elsewhere").mkdir()
        result = ingest_hook(event, self.settings)
        self.assertEqual(result.status, "ignored_unregistered")
        self.assertFalse(self.settings.db_path.exists())

    def test_apply_patch_stores_git_paths_not_tool_bodies(self):
        (self.repo / "seed.txt").write_text("changed", encoding="utf-8")
        result = ingest_hook(self.fixture("post_tool_apply_patch.json"), self.settings)
        self.assertEqual(result.status, "recorded")
        raw = self.database_bytes()
        self.assertIn(b"seed.txt", raw)
        self.assertNotIn(b"source-content", raw)
        self.assertNotIn(b"fixture-secret", raw)

    def test_verification_and_stop_store_only_safe_summaries(self):
        ingest_hook(self.fixture("post_tool_bash_success.json"), self.settings)
        ingest_hook(self.fixture("post_tool_bash_failure.json"), self.settings)
        ingest_hook(self.fixture("stop.json"), self.settings)
        raw = self.database_bytes()
        self.assertNotIn(b"fixture-secret", raw)
        self.assertNotIn(b"npm test", raw)
        with closing(sqlite3.connect(self.settings.db_path)) as connection:
            statuses = [row[0] for row in connection.execute("SELECT status FROM verification_records ORDER BY observed_at")]
        self.assertEqual(statuses, ["passed", "failed"])

    def test_turn_row_tracks_redacted_prompt_and_stop_claim(self):
        ingest_hook(self.fixture("user_prompt_submit.json"), self.settings)
        ingest_hook(self.fixture("stop.json"), self.settings)
        with closing(sqlite3.connect(self.settings.db_path)) as connection:
            row = connection.execute(
                "SELECT status,prompt_summary,assistant_claim_summary,stopped_at FROM turns WHERE thread_id=? AND turn_id=?",
                ("thread-test-1", "turn-test-1"),
            ).fetchone()
        self.assertEqual(row[0], "stopped")
        self.assertIn("[REDACTED]", row[1])
        self.assertIn("[REDACTED]", row[2])
        self.assertIsNotNone(row[3])

    def test_stop_does_not_overwrite_evidence_status(self):
        ingest_hook(self.fixture("user_prompt_submit.json"), self.settings)
        (self.repo / "seed.txt").write_text("changed", encoding="utf-8")
        ingest_hook(self.fixture("post_tool_apply_patch.json"), self.settings)
        ingest_hook(self.fixture("stop.json"), self.settings)
        self.assertEqual(self._turn_status(), "evidence_incomplete")

    def test_passing_and_failing_verification_are_deterministic(self):
        ingest_hook(self.fixture("user_prompt_submit.json"), self.settings)
        (self.repo / "seed.txt").write_text("changed", encoding="utf-8")
        ingest_hook(self.fixture("post_tool_apply_patch.json"), self.settings)
        ingest_hook(self.fixture("post_tool_bash_success.json"), self.settings)
        ingest_hook(self.fixture("stop.json"), self.settings)
        self.assertEqual(self._turn_status(), "verification_passed")

    def test_failure_wins_over_stop_and_passing_evidence(self):
        ingest_hook(self.fixture("user_prompt_submit.json"), self.settings)
        ingest_hook(self.fixture("post_tool_bash_success.json"), self.settings)
        ingest_hook(self.fixture("post_tool_bash_failure.json"), self.settings)
        ingest_hook(self.fixture("stop.json"), self.settings)
        self.assertEqual(self._turn_status(), "verification_failed")

    def test_database_lock_before_initialization_buffers_redacted_event(self):
        with patch("observer.observer.EventStore.initialize", side_effect=sqlite_module.OperationalError("database is locked")):
            result = ingest_hook(self.fixture("user_prompt_submit.json"), self.settings)
        self.assertEqual(result.status, "buffered")
        files = list(self.settings.buffer_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn("fixture-secret", files[0].read_text(encoding="utf-8"))

    def _turn_status(self):
        with closing(sqlite3.connect(self.settings.db_path)) as connection:
            return connection.execute(
                "SELECT status FROM turns WHERE thread_id='thread-test-1' AND turn_id='turn-test-1'"
            ).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
