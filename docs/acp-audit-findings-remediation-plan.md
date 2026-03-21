# ACP Audit Findings Remediation Plan

Status: Draft  
Repo: `D:/DEV/rlm-workflow-acp`  
Depends On:
- `docs/acp-improvement-spec.md`
- `docs/acp-improvement-implementation-plan.md`

## Purpose

This plan addresses the remaining inconsistencies and integrity gaps found in the post-implementation audit of the ACP delegation flow.

It is narrower than the original implementation plan. The original plan focused on building the capability set. This follow-up plan focuses on making the shipped behavior internally consistent, accurately documented, and safe under real operator use.

## Findings To Fix

### F1. `--validate-only` is not fully dirty-worktree-safe on first use

Current issue:

- the normal delegation path captures `baselineDirtyTrackedFiles` and `baselineDirtySnapshots`
- the first-run `--validate-only` path reads those fields if they already exist, but does not initialize them if they do not
- this makes the validation contract weaker than the documented guarantee

Impact:

- a first `--validate-only` run on a dirty worktree can still classify pre-existing dirt as delegated writes

Primary files:

- `scripts/delegate-to-kimi.py`
- `scripts/lib/completion_check.py`
- tests in `tests/test_completion_check.py`

### F2. `--allowed-read-paths` is documented like an enforced control, but is currently advisory

Current issue:

- the CLI, sidecar, transcript metadata, and prompt all carry `allowedReadPaths`
- completion validation does not enforce read constraints
- docs currently imply the flag “narrows or replaces allowed read paths”

Impact:

- operator expectations are stronger than the actual guarantee
- the tool currently offers a policy hint, not an enforcement boundary

Decision:

- keep `--allowed-read-paths` advisory for now
- downgrade docs and skill text to state that explicitly
- postpone true read-path enforcement until a reliable and portable mechanism exists

Primary files:

- `README.md`
- `SKILL.md`
- `references/agents-block.md`
- `scripts/delegate-to-kimi.py`

### F3. `--output-contract` can override the sealed handoff in production-like runs

Current issue:

- the sealed handoff is parsed and hash-verified first
- then `effective_output_contract` may be replaced from CLI flags
- validation may therefore enforce a different contract than the sealed handoff specified

Impact:

- weakens the sealed execution contract
- permits operator drift from the signed handoff
- makes run artifacts less trustworthy

Required outcome:

- treat the sealed handoff as authoritative by default
- require an explicit debug/unsafe override mode before output contract substitution is allowed

Primary files:

- `scripts/delegate-to-kimi.py`
- `scripts/lib/handoff_parser.py`
- tests in `tests/test_delegate_prompt.py` and/or new delegate CLI tests
- docs in `README.md` and `SKILL.md`

### F4. The `multi-slice-disjoint` smoke scenario over-claims what it proves

Current issue:

- the scenario creates multiple slices
- but only validates `sp1`
- it does not prove that `sp1` and `sp2` are independently enforced
- it does not include a negative case for cross-slice ownership leakage

Impact:

- docs claim stronger isolation proof than the harness actually supplies

Required outcome:

- either reduce the claim in docs
- or improve the smoke scenario so it validates both slices and a cross-slice violation case

Preferred direction:

- improve the harness so the claim becomes true

Primary files:

- `scripts/smoke-acp-functional.py`
- `tests/test_smoke_acp_functional.py`
- `README.md`
- `SKILL.md`

### F5. Custom role-template overrides do not flow consistently into auto review/repair loops

Current issue:

- the initial ACP attempt respects `--role-template`
- auto review/repair follow-up passes only conditionally forward it
- in the common `implement` flow, custom templates do not consistently shape follow-up passes

Impact:

- operator intent is not preserved across the full delegated lifecycle
- prompt behavior is inconsistent across initial vs follow-up attempts

Required outcome:

- define explicit behavior for role-template propagation
- make it deterministic and documented

Preferred direction:

- allow a supplied role template path or role alias to propagate to all attempts unless the operator explicitly disables inheritance

Primary files:

- `scripts/delegate-to-kimi.py`
- tests in `tests/test_delegate_prompt.py`
- docs in `README.md` and `SKILL.md`

## Success Criteria

This remediation plan is complete when all of the following are true:

- first-run `--validate-only` initializes dirty-worktree baseline state before validation
- docs clearly state that `--allowed-read-paths` is advisory, not machine-enforced
- `--output-contract` cannot silently override the sealed handoff during normal production use
- multi-slice smoke coverage proves both positive isolation and negative cross-slice rejection, or the docs are narrowed accordingly
- custom role-template behavior is deterministic across initial, review, and repair attempts
- `README.md` and the main `SKILL.md` both accurately describe the final behavior

## Constraints

- preserve the sealed handoff as the default source of truth
- preserve repo-mediated validation as the completion authority
- preserve Windows/macOS/Linux support
- do not introduce shell-dependent enforcement features that only work on one platform
- keep PowerShell wrappers as wrappers; Python entrypoints remain the canonical implementation path

## Implementation Strategy

1. fix contract integrity first
2. fix validation baseline capture second
3. fix smoke-proof quality third
4. update docs and skill text last, after code behavior is settled

## Work Breakdown

## Phase 1. Seal Contract Integrity

Goal:

- remove or gate unsafe CLI overrides that weaken the sealed handoff contract

Tasks:

- change `scripts/delegate-to-kimi.py` so `--output-contract` does not override the sealed handoff during normal execution
- add an explicit escape hatch such as:
  - `--allow-sealed-override`
  - or `--debug-override-contract`
- require a visible sidecar/transcript marker when the unsafe override path is used
- reject mismatched override attempts by default

Files:

- `scripts/delegate-to-kimi.py`
- `scripts/delegate-to-kimi.ps1`
- docs:
  - `README.md`
  - `SKILL.md`

Tests:

- normal run: CLI output-contract mismatch is rejected
- explicit debug override: mismatch is allowed and recorded
- sidecar/transcript metadata includes the override decision

Acceptance Gate:

- production-default behavior cannot validate against a different contract than the sealed handoff

## Phase 2. Fix First-Run Validate-Only Baseline Capture

Goal:

- make `--validate-only` honor the same dirty-worktree-safe rules as the normal delegation path

Tasks:

- initialize `baselineHead` if missing in the validate-only branch
- initialize `baselineDirtyTrackedFiles` and `baselineDirtySnapshots` if missing
- persist that state before running completion validation
- ensure repeated validate-only calls reuse the captured baseline rather than drifting

Files:

- `scripts/delegate-to-kimi.py`
- `scripts/lib/completion_check.py`

Tests:

- validate-only on a clean tree
- first validate-only on a dirty tree with unrelated dirt
- repeated validate-only on the same dirty tree
- validate-only where a pre-existing dirty file changes again after baseline capture

Acceptance Gate:

- first-run validate-only is behaviorally consistent with the non-validate delegation path

## Phase 3. Clarify Advisory Read-Path Behavior

Goal:

- eliminate over-claiming around `--allowed-read-paths`

Tasks:

- update docs to state that `--allowed-read-paths` is:
  - prompt guidance
  - state/transcript metadata
  - not currently machine-enforced
- update prompt wording, if needed, so it does not imply validator-backed enforcement
- update the skill guidance to distinguish:
  - enforced write ownership
  - advisory read scoping

Files:

- `README.md`
- `SKILL.md`
- `references/agents-block.md`
- optionally `references/plans-canonical.md`

Tests:

- no code-behavior test required unless prompt text changes materially
- if prompt wording changes, add/update prompt tests

Acceptance Gate:

- operator docs do not present advisory read scoping as a hard security/control boundary

## Phase 4. Strengthen Multi-Slice Smoke Coverage

Goal:

- make the `multi-slice-disjoint` claim true

Tasks:

- update `scripts/smoke-acp-functional.py` so the scenario validates:
  - `sp1` success on its owned files
  - `sp2` success on its owned files
  - at least one negative cross-slice case where one slice touches the other slice’s tracked file and is rejected
- ensure scenario output clearly reports which slice passed/failed
- add or update tests to reflect the stronger scenario design

Files:

- `scripts/smoke-acp-functional.py`
- `scripts/smoke-acp-functional.ps1`
- `tests/test_smoke_acp_functional.py`

Docs:

- `README.md`
- `SKILL.md`

Acceptance Gate:

- the smoke harness genuinely proves slice-isolated ownership, not just slice-specific file naming

## Phase 5. Make Role-Template Propagation Deterministic

Goal:

- remove ambiguity about how custom templates apply across follow-up attempts

Tasks:

- define propagation rules:
  - default: propagate custom template configuration across implement/review/repair attempts
  - or split by role with explicit CLI such as separate `--implementer-template`, `--reviewer-template`, `--repairer-template`
- implement the chosen rule consistently in `scripts/delegate-to-kimi.py`
- record the effective template for each attempt in transcript metadata
- document the rule clearly

Preferred option:

- long term: per-role template flags
- short term: inherited template config with explicit metadata

Files:

- `scripts/delegate-to-kimi.py`
- `scripts/lib/delegation_runtime.py` if transcript metadata shape changes
- `tests/test_delegate_prompt.py`

Acceptance Gate:

- an operator can predict exactly which role template is used for every ACP sub-attempt

## Phase 6. Documentation and Skill Alignment

Goal:

- align the operator-facing docs with actual behavior after the code fixes land

Required files:

- `README.md`
- `SKILL.md`

Additional files:

- `references/agents-block.md`
- `references/plans-canonical.md`

Required doc updates:

### README.md

- document that `--allowed-read-paths` is advisory today
- document the new default rule for `--output-contract`:
  - sealed handoff authoritative by default
  - explicit unsafe/debug override required for mismatch
- document first-run validate-only dirty-baseline behavior
- document the stronger `multi-slice-disjoint` scenario, or narrow the claim if implementation is deferred
- document role-template propagation rules across follow-up loops

### SKILL.md

- mirror the same operator guidance from the README
- explicitly distinguish:
  - enforced write ownership
  - advisory read scoping
- document the sealed-contract override policy
- document how slice-aware handoffs and follow-up template inheritance work

Acceptance Gate:

- README and SKILL contain no claims stronger than the code can currently guarantee

## Proposed Delivery Order

### Milestone A

- Phase 1
- Phase 2

Result:

- contract integrity restored
- validate-only baseline consistency fixed

### Milestone B

- Phase 3
- Phase 5

Result:

- operator-facing semantics aligned for advisory read scoping and template inheritance

### Milestone C

- Phase 4
- Phase 6

Result:

- smoke claims are justified
- README and SKILL are fully aligned with shipped behavior

## Test Plan

### Unit tests

- `tests/test_completion_check.py`
  - add validate-only baseline initialization coverage
- `tests/test_delegate_prompt.py`
  - add role-template propagation assertions
- `tests/test_smoke_acp_functional.py`
  - add multi-slice dual-pass and cross-slice rejection assertions

### CLI tests

- `python scripts/delegate-to-kimi.py --help`
- explicit override rejection/acceptance cases

### Smoke harness checks

- `python scripts/smoke-acp-functional.py --list-scenarios`
- scenario-level dry validation for:
  - `dirty-baseline-validation`
  - `multi-slice-disjoint`

## Risks and Mitigations

### Risk 1. Contract override tightening breaks current manual workflows

Mitigation:

- keep an explicit unsafe override path
- document it clearly as non-default behavior

### Risk 2. Multi-slice smoke logic becomes too synthetic

Mitigation:

- prefer small deterministic validation-only scenarios
- avoid depending on live ACP for the slice-isolation proof

### Risk 3. Role-template propagation becomes overly complicated

Mitigation:

- define a simple default inheritance rule first
- defer per-role template matrix support unless operators actually need it

## Completion Definition

This remediation plan is complete when:

- every audit finding above has an implemented fix or an intentional documented downgrade
- `README.md` and `SKILL.md` are both updated in the same change set
- the repo’s claims about slice isolation, baseline safety, and override semantics match the code exactly
