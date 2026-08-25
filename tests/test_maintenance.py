import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from observer.event_store import EventStore
from observer.maintenance import MaintenanceLoop
from observer.reconcile import ReconcileReport
from observer.event_store import EventRecord


class MaintenanceLoopTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.store = EventStore(root / "factory.sqlite", root / "buffer")
        self.store.initialize()

    def test_runs_immediately_then_periodically_and_stops_cleanly(self):
        called = threading.Event()
        called_twice = threading.Event()
        calls = []

        def reconcile(store, command, now, stale_after):
            calls.append((store, command, stale_after))
            called.set()
            if len(calls) >= 2:
                called_twice.set()
            return ReconcileReport(examined=2, interrupted=1)

        loop = MaintenanceLoop(
            self.store,
            ("fake-codex", "app-server", "--listen", "stdio://"),
            interval_seconds=0.02,
            stale_after=timedelta(minutes=30),
            reconcile=reconcile,
        )
        loop.start()
        self.assertTrue(called.wait(0.5))
        self.assertTrue(called_twice.wait(1.0))
        loop.stop()
        count_after_stop = len(calls)
        time.sleep(0.04)

        self.assertGreaterEqual(count_after_stop, 2)
        self.assertEqual(len(calls), count_after_stop)
        health = loop.health_snapshot()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["lastReport"]["interrupted"], 1)
        self.assertIsNotNone(health["lastReconcileAt"])

    def test_error_is_safe_and_does_not_kill_future_cycles(self):
        attempts = 0
        recovered = threading.Event()

        def reconcile(store, command, now, stale_after):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("PRIVATE_FAILURE_DETAIL")
            recovered.set()
            return ReconcileReport(examined=1, still_active=1)

        loop = MaintenanceLoop(
            self.store,
            ("fake",),
            interval_seconds=0.01,
            reconcile=reconcile,
        )
        loop.start()
        self.assertTrue(recovered.wait(0.5))
        loop.stop()

        health = loop.health_snapshot()
        self.assertEqual(health["status"], "ok")
        self.assertNotIn("PRIVATE_FAILURE_DETAIL", str(health))

    def test_app_server_start_failure_marks_stale_work_unknown(self):
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        self.store.append_event(EventRecord(
            "event", "dedupe", "demo", "thread", "turn", "turn_started",
            old, 1, {}, None, None,
        ))

        def fail(store, command, now, stale_after):
            raise OSError("unavailable")

        loop = MaintenanceLoop(
            self.store, ("missing",), interval_seconds=60, reconcile=fail,
        )
        loop.start()
        for _ in range(50):
            if loop.health_snapshot()["status"] == "degraded":
                break
            time.sleep(0.01)
        loop.stop()

        self.assertEqual(self.store.get_turn("thread", "turn")["status"], "unknown")

    def test_refreshes_project_work_after_each_reconcile_cycle(self):
        refreshed = threading.Event()
        calls = []

        def reconcile(store, command, now, stale_after):
            return ReconcileReport(examined=1)

        def refresh(now):
            calls.append(now)
            refreshed.set()

        loop = MaintenanceLoop(
            self.store, ("fake",), interval_seconds=0.01,
            reconcile=reconcile, refresh_project_work=refresh,
        )
        loop.start()
        self.assertTrue(refreshed.wait(0.5))
        loop.stop()

        self.assertGreaterEqual(len(calls), 1)
        self.assertTrue(all(value.tzinfo is not None for value in calls))

    def test_refreshes_project_work_when_reconciliation_fails(self):
        refreshed = threading.Event()

        def reconcile(store, command, now, stale_after):
            raise OSError("unavailable")

        def refresh(now):
            refreshed.set()

        loop = MaintenanceLoop(
            self.store, ("missing",), interval_seconds=60,
            reconcile=reconcile, refresh_project_work=refresh,
        )
        loop.start()
        self.assertTrue(refreshed.wait(0.5))
        loop.stop()

        health = loop.health_snapshot()
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["reconcileErrorCode"], "OSError")
        self.assertIsNone(health["projectWorkRefreshErrorCode"])


if __name__ == "__main__":
    unittest.main()
