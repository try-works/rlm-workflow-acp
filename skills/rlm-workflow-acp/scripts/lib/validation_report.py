from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
from typing import Iterable

from .completion_check import CompletionResult


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


OUTCOME_TEMPLATE_MD = """## ACP Delegation Outcome

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
"""


@dataclass(frozen=True)
class ValidationReportContext:
    run_id: str
    run_dir: Path
    handoff_path: Path
    worktree_path: Path
    delegated_phases: list[int]
    required_updates: list[Path]
    evidence_json_path: Path | None
    session_name: str
    output_contract: str = "handoff_outcome"
    owned_write_files: list[str] | None = None
    acp_returncode: int | None = None


def _bullets(lines: Iterable[str]) -> str:
    return "\n".join(f"- {ln}" for ln in lines if str(ln).strip())


def render_validation_report_md(*, ctx: ValidationReportContext, result: CompletionResult) -> str:
    """
    Render a deterministic, actionable validation report for humans + for a worker agent to repair.

    This intentionally does not "fix" artifacts. It only explains what is missing and provides
    copy/paste templates and the expected evidence contract.
    """

    problems = result.problems or ["Unknown completion check failure."]
    evidence_note = (
        f"`{ctx.evidence_json_path}`" if ctx.evidence_json_path else "(none specified in handoff)"
    )
    required_updates = [str(p).replace("\\", "/") for p in ctx.required_updates]

    hints: list[str] = []
    ptxt = "\n".join(problems).lower()
    if "missing required field in outcome section" in ptxt or "missing required completion signal" in ptxt:
        hints.append(
            "Your summaries are missing required fields in the `## ACP Delegation Outcome` section. "
            "Copy/paste the template below into each required artifact and fill it in with real values."
        )
    if "verification evidence json" in ptxt:
        hints.append(
            "Do not hand-edit or hand-write the evidence JSON. Re-run verification via the MCP tool "
            "`rlm_run_command` (server `rlm-command-runner`) with `evidenceJsonPath` so the tool writes a valid schema."
        )
    if "verification output sha" in ptxt:
        hints.append(
            "Ensure `04-test-summary.md` records the verification output sha that matches the evidence JSON `outputSha256`."
        )
    if "missing required tracked worktree changes" in ptxt:
        hints.append(
            "The handoff required specific tracked file changes that were not detected. Ensure you modified the required files "
            "in the assigned worktree and did not touch unrelated tracked files."
        )
    if "owned write set" in ptxt or "tracked writes escaped" in ptxt:
        hints.append(
            "You modified tracked files outside the declared `## Owned Write Files` set. Revert or avoid those unrelated edits "
            "and keep the repair inside the owned paths only."
        )
    if "review output contract" in ptxt or "saved acp transcript does not satisfy review output contract" in ptxt:
        hints.append(
            "For review mode, produce exactly `NO_DEFECTS` or a flat top-level defect list where every item starts with a file/artifact reference and a concrete issue. Avoid checklist bullets and narrative summaries unless the handoff explicitly requires a review artifact."
        )
    if "patch plan artifact" in ptxt:
        hints.append(
            "Patch-plan outputs must contain headings or actionable top-level bullets; avoid empty placeholder files."
        )

    acp_line = "unknown"
    if ctx.acp_returncode is not None:
        acp_line = f"exit {ctx.acp_returncode} ({'success' if ctx.acp_returncode == 0 else 'failed'})"

    delegated = ",".join(str(p) for p in ctx.delegated_phases) if ctx.delegated_phases else "(unknown)"

    md = "\n".join(
        [
            "# ACP Delegation Validation Report",
            "",
            f"Run ID: {ctx.run_id}",
            f"Generated At (UTC): {_utc_now_iso()}",
            f"Session Name: {ctx.session_name}",
            f"Delegated Phases: {delegated}",
            f"Output Contract: {ctx.output_contract}",
            f"Assigned Worktree Path: {str(ctx.worktree_path).replace('\\\\', '/')}",
            f"Sealed Handoff: {str(ctx.handoff_path).replace('\\\\', '/')}",
            f"Evidence JSON Path: {evidence_note}",
            "",
            *(
                [
                    "## Owned Write Files",
                    _bullets(f"`{path}`" for path in ctx.owned_write_files or []),
                    "",
                ]
                if ctx.owned_write_files
                else []
            ),
            "## Summary",
            f"- ACP invocation status: {acp_line}",
            "- Completion validation status: failed",
            "",
            "## Problems (Blocking)",
            _bullets(problems),
            "",
            "## Repair Instructions (Worker)",
            _bullets(hints) if hints else "- Fix the blocking items above and re-run validation.",
            "",
            "### Required Artifacts To Update",
            _bullets(f"`{p}`" for p in required_updates),
            "",
            "### Outcome Section Template (Copy/Paste)",
            OUTCOME_TEMPLATE_MD.rstrip(),
            "",
            "### Evidence JSON Contract (Windows Supported Path)",
            "- Evidence must be written by the MCP argv-runner tool `rlm_run_command` (server: `rlm-command-runner`).",
            "- Evidence JSON must include: `argv` (array), `cwd` (string), `exitCode` (0 for success), `outputSha256`.",
            "- `04-test-summary.md` must record `Verification Output Sha256: <sha256>` matching evidence `outputSha256`.",
            "",
            "### Re-Validate (Supervisor)",
            "- Run validation-only mode with the same sealed handoff + current repo state:",
            f"  `python ./scripts/delegate-to-kimi.py --run \"{ctx.run_id}\" --validate-only`",
            "",
        ]
    )
    return md + "\n"
