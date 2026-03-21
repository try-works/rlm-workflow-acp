from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.handoff_parser import parse_handoff_markdown  # noqa: E402


def _handoff_text(*, extra_sections: str = "", output_contract: str = "handoff_outcome") -> str:
    return textwrap.dedent(
        f"""\
        # ACP Handoff

        Run ID: run-61
        Delegated Phases: 3,4
        Delegation Origin: unit-test
        Delegation Role: reviewer
        Phase: 02.5 ACP Handoff
        Requirement IDs: R1
        Assigned Worktree Path: /tmp/worktree
        Assigned Branch: stage
        Created At: 2026-03-21T00:00:00Z

        ## Lock
        Algorithm: sha256
        Hash: abc123

        ## Input Artifacts
        - `/.codex/rlm/run-61/02-to-be-plan.md`

        ## Required Artifact Updates
        - `/.codex/rlm/run-61/03.5-code-review.md`

        ## Owned Write Files
        - `/.codex/rlm/run-61/03.5-code-review.md`

        ## Allowed Read Paths
        - `/.codex/rlm/run-61/02-to-be-plan.md`
        - `/.codex/rlm/run-61/03.5-code-review.md`

        ## Output Contract
        {output_contract}

        ## Current Worktree State Rules
        - continue from the current assigned worktree state

        ## Scope In
        - update the review artifact only

        ## Scope Out
        - no tracked files outside the owned set

        ## Required Verification
        - Review only
        - Evidence JSON: `/.codex/rlm/run-61/evidence/logs/review.json`

        ## Artifact Ownership
        - Worker must write its own changes

        ## Stop Conditions
        - Stop on ambiguity

        ## Completion Conditions
        - Produce output matching the contract

        ## Review Questions
        - Is the diff safe?

        ## Multi-Turn Requirement
        not required
        {extra_sections}
        """
    )


class HandoffParserTests(unittest.TestCase):
    def test_parses_extended_schema(self) -> None:
        doc = parse_handoff_markdown(_handoff_text(output_contract="defects_or_no_defects"))
        self.assertEqual(doc.delegation_role, "reviewer")
        self.assertEqual(doc.output_contract, "defects_or_no_defects")
        self.assertEqual(doc.review_questions, ["Is the diff safe?"])
        self.assertFalse(doc.multi_turn_required)
        self.assertEqual(doc.owned_write_files, ["/.codex/rlm/run-61/03.5-code-review.md"])

    def test_defaults_owned_write_files_to_artifacts_and_changes(self) -> None:
        legacy = textwrap.dedent(
            """\
            # ACP Handoff

            Run ID: run-61
            Delegated Phases: 3
            Delegation Origin: unit-test
            Phase: 02.5 ACP Handoff
            Requirement IDs: R1
            Assigned Worktree Path: /tmp/worktree
            Assigned Branch: stage
            Created At: 2026-03-21T00:00:00Z

            ## Lock
            Algorithm: sha256
            Hash: abc123

            ## Input Artifacts
            - `/.codex/rlm/run-61/02-to-be-plan.md`

            ## Required Artifact Updates
            - `/.codex/rlm/run-61/03-implementation-summary.md`

            ## Required Worktree Changes
            - `app/example.ts`

            ## Current Worktree State Rules
            - continue from the current assigned worktree state

            ## Scope In
            - update the implementation summary and source file

            ## Scope Out
            - no unrelated files

            ## Required Verification
            - Run focused verification

            ## Artifact Ownership
            - Worker must write its own changes

            ## Stop Conditions
            - Stop on ambiguity

            ## Completion Conditions
            - Produce output matching the contract
            """
        )
        doc = parse_handoff_markdown(legacy)
        self.assertEqual(
            doc.owned_write_files,
            ["/.codex/rlm/run-61/03-implementation-summary.md", "app/example.ts"],
        )
        self.assertEqual(
            doc.allowed_read_paths,
            ["/.codex/rlm/run-61/02-to-be-plan.md", "/.codex/rlm/run-61/03-implementation-summary.md", "app/example.ts"],
        )

    def test_rejects_invalid_output_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "Output Contract must be one of"):
            parse_handoff_markdown(_handoff_text(output_contract="essay"))


if __name__ == "__main__":
    unittest.main()
