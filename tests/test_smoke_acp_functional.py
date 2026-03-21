from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "smoke-acp-functional.py"

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("smoke_acp_functional_module", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load smoke-acp-functional.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SmokeScenarioTests(unittest.TestCase):
    def test_scenario_registry_contains_milestone_four_variants(self) -> None:
        module = _load_smoke_module()
        self.assertEqual(
            sorted(module.SCENARIOS.keys()),
            [
                "dirty-baseline-validation",
                "implement-exec",
                "implement-persistent",
                "multi-slice-disjoint",
                "ownership-violation",
                "review-exec",
            ],
        )

    def test_delegate_args_include_mode_policy_and_loop_count(self) -> None:
        module = _load_smoke_module()
        scenario = module.SCENARIOS["implement-persistent"]
        ctx = module.RunContext(
            repo_root=Path("/repo"),
            run_id="run-61",
            branch="stage",
            worktree_path=Path("/repo-worktree"),
            session_name="rlm-run-61-kimi",
            fixture_pkg="acp_smoke",
        )
        args = module._delegate_args(ctx, scenario)
        self.assertIn("--mode", args)
        self.assertIn("implement", args)
        self.assertIn("--session-policy", args)
        self.assertIn("persistent", args)
        self.assertIn("--max-review-loops", args)
        self.assertIn("2", args)

    def test_delegate_args_include_slice_when_scenario_declares_one(self) -> None:
        module = _load_smoke_module()
        scenario = module.SCENARIOS["multi-slice-disjoint"]
        ctx = module.RunContext(
            repo_root=Path("/repo"),
            run_id="run-61",
            branch="stage",
            worktree_path=Path("/repo-worktree"),
            session_name="rlm-run-61-kimi",
            fixture_pkg="acp_smoke",
        )
        args = module._delegate_args(ctx, scenario)
        self.assertIn("--slice", args)
        self.assertIn("sp1", args)

    def test_multi_slice_scenario_description_states_positive_and_negative_proof(self) -> None:
        module = _load_smoke_module()
        description = module.SCENARIOS["multi-slice-disjoint"].description
        self.assertIn("sp1 and sp2", description)
        self.assertIn("rejects cross-slice", description)

    def test_prepare_multi_slice_disjoint_validates_both_slices_and_cross_slice_rejection(self) -> None:
        module = _load_smoke_module()
        ctx = module.RunContext(
            repo_root=Path("/repo"),
            run_id="run-61",
            branch="stage",
            worktree_path=Path("/repo-worktree"),
            session_name="rlm-run-61-kimi",
            fixture_pkg="acp_smoke",
        )
        run_dir = Path("/repo/.codex/rlm/run-61")
        validation_calls: list[tuple[bool, str | None, bool]] = []

        def fake_validate(*_args, expect_success: bool, slice_name: str | None, run_tests: bool):
            validation_calls.append((expect_success, slice_name, run_tests))
            return ["ok"], True

        with (
            patch.object(module, "_prime_slice_clean_baseline"),
            patch.object(module, "_write_fixture_files"),
            patch.object(module, "_write_shared_slice_artifacts"),
            patch.object(module, "_validate_for_slice_with_test_policy", side_effect=fake_validate),
        ):
            summary, ok = module._prepare_multi_slice_disjoint(ctx, run_dir)

        self.assertTrue(ok)
        self.assertEqual(
            validation_calls,
            [
                (True, "sp1", False),
                (True, "sp2", False),
                (False, "sp1", False),
            ],
        )
        self.assertEqual(
            summary,
            [
                "sp1-success=pass",
                "sp2-success=pass",
                "sp1-cross-slice-rejected=pass",
            ],
        )

    def test_write_review_handoff_creates_review_contract(self) -> None:
        module = _load_smoke_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / ".codex" / "rlm" / "run-61"
            run_dir.mkdir(parents=True)
            ctx = module.RunContext(
                repo_root=root,
                run_id="run-61",
                branch="stage",
                worktree_path=root / "wt",
                session_name="rlm-run-61-kimi",
                fixture_pkg="acp_smoke",
            )
            module._write_review_handoff(ctx, run_dir)
            handoff = (run_dir / "02.5-acp-handoff.lock.md").read_text(encoding="utf-8")
            state = (run_dir / "02.5-acp-handoff.state.json").read_text(encoding="utf-8")
            self.assertIn("Delegation Role: reviewer", handoff)
            self.assertIn("## Output Contract\n" + "defects_or_no_defects", handoff)
            self.assertIn('"outputContract": "defects_or_no_defects"', state)

    def test_verification_command_uses_current_interpreter(self) -> None:
        module = _load_smoke_module()
        command = module._verification_command(fixture_pkg="acp_smoke")
        self.assertIn("-m unittest -q acp_smoke.test_adder", command)
        self.assertIn(Path(sys.executable).name.lower(), command.lower())

    def test_write_verification_evidence_matches_outcome_hash_contract(self) -> None:
        module = _load_smoke_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / ".codex" / "rlm" / "run-61"
            run_dir.mkdir(parents=True)
            ctx = module.RunContext(
                repo_root=root,
                run_id="run-61",
                branch="stage",
                worktree_path=root / "wt",
                session_name="rlm-run-61-kimi",
                fixture_pkg="acp_smoke",
            )

            evidence_json_path, verification_output_sha = module._write_verification_evidence(
                ctx,
                run_dir,
                argv=[sys.executable, "-m", "unittest", "-q", "acp_smoke.test_adder"],
                output_text="smoke verification output\n",
            )
            module._make_valid_outcome_artifact(
                run_dir / "04-test-summary.md",
                changed_files=["acp_smoke/test_adder.py"],
                command=module._verification_command(fixture_pkg="acp_smoke"),
                evidence_json_path=evidence_json_path,
                verification_output_sha=verification_output_sha,
            )

            evidence = json.loads((run_dir / "evidence" / "logs" / "acp-verification.json").read_text(encoding="utf-8"))
            outcome = (run_dir / "04-test-summary.md").read_text(encoding="utf-8")
            self.assertEqual(evidence["cwd"], str(ctx.worktree_path))
            self.assertEqual(evidence["outputSha256"], verification_output_sha)
            self.assertIn(evidence_json_path, outcome)
            self.assertIn(f"Verification Output Sha256: {verification_output_sha}", outcome)


if __name__ == "__main__":
    unittest.main()
