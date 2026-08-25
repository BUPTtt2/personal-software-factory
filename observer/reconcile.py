from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from observer.event_store import EventStore


class AppServerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ThreadSummary:
    thread_id: str
    cwd: str | None
    runtime_status: str


@dataclass(frozen=True)
class ReconcileReport:
    examined: int = 0
    interrupted: int = 0
    still_active: int = 0
    unknown: int = 0
    errors: int = 0


class AppServerClient:
    def __init__(self, command: tuple[str, ...], request_timeout: float = 5.0):
        self.command = command
        self.request_timeout = request_timeout
        self.process: subprocess.Popen[str] | None = None
        self.next_id = 1
        self.initialized = False
        self.messages: queue.Queue[str | None] = queue.Queue()
        self.reader: threading.Thread | None = None

    def __enter__(self):
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        assert self.process.stdout is not None
        self.reader = threading.Thread(target=self._read_stdout, args=(self.process.stdout,), daemon=True)
        self.reader.start()
        return self

    def _read_stdout(self, stream) -> None:
        try:
            for line in stream:
                self.messages.put(line)
        finally:
            self.messages.put(None)

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=0.5)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
        if self.reader:
            self.reader.join(timeout=0.5)
            self.reader = None
        self.process = None

    def _ensure_process(self) -> subprocess.Popen[str]:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise AppServerError("app server is not running")
        if self.process.poll() is not None:
            raise AppServerError("app server exited")
        return self.process

    def _send(self, message: dict[str, Any]) -> None:
        process = self._ensure_process()
        assert process.stdin is not None
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self.request_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError(f"timeout waiting for {method}")
            try:
                line = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise AppServerError(f"timeout waiting for {method}")
            if line is None:
                raise AppServerError("app server closed stdout")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AppServerError("app server returned invalid JSON") from exc
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise AppServerError(str(message["error"].get("message", "app server error")))
            result = message.get("result")
            if not isinstance(result, dict):
                raise AppServerError("app server returned invalid result")
            return result

    def initialize(self) -> None:
        if self.initialized:
            return
        self._request("initialize", {"clientInfo": {
            "name": "personal_software_factory_observer",
            "title": "Personal Software Factory Observer",
            "version": "0.1.0",
        }})
        self._send({"method": "initialized", "params": {}})
        self.initialized = True

    def thread_list(self, cwds: tuple[str, ...]) -> tuple[ThreadSummary, ...]:
        self.initialize()
        result = self._request("thread/list", {
            "limit": 100, "sortKey": "updated_at", "sortDirection": "desc",
            "cwd": list(cwds), "useStateDbOnly": True,
        })
        return tuple(_thread_summary(item) for item in result.get("data", []) if isinstance(item, dict))

    def thread_read(self, thread_id: str) -> ThreadSummary:
        self.initialize()
        result = self._request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AppServerError("thread/read omitted thread")
        return _thread_summary(thread)


def _thread_summary(value: dict[str, Any]) -> ThreadSummary:
    status = value.get("status")
    runtime = status.get("type", "unknown") if isinstance(status, dict) else "unknown"
    return ThreadSummary(str(value.get("id", "")), value.get("cwd") if isinstance(value.get("cwd"), str) else None, runtime)


def reconcile_stale_turns(
    store: EventStore,
    client: AppServerClient,
    now: datetime,
    stale_after: timedelta,
) -> ReconcileReport:
    with closing(store.connect()) as connection:
        connection.row_factory = __import__("sqlite3").Row
        rows = list(connection.execute(
            "SELECT thread_id,turn_id,started_at,status FROM turns WHERE stopped_at IS NULL AND status IN ('working','unknown')"
        ))
    examined = interrupted = still_active = unknown = errors = 0
    for row in rows:
        started = datetime.fromisoformat(row["started_at"])
        if now - started < stale_after:
            continue
        examined += 1
        try:
            runtime = client.thread_read(row["thread_id"]).runtime_status
            if runtime == "active":
                still_active += 1
                new_status = "working"
            elif runtime in {"idle", "notLoaded", "systemError"}:
                interrupted += 1
                new_status = "interrupted"
            else:
                unknown += 1
                new_status = "unknown"
        except AppServerError:
            unknown += 1
            errors += 1
            new_status = "unknown"
        with closing(store.connect()) as connection:
            with connection:
                connection.execute(
                    "UPDATE turns SET status=? WHERE thread_id=? AND turn_id=?",
                    (new_status, row["thread_id"], row["turn_id"]),
                )
                connection.execute(
                    "UPDATE threads SET status=? WHERE thread_id=?",
                    (new_status, row["thread_id"]),
                )
    return ReconcileReport(examined, interrupted, still_active, unknown, errors)


def mark_stale_turns_unknown(
    store: EventStore,
    now: datetime,
    stale_after: timedelta,
) -> int:
    with closing(store.connect()) as connection:
        connection.row_factory = __import__("sqlite3").Row
        rows = list(connection.execute(
            "SELECT thread_id,turn_id,started_at FROM turns "
            "WHERE stopped_at IS NULL AND status IN ('working','unknown')"
        ))
        stale = []
        for row in rows:
            if not row["started_at"]:
                continue
            started = datetime.fromisoformat(row["started_at"])
            if now - started >= stale_after:
                stale.append((row["thread_id"], row["turn_id"]))
        with connection:
            for thread_id, turn_id in stale:
                connection.execute(
                    "UPDATE turns SET status='unknown' WHERE thread_id=? AND turn_id=?",
                    (thread_id, turn_id),
                )
                connection.execute(
                    "UPDATE threads SET status='unknown' WHERE thread_id=?",
                    (thread_id,),
                )
    return len(stale)
