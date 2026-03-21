from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "delegate-to-kimi.py"

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load_delegate_module():
    spec = importlib.util.spec_from_file_location("delegate_to_kimi_loops", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load delegate-to-kimi.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DelegateLoopTests(unittest.TestCase):
    def test_extract_review_verdict_supports_no_defects_and_flat_findings(self) -> None:
        module = _load_delegate_module()
        with tempfile.TemporaryDirectory() as tmp:
            transcript_dir = Path(tmp)
            (transcript_dir / "stdout.txt").write_text("NO_DEFECTS\n", encoding="utf-8")
            verdict = module._extract_review_verdict(transcript_dir=transcript_dir)
            self.assertEqual(verdict.verdict, "no_defects")

            (transcript_dir / "stdout.txt").write_text(
                "- file.py:12 parser still accepts TODO bullets as findings\n"
                "- other.py:3 review contract docs do not mention file references\n",
                encoding="utf-8",
            )
            verdict = module._extract_review_verdict(transcript_dir=transcript_dir)
            self.assertEqual(verdict.verdict, "defects")
            self.assertEqual(
                verdict.findings,
                [
                    "file.py:12 parser still accepts TODO bullets as findings",
                    "other.py:3 review contract docs do not mention file references",
                ],
            )

    def test_extract_review_verdict_rejects_checklist_bullets(self) -> None:
        module = _load_delegate_module()
        with tempfile.TemporaryDirectory() as tmp:
            transcript_dir = Path(tmp)
            (transcript_dir / "stdout.txt").write_text("- verify tests\n- update docs\n", encoding="utf-8")
            verdict = module._extract_review_verdict(transcript_dir=transcript_dir)
            self.assertEqual(verdict.verdict, "invalid")
            self.assertEqual(verdict.findings, [])

    def test_run_follow_up_loop_repairs_then_passes_review(self) -> None:
        module = _load_delegate_module()
        review_calls = []
        repair_calls = []

        def run_review():
            review_calls.append("review")
            return {"ok": True, "verdict": "no_defects", "report_path": None}

        def run_repair(report_path):
            repair_calls.append(report_path)
            return {"completion_ok": True, "report_path": None}

        result = module._run_follow_up_loop(
            max_review_loops=2,
            initial_completion_ok=False,
            initial_report_path=Path("validation.md"),
            run_review=run_review,
            run_repair=run_repair,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.loops_used, 1)
        self.assertEqual(repair_calls, [Path("validation.md")])
        self.assertEqual(review_calls, ["review"])

    def test_run_follow_up_loop_stops_when_review_defects_exhaust_budget(self) -> None:
        module = _load_delegate_module()

        def run_review():
            return {"ok": True, "verdict": "defects", "report_path": Path("review.md")}

        def run_repair(report_path):
            return {"completion_ok": True, "report_path": None}

        result = module._run_follow_up_loop(
            max_review_loops=0,
            initial_completion_ok=True,
            initial_report_path=None,
            run_review=run_review,
            run_repair=run_repair,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_reason, "review_defects_remaining")
        self.assertEqual(result.last_report_path, Path("review.md"))

    def test_run_follow_up_loop_repairs_review_findings_then_converges(self) -> None:
        module = _load_delegate_module()
        review_calls = []
        repair_calls = []

        def run_review():
            review_calls.append("review")
            if len(review_calls) == 1:
                return {"ok": True, "verdict": "defects", "report_path": Path("review-findings.md")}
            return {"ok": True, "verdict": "no_defects", "report_path": None}

        def run_repair(report_path):
            repair_calls.append(report_path)
            return {"completion_ok": True, "report_path": None}

        result = module._run_follow_up_loop(
            max_review_loops=2,
            initial_completion_ok=True,
            initial_report_path=None,
            run_review=run_review,
            run_repair=run_repair,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.loops_used, 1)
        self.assertEqual(repair_calls, [Path("review-findings.md")])
        self.assertEqual(review_calls, ["review", "review"])

    def test_collect_trust_events_detects_transport_and_contract_failures(self) -> None:
        module = _load_delegate_module()
        events = module._collect_trust_events(
            problems=[
                "Tracked writes escaped the declared owned write set:",
                "Missing required completion signal section in 03-implementation-summary.md",
            ],
            acp_returncode=1,
        )
        self.assertEqual(events, ["acp_transport_failure", "ownership_violation", "output_contract_violation"])


if __name__ == "__main__":
    unittest.main()
