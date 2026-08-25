from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from observer.config import load_projects
from observer.event_store import EventRecord, EventStore, VerificationRecord
from observer.git_snapshot import capture_git_snapshot, resolve_git_executable
from observer.project_identity import identify_project
from observer.verification import classify_command


class VerificationRejected(ValueError):
    pass


@dataclass(frozen=True)
class VerificationResult:
    event_id: str
    project_id: str
    kind: str
    command_class: str
    exit_code: int
    status: str


class VerificationRunner:
    def __init__(
        self,
        root: Path,
        executor: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        registry_path: Path | None = None,
    ):
        self.root = root.resolve(strict=False)
        legacy_registry = self.root / "config/projects.yaml"
        self.registry_path = (registry_path or (
            legacy_registry if legacy_registry.exists() else self.root / "config/projects.json"
        )).resolve(strict=False)
        self.store = EventStore(self.root / "data/factory.sqlite", self.root / "buffer")
        self.git_executable = resolve_git_executable()
        self.executor = executor

    def run(self, cwd: Path, command: list[str]) -> VerificationResult:
        resolved_cwd = cwd.resolve(strict=False)
        classification = classify_command(command)
        if not classification:
            raise VerificationRejected("command_not_allowlisted")
        trusted_command = _trusted_command(command)
        identity = identify_project(
            resolved_cwd,
            load_projects(self.registry_path),
            git_executable=self.git_executable,
        )
        if identity is None:
            raise VerificationRejected("project_not_registered")

        completed = self.executor(trusted_command, cwd=resolved_cwd, shell=False, check=False)
        occurred_at = datetime.now(timezone.utc)
        status = "passed" if completed.returncode == 0 else "failed"
        kind, command_class = classification
        random_id = str(uuid.uuid4())
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"observer-verification:{random_id}"))
        dedupe = hashlib.sha256(f"verification|{random_id}".encode("utf-8")).hexdigest()
        record = EventRecord(
            event_id=event_id,
            deduplication_key=dedupe,
            project_id=identity.project_id,
            thread_id=f"observer-verifier:{identity.project_id}",
            turn_id=None,
            event_type="verification_receipt",
            occurred_at=occurred_at,
            payload_version=1,
            redacted_payload={
                "kind": kind,
                "commandClass": command_class,
                "status": status,
            },
            git_snapshot=capture_git_snapshot(
                resolved_cwd,
                git_executable=self.git_executable,
            ),
            verification=VerificationRecord(
                kind,
                command_class,
                completed.returncode,
                status,
            ),
        )
        self.store.initialize()
        self.store.append_event(record)
        return VerificationResult(
            event_id,
            identity.project_id,
            kind,
            command_class,
            completed.returncode,
            status,
        )


def _trusted_command(command: list[str]) -> list[str]:
    raw = Path(command[0])
    name = raw.name
    search_dirs = tuple(dict.fromkeys((
        str(Path(sys.executable).resolve().parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    )))
    found = shutil.which(name, path=os.pathsep.join(search_dirs))
    if not found:
        raise VerificationRejected("verification_executable_unavailable")
    trusted = Path(found).resolve(strict=True)
    if raw.parent != Path(".") or "/" in command[0]:
        supplied = raw.expanduser().resolve(strict=False)
        if supplied != trusted:
            raise VerificationRejected("verification_executable_untrusted")
    return [str(trusted), *command[1:]]
