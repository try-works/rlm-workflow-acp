from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lib import acpx_runner  # noqa: E402


class AcpxRunnerTests(unittest.TestCase):
    def test_acpx_argv_wraps_cmd_install_on_windows(self) -> None:
        with patch.object(acpx_runner.os, "name", "nt"), patch.object(
            acpx_runner, "require_acpx_on_path", return_value=r"C:\Tools\acpx.cmd"
        ):
            argv = acpx_runner._acpx_argv(["acpx", "--cwd", r"C:\repo", "kimi", "exec", "--file", "-"])

        self.assertEqual(
            argv,
            ["cmd", "/c", r"C:\Tools\acpx.cmd", "--cwd", r"C:\repo", "kimi", "exec", "--file", "-"],
        )

    def test_run_agent_exec_uses_exec_subcommand(self) -> None:
        completed = subprocess.CompletedProcess(args=["acpx"], returncode=0, stdout="ok\n", stderr="")
        with patch.object(acpx_runner, "require_acpx_on_path", return_value="/usr/bin/acpx"), patch.object(
            acpx_runner, "_run_capture", return_value=completed
        ) as run_capture:
            result = acpx_runner.run_agent_exec(
                agent="kimi",
                cwd=Path("/tmp/worktree"),
                prompt_text="hello",
                approve_all=False,
                emit_output=False,
            )

        run_capture.assert_called_once()
        cmd = run_capture.call_args.args[0]
        self.assertEqual(cmd, ["acpx", "--cwd", str(Path("/tmp/worktree")), "kimi", "exec", "--file", "-"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.execution_kind, "exec")
        self.assertEqual(result.stdout, "ok\n")
        self.assertIsNone(result.session_name)

    def test_run_agent_prompt_retries_when_session_needs_reconnect(self) -> None:
        first = acpx_runner.AcpxResult(
            returncode=1,
            stdout="",
            stderr="failed",
            requested_argv=["acpx"],
            invoked_argv=["acpx"],
            cwd="/tmp/worktree",
            started_at="2026-03-21T00:00:00Z",
            completed_at="2026-03-21T00:00:01Z",
            agent="kimi",
            session_name="demo",
            execution_kind="prompt",
            approval_mode="approve-all",
        )
        second = acpx_runner.AcpxResult(
            returncode=0,
            stdout="ok",
            stderr="",
            requested_argv=["acpx"],
            invoked_argv=["acpx"],
            cwd="/tmp/worktree",
            started_at="2026-03-21T00:00:02Z",
            completed_at="2026-03-21T00:00:03Z",
            agent="kimi",
            session_name="demo",
            execution_kind="prompt",
            approval_mode="approve-all",
        )
        with patch.object(acpx_runner, "require_acpx_on_path", return_value="/usr/bin/acpx"), patch.object(
            acpx_runner, "ensure_named_session"
        ), patch.object(acpx_runner, "_ensure_session_alive"), patch.object(
            acpx_runner,
            "_run_acpx_capture",
            side_effect=[first, second],
        ) as run_capture, patch.object(
            acpx_runner,
            "get_session_status",
            return_value={"status": "dead", "summary": "queue owner unavailable"},
        ), patch.object(acpx_runner.time, "sleep"):
            result = acpx_runner.run_agent_prompt(
                agent="kimi",
                cwd=Path("/tmp/worktree"),
                session_name="demo",
                prompt_text="hello",
                emit_output=False,
            )

        self.assertEqual(run_capture.call_count, 2)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
