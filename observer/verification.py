from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VerificationObservation:
    kind: str
    command_class: str
    exit_code: int | None
    status: str


_CONTROL_OPERATORS = re.compile(r"(?:&&|\|\||[;|\n\r])")


def _exit_code(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("exit_code", "exitCode"):
            item = value.get(key)
            if isinstance(item, int) and not isinstance(item, bool):
                return item
        for key in ("result", "response"):
            if key in value:
                found = _exit_code(value[key])
                if found is not None:
                    return found
    return None


def classify_command(parts: list[str]) -> tuple[str, str] | None:
    if not parts:
        return None
    executable = parts[0].rsplit("/", 1)[-1]
    if executable.startswith("python") and len(parts) >= 3 and parts[1:3] == ["-m", "unittest"]:
        return "test", "python_unittest"
    if executable == "pytest":
        return "test", "pytest"
    if executable in {"npm", "pnpm", "yarn"}:
        if len(parts) >= 2 and parts[1] == "test":
            return "test", f"{executable}_test"
        if len(parts) >= 3 and parts[1] == "run" and parts[2] in {"test", "lint", "type-check", "check", "build"}:
            kind = "test" if parts[2] == "test" else "check"
            return kind, f"{executable}_{parts[2].replace('-', '_')}"
    if executable in {"mvn", "mvnw"} and parts[1:] == ["test"]:
        return "test", "maven_test"
    if executable in {"gradle", "gradlew"} and parts[1:] == ["test"]:
        return "test", "gradle_test"
    if executable == "go" and len(parts) >= 2 and parts[1] == "test":
        return "test", "go_test"
    if executable == "cargo" and len(parts) >= 2 and parts[1] == "test":
        return "test", "cargo_test"
    return None


def classify_verification(tool_name: str, tool_input: Any, tool_response: Any) -> VerificationObservation | None:
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or _CONTROL_OPERATORS.search(command):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    classification = classify_command(parts)
    if not classification:
        return None
    exit_code = _exit_code(tool_response)
    status = "unknown" if exit_code is None else ("passed" if exit_code == 0 else "failed")
    return VerificationObservation(classification[0], classification[1], exit_code, status)
