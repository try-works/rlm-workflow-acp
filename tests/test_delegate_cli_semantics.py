from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "delegate-to-kimi.py"
WRAPPER_PATH = REPO_ROOT / "scripts" / "delegate-to-kimi.ps1"

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load_delegate_module():
    spec = importlib.util.spec_from_file_location("delegate_to_kimi_cli_semantics", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load delegate-to-kimi.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DelegateCliSemanticsTests(unittest.TestCase):
    def test_help_marks_save_transcript_as_deprecated_no_op(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Deprecated no-op", proc.stdout)
        self.assertIn("transcripts are always persisted", proc.stdout)

    def test_transcript_policy_is_always_on(self) -> None:
        module = _load_delegate_module()
        self.assertEqual(module.TRANSCRIPT_POLICY, "always_on")
        self.assertTrue(module._transcripts_always_on())

    def test_powershell_wrapper_defaults_and_forwards_loop_count_exactly(self) -> None:
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")
        self.assertIn("[int]$MaxReviewLoops = 2", wrapper)
        self.assertIn('$argsList += @("--max-review-loops", [string]$MaxReviewLoops)', wrapper)
        self.assertNotIn("$MaxReviewLoops -gt 0", wrapper)


if __name__ == "__main__":
    unittest.main()
