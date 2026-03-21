from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .acpx_runner import AcpxResult


ALLOWED_DELEGATION_MODES = {"implement", "review", "repair"}
ALLOWED_SESSION_POLICIES = {"auto", "persistent", "exec"}


def resolve_session_policy(*, mode: str, session_policy: str, multi_turn_required: bool = False) -> str:
    mode_norm = mode.strip().lower()
    policy_norm = session_policy.strip().lower()

    if mode_norm not in ALLOWED_DELEGATION_MODES:
        raise ValueError(f"Unsupported delegation mode: {mode}")
    if policy_norm not in ALLOWED_SESSION_POLICIES:
        raise ValueError(f"Unsupported session policy: {session_policy}")

    if policy_norm in {"persistent", "exec"}:
        return policy_norm

    if mode_norm in {"review", "repair"}:
        return "exec"
    return "persistent" if multi_turn_required else "exec"


def write_acp_transcript(
    *,
    run_dir: Path,
    attempt: int,
    mode: str,
    session_policy: str,
    prompt_text: str,
    result: AcpxResult,
    session_status_before: dict[str, Any] | None,
    session_status_after: dict[str, Any] | None,
    extra_metadata: dict[str, Any] | None = None,
) -> Path:
    bundle_dir = (run_dir / "evidence" / "acp" / f"attempt-{attempt:03d}").resolve()
    bundle_dir.mkdir(parents=True, exist_ok=True)

    (bundle_dir / "prompt.txt").write_text(prompt_text, encoding="utf-8", newline="\n")
    (bundle_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8", newline="\n")
    (bundle_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8", newline="\n")

    metadata = {
        "mode": mode,
        "sessionPolicy": session_policy,
        "attempt": attempt,
        "result": result.to_dict(),
        "sessionStatusBefore": session_status_before,
        "sessionStatusAfter": session_status_after,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    (bundle_dir / "invocation.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return bundle_dir
