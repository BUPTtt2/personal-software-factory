from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    code_root: Path
    state_root: Path
    registry_path: Path
    db_path: Path
    buffer_dir: Path


def resolve_runtime_paths(
    code_root: Path | None = None,
    state_root: Path | None = None,
    registry_path: Path | None = None,
) -> RuntimePaths:
    code = (code_root or Path(__file__).resolve().parents[1]).expanduser().absolute()
    state = (state_root or _configured_state_root()).expanduser().absolute()
    registry = (registry_path or state / "config/projects.json").expanduser().absolute()
    return RuntimePaths(
        code_root=code,
        state_root=state,
        registry_path=registry,
        db_path=state / "data/factory.sqlite",
        buffer_dir=state / "buffer",
    )


def _configured_state_root() -> Path:
    configured = os.environ.get("SOFTWARE_FACTORY_HOME")
    if configured:
        return Path(configured)
    home = Path.home()
    if platform.system() == "Darwin":
        return home / "Library/Application Support/PersonalSoftwareFactory"
    if platform.system() == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "PersonalSoftwareFactory"
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "personal-software-factory"
