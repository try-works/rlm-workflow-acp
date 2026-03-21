#!/usr/bin/env python3
"""
Functional ACP delegation smoke test for rlm-workflow-acp (Windows-friendly).

This is intentionally not a CI test. It requires:
- `acpx` on PATH
- `kimi` on PATH and authenticated for `acpx kimi`

It creates:
- a dedicated git worktree
- a minimal tracked source+test fixture with a known failing baseline
- a normal RLM run folder under `.codex/rlm/<run-id>/`
- a sealed `02.5-acp-handoff.lock.md` for delegated phases 3+4

Then it delegates to Kimi and validates completion strictly via repo/artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lib.handoff_lock import compute_handoff_sha256


@dataclass(frozen=True)
class RunContext:
    repo_root: Path
    run_id: str
    branch: str
    worktree_path: Path
    session_name: str
    fixture_pkg: str


@dataclass(frozen=True)
class SmokeScenario:
    name: str
    description: str
    mode: str
    session_policy: str
    max_review_loops: int
    perform_delegate: bool = True
    expects_success: bool = True
    handoff_style: str = "implement"
    slice_name: str | None = None


SCENARIOS: dict[str, SmokeScenario] = {
    "implement-exec": SmokeScenario(
        name="implement-exec",
        description="One-shot implement mode with automatic review loop and transcript capture.",
        mode="implement",
        session_policy="exec",
        max_review_loops=2,
    ),
    "implement-persistent": SmokeScenario(
        name="implement-persistent",
        description="Persistent-session implement mode for a multi-turn implementation slice.",
        mode="implement",
        session_policy="persistent",
        max_review_loops=2,
    ),
    "review-exec": SmokeScenario(
        name="review-exec",
        description="One-shot review mode using the defects-or-no-defects output contract.",
        mode="review",
        session_policy="exec",
        max_review_loops=0,
        handoff_style="review",
    ),
    "ownership-violation": SmokeScenario(
        name="ownership-violation",
        description="Validation-only scenario that proves owned-write enforcement rejects unrelated tracked changes.",
        mode="implement",
        session_policy="exec",
        max_review_loops=0,
        perform_delegate=False,
        expects_success=False,
        handoff_style="ownership-violation",
    ),
    "dirty-baseline-validation": SmokeScenario(
        name="dirty-baseline-validation",
        description="Validation-only scenario that proves baseline dirty tracked files are ignored unless they change again.",
        mode="implement",
        session_policy="exec",
        max_review_loops=0,
        perform_delegate=False,
        expects_success=True,
        handoff_style="dirty-baseline",
    ),
    "multi-slice-disjoint": SmokeScenario(
        name="multi-slice-disjoint",
        description=(
            "Validation-only scenario that proves sp1 and sp2 each validate within their own owned write sets, "
            "and that sp1 rejects cross-slice tracked writes into sp2-owned files."
        ),
        mode="implement",
        session_policy="exec",
        max_review_loops=0,
        perform_delegate=False,
        expects_success=True,
        handoff_style="multi-slice",
        slice_name="sp1",
    ),
}


def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {proc.returncode}): {' '.join(cmd)}\n"
            f"--- STDOUT ---\n{proc.stdout}\n--- STDERR ---\n{proc.stderr}"
        )
    return proc


def _git(args: list[str], *, cwd: Path) -> str:
    return _run(["git", *args], cwd=cwd, check=True).stdout.strip()


def _python_exe() -> list[str]:
    # Prefer Windows py launcher if present; otherwise use current interpreter.
    if shutil.which("py"):
        return ["py", "-3"]
    return [sys.executable]


def _verification_argv(*, fixture_pkg: str) -> list[str]:
    return [sys.executable, "-m", "unittest", "-q", f"{fixture_pkg}.test_adder"]


def _shell_quote_arg(arg: str) -> str:
    if re.search(r'[\s"]', arg):
        return '"' + arg.replace('"', '\\"') + '"'
    return arg


def _verification_command(*, fixture_pkg: str) -> str:
    return " ".join(_shell_quote_arg(arg) for arg in _verification_argv(fixture_pkg=fixture_pkg))


def _baseline_adder_source() -> str:
    return '''"""Small ACP smoke fixture.

The baseline implementation is intentionally wrong so the delegated worker must fix it.
"""

def add(a: int, b: int) -> int:
    """Return a + b."""
    # BUG: should be a + b
    return a - b
'''


def _fixed_adder_source() -> str:
    return '''"""Small ACP smoke fixture.

The baseline implementation is intentionally wrong so the delegated worker must fix it.
"""

def add(a: int, b: int) -> int:
    """Return a + b."""
    return a + b
'''


def _baseline_test_source(*, fixture_pkg: str) -> str:
    return f"""import unittest

from {fixture_pkg}.adder import add


class TestAdd(unittest.TestCase):
    def test_add_basic(self):
        self.assertEqual(add(2, 2), 4)

    # TODO: Add at least one additional assertion (negative or zero case).


if __name__ == "__main__":
    unittest.main()
"""


def _extended_test_source(*, fixture_pkg: str) -> str:
    return f"""import unittest

from {fixture_pkg}.adder import add


class TestAdd(unittest.TestCase):
    def test_add_basic(self):
        self.assertEqual(add(2, 2), 4)

    def test_add_negative(self):
        self.assertEqual(add(-2, 1), -1)


if __name__ == "__main__":
    unittest.main()
"""


def _require_tools(*, needs_acp: bool) -> None:
    required = ["git"]
    if needs_acp:
        required.extend(["acpx", "kimi"])
    for tool in required:
        if not shutil.which(tool):
            raise FileNotFoundError(f"Missing required tool on PATH: {tool}")


def _repo_root() -> Path:
    proc = _run(["git", "rev-parse", "--show-toplevel"], check=True)
    return Path(proc.stdout.strip()).resolve()


def _make_context(*, run_id: str | None, worktree_root: Path, session_name: str | None) -> RunContext:
    repo_root = _repo_root()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rid = (run_id or f"acp-smoke-{ts}").strip()
    if not rid:
        raise ValueError("run_id must be non-empty")
    branch = f"acp-smoke-{rid}"
    wt = (worktree_root / rid).resolve()
    sess = (session_name or f"rlm-{rid}-kimi").strip()
    if not sess:
        raise ValueError("session_name must be non-empty")
    return RunContext(repo_root=repo_root, run_id=rid, branch=branch, worktree_path=wt, session_name=sess, fixture_pkg="acp_smoke")


def _create_worktree(ctx: RunContext) -> None:
    ctx.worktree_path.parent.mkdir(parents=True, exist_ok=True)
    # Use HEAD as the base for the worktree (clean, deterministic).
    _run(["git", "worktree", "add", "-b", ctx.branch, str(ctx.worktree_path), "HEAD"], cwd=ctx.repo_root, check=True)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _setup_fixture(ctx: RunContext) -> None:
    wt = ctx.worktree_path
    pkg_name = ctx.fixture_pkg
    pkg = wt / pkg_name
    pkg.mkdir(parents=True, exist_ok=True)

    _write(pkg / "__init__.py", "\n")
    _write(
        pkg / "adder.py",
        _baseline_adder_source(),
    )
    _write(
        pkg / "test_adder.py",
        _baseline_test_source(fixture_pkg=pkg_name),
    )

    _git(
        ["add", f"{pkg_name}/__init__.py", f"{pkg_name}/adder.py", f"{pkg_name}/test_adder.py"],
        cwd=wt,
    )
    _git(["commit", "-m", "Add ACP smoke fixture with failing test"], cwd=wt)

    # Verify baseline fails (deterministic).
    proc = _run(_python_exe() + ["-m", "unittest", "-q", f"{pkg_name}.test_adder"], cwd=wt, check=False)
    if proc.returncode == 0:
        raise RuntimeError("Expected baseline test failure, but tests passed unexpectedly.")


def _setup_run_folder(ctx: RunContext) -> Path:
    wt = ctx.worktree_path
    # Ensure scaffold exists in the worktree.
    installer = (ctx.repo_root / "scripts" / "install-rlm-workflow.py").resolve()
    _run(_python_exe() + [str(installer), "--repo-root", "."], cwd=wt, check=True)

    run_dir = (wt / ".codex" / "rlm" / ctx.run_id).resolve()
    (run_dir / "addenda").mkdir(parents=True, exist_ok=True)

    _write(
        run_dir / "00-requirements.md",
        """# Requirements

- Fix `<FIXTURE>/adder.py:add` so tests can pass.
- Add at least one more assertion to the unit test.
- Run `<VERIFY_CMD>` and record results.
""".replace("<FIXTURE>", ctx.fixture_pkg).replace("<VERIFY_CMD>", _verification_command(fixture_pkg=ctx.fixture_pkg)),
    )
    _write(
        run_dir / "00-worktree.md",
        f"""# Worktree

- Worktree Path: {wt}
- Branch: {ctx.branch}
""",
    )
    _write(
        run_dir / "01-as-is.md",
        """# As-Is

- `add(a,b)` currently returns `a - b`.
- `<VERIFY_CMD>` fails.
""".replace("<FIXTURE>", ctx.fixture_pkg).replace("<VERIFY_CMD>", _verification_command(fixture_pkg=ctx.fixture_pkg)),
    )
    _write(
        run_dir / "02-to-be-plan.md",
        """# To-Be Plan

1. Update `<FIXTURE>/adder.py` so `add(a,b)` returns `a + b`.
2. Update `<FIXTURE>/test_adder.py` to add at least one additional assertion (e.g. zero or negative case).
3. Run verification: `<VERIFY_CMD>`.
4. Update `03-implementation-summary.md` and `04-test-summary.md` with `## ACP Delegation Outcome`.
""".replace("<FIXTURE>", ctx.fixture_pkg).replace("<VERIFY_CMD>", _verification_command(fixture_pkg=ctx.fixture_pkg)),
    )
    _write(run_dir / "03-implementation-summary.md", "")
    _write(run_dir / "03.5-code-review.md", "")
    _write(run_dir / "04-test-summary.md", "")
    return run_dir


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _replace_top_field(content: str, field: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^({re.escape(field)}:\s*).*$")
    if pattern.search(content):
        return pattern.sub(rf"\1{value}", content, count=1)
    return content


def _replace_section(content: str, title: str, body_lines: list[str]) -> str:
    pattern = re.compile(
        rf"(?ms)(^##\s+{re.escape(title)}\s*$\n)(.*?)(?=^##\s+|\Z)"
    )
    replacement = "\\1" + ("\n".join(body_lines).rstrip() + "\n\n" if body_lines else "\n")
    if pattern.search(content):
        return pattern.sub(replacement, content, count=1)
    suffix = "\n" if content.endswith("\n") else "\n\n"
    return content + suffix + f"## {title}\n" + ("\n".join(body_lines).rstrip() + "\n")


def _remove_section(content: str, title: str) -> str:
    pattern = re.compile(rf"(?ms)^##\s+{re.escape(title)}\s*$\n.*?(?=^##\s+|\Z)")
    return pattern.sub("", content)


def _reseal_handoff(handoff_path: Path) -> None:
    content = _read(handoff_path)
    pending = re.sub(r"(?m)^Hash:\s*.+$", "Hash: <pending>", content, count=1)
    sha = compute_handoff_sha256(pending)
    sealed = pending.replace("Hash: <pending>", f"Hash: {sha}", 1)
    handoff_path.write_text(sealed, encoding="utf-8", newline="\n")


def _write_review_handoff(ctx: RunContext, run_dir: Path) -> None:
    handoff_path = run_dir / "02.5-acp-handoff.lock.md"
    state_path = run_dir / "02.5-acp-handoff.state.json"
    handoff = "\n".join(
        [
            "# ACP Handoff",
            "",
            f"Run ID: {ctx.run_id}",
            "Delegated Phases: 3",
            "Delegation Origin: smoke-test review scenario",
            "Delegation Role: reviewer",
            "Phase: 02.5 ACP Handoff",
            "Requirement IDs: RSMOKE1",
            f"Assigned Worktree Path: {str(ctx.worktree_path).replace('\\', '/')}",
            f"Assigned Branch: {ctx.branch}",
            f"Created At: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "",
            "## Lock",
            "Algorithm: sha256",
            "Hash: <pending>",
            "",
            "## Input Artifacts",
            f"- `/.codex/rlm/{ctx.run_id}/02-to-be-plan.md`",
            f"- `/.codex/rlm/{ctx.run_id}/03-implementation-summary.md`",
            "",
            "## Required Artifact Updates",
            f"- `/.codex/rlm/{ctx.run_id}/03.5-code-review.md`",
            "",
            "## Owned Write Files",
            f"- `/.codex/rlm/{ctx.run_id}/03.5-code-review.md`",
            "",
            "## Allowed Read Paths",
            f"- `/.codex/rlm/{ctx.run_id}/02-to-be-plan.md`",
            f"- `/.codex/rlm/{ctx.run_id}/03-implementation-summary.md`",
            f"- `/.codex/rlm/{ctx.run_id}/03.5-code-review.md`",
            "",
            "## Output Contract",
            "defects_or_no_defects",
            "",
            "## Current Worktree State Rules",
            "- continue from the current assigned worktree state",
            "- do not switch worktrees",
            "- do not switch branches",
            "",
            "## Scope In",
            "- Review the fixture implementation and plan alignment.",
            "- Update only the review artifact in the run folder.",
            "",
            "## Scope Out",
            "- Do not edit tracked source files.",
            "- Do not edit implementation or test summary artifacts.",
            "",
            "## Required Verification",
            "- Read the implementation and plan artifacts and produce a review verdict.",
            "",
            "## Artifact Ownership",
            "- Kimi must update only the review artifact.",
            "",
            "## Stop Conditions",
            "- If the sealed handoff is ambiguous, stop and report blockers.",
            "",
            "## Completion Conditions",
            "- Produce either `NO_DEFECTS` or a flat defect list.",
            "",
            "## Review Questions",
            "- Is the implementation aligned with the plan?",
            "- Are there any obvious correctness regressions?",
            "",
            "## Multi-Turn Requirement",
            "not required",
            "",
        ]
    )
    sha = compute_handoff_sha256(handoff)
    handoff_path.write_text(handoff.replace("Hash: <pending>", f"Hash: {sha}", 1), encoding="utf-8", newline="\n")
    state_path.write_text(
        json.dumps(
            {
                "runId": ctx.run_id,
                "phase": "delegation",
                "handoffFile": f".codex/rlm/{ctx.run_id}/02.5-acp-handoff.lock.md",
                "handoffHash": f"sha256:{sha}",
                "agent": "kimi",
                "transport": "acp",
                "status": "pending",
                "sessionName": ctx.session_name,
                "worktreePath": str(ctx.worktree_path).replace("\\", "/"),
                "branch": ctx.branch,
                "delegationRole": "reviewer",
                "ownedWriteFiles": [f"/.codex/rlm/{ctx.run_id}/03.5-code-review.md"],
                "allowedReadPaths": [
                    f"/.codex/rlm/{ctx.run_id}/02-to-be-plan.md",
                    f"/.codex/rlm/{ctx.run_id}/03-implementation-summary.md",
                    f"/.codex/rlm/{ctx.run_id}/03.5-code-review.md",
                ],
                "outputContract": "defects_or_no_defects",
                "requiredWorktreeChanges": [],
                "attempt": 0,
                "startedAt": None,
                "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "completedAt": None,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_verification_evidence(
    ctx: RunContext,
    run_dir: Path,
    *,
    argv: list[str],
    output_text: str,
) -> tuple[str, str]:
    evidence_path = run_dir / "evidence" / "logs" / "acp-verification.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    output_sha = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    evidence_path.write_text(
        json.dumps(
            {
                "argv": argv,
                "cwd": str(ctx.worktree_path),
                "exitCode": 0,
                "outputSha256": output_sha,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return f"/.codex/rlm/{ctx.run_id}/evidence/logs/acp-verification.json", output_sha


def _make_valid_outcome_artifact(
    path: Path,
    *,
    changed_files: list[str],
    command: str,
    evidence_json_path: str,
    verification_output_sha: str,
) -> None:
    _write(
        path,
        "\n".join(
            [
                "## ACP Delegation Outcome",
                "",
                "Status: success",
                "Summary: Prepared artifact for owned-write validation.",
                "Changed Files:",
                *[f"- {item}" for item in changed_files],
                "",
                "Verification Run:",
                "- Tool: rlm_run_command (MCP argv runner)",
                f"- Command: {command}",
                f"- Evidence JSON: `{evidence_json_path}`",
                f"- Verification Output Sha256: {verification_output_sha}",
                "",
                "Blockers: none",
                "Out-of-Scope Findings: none",
                "",
            ]
        ),
    )


def _init_handoff(ctx: RunContext) -> None:
    _init_handoff_for_slice(ctx, slice_name=None, fixture_source=ctx.fixture_pkg + "/adder.py", fixture_test=ctx.fixture_pkg + "/test_adder.py")


def _init_handoff_for_slice(
    ctx: RunContext,
    *,
    slice_name: str | None,
    fixture_source: str | None,
    fixture_test: str | None,
) -> None:
    wt = ctx.worktree_path
    delegate = (ctx.repo_root / "scripts" / "delegate-to-kimi.py").resolve()
    pkg = ctx.fixture_pkg
    _run(
        _python_exe()
        + [
            str(delegate),
            "--run",
            ctx.run_id,
            "--worktree",
            str(wt),
            "--init-handoff",
            *(["--slice", slice_name] if slice_name else []),
            "--delegated-phases",
            "3,4",
            *(["--fixture-source", fixture_source] if fixture_source else []),
            *(["--fixture-test", fixture_test] if fixture_test else []),
            "--test-command",
            _verification_command(fixture_pkg=pkg),
        ],
        cwd=wt,
        check=True,
    )


def _configure_handoff_for_scenario(ctx: RunContext, run_dir: Path, scenario: SmokeScenario) -> None:
    if scenario.handoff_style == "implement":
        _init_handoff(ctx)
        return
    if scenario.handoff_style == "review":
        _write_review_handoff(ctx, run_dir)
        return
    if scenario.handoff_style == "ownership-violation":
        _init_handoff(ctx)
        return
    if scenario.handoff_style == "dirty-baseline":
        _init_handoff(ctx)
        return
    if scenario.handoff_style == "multi-slice":
        _init_handoff_for_slice(
            ctx,
            slice_name="sp1",
            fixture_source=f"{ctx.fixture_pkg}/adder.py",
            fixture_test=None,
        )
        _init_handoff_for_slice(
            ctx,
            slice_name="sp2",
            fixture_source=None,
            fixture_test=f"{ctx.fixture_pkg}/test_adder.py",
        )
        return
    raise ValueError(f"Unsupported handoff style: {scenario.handoff_style}")


def _delegate_args(ctx: RunContext, scenario: SmokeScenario) -> list[str]:
    delegate = (ctx.repo_root / "scripts" / "delegate-to-kimi.py").resolve()
    args = [
        str(delegate),
        "--run",
        ctx.run_id,
        "--worktree",
        str(ctx.worktree_path),
        "--session-name",
        ctx.session_name,
        *(["--slice", scenario.slice_name] if scenario.slice_name else []),
        "--mode",
        scenario.mode,
        "--session-policy",
        scenario.session_policy,
        "--max-review-loops",
        str(scenario.max_review_loops),
    ]
    return args


def _delegate(ctx: RunContext, scenario: SmokeScenario) -> None:
    wt = ctx.worktree_path
    _run(_python_exe() + _delegate_args(ctx, scenario), cwd=wt, check=True)


def _validate(ctx: RunContext, *, expect_success: bool) -> tuple[list[str], bool]:
    return _validate_for_slice(ctx, expect_success=expect_success, slice_name=None)


def _validate_for_slice(ctx: RunContext, *, expect_success: bool, slice_name: str | None) -> tuple[list[str], bool]:
    return _validate_for_slice_with_test_policy(ctx, expect_success=expect_success, slice_name=slice_name, run_tests=expect_success)


def _validate_for_slice_with_test_policy(
    ctx: RunContext,
    *,
    expect_success: bool,
    slice_name: str | None,
    run_tests: bool,
) -> tuple[list[str], bool]:
    wt = ctx.worktree_path
    # Strict validation (updates sidecar; exits non-zero on failure).
    delegate = (ctx.repo_root / "scripts" / "delegate-to-kimi.py").resolve()
    validation = _run(
        _python_exe()
        + [str(delegate), "--run", ctx.run_id, "--worktree", str(wt), *(["--slice", slice_name] if slice_name else []), "--validate-only"],
        cwd=wt,
        check=False,
    )
    if expect_success and validation.returncode != 0:
        raise RuntimeError(
            "Validation expected success but failed.\n"
            f"--- STDOUT ---\n{validation.stdout}\n--- STDERR ---\n{validation.stderr}"
        )
    if (not expect_success) and validation.returncode == 0:
        raise RuntimeError("Validation expected failure but passed unexpectedly.")

    changed = _git(["diff", "--name-only"], cwd=wt).splitlines()
    changed = [c.strip() for c in changed if c.strip()]
    tests_ok = True
    if run_tests:
        tests = _run(_python_exe() + ["-m", "unittest", "-q", f"{ctx.fixture_pkg}.test_adder"], cwd=wt, check=False)
        tests_ok = tests.returncode == 0
    return sorted(changed), tests_ok


def _prepare_ownership_violation(ctx: RunContext, run_dir: Path) -> None:
    evidence_json_path, verification_output_sha = _write_verification_evidence(
        ctx,
        run_dir,
        argv=_verification_argv(fixture_pkg=ctx.fixture_pkg),
        output_text="ownership violation smoke verification\n",
    )
    _make_valid_outcome_artifact(
        run_dir / "03-implementation-summary.md",
        changed_files=[f"{ctx.fixture_pkg}/adder.py"],
        command=_verification_command(fixture_pkg=ctx.fixture_pkg),
        evidence_json_path=evidence_json_path,
        verification_output_sha=verification_output_sha,
    )
    _make_valid_outcome_artifact(
        run_dir / "04-test-summary.md",
        changed_files=[f"{ctx.fixture_pkg}/test_adder.py"],
        command=_verification_command(fixture_pkg=ctx.fixture_pkg),
        evidence_json_path=evidence_json_path,
        verification_output_sha=verification_output_sha,
    )
    _write(ctx.worktree_path / "README.md", "owned write violation smoke\n")


def _prepare_dirty_baseline_validation(ctx: RunContext, run_dir: Path) -> None:
    evidence_json_path, verification_output_sha = _write_verification_evidence(
        ctx,
        run_dir,
        argv=_verification_argv(fixture_pkg=ctx.fixture_pkg),
        output_text="dirty baseline smoke verification\n",
    )
    _make_valid_outcome_artifact(
        run_dir / "03-implementation-summary.md",
        changed_files=[f"{ctx.fixture_pkg}/adder.py"],
        command=_verification_command(fixture_pkg=ctx.fixture_pkg),
        evidence_json_path=evidence_json_path,
        verification_output_sha=verification_output_sha,
    )
    _make_valid_outcome_artifact(
        run_dir / "04-test-summary.md",
        changed_files=[f"{ctx.fixture_pkg}/test_adder.py"],
        command=_verification_command(fixture_pkg=ctx.fixture_pkg),
        evidence_json_path=evidence_json_path,
        verification_output_sha=verification_output_sha,
    )

    readme = ctx.worktree_path / "README.md"
    readme.write_text("baseline\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=ctx.worktree_path)
    _git(["commit", "-m", "Add smoke README"], cwd=ctx.worktree_path)

    readme.write_text("preexisting dirty change\n", encoding="utf-8")
    state_path = run_dir / "02.5-acp-handoff.state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["baselineHead"] = _git(["rev-parse", "HEAD"], cwd=ctx.worktree_path)
    state["baselineDirtyTrackedFiles"] = ["README.md"]
    state["baselineDirtySnapshots"] = {"README.md": {"exists": True, "sha256": _sha256(readme)}}
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    (ctx.worktree_path / ctx.fixture_pkg / "adder.py").write_text(_fixed_adder_source(), encoding="utf-8", newline="\n")
    (ctx.worktree_path / ctx.fixture_pkg / "test_adder.py").write_text(
        _extended_test_source(fixture_pkg=ctx.fixture_pkg),
        encoding="utf-8",
        newline="\n",
    )


def _prime_slice_clean_baseline(ctx: RunContext, run_dir: Path, *, slice_name: str) -> None:
    state_path = run_dir / f"02.5-acp-handoff.{slice_name}.state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["baselineHead"] = _git(["rev-parse", "HEAD"], cwd=ctx.worktree_path)
    state["baselineDirtyTrackedFiles"] = []
    state["baselineDirtySnapshots"] = {}
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _write_fixture_files(
    ctx: RunContext,
    *,
    adder_source: str,
    test_source: str,
) -> None:
    (ctx.worktree_path / ctx.fixture_pkg / "adder.py").write_text(adder_source, encoding="utf-8", newline="\n")
    (ctx.worktree_path / ctx.fixture_pkg / "test_adder.py").write_text(test_source, encoding="utf-8", newline="\n")


def _write_shared_slice_artifacts(
    ctx: RunContext,
    run_dir: Path,
    *,
    changed_files: list[str],
    fixture_pkg: str,
) -> None:
    evidence_json_path, verification_output_sha = _write_verification_evidence(
        ctx,
        run_dir,
        argv=_verification_argv(fixture_pkg=fixture_pkg),
        output_text="multi-slice smoke verification\n",
    )
    _make_valid_outcome_artifact(
        run_dir / "03-implementation-summary.md",
        changed_files=changed_files,
        command=_verification_command(fixture_pkg=fixture_pkg),
        evidence_json_path=evidence_json_path,
        verification_output_sha=verification_output_sha,
    )
    _make_valid_outcome_artifact(
        run_dir / "04-test-summary.md",
        changed_files=changed_files,
        command=_verification_command(fixture_pkg=fixture_pkg),
        evidence_json_path=evidence_json_path,
        verification_output_sha=verification_output_sha,
    )


def _prepare_multi_slice_disjoint(ctx: RunContext, run_dir: Path) -> tuple[list[str], bool]:
    summary_lines: list[str] = []

    # Positive proof: sp1 may modify only its own tracked source slice.
    _prime_slice_clean_baseline(ctx, run_dir, slice_name="sp1")
    _write_fixture_files(
        ctx,
        adder_source=_fixed_adder_source(),
        test_source=_baseline_test_source(fixture_pkg=ctx.fixture_pkg),
    )
    _write_shared_slice_artifacts(ctx, run_dir, changed_files=[f"{ctx.fixture_pkg}/adder.py"], fixture_pkg=ctx.fixture_pkg)
    _, sp1_ok = _validate_for_slice_with_test_policy(
        ctx,
        expect_success=True,
        slice_name="sp1",
        run_tests=False,
    )
    summary_lines.append("sp1-success=pass" if sp1_ok else "sp1-success=fail")

    # Positive proof: sp2 may modify only its own tracked test slice.
    _prime_slice_clean_baseline(ctx, run_dir, slice_name="sp2")
    _write_fixture_files(
        ctx,
        adder_source=_baseline_adder_source(),
        test_source=_extended_test_source(fixture_pkg=ctx.fixture_pkg),
    )
    _write_shared_slice_artifacts(ctx, run_dir, changed_files=[f"{ctx.fixture_pkg}/test_adder.py"], fixture_pkg=ctx.fixture_pkg)
    _, sp2_ok = _validate_for_slice_with_test_policy(
        ctx,
        expect_success=True,
        slice_name="sp2",
        run_tests=False,
    )
    summary_lines.append("sp2-success=pass" if sp2_ok else "sp2-success=fail")

    # Negative proof: sp1 must reject cross-slice writes into sp2-owned tracked files.
    _prime_slice_clean_baseline(ctx, run_dir, slice_name="sp1")
    _write_fixture_files(
        ctx,
        adder_source=_fixed_adder_source(),
        test_source=_extended_test_source(fixture_pkg=ctx.fixture_pkg),
    )
    _write_shared_slice_artifacts(
        ctx,
        run_dir,
        changed_files=[f"{ctx.fixture_pkg}/adder.py", f"{ctx.fixture_pkg}/test_adder.py"],
        fixture_pkg=ctx.fixture_pkg,
    )
    _, cross_slice_tests_ok = _validate_for_slice_with_test_policy(
        ctx,
        expect_success=False,
        slice_name="sp1",
        run_tests=False,
    )
    summary_lines.append("sp1-cross-slice-rejected=pass" if cross_slice_tests_ok else "sp1-cross-slice-rejected=fail")

    return summary_lines, sp1_ok and sp2_ok and cross_slice_tests_ok


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Functional ACP delegation smoke test (delegated phases 3+4).")
    parser.add_argument("--run-id", default="", help="Optional explicit run id (default: acp-smoke-<timestamp>).")
    parser.add_argument(
        "--scenario",
        default="implement-exec",
        choices=sorted(SCENARIOS.keys()),
        help="Smoke scenario to execute (default: implement-exec).",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="List supported smoke scenarios and exit.",
    )
    parser.add_argument(
        "--fixture-pkg",
        default="acp_smoke",
        help="Fixture package directory to create (default: acp_smoke). Use distinct values for parallel isolation tests.",
    )
    parser.add_argument(
        "--worktree-root",
        default="",
        help="Where to create the dedicated worktree (default: sibling dir <repo>-smoke-worktrees).",
    )
    parser.add_argument("--session-name", default="", help="Optional explicit acpx session name.")
    parser.add_argument("--keep-worktree", action="store_true", help="Do not remove the worktree after the run.")
    args = parser.parse_args()

    if args.list_scenarios:
        for scenario in SCENARIOS.values():
            print(f"{scenario.name}: {scenario.description}")
        return 0

    scenario = SCENARIOS[args.scenario]
    _require_tools(needs_acp=scenario.perform_delegate)
    repo_root = _repo_root()
    # IMPORTANT: do not create the worktree nested under the repo root.
    # acpx's session scoping logic only detects `.git` directories (not worktree `.git` files),
    # so a nested worktree can cause sessions to be treated as closed/unroutable.
    default_wt_root = (repo_root.parent / f"{repo_root.name}-smoke-worktrees").resolve()
    wt_root = Path(args.worktree_root).expanduser().resolve() if args.worktree_root.strip() else default_wt_root
    ctx = _make_context(run_id=args.run_id.strip() or None, worktree_root=wt_root, session_name=args.session_name.strip() or None)
    ctx = RunContext(
        repo_root=ctx.repo_root,
        run_id=ctx.run_id,
        branch=ctx.branch,
        worktree_path=ctx.worktree_path,
        session_name=ctx.session_name,
        fixture_pkg=args.fixture_pkg.strip() or "acp_smoke",
    )

    try:
        _create_worktree(ctx)
        _setup_fixture(ctx)
        run_dir = _setup_run_folder(ctx)
        _configure_handoff_for_scenario(ctx, run_dir, scenario)
        if scenario.perform_delegate:
            _delegate(ctx, scenario)
            changed, tests_ok = _validate_for_slice(ctx, expect_success=scenario.expects_success, slice_name=scenario.slice_name)
        else:
            if scenario.handoff_style == "ownership-violation":
                _prepare_ownership_violation(ctx, run_dir)
                changed, tests_ok = _validate_for_slice(ctx, expect_success=scenario.expects_success, slice_name=scenario.slice_name)
            elif scenario.handoff_style == "dirty-baseline":
                _prepare_dirty_baseline_validation(ctx, run_dir)
                changed, tests_ok = _validate_for_slice(ctx, expect_success=scenario.expects_success, slice_name=scenario.slice_name)
            elif scenario.handoff_style == "multi-slice":
                changed, tests_ok = _prepare_multi_slice_disjoint(ctx, run_dir)
            else:
                raise RuntimeError(f"Unsupported validation-only scenario: {scenario.handoff_style}")

        print(f"scenario={scenario.name}")
        print(f"runId={ctx.run_id}")
        print(f"worktree={ctx.worktree_path}")
        print(f"branch={ctx.branch}")
        print(f"sessionName={ctx.session_name}")
        print(f"fixturePkg={ctx.fixture_pkg}")
        print("changedTrackedFiles=" + ",".join(changed))
        print(f"testsPassed={tests_ok}")
        return 0 if (tests_ok or not scenario.expects_success) else 2
    finally:
        if not args.keep_worktree:
            # Best-effort cleanup (may fail if external processes keep files open).
            _run(["git", "worktree", "remove", "--force", str(ctx.worktree_path)], cwd=ctx.repo_root, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
