## RLM Workflow Skill

This repository uses the `rlm-workflow-acp` skill to execute the repo-document RLM workflow.
Canonical workflow rules are defined in `/.agent/PLANS.md`.
Triggers on RLM requests like Implement requirement 'run-id' and phase-specific commands.

Optional delegation mode (bounded work only):
- Codex remains supervisor and source-of-truth reader.
- Bounded implementation and/or testing tasks may be delegated to Kimi via ACP (`acpx`) using a sealed handoff: `/.codex/rlm/<run-id>/02.5-acp-handoff.lock.md`.
- Kimi must write its own code changes and update its own RLM artifacts directly in the run folder.
- ACP is control-only and must not be treated as the durable record.
- Delegation supports `implement`, `review`, and `repair` modes with `exec`, `persistent`, or `auto` session policy.
- Default policy is one-shot `exec` unless the handoff explicitly requires multi-turn context.
- Implement mode may automatically run validate -> review -> repair loops up to `--max-review-loops`.
- Python and PowerShell wrappers both default `--max-review-loops` to `2`, and explicit `0` disables the automatic review/repair loop.
- `--allowed-read-paths` is advisory only today; write ownership is enforced, read scoping is prompt/state guidance.
- `--role-template` applies to all ACP sub-attempts in a run unless future per-role template flags are introduced.
- ACP transcript evidence is always written under `/.codex/rlm/<run-id>/evidence/acp/attempt-XXX/`; `--save-transcript` is a backward-compatible no-op.

At the start of every new session, read these files before doing RLM work:
- `/.codex/STATE.md` - understand the current state of the app and codebase
- `/.codex/DECISIONS.md` - understand past work and why prior changes were made

At the start of every new RLM run, re-read these files before drafting or executing the run:
- `/.codex/STATE.md`
- `/.codex/DECISIONS.md`

When starting a new run, use `/.codex/DECISIONS.md` to identify prior RLM runs related to the new requirement or AS-IS analysis, if any.
If relevant prior runs are found, read only the docs needed to understand those affected codebase areas before writing the new run artifacts.
If no relevant prior runs are identified from `/.codex/DECISIONS.md`, skip that step.

Primary run artifacts live in:
- `/.codex/rlm/<run-id>/00-requirements.md`
- `/.codex/rlm/<run-id>/00-worktree.md`
- `/.codex/rlm/<run-id>/01-as-is.md`
- `/.codex/rlm/<run-id>/01.5-root-cause.md` (optional)
- `/.codex/rlm/<run-id>/02-to-be-plan.md`
- `/.codex/rlm/<run-id>/02.5-acp-handoff.lock.md` (optional, sealed ACP execution contract)
- `/.codex/rlm/<run-id>/02.5-acp-handoff.state.json` (optional, operational sidecar; not canonical)
- `/.codex/rlm/<run-id>/02.5-acp-handoff.validation-report.md` (optional, generated on completion validation failure)
- `/.codex/rlm/<run-id>/02.5-acp-handoff.review-report.md` (optional, generated when review mode returns defects)
- `/.codex/rlm/<run-id>/03-implementation-summary.md`
- `/.codex/rlm/<run-id>/03.5-code-review.md` (optional)
- `/.codex/rlm/<run-id>/04-test-summary.md`
- `/.codex/rlm/<run-id>/05-manual-qa.md`
- `/.codex/rlm/<run-id>/addenda/`
- `/.codex/rlm/<run-id>/evidence/`

Useful utilities (in the installed `rlm-workflow-acp` skill):
- `scripts/install-rlm-workflow.py` - cross-platform bootstrap/update (Windows/macOS/Linux)
- `scripts/install-rlm-workflow.sh` - bash wrapper for bootstrap/update (macOS/Linux)
- `scripts/rlm-init.py` - initialize a new run folder + templates (cross-platform)
- `scripts/rlm-init.ps1` - initialize a new run folder + templates (PowerShell equivalent)
- `scripts/rlm-status.py` - run status + lock chain summary (cross-platform)
- `scripts/rlm-status.ps1` - run status + lock chain summary (PowerShell equivalent)
- `scripts/lint-rlm-run.py` - artifact structure + TODO discipline linter (cross-platform)
- `scripts/lint-rlm-run.ps1` - artifact structure + TODO discipline linter (PowerShell equivalent)
- `scripts/verify-locks.py` - verify LockHash integrity for locked artifacts (cross-platform)
- `scripts/verify-locks.ps1` - verify LockHash integrity for locked artifacts (PowerShell equivalent)
- `scripts/delegate-to-kimi.py` - invoke Kimi via ACP (`acpx`) using the sealed handoff (cross-platform)
- `scripts/delegate-to-kimi.ps1` - PowerShell wrapper for `delegate-to-kimi.py`
- `scripts/smoke-acp-functional.py` - manual ACP scenario smoke harness (cross-platform)
- `scripts/smoke-acp-functional.ps1` - PowerShell wrapper for `smoke-acp-functional.py`
