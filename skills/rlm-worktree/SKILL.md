---
name: rlm-worktree
description: Use when starting any RLM requirement to set up an isolated git worktree. Required before Phase 1. Create an isolated workspace, verify the worktree directory is safe to use, run project setup, confirm a clean test baseline, and prevent direct main/master branch work.
---

# RLM Worktree Isolation

Use this skill for Phase 0 worktree setup before any other RLM phase work begins.

## Trigger Examples

- `Create an isolated worktree for this requirement`
- `Set up Phase 0 worktree isolation for run '2026-02-24-add-oauth'`
- `I'm on main; move me to a worktree`
- `Use .worktrees/ and ensure it is git-ignored`

## Non-Negotiable Rules

- Do not proceed with any later RLM phase until the isolated worktree exists.
- Do not work on `main` or `master` without explicit user consent.
- Do not use a project-local worktree directory until it is verified as git-ignored.
- Do not begin changes until setup completes and the baseline test state is known.

## Required Outcome

Produce:

- an isolated worktree on a feature branch such as `rlm/<run-id>`
- a locked `00-worktree.md` artifact recording:
  - chosen worktree location
  - branch name
  - setup commands run
  - baseline validation result

## Directory Selection Order

Select the worktree directory in this order:

1. existing `.worktrees/`
2. existing `worktrees/`
3. documented convention in local repo docs
4. ask the user if no convention exists

Default to `.worktrees/` when no convention exists.

## Required Procedure

1. Detect the current branch.
2. If on `main` or `master`, stop and require explicit consent or create a worktree by default.
3. Resolve the worktree directory using the selection order above.
4. For project-local directories, verify they are git-ignored.
5. Create the worktree and feature branch.
6. Run project setup based on the detected stack.
7. Run the baseline test command or otherwise record the baseline test state.
8. Write `00-worktree.md` with evidence and gate results.

## Required References

Read these only when needed:

- `references/worktree-manual.md`
  - full worktree workflow, branch protection flow, and artifact template
- `../rlm-workflow-acp/references/artifact-template.md`
  - artifact structure and gate patterns

## Safety Checks

- Verify the selected project-local directory is ignored with `git check-ignore`.
- If it is not ignored, fix that before creating the worktree.
- Record any baseline test failures explicitly instead of assuming a clean start.

## Output Discipline

Keep the Phase 0 artifact concrete and reproducible:

- exact worktree path
- exact branch
- exact setup commands
- exact baseline verification result
