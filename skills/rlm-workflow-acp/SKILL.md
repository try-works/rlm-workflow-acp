---
name: rlm-workflow-acp
description: Orchestrate the RLM repo workflow end to end with strict sequential phase gates, locked artifacts, traceability, TODO discipline, and optional ACP delegation to Kimi via `acpx`. Use for requests like "Implement requirement <run-id>", "Run RLM Phase <N>", "resume requirement", "lock Phase <N>", or "verify locks".
---

# RLM Workflow ACP

Use this skill to execute repository work through the canonical RLM process while keeping the repository artifacts as the source of truth.

## Trigger Examples

- `Implement requirement '2026-02-24-add-oauth'`
- `Run RLM Phase 2 for .codex/rlm/2026-02-24-add-oauth/`
- `Resume requirement '2026-02-24-add-oauth' after manual QA`
- `Verify locks for .codex/rlm/2026-02-24-add-oauth/`

## Core Operating Model

1. Read local invocation conventions from `.codex/AGENTS.md`.
2. Read canonical workflow rules from `.agent/PLANS.md`.
3. Resolve the run folder under `.codex/rlm/<run-id>/`.
4. Enforce the lock chain before touching later phases.
5. Execute exactly one active phase at a time.
6. Keep locked artifacts immutable; use addenda for new findings.
7. Pause only for explicit manual QA in Phase 5.

If `.codex/AGENTS.md` and `.agent/PLANS.md` disagree, follow `.agent/PLANS.md` and note the mismatch in the current phase artifact when relevant.

## Bootstrap First

Before starting any run, ensure the repo scaffolding exists and is up to date:

- `.codex/AGENTS.md`
- `.agent/PLANS.md`
- `.codex/rlm/`
- installer-managed scaffolding files

Run one of:

```bash
python ./scripts/install-rlm-workflow.py --repo-root .
python3 ./scripts/install-rlm-workflow.py --repo-root .
powershell -ExecutionPolicy Bypass -File ./scripts/install-rlm-workflow.ps1 -RepoRoot .
pwsh -NoProfile -File ./scripts/install-rlm-workflow.ps1 -RepoRoot .
bash ./scripts/install-rlm-workflow.sh --repo-root .
```

## Required Read Order

Read only what is needed, in this order:

1. `.codex/AGENTS.md`
2. `.agent/PLANS.md`
3. Active run artifacts in `.codex/rlm/<run-id>/`
4. Relevant references from this skill:
   - `references/workflow-manual.md` for the full workflow narrative
   - `references/plans-canonical.md` for canonical plans content
   - `references/artifact-template.md` for artifact structure and gate scaffolding
   - `references/agents-block.md` for installer-managed AGENTS block content

## Phase Protocol

Apply the same protocol for every phase:

1. Identify the active phase from the earliest missing or not-lock-valid artifact.
2. Read the base artifact plus current-phase addenda before drafting output.
3. Write the phase artifact with required headers, traceability, TODO section, Coverage Gate, and Approval Gate.
4. Do not lock until all TODO items are complete and both gates pass.
5. When locking, add `LockedAt` and `LockHash`.

Do not create or edit later-phase artifacts while an earlier phase is unresolved.

## Mandatory Hard Rules

- Never work on `main` or `master` without explicit consent.
- Always create or enter an isolated worktree before implementation work.
- Do not write production code in Phase 3 without a failing test first.
- Do not skip Phase 1.5 when the requirement is a debugging/bug-fix requirement.
- Do not advance to later phases without a valid lock chain.
- Do not update global artifacts before manual QA sign-off.
- Do not lock any phase artifact with unchecked TODO items.

## Optional Companion Skills

When installed alongside this main skill, also use these subskills when their trigger conditions apply:

- `rlm-worktree`
  - Use for Phase 0 worktree isolation.
- `rlm-debugging`
  - Use for Phase 1.5 root cause analysis.
- `rlm-tdd`
  - Use for Phase 3 TDD discipline.
- `rlm-subagent`
  - Use for parallel sub-phase execution and optional Phase 3.5 review.

## ACP Delegation

Use ACP delegation only for bounded implementation-heavy or repairable work where repo-mediated validation is possible.

### Allowed Delegation Modes

- `implement`
- `review`
- `repair`

### Allowed Session Policies

- `exec`
- `persistent`
- `auto`

Use `auto` unless there is a concrete reason to force another policy.

### Required ACP Sequence

1. Validate the run is in a delegable state.
2. Create the sealed handoff artifact:
   - `02.5-acp-handoff.lock.md`
   - optional slice variant: `02.5-acp-handoff.<slice>.lock.md`
3. Invoke the delegation wrapper:
   - `scripts/delegate-to-kimi.py`
   - `scripts/delegate-to-kimi.ps1`
4. Re-read repo/worktree state after ACP completes.
5. Determine success from repo-mediated validation, not from ACP exit alone.

### ACP References

Read these only when delegating:

- `references/workflow-manual.md`
- `scripts/delegate-to-kimi.py`
- `scripts/lib/completion_check.py`
- `scripts/smoke-acp-functional.py`

## Validation and Utilities

Use the provided scripts instead of ad hoc checks:

- `scripts/verify-locks.py` / `.ps1`
- `scripts/lint-rlm-run.py` / `.ps1`
- `scripts/rlm-init.py` / `.ps1`
- `scripts/rlm-status.py` / `.ps1`

For ACP validation and smoke scenarios:

- `scripts/delegate-to-kimi.py`
- `scripts/smoke-acp-functional.py`

The validation-only smoke scenarios are expected to satisfy the same repo-mediated evidence contract as delegated Phase 4 runs. Positive smoke paths should therefore write deterministic verification evidence JSON under the run folder rather than using placeholder metadata.

Smoke scenario categories:

- ACP-invoking: `implement-exec`, `implement-persistent`, `review-exec`
- validation-only: `ownership-violation`, `dirty-baseline-validation`, `multi-slice-disjoint`

ACP transcript bundles are always persisted for every ACP sub-attempt. Treat `--save-transcript` as a backward-compatible no-op, not as an opt-in switch. This is required so repo-mediated validation and follow-up repair loops can rely on deterministic attempt evidence.

Loop control semantics are explicit:

- Python and PowerShell wrappers both default `--max-review-loops` to `2`
- passing `0` disables the automatic review/repair loop
- non-zero values are forwarded exactly as provided

For review-mode output (`defects_or_no_defects`), the reviewer must return either:

- `NO_DEFECTS`
- a flat top-level defect list where every item begins with a concrete file or artifact reference plus a concrete issue, for example:
  - `pm-auth/model.ts:128 request-time DDL fallback still runs on validate-only`
  - `` `/.codex/rlm/<run-id>/03.5-code-review.md`: missing explicit reviewer verdict ``

Checklist/TODO bullets like `- verify tests` or `- update docs` are invalid and must fail review-contract validation.

## When to Read Detailed References

- Read `references/workflow-manual.md` when you need the full phase-by-phase workflow narrative.
- Read `references/artifact-template.md` when drafting or repairing artifact structure.
- Read `references/plans-canonical.md` when installer-managed PLANS content matters.
- Read `references/agents-block.md` when installer-managed AGENTS content matters.

## Output Standard

Keep prompts and coordination short. Put substantive requirements, plans, execution details, and evidence in the repository artifacts rather than in chat.
