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
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import os

from lib.acpx_runner import get_session_status, require_acpx_on_path, run_agent_exec, run_agent_prompt
from lib.completion_check import (
    capture_dirty_worktree_baseline,
    extract_review_findings,
    verify_acp_completion,
)
from lib.delegation_runtime import resolve_session_policy, write_acp_transcript
from lib.handoff_lock import compute_handoff_sha256, verify_handoff_hash
from lib.handoff_parser import read_handoff
from lib.validation_report import ValidationReportContext, render_validation_report_md

TRANSCRIPT_POLICY = "always_on"


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


def _split_override_values(values: list[str] | None) -> list[str]:
    items: list[str] = []
    for raw in values or []:
        for part in re.split(r"[\r\n,]+", str(raw)):
            value = part.strip().strip("`")
            if value:
                items.append(value)
    deduped: list[str] = []
    for item in items:
        normalized = item.replace("\\", "/")
        if normalized not in deduped:
            deduped.append(normalized)
    return deduped


def _normalize_slice_name(slice_name: str | None) -> str | None:
    raw = str(slice_name or "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        raise ValueError(f"Invalid --slice value: {raw!r} (expected [A-Za-z0-9_-]+)")
    return raw


@dataclass(frozen=True)
class HandoffPaths:
    base_name: str
    slice_name: str | None
    handoff_path: Path
    state_path: Path
    validation_report_path: Path
    review_report_path: Path


def _resolve_handoff_paths(*, run_dir: Path, slice_name: str | None) -> HandoffPaths:
    normalized_slice = _normalize_slice_name(slice_name)
    suffix = f".{normalized_slice}" if normalized_slice else ""
    base_name = f"02.5-acp-handoff{suffix}"
    return HandoffPaths(
        base_name=base_name,
        slice_name=normalized_slice,
        handoff_path=(run_dir / f"{base_name}.lock.md").resolve(),
        state_path=(run_dir / f"{base_name}.state.json").resolve(),
        validation_report_path=(run_dir / f"{base_name}.validation-report.md").resolve(),
        review_report_path=(run_dir / f"{base_name}.review-report.md").resolve(),
    )


def _resolve_effective_output_contract(
    *,
    handoff_output_contract: str,
    override_output_contract: str | None,
    allow_sealed_override: bool,
) -> tuple[str, bool]:
    override = str(override_output_contract or "").strip()
    if not override or override == handoff_output_contract:
        return handoff_output_contract, False
    if not allow_sealed_override:
        raise ValueError(
            "Refusing to override the sealed handoff output contract. "
            "Pass --allow-sealed-override only for explicit unsafe/debug override usage."
        )
    return override, True


def _resolve_effective_role_template_spec(
    *,
    role_template_spec: str | None,
) -> str | None:
    spec = str(role_template_spec or "").strip()
    return spec or None


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


def _build_prompt(
    *,
    repo_root: Path,
    run_id: str,
    mode: str,
    session_policy: str,
    delegation_role: str,
    role_template_spec: str | None = None,
    output_contract: str,
    owned_write_files: list[str] | None = None,
    allowed_read_paths: list[str] | None = None,
    handoff_path: Path,
    handoff_content: str,
    validation_report_path: Path | None = None,
    validation_report_text: str | None = None,
) -> str:
    role_template = _load_role_template(delegation_role=delegation_role, role_template_spec=role_template_spec)
    contract_guidance = _output_contract_guidance(output_contract=output_contract)

    # Deterministic prompt: fixed preamble + exact sealed handoff content (+ optional prior validation report).
    repair = ""
    if validation_report_path and validation_report_text:
        repair = (
            "Repair pass instructions (prior attempt failed validation):\n"
            f"- Read `{validation_report_path}` and fix ONLY the listed validation problems.\n"
            "- Do not redo implementation work unless the report explicitly says code changes are missing.\n"
            "- Do not touch any tracked files outside the handoff's required worktree changes.\n"
            "- Prefer fixing artifacts/evidence formatting, regenerating evidence JSON via `rlm_run_command` if needed, "
            "and updating `04-test-summary.md` to match the evidence sha.\n"
            "\n"
            "----- BEGIN VALIDATION REPORT -----\n"
            f"{validation_report_text.rstrip()}\n"
            "----- END VALIDATION REPORT -----\n"
            "\n"
        )

    override_guidance = ""
    if owned_write_files or allowed_read_paths:
        override_lines = ["CLI override constraints in force:"]
        if owned_write_files:
            override_lines.append("- Owned write files (effective):")
            override_lines.extend(f"  - {path}" for path in owned_write_files)
        if allowed_read_paths:
            override_lines.append("- Allowed read paths (effective, advisory only):")
            override_lines.extend(f"  - {path}" for path in allowed_read_paths)
        override_guidance = "\n".join(override_lines) + "\n\n"

    preamble = (
        f"You are Kimi, acting as the {mode} worker for an RLM run.\n"
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
        f"- Session policy for this invocation: {session_policy}.\n"
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
        f"- Output contract for this run: `{output_contract}`.\n"
        f"{contract_guidance}\n"
        "\n"
        "Role-specific instructions follow. Treat them as binding alongside the sealed handoff.\n"
        "----- BEGIN ROLE TEMPLATE -----\n"
        f"{role_template.rstrip()}\n"
        "----- END ROLE TEMPLATE -----\n"
        "\n"
    )

    tail = (
        f"{override_guidance}"
        f"Repo root (reference only): {repo_root}\n"
        f"Sealed handoff file path: {handoff_path}\n"
        "\n"
        "Sealed handoff content follows. Treat it as the source of truth for this delegated task.\n"
        "----- BEGIN SEALED HANDOFF -----\n"
        f"{handoff_content.rstrip()}\n"
        "----- END SEALED HANDOFF -----\n"
    )

    return preamble + repair + tail


def _role_template_path(*, delegation_role: str, role_template_spec: str | None = None) -> Path:
    if role_template_spec:
        candidate = Path(role_template_spec).expanduser()
        if candidate.exists():
            return candidate.resolve()
        delegation_role = role_template_spec.strip().lower()

    role_to_file = {
        "implementer": "implementer.md",
        "reviewer": "code-reviewer.md",
        "repairer": "repairer.md",
    }
    try:
        filename = role_to_file[delegation_role]
    except KeyError as exc:
        raise ValueError(f"Unsupported delegation role: {delegation_role}") from exc
    return (Path(__file__).resolve().parents[1] / "agents" / filename).resolve()


def _load_role_template(*, delegation_role: str, role_template_spec: str | None = None) -> str:
    path = _role_template_path(delegation_role=delegation_role, role_template_spec=role_template_spec)
    content = path.read_text(encoding="utf-8")
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) == 3:
            content = parts[2]
    return content.strip()


def _output_contract_guidance(*, output_contract: str) -> str:
    if output_contract in {"handoff_outcome", "repair_summary"}:
        return textwrap.dedent(
            """\
            - Append a non-empty `## ACP Delegation Outcome` section to every artifact listed under `## Required Artifact Updates`.
            - Include at minimum: `Status`, `Summary` (or `Changed Areas/Files`), `Verification Run`, and `Blockers`.
            - Use this exact template (copy/paste, then fill it in with real values):

            ```md
            ## ACP Delegation Outcome

            Status: success|blocked|failed
            Summary: <what was done; keep it concrete>
            Changed Files:
            - <repo-relative path>

            Verification Run:
            - Tool: rlm_run_command (MCP argv runner)
            - Command: <exact command line or argv list>
            - Evidence JSON: `/.codex/rlm/<run-id>/evidence/logs/acp-verification.json`
            - Verification Output Sha256: <sha256>

            Blockers: none|<describe>
            Out-of-Scope Findings: none|<describe>
            ```
            """
        ).rstrip()
    if output_contract == "defects_or_no_defects":
        return textwrap.dedent(
            """\
            - Return either a single line `NO_DEFECTS` or a flat defect list.
            - If defects exist, each defect must be a single top-level bullet or numbered item in this shape:
              - `path/to/file.ts: concrete issue`
              - `path/to/file.ts:123 concrete issue`
              - `` `/.codex/rlm/<run-id>/03.5-code-review.md`: concrete issue ``
            - Each finding must start with a concrete file or artifact reference, then a concrete issue statement.
            - Checklist bullets like `- verify tests` or `- update docs` are invalid reviewer output.
            - Do not wrap the review in narrative sections unless the sealed handoff explicitly requires a review artifact file.
            """
        ).rstrip()
    if output_contract == "patch_plan":
        return textwrap.dedent(
            """\
            - Produce a concrete patch plan with headings and actionable bullets.
            - Keep the plan scoped to the owned write set from the sealed handoff.
            """
        ).rstrip()
    raise ValueError(f"Unsupported output contract: {output_contract}")


@dataclass(frozen=True)
class AttemptExecution:
    mode: str
    delegation_role: str
    output_contract: str
    session_policy: str
    result: object
    transcript_dir: Path | None
    validation_report_path: Path | None


@dataclass(frozen=True)
class ReviewVerdict:
    verdict: str
    findings: list[str]


@dataclass(frozen=True)
class FollowUpLoopResult:
    ok: bool
    loops_used: int
    failure_reason: str | None = None
    last_report_path: Path | None = None


def _ensure_loop_state_defaults(state: dict) -> None:
    state.setdefault("attemptHistory", [])
    state.setdefault("trustLevel", "normal")
    state.setdefault("trustEvents", [])
    state.setdefault("forcedSessionPolicy", None)
    state.setdefault("acpAttemptCounter", 0)
    state.setdefault("reviewLoopCount", 0)


def _next_acp_attempt_number(state: dict) -> int:
    _ensure_loop_state_defaults(state)
    state["acpAttemptCounter"] = int(state.get("acpAttemptCounter", 0)) + 1
    return int(state["acpAttemptCounter"])


def _append_attempt_history(
    state: dict,
    *,
    attempt_number: int,
    mode: str,
    delegation_role: str,
    output_contract: str,
    requested_session_policy: str,
    resolved_session_policy: str,
    result: object,
    transcript_dir: Path | None,
    validation_report_path: Path | None,
) -> None:
    _ensure_loop_state_defaults(state)
    history = state.setdefault("attemptHistory", [])
    history.append(
        {
            "attemptNumber": attempt_number,
            "mode": mode,
            "delegationRole": delegation_role,
            "outputContract": output_contract,
            "requestedSessionPolicy": requested_session_policy,
            "resolvedSessionPolicy": resolved_session_policy,
            "returnCode": int(result.returncode),
            "executionKind": result.execution_kind,
            "sessionName": result.session_name,
            "startedAt": result.started_at,
            "completedAt": result.completed_at,
            "transcriptDir": str(transcript_dir).replace("\\", "/") if transcript_dir else None,
            "validationReport": str(validation_report_path).replace("\\", "/") if validation_report_path else None,
        }
    )


def _collect_trust_events(*, problems: list[str] | None = None, acp_returncode: int | None = None) -> list[str]:
    events: list[str] = []
    text = "\n".join(problems or []).lower()

    if acp_returncode not in (None, 0):
        events.append("acp_transport_failure")
    if "owned write set" in text or "tracked writes escaped" in text:
        events.append("ownership_violation")
    if "review output contract" in text or "missing required completion signal" in text or "missing required field in outcome section" in text:
        events.append("output_contract_violation")

    deduped: list[str] = []
    for event in events:
        if event not in deduped:
            deduped.append(event)
    return deduped


def _apply_trust_events(state: dict, trust_events: list[str]) -> None:
    if not trust_events:
        return

    _ensure_loop_state_defaults(state)
    existing = list(state.get("trustEvents") or [])
    for event in trust_events:
        if event not in existing:
            existing.append(event)
    state["trustEvents"] = existing
    state["trustLevel"] = "degraded"
    state["forcedSessionPolicy"] = "exec"


def _load_transcript_text(transcript_dir: Path | None) -> str:
    if transcript_dir is None:
        return ""

    chunks: list[str] = []
    for name in ("stdout.txt", "stderr.txt"):
        path = transcript_dir / name
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks).strip()


def _extract_review_verdict(*, transcript_dir: Path | None) -> ReviewVerdict:
    text = _load_transcript_text(transcript_dir)
    if not text:
        return ReviewVerdict(verdict="invalid", findings=[])

    if re.search(r"(?mi)^[ \t]*NO_DEFECTS[ \t]*$", text):
        return ReviewVerdict(verdict="no_defects", findings=[])

    findings, invalid = extract_review_findings(text)
    if findings and not invalid:
        return ReviewVerdict(verdict="defects", findings=findings)
    return ReviewVerdict(verdict="invalid", findings=[])


def _transcripts_always_on() -> bool:
    return TRANSCRIPT_POLICY == "always_on"


def _write_review_report(*, report_path: Path, review: ReviewVerdict, transcript_dir: Path | None) -> Path:
    transcript_ref = str(transcript_dir).replace("\\", "/") if transcript_dir else "(missing transcript)"
    findings = review.findings or ["(review transcript did not include a defect list)"]
    body = "\n".join(
        [
            "# ACP Review Findings",
            "",
            f"Generated At (UTC): {_utc_now_iso()}",
            f"Transcript: {transcript_ref}",
            "",
            "## Findings",
            *[f"- {item}" for item in findings],
            "",
            "## Repair Guidance",
            "- Fix only the findings above.",
            "- Stay inside the sealed handoff owned write set.",
            "",
        ]
    )
    report_path.write_text(body + "\n", encoding="utf-8", newline="\n")
    return report_path


def _init_handoff_and_state(
    *,
    repo_root: Path,
    run_id: str,
    slice_name: str | None,
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
    handoff_paths = _resolve_handoff_paths(run_dir=run_dir, slice_name=slice_name)

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

    handoff_path = handoff_paths.handoff_path
    state_path = handoff_paths.state_path

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

    owned_write_files = sorted(set(required_updates + required_worktree_changes))
    allowed_read_paths = sorted(set(input_artifacts + required_updates + required_worktree_changes))

    handoff_body = "\n".join(
        [
            "# ACP Handoff",
            "",
            f"Run ID: {run_id}",
            f"Delegated Phases: {delegated_phases_str}",
            "Delegation Origin: smoke-test (generated by delegate-to-kimi.py --init-handoff)",
            "Delegation Role: implementer",
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
            "## Owned Write Files",
            *([f"- `{p}`" for p in owned_write_files]),
            "",
            "## Allowed Read Paths",
            *([f"- `{p}`" for p in allowed_read_paths]),
            "",
            "## Output Contract",
            "handoff_outcome",
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
            "## Multi-Turn Requirement",
            "not required",
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
        "handoffFile": f".codex/rlm/{run_id}/{handoff_paths.handoff_path.name}",
        "handoffHash": f"sha256:{sha}",
        "agent": "kimi",
        "transport": "acp",
        "status": "pending",
        "sessionName": session_name,
        "worktreePath": str(worktree_path).replace("\\", "/"),
        "branch": branch,
        "delegationRole": "implementer",
        "ownedWriteFiles": owned_write_files,
        "allowedReadPaths": allowed_read_paths,
        "outputContract": "handoff_outcome",
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


def _write_validation_report(
    *,
    run_id: str,
    report_path: Path,
    run_dir: Path,
    handoff_path: Path,
    worktree_path: Path,
    delegated_phases: list[int],
    required_update_paths: list[Path],
    evidence_json_path: Path | None,
    session_name: str,
    output_contract: str,
    owned_write_files: list[str],
    acp_returncode: int | None,
    completion,
) -> Path:
    ctx = ValidationReportContext(
        run_id=run_id,
        run_dir=run_dir,
        handoff_path=handoff_path,
        worktree_path=worktree_path,
        delegated_phases=delegated_phases,
        required_updates=required_update_paths,
        evidence_json_path=evidence_json_path,
        session_name=session_name,
        output_contract=output_contract,
        owned_write_files=owned_write_files,
        acp_returncode=acp_returncode,
    )
    report_path.write_text(render_validation_report_md(ctx=ctx, result=completion), encoding="utf-8", newline="\n")
    return report_path


def _ensure_validation_baseline_state(*, state: dict, worktree_path: Path) -> tuple[str | None, list[str] | None, dict[str, dict[str, object]] | None]:
    baseline_head = str(state.get("baselineHead") or "").strip() or None
    if not baseline_head:
        baseline_head = _git(["rev-parse", "HEAD"], cwd=worktree_path)
        state["baselineHead"] = baseline_head

    if "baselineDirtyTrackedFiles" not in state or "baselineDirtySnapshots" not in state:
        dirty_paths, dirty_snapshots = capture_dirty_worktree_baseline(
            worktree_path=worktree_path,
            baseline_head=baseline_head,
        )
        state["baselineDirtyTrackedFiles"] = dirty_paths
        state["baselineDirtySnapshots"] = dirty_snapshots

    baseline_dirty_paths = list(state.get("baselineDirtyTrackedFiles") or []) or None
    baseline_dirty_snapshots = state.get("baselineDirtySnapshots") or None
    return baseline_head, baseline_dirty_paths, baseline_dirty_snapshots


def _validate_primary_contract(
    *,
    run_dir: Path,
    required_update_paths: list[Path],
    worktree_path: Path,
    baseline_head: str | None,
    baseline_dirty_paths: list[str] | None,
    baseline_dirty_snapshots: dict[str, dict[str, object]] | None,
    required_worktree_changes: list[str] | None,
    owned_write_files: list[str] | None,
    output_contract: str,
    verification_evidence_json: Path | None,
    transcript_dir: Path | None,
):
    return verify_acp_completion(
        run_dir,
        required_update_paths,
        worktree_path=worktree_path,
        baseline_head=baseline_head,
        baseline_dirty_paths=baseline_dirty_paths,
        baseline_dirty_snapshots=baseline_dirty_snapshots,
        required_worktree_changes=required_worktree_changes,
        owned_write_files=owned_write_files,
        output_contract=output_contract,
        verification_evidence_json=verification_evidence_json,
        transcript_dir=transcript_dir,
    )


def _run_review_contract_check(*, run_dir: Path, transcript_dir: Path | None):
    return verify_acp_completion(
        run_dir,
        [],
        output_contract="defects_or_no_defects",
        transcript_dir=transcript_dir,
    )


def _invoke_acp_attempt(
    *,
    repo_root: Path,
    run_id: str,
    run_dir: Path,
    state: dict,
    state_path: Path,
    worktree_path: Path,
    handoff_path: Path,
    handoff_content: str,
    session_name: str,
    requested_session_policy: str,
    mode: str,
    delegation_role: str,
    role_template_spec: str | None,
    output_contract: str,
    multi_turn_required: bool,
    save_transcript_flag: bool,
    owned_write_files: list[str],
    allowed_read_paths: list[str],
    required_worktree_changes: list[str],
    validation_report_path: Path | None = None,
    validation_report_text: str | None = None,
) -> AttemptExecution:
    forced_session_policy = str(state.get("forcedSessionPolicy") or "").strip().lower()
    resolved_session_policy = (
        forced_session_policy
        if forced_session_policy in {"exec", "persistent"}
        else resolve_session_policy(
            mode=mode,
            session_policy=requested_session_policy,
            multi_turn_required=multi_turn_required,
        )
    )
    prompt_text = _build_prompt(
        repo_root=repo_root,
        run_id=run_id,
        mode=mode,
        session_policy=resolved_session_policy,
        delegation_role=delegation_role,
        role_template_spec=role_template_spec,
        output_contract=output_contract,
        owned_write_files=owned_write_files,
        allowed_read_paths=allowed_read_paths,
        handoff_path=handoff_path,
        handoff_content=handoff_content,
        validation_report_path=validation_report_path,
        validation_report_text=validation_report_text,
    )

    session_status_before = None
    if resolved_session_policy == "persistent":
        session_status_before = get_session_status(agent="kimi", cwd=worktree_path, session_name=session_name)

    if resolved_session_policy == "exec":
        result = run_agent_exec(
            agent="kimi",
            cwd=worktree_path,
            prompt_text=prompt_text,
            approve_all=True,
        )
    else:
        result = run_agent_prompt(
            agent="kimi",
            cwd=worktree_path,
            session_name=session_name,
            prompt_text=prompt_text,
            approve_all=True,
        )

    session_status_after = None
    if resolved_session_policy == "persistent":
        session_status_after = get_session_status(agent="kimi", cwd=worktree_path, session_name=session_name)

    attempt_number = _next_acp_attempt_number(state)
    transcript_dir = write_acp_transcript(
        run_dir=run_dir,
        attempt=attempt_number,
        mode=mode,
        session_policy=resolved_session_policy,
        prompt_text=prompt_text,
        result=result,
        session_status_before=session_status_before,
        session_status_after=session_status_after,
        extra_metadata={
            "delegationRole": delegation_role,
            "outputContract": output_contract,
            "sessionName": session_name,
            "usedValidationReport": str(validation_report_path).replace("\\", "/") if validation_report_path else None,
            "ownedWriteFiles": owned_write_files,
            "requiredWorktreeChanges": required_worktree_changes,
            "roleTemplate": role_template_spec or delegation_role,
            "allowedReadPathsAdvisory": True,
            "allowSealedOverride": bool(state.get("allowSealedOverride")),
            "sealedOverrideApplied": bool(state.get("sealedOverrideApplied")),
            "sealedOutputContract": state.get("sealedOutputContract"),
            "trustLevel": state.get("trustLevel"),
            "forcedSessionPolicy": state.get("forcedSessionPolicy"),
            "transcriptPolicy": TRANSCRIPT_POLICY,
            "saveTranscriptFlagIgnored": bool(save_transcript_flag),
        },
    )

    state["transcriptDir"] = str(transcript_dir).replace("\\", "/") if transcript_dir else None
    _append_attempt_history(
        state,
        attempt_number=attempt_number,
        mode=mode,
        delegation_role=delegation_role,
        output_contract=output_contract,
        requested_session_policy=requested_session_policy,
        resolved_session_policy=resolved_session_policy,
        result=result,
        transcript_dir=transcript_dir,
        validation_report_path=validation_report_path,
    )
    state["acpReturnCode"] = int(result.returncode)
    state["acpStatus"] = "success" if result.returncode == 0 else "failed"
    state["updatedAt"] = _utc_now_iso()
    _write_json(state_path, state)

    return AttemptExecution(
        mode=mode,
        delegation_role=delegation_role,
        output_contract=output_contract,
        session_policy=resolved_session_policy,
        result=result,
        transcript_dir=transcript_dir,
        validation_report_path=validation_report_path,
    )


def _run_follow_up_loop(
    *,
    max_review_loops: int,
    initial_completion_ok: bool,
    initial_report_path: Path | None,
    run_review,
    run_repair,
) -> FollowUpLoopResult:
    loops_used = 0
    completion_ok = initial_completion_ok
    current_report_path = initial_report_path

    while True:
        if not completion_ok:
            if loops_used >= max_review_loops:
                return FollowUpLoopResult(
                    ok=False,
                    loops_used=loops_used,
                    failure_reason="validation_failed",
                    last_report_path=current_report_path,
                )

            repair_result = run_repair(current_report_path)
            loops_used += 1
            completion_ok = bool(repair_result["completion_ok"])
            current_report_path = repair_result.get("report_path")
            if not completion_ok:
                continue

        review_result = run_review()
        if not review_result["ok"]:
            return FollowUpLoopResult(
                ok=False,
                loops_used=loops_used,
                failure_reason="review_output_invalid",
                last_report_path=review_result.get("report_path"),
            )

        if review_result["verdict"] == "no_defects":
            return FollowUpLoopResult(ok=True, loops_used=loops_used, last_report_path=review_result.get("report_path"))

        if review_result["verdict"] != "defects":
            return FollowUpLoopResult(
                ok=False,
                loops_used=loops_used,
                failure_reason="review_unknown_verdict",
                last_report_path=review_result.get("report_path"),
            )

        if loops_used >= max_review_loops:
            return FollowUpLoopResult(
                ok=False,
                loops_used=loops_used,
                failure_reason="review_defects_remaining",
                last_report_path=review_result.get("report_path"),
            )

        repair_result = run_repair(review_result.get("report_path"))
        loops_used += 1
        completion_ok = bool(repair_result["completion_ok"])
        current_report_path = repair_result.get("report_path")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delegate a sealed RLM ACP handoff (02.5) to Kimi via ACP (acpx)."
    )
    parser.add_argument("--run", required=True, help="Run ID under .codex/rlm/<run-id>/")
    parser.add_argument("--slice", default="", help="Optional handoff slice name (e.g. sp1 -> 02.5-acp-handoff.sp1.lock.md).")
    parser.add_argument("--worktree", default="", help="Optional override for assigned worktree path (must match handoff).")
    parser.add_argument("--session-name", default="", help="Optional acpx session name (default: rlm-<run-id>-kimi).")
    parser.add_argument(
        "--mode",
        default="implement",
        choices=["implement", "review", "repair"],
        help="Delegation mode for this ACP invocation (default: implement).",
    )
    parser.add_argument(
        "--session-policy",
        default="auto",
        choices=["auto", "persistent", "exec"],
        help="ACP session policy: auto, persistent, or exec (default: auto).",
    )
    parser.add_argument(
        "--save-transcript",
        action="store_true",
        help="Deprecated no-op. ACP transcripts are always persisted under .codex/rlm/<run-id>/evidence/acp/ for validation and auditability.",
    )
    parser.add_argument(
        "--max-review-loops",
        type=int,
        default=2,
        help="Maximum automated repair/review loops for implement mode (default: 2).",
    )
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
    parser.add_argument(
        "--role-template",
        default="",
        help="Optional role template override (implementer|reviewer|repairer or a template file path).",
    )
    parser.add_argument(
        "--owned-write-files",
        action="append",
        default=[],
        help="Optional override for the owned write set. Repeat or provide comma-separated repo-relative paths.",
    )
    parser.add_argument(
        "--allowed-read-paths",
        action="append",
        default=[],
        help="Optional advisory override for allowed read paths. Repeat or provide comma-separated repo-relative paths.",
    )
    parser.add_argument(
        "--output-contract",
        default="",
        choices=["", "handoff_outcome", "defects_or_no_defects", "repair_summary", "patch_plan"],
        help="Optional output contract override.",
    )
    parser.add_argument(
        "--allow-sealed-override",
        action="store_true",
        help="Allow unsafe CLI override of sealed handoff contract fields such as --output-contract.",
    )
    args = parser.parse_args()

    try:
        repo_root = _require_git_repo_root()

        run_id = args.run.strip()
        if not run_id:
            raise ValueError("--run must be non-empty")

        slice_name = _normalize_slice_name(args.slice)
        session_name = args.session_name.strip() or f"rlm-{run_id}-kimi"
        mode = args.mode.strip().lower()
        requested_session_policy = args.session_policy.strip().lower()
        role_template_spec = _resolve_effective_role_template_spec(role_template_spec=args.role_template.strip() or None)
        override_owned_write_files = _split_override_values(args.owned_write_files)
        override_allowed_read_paths = _split_override_values(args.allowed_read_paths)
        override_output_contract = args.output_contract.strip() or None
        allow_sealed_override = bool(args.allow_sealed_override)

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
                slice_name=slice_name,
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

        handoff_paths = _resolve_handoff_paths(run_dir=run_dir, slice_name=slice_name)
        handoff_path = handoff_paths.handoff_path
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

        effective_owned_write_files = override_owned_write_files or list(handoff.owned_write_files)
        effective_allowed_read_paths = override_allowed_read_paths or list(handoff.allowed_read_paths)
        effective_output_contract, sealed_override_applied = _resolve_effective_output_contract(
            handoff_output_contract=handoff.output_contract,
            override_output_contract=override_output_contract,
            allow_sealed_override=allow_sealed_override,
        )

        resolved_session_policy = resolve_session_policy(
            mode=mode,
            session_policy=requested_session_policy,
            multi_turn_required=handoff.multi_turn_required,
        )

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
            state_path = handoff_paths.state_path
            state = _load_or_init_state(
                state_path=state_path,
                run_id=run_id,
                handoff_path=handoff_path,
                handoff_hash_hex=actual_hash,
                session_name=session_name,
                worktree_path=worktree_path,
                branch=current_branch,
            )

            state["allowSealedOverride"] = allow_sealed_override
            state["sealedOverrideApplied"] = sealed_override_applied
            state["sealedOutputContract"] = handoff.output_contract
            baseline_head, baseline_dirty_paths, baseline_dirty_snapshots = _ensure_validation_baseline_state(
                state=state,
                worktree_path=worktree_path,
            )
            completion = verify_acp_completion(
                run_dir,
                required_update_paths,
                worktree_path=worktree_path,
                baseline_head=baseline_head,
                baseline_dirty_paths=baseline_dirty_paths,
                baseline_dirty_snapshots=baseline_dirty_snapshots,
                required_worktree_changes=handoff.required_worktree_changes or None,
                owned_write_files=effective_owned_write_files or None,
                output_contract=effective_output_contract,
                verification_evidence_json=evidence_json_path,
                transcript_dir=Path(str(state.get("transcriptDir"))).resolve() if state.get("transcriptDir") else None,
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
            report_path = handoff_paths.validation_report_path
            ctx = ValidationReportContext(
                run_id=run_id,
                run_dir=run_dir,
                handoff_path=handoff_path,
                worktree_path=worktree_path,
                delegated_phases=handoff.delegated_phases,
                required_updates=required_update_paths,
                evidence_json_path=evidence_json_path,
                session_name=session_name,
                output_contract=effective_output_contract,
                owned_write_files=effective_owned_write_files,
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

        state_path = handoff_paths.state_path
        state = _load_or_init_state(
            state_path=state_path,
            run_id=run_id,
            handoff_path=handoff_path,
            handoff_hash_hex=actual_hash,
            session_name=session_name,
            worktree_path=worktree_path,
            branch=current_branch,
        )
        _ensure_loop_state_defaults(state)
        if not state.get("startedAt"):
            state["startedAt"] = _utc_now_iso()
        baseline_head, baseline_dirty_paths, baseline_dirty_snapshots = _ensure_validation_baseline_state(
            state=state,
            worktree_path=worktree_path,
        )
        state["status"] = "running"
        state["acpStatus"] = "running"
        state["validationStatus"] = "pending"
        state["delegationMode"] = mode
        state["handoffDelegationRole"] = handoff.delegation_role
        state["roleTemplate"] = role_template_spec or handoff.delegation_role
        state["allowSealedOverride"] = allow_sealed_override
        state["sealedOverrideApplied"] = sealed_override_applied
        state["sealedOutputContract"] = handoff.output_contract
        state["requestedSessionPolicy"] = requested_session_policy
        state["resolvedSessionPolicy"] = resolved_session_policy
        state["transcriptPolicy"] = TRANSCRIPT_POLICY
        state["saveTranscript"] = _transcripts_always_on()
        state["saveTranscriptFlagIgnored"] = bool(args.save_transcript)
        state["maxReviewLoops"] = int(args.max_review_loops)
        state["outputContract"] = effective_output_contract
        state["ownedWriteFiles"] = effective_owned_write_files
        state["allowedReadPaths"] = effective_allowed_read_paths
        state["transcriptDir"] = None
        state["updatedAt"] = _utc_now_iso()
        state.pop("lastError", None)
        _write_json(state_path, state)

        try:
            report_path = handoff_paths.validation_report_path
            report_text: str | None = None
            if report_path.exists():
                try:
                    report_text = report_path.read_text(encoding="utf-8")
                except Exception:
                    report_text = None

            initial_attempt = _invoke_acp_attempt(
                repo_root=repo_root,
                run_id=run_id,
                run_dir=run_dir,
                state=state,
                state_path=state_path,
                worktree_path=worktree_path,
                handoff_path=handoff_path,
                handoff_content=handoff_content,
                session_name=session_name,
                requested_session_policy=requested_session_policy,
                mode=mode,
                delegation_role=handoff.delegation_role,
                role_template_spec=role_template_spec,
                output_contract=effective_output_contract,
                multi_turn_required=handoff.multi_turn_required,
                save_transcript_flag=bool(args.save_transcript),
                owned_write_files=effective_owned_write_files,
                allowed_read_paths=effective_allowed_read_paths,
                required_worktree_changes=handoff.required_worktree_changes,
                validation_report_path=report_path if report_text else None,
                validation_report_text=report_text,
            )
            if initial_attempt.result.returncode != 0:
                trust_events = _collect_trust_events(acp_returncode=initial_attempt.result.returncode)
                _apply_trust_events(state, trust_events)
                state["updatedAt"] = _utc_now_iso()
                _write_json(state_path, state)
                raise RuntimeError(f"acpx invocation failed (exit {initial_attempt.result.returncode})")
        finally:
            cleanup_acpxrc()

        # Post-run: verify state is still as expected.
        current_branch_after = _git(["branch", "--show-current"], cwd=worktree_path)
        if current_branch_after != expected_branch:
            raise RuntimeError(f"Branch changed during delegation (expected {expected_branch}, got {current_branch_after})")

        current_completion = _validate_primary_contract(
            run_dir=run_dir,
            required_update_paths=required_update_paths,
            worktree_path=worktree_path,
            baseline_head=baseline_head,
            baseline_dirty_paths=baseline_dirty_paths,
            baseline_dirty_snapshots=baseline_dirty_snapshots,
            required_worktree_changes=handoff.required_worktree_changes or None,
            owned_write_files=effective_owned_write_files or None,
            output_contract=effective_output_contract,
            verification_evidence_json=evidence_json_path,
            transcript_dir=initial_attempt.transcript_dir,
        )

        current_report_path: Path | None = None
        if current_completion.ok:
            state["validationStatus"] = "success"
            state["validationProblems"] = None
            state["validationReport"] = None
            state["completionArtifact"] = (
                str(current_completion.artifact_path).replace("\\", "/") if current_completion.artifact_path else None
            )
            if current_completion.changed_tracked_files is not None:
                state["changedTrackedFiles"] = current_completion.changed_tracked_files
            _write_json(state_path, state)
        else:
            current_report_path = _write_validation_report(
                run_id=run_id,
                report_path=handoff_paths.validation_report_path,
                run_dir=run_dir,
                handoff_path=handoff_path,
                worktree_path=worktree_path,
                delegated_phases=handoff.delegated_phases,
                required_update_paths=required_update_paths,
                evidence_json_path=evidence_json_path,
                session_name=session_name,
                output_contract=effective_output_contract,
                owned_write_files=effective_owned_write_files,
                acp_returncode=state.get("acpReturnCode"),
                completion=current_completion,
            )
            trust_events = _collect_trust_events(problems=current_completion.problems, acp_returncode=state.get("acpReturnCode"))
            _apply_trust_events(state, trust_events)
            state["validationStatus"] = "failed"
            state["validationProblems"] = current_completion.problems or ["Unknown completion check failure"]
            state["validationReport"] = str(current_report_path).replace("\\", "/")
            state["updatedAt"] = _utc_now_iso()
            _write_json(state_path, state)

        if mode == "implement" and int(args.max_review_loops) > 0:
            def run_review() -> dict:
                review_attempt = _invoke_acp_attempt(
                    repo_root=repo_root,
                    run_id=run_id,
                    run_dir=run_dir,
                    state=state,
                    state_path=state_path,
                    worktree_path=worktree_path,
                    handoff_path=handoff_path,
                    handoff_content=handoff_content,
                    session_name=session_name,
                    requested_session_policy=requested_session_policy,
                    mode="review",
                    delegation_role="reviewer",
                    role_template_spec=role_template_spec,
                    output_contract="defects_or_no_defects",
                    multi_turn_required=False,
                    save_transcript_flag=bool(args.save_transcript),
                    owned_write_files=[],
                    allowed_read_paths=effective_allowed_read_paths,
                    required_worktree_changes=[],
                )
                if review_attempt.result.returncode != 0:
                    trust_events = _collect_trust_events(acp_returncode=review_attempt.result.returncode)
                    _apply_trust_events(state, trust_events)
                    _write_json(state_path, state)
                    return {"ok": False, "verdict": "invalid", "report_path": None}

                review_check = _run_review_contract_check(run_dir=run_dir, transcript_dir=review_attempt.transcript_dir)
                if not review_check.ok:
                    review_report = _write_review_report(
                        report_path=handoff_paths.review_report_path,
                        review=ReviewVerdict(verdict="invalid", findings=review_check.problems or []),
                        transcript_dir=review_attempt.transcript_dir,
                    )
                    trust_events = _collect_trust_events(problems=review_check.problems)
                    _apply_trust_events(state, trust_events)
                    state["validationProblems"] = review_check.problems or ["Invalid review output contract"]
                    state["validationReport"] = str(review_report).replace("\\", "/")
                    state["updatedAt"] = _utc_now_iso()
                    _write_json(state_path, state)
                    return {"ok": False, "verdict": "invalid", "report_path": review_report}

                review_verdict = _extract_review_verdict(transcript_dir=review_attempt.transcript_dir)
                review_report = None
                if review_verdict.verdict == "defects":
                    review_report = _write_review_report(
                        report_path=handoff_paths.review_report_path,
                        review=review_verdict,
                        transcript_dir=review_attempt.transcript_dir,
                    )
                state["updatedAt"] = _utc_now_iso()
                _write_json(state_path, state)
                return {"ok": True, "verdict": review_verdict.verdict, "report_path": review_report}

            def run_repair(report_for_repair: Path | None) -> dict:
                repair_text = report_for_repair.read_text(encoding="utf-8") if report_for_repair and report_for_repair.exists() else None
                repair_attempt = _invoke_acp_attempt(
                    repo_root=repo_root,
                    run_id=run_id,
                    run_dir=run_dir,
                    state=state,
                    state_path=state_path,
                    worktree_path=worktree_path,
                    handoff_path=handoff_path,
                    handoff_content=handoff_content,
                    session_name=session_name,
                    requested_session_policy=requested_session_policy,
                    mode="repair",
                    delegation_role="repairer",
                    role_template_spec=role_template_spec,
                    output_contract=effective_output_contract,
                    multi_turn_required=False,
                    save_transcript_flag=bool(args.save_transcript),
                    owned_write_files=effective_owned_write_files,
                    allowed_read_paths=effective_allowed_read_paths,
                    required_worktree_changes=handoff.required_worktree_changes,
                    validation_report_path=report_for_repair,
                    validation_report_text=repair_text,
                )
                if repair_attempt.result.returncode != 0:
                    trust_events = _collect_trust_events(acp_returncode=repair_attempt.result.returncode)
                    _apply_trust_events(state, trust_events)
                    state["updatedAt"] = _utc_now_iso()
                    _write_json(state_path, state)
                    return {"completion_ok": False, "report_path": report_for_repair}

                repair_completion = _validate_primary_contract(
                    run_dir=run_dir,
                    required_update_paths=required_update_paths,
                    worktree_path=worktree_path,
                    baseline_head=baseline_head,
                    baseline_dirty_paths=baseline_dirty_paths,
                    baseline_dirty_snapshots=baseline_dirty_snapshots,
                    required_worktree_changes=handoff.required_worktree_changes or None,
                    owned_write_files=effective_owned_write_files or None,
                    output_contract=effective_output_contract,
                    verification_evidence_json=evidence_json_path,
                    transcript_dir=repair_attempt.transcript_dir,
                )
                if repair_completion.ok:
                    state["validationStatus"] = "success"
                    state["validationProblems"] = None
                    state["validationReport"] = None
                    state["completionArtifact"] = (
                        str(repair_completion.artifact_path).replace("\\", "/") if repair_completion.artifact_path else None
                    )
                    if repair_completion.changed_tracked_files is not None:
                        state["changedTrackedFiles"] = repair_completion.changed_tracked_files
                    state["updatedAt"] = _utc_now_iso()
                    _write_json(state_path, state)
                    return {"completion_ok": True, "report_path": None}

                next_report_path = _write_validation_report(
                    run_id=run_id,
                    report_path=handoff_paths.validation_report_path,
                    run_dir=run_dir,
                    handoff_path=handoff_path,
                    worktree_path=worktree_path,
                    delegated_phases=handoff.delegated_phases,
                    required_update_paths=required_update_paths,
                    evidence_json_path=evidence_json_path,
                    session_name=session_name,
                    output_contract=effective_output_contract,
                    owned_write_files=effective_owned_write_files,
                    acp_returncode=state.get("acpReturnCode"),
                    completion=repair_completion,
                )
                trust_events = _collect_trust_events(
                    problems=repair_completion.problems,
                    acp_returncode=state.get("acpReturnCode"),
                )
                _apply_trust_events(state, trust_events)
                state["validationStatus"] = "failed"
                state["validationProblems"] = repair_completion.problems or ["Unknown completion check failure"]
                state["validationReport"] = str(next_report_path).replace("\\", "/")
                state["updatedAt"] = _utc_now_iso()
                _write_json(state_path, state)
                return {"completion_ok": False, "report_path": next_report_path}

            loop_result = _run_follow_up_loop(
                max_review_loops=int(args.max_review_loops),
                initial_completion_ok=current_completion.ok,
                initial_report_path=current_report_path,
                run_review=run_review,
                run_repair=run_repair,
            )
            state["reviewLoopCount"] = int(loop_result.loops_used)
            state["updatedAt"] = _utc_now_iso()
            _write_json(state_path, state)
            if not loop_result.ok:
                state["status"] = "failed"
                state["validationStatus"] = "failed"
                state["completedAt"] = _utc_now_iso()
                state["lastError"] = f"ACP review/repair loop failed: {loop_result.failure_reason}"
                if loop_result.last_report_path:
                    state["validationReport"] = str(loop_result.last_report_path).replace("\\", "/")
                _write_json(state_path, state)
                raise RuntimeError(f"ACP review/repair loop failed: {loop_result.failure_reason}")

        elif not current_completion.ok:
            state["status"] = "failed"
            state["validationStatus"] = "failed"
            state["updatedAt"] = _utc_now_iso()
            state["completedAt"] = _utc_now_iso()
            state["lastError"] = "ACP delegation completion validation failed (see validation report)."
            _write_json(state_path, state)

            details = "\n".join(f"- {p}" for p in (current_completion.problems or ["Unknown completion check failure"]))
            raise RuntimeError(f"ACP delegation completion validation failed:\n{details}")

        state["status"] = "success"
        state["validationStatus"] = "success"
        state["validationProblems"] = None
        state["validationReport"] = None
        state["completedAt"] = _utc_now_iso()
        state["updatedAt"] = _utc_now_iso()
        _write_json(state_path, state)

        return 0
    except Exception as e:
        # Best-effort state update.
        try:
            repo_root = _require_git_repo_root()
            run_dir = (repo_root / ".codex" / "rlm" / args.run.strip()).resolve()
            state_path = _resolve_handoff_paths(run_dir=run_dir, slice_name=_normalize_slice_name(getattr(args, "slice", ""))).state_path
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
