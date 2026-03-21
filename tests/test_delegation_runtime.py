from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.acpx_runner import AcpxResult  # noqa: E402
from scripts.lib.delegation_runtime import resolve_session_policy, write_acp_transcript  # noqa: E402


class DelegationRuntimeTests(unittest.TestCase):
    def test_resolve_session_policy_auto_defaults(self) -> None:
        self.assertEqual(resolve_session_policy(mode="implement", session_policy="auto"), "exec")
        self.assertEqual(resolve_session_policy(mode="review", session_policy="auto"), "exec")
        self.assertEqual(resolve_session_policy(mode="repair", session_policy="auto"), "exec")

    def test_resolve_session_policy_honors_explicit_choice(self) -> None:
        self.assertEqual(resolve_session_policy(mode="review", session_policy="persistent"), "persistent")
        self.assertEqual(resolve_session_policy(mode="implement", session_policy="exec"), "exec")

    def test_resolve_session_policy_keeps_persistent_for_multi_turn_implement(self) -> None:
        self.assertEqual(
            resolve_session_policy(mode="implement", session_policy="auto", multi_turn_required=True),
            "persistent",
        )

    def test_write_acp_transcript_writes_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            result = AcpxResult(
                returncode=0,
                stdout="hello\n",
                stderr="warn\n",
                requested_argv=["acpx", "kimi", "exec"],
                invoked_argv=["acpx", "kimi", "exec"],
                cwd=str(run_dir),
                started_at="2026-03-21T01:00:00Z",
                completed_at="2026-03-21T01:00:01Z",
                agent="kimi",
                session_name=None,
                execution_kind="exec",
                approval_mode="approve-all",
            )
            bundle_dir = write_acp_transcript(
                run_dir=run_dir,
                attempt=2,
                mode="review",
                session_policy="exec",
                prompt_text="check this diff",
                result=result,
                session_status_before=None,
                session_status_after=None,
                extra_metadata={"ownedWriteFiles": ["app/example.ts"], "usedValidationReport": "report.md"},
            )

            self.assertTrue((bundle_dir / "prompt.txt").exists())
            self.assertTrue((bundle_dir / "stdout.txt").exists())
            self.assertTrue((bundle_dir / "stderr.txt").exists())
            self.assertTrue((bundle_dir / "invocation.json").exists())
            self.assertEqual((bundle_dir / "prompt.txt").read_text(encoding="utf-8"), "check this diff")
            metadata = json.loads((bundle_dir / "invocation.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["mode"], "review")
            self.assertEqual(metadata["sessionPolicy"], "exec")
            self.assertEqual(metadata["attempt"], 2)
            self.assertEqual(metadata["result"]["execution_kind"], "exec")
            self.assertEqual(metadata["ownedWriteFiles"], ["app/example.ts"])
            self.assertEqual(metadata["usedValidationReport"], "report.md")


if __name__ == "__main__":
    unittest.main()
