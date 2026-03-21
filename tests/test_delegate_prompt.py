from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "delegate-to-kimi.py"

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load_delegate_module():
    spec = importlib.util.spec_from_file_location("delegate_to_kimi_module", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load delegate-to-kimi.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DelegatePromptTests(unittest.TestCase):
    def test_build_prompt_includes_role_template_and_output_contract_guidance(self) -> None:
        module = _load_delegate_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            prompt = module._build_prompt(
                repo_root=repo_root,
                run_id="run-61",
                mode="review",
                session_policy="exec",
                delegation_role="reviewer",
                output_contract="defects_or_no_defects",
                handoff_path=repo_root / ".codex" / "rlm" / "run-61" / "02.5-acp-handoff.lock.md",
                handoff_content="# ACP Handoff",
            )

        self.assertIn("BEGIN ROLE TEMPLATE", prompt)
        self.assertIn("Code Reviewer Agent", prompt)
        self.assertIn("Output contract for this run: `defects_or_no_defects`", prompt)
        self.assertIn("Return either a single line `NO_DEFECTS`", prompt)
        self.assertIn("Each finding must start with a concrete file or artifact reference", prompt)
        self.assertIn("Checklist bullets like `- verify tests`", prompt)

    def test_build_prompt_supports_custom_role_template_file(self) -> None:
        module = _load_delegate_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            template_path = repo_root / "custom-role.md"
            template_path.write_text("# Custom Role\n\nStay bounded.\n", encoding="utf-8")
            prompt = module._build_prompt(
                repo_root=repo_root,
                run_id="run-61",
                mode="implement",
                session_policy="exec",
                delegation_role="implementer",
                role_template_spec=str(template_path),
                output_contract="handoff_outcome",
                handoff_path=repo_root / ".codex" / "rlm" / "run-61" / "02.5-acp-handoff.sp1.lock.md",
                handoff_content="# ACP Handoff",
            )

        self.assertIn("# Custom Role", prompt)
        self.assertIn("Stay bounded.", prompt)

    def test_resolve_handoff_paths_supports_slice_suffixes(self) -> None:
        module = _load_delegate_module()
        paths = module._resolve_handoff_paths(run_dir=Path("/repo/.codex/rlm/run-61"), slice_name="sp1")
        self.assertEqual(paths.base_name, "02.5-acp-handoff.sp1")
        self.assertEqual(paths.handoff_path.name, "02.5-acp-handoff.sp1.lock.md")
        self.assertEqual(paths.state_path.name, "02.5-acp-handoff.sp1.state.json")

    def test_role_template_spec_resolution_is_stable_and_inheritable(self) -> None:
        module = _load_delegate_module()
        self.assertEqual(
            module._resolve_effective_role_template_spec(role_template_spec=" reviewer "),
            "reviewer",
        )
        self.assertIsNone(module._resolve_effective_role_template_spec(role_template_spec=""))

    def test_build_prompt_lists_effective_override_constraints(self) -> None:
        module = _load_delegate_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            prompt = module._build_prompt(
                repo_root=repo_root,
                run_id="run-61",
                mode="implement",
                session_policy="exec",
                delegation_role="implementer",
                output_contract="handoff_outcome",
                owned_write_files=["app/example.ts"],
                allowed_read_paths=[".codex/rlm/run-61/02-to-be-plan.md"],
                handoff_path=repo_root / ".codex" / "rlm" / "run-61" / "02.5-acp-handoff.lock.md",
                handoff_content="# ACP Handoff",
            )

        self.assertIn("CLI override constraints in force:", prompt)
        self.assertIn("app/example.ts", prompt)
        self.assertIn(".codex/rlm/run-61/02-to-be-plan.md", prompt)

    def test_resolve_effective_output_contract_rejects_sealed_mismatch_by_default(self) -> None:
        module = _load_delegate_module()
        with self.assertRaisesRegex(ValueError, "Refusing to override the sealed handoff output contract"):
            module._resolve_effective_output_contract(
                handoff_output_contract="handoff_outcome",
                override_output_contract="patch_plan",
                allow_sealed_override=False,
            )

    def test_resolve_effective_output_contract_allows_explicit_unsafe_override(self) -> None:
        module = _load_delegate_module()
        effective, applied = module._resolve_effective_output_contract(
            handoff_output_contract="handoff_outcome",
            override_output_contract="patch_plan",
            allow_sealed_override=True,
        )
        self.assertEqual(effective, "patch_plan")
        self.assertTrue(applied)

    def test_ensure_validation_baseline_state_initializes_missing_baseline_fields(self) -> None:
        module = _load_delegate_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Tests"], cwd=repo, check=True, capture_output=True, text=True)
            tracked = repo / "README.md"
            tracked.write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True, text=True)

            tracked.write_text("dirty\n", encoding="utf-8")
            state: dict[str, object] = {}

            baseline_head, baseline_dirty_paths, baseline_dirty_snapshots = module._ensure_validation_baseline_state(
                state=state,
                worktree_path=repo,
            )

            self.assertTrue(baseline_head)
            self.assertEqual(baseline_dirty_paths, ["README.md"])
            self.assertIn("README.md", baseline_dirty_snapshots)
            self.assertEqual(state["baselineHead"], baseline_head)
            self.assertEqual(state["baselineDirtyTrackedFiles"], ["README.md"])


if __name__ == "__main__":
    unittest.main()
