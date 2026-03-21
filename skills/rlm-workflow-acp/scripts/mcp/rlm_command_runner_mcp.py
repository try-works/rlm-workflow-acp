#!/usr/bin/env python3
"""
Minimal MCP (Model Context Protocol) stdio server providing a single tool to run
verification commands with argv splitting.

Why this exists:
- `kimi acp` currently issues `terminal/create` with a single combined command
  string (e.g. "python -m unittest ...") and no args array, which causes
  `acpx` to `spawn()` a non-existent executable on Windows (ENOENT).
- This MCP server provides a reliable, argv-based command runner that the Kimi
  worker can call to execute verification inside the delegated ACP session.

This is intentionally small and self-contained (no third-party deps).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "rlm-command-runner-mcp", "version": "0.1.0"}


class _FramedIO:
    """
    Supports both:
    - LSP-style `Content-Length:` framing
    - newline-delimited JSON framing

    Some MCP clients (including some CLI integrations) use JSON-lines framing.
    """

    mode: str | None = None  # "content-length" | "jsonl"

    def read_message(self) -> dict[str, Any] | None:
        if self.mode is None:
            # Detect framing mode from the first non-empty line.
            while True:
                line = sys.stdin.buffer.readline()
                if not line:
                    return None
                if line in (b"\r\n", b"\n"):
                    continue
                if line.lower().startswith(b"content-length:"):
                    self.mode = "content-length"
                    return self._read_content_length(first_line=line)
                self.mode = "jsonl"
                return json.loads(line.decode("utf-8"))
        if self.mode == "content-length":
            return self._read_content_length(first_line=None)
        return self._read_jsonl()

    def _read_jsonl(self) -> dict[str, Any] | None:
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                continue
            return json.loads(line.decode("utf-8"))

    def _read_content_length(self, first_line: bytes | None) -> dict[str, Any] | None:
        headers: dict[str, str] = {}
        if first_line:
            k, v = first_line.decode("utf-8").split(":", 1)
            headers[k.strip().lower()] = v.strip()
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            if b":" not in line:
                continue
            k, v = line.decode("utf-8").split(":", 1)
            headers[k.strip().lower()] = v.strip()
        length_raw = headers.get("content-length")
        if not length_raw:
            return None
        length = int(length_raw)
        body = sys.stdin.buffer.read(length)
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def write_message(self, payload: dict[str, Any]) -> None:
        if self.mode == "jsonl":
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            return

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _rpc_error(*, _id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": _id, "error": err}


def _rpc_result(*, _id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": _id, "result": result}


def _tool_spec() -> dict[str, Any]:
    return {
        "name": "rlm_run_command",
        "description": "Run a verification command with argv splitting and capture stdout/stderr/exitCode. Optionally write an evidence JSON file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "timeoutSec": {"type": "number"},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
                "evidenceJsonPath": {"type": "string"},
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    }


@dataclass(frozen=True)
class RunResult:
    argv: list[str]
    cwd: str
    exit_code: int
    duration_ms: int
    stdout: str
    stderr: str
    output_sha256: str


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_command(arguments: dict[str, Any]) -> RunResult:
    argv = arguments.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise ValueError("argv must be a non-empty array of strings")

    cwd = str(arguments.get("cwd") or os.getcwd())
    timeout_sec = arguments.get("timeoutSec")
    timeout = float(timeout_sec) if timeout_sec is not None else None

    env_in = arguments.get("env") or {}
    if not isinstance(env_in, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env_in.items()):
        raise ValueError("env must be an object map of string->string when provided")

    env = os.environ.copy()
    env.update(env_in)

    t0 = time.time()
    proc = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    t1 = time.time()

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    combined = stdout + ("\n" if stdout and stderr else "") + stderr
    out_sha = _sha256_text(combined)

    return RunResult(
        argv=argv,
        cwd=cwd,
        exit_code=int(proc.returncode),
        duration_ms=int(round((t1 - t0) * 1000)),
        stdout=stdout,
        stderr=stderr,
        output_sha256=out_sha,
    )


_PSEUDO_ABS_CODEX_RE = re.compile(r"^[\\/]+\.codex[\\/]", re.IGNORECASE)


def _maybe_write_evidence(path_text: str, *, base_cwd: str, result: RunResult) -> None:
    raw = str(path_text).strip()
    if not raw:
        raise ValueError("evidenceJsonPath must be a non-empty string")

    # In this workflow, handoffs commonly describe paths like `/.codex/...` as "repo-relative".
    # On Windows, `Path('/.codex/...')` is interpreted as an absolute path on the current drive
    # (e.g. `D:\\.codex\\...`), which is not what we want. Treat `/.codex/...` as relative.
    if os.name == "nt" and _PSEUDO_ABS_CODEX_RE.match(raw):
        raw = raw.lstrip("/\\")

    p = Path(raw).expanduser()

    base = Path(base_cwd).expanduser()
    if not base.is_absolute():
        base = (Path.cwd() / base).resolve()

    if not p.is_absolute():
        p = (base / p).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "argv": result.argv,
        "cwd": result.cwd,
        "exitCode": result.exit_code,
        "durationMs": result.duration_ms,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "outputSha256": result.output_sha256,
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    io = _FramedIO()
    while True:
        msg = io.read_message()
        if msg is None:
            return 0

        _id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                }
                io.write_message(_rpc_result(_id=_id, result=result))
                continue

            if method == "tools/list":
                io.write_message(_rpc_result(_id=_id, result={"tools": [_tool_spec()]}))
                continue

            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if name != "rlm_run_command":
                    raise ValueError(f"Unknown tool: {name!r}")
                if not isinstance(arguments, dict):
                    raise ValueError("tools/call arguments must be an object")

                res = _run_command(arguments)
                evidence_path = arguments.get("evidenceJsonPath")
                if evidence_path:
                    _maybe_write_evidence(str(evidence_path), base_cwd=res.cwd, result=res)

                text = (
                    f"exitCode={res.exit_code}\n"
                    f"durationMs={res.duration_ms}\n"
                    f"outputSha256={res.output_sha256}\n"
                    "--- STDOUT ---\n"
                    f"{res.stdout.rstrip()}\n"
                    "--- STDERR ---\n"
                    f"{res.stderr.rstrip()}\n"
                )
                result = {"content": [{"type": "text", "text": text}]}
                io.write_message(_rpc_result(_id=_id, result=result))
                continue

            if method == "shutdown":
                io.write_message(_rpc_result(_id=_id, result={}))
                return 0

            # Notifications we can safely ignore.
            if _id is None:
                continue

            io.write_message(_rpc_error(_id=_id, code=-32601, message=f"Method not found: {method}"))
        except Exception as e:
            if _id is None:
                continue
            io.write_message(_rpc_error(_id=_id, code=-32603, message="Internal error", data={"details": str(e)}))


if __name__ == "__main__":
    raise SystemExit(main())
