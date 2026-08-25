import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from observer.agent_runtime import AgentRunStore, CodexSupervisorRunner, _controlled_path
from observer.config import ProjectConfig
from observer.console_model import EvidenceItem, ProjectSnapshot
from observer.event_store import EventStore


NOW = datetime(2026, 8, 13, 10, tzinfo=timezone.utc)


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        EventStore(self.root / "data/factory.sqlite", self.root / "buffer").initialize()
        self.store = AgentRunStore(self.root / "data/factory.sqlite")
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.project = ProjectConfig("demo", "Demo", (self.project_root,), (), True)
        self.snapshot = ProjectSnapshot(
            project_id="demo", display_name="Demo", source="policy",
            judgement="需要检查", reason="状态未知", next_action="只读检查",
            can_run_agent=True,
            evidence=(EvidenceItem("Observer", "没有退出码证据"),),
            last_event_at=None, active_run_id=None,
        )
        self.capture_path = self.root / "captured-args.json"
        self.fake_codex = self.root / "fake-codex"
        self.fake_codex.write_text(
            f"#!{sys.executable}\n"
            "import json,os,sys,time\n"
            "mode=os.environ.get('FAKE_CODEX_MODE','success')\n"
            "open(os.environ['FAKE_CAPTURE'],'w').write(json.dumps(sys.argv[1:]))\n"
            "out=sys.argv[sys.argv.index('-o')+1]\n"
            "print(json.dumps({'type':'thread.started','thread_id':'thread-supervisor'}),flush=True)\n"
            "if mode=='timeout': time.sleep(2)\n"
            "if mode=='failed': raise SystemExit(7)\n"
            "if mode=='invalid': open(out,'w').write('not-json')\n"
            "else: open(out,'w').write(json.dumps({'judgement':'Need review',"
            "'reason':'Evidence is incomplete','nextAction':'Inspect verification',"
            "'requiresUser':False}))\n",
            encoding="utf-8",
        )
        self.fake_codex.chmod(0o755)
        self.schema = self.root / "schema.json"
        self.schema.write_text("{}", encoding="utf-8")

    def create_run(self, key="request-1"):
        return self.store.create_or_get("demo", key, NOW)

    def runner(self, timeout=2):
        environment = {"FAKE_CAPTURE": str(self.capture_path)}
        if "FAKE_CODEX_MODE" in os.environ:
            environment["FAKE_CODEX_MODE"] = os.environ["FAKE_CODEX_MODE"]
        return CodexSupervisorRunner(
            self.root, self.store, str(self.fake_codex), self.schema,
            timeout_seconds=timeout,
            environment=environment,
        )

    def test_create_is_idempotent(self):
        first = self.create_run()
        second = self.create_run()
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.run.run_id, second.run.run_id)
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM agent_runs").fetchone()[0], 1)

    def test_concurrent_requests_create_one_active_run(self):
        def create(index):
            return self.store.create_or_get("demo", f"request-{index}", NOW).run.run_id

        with ThreadPoolExecutor(max_workers=8) as executor:
            run_ids = list(executor.map(create, range(12)))

        self.assertEqual(len(set(run_ids)), 1)
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            active = connection.execute(
                "SELECT count(*) FROM agent_runs WHERE project_id='demo' AND status IN ('queued','running')"
            ).fetchone()[0]
        self.assertEqual(active, 1)

    def test_idempotency_is_scoped_to_project(self):
        first = self.store.create_or_get("demo", "same-key", NOW).run
        self.store.mark_failed(first.run_id, "finished", NOW)
        second = self.store.create_or_get("other", "same-key", NOW).run
        self.assertNotEqual(first.run_id, second.run_id)

    def test_recover_abandoned_runs_releases_project(self):
        abandoned = self.store.create_or_get(
            "demo", "abandoned", NOW - timedelta(seconds=700),
        ).run
        recovered = self.store.recover_abandoned(NOW)
        self.assertEqual(recovered, 1)
        self.assertEqual(self.store.get(abandoned.run_id).error_code, "server_restarted")
        self.assertTrue(self.store.create_or_get("demo", "after-restart", NOW).created)

    def test_recovery_does_not_kill_fresh_run_from_another_server(self):
        active = self.create_run("fresh").run
        self.assertEqual(self.store.recover_abandoned(NOW), 0)
        self.assertEqual(self.store.get(active.run_id).status, "queued")

    def test_success_is_read_only_structured_and_redacted(self):
        created = self.create_run()
        previous = os.environ.get("FAKE_CODEX_MODE")
        os.environ["FAKE_CODEX_MODE"] = "success"
        try:
            result = self.runner().run(created.run.run_id, self.project, self.snapshot)
        finally:
            if previous is None:
                os.environ.pop("FAKE_CODEX_MODE", None)
            else:
                os.environ["FAKE_CODEX_MODE"] = previous

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.thread_id, "thread-supervisor")
        self.assertNotIn("sk-live-secret", result.judgement)
        args = json.loads(self.capture_path.read_text(encoding="utf-8"))
        self.assertIn("--sandbox", args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", args)
        self.assertNotEqual(args[args.index("--cd") + 1], str(self.project_root))
        self.assertEqual(args[args.index("--add-dir") + 1], str(self.project_root))
        self.assertTrue(any(item.startswith("shell_environment_policy.set.PATH=") for item in args))
        self.assertEqual(args[args.index("--output-schema") + 1], str(self.schema))
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            stored = json.dumps(connection.execute("SELECT * FROM agent_runs").fetchone())
        self.assertNotIn("sk-live-secret", stored)

    def test_standalone_secret_output_fails_closed(self):
        self.fake_codex.write_text(
            self.fake_codex.read_text(encoding="utf-8").replace(
                "Need review", "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            ),
            encoding="utf-8",
        )
        created = self.create_run("standalone-secret")
        result = self.runner().run(created.run.run_id, self.project, self.snapshot)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "agent_sensitive_output")
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            stored = json.dumps(connection.execute("SELECT * FROM agent_runs").fetchone())
        self.assertNotIn("ghp_", stored)

    def test_source_excerpt_output_fails_closed(self):
        self.fake_codex.write_text(
            self.fake_codex.read_text(encoding="utf-8").replace(
                "Need review", "def leaked_source(): return 1"
            ),
            encoding="utf-8",
        )
        created = self.create_run("source-excerpt")
        result = self.runner().run(created.run.run_id, self.project, self.snapshot)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "agent_source_output")
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            stored = json.dumps(connection.execute("SELECT * FROM agent_runs").fetchone())
        self.assertNotIn("leaked_source", stored)

    def test_invalid_output_fails_with_safe_code(self):
        created = self.create_run("invalid")
        os.environ["FAKE_CODEX_MODE"] = "invalid"
        try:
            result = self.runner().run(created.run.run_id, self.project, self.snapshot)
        finally:
            os.environ.pop("FAKE_CODEX_MODE", None)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "invalid_agent_output")

    def test_nonzero_exit_fails_with_safe_code(self):
        created = self.create_run("failed")
        os.environ["FAKE_CODEX_MODE"] = "failed"
        try:
            result = self.runner().run(created.run.run_id, self.project, self.snapshot)
        finally:
            os.environ.pop("FAKE_CODEX_MODE", None)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "codex_failed")

    def test_timeout_terminates_process(self):
        created = self.create_run("timeout")
        os.environ["FAKE_CODEX_MODE"] = "timeout"
        try:
            result = self.runner(timeout=0.05).run(created.run.run_id, self.project, self.snapshot)
        finally:
            os.environ.pop("FAKE_CODEX_MODE", None)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "agent_timeout")

    def test_runner_retries_state_write_during_transient_database_lock(self):
        created = self.create_run("locked")
        blocker = sqlite3.connect(self.store.db_path, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        def release_lock():
            time.sleep(0.4)
            blocker.rollback()
            blocker.close()
        release = threading.Thread(target=release_lock)
        release.start()
        result = self.runner().run(created.run.run_id, self.project, self.snapshot)
        release.join(timeout=2)
        self.assertEqual(result.status, "completed")

    def test_runner_does_not_escape_when_status_database_stays_locked(self):
        created = self.create_run("long-lock")
        runner = self.runner()
        original = self.store.get(created.run.run_id)
        runner.store.mark_running = lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
        runner.store.mark_failed = lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
        runner.store.get = lambda *args: original
        result = runner.run(created.run.run_id, self.project, self.snapshot)
        self.assertEqual(result.run_id, created.run.run_id)
        self.assertEqual(result.status, "queued")

    def test_controlled_path_starts_with_working_git(self):
        first = Path(_controlled_path().split(os.pathsep)[0]) / "git"
        self.assertEqual(os.spawnv(os.P_WAIT, str(first), [str(first), "--version"]), 0)


if __name__ == "__main__":
    unittest.main()
