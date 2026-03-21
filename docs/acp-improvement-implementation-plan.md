# ACP Improvement Implementation Plan

Status: Draft  
Depends On: `docs/acp-improvement-spec.md`  
Repo: `D:/DEV/rlm-workflow-acp`

## Objective

Implement the ACP delegation improvements defined in `docs/acp-improvement-spec.md` with low risk, incremental validation, and backward compatibility where practical.

This plan is execution-oriented. It breaks the work into slices that can be implemented and verified independently.

## Success Criteria

The implementation is complete when all of the following are true:

- one-shot `exec` delegation is supported and works for bounded tasks
- `implement`, `review`, and `repair` modes exist and validate differently
- sealed handoffs can declare:
  - role
  - owned write files
  - allowed read paths
  - output contract
- ownership validation blocks writes outside the declared write set
- built-in implement -> review -> repair loops can run up to a configured limit
- ACP transcripts and invocation metadata are persisted under run evidence
- Windows delegated verification remains argv-safe via MCP command runner
- smoke tests cover the new execution patterns
- docs explain when to use one-shot vs persistent sessions

## Constraints

- preserve the sealed handoff model
- preserve repo-mediated validation as the source of completion truth
- avoid breaking existing runs that use the current `02.5-acp-handoff.lock.md` shape unless explicitly migrated
- do not remove the existing Windows MCP workaround

## High-Level Delivery Order

1. Add execution primitives and transcript capture.
2. Add mode and policy plumbing.
3. Extend handoff schema and validation.
4. Add role-aware prompt composition.
5. Add automated review/repair loops.
6. Expand smoke coverage.
7. Update docs and migration notes.

## Work Breakdown

## Phase 1. ACP Runner Foundation

Goal:

- support one-shot execution and richer invocation evidence without changing higher-level delegation semantics yet

Files:

- `scripts/lib/acpx_runner.py`
- `scripts/delegate-to-kimi.py`

Tasks:

- add `run_agent_exec()` to `scripts/lib/acpx_runner.py`
- refactor command construction so both persistent and one-shot execution share common logic
- add structured capture helpers for:
  - stdout
  - stderr
  - return code
  - invocation argv
  - timing
- keep current `run_agent_prompt()` behavior intact for compatibility
- add a transcript writer utility in `scripts/delegate-to-kimi.py` or a new helper module

Deliverables:

- one-shot ACP execution path
- transcript/evidence writer
- no behavior change to current default delegation yet

Test Plan:

- unit tests for command construction on Windows and non-Windows
- unit tests for `.cmd`/`cmd /c` wrapping behavior
- unit tests for transcript metadata serialization

Acceptance Gate:

- existing behavior still works
- new one-shot helper can be invoked in isolation

## Phase 2. Delegation Mode and Session Policy Plumbing

Goal:

- allow controller-level choice of `implement`, `review`, `repair` and `exec`/`persistent`/`auto`

Files:

- `scripts/delegate-to-kimi.py`
- `scripts/delegate-to-kimi.ps1`

Tasks:

- add CLI flags:
  - `--mode`
  - `--session-policy`
  - `--max-review-loops`
  - `--save-transcript`
- implement policy resolution:
  - `review` -> `exec`
  - `repair` -> `exec`
  - `implement` -> `exec` unless multi-turn explicitly required
- record resolved mode/session policy in sidecar state
- preserve backward-compatible defaults for existing invocation when no flags are passed

Deliverables:

- controller can run Kimi in explicit mode/policy combinations

Test Plan:

- unit tests for mode parsing
- unit tests for session-policy resolution
- unit tests for backward-compatible defaults

Acceptance Gate:

- running current CLI without new flags still behaves as before
- new flags persist to sidecar state correctly

## Phase 3. Handoff Schema Extension

Goal:

- encode ownership and output expectations explicitly in the sealed handoff

Files:

- `scripts/lib/handoff_parser.py`
- `scripts/delegate-to-kimi.py`
- handoff generation logic in `scripts/delegate-to-kimi.py`

Tasks:

- extend handoff schema with:
  - `## Delegation Role`
  - `## Owned Write Files`
  - `## Allowed Read Paths`
  - `## Output Contract`
- add optional sections:
  - `## Review Questions`
  - `## Repair Scope`
  - `## Multi-Turn Requirement`
- implement parser validation for new sections
- define compatibility behavior for older handoffs:
  - if missing new fields, infer safe defaults
  - or require explicit migration flag
- update init-handoff generation for smoke runs to emit the new schema

Deliverables:

- versioned or backward-compatible handoff schema

Test Plan:

- parser tests for valid new handoff
- parser tests for malformed ownership/output sections
- parser tests for backward compatibility

Acceptance Gate:

- sealed handoff remains hash-validated correctly
- parser clearly rejects invalid new schema values

## Phase 4. Validation Upgrade: Ownership and Output Contracts

Goal:

- machine-enforce bounded worker behavior

Files:

- `scripts/lib/completion_check.py`
- `scripts/lib/validation_report.py`
- possibly new helper module for snapshots

Tasks:

- add ownership checks:
  - changed tracked files must be subset of owned write files
  - required tracked changes must be satisfied
- add review-mode validation:
  - success if output is `NO_DEFECTS` or valid defect list
  - no artifact update required unless requested by handoff
- add repair-mode validation:
  - ensure changes stay within repair scope
- add output-contract validators:
  - `handoff_outcome`
  - `defects_or_no_defects`
  - `repair_summary`
  - `patch_plan`
- add dirty-worktree-safe comparison:
  - capture file snapshots for owned write files at delegation start
  - compare against current file content, not only `baselineHead`

Deliverables:

- stronger completion validation
- better validation reports for ownership and output errors

Test Plan:

- unit tests for ownership success/failure
- unit tests for review-mode output validation
- unit tests for dirty-worktree-safe detection
- unit tests for repair-scope violations

Acceptance Gate:

- unrelated file edits are rejected deterministically
- review mode can pass without implementation artifact updates

## Phase 5. Role-Aware Prompt Composition

Goal:

- make Kimi behave like a bounded role-specific worker instead of a generic delegate

Files:

- `scripts/delegate-to-kimi.py`
- `agents/implementer.md`
- `agents/code-reviewer.md`
- `agents/repairer.md` new

Tasks:

- load role templates from the `agents/` directory
- refactor `_build_prompt()` to compose:
  - common ACP preamble
  - role template
  - mode-specific rules
  - optional repair/validation report
  - sealed handoff
- add explicit Windows instruction:
  - do not use shell-style `cd && command`
  - use MCP argv runner when execution is required
- add strict review output rules:
  - `NO_DEFECTS` or flat defect list only

Deliverables:

- prompt builder aware of role, mode, and repair context

Test Plan:

- unit tests for prompt composition
- golden-file tests for implement/review/repair prompts

Acceptance Gate:

- prompt text includes the correct role template and output contract

## Phase 6. Automated Review/Repair Loop Orchestration

Goal:

- make ACP delegation self-correcting within bounded limits

Files:

- `scripts/delegate-to-kimi.py`
- sidecar state logic

Tasks:

- add loop orchestration:
  - run implement
  - validate
  - run review
  - if defects, run repair
  - re-validate
  - re-review
- add `--max-review-loops`
- add trust degradation logic:
  - detect syntax-broken or ownership-violating worker output
  - force one-shot mode on retry
  - optionally demote worker to review-only for that slice
- persist loop-attempt metadata in sidecar

Deliverables:

- controller-managed ACP review loop

Test Plan:

- integration tests for:
  - review returns defects -> repair -> success
  - validation failure -> repair -> success
  - repeated bad outputs -> fail after max loops

Acceptance Gate:

- repair loops terminate deterministically
- state sidecar clearly shows attempt history and failure reason

## Phase 7. Evidence and Transcript Persistence

Goal:

- make ACP runs auditable and debuggable

Files:

- `scripts/delegate-to-kimi.py`
- optional new helper module for evidence writing

Tasks:

- write evidence under:
  - `.codex/rlm/<run-id>/evidence/acp/`
- persist per-attempt artifacts:
  - prompt text
  - stdout
  - stderr
  - invocation metadata JSON
  - session status before/after
- include:
  - mode
  - session policy
  - attempt number
  - session name
  - owned write files
  - required changes
  - validation report reference if used

Deliverables:

- ACP evidence package per attempt

Test Plan:

- integration tests for evidence file creation
- validation that evidence files are written for both success and failure

Acceptance Gate:

- every ACP run leaves behind enough evidence to reconstruct what happened

## Phase 8. Smoke-Test Expansion

Goal:

- prove the improved ACP flow works in the scenarios the spec is targeting

Files:

- `scripts/smoke-acp-functional.py`

Tasks:

- add scenarios for:
  - one-shot review
  - one-shot implement
  - persistent implement
  - validation-repair loop
  - reviewer-finding repair loop
  - ownership violation
  - dirty-worktree-safe validation
  - multi-slice delegation with disjoint write sets
- keep current smoke scenario intact as a compatibility baseline

Deliverables:

- broader manual functional smoke coverage

Test Plan:

- run smoke scenarios manually in a controlled environment
- document expected outputs and sidecar states

Acceptance Gate:

- each scenario has deterministic pass/fail criteria

## Phase 9. Docs and Migration

Goal:

- align docs and operational guidance with the new behavior

Files:

- `SKILL.md`
- `README.md`
- `references/agents-block.md`
- new docs if needed

Tasks:

- document the new mode model
- document session-policy guidance:
  - one-shot by default
  - persistent only when justified
- document ownership fields in handoffs
- document review-loop behavior
- document transcript/evidence locations
- add migration notes for old handoff format

Deliverables:

- updated operator documentation

Acceptance Gate:

- new users can understand when and how to use ACP delegation without relying on internal source knowledge

## File-Level Change Map

### Core runtime

- `scripts/lib/acpx_runner.py`
  - add one-shot execution path
  - add richer capture/metadata support

- `scripts/delegate-to-kimi.py`
  - add mode and policy orchestration
  - add review loops
  - add evidence writing
  - add role-aware prompt composition

### Validation and schema

- `scripts/lib/handoff_parser.py`
  - parse new handoff fields

- `scripts/lib/completion_check.py`
  - add ownership validation
  - add output-contract validation
  - add review-mode behavior

- `scripts/lib/validation_report.py`
  - add report variants for review/repair failures

### Agent roles

- `agents/implementer.md`
  - tighten ownership wording if needed

- `agents/code-reviewer.md`
  - align output with `NO_DEFECTS` or flat defect list mode

- `agents/repairer.md`
  - new role template for bounded repair work

### Testing and docs

- `scripts/smoke-acp-functional.py`
- `README.md`
- `SKILL.md`
- `references/agents-block.md`

## Risks and Mitigations

### Risk 1. Breaking existing handoff flows

Mitigation:

- add backward-compatible parsing first
- keep legacy flow valid until migration is documented

### Risk 2. Overcomplicating validation

Mitigation:

- stage validation upgrades behind clear output contracts
- keep error reports deterministic and repairable

### Risk 3. Review loops become noisy or expensive

Mitigation:

- default loop count to `2`
- keep review output contract extremely tight

### Risk 4. Session-policy changes introduce regressions

Mitigation:

- keep old persistent path intact initially
- add one-shot mode as additive behavior first

### Risk 5. Windows execution regressions

Mitigation:

- do not remove MCP argv-runner path
- add tests and prompts that explicitly avoid shell-string patterns

## Rollout Strategy

### Milestone 1

- Phase 1 + Phase 2
- result: one-shot support and mode/session-policy plumbing

### Milestone 2

- Phase 3 + Phase 4 + Phase 5
- result: new handoff schema, ownership validation, role-aware prompts

### Milestone 3

- Phase 6 + Phase 7
- result: automated review/repair loops and complete evidence capture

### Milestone 4

- Phase 8 + Phase 9
- result: smoke coverage and updated docs

## Suggested First PR

Keep the first implementation PR small and safe:

- add `run_agent_exec()` to `scripts/lib/acpx_runner.py`
- add `--session-policy` with `exec|persistent|auto`
- add transcript persistence
- do not change handoff schema yet

Reason:

- this gives immediate operational value
- it reduces reliance on persistent ACP sessions
- it makes later failures easier to debug

## Suggested Second PR

- extend handoff schema
- add ownership validation
- add role-aware prompt composition

## Suggested Third PR

- add review/repair modes
- add automated loop orchestration
- expand smoke tests

## Completion Definition

This implementation plan is complete when:

- the milestones above are implemented and merged
- smoke scenarios cover the new flow
- docs match actual behavior
- the default ACP delegation path is safer, more observable, and more bounded than the current implementation
