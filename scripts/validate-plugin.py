#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve(strict=False)
    try:
        manifest = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", manifest["name"]):
            raise ValueError("invalid plugin name")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", manifest["version"]):
            raise ValueError("invalid semantic version")
        for field in ("description", "author", "interface"):
            if not manifest.get(field):
                raise ValueError(f"missing {field}")
        if "hooks" in manifest:
            raise ValueError("unsupported manifest field: hooks")
        for field in ("skills", "mcpServers"):
            value = manifest.get(field)
            if not isinstance(value, str) or not value.startswith("./") or not (root / value).exists():
                raise ValueError(f"invalid {field} path")
        mcp = json.loads((root / manifest["mcpServers"]).read_text(encoding="utf-8"))
        if not isinstance(mcp.get("mcpServers"), dict) or not mcp["mcpServers"]:
            raise ValueError("MCP server is required")
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, separators=(",", ":")))
        return 1
    print(json.dumps({"status": "passed", "plugin": manifest["name"], "version": manifest["version"]}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
