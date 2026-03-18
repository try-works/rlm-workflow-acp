#!/usr/bin/env python3
"""
Delegate bounded implementation and/or testing work to Kimi via ACP using `acpx`, driven by a sealed `02.5-acp-handoff.lock.md`.

ACP is control/invocation only. Durable state lives in:
- the assigned worktree
- the RLM run folder artifacts

This script refuses to fabricate results. Completion is repo-mediated.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import shlex
from datetime import datetime, timezone
from pathlib import Path
import re
import os

from lib.acpx_runner import require_acpx_on_path, run_agent_prompt
from lib.completion_check import verify_acp_completion
from lib.handoff_lock import compute_handoff_sha256, verify_handoff_hash
from lib.handoff_parser import read_handoff
from lib.validation_report import ValidationReportContext, render_validation_report_md


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(args: list[str], *, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed (exit {proc.returncode})")
    return proc.stdout.strip()


def _require_git_repo_root() -> Path:
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError("Not in a git repository (git rev-parse --show-toplevel failed)")
    return Path(proc.stdout.strip()).resolve()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _ensure_acpxrc_mcp_command_runner(*, worktree_path: Path) -> callable:
    """
    Mitigation for kimi-acp shell execution on Windows:
    - Kimi Code CLI (kimi acp) issues terminal/create with a single combined command string.
    - acpx expects argv splitting via params.command + params.args, so combined strings fail with ENOENT.

    We attach an MCP stdio server that provides an argv-based command runner tool, so Kimi can
    run verification inside the delegated session without relying on terminal/create string parsing.
    """

    server_script = (Path(__file__).resolve().parent / "mcp" / "rlm_command_runner_mcp.py").resolve()
    if not server_script.exists():
        raise FileNotFoundError(f"Missing MCP command runner helper: {server_script}")

    acpxrc_path = (worktree_path / ".acpxrc.json").resolve()
    original_text: str | None = None
    try:
        if acpxrc_path.exists():
            original_text = acpxrc_path.read_text(encoding="utf-8")
            existing = json.loads(original_text)
        else:
            existing = {}
    except Exception:
        existing = {}

    if not isinstance(existing, dict):
        existing = {}

    mcp_servers = existing.get("mcpServers")
    if mcp_servers is None:
        mcp_servers = []
    if not isinstance(mcp_servers, list):
        # acpx expects `mcpServers` to be an array (see `acpx` config parser). If an existing file
        # is invalid, overwrite during the run but restore on cleanup.
        mcp_servers = []

    desired_name = "rlm-command-runner"
    already = False
    for s in mcp_servers:
        if isinstance(s, dict) and str(s.get("name") or "").strip() == desired_name:
            already = True
            break
    if not already:
        # Use the current Python interpreter path for reliability (no reliance on `py` launcher).
        # `-u` avoids buffering surprises when running under stdio framing.
        python = Path(sys.executable).resolve()
        env_entries = []
        if os.name == "nt":
            env_entries = [
                {"name": "PYTHONUTF8", "value": "1"},
                {"name": "PYTHONIOENCODING", "value": "utf-8"},
            ]
        mcp_servers.append(
            {
                "name": desired_name,
                "command": str(python),
                "args": ["-u", str(server_script)],
                "env": env_entries,
                "_meta": {"rlm": "generated-by-rlm-workflow-acp"},
            }
        )

    existing["mcpServers"] = mcp_servers
    acpxrc_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    def cleanup() -> None:
        # Best-effort cleanup. On Windows, transient file locks can happen.
        for _ in range(5):
            try:
                if original_text is None:
                    if acpxrc_path.exists():
                        acpxrc_path.unlink()
                else:
                    acpxrc_path.write_text(original_text, encoding="utf-8", newline="\n")
                return
            except Exception:
                import time

                time.sleep(0.1)

    return cleanup


def _normalize_repo_relative_path(repo_root: Path, path_text: str) -> Path:
    raw = path_text.strip().strip("`").strip()
    if not raw:
        raise ValueError("Empty artifact path")
    if "<" in raw or "..." in raw:
        raise ValueError(f"Template placeholder not allowed in sealed handoff: {raw}")

    # Handoff paths are commonly written as "/.codex/rlm/<run-id>/...".
    if raw.startswith("/"):
        raw = raw.lstrip("/")
    return (repo_root / raw).resolve()


def _verify_input_artifacts(repo_root: Path, artifact_paths: list[str]) -> None:
    missing: list[str] = []
    for p in artifact_paths:
        fs_path = _normalize_repo_relative_path(repo_root, p)
        if not fs_path.exists():
            missing.append(str(fs_path))
    if missing:
        formatted = "\n".join(f"- {m}" for m in missing)
        raise FileNotFoundError(f"Missing required input artifacts from handoff:\n{formatted}")

def _validate_required_update_targets(run_dir: Path, repo_root: Path, artifact_paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in artifact_paths:
        fs_path = _normalize_repo_relative_path(repo_root, p)
        try:
            fs_path.relative_to(run_dir)
        except ValueError as e:
            raise ValueError(
                f"Required Artifact Updates must be under the run folder {run_dir} (got: {fs_path})"
            ) from e
        out.append(fs_path)
    return out


_EVIDENCE_JSON_RE = re.compile(r"(?mi)^[ \t]*-\s*Evidence JSON.*?:\s*`([^`]+)`\s*$")


def _extract_verification_evidence_json_path(required_verification_section: str) -> str | None:
    m = _EVIDENCE_JSON_RE.search(required_verification_section or "")
    return m.group(1).strip() if m else None


def _load_or_init_state(
    *,
    state_path: Path,
    run_id: str,
    handoff_path: Path,
    handoff_hash_hex: str,
    session_name: str,
    worktree_path: Path,
    branch: str,
) -> dict:
    if state_path.exists():
        state = _read_json(state_path)
        attempt = int(state.get("attempt", 0)) + 1
    else:
        attempt = 1
        state = {}

    state.update(
        {
            "agent": "kimi",
            "attempt": attempt,
            "acpReturnCode": state.get("acpReturnCode"),
            "acpStatus": state.get("acpStatus", "pending"),
            "branch": branch,
            "completedAt": None,
            "handoffFile": str(handoff_path).replace("\\", "/"),
            "handoffHash": f"sha256:{handoff_hash_hex}",
            "phase": "delegation",
            "runId": run_id,
            "sessionName": session_name,
            "startedAt": state.get("startedAt"),
            "status": "pending",
            "transport": "acp",
            "updatedAt": _utc_now_iso(),
            "validationProblems": state.get("validationProblems"),
            "validationReport": state.get("validationReport"),
            "validationStatus": state.get("validationStatus", "pending"),
            "worktreePath": str(worktree_path).replace("\\", "/"),
        }
    )
    return state


def _build_prompt(*, repo_root: Path, run_id: str, handoff_path: Path, handoff_content: str) -> str:
    # Deterministic prompt: fixed preamble + exact sealed handoff content.
    return (
        "You are Kimi, acting as the implementation worker for an RLM run.\n"
        "\n"
        "Hard rules:\n"
        "- Follow the sealed handoff exactly. Do not expand scope beyond Scope In.\n"
        "- Respect Scope Out. If the handoff is ambiguous, stop and report blockers.\n"
        "- Continue from the current assigned worktree state. Do not switch worktrees or branches.\n"
        "- Do not create commits unless the handoff explicitly requires it.\n"
        "- Write code directly in the assigned worktree.\n"
        "- Update RLM artifacts directly in the run folder under .codex/rlm/<run-id>/.\n"
        "- ACP is control-only. The repo/worktree artifacts are the durable record.\n"
        "- Do not modify the sealed handoff file after starting.\n"
        "\n"
        "Verification execution note:\n"
        "- On Windows, direct `kimi acp` Shell/terminal execution is unreliable.\n"
        "- Run delegated verification via the MCP tool `rlm_run_command` (server name: rlm-command-runner) with argv splitting.\n"
        "- For delegated Phase 4 testing, you MUST run the required verification command via `rlm_run_command`.\n"
        "- When running verification, pass `evidenceJsonPath` so the tool writes an evidence JSON file under the run folder.\n"
        f"  Recommended evidence path: `/.codex/rlm/{run_id}/evidence/logs/acp-verification.json` (or `.codex/rlm/{run_id}/evidence/logs/acp-verification.json`)\n"
        "- After the tool completes, read the evidence JSON and record its `outputSha256` in `04-test-summary.md`:\n"
        "  `Verification Output Sha256: <sha256>`\n"
        "\n"
        "Required completion signal:\n"
        "- Append a non-empty '## ACP Delegation Outcome' section to every artifact listed under '## Required Artifact Updates' in the sealed handoff.\n"
        "- Include at minimum: Status, Summary (or Changed Areas/Files), Verification Run, Blockers (or 'none').\n"
        "- Use this exact template (copy/paste, then fill it in with real values):\n"
        "\n"
        "```md\n"
        "## ACP Delegation Outcome\n"
        "\n"
        "Status: success|blocked|failed\n"
        "Summary: <what was done; keep it concrete>\n"
        "Changed Files:\n"
        "- <repo-relative path>\n"
        "\n"
        "Verification Run:\n"
        "- Tool: rlm_run_command (MCP argv runner)\n"
        "- Command: <exact command line or argv list>\n"
        "- Evidence JSON: `/.codex/rlm/<run-id>/evidence/logs/acp-verification.json`\n"
        "- Verification Output Sha256: <sha256>\n"
        "\n"
        "Blockers: none|<describe>\n"
        "Out-of-Scope Findings: none|<describe>\n"
        "```\n"
        "\n"
        f"Repo root (reference only): {repo_root}\n"
        f"Sealed handoff file path: {handoff_path}\n"
        "\n"
        "Sealed handoff content follows. Treat it as the source of truth for this delegated task.\n"
        "----- BEGIN SEALED HANDOFF -----\n"
        f"{handoff_content.rstrip()}\n"
        "----- END SEALED HANDOFF -----\n"
    )


def _init_handoff_and_state(
    *,
    repo_root: Path,
    run_id: str,
    worktree_path: Path,
    branch: str,
    delegated_phases: list[int],
    session_name: str,
    fixture_source: str | None,
    fixture_test: str | None,
    test_command: str | None,
) -> tuple[Path, Path]:
    run_dir = (repo_root / ".codex" / "rlm" / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "addenda").mkdir(parents=True, exist_ok=True)

    # Ensure required artifacts exist for a minimal smoke run.
    required_files = [
        run_dir / "02-to-be-plan.md",
        run_dir / "03-implementation-summary.md",
        run_dir / "04-test-summary.md",
    ]
    for p in required_files:
        if not p.exists():
            p.write_text("", encoding="utf-8", newline="\n")

    delegated_phases_str = ",".join(str(p) for p in delegated_phases)
    required_updates: list[str] = []
    if 3 in delegated_phases:
        required_updates.append(f"/.codex/rlm/{run_id}/03-implementation-summary.md")
    if 4 in delegated_phases:
        required_updates.append(f"/.codex/rlm/{run_id}/04-test-summary.md")

    handoff_path = run_dir / "02.5-acp-handoff.lock.md"
    state_path = run_dir / "02.5-acp-handoff.state.json"

    created_at = _utc_now_iso()
    input_artifacts = [
        f"/.codex/rlm/{run_id}/02-to-be-plan.md",
    ]

    fixture_lines: list[str] = []
    required_worktree_changes: list[str] = []
    if fixture_source:
        required_worktree_changes.append(fixture_source)
        fixture_lines.append(f"- Tracked source file (must modify): `{fixture_source}`")
    if fixture_test:
        required_worktree_changes.append(fixture_test)
        fixture_lines.append(f"- Tracked test file (must modify): `{fixture_test}`")

    verify_lines: list[str] = []
    if test_command:
        try:
            argv = shlex.split(test_command, posix=True)
        except Exception:
            argv = []
        verify_lines.append(f"- Run (in assigned worktree): `{test_command}`")
        verify_lines.append("- Run it via MCP tool: `rlm_run_command` (server: rlm-command-runner) with argv splitting.")
        if argv:
            verify_lines.append(f"- MCP argv (recommended): `{json.dumps(argv)}`")
        verify_lines.append(f"- Evidence JSON (must be written by tool): `/.codex/rlm/{run_id}/evidence/logs/acp-verification.json`")
        verify_lines.append("- Record command, exit code, and `outputSha256` (from evidence JSON) in `04-test-summary.md` ACP outcome.")
    else:
        verify_lines.append("- Verify the required artifacts contain the required completion signal section and fields.")

    handoff_body = "\n".join(
        [
            "# ACP Handoff",
            "",
            f"Run ID: {run_id}",
            f"Delegated Phases: {delegated_phases_str}",
            "Delegation Origin: smoke-test (generated by delegate-to-kimi.py --init-handoff)",
            "Phase: 02.5 ACP Handoff",
            "Requirement IDs: RSMOKE1",
            f"Assigned Worktree Path: {str(worktree_path).replace('\\\\', '/')}",
            f"Assigned Branch: {branch}",
            f"Created At: {created_at}",
            "",
            "## Lock",
            "Algorithm: sha256",
            "Hash: <pending>",
            "",
            "## Input Artifacts",
            *[f"- `{p}`" for p in input_artifacts],
            "",
            "## Required Artifact Updates",
            *[f"- `{p}`" for p in required_updates],
            "",
            *(
                [
                    "## Required Worktree Changes",
                    *[f"- `{p}`" for p in required_worktree_changes],
                    "",
                ]
                if required_worktree_changes
                else []
            ),
            "## Current Worktree State Rules",
            "- continue from the current assigned worktree state",
            "- do not switch worktrees",
            "- do not switch branches",
            "- do not discard changes unless explicitly instructed",
            "",
            "## Scope In",
            f"- Update only the files listed in ## Required Artifact Updates under `/.codex/rlm/{run_id}/`.",
            *(
                [
                    "- Modify only the tracked files listed in ## Required Worktree Changes.",
                ]
                if required_worktree_changes
                else []
            ),
            *fixture_lines,
            "",
            "## Scope Out",
            "- Any artifact file outside the run folder (except those listed in ## Required Artifact Updates)",
            *(
                ["- Any tracked file not listed in ## Required Worktree Changes"]
                if required_worktree_changes
                else ["- Any tracked file changes in the worktree"]
            ),
            "- Any git operations that change branches/worktrees",
            "",
            "## Required Verification",
            *verify_lines,
            "",
            "## Artifact Ownership",
            "- Kimi must write its own changes",
            "- Kimi must update the required artifacts directly",
            "",
            "## Stop Conditions",
            "- If any ambiguity exists, stop and report blockers instead of guessing.",
            "",
            "## Completion Conditions",
            "- Append '## ACP Delegation Outcome' to each required artifact update file.",
            "- Include required fields: Status, Summary (or Changed Areas/Files), Verification Run, Blockers.",
            "- For this smoke test, set Status: success and Blockers: none if completed.",
            "",
        ]
    )

    # Seal: compute hash with Hash line removed, then write the final Hash value.
    sha = compute_handoff_sha256(handoff_body)
    sealed = handoff_body.replace("Hash: <pending>", f"Hash: {sha}")
    handoff_path.write_text(sealed, encoding="utf-8", newline="\n")

    state = {
        "runId": run_id,
        "phase": "delegation",
        "handoffFile": f".codex/rlm/{run_id}/02.5-acp-handoff.lock.md",
        "handoffHash": f"sha256:{sha}",
        "agent": "kimi",
        "transport": "acp",
        "status": "pending",
        "sessionName": session_name,
        "worktreePath": str(worktree_path).replace("\\", "/"),
        "branch": branch,
        "requiredWorktreeChanges": required_worktree_changes,
        "attempt": 0,
        "startedAt": None,
        "updatedAt": _utc_now_iso(),
        "completedAt": None,
    }
    _write_json(state_path, state)

    # Verify the written handoff is valid and sealed.
    written_content, written_doc = read_handoff(handoff_path)
    ok, actual = verify_handoff_hash(written_content, written_doc.lock_hash)
    if not ok:
        raise RuntimeError(
            "Init handoff produced an invalid seal.\n"
            f"Stored: {written_doc.lock_hash}\n"
            f"Actual: {actual}"
        )

    return handoff_path, state_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delegate a sealed RLM ACP handoff (02.5) to Kimi via ACP (acpx)."
    )
    parser.add_argument("--run", required=True, help="Run ID under .codex/rlm/<run-id>/")
    parser.add_argument("--worktree", default="", help="Optional override for assigned worktree path (must match handoff).")
    parser.add_argument("--session-name", default="", help="Optional acpx session name (default: rlm-<run-id>-kimi).")
    parser.add_argument(
        "--init-handoff",
        action="store_true",
        help="Create/overwrite 02.5-acp-handoff.lock.md and 02.5-acp-handoff.state.json for a smoke run (no ACP execution).",
    )
    parser.add_argument(
        "--delegated-phases",
        default="3,4",
        help="Delegated phases for init mode: 3, 4, or 3,4 (default: 3,4).",
    )
    parser.add_argument(
        "--fixture-source",
        default="",
        help="Optional repo-relative path to a tracked source file that must be modified (init mode only).",
    )
    parser.add_argument(
        "--fixture-test",
        default="",
        help="Optional repo-relative path to a tracked test file that must be modified (init mode only).",
    )
    parser.add_argument(
        "--test-command",
        default="",
        help="Optional test command to run in the assigned worktree (init mode only).",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate completion based on the sealed handoff and repo state, update the state sidecar, and exit (no ACP execution).",
    )
    args = parser.parse_args()

    try:
        repo_root = _require_git_repo_root()

        run_id = args.run.strip()
        if not run_id:
            raise ValueError("--run must be non-empty")

        session_name = args.session_name.strip() or f"rlm-{run_id}-kimi"

        worktree_override = Path(args.worktree).expanduser().resolve() if args.worktree.strip() else None
        if args.init_handoff:
            if not worktree_override:
                raise ValueError("--init-handoff requires --worktree (assigned worktree path)")
            if not worktree_override.exists():
                raise FileNotFoundError(f"Assigned worktree path does not exist: {worktree_override}")
            current_branch = _git(["branch", "--show-current"], cwd=worktree_override)

            delegated_phases: list[int] = []
            for part in [p for p in args.delegated_phases.replace(" ", "").split(",") if p]:
                if part not in ("3", "4"):
                    raise ValueError("--delegated-phases must be 3, 4, or 3,4")
                delegated_phases.append(int(part))
            delegated_phases = sorted(set(delegated_phases))
            if not delegated_phases:
                raise ValueError("--delegated-phases must include at least one of: 3, 4")

            handoff_path, state_path = _init_handoff_and_state(
                repo_root=repo_root,
                run_id=run_id,
                worktree_path=worktree_override,
                branch=current_branch,
                delegated_phases=delegated_phases,
                session_name=session_name,
                fixture_source=args.fixture_source.strip() or None,
                fixture_test=args.fixture_test.strip() or None,
                test_command=args.test_command.strip() or None,
            )
            print(f"[OK] Wrote sealed ACP handoff: {handoff_path}")
            print(f"[OK] Wrote state sidecar: {state_path}")
            return 0

        run_dir = (repo_root / ".codex" / "rlm" / run_id).resolve()
        if not run_dir.exists():
            raise FileNotFoundError(f"Run directory not found: {run_dir}")

        handoff_path = run_dir / "02.5-acp-handoff.lock.md"
        if not handoff_path.exists():
            raise FileNotFoundError(f"Missing sealed handoff artifact: {handoff_path}")

        handoff_content, handoff = read_handoff(handoff_path)
        if handoff.lock_algorithm.strip().lower() != "sha256":
            raise ValueError(f"Unsupported handoff lock algorithm: {handoff.lock_algorithm} (expected sha256)")

        ok, actual_hash = verify_handoff_hash(handoff_content, handoff.lock_hash)
        if not ok:
            raise ValueError(
                "Handoff hash is invalid (sealed handoff was modified or hash is wrong).\n"
                f"Stored: {handoff.lock_hash}\n"
                f"Actual: {actual_hash}"
            )

        if handoff.run_id.strip() != run_id:
            raise ValueError(f"Handoff Run ID mismatch: handoff has '{handoff.run_id}', expected '{run_id}'")

        worktree_from_handoff = Path(handoff.assigned_worktree_path).expanduser().resolve()
        worktree_path = worktree_override or worktree_from_handoff
        if worktree_override and worktree_override != worktree_from_handoff:
            raise ValueError(f"--worktree does not match sealed handoff (handoff: {worktree_from_handoff}, arg: {worktree_override})")
        if not worktree_path.exists():
            raise FileNotFoundError(f"Assigned worktree path does not exist: {worktree_path}")

        # Worktree and branch enforcement.
        in_worktree = _git(["rev-parse", "--is-inside-work-tree"], cwd=worktree_path)
        if in_worktree.lower() != "true":
            raise ValueError(f"Assigned worktree path is not a git worktree: {worktree_path}")

        current_branch = _git(["branch", "--show-current"], cwd=worktree_path)
        expected_branch = handoff.assigned_branch.strip()
        if current_branch != expected_branch:
            raise ValueError(f"Branch mismatch in assigned worktree (expected {expected_branch}, got {current_branch})")

        # Input artifact enforcement (repo-relative paths).
        _verify_input_artifacts(repo_root, handoff.input_artifacts)
        required_update_paths = _validate_required_update_targets(run_dir, repo_root, handoff.required_artifact_updates)
        evidence_json_text = _extract_verification_evidence_json_path(handoff.sections.get("Required Verification", ""))
        if 4 in handoff.delegated_phases and not evidence_json_text:
            raise ValueError(
                "Delegated Phases includes 4 (testing) but the sealed handoff is missing an Evidence JSON line under ## Required Verification."
            )
        evidence_json_path = _normalize_repo_relative_path(repo_root, evidence_json_text) if evidence_json_text else None
        if evidence_json_path:
            try:
                evidence_json_path.relative_to(run_dir)
            except ValueError as e:
                raise ValueError(
                    f"Evidence JSON path must be under the run folder {run_dir} (got: {evidence_json_path})"
                ) from e

        if args.validate_only:
            state_path = run_dir / "02.5-acp-handoff.state.json"
            state = _load_or_init_state(
                state_path=state_path,
                run_id=run_id,
                handoff_path=handoff_path,
                handoff_hash_hex=actual_hash,
                session_name=session_name,
                worktree_path=worktree_path,
                branch=current_branch,
            )

            baseline_head = str(state.get("baselineHead") or "").strip() or None
            completion = verify_acp_completion(
                run_dir,
                required_update_paths,
                worktree_path=worktree_path,
                baseline_head=baseline_head,
                required_worktree_changes=handoff.required_worktree_changes or None,
                verification_evidence_json=evidence_json_path,
            )

            if completion.ok:
                state["status"] = "success"
                state["validationStatus"] = "success"
                state["validationProblems"] = None
                state["validationReport"] = None
                state["completedAt"] = _utc_now_iso()
                state["updatedAt"] = _utc_now_iso()
                state["completionArtifact"] = str(completion.artifact_path).replace("\\", "/") if completion.artifact_path else None
                if completion.changed_tracked_files is not None:
                    state["changedTrackedFiles"] = completion.changed_tracked_files
                state.pop("lastError", None)
                _write_json(state_path, state)
                print("[OK] Completion validation passed.")
                return 0

            details = "\n".join(f"- {p}" for p in (completion.problems or ["Unknown completion check failure"]))
            report_path = run_dir / "02.5-acp-handoff.validation-report.md"
            ctx = ValidationReportContext(
                run_id=run_id,
                run_dir=run_dir,
                handoff_path=handoff_path,
                worktree_path=worktree_path,
                delegated_phases=handoff.delegated_phases,
                required_updates=required_update_paths,
                evidence_json_path=evidence_json_path,
                session_name=session_name,
                acp_returncode=state.get("acpReturnCode"),
            )
            report_path.write_text(render_validation_report_md(ctx=ctx, result=completion), encoding="utf-8", newline="\n")

            state["status"] = "failed"
            state["validationStatus"] = "failed"
            state["validationProblems"] = completion.problems or ["Unknown completion check failure"]
            state["validationReport"] = str(report_path).replace("\\", "/")
            state["updatedAt"] = _utc_now_iso()
            state["completedAt"] = _utc_now_iso()
            state["lastError"] = f"ACP delegation completion validation failed:\n{details}"
            _write_json(state_path, state)
            raise RuntimeError(state["lastError"])

        require_acpx_on_path()

        needs_mcp_command_runner = (os.name == "nt") and (4 in handoff.delegated_phases)
        cleanup_acpxrc = _ensure_acpxrc_mcp_command_runner(worktree_path=worktree_path) if needs_mcp_command_runner else (lambda: None)

        state_path = run_dir / "02.5-acp-handoff.state.json"
        state = _load_or_init_state(
            state_path=state_path,
            run_id=run_id,
            handoff_path=handoff_path,
            handoff_hash_hex=actual_hash,
            session_name=session_name,
            worktree_path=worktree_path,
            branch=current_branch,
        )

        if not state.get("startedAt"):
            state["startedAt"] = _utc_now_iso()
        if not state.get("baselineHead"):
            state["baselineHead"] = _git(["rev-parse", "HEAD"], cwd=worktree_path)
        state["status"] = "running"
        state["acpStatus"] = "running"
        state["validationStatus"] = "pending"
        state["updatedAt"] = _utc_now_iso()
        state.pop("lastError", None)
        _write_json(state_path, state)

        try:
            prompt_text = _build_prompt(repo_root=repo_root, run_id=run_id, handoff_path=handoff_path, handoff_content=handoff_content)
            result = run_agent_prompt(agent="kimi", cwd=worktree_path, session_name=session_name, prompt_text=prompt_text, approve_all=True)
            if result.returncode != 0:
                state["acpReturnCode"] = int(result.returncode)
                state["acpStatus"] = "failed"
                state["updatedAt"] = _utc_now_iso()
                _write_json(state_path, state)
                raise RuntimeError(f"acpx invocation failed (exit {result.returncode})")
            state["acpReturnCode"] = 0
            state["acpStatus"] = "success"
            state["updatedAt"] = _utc_now_iso()
            _write_json(state_path, state)
        finally:
            cleanup_acpxrc()

        # Post-run: verify state is still as expected.
        current_branch_after = _git(["branch", "--show-current"], cwd=worktree_path)
        if current_branch_after != expected_branch:
            raise RuntimeError(f"Branch changed during delegation (expected {expected_branch}, got {current_branch_after})")

        completion = verify_acp_completion(
            run_dir,
            required_update_paths,
            worktree_path=worktree_path,
            baseline_head=str(state.get("baselineHead") or "").strip() or None,
            required_worktree_changes=handoff.required_worktree_changes or None,
            verification_evidence_json=evidence_json_path,
        )
        if not completion.ok:
            report_path = run_dir / "02.5-acp-handoff.validation-report.md"
            ctx = ValidationReportContext(
                run_id=run_id,
                run_dir=run_dir,
                handoff_path=handoff_path,
                worktree_path=worktree_path,
                delegated_phases=handoff.delegated_phases,
                required_updates=required_update_paths,
                evidence_json_path=evidence_json_path,
                session_name=session_name,
                acp_returncode=state.get("acpReturnCode"),
            )
            report_path.write_text(render_validation_report_md(ctx=ctx, result=completion), encoding="utf-8", newline="\n")

            state["status"] = "failed"
            state["validationStatus"] = "failed"
            state["validationProblems"] = completion.problems or ["Unknown completion check failure"]
            state["validationReport"] = str(report_path).replace("\\", "/")
            state["updatedAt"] = _utc_now_iso()
            state["completedAt"] = _utc_now_iso()
            state["lastError"] = "ACP delegation completion validation failed (see validation report)."
            _write_json(state_path, state)

            details = "\n".join(f"- {p}" for p in (completion.problems or ["Unknown completion check failure"]))
            raise RuntimeError(f"ACP delegation completion validation failed:\n{details}")

        state["status"] = "success"
        state["validationStatus"] = "success"
        state["validationProblems"] = None
        state["validationReport"] = None
        state["completedAt"] = _utc_now_iso()
        state["updatedAt"] = _utc_now_iso()
        state["completionArtifact"] = str(completion.artifact_path).replace("\\", "/") if completion.artifact_path else None
        if completion.changed_tracked_files is not None:
            state["changedTrackedFiles"] = completion.changed_tracked_files
        _write_json(state_path, state)

        return 0
    except Exception as e:
        # Best-effort state update.
        try:
            repo_root = _require_git_repo_root()
            run_dir = (repo_root / ".codex" / "rlm" / args.run.strip()).resolve()
            state_path = run_dir / "02.5-acp-handoff.state.json"
            if state_path.exists():
                state = _read_json(state_path)
            else:
                state = {
                    "runId": args.run.strip(),
                    "phase": "delegation",
                    "agent": "kimi",
                    "transport": "acp",
                    "attempt": 1,
                    "status": "pending",
                }
            state["status"] = "failed"
            # Preserve any more granular status fields if they exist.
            if "acpStatus" not in state:
                state["acpStatus"] = "unknown"
            if "validationStatus" not in state:
                state["validationStatus"] = "unknown"
            state["updatedAt"] = _utc_now_iso()
            state["completedAt"] = _utc_now_iso()
            state["lastError"] = str(e)
            _write_json(state_path, state)
        except Exception:
            pass

        print(f"[FAIL] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
