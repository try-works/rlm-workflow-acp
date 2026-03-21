# ACP End-to-End Audit Remediation Plan

## Document Control

- Scope: Fix all findings from the latest end-to-end repo audit
- Repository: `D:/DEV/rlm-workflow-acp`
- Primary surfaces:
  - `scripts/smoke-acp-functional.py`
  - `scripts/lib/completion_check.py`
  - `scripts/delegate-to-kimi.py`
  - `scripts/delegate-to-kimi.ps1`
  - `README.md`
  - `SKILL.md`
- Goal: align actual behavior, smoke validation, CLI semantics, and operator docs

## Audit Findings To Remediate

1. Positive validation-only smoke scenarios are broken because the harness does not generate the required verification evidence JSON.
2. Review output contract validation is too weak and can false-pass on arbitrary bullet lists.
3. `--save-transcript` is documented and surfaced as optional, but the implementation always saves transcripts.
4. `delegate-to-kimi.ps1` is not behaviorally equivalent to the Python entrypoint for `--max-review-loops 0`.

## Target Outcome

After this remediation:

- All documented validation-only smoke scenarios pass end to end without ACP.
- Review-mode contract validation requires a true machine-checkable reviewer signal.
- Transcript behavior is either actually optional or intentionally mandatory, with CLI and docs matching reality.
- PowerShell and Python entrypoints behave the same for loop control.
- `README.md` and `SKILL.md` describe the shipped behavior precisely.

## Remediation Strategy

Implement in four milestones so failures are isolated and easy to verify:

1. Fix smoke harness evidence generation first.
2. Tighten review contract validation next.
3. Resolve transcript flag semantics and wrapper parity.
4. Update docs and re-run the full verification ring.

## Milestone 1: Repair Positive Smoke Scenarios

### Problem

The smoke harness writes placeholder verification metadata into `03-implementation-summary.md` and `04-test-summary.md`, but does not create the evidence JSON file required by `verify_acp_completion()`.

### Files

- `scripts/smoke-acp-functional.py`
- `tests/test_smoke_acp_functional.py`
- `tests/test_completion_check.py`
- `README.md`
- `SKILL.md`

### Changes

1. Add a smoke helper that writes a real deterministic evidence JSON file under the current run folder:
   - `/.codex/rlm/<run-id>/evidence/logs/acp-verification.json`
2. Ensure the helper records:
   - `argv`
   - `cwd`
   - `exitCode`
   - `outputSha256`
3. Update smoke artifact generation so the written `Verification Output Sha256` matches the generated evidence JSON.
4. Remove the fake hardcoded manual evidence path from validation-only smoke artifacts.
5. Keep the positive validation-only scenarios ACP-free, but make them satisfy the same repo-mediated contract as real delegated runs.

### Acceptance Criteria

- `dirty-baseline-validation` passes end to end.
- `multi-slice-disjoint` passes end to end.
- `ownership-violation` still fails/passes exactly as intended.
- No validation-only smoke scenario relies on fake placeholder evidence paths.

### Tests

- Add/extend unit tests for smoke evidence file generation.
- Run:
  - `python scripts/smoke-acp-functional.py --scenario dirty-baseline-validation --run-id audit-dirty-fix`
  - `python scripts/smoke-acp-functional.py --scenario multi-slice-disjoint --run-id audit-slice-fix`
  - `python scripts/smoke-acp-functional.py --scenario ownership-violation --run-id audit-own-fix`

## Milestone 2: Strengthen Review Output Contract Validation

### Problem

The current review contract accepts any flat bullet list, which can false-pass TODO/checklist prose that is not an actual reviewer verdict.

### Files

- `scripts/lib/completion_check.py`
- `tests/test_completion_check.py`
- `tests/test_delegate_loops.py`
- `README.md`
- `SKILL.md`

### Changes

1. Tighten `defects_or_no_defects` validation to require one of:
   - exact `NO_DEFECTS`
   - a flat defect list where each item matches a stricter reviewer pattern
2. Define the stricter defect-list contract in code and docs.
3. Recommended minimum reviewer defect shape:
   - file/path or artifact reference
   - a concrete issue statement
4. Reject generic TODO/checklist bullets that do not qualify as defects.
5. Preserve transcript-only review validation support, but make it contract-driven instead of formatting-accidental.

### Acceptance Criteria

- `NO_DEFECTS` still passes.
- Real defect lists still pass.
- Generic bullets like “- verify tests” or “- update docs” fail review-contract validation.
- Review loop tests still pass with the stronger contract.

### Tests

- Add positive tests for valid defect lists.
- Add negative tests for checklist/TODO-style bullets.
- Re-run:
  - `python -m unittest discover -s tests -q`

## Milestone 3: Align Transcript and Wrapper Semantics

### Problem A

`--save-transcript` is exposed as optional but the implementation always enables it.

### Problem B

`delegate-to-kimi.ps1` cannot explicitly forward `--max-review-loops 0`, so the Python default of `2` leaks through.

### Files

- `scripts/delegate-to-kimi.py`
- `scripts/delegate-to-kimi.ps1`
- `tests/test_delegate_prompt.py`
- `tests/test_delegate_loops.py`
- `README.md`
- `SKILL.md`

### Decision Required In Implementation

Choose one transcript policy and make all surfaces match:

- Option A: transcripts are mandatory for all ACP attempts
- Option B: transcripts are actually optional and only written when requested

Default recommendation:

- Keep transcripts mandatory, because they materially support repo-mediated validation and auditability.
- Remove the illusion of optional behavior from CLI help and docs.

### Changes

1. If transcripts remain mandatory:
   - remove `--save-transcript` from the CLI and PowerShell wrapper, or
   - keep it only as a backward-compatible no-op with explicit “always on” help text
2. If transcripts become truly optional:
   - stop forcing `save_transcript=True`
   - preserve contract behavior for modes that depend on transcript-only validation
3. Fix `delegate-to-kimi.ps1` so `-MaxReviewLoops 0` is forwarded explicitly when set by the operator.
4. Ensure Python and PowerShell wrappers have aligned defaults and explicit override semantics.

### Acceptance Criteria

- Transcript behavior is consistent across implementation, help text, `README.md`, and `SKILL.md`.
- PowerShell can explicitly disable review loops.
- Wrapper parity is covered by tests.

### Tests

- Add/extend tests for wrapper argument forwarding.
- Add/extend tests for transcript policy expectations.
- Run:
  - `python scripts/delegate-to-kimi.py --help`
  - PowerShell parse/argument coverage tests if present

## Milestone 4: Documentation and Operator Guidance Sync

### Files

- `README.md`
- `SKILL.md`

### Required Documentation Updates

1. Update the functional smoke section in `README.md`:
   - describe which scenarios are validation-only
   - state that positive validation-only scenarios now generate deterministic verification evidence
   - keep scenario descriptions aligned with actual behavior
2. Update the review output contract guidance in both docs:
   - define what qualifies as a valid defect list
   - note that checklist/TODO bullets are not sufficient
3. Update transcript guidance:
   - if mandatory, say so explicitly
   - if optional, document exactly when transcripts are required
4. Update wrapper/operator guidance:
   - explain loop control semantics consistently across Python and PowerShell entrypoints

### Acceptance Criteria

- No doc claims a behavior that the code does not implement.
- `README.md` and `SKILL.md` agree on:
  - smoke scenario behavior
  - review contract rules
  - transcript behavior
  - loop control semantics

## Verification Plan

### Unit and Static Verification

- `python -m py_compile scripts\\delegate-to-kimi.py scripts\\delegate-to-kimi.ps1 scripts\\smoke-acp-functional.py scripts\\lib\\completion_check.py`
- `python -m unittest discover -s tests -q`

### Functional Smoke Verification

- `python scripts\\smoke-acp-functional.py --list-scenarios`
- `python scripts\\smoke-acp-functional.py --scenario ownership-violation --run-id audit-own-fix`
- `python scripts\\smoke-acp-functional.py --scenario dirty-baseline-validation --run-id audit-dirty-fix`
- `python scripts\\smoke-acp-functional.py --scenario multi-slice-disjoint --run-id audit-slice-fix`

### Manual CLI Verification

- `python scripts\\delegate-to-kimi.py --help`
- Verify PowerShell wrapper parity for:
  - transcript flag/help behavior
  - `-MaxReviewLoops 0`

## Risks

- Tightening review contract parsing may invalidate existing review artifacts used in historical smoke/manual runs.
- Changing transcript semantics may break operator expectations if the transition is not documented clearly.
- Smoke harness changes must avoid introducing ACP dependencies into validation-only scenarios.

## Mitigations

- Keep defect parsing explicit and documented.
- Prefer transcript-always-on unless there is a strong reason to make it optional.
- Cover every changed behavioral rule with a focused unit test plus one end-to-end smoke execution.

## Recommended Implementation Order

1. Milestone 1: smoke evidence generation
2. Milestone 2: review contract tightening
3. Milestone 3: transcript/wrapper parity
4. Milestone 4: final docs sync and verification

## Done Definition

This remediation is complete only when all of the following are true:

- All four audited findings are fixed in code.
- `README.md` and `SKILL.md` are updated in the same change set.
- The full unit test suite passes.
- The validation-only smoke scenarios pass end to end.
- The final docs describe shipped behavior exactly, without advisory/optional wording that contradicts implementation.
