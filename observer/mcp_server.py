from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TextIO

from observer.mcp_tools import FactoryServiceClient, call_tool, list_tool_definitions
from observer.paths import resolve_runtime_paths


_SERVER_NAME = "personal-software-factory"
_SERVER_VERSION = "0.1.0"
_PROTOCOL_VERSION = "2025-03-26"
_MAX_MESSAGE_CHARS = 1_048_576


def serve_stdio(stdin: TextIO, stdout: TextIO, client: FactoryServiceClient) -> int:
    for raw_line in stdin:
        if len(raw_line) > _MAX_MESSAGE_CHARS:
            _write(stdout, _error(None, -32600, "Request too large"))
            continue
        try:
            request = json.loads(raw_line)
        except json.JSONDecodeError:
            _write(stdout, _error(None, -32700, "Parse error"))
            continue
        if not isinstance(request, dict):
            _write(stdout, _error(None, -32600, "Invalid Request"))
            continue
        request_id = request.get("id")
        is_notification = "id" not in request
        method = request.get("method")
        params = request.get("params", {})
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
            if not is_notification:
                _write(stdout, _error(request_id, -32600, "Invalid Request"))
            continue
        if is_notification:
            continue
        if method == "initialize":
            if not isinstance(params, dict):
                _write(stdout, _error(request_id, -32602, "Invalid params"))
                continue
            _write(stdout, _result(request_id, {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
            }))
            continue
        if method == "ping":
            _write(stdout, _result(request_id, {}))
            continue
        if method == "tools/list":
            if not isinstance(params, dict):
                _write(stdout, _error(request_id, -32602, "Invalid params"))
                continue
            _write(stdout, _result(request_id, {"tools": list(list_tool_definitions())}))
            continue
        if method == "tools/call":
            if not isinstance(params, dict):
                _write(stdout, _error(request_id, -32602, "Invalid params"))
                continue
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                _write(stdout, _error(request_id, -32602, "Invalid params"))
                continue
            _write(stdout, _result(request_id, call_tool(name, arguments, client).to_mcp_result()))
            continue
        _write(stdout, _error(request_id, -32601, "Method not found"))
    return 0


def main() -> int:
    registry_value = os.environ.get("SOFTWARE_FACTORY_REGISTRY")
    paths = resolve_runtime_paths(
        registry_path=Path(registry_value) if registry_value else None,
    )
    client = FactoryServiceClient(
        os.environ.get("SOFTWARE_FACTORY_URL", "http://127.0.0.1:8765"),
        paths.registry_path,
    )
    return serve_stdio(sys.stdin, sys.stdout, client)


def _write(stdout: TextIO, value: dict[str, object]) -> None:
    stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def _result(request_id: object, value: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


if __name__ == "__main__":
    raise SystemExit(main())
