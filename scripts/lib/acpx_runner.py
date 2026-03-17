from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
import json
import time


@dataclass(frozen=True)
class AcpxResult:
    returncode: int


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


def _run(cmd: list[str], *, cwd: Path | None = None, stdin_text: str | None = None, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _acpx_argv(cmd),
        cwd=str(cwd) if cwd else None,
        input=stdin_text,
        text=True,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
        check=False,
    )

def _run_capture(cmd: list[str], *, cwd: Path | None = None, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _acpx_argv(cmd),
        cwd=str(cwd) if cwd else None,
        input=stdin_text,
        text=True,
        capture_output=True,
        check=False,
    )


def _acpx_status_json(*, agent: str, cwd: Path, session_name: str) -> dict | None:
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

    snap = _acpx_status_json(agent=agent, cwd=cwd, session_name=session_name)
    if not isinstance(snap, dict):
        return

    status = str(snap.get("status") or "").strip().lower()
    if status == "no-session":
        ensure_named_session(agent=agent, cwd=cwd, session_name=session_name)
        return
    if status == "dead":
        # Try to close+recreate the named session scope.
        _run(["acpx", "--cwd", str(cwd), agent, "sessions", "close", session_name], quiet=True)
        ensure_named_session(agent=agent, cwd=cwd, session_name=session_name)
        return


def ensure_named_session(*, agent: str, cwd: Path, session_name: str) -> None:
    require_acpx_on_path()
    # `sessions ensure` is more robust than `sessions new` with some agents/cwds.
    ensured = _run(["acpx", "--cwd", str(cwd), agent, "sessions", "ensure", "--name", session_name], quiet=False)
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
) -> AcpxResult:
    require_acpx_on_path()
    ensure_named_session(agent=agent, cwd=cwd, session_name=session_name)
    _ensure_session_alive(agent=agent, cwd=cwd, session_name=session_name)

    cmd: list[str] = ["acpx", "--cwd", str(cwd)]
    if approve_all:
        cmd.append("--approve-all")
    cmd += [agent, "-s", session_name, "--file", "-"]

    proc = _run(cmd, stdin_text=prompt_text, quiet=False)
    if proc.returncode == 0:
        return AcpxResult(returncode=0)

    # One retry for transient "needs reconnect"/dead-owner conditions.
    snap = _acpx_status_json(agent=agent, cwd=cwd, session_name=session_name)
    status = str((snap or {}).get("status") or "").strip().lower()
    summary = str((snap or {}).get("summary") or "").strip().lower()
    if status in ("dead", "no-session") or "needs reconnect" in summary or "queue owner unavailable" in summary:
        time.sleep(0.5)
        _ensure_session_alive(agent=agent, cwd=cwd, session_name=session_name)
        proc2 = _run(cmd, stdin_text=prompt_text, quiet=False)
        return AcpxResult(returncode=proc2.returncode)

    return AcpxResult(returncode=proc.returncode)
