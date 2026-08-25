from __future__ import annotations

import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Callable

from observer.event_store import EventStore
from observer.reconcile import (
    AppServerClient,
    ReconcileReport,
    mark_stale_turns_unknown,
    reconcile_stale_turns,
)


ReconcileFunction = Callable[
    [EventStore, tuple[str, ...], datetime, timedelta],
    ReconcileReport,
]
ProjectWorkRefreshFunction = Callable[[datetime], None]


def _run_reconcile(
    store: EventStore,
    command: tuple[str, ...],
    now: datetime,
    stale_after: timedelta,
) -> ReconcileReport:
    with AppServerClient(command) as client:
        return reconcile_stale_turns(store, client, now, stale_after)


class MaintenanceLoop:
    def __init__(
        self,
        store: EventStore,
        app_server_command: tuple[str, ...],
        interval_seconds: float = 30.0,
        stale_after: timedelta = timedelta(minutes=30),
        reconcile: ReconcileFunction = _run_reconcile,
        refresh_project_work: ProjectWorkRefreshFunction | None = None,
    ):
        self.store = store
        self.app_server_command = app_server_command
        self.interval_seconds = interval_seconds
        self.stale_after = stale_after
        self.reconcile = reconcile
        self.refresh_project_work = refresh_project_work
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._last_reconcile_at: str | None = None
        self._last_report = ReconcileReport()
        self._status = "starting"
        self._error_code: str | None = None
        self._reconcile_error_code: str | None = None
        self._last_project_work_refresh_at: str | None = None
        self._project_work_refresh_error_code: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="observer-maintenance",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1.0, min(self.interval_seconds + 0.5, 5.0)))
            self._thread = None

    def health_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "status": self._status,
                "serviceStartedAt": self._started_at,
                "lastReconcileAt": self._last_reconcile_at,
                "lastReport": asdict(self._last_report),
                "errorCode": self._error_code,
                "reconcileErrorCode": self._reconcile_error_code,
                "lastProjectWorkRefreshAt": self._last_project_work_refresh_at,
                "projectWorkRefreshErrorCode": self._project_work_refresh_error_code,
            }

    def record_project_work_refresh_failure(self, now: datetime, error: Exception) -> None:
        with self._lock:
            self._last_project_work_refresh_at = now.isoformat()
            self._project_work_refresh_error_code = type(error).__name__
            self._update_status_locked()

    def _update_status_locked(self) -> None:
        self._status = (
            "degraded"
            if self._reconcile_error_code or self._project_work_refresh_error_code
            else "ok"
        )
        self._error_code = self._reconcile_error_code or self._project_work_refresh_error_code

    def _run(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            try:
                report = self.reconcile(
                    self.store,
                    self.app_server_command,
                    now,
                    self.stale_after,
                )
                with self._lock:
                    self._last_report = report
                    self._last_reconcile_at = now.isoformat()
                    self._reconcile_error_code = None
                    self._update_status_locked()
            except Exception as exc:
                try:
                    mark_stale_turns_unknown(self.store, now, self.stale_after)
                except Exception:
                    pass
                with self._lock:
                    self._last_reconcile_at = now.isoformat()
                    self._reconcile_error_code = type(exc).__name__
                    self._update_status_locked()
            if self.refresh_project_work:
                try:
                    self.refresh_project_work(now)
                    with self._lock:
                        self._last_project_work_refresh_at = now.isoformat()
                        self._project_work_refresh_error_code = None
                        self._update_status_locked()
                except Exception as exc:
                    self.record_project_work_refresh_failure(now, exc)
            if self._stop.wait(self.interval_seconds):
                break
