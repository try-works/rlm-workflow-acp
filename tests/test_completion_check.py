from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.completion_check import capture_dirty_worktree_baseline, verify_acp_completion  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


class CompletionCheckTests(unittest.TestCase):
    def test_handoff_outcome_honors_owned_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init")
            _git(repo, "config", "user.email", "tests@example.com")
            _git(repo, "config", "user.name", "Tests")

            run_dir = repo / ".codex" / "rlm" / "run-61"
            run_dir.mkdir(parents=True)
            artifact = run_dir / "03-implementation-summary.md"
            artifact.write_text("seed\n", encoding="utf-8")
            source = repo / "app" / "example.ts"
            source.parent.mkdir(parents=True)
            source.write_text("export const value = 1;\n", encoding="utf-8")

            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "baseline")
            baseline_head = _git(repo, "rev-parse", "HEAD")

            artifact.write_text(
                textwrap.dedent(
                    """\
                    ## ACP Delegation Outcome

                    Status: success
                    Summary: Updated implementation artifact
                    Changed Files:
                    - app/example.ts

                    Verification Run:
                    - Tool: rlm_run_command (MCP argv runner)
                    - Command: ["python", "-m", "pytest"]
                    - Evidence JSON: `/.codex/rlm/run-61/evidence/logs/acp-verification.json`
                    - Verification Output Sha256: abc

                    Blockers: none
                    Out-of-Scope Findings: none
                    """
                ),
                encoding="utf-8",
            )
            source.write_text("export const value = 2;\n", encoding="utf-8")

            result = verify_acp_completion(
                run_dir,
                [artifact],
                worktree_path=repo,
                baseline_head=baseline_head,
                required_worktree_changes=["app/example.ts"],
                owned_write_files=[
                    ".codex/rlm/run-61/03-implementation-summary.md",
                    "app/example.ts",
                ],
                output_contract="handoff_outcome",
            )

            self.assertTrue(result.ok, result.problems)
            self.assertEqual(
                result.changed_tracked_files,
                [".codex/rlm/run-61/03-implementation-summary.md", "app/example.ts"],
            )

    def test_owned_write_violation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init")
            _git(repo, "config", "user.email", "tests@example.com")
            _git(repo, "config", "user.name", "Tests")

            run_dir = repo / ".codex" / "rlm" / "run-61"
            run_dir.mkdir(parents=True)
            artifact = run_dir / "03-implementation-summary.md"
            artifact.write_text(
                "## ACP Delegation Outcome\n\nStatus: success\nVerification: ok\nSummary: ok\n",
                encoding="utf-8",
            )
            source = repo / "app" / "example.ts"
            source.parent.mkdir(parents=True)
            source.write_text("export const value = 1;\n", encoding="utf-8")
            stray = repo / "README.md"
            stray.write_text("baseline\n", encoding="utf-8")

            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "baseline")
            baseline_head = _git(repo, "rev-parse", "HEAD")

            source.write_text("export const value = 2;\n", encoding="utf-8")
            stray.write_text("changed\n", encoding="utf-8")

            result = verify_acp_completion(
                run_dir,
                [artifact],
                worktree_path=repo,
                baseline_head=baseline_head,
                required_worktree_changes=["app/example.ts"],
                owned_write_files=[".codex/rlm/run-61/03-implementation-summary.md", "app/example.ts"],
                output_contract="handoff_outcome",
            )

            self.assertFalse(result.ok)
            self.assertIn("owned write set", "\n".join(result.problems or []).lower())

    def test_review_contract_can_pass_via_transcript_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / ".codex" / "rlm" / "run-61"
            transcript_dir = run_dir / "evidence" / "acp" / "attempt-001"
            transcript_dir.mkdir(parents=True)
            (transcript_dir / "stdout.txt").write_text("NO_DEFECTS\n", encoding="utf-8")

            result = verify_acp_completion(
                run_dir,
                [],
                output_contract="defects_or_no_defects",
                transcript_dir=transcript_dir,
            )

            self.assertTrue(result.ok, result.problems)

    def test_review_contract_accepts_concrete_defect_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / ".codex" / "rlm" / "run-61"
            artifact = run_dir / "03.5-code-review.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                "- pm-auth/model.ts:128 request-time DDL fallback still runs on validate-only\n"
                "- /.codex/rlm/run-61/04-test-summary.md: missing verification output sha\n",
                encoding="utf-8",
            )

            result = verify_acp_completion(
                run_dir,
                [artifact],
                output_contract="defects_or_no_defects",
            )

            self.assertTrue(result.ok, result.problems)

    def test_review_contract_rejects_missing_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / ".codex" / "rlm" / "run-61"
            artifact = run_dir / "03.5-code-review.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("Review pending\n", encoding="utf-8")

            result = verify_acp_completion(
                run_dir,
                [artifact],
                output_contract="defects_or_no_defects",
            )

            self.assertFalse(result.ok)
            self.assertIn("review output contract", "\n".join(result.problems or []).lower())

    def test_review_contract_rejects_checklist_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / ".codex" / "rlm" / "run-61"
            artifact = run_dir / "03.5-code-review.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                "- verify tests\n- update docs\n",
                encoding="utf-8",
            )

            result = verify_acp_completion(
                run_dir,
                [artifact],
                output_contract="defects_or_no_defects",
            )

            self.assertFalse(result.ok)
            joined = "\n".join(result.problems or []).lower()
            self.assertIn("review output contract", joined)
            self.assertIn("verify tests", joined)

    def test_dirty_baseline_ignores_unrelated_preexisting_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init")
            _git(repo, "config", "user.email", "tests@example.com")
            _git(repo, "config", "user.name", "Tests")

            run_dir = repo / ".codex" / "rlm" / "run-61"
            run_dir.mkdir(parents=True)
            artifact = run_dir / "03-implementation-summary.md"
            artifact.write_text(
                "## ACP Delegation Outcome\n\nStatus: success\nVerification: ok\nSummary: ok\n",
                encoding="utf-8",
            )
            source = repo / "app" / "example.ts"
            source.parent.mkdir(parents=True)
            source.write_text("export const value = 1;\n", encoding="utf-8")
            preexisting = repo / "README.md"
            preexisting.write_text("baseline\n", encoding="utf-8")

            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "baseline")
            baseline_head = _git(repo, "rev-parse", "HEAD")

            preexisting.write_text("preexisting dirty change\n", encoding="utf-8")
            baseline_dirty_paths, baseline_dirty_snapshots = capture_dirty_worktree_baseline(
                worktree_path=repo,
                baseline_head=baseline_head,
            )

            artifact.write_text(
                "## ACP Delegation Outcome\n\nStatus: success\nVerification: ok\nSummary: updated after dirty baseline\n",
                encoding="utf-8",
            )
            source.write_text("export const value = 2;\n", encoding="utf-8")

            result = verify_acp_completion(
                run_dir,
                [artifact],
                worktree_path=repo,
                baseline_head=baseline_head,
                baseline_dirty_paths=baseline_dirty_paths,
                baseline_dirty_snapshots=baseline_dirty_snapshots,
                required_worktree_changes=["app/example.ts"],
                owned_write_files=[".codex/rlm/run-61/03-implementation-summary.md", "app/example.ts"],
                output_contract="handoff_outcome",
            )

            self.assertTrue(result.ok, result.problems)
            self.assertEqual(result.changed_tracked_files, [".codex/rlm/run-61/03-implementation-summary.md", "app/example.ts"])

    def test_dirty_baseline_owned_file_counts_when_changed_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init")
            _git(repo, "config", "user.email", "tests@example.com")
            _git(repo, "config", "user.name", "Tests")

            run_dir = repo / ".codex" / "rlm" / "run-61"
            run_dir.mkdir(parents=True)
            artifact = run_dir / "03-implementation-summary.md"
            artifact.write_text(
                "## ACP Delegation Outcome\n\nStatus: success\nVerification: ok\nSummary: ok\n",
                encoding="utf-8",
            )
            source = repo / "app" / "example.ts"
            source.parent.mkdir(parents=True)
            source.write_text("export const value = 1;\n", encoding="utf-8")

            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "baseline")
            baseline_head = _git(repo, "rev-parse", "HEAD")

            source.write_text("export const value = 2;\n", encoding="utf-8")
            baseline_dirty_paths, baseline_dirty_snapshots = capture_dirty_worktree_baseline(
                worktree_path=repo,
                baseline_head=baseline_head,
            )

            artifact.write_text(
                "## ACP Delegation Outcome\n\nStatus: success\nVerification: ok\nSummary: updated after dirty owned file\n",
                encoding="utf-8",
            )
            source.write_text("export const value = 3;\n", encoding="utf-8")

            result = verify_acp_completion(
                run_dir,
                [artifact],
                worktree_path=repo,
                baseline_head=baseline_head,
                baseline_dirty_paths=baseline_dirty_paths,
                baseline_dirty_snapshots=baseline_dirty_snapshots,
                required_worktree_changes=["app/example.ts"],
                owned_write_files=[".codex/rlm/run-61/03-implementation-summary.md", "app/example.ts"],
                output_contract="handoff_outcome",
            )

            self.assertTrue(result.ok, result.problems)
            self.assertEqual(result.changed_tracked_files, [".codex/rlm/run-61/03-implementation-summary.md", "app/example.ts"])


if __name__ == "__main__":
    unittest.main()
