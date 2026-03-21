# ACP Delegation Improvement Spec

Status: Draft  
Repo: `D:/DEV/rlm-workflow-acp`  
Scope: `rlm-workflow-acp` ACP delegation path to Kimi via `acpx`

## Purpose

This spec defines improvements to the current ACP delegation flow so that Kimi can be used more reliably as a bounded implementation and review worker inside the RLM workflow.

The goals are:

- reduce flaky session behavior
- prefer bounded one-shot delegation for narrow tasks
- make worker ownership explicit
- support implement, review, and repair modes separately
- add built-in review loops
- improve observability and evidence
- preserve the existing sealed-handoff and repo-mediated validation model

## Current State Summary

The current ACP path is centered on:

- sealed handoff artifact generation in `scripts/delegate-to-kimi.py`
- persistent named ACP sessions in `scripts/lib/acpx_runner.py`
- repo-mediated completion validation in `scripts/lib/completion_check.py`
- optional Windows MCP argv-runner injection for delegated Phase 4 verification

This is a strong base for safety, but it has several execution weaknesses:

- persistent sessions are the default for all work, even when one-shot execution is safer
- prompt construction is generic and does not explicitly differentiate implementer vs reviewer vs repair worker behavior
- validation checks outputs after the fact but does not orchestrate review/repair loops
- handoff schema does not explicitly encode worker ownership boundaries beyond required changed paths
- ACP evidence is not captured richly enough for debugging bad runs

## Problems To Solve

### P1. Persistent session overuse

Current behavior:

- `run_agent_prompt()` always ensures a named session and uses it for prompt delivery.
- This increases exposure to session liveness issues, queue-owner drift, and stale context bleed.

Relevant code:

- `scripts/lib/acpx_runner.py`

Impact:

- bounded work takes on unnecessary session complexity
- review tasks inherit irrelevant context
- failure recovery is harder than it needs to be

### P2. Single generic worker prompt

Current behavior:

- `_build_prompt()` in `scripts/delegate-to-kimi.py` constructs one generic worker contract
- the repo already has role templates in `agents/implementer.md` and `agents/code-reviewer.md`, but they are not composed into ACP delegation

Impact:

- implementation and review tasks are not sharply separated
- Kimi is encouraged to behave like a broad autonomous worker instead of a bounded specialist

### P3. Weak write-ownership modeling

Current behavior:

- `Required Worktree Changes` exists in the handoff and is validated
- extra tracked changes are rejected
- however, there is no explicit distinction between:
  - files the worker may modify
  - files that must change
  - files the worker may read for context

Impact:

- prompts are less precise than they should be
- a worker can still overreach within poorly bounded slices
- review and repair passes cannot be constrained as tightly as they should be

### P4. No first-class review and repair loop

Current behavior:

- validation can fail and produce a repair report
- the tool does not have explicit `review` and `repair` modes
- the controller logic does not automatically run implement -> review -> repair

Impact:

- bad implementation passes require ad hoc human supervision
- there is no standardized “NO_DEFECTS or flat defect list” contract
- the process is slower and less predictable than it could be

### P5. Incomplete observability

Current behavior:

- sidecar state records coarse status fields
- there is no standardized persisted transcript package for prompt text, agent output, ACP mode, and session snapshots

Impact:

- it is difficult to diagnose why a worker failed
- hard to distinguish:
  - ACP transport failure
  - worker misunderstanding
  - validation mismatch
  - ownership violation

### P6. Windows ACP execution policy is documented but not operationalized enough

Current behavior:

- the repo documents Windows terminal limitations
- the MCP argv-runner workaround is injected for delegated Phase 4 testing
- there is no stronger policy in prompt construction around shell usage

Impact:

- workers may still reason toward shell-based execution patterns that are unreliable on Windows

## Design Principles

### D1. Kimi is a bounded contributor, not the overall integrator

The supervisor remains responsible for:

- reading requirements and plans
- selecting delegation slices
- auditing diffs
- deciding whether work is accepted

Kimi should be used for:

- isolated implementation slices
- narrow review passes
- bounded repair work

### D2. One-shot by default

If a task can be expressed as a bounded ask with a clear output contract, it should use one-shot ACP execution.

Persistent sessions should be reserved for:

- long-running implementation slices that genuinely need retained conversational context
- explicit human choice

### D3. Ownership must be machine-checkable

Delegated work must clearly declare:

- what can be changed
- what must be changed
- what may be read

The validator must enforce those boundaries.

### D4. Review is a first-class mode

Review should not be treated as a human-only outer loop. It should be a built-in ACP mode with a strict output contract.

### D5. Transport evidence is required

Every ACP run should leave behind enough evidence to explain:

- what was asked
- how it was run
- what came back
- why validation passed or failed

## Proposed Changes

## 1. Add Delegation Modes

Add explicit delegation modes to `scripts/delegate-to-kimi.py`:

- `implement`
- `review`
- `repair`

### 1.1 Implement mode

Purpose:

- code and artifact modification inside an owned slice

Behavior:

- may write to owned files
- must satisfy required artifact updates
- may run delegated verification if allowed by handoff

### 1.2 Review mode

Purpose:

- review existing implementation without modifying tracked files unless explicitly allowed

Output contract:

- either `NO_DEFECTS`
- or a flat, concrete defect list with file references and remediation guidance

Validation:

- success does not require implementation artifact mutation unless the handoff explicitly requests a written review artifact

### 1.3 Repair mode

Purpose:

- fix only defects listed in a prior validation report or review report

Behavior:

- bounded to a smaller scope than implement mode
- must not broaden changes beyond the repair target

## 2. Add Session Policy Selection

Add `--session-policy` to `delegate-to-kimi.py` and corresponding support in `scripts/lib/acpx_runner.py`.

Allowed values:

- `exec`
- `persistent`
- `auto`

### 2.1 Exec

Behavior:

- uses `acpx --cwd <path> kimi exec ...`
- no saved session state
- preferred for:
  - review
  - repair
  - narrow implementation slices

### 2.2 Persistent

Behavior:

- uses named session flow similar to current implementation
- appropriate for:
  - multi-turn implementation slices
  - deliberate long-running worker contexts

### 2.3 Auto

Default policy:

- `review` -> `exec`
- `repair` -> `exec`
- `implement` -> `exec` unless handoff explicitly marks `multi_turn_required: true`

## 3. Extend the Sealed Handoff Schema

Extend `02.5-acp-handoff.lock.md` and `scripts/lib/handoff_parser.py`.

### 3.1 New required sections

- `## Delegation Role`
- `## Owned Write Files`
- `## Allowed Read Paths`
- `## Output Contract`

### 3.2 New optional sections

- `## Review Questions`
- `## Repair Scope`
- `## Multi-Turn Requirement`

### 3.3 Semantics

#### Delegation Role

Allowed values:

- `implementer`
- `reviewer`
- `repairer`

#### Owned Write Files

Definition:

- exact repo-relative tracked files the worker may modify

#### Allowed Read Paths

Definition:

- repo-relative files or directories the worker may inspect for context

#### Output Contract

Allowed values:

- `handoff_outcome`
- `defects_or_no_defects`
- `repair_summary`
- `patch_plan`

#### Review Questions

Definition:

- optional explicit review prompts the reviewer must answer

#### Repair Scope

Definition:

- references a validation report or review report that defines the allowed repair targets

## 4. Separate Ownership From Required Changes

Retain `## Required Worktree Changes`, but define it separately from `## Owned Write Files`.

### 4.1 Owned write files

What the worker may change.

### 4.2 Required worktree changes

What must actually change for the delegation to count as complete.

### 4.3 Allowed read paths

What the worker may inspect without broadening scope.

This allows:

- large context reads with narrow writes
- reviewer mode with no writes
- repair mode with smaller ownership than initial implement mode

## 5. Role-Aware Prompt Composition

Refactor `_build_prompt()` in `scripts/delegate-to-kimi.py`.

### 5.1 Current issue

The prompt is a generic worker preamble plus sealed handoff content.

### 5.2 New design

Prompt assembly should include:

1. common ACP transport and hard-rule preamble
2. role template content loaded from:
   - `agents/implementer.md`
   - `agents/code-reviewer.md`
   - new `agents/repairer.md`
3. mode-specific instructions
4. optional prior validation/report content
5. sealed handoff content

### 5.3 Mode-specific prompt rules

#### Implement mode

- emphasize exact file ownership
- require minimal code changes
- require adherence to output contract

#### Review mode

- explicitly forbid unrelated edits
- require either `NO_DEFECTS` or a defect list
- require concrete file references when defects exist

#### Repair mode

- explicitly state that only listed issues may be fixed
- require summarizing how each issue was addressed

## 6. Add Built-In Review Loops

Add controller-level review orchestration to `delegate-to-kimi.py`.

### 6.1 New flag

- `--max-review-loops <n>`

Default:

- `2`

Recommended upper bound:

- `5`

### 6.2 Flow

For implementation slices:

1. run `implement`
2. run local validation
3. run `review`
4. if review returns defects:
   - generate repair report artifact or sidecar
   - run `repair`
   - re-run validation
   - re-run `review`
5. stop when:
   - review returns `NO_DEFECTS`
   - validation passes
   - loop limit reached

### 6.3 Failure policy

If loop limit is reached:

- set sidecar status to failed
- record last review findings
- preserve transcripts

## 7. Add Trust Degradation Policy

Add worker-quality handling rules inside `delegate-to-kimi.py`.

### 7.1 Trigger conditions

Mark an implementation pass as low-trust if any occur:

- syntax-broken output in owned files
- clear ownership violation
- broad unrelated changes
- malformed output contract

### 7.2 Response

After low-trust trigger:

- do not reuse the same persistent session for further implementation
- force next attempt to use one-shot `exec`
- optionally restrict the same worker to review-only role for the remainder of the slice

### 7.3 Sidecar recording

Record:

- `trustLevel`
- `trustEvents`
- `forcedSessionPolicy`

## 8. Improve ACP Evidence Capture

Add transcript persistence under:

- `/.codex/rlm/<run-id>/evidence/acp/`

### 8.1 Required files per attempt

- prompt text
- stdout text
- stderr text
- invocation metadata JSON
- session status snapshot JSON before/after run

### 8.2 Invocation metadata JSON fields

- `mode`
- `sessionPolicy`
- `agent`
- `sessionName`
- `attempt`
- `cwd`
- `startTime`
- `endTime`
- `returnCode`
- `usedValidationReport`
- `ownedWriteFiles`
- `requiredWorktreeChanges`

### 8.3 Benefits

- easier debugging of bad runs
- easier comparison of `exec` vs persistent reliability
- easier reproduction of failures

## 9. Strengthen Validation Logic

Extend `scripts/lib/completion_check.py`.

### 9.1 Ownership validation

New checks:

- changed tracked files must be a subset of `Owned Write Files`
- `Required Worktree Changes` must be satisfied
- in review mode, tracked writes must be absent unless explicitly allowed

### 9.2 Dirty-worktree-safe validation

Current validation relies heavily on `baselineHead`.

Add starting snapshot support:

- store file hash snapshots for owned write files at delegation start
- compare current file hashes against those snapshots
- distinguish:
  - pre-existing dirty state
  - actual delegated modifications

### 9.3 Output-contract validation

Add validation per output contract:

- `handoff_outcome`
- `defects_or_no_defects`
- `repair_summary`
- `patch_plan`

### 9.4 Optional gates

Add optional strict gates:

- syntax check
- typecheck
- test command success

These should be configurable in the handoff.

## 10. Strengthen Windows Execution Policy

Extend both documentation and prompt rules.

### 10.1 Policy

On Windows:

- always inject MCP argv-runner for delegated Phase 4
- optionally inject it for any mode that allows verification commands
- explicitly forbid shell patterns like:
  - `cd ... && ...`
  - combined single-string terminal commands

### 10.2 Prompt wording

Add explicit instruction:

- do not use ACP shell/terminal patterns that rely on combined command strings
- use `rlm_run_command` with argv splitting whenever execution is required

### 10.3 Validation

If evidence JSON exists, ensure:

- `argv` is present
- `cwd` matches assigned worktree
- no shell-string fallback was used when MCP runner was required

## 11. Add Slice-Aware Delegation

Support multiple narrow handoffs per run instead of one broad delegated phase.

### 11.1 Model

Allow:

- one handoff per sub-phase or slice
- one state sidecar per slice
- one evidence folder per slice

Example:

- `02.5-acp-handoff.sp1.lock.md`
- `02.5-acp-handoff.sp2.lock.md`
- `02.5-acp-handoff.sp3.lock.md`

### 11.2 Benefits

- disjoint file ownership
- clearer validation
- safer parallelism
- easier review loops

## 12. Add Reviewer-Specific Validation

Review mode should not be forced into implementation-style artifact mutation.

### 12.1 Success conditions for review mode

A review pass is valid if:

- output contract is satisfied
- response is either `NO_DEFECTS` or valid defect list
- owned write constraints were respected

### 12.2 Optional review artifact

If requested by handoff:

- the reviewer may write a review artifact under the run folder

Otherwise:

- transcript evidence alone is sufficient

## 13. CLI Changes

Add flags to `scripts/delegate-to-kimi.py`:

- `--mode`
- `--session-policy`
- `--max-review-loops`
- `--role-template`
- `--save-transcript`
- `--owned-write-files`
- `--allowed-read-paths`
- `--output-contract`

Add corresponding support in wrappers:

- `scripts/delegate-to-kimi.ps1`

## 14. Smoke-Test Expansion

Extend `scripts/smoke-acp-functional.py`.

### 14.1 New scenarios

- one-shot review mode
- one-shot implement mode
- persistent implement mode
- validation-failure repair loop
- reviewer-finding repair loop
- ownership violation rejection
- dirty-worktree-safe validation
- disjoint multi-slice delegation

### 14.2 Success criteria

Each scenario must verify:

- correct mode/session policy used
- expected artifacts and evidence created
- ownership rules enforced
- validation result matches expected outcome

## 15. Documentation Changes

Update:

- `SKILL.md`
- `README.md`
- `references/agents-block.md`

### 15.1 Documented guidance

Add explicit recommendations:

- default to one-shot `exec` for bounded work
- use persistent sessions only when multi-turn context is required
- use review mode before trusting complex-file implementation
- treat Kimi as bounded contributor; supervisor remains integrator

## Implementation Plan

### Phase A. Session and transcript foundation

Files:

- `scripts/lib/acpx_runner.py`
- `scripts/delegate-to-kimi.py`

Changes:

- add `run_agent_exec()`
- add session-policy selection
- capture transcripts and invocation metadata

### Phase B. Handoff schema extension

Files:

- `scripts/lib/handoff_parser.py`
- `scripts/delegate-to-kimi.py`
- docs/templates as needed

Changes:

- add new handoff sections
- validate ownership/output contract fields

### Phase C. Role-aware prompt composition

Files:

- `scripts/delegate-to-kimi.py`
- `agents/repairer.md` new

Changes:

- compose role templates into ACP prompt building
- add mode-specific instruction layers

### Phase D. Validation upgrades

Files:

- `scripts/lib/completion_check.py`
- `scripts/lib/validation_report.py`

Changes:

- ownership validation
- review-mode validation
- dirty-worktree-safe snapshot support
- output-contract validation

### Phase E. Automated review loops

Files:

- `scripts/delegate-to-kimi.py`

Changes:

- implement -> validate -> review -> repair loop orchestration
- trust degradation behavior

### Phase F. Smoke tests and docs

Files:

- `scripts/smoke-acp-functional.py`
- `README.md`
- `SKILL.md`
- `references/agents-block.md`

Changes:

- add new scenarios
- document recommended usage patterns

## Acceptance Criteria

- bounded review tasks can run successfully in one-shot `exec` mode
- implement/review/repair modes are distinct and validated differently
- ownership is explicitly declared and machine-enforced
- review loops can run automatically up to a configured maximum
- transcript evidence is persisted for every ACP attempt
- dirty worktree state does not produce false ownership failures
- Windows verification continues to use argv-safe MCP execution
- smoke tests cover the new execution modes and loop behavior

## Non-Goals

- building a distributed worker scheduler
- generalized merge orchestration
- replacing repo-mediated validation with ACP-only trust
- making ACP the durable record instead of the repo/worktree artifacts

## Recommended Default Policy

For future RLM ACP usage:

- implementation slices: one-shot `exec` unless explicitly marked multi-turn
- review passes: always one-shot `exec`
- repair passes: always one-shot `exec`
- persistent sessions: opt-in, not default
- complex risky files: review-first, then implement, then review again

## Notes From Run 61

This spec is directly informed by a real delegation run where:

- a bounded status/top-bar slice was useful
- a settings/gateway slice was partially useful
- an early ChatWindow implementation pass was bad and had to be rejected
- later ACP review passes were more reliable than ACP implementation for the risky file

That outcome supports the design choice to:

- make review mode first-class
- prefer one-shot execution for bounded tasks
- make ownership stricter
- add trust degradation and repair loops
