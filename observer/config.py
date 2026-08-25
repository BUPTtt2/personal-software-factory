from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when the project registry is unsafe or malformed."""


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    display_name: str
    roots: tuple[Path, ...]
    git_remotes: tuple[str, ...]
    enabled: bool


def load_projects(path: Path) -> tuple[ProjectConfig, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid project registry: {path}") from exc
    entries = payload.get("projects")
    if not isinstance(entries, list):
        raise ConfigError("projects must be a list")
    projects: list[ProjectConfig] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError("each project must be an object")
        project_id = entry.get("id")
        display_name = entry.get("displayName")
        roots_value = entry.get("roots")
        remotes_value = entry.get("gitRemotes", [])
        enabled = entry.get("enabled", True)
        if not isinstance(project_id, str) or not project_id.strip():
            raise ConfigError("project id must be a non-empty string")
        if project_id in seen:
            raise ConfigError(f"duplicate project id: {project_id}")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ConfigError(f"displayName is required for {project_id}")
        if not isinstance(roots_value, list) or not roots_value:
            raise ConfigError(f"roots must be a non-empty list for {project_id}")
        roots: list[Path] = []
        for raw_root in roots_value:
            if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
                raise ConfigError(f"root must be absolute for {project_id}")
            roots.append(Path(raw_root).resolve(strict=False))
        if not isinstance(remotes_value, list) or not all(isinstance(item, str) for item in remotes_value):
            raise ConfigError(f"gitRemotes must be a string list for {project_id}")
        if not isinstance(enabled, bool):
            raise ConfigError(f"enabled must be boolean for {project_id}")
        seen.add(project_id)
        projects.append(ProjectConfig(
            project_id=project_id,
            display_name=display_name,
            roots=tuple(roots),
            git_remotes=tuple(remotes_value),
            enabled=enabled,
        ))
    return tuple(projects)
