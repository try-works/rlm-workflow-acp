#!/usr/bin/env python3
"""
Functional ACP delegation smoke test for rlm-workflow-acp (Windows-friendly).

This is intentionally not a CI test. It requires:
- `acpx` on PATH
- `kimi` on PATH and authenticated for `acpx kimi`

It creates:
- a dedicated git worktree
- a minimal tracked source+test fixture with a known failing baseline
- a normal RLM run folder under `.codex/rlm/<run-id>/`
- a sealed `02.5-acp-handoff.lock.md` for delegated phases 3+4

Then it delegates to Kimi and validates completion strictly via repo/artifacts.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class RunContext:
    repo_root: Path
    run_id: str
    branch: str
    worktree_path: Path
    session_name: str
    fixture_pkg: str


def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {proc.returncode}): {' '.join(cmd)}\n"
            f"--- STDOUT ---\n{proc.stdout}\n--- STDERR ---\n{proc.stderr}"
        )
    return proc


def _git(args: list[str], *, cwd: Path) -> str:
    return _run(["git", *args], cwd=cwd, check=True).stdout.strip()


def _python_exe() -> list[str]:
    # Prefer Windows py launcher if present; otherwise use current interpreter.
    if shutil.which("py"):
        return ["py", "-3"]
    return [sys.executable]


def _require_tools() -> None:
    for tool in ("git", "acpx", "kimi"):
        if not shutil.which(tool):
            raise FileNotFoundError(f"Missing required tool on PATH: {tool}")


def _repo_root() -> Path:
    proc = _run(["git", "rev-parse", "--show-toplevel"], check=True)
    return Path(proc.stdout.strip()).resolve()


def _make_context(*, run_id: str | None, worktree_root: Path, session_name: str | None) -> RunContext:
    repo_root = _repo_root()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rid = (run_id or f"acp-smoke-{ts}").strip()
    if not rid:
        raise ValueError("run_id must be non-empty")
    branch = f"acp-smoke-{rid}"
    wt = (worktree_root / rid).resolve()
    sess = (session_name or f"rlm-{rid}-kimi").strip()
    if not sess:
        raise ValueError("session_name must be non-empty")
    return RunContext(repo_root=repo_root, run_id=rid, branch=branch, worktree_path=wt, session_name=sess, fixture_pkg="acp_smoke")


def _create_worktree(ctx: RunContext) -> None:
    ctx.worktree_path.parent.mkdir(parents=True, exist_ok=True)
    # Use HEAD as the base for the worktree (clean, deterministic).
    _run(["git", "worktree", "add", "-b", ctx.branch, str(ctx.worktree_path), "HEAD"], cwd=ctx.repo_root, check=True)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _setup_fixture(ctx: RunContext) -> None:
    wt = ctx.worktree_path
    pkg_name = ctx.fixture_pkg
    pkg = wt / pkg_name
    pkg.mkdir(parents=True, exist_ok=True)

    _write(pkg / "__init__.py", "\n")
    _write(
        pkg / "adder.py",
        '''"""Small ACP smoke fixture.

The baseline implementation is intentionally wrong so the delegated worker must fix it.
"""

def add(a: int, b: int) -> int:
    """Return a + b."""
    # BUG: should be a + b
    return a - b
''',
    )
    _write(
        pkg / "test_adder.py",
        """import unittest

from {PKG}.adder import add


class TestAdd(unittest.TestCase):
    def test_add_basic(self):
        self.assertEqual(add(2, 2), 4)

    # TODO: Add at least one additional assertion (negative or zero case).


if __name__ == "__main__":
    unittest.main()
""".replace("{PKG}", pkg_name),
    )

    _git(
        ["add", f"{pkg_name}/__init__.py", f"{pkg_name}/adder.py", f"{pkg_name}/test_adder.py"],
        cwd=wt,
    )
    _git(["commit", "-m", "Add ACP smoke fixture with failing test"], cwd=wt)

    # Verify baseline fails (deterministic).
    proc = _run(_python_exe() + ["-m", "unittest", "-q", f"{pkg_name}.test_adder"], cwd=wt, check=False)
    if proc.returncode == 0:
        raise RuntimeError("Expected baseline test failure, but tests passed unexpectedly.")


def _setup_run_folder(ctx: RunContext) -> Path:
    wt = ctx.worktree_path
    # Ensure scaffold exists in the worktree.
    installer = (ctx.repo_root / "scripts" / "install-rlm-workflow.py").resolve()
    _run(_python_exe() + [str(installer), "--repo-root", "."], cwd=wt, check=True)

    run_dir = (wt / ".codex" / "rlm" / ctx.run_id).resolve()
    (run_dir / "addenda").mkdir(parents=True, exist_ok=True)

    _write(
        run_dir / "00-requirements.md",
        """# Requirements

- Fix `<FIXTURE>/adder.py:add` so tests can pass.
- Add at least one more assertion to the unit test.
- Run `py -3 -m unittest -q <FIXTURE>.test_adder` and record results.
""".replace("<FIXTURE>", ctx.fixture_pkg),
    )
    _write(
        run_dir / "00-worktree.md",
        f"""# Worktree

- Worktree Path: {wt}
- Branch: {ctx.branch}
""",
    )
    _write(
        run_dir / "01-as-is.md",
        """# As-Is

- `add(a,b)` currently returns `a - b`.
- `py -3 -m unittest -q <FIXTURE>.test_adder` fails.
""".replace("<FIXTURE>", ctx.fixture_pkg),
    )
    _write(
        run_dir / "02-to-be-plan.md",
        """# To-Be Plan

1. Update `<FIXTURE>/adder.py` so `add(a,b)` returns `a + b`.
2. Update `<FIXTURE>/test_adder.py` to add at least one additional assertion (e.g. zero or negative case).
3. Run verification: `py -3 -m unittest -q <FIXTURE>.test_adder`.
4. Update `03-implementation-summary.md` and `04-test-summary.md` with `## ACP Delegation Outcome`.
""".replace("<FIXTURE>", ctx.fixture_pkg),
    )
    _write(run_dir / "03-implementation-summary.md", "")
    _write(run_dir / "04-test-summary.md", "")
    return run_dir


def _init_handoff(ctx: RunContext) -> None:
    wt = ctx.worktree_path
    delegate = (ctx.repo_root / "scripts" / "delegate-to-kimi.py").resolve()
    pkg = ctx.fixture_pkg
    _run(
        _python_exe()
        + [
            str(delegate),
            "--run",
            ctx.run_id,
            "--worktree",
            str(wt),
            "--init-handoff",
            "--delegated-phases",
            "3,4",
            "--fixture-source",
            f"{pkg}/adder.py",
            "--fixture-test",
            f"{pkg}/test_adder.py",
            "--test-command",
            f"py -3 -m unittest -q {pkg}.test_adder",
        ],
        cwd=wt,
        check=True,
    )


def _delegate(ctx: RunContext) -> None:
    wt = ctx.worktree_path
    delegate = (ctx.repo_root / "scripts" / "delegate-to-kimi.py").resolve()
    _run(
        _python_exe()
        + [
            str(delegate),
            "--run",
            ctx.run_id,
            "--worktree",
            str(wt),
            "--session-name",
            ctx.session_name,
        ],
        cwd=wt,
        check=True,
    )


def _validate(ctx: RunContext) -> tuple[list[str], bool]:
    wt = ctx.worktree_path
    # Strict validation (updates sidecar; exits non-zero on failure).
    delegate = (ctx.repo_root / "scripts" / "delegate-to-kimi.py").resolve()
    _run(_python_exe() + [str(delegate), "--run", ctx.run_id, "--worktree", str(wt), "--validate-only"], cwd=wt, check=True)

    changed = _git(["diff", "--name-only"], cwd=wt).splitlines()
    changed = [c.strip() for c in changed if c.strip()]
    tests = _run(_python_exe() + ["-m", "unittest", "-q", f"{ctx.fixture_pkg}.test_adder"], cwd=wt, check=False)
    return sorted(changed), tests.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Functional ACP delegation smoke test (delegated phases 3+4).")
    parser.add_argument("--run-id", default="", help="Optional explicit run id (default: acp-smoke-<timestamp>).")
    parser.add_argument(
        "--fixture-pkg",
        default="acp_smoke",
        help="Fixture package directory to create (default: acp_smoke). Use distinct values for parallel isolation tests.",
    )
    parser.add_argument(
        "--worktree-root",
        default="",
        help="Where to create the dedicated worktree (default: sibling dir <repo>-smoke-worktrees).",
    )
    parser.add_argument("--session-name", default="", help="Optional explicit acpx session name.")
    parser.add_argument("--keep-worktree", action="store_true", help="Do not remove the worktree after the run.")
    args = parser.parse_args()

    _require_tools()
    repo_root = _repo_root()
    # IMPORTANT: do not create the worktree nested under the repo root.
    # acpx's session scoping logic only detects `.git` directories (not worktree `.git` files),
    # so a nested worktree can cause sessions to be treated as closed/unroutable.
    default_wt_root = (repo_root.parent / f"{repo_root.name}-smoke-worktrees").resolve()
    wt_root = Path(args.worktree_root).expanduser().resolve() if args.worktree_root.strip() else default_wt_root
    ctx = _make_context(run_id=args.run_id.strip() or None, worktree_root=wt_root, session_name=args.session_name.strip() or None)
    ctx = RunContext(
        repo_root=ctx.repo_root,
        run_id=ctx.run_id,
        branch=ctx.branch,
        worktree_path=ctx.worktree_path,
        session_name=ctx.session_name,
        fixture_pkg=args.fixture_pkg.strip() or "acp_smoke",
    )

    try:
        _create_worktree(ctx)
        _setup_fixture(ctx)
        _setup_run_folder(ctx)
        _init_handoff(ctx)
        _delegate(ctx)
        changed, tests_ok = _validate(ctx)

        print(f"runId={ctx.run_id}")
        print(f"worktree={ctx.worktree_path}")
        print(f"branch={ctx.branch}")
        print(f"sessionName={ctx.session_name}")
        print(f"fixturePkg={ctx.fixture_pkg}")
        print("changedTrackedFiles=" + ",".join(changed))
        print(f"testsPassed={tests_ok}")
        return 0 if tests_ok else 2
    finally:
        if not args.keep_worktree:
            # Best-effort cleanup (may fail if external processes keep files open).
            _run(["git", "worktree", "remove", "--force", str(ctx.worktree_path)], cwd=ctx.repo_root, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
