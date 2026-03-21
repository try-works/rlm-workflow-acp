from __future__ import annotations

import re
from dataclasses import dataclass
import hashlib
from pathlib import Path
import subprocess
import json
from typing import Iterable


@dataclass(frozen=True)
class CompletionResult:
    ok: bool
    artifact_path: Path | None = None
    problems: list[str] | None = None
    changed_tracked_files: list[str] | None = None


_OUTCOME_HEADING_RE = re.compile(r"(?m)^[ \t]*##\s+ACP Delegation Outcome\s*$")
_LEGACY_OUTCOME_HEADING_RE = re.compile(r"(?m)^[ \t]*##\s+Kimi Delegation Outcome\s*$")
_NEXT_HEADING_RE = re.compile(r"(?m)^[ \t]*#{1,6}\s+")
_REVIEW_NO_DEFECTS_RE = re.compile(r"(?mi)^[ \t]*NO_DEFECTS[ \t]*$")
_REVIEW_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*]|\d+\.)\s+(?P<body>\S.*)$")
_REVIEW_REFERENCE_BASE_PATTERN = r"`[^`]+`|/?(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+|[A-Za-z0-9_.-]*\.[A-Za-z0-9_.-]+)"
_REVIEW_REFERENCE_PATTERN = rf"(?:{_REVIEW_REFERENCE_BASE_PATTERN})(?::\d+(?::\d+)?)?"
_REVIEW_REFERENCE_RE = re.compile(rf"(?P<ref>{_REVIEW_REFERENCE_PATTERN})")


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


def _normalize_path(path: str) -> str:
    return path.strip().strip("`").replace("\\", "/")


def _normalize_snapshot_map(snapshot_map: dict[str, dict[str, object]] | None) -> dict[str, dict[str, object]]:
    normalized: dict[str, dict[str, object]] = {}
    if not snapshot_map:
        return normalized
    for raw_path, raw_snapshot in snapshot_map.items():
        path = _normalize_path(str(raw_path))
        if not path or not isinstance(raw_snapshot, dict):
            continue
        normalized[path] = {
            "exists": bool(raw_snapshot.get("exists")),
            "sha256": str(raw_snapshot.get("sha256") or "") or None,
        }
    return normalized


def _file_snapshot(*, worktree_path: Path, repo_relative_path: str) -> dict[str, object]:
    file_path = worktree_path / repo_relative_path
    if not file_path.exists():
        return {"exists": False, "sha256": None}

    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return {"exists": True, "sha256": digest}


def capture_dirty_worktree_baseline(*, worktree_path: Path, baseline_head: str | None) -> tuple[list[str], dict[str, dict[str, object]]]:
    dirty_paths = get_changed_tracked_files(worktree_path=worktree_path, baseline_head=baseline_head)
    snapshots = {
        path: _file_snapshot(worktree_path=worktree_path, repo_relative_path=path)
        for path in dirty_paths
    }
    return dirty_paths, snapshots


def get_changed_tracked_files(
    *,
    worktree_path: Path,
    baseline_head: str | None,
    baseline_dirty_paths: list[str] | None = None,
    baseline_dirty_snapshots: dict[str, dict[str, object]] | None = None,
) -> list[str]:
    """
    Repo-mediated change detection that works whether the worker left changes
    uncommitted or committed them during the run.
    """

    changes: set[str] = set()

    # Committed changes since baseline.
    if baseline_head:
        out = _git(["diff", "--name-only", f"{baseline_head}..HEAD"], cwd=worktree_path)
        for line in out.splitlines():
            p = _normalize_path(line)
            if p:
                changes.add(p)

    # Unstaged + staged changes.
    for extra in (["diff", "--name-only"], ["diff", "--name-only", "--cached"]):
        out = _git(extra, cwd=worktree_path)
        for line in out.splitlines():
            p = _normalize_path(line)
            if p:
                changes.add(p)

    baseline_dirty_set = {_normalize_path(path) for path in (baseline_dirty_paths or []) if _normalize_path(path)}
    if not baseline_dirty_set:
        return sorted(changes)

    normalized_snapshots = _normalize_snapshot_map(baseline_dirty_snapshots)
    net_changes = {path for path in changes if path not in baseline_dirty_set}

    # If a path was already dirty at delegation start, treat it as changed only when
    # its working-tree content moved again relative to the captured starting snapshot.
    for path in sorted(baseline_dirty_set):
        starting_snapshot = normalized_snapshots.get(path)
        if not starting_snapshot:
            continue
        current_snapshot = _file_snapshot(worktree_path=worktree_path, repo_relative_path=path)
        if current_snapshot != starting_snapshot:
            net_changes.add(path)

    return sorted(net_changes)


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

    return problems


def _normalize_declared_paths(paths: Iterable[str]) -> set[str]:
    normalized: set[str] = set()
    for raw in paths:
        value = _normalize_path(str(raw))
        if value:
            normalized.add(value)
    return normalized


def _verify_owned_write_files(
    *,
    owned_write_files: list[str],
    changed_paths: list[str],
) -> list[str]:
    problems: list[str] = []
    owned_set = _normalize_declared_paths(owned_write_files)
    changed_set = _normalize_declared_paths(changed_paths)

    extra = sorted(path for path in changed_set if path not in owned_set)
    if extra:
        problems.append("Tracked writes escaped the declared owned write set:\n" + "\n".join(f"- {p}" for p in extra))

    return problems


def _read_transcript_text(transcript_dir: Path | None) -> str:
    if transcript_dir is None:
        return ""

    chunks: list[str] = []
    for name in ("stdout.txt", "stderr.txt", "invocation.json"):
        path = transcript_dir / name
        if not path.exists():
            continue
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks).strip()


def _normalize_review_finding(text: str) -> str:
    return re.sub(r"^[ \t]*(?:[-*]|\d+\.)\s+", "", text.strip())


def _is_valid_review_finding_body(body: str) -> bool:
    body = body.strip()
    if not body:
        return False

    # Preferred forms:
    # - path/to/file.ts: concrete issue
    # - path/to/file.ts - concrete issue
    # - file.ts:123 concrete issue
    colon_or_dash = re.match(
        rf"^(?P<ref>{_REVIEW_REFERENCE_PATTERN})\s*(?::|-)\s+(?P<issue>\S.*)$",
        body,
    )
    if colon_or_dash:
        return True

    # Allow file:line issue without a second separator, because reviewers commonly
    # write `path.ts:12 issue text`.
    line_ref = re.match(
        rf"^(?P<ref>{_REVIEW_REFERENCE_BASE_PATTERN}:\d+(?::\d+)?)\s+(?P<issue>\S.*)$",
        body,
    )
    return bool(line_ref)


def extract_review_findings(text: str) -> tuple[list[str], list[str]]:
    findings: list[str] = []
    invalid: list[str] = []

    for raw_line in text.splitlines():
        match = _REVIEW_LIST_ITEM_RE.match(raw_line)
        if not match:
            continue

        body = match.group("body").strip()
        normalized = _normalize_review_finding(raw_line)
        if _is_valid_review_finding_body(body):
            findings.append(normalized)
        else:
            invalid.append(normalized)

    return findings, invalid


def review_text_has_contract(text: str) -> bool:
    if _REVIEW_NO_DEFECTS_RE.search(text):
        return True

    findings, invalid = extract_review_findings(text)
    return bool(findings) and not invalid


def _validate_handoff_outcome_artifacts(candidates: list[Path]) -> list[str]:
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
    return problems


def _validate_review_output_contract(
    *,
    candidates: list[Path],
    transcript_dir: Path | None,
) -> list[str]:
    problems: list[str] = []

    if candidates:
        for p in candidates:
            if not p.exists():
                problems.append(f"Missing required artifact update: {p.name}")
                continue
            content = p.read_text(encoding="utf-8")
            if review_text_has_contract(content):
                continue
            _, invalid = extract_review_findings(content)
            extra = f"; invalid items: {', '.join(invalid)}" if invalid else ""
            problems.append(
                f"{p.name}: review output contract not satisfied; expected `NO_DEFECTS` or a flat defect list where each item starts with a file/artifact reference and concrete issue{extra}"
            )
        return problems

    transcript_text = _read_transcript_text(transcript_dir)
    if not transcript_text:
        problems.append("Review output contract requires either a review artifact update or a saved ACP transcript")
    elif not review_text_has_contract(transcript_text):
        _, invalid = extract_review_findings(transcript_text)
        extra = f"; invalid items: {', '.join(invalid)}" if invalid else ""
        problems.append(
            "Saved ACP transcript does not satisfy review output contract "
            "(`NO_DEFECTS` or a flat defect list with file/artifact references and concrete issues)"
            f"{extra}"
        )

    return problems


def _validate_patch_plan_artifacts(candidates: list[Path]) -> list[str]:
    problems: list[str] = []
    for p in candidates:
        if not p.exists():
            problems.append(f"Missing required artifact update: {p.name}")
            continue
        content = p.read_text(encoding="utf-8").strip()
        if not content:
            problems.append(f"Patch plan artifact is empty: {p.name}")
            continue
        if "##" not in content and not re.search(r"(?m)^[ \t]*(?:[-*]|\d+\.)\s+\S+", content):
            problems.append(f"Patch plan artifact lacks headings or actionable list items: {p.name}")
    return problems


def _validate_output_contract(
    *,
    output_contract: str,
    candidates: list[Path],
    transcript_dir: Path | None,
) -> list[str]:
    if output_contract in {"handoff_outcome", "repair_summary"}:
        return _validate_handoff_outcome_artifacts(candidates)
    if output_contract == "defects_or_no_defects":
        return _validate_review_output_contract(candidates=candidates, transcript_dir=transcript_dir)
    if output_contract == "patch_plan":
        return _validate_patch_plan_artifacts(candidates)
    return [f"Unsupported output contract: {output_contract}"]


def verify_acp_completion(
    run_dir: Path,
    required_updates: list[Path],
    *,
    worktree_path: Path | None = None,
    baseline_head: str | None = None,
    baseline_dirty_paths: list[str] | None = None,
    baseline_dirty_snapshots: dict[str, dict[str, object]] | None = None,
    required_worktree_changes: list[str] | None = None,
    owned_write_files: list[str] | None = None,
    output_contract: str = "handoff_outcome",
    verification_evidence_json: Path | None = None,
    transcript_dir: Path | None = None,
) -> CompletionResult:
    candidates = required_updates

    problems = _validate_output_contract(
        output_contract=output_contract,
        candidates=candidates,
        transcript_dir=transcript_dir,
    )

    changed: list[str] | None = None
    if required_worktree_changes or owned_write_files:
        if not worktree_path:
            problems.append("Worktree change validation requested but no worktree_path was provided")
        else:
            changed = get_changed_tracked_files(
                worktree_path=worktree_path,
                baseline_head=baseline_head,
                baseline_dirty_paths=baseline_dirty_paths,
                baseline_dirty_snapshots=baseline_dirty_snapshots,
            )
            if owned_write_files:
                problems.extend(_verify_owned_write_files(owned_write_files=owned_write_files, changed_paths=changed))
            problems.extend(
                _verify_required_worktree_changes(required_paths=required_worktree_changes, changed_paths=changed)
                if required_worktree_changes
                else []
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
