from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
import subprocess
import json


@dataclass(frozen=True)
class CompletionResult:
    ok: bool
    artifact_path: Path | None = None
    problems: list[str] | None = None
    changed_tracked_files: list[str] | None = None


_OUTCOME_HEADING_RE = re.compile(r"(?m)^[ \t]*##\s+ACP Delegation Outcome\s*$")
_LEGACY_OUTCOME_HEADING_RE = re.compile(r"(?m)^[ \t]*##\s+Kimi Delegation Outcome\s*$")
_NEXT_HEADING_RE = re.compile(r"(?m)^[ \t]*#{1,6}\s+")


def _extract_outcome_body(content: str) -> str | None:
    m = _OUTCOME_HEADING_RE.search(content) or _LEGACY_OUTCOME_HEADING_RE.search(content)
    if not m:
        return None
    after = content[m.end() :]
    next_heading = _NEXT_HEADING_RE.search(after)
    body = after[: next_heading.start()] if next_heading else after
    body = body.strip()
    return body if body else ""


def _has_required_fields(body: str) -> list[str]:
    # Required keys: status + verification + at least one of summary/changed.
    problems: list[str] = []

    def has_key(name: str) -> bool:
        # Accept:
        # - `Key: value`
        # - `**Key:** value` (colon inside bold)
        # - `**Key**: value` (colon outside bold)
        # - Markdown tables: `| Key | value |` or `| **Key** | value |`
        # Also accept optional list markers like `- ` or `* ` used in outcomes.
        colon_pattern = rf"(?mi)^[ \t]*(?:[-*]\s+)?(?:\*\*)?{re.escape(name)}(?:\*\*)?\s*:\s*(?:\*\*)?\s*\S+"
        if re.search(colon_pattern, body):
            return True

        table_pattern = rf"(?mi)^[ \t]*\|\s*(?:\*\*)?{re.escape(name)}(?:\*\*)?\s*\|\s*(?:`[^`]+`|[^|\r\n]*\S[^|\r\n]*)\s*\|"
        return bool(re.search(table_pattern, body))

    status_ok = has_key("Status")
    verification_ok = has_key("Verification") or has_key("Verification Run")
    summary_ok = has_key("Summary")
    changed_ok = has_key("Changed Areas") or has_key("Changed Areas/Files") or has_key("Changed Files")

    if not status_ok:
        problems.append("Missing required field in outcome section: Status:")
    if not verification_ok:
        problems.append("Missing required field in outcome section: Verification: (or Verification Run:)")
    if not (summary_ok or changed_ok):
        problems.append("Missing required field in outcome section: Summary: (or Changed Areas/Files:)")

    return problems


def _git(args: list[str], *, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed (exit {proc.returncode})")
    return proc.stdout.strip()


def get_changed_tracked_files(*, worktree_path: Path, baseline_head: str | None) -> list[str]:
    """
    Repo-mediated change detection that works whether the worker left changes
    uncommitted or committed them during the run.
    """

    changes: set[str] = set()

    # Committed changes since baseline.
    if baseline_head:
        out = _git(["diff", "--name-only", f"{baseline_head}..HEAD"], cwd=worktree_path)
        for line in out.splitlines():
            p = line.strip()
            if p:
                changes.add(p)

    # Unstaged + staged changes.
    for extra in (["diff", "--name-only"], ["diff", "--name-only", "--cached"]):
        out = _git(extra, cwd=worktree_path)
        for line in out.splitlines():
            p = line.strip()
            if p:
                changes.add(p)

    return sorted(changes)


def _verify_required_worktree_changes(
    *,
    required_paths: list[str],
    changed_paths: list[str],
) -> list[str]:
    problems: list[str] = []
    required_set = {p.strip().strip("`") for p in required_paths if p.strip()}
    changed_set = {p.strip() for p in changed_paths if p.strip()}

    missing = sorted(p for p in required_set if p not in changed_set)
    if missing:
        problems.append("Missing required tracked worktree changes:\n" + "\n".join(f"- {p}" for p in missing))

    extra = sorted(p for p in changed_set if p not in required_set)
    if extra:
        problems.append("Unrelated tracked files changed (not allowed by handoff):\n" + "\n".join(f"- {p}" for p in extra))

    return problems


def verify_acp_completion(
    run_dir: Path,
    required_updates: list[Path],
    *,
    worktree_path: Path | None = None,
    baseline_head: str | None = None,
    required_worktree_changes: list[str] | None = None,
    verification_evidence_json: Path | None = None,
) -> CompletionResult:
    candidates = required_updates

    problems: list[str] = []
    for p in candidates:
        if not p.exists():
            problems.append(f"Missing required artifact update: {p.name}")
            continue

        content = p.read_text(encoding="utf-8")
        body = _extract_outcome_body(content)
        if body is None:
            problems.append(f"Missing required completion signal section in {p.name}: '## ACP Delegation Outcome'")
            continue
        if body.strip() == "":
            problems.append(f"Outcome section is empty in {p.name}")
            continue
        field_problems = _has_required_fields(body)
        if field_problems:
            problems.extend([f"{p.name}: {msg}" for msg in field_problems])
            continue

    changed: list[str] | None = None
    if required_worktree_changes:
        if not worktree_path:
            problems.append("Worktree change validation requested but no worktree_path was provided")
        else:
            changed = get_changed_tracked_files(worktree_path=worktree_path, baseline_head=baseline_head)
            problems.extend(
                _verify_required_worktree_changes(required_paths=required_worktree_changes, changed_paths=changed)
            )

    if verification_evidence_json:
        if not verification_evidence_json.exists():
            problems.append(f"Missing required verification evidence JSON: {verification_evidence_json}")
        else:
            try:
                evidence = json.loads(verification_evidence_json.read_text(encoding='utf-8'))
            except Exception as e:
                evidence = None
                problems.append(f"Verification evidence JSON is not valid JSON: {verification_evidence_json} ({e})")

            if isinstance(evidence, dict):
                argv = evidence.get("argv")
                if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x.strip() for x in argv):
                    problems.append("Verification evidence JSON missing valid argv array")

                ev_cwd = str(evidence.get("cwd") or "").strip()
                if not ev_cwd:
                    problems.append("Verification evidence JSON missing cwd")
                elif worktree_path:
                    try:
                        ev_cwd_resolved = str(Path(ev_cwd).resolve())
                        wt_resolved = str(worktree_path.resolve())
                        if ev_cwd_resolved.lower() != wt_resolved.lower():
                            problems.append(
                                "Verification evidence JSON cwd does not match assigned worktree "
                                f"(evidence={ev_cwd_resolved}, worktree={wt_resolved})"
                            )
                    except Exception:
                        # If we can't resolve, don't block success on path normalization edge cases.
                        pass

                exit_code = evidence.get("exitCode")
                out_sha = str(evidence.get("outputSha256") or "").strip()
                if exit_code != 0:
                    problems.append(f"Verification command exitCode is not 0 in evidence JSON: exitCode={exit_code}")
                if not out_sha:
                    problems.append("Verification evidence JSON missing outputSha256")

                # If the run delegated Phase 4, require the test summary to record the output hash.
                test_summary = next((p for p in required_updates if p.name == "04-test-summary.md"), None)
                if test_summary and out_sha:
                    content = test_summary.read_text(encoding="utf-8")
                    recorded: str | None = None

                    # Preferred: explicit key/value (works well for machine parsing).
                    m = re.search(
                        r"(?mi)^[ \t]*(?:[-*]\s+)?(?:\*\*)?Verification Output Sha256(?:\*\*)?\s*:\s*(?:\*\*)?\s*(\S+)\s*$",
                        content,
                    )
                    if m:
                        recorded = m.group(1).strip()

                    # Accept common variant labels.
                    if not recorded:
                        m = re.search(
                            r"(?mi)^[ \t]*(?:[-*]\s+)?(?:\*\*)?Output Sha256(?:\*\*)?\s*:\s*(?:\*\*)?\s*(\S+)\s*$",
                            content,
                        )
                        if m:
                            recorded = m.group(1).strip()

                    # Accept a common alternative produced by some workers: a heading + code block.
                    if not recorded:
                        h = re.search(r"(?mi)^[ \t]*##\s+Verification Output Sha256\s*$", content)
                        if h:
                            after = content[h.end() :]
                            nxt = _NEXT_HEADING_RE.search(after)
                            section = after[: nxt.start()] if nxt else after
                            # Accept bare sha anywhere in the section or within a fenced block.
                            m2 = re.search(r"(?i)\b([0-9a-f]{64})\b", section or "")
                            if m2:
                                recorded = m2.group(1).strip()

                    # Accept table rows in the test summary outcome section.
                    if not recorded:
                        m3 = re.search(
                            r"(?mi)^[ \t]*\|\s*(?:\*\*)?Verification Output Sha256(?:\*\*)?\s*\|\s*([0-9a-fA-F]{64})\s*\|",
                            content,
                        )
                        if m3:
                            recorded = m3.group(1).strip()

                    # Final fallback: require the exact evidence sha to appear somewhere in the test summary.
                    # This is still strict (must match evidence), but avoids brittleness around labels.
                    if not recorded:
                        if out_sha.lower() in content.lower():
                            recorded = out_sha

                    if not recorded:
                        problems.append(
                            "04-test-summary.md missing required verification output sha (must match evidence JSON). "
                            "Provide either `Verification Output Sha256: <sha256>`, a `## Verification Output Sha256` section containing the sha, or a table row."
                        )
                    elif recorded.lower() != out_sha.lower():
                        problems.append(
                            "04-test-summary.md verification output sha does not match evidence JSON "
                            f"(recorded={recorded}, evidence={out_sha})"
                        )

    if problems:
        return CompletionResult(ok=False, artifact_path=None, problems=problems, changed_tracked_files=changed)
    return CompletionResult(
        ok=True,
        artifact_path=candidates[0] if candidates else None,
        problems=None,
        changed_tracked_files=changed,
    )
