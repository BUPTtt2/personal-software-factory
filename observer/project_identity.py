from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from observer.config import ProjectConfig
from observer.git_snapshot import _minimal_env, capture_git_snapshot, resolve_git_executable


@dataclass(frozen=True)
class ProjectIdentity:
    project_id: str
    matched_by: str
    repo_root: Path | None
    common_git_dir: Path | None


def _contains(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_remote(value: str) -> str:
    normalized = value.strip().removesuffix(".git")
    match = re.match(r"git@([^:]+):(.+)", normalized)
    if match:
        normalized = f"{match.group(1)}/{match.group(2)}"
    normalized = re.sub(r"^https?://", "", normalized)
    return normalized.rstrip("/").lower()


def _remote(git: str, cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            [git, "-C", str(cwd), "remote", "get-url", "origin"],
            shell=False, capture_output=True, text=True, timeout=0.5,
            check=False, env=_minimal_env(),
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def identify_project(
    cwd: Path,
    projects: tuple[ProjectConfig, ...],
    git_executable: str | None = None,
) -> ProjectIdentity | None:
    cwd = cwd.resolve(strict=False)
    enabled = tuple(project for project in projects if project.enabled)
    matches = [
        (root.resolve(strict=False), project) for project in enabled for root in project.roots
        if _contains(root.resolve(strict=False), cwd)
    ]
    if matches:
        root, project = max(matches, key=lambda item: len(item[0].parts))
        return ProjectIdentity(project.project_id, "root", None, None)
    git = git_executable or resolve_git_executable()
    snapshot = capture_git_snapshot(cwd, git_executable=git) if git else None
    if snapshot and snapshot.available and snapshot.common_git_dir:
        current_common = Path(snapshot.common_git_dir)
        for project in enabled:
            for root in project.roots:
                registered = capture_git_snapshot(root, git_executable=git)
                if registered.available and registered.common_git_dir and Path(registered.common_git_dir) == current_common:
                    return ProjectIdentity(project.project_id, "git_common_dir", Path(snapshot.repo_root), current_common)
        current_remote = _remote(git, cwd)
        if current_remote:
            normalized = _normalize_remote(current_remote)
            for project in enabled:
                if normalized in {_normalize_remote(item) for item in project.git_remotes}:
                    return ProjectIdentity(project.project_id, "git_remote", Path(snapshot.repo_root), current_common)
    return None
