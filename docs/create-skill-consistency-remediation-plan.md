# /create-skill Consistency Remediation Plan

## Document Control

- Repository: `D:/DEV/rlm-workflow-acp`
- Reference standard: `/create-skill` guidance from `C:/Users/erikb/.codex/skills/.system/skill-creator/SKILL.md`
- Objective: make this repo consistent with the expectations for a well-formed installable skill while preserving the current source-repo development workflow

## Summary of Current Gaps

1. The repo root is simultaneously the source repo and the installable root skill.
   - `/create-skill` says a skill folder should not contain extraneous repo-only files like `README.md`, planning docs, changelogs, and similar auxiliary material.
   - This repo currently ships the root skill from the repository root, which also contains `README.md`, `docs/`, `tests/`, and development-only scripts.

2. The root `SKILL.md` is too large and carries too much detail directly in the body.
   - `/create-skill` recommends keeping `SKILL.md` lean and using progressive disclosure, ideally under 500 lines.
   - Current sizes are materially larger:
     - root `SKILL.md`: 881 lines
     - `skills/rlm-debugging/SKILL.md`: 477 lines
     - `skills/rlm-subagent/SKILL.md`: 389 lines
     - `skills/rlm-worktree/SKILL.md`: 543 lines

3. The repo references `references/openai_yaml.md`, but that file does not exist.
   - `/create-skill` explicitly says to read that reference before generating `agents/openai.yaml`.
   - This repo currently has no local copy of that reference.

4. The root skill has `agents/openai.yaml`, but the subskills do not.
   - `/create-skill` marks `agents/openai.yaml` as recommended.
   - For a multi-skill repo, consistency is better if each distributed skill either has UI metadata or there is a documented reason it does not.

5. Triggering/use guidance is split between frontmatter and body in a way that is heavier than `/create-skill` recommends.
   - `/create-skill` says “when to use” information belongs primarily in frontmatter because that is the trigger surface.
   - The current root and subskills still include substantial “When to Use” and workflow prose in the body that can be compressed or moved to references.

6. Progressive-disclosure boundaries are weak.
   - A lot of detailed canonical workflow, examples, and policy text is embedded directly in the skill bodies instead of being pushed down into `references/`.
   - That increases context cost after triggering.

## Target State

After remediation:

- The installable root skill lives in a lean dedicated skill folder, separate from repo-only docs/tests/source material.
- Every installable skill has:
  - a concise `SKILL.md`
  - frontmatter with `name` and `description` only
  - `agents/openai.yaml` metadata
  - references/scripts/assets only where justified
- `README.md` becomes repo-level maintainer documentation, not part of the installable skill payload.
- The main `SKILL.md` for the installable root skill is concise and uses references for deep detail.
- The repo includes the missing metadata reference needed to maintain `agents/openai.yaml` correctly.

## Remediation Strategy

Implement in four milestones:

1. Separate source-repo concerns from installable-skill concerns.
2. Restore skill metadata/reference completeness.
3. Refactor `SKILL.md` files for progressive disclosure.
4. Update repo docs and validation workflow.

## Milestone 1: Separate the Installable Root Skill from the Source Repo

### Problem

The current repo root violates `/create-skill`’s “What to Not Include in a Skill” guidance because the installable root skill is mixed with repository maintenance files.

### Proposed Change

Move the installable root skill into a dedicated folder:

- `skill-root/rlm-workflow-acp/`
  - `SKILL.md`
  - `agents/openai.yaml`
  - `scripts/` (only installable/runtime scripts)
  - `references/`
  - `assets/` if needed

Alternative acceptable structure:

- `skills/rlm-workflow-acp/`

Then make the repository root a pure source/dev repo:

- repo-level `README.md`
- repo-level `tests/`
- repo-level `docs/`
- source-generation and maintenance scripts

### Required Changes

1. Create a dedicated folder for the root installable skill.
2. Move installable skill files there:
   - root `SKILL.md`
   - root `agents/openai.yaml`
   - skill runtime scripts/references needed at install time
3. Update install/bootstrap scripts to point at the new root skill folder.
4. Update installation examples in `README.md`.
5. Ensure subskills remain installable from `skills/*`.

### Acceptance Criteria

- The installable root skill folder does not contain repo-only files like `README.md`, `docs/`, or `tests/`.
- The repo root is clearly source/dev-only.
- Skills CLI installation instructions still work for:
  - root skill
  - subskills
  - full-depth listing/install

## Milestone 2: Restore Metadata and Reference Completeness

### Problem A

The repo references `references/openai_yaml.md` but does not contain it.

### Problem B

Subskills are missing `agents/openai.yaml`.

### Required Changes

1. Add `references/openai_yaml.md` to the repo or vendor the required local equivalent.
2. Generate or regenerate `agents/openai.yaml` for:
   - root skill
   - `skills/rlm-tdd`
   - `skills/rlm-debugging`
   - `skills/rlm-subagent`
   - `skills/rlm-worktree`
3. Ensure metadata is derived from the skill content and kept deterministic.
4. Add a maintainer note or script path for regenerating stale metadata.

### Acceptance Criteria

- Every installable skill has `agents/openai.yaml`.
- The referenced metadata guidance file exists in the repo.
- `agents/openai.yaml` content aligns with the skill body and frontmatter.

## Milestone 3: Progressive Disclosure Refactor

### Problem

The current `SKILL.md` files are too large and embed too much detail directly in the body.

### Required Changes

1. Reduce the root installable `SKILL.md` to the essential trigger/workflow instructions.
2. Move detailed procedural and reference material into `references/` files.
3. Apply the same treatment to oversized subskills:
   - `skills/rlm-worktree/SKILL.md`
   - `skills/rlm-debugging/SKILL.md`
   - optionally `skills/rlm-tdd/SKILL.md` if simplification is beneficial
4. Remove redundant “when to use” duplication from bodies when it is already captured in frontmatter.
5. Ensure reference files are one hop away from `SKILL.md` and clearly named.

### Suggested Reference Split

For the root skill:

- `references/workflow-core.md`
- `references/hard-gates.md`
- `references/acp-delegation.md`
- `references/phase-protocol.md`
- `references/lock-verification.md`

For subskills:

- worktree:
  - `references/directory-selection.md`
  - `references/baseline-verification.md`
- debugging:
  - `references/root-cause-workflow.md`
  - `references/evidence-gathering.md`
- subagent:
  - `references/parallel-mode.md`
  - `references/sequential-fallback.md`

### Acceptance Criteria

- Root installable `SKILL.md` is materially shorter and focused on trigger + navigation + mandatory workflow.
- Oversized subskill bodies are trimmed and rely on references.
- The skill bodies no longer duplicate large bodies of canonical guidance that can live in references.

## Milestone 4: Documentation and Validation Workflow Sync

### Problem

The repo currently mixes repo-maintainer documentation with installable skill guidance.

### Required Changes

1. Update repo-level `README.md` to explicitly distinguish:
   - source repo / maintainer docs
   - installable skill payload(s)
2. Update the main installable `SKILL.md` to align with `/create-skill` rules:
   - concise body
   - no repo-maintainer prose that belongs in `README.md`
3. Add a maintainer validation checklist for skill compliance:
   - frontmatter fields
   - `agents/openai.yaml`
   - progressive disclosure
   - no extraneous files in installable skill folders
4. If available, add or document a repo-local validation command equivalent to `/create-skill` expectations.

### Acceptance Criteria

- `README.md` accurately describes the source repo and installation model.
- The main installable `SKILL.md` is compliant with the lean-skill design.
- Maintainers have a clear repeatable way to keep the repo aligned with `/create-skill`.

## Verification Plan

### Structural Verification

- Confirm each installable skill folder contains only skill-appropriate files.
- Confirm the repo root is no longer the installable root skill surface.

### Metadata Verification

- Confirm every installable skill has:
  - `SKILL.md`
  - `agents/openai.yaml`
- Confirm `references/openai_yaml.md` exists.

### Content Verification

- Count lines in each `SKILL.md`.
- Review frontmatter fields for `name` + `description` only.
- Confirm key “when to use” guidance is in frontmatter descriptions.

### Installation Verification

- Re-run the documented install/list commands after the restructure.
- Verify root-skill install path and subskill install paths remain valid.

## Risks

- Moving the root installable skill out of the repo root may break current install commands or assumptions.
- Shortening `SKILL.md` too aggressively could remove important procedural guardrails.
- Generating `agents/openai.yaml` for subskills may require careful wording to avoid trigger overlap.

## Mitigations

- Preserve backward-compatible install instructions where possible, or document the migration clearly.
- Move content to references, not delete it.
- Keep the root skill’s frontmatter descriptions explicit enough to trigger correctly.

## Recommended Implementation Order

1. Milestone 1: structural separation of installable root skill
2. Milestone 2: metadata/reference completeness
3. Milestone 3: progressive-disclosure refactor
4. Milestone 4: docs and maintainer validation sync

## Done Definition

This repo is considered consistent with `/create-skill` only when:

- the installable root skill is no longer the repository root
- every installable skill has proper metadata
- the missing metadata guidance reference exists locally
- the main `SKILL.md` and subskills follow progressive-disclosure principles
- `README.md` and the main installable `SKILL.md` are updated to reflect the new structure
