from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitSnapshot:
    repo_root: str | None
    common_git_dir: str | None
    branch: str | None
    head_sha: str | None
    dirty: bool | None
    changed_paths: tuple[str, ...]
    available: bool
    error_code: str | None


def resolve_git_executable() -> str | None:
    candidates = [
        os.environ.get("CODEX_OBSERVER_GIT"),
        shutil.which("git"),
        "/opt/homebrew/bin/git",
        "/usr/local/bin/git",
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            result = subprocess.run(
                [candidate, "--version"], capture_output=True, text=True,
                timeout=0.3, check=False, env=_minimal_env(),
            )
            if result.returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _minimal_env() -> dict[str, str]:
    return {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}


def _run(git: str, cwd: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [git, "-C", str(cwd), *args], shell=False, capture_output=True,
        text=True, timeout=timeout, check=False, env=_minimal_env(),
    )


def _absolute_git_path(raw: str, repo_root: Path) -> Path:
    path = Path(raw)
    return (path if path.is_absolute() else repo_root / path).resolve(strict=False)


def _parse_changed_paths(raw: str) -> tuple[str, ...]:
    fields = raw.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4:
            raise ValueError("invalid porcelain entry")
        status = entry[:2]
        paths.add(entry[3:])
        if "R" in status or "C" in status:
            if index < len(fields) and fields[index]:
                paths.add(fields[index])
                index += 1
    return tuple(sorted(paths)[:500])


def capture_git_snapshot(
    cwd: Path,
    timeout_seconds: float = 0.75,
    git_executable: str | None = None,
) -> GitSnapshot:
    git = git_executable or resolve_git_executable()
    if not git:
        return GitSnapshot(None, None, None, None, None, (), False, "git_unavailable")
    try:
        root_result = _run(git, cwd, ["rev-parse", "--show-toplevel"], timeout_seconds)
        if root_result.returncode != 0:
            return GitSnapshot(None, None, None, None, None, (), False, "not_git")
        repo_root = Path(root_result.stdout.strip()).resolve(strict=False)
        common = _run(git, cwd, ["rev-parse", "--git-common-dir"], timeout_seconds)
        head = _run(git, cwd, ["rev-parse", "HEAD"], timeout_seconds)
        branch = _run(git, cwd, ["branch", "--show-current"], timeout_seconds)
        status = _run(git, cwd, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"], timeout_seconds)
        if any(item.returncode != 0 for item in (common, head, branch, status)):
            return GitSnapshot(str(repo_root), None, None, None, None, (), False, "parse_error")
        changed_paths = _parse_changed_paths(status.stdout)
        return GitSnapshot(
            repo_root=str(repo_root),
            common_git_dir=str(_absolute_git_path(common.stdout.strip(), repo_root)),
            branch=branch.stdout.strip() or None,
            head_sha=head.stdout.strip() or None,
            dirty=bool(changed_paths),
            changed_paths=changed_paths,
            available=True,
            error_code=None,
        )
    except subprocess.TimeoutExpired:
        return GitSnapshot(None, None, None, None, None, (), False, "timeout")
    except OSError:
        return GitSnapshot(None, None, None, None, None, (), False, "git_unavailable")
    except (ValueError, UnicodeError):
        return GitSnapshot(None, None, None, None, None, (), False, "parse_error")
