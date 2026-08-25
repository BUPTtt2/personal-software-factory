import tempfile
import sys
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from observer.event_store import EventRecord, EventStore
from observer.reconcile import AppServerClient, AppServerError, reconcile_stale_turns


PYTHON = sys.executable
FAKE = Path(__file__).parent / "fakes/fake_app_server.py"


class AppServerClientTests(unittest.TestCase):
    def client(self, mode: str, timeout: float = 1.0):
        return AppServerClient((PYTHON, str(FAKE), mode), request_timeout=timeout)

    def test_initializes_and_ignores_unrelated_notifications(self):
        with self.client("idle") as client:
            data = client.thread_list(("/repo",))
            thread = client.thread_read("thread-1")
        self.assertEqual(data[0].thread_id, "thread-1")
        self.assertEqual(thread.runtime_status, "idle")

    def test_timeout_and_early_exit_are_errors(self):
        with self.assertRaises(AppServerError):
            with self.client("hang", timeout=0.05) as client:
                client.initialize()
        with self.assertRaises(AppServerError):
            with self.client("exit") as client:
                client.initialize()


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.store = EventStore(root / "factory.sqlite", root / "buffer")
        self.store.initialize()
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        self.store.append_event(EventRecord(
            "event-1", "dedupe-1", "demo", "thread-1", "turn-1", "turn_started",
            old, 1, {"promptSummary": "safe"}, None, None,
        ))

    def turn_status(self):
        return self.store.get_turn("thread-1", "turn-1")["status"]

    def thread_status(self):
        with closing(self.store.connect()) as connection:
            return connection.execute(
                "SELECT status FROM threads WHERE thread_id='thread-1'"
            ).fetchone()[0]

    def test_inactive_stale_turn_becomes_interrupted(self):
        with AppServerClient((PYTHON, str(FAKE), "idle")) as client:
            report = reconcile_stale_turns(self.store, client, datetime.now(timezone.utc), timedelta(minutes=30))
        self.assertEqual((report.examined, report.interrupted), (1, 1))
        self.assertEqual(self.turn_status(), "interrupted")
        self.assertEqual(self.thread_status(), "interrupted")

    def test_active_stale_turn_remains_working(self):
        with AppServerClient((PYTHON, str(FAKE), "active")) as client:
            report = reconcile_stale_turns(self.store, client, datetime.now(timezone.utc), timedelta(minutes=30))
        self.assertEqual(report.still_active, 1)
        self.assertEqual(self.turn_status(), "working")
        self.assertEqual(self.thread_status(), "working")

    def test_unavailable_server_marks_runtime_unknown(self):
        with AppServerClient((PYTHON, str(FAKE), "exit")) as client:
            report = reconcile_stale_turns(self.store, client, datetime.now(timezone.utc), timedelta(minutes=30))
        self.assertEqual(report.unknown, 1)
        self.assertEqual(self.turn_status(), "unknown")
        self.assertEqual(self.thread_status(), "unknown")


if __name__ == "__main__":
    unittest.main()
