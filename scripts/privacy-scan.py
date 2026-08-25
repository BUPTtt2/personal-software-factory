#!/usr/bin/env python3
from __future__ import annotations

import fnmatch
import json
import re
import sys
from pathlib import Path


SKIP_PARTS = {".git", ".worktrees", "__pycache__", ".pytest_cache", ".venv"}
RUNTIME_PATTERNS = ("*.sqlite", "*.sqlite-shm", "*.sqlite-wal", "*.mov", "*.log", "*.bak")
CONTENT_PATTERNS = (
    ("absolute_user_path", re.compile(re.escape("/" + "Users" + "/") + r"[^\s\"']+")),
    ("standalone_token", re.compile(r"\b(?:sk-(?:live|proj)-|gh[pousr]_)[A-Za-z0-9_-]{12,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve(strict=False)
    release = "--release" in argv[2:]
    ignored = _ignored_prefixes(root)
    findings: list[dict[str, str]] = []
    if release:
        for prefix in ignored:
            if (root / prefix).exists():
                findings.append({"path": prefix, "kind": "ignored_prefix_present"})
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in SKIP_PARTS for part in path.relative_to(root).parts):
            continue
        if any(relative == prefix or relative.startswith(prefix.rstrip("/") + "/") for prefix in ignored):
            continue
        for pattern in RUNTIME_PATTERNS:
            if fnmatch.fnmatch(path.name, pattern):
                findings.append({"path": relative, "kind": "runtime_file"})
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for kind, pattern in CONTENT_PATTERNS:
            if pattern.search(text):
                findings.append({"path": relative, "kind": kind})
    report = {
        "status": "failed" if findings else "passed",
        "findingCount": len(findings),
        "findings": findings,
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 1 if findings else 0


def _ignored_prefixes(root: Path) -> tuple[str, ...]:
    path = root / ".publicignore"
    if not path.exists():
        return ()
    return tuple(
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
