from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class AcpxResult:
    returncode: int
    stdout: str
    stderr: str
    requested_argv: list[str]
    invoked_argv: list[str]
    cwd: str | None
    started_at: str
    completed_at: str
    agent: str
    session_name: str | None = None
    execution_kind: str = "prompt"
    approval_mode: str = "approve-all"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def require_acpx_on_path() -> str:
    acpx = shutil.which("acpx")
    if not acpx:
        raise FileNotFoundError("acpx not found on PATH")
    return acpx


def _acpx_argv(base: list[str]) -> list[str]:
    """
    On Windows, `acpx` is commonly installed as `acpx.cmd`.
    `subprocess` cannot execute `.cmd` directly without going through `cmd.exe /c`.
    """

    if os.name != "nt":
        return base

    acpx = require_acpx_on_path()
    if acpx.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", acpx, *base[1:]]
    return base


def _run_capture(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _acpx_argv(cmd),
        cwd=str(cwd) if cwd else None,
        input=stdin_text,
        text=True,
        capture_output=True,
        check=False,
    )


def _emit_capture(proc: subprocess.CompletedProcess[str]) -> None:
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        if not proc.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        if not proc.stderr.endswith("\n"):
            sys.stderr.write("\n")


def _run_acpx_capture(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    stdin_text: str | None = None,
    emit_output: bool = True,
    agent: str,
    session_name: str | None,
    execution_kind: str,
    approval_mode: str,
) -> AcpxResult:
    requested_argv = list(cmd)
    invoked_argv = _acpx_argv(cmd)
    started_at = _utc_now_iso()
    proc = _run_capture(cmd, cwd=cwd, stdin_text=stdin_text)
    completed_at = _utc_now_iso()
    if emit_output:
        _emit_capture(proc)
    return AcpxResult(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        requested_argv=requested_argv,
        invoked_argv=invoked_argv,
        cwd=str(cwd) if cwd else None,
        started_at=started_at,
        completed_at=completed_at,
        agent=agent,
        session_name=session_name,
        execution_kind=execution_kind,
        approval_mode=approval_mode,
    )


def get_session_status(*, agent: str, cwd: Path, session_name: str) -> dict | None:
    """
    Best-effort structured status. If acpx can't provide JSON for any reason, return None.
    """

    proc = _run_capture(["acpx", "--cwd", str(cwd), "--format", "json", agent, "status", "-s", session_name], cwd=None)
    if proc.returncode != 0:
        return None
    try:
        return json.loads((proc.stdout or "").strip() or "{}")
    except Exception:
        return None


def _ensure_session_alive(*, agent: str, cwd: Path, session_name: str) -> None:
    """
    acpx can keep a session record around even if the queue-owner process died.
    When that happens, a first prompt can fail with "needs reconnect".
    This function nudges the session into a runnable state (best-effort).
    """

    snap = get_session_status(agent=agent, cwd=cwd, session_name=session_name)
    if not isinstance(snap, dict):
        return

    status = str(snap.get("status") or "").strip().lower()
    if status == "no-session":
        ensure_named_session(agent=agent, cwd=cwd, session_name=session_name)
        return
    if status == "dead":
        _run_capture(["acpx", "--cwd", str(cwd), agent, "sessions", "close", session_name], cwd=None)
        ensure_named_session(agent=agent, cwd=cwd, session_name=session_name)
        return


def ensure_named_session(*, agent: str, cwd: Path, session_name: str) -> None:
    require_acpx_on_path()
    ensured = _run_capture(["acpx", "--cwd", str(cwd), agent, "sessions", "ensure", "--name", session_name], cwd=None)
    if ensured.returncode != 0:
        raise RuntimeError(
            f"Failed to ensure acpx session '{session_name}' for agent '{agent}' (exit {ensured.returncode})"
        )


def run_agent_prompt(
    *,
    agent: str,
    cwd: Path,
    session_name: str,
    prompt_text: str,
    approve_all: bool = True,
    emit_output: bool = True,
) -> AcpxResult:
    require_acpx_on_path()
    ensure_named_session(agent=agent, cwd=cwd, session_name=session_name)
    _ensure_session_alive(agent=agent, cwd=cwd, session_name=session_name)

    cmd: list[str] = ["acpx", "--cwd", str(cwd)]
    approval_mode = "approve-all" if approve_all else "default"
    if approve_all:
        cmd.append("--approve-all")
    cmd += [agent, "-s", session_name, "--file", "-"]

    result = _run_acpx_capture(
        cmd,
        cwd=None,
        stdin_text=prompt_text,
        emit_output=emit_output,
        agent=agent,
        session_name=session_name,
        execution_kind="prompt",
        approval_mode=approval_mode,
    )
    if result.returncode == 0:
        return result

    snap = get_session_status(agent=agent, cwd=cwd, session_name=session_name)
    status = str((snap or {}).get("status") or "").strip().lower()
    summary = str((snap or {}).get("summary") or "").strip().lower()
    if status in ("dead", "no-session") or "needs reconnect" in summary or "queue owner unavailable" in summary:
        time.sleep(0.5)
        _ensure_session_alive(agent=agent, cwd=cwd, session_name=session_name)
        return _run_acpx_capture(
            cmd,
            cwd=None,
            stdin_text=prompt_text,
            emit_output=emit_output,
            agent=agent,
            session_name=session_name,
            execution_kind="prompt",
            approval_mode=approval_mode,
        )

    return result


def run_agent_exec(
    *,
    agent: str,
    cwd: Path,
    prompt_text: str,
    approve_all: bool = True,
    emit_output: bool = True,
) -> AcpxResult:
    require_acpx_on_path()
    cmd: list[str] = ["acpx", "--cwd", str(cwd)]
    approval_mode = "approve-all" if approve_all else "default"
    if approve_all:
        cmd.append("--approve-all")
    cmd += [agent, "exec", "--file", "-"]
    return _run_acpx_capture(
        cmd,
        cwd=None,
        stdin_text=prompt_text,
        emit_output=emit_output,
        agent=agent,
        session_name=None,
        execution_kind="exec",
        approval_mode=approval_mode,
    )
