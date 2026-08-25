#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    directory = Path(argv[1] if len(argv) > 1 else ".")
    path = directory / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 1
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match or "name: software-factory" not in match.group(1) or "description: Use when" not in match.group(1):
        return 1
    if "[TODO:" in text:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
