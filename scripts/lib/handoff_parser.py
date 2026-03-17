from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HandoffDoc:
    run_id: str
    delegated_phases: list[int]
    delegation_origin: str
    phase: str
    requirement_ids: str
    assigned_worktree_path: str
    assigned_branch: str
    created_at: str
    lock_algorithm: str
    lock_hash: str
    input_artifacts: list[str]
    required_artifact_updates: list[str]
    required_worktree_changes: list[str]
    sections: dict[str, str]


_TOP_FIELD_RE = re.compile(r"(?m)^[ \t]*([A-Za-z][A-Za-z0-9 _-]*?):\s*(.+?)\s*$")
_H2_RE = re.compile(r"(?m)^[ \t]*##\s+(.+?)\s*$")
_EVIDENCE_JSON_RE = re.compile(r"(?mi)^[ \t]*-\s*Evidence JSON.*?:\s*`([^`]+)`\s*$")


def _split_h2_sections(content: str) -> dict[str, str]:
    matches = list(_H2_RE.finditer(content))
    if not matches:
        return {}

    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        body = content[start:end].strip("\r\n")
        sections[title] = body.strip()
    return sections


def _extract_top_fields(content: str) -> dict[str, str]:
    # Parse "Key: Value" pairs before the first "## " section heading.
    first_h2 = _H2_RE.search(content)
    head = content[: first_h2.start()] if first_h2 else content
    out: dict[str, str] = {}
    for m in _TOP_FIELD_RE.finditer(head):
        key = m.group(1).strip()
        value = m.group(2).strip()
        out[key] = value
    return out


def _parse_bulleted_paths(section_body: str) -> list[str]:
    paths: list[str] = []
    for raw in section_body.splitlines():
        line = raw.strip()
        if not line.startswith("-"):
            continue
        item = line.lstrip("-").strip()
        item = item.strip("`").strip()
        if not item:
            continue
        paths.append(item)
    return paths


def parse_handoff_markdown(content: str, *, path_for_errors: str = "<handoff>") -> HandoffDoc:
    sections = _split_h2_sections(content)
    top = _extract_top_fields(content)

    def require_top(name: str) -> str:
        value = top.get(name, "").strip()
        if not value:
            raise ValueError(f"{path_for_errors}: missing required top field: {name}")
        return value

    run_id = require_top("Run ID")
    delegated_phases_raw = require_top("Delegated Phases")
    delegation_origin = require_top("Delegation Origin")
    phase = require_top("Phase")
    requirement_ids = require_top("Requirement IDs")
    assigned_worktree_path = require_top("Assigned Worktree Path")
    assigned_branch = require_top("Assigned Branch")
    created_at = require_top("Created At")

    delegated_phases: list[int] = []
    for part in re.split(r"[,\s]+", delegated_phases_raw.strip()):
        if not part:
            continue
        if part not in ("3", "4"):
            raise ValueError(f"{path_for_errors}: Delegated Phases must be 3, 4, or 3,4 (got: {delegated_phases_raw!r})")
        delegated_phases.append(int(part))
    delegated_phases = sorted(set(delegated_phases))
    if not delegated_phases:
        raise ValueError(f"{path_for_errors}: Delegated Phases must include at least one of: 3, 4")

    lock_body = sections.get("Lock", "").strip()
    if not lock_body:
        raise ValueError(f"{path_for_errors}: missing required section: ## Lock")

    lock_algorithm = ""
    lock_hash = ""
    for line in lock_body.splitlines():
        m = re.match(r"^[ \t]*Algorithm:\s*(.+?)\s*$", line)
        if m:
            lock_algorithm = m.group(1).strip()
        m = re.match(r"^[ \t]*Hash:\s*(.+?)\s*$", line)
        if m:
            lock_hash = m.group(1).strip()

    if not lock_algorithm:
        raise ValueError(f"{path_for_errors}: ## Lock missing 'Algorithm: ...'")
    if not lock_hash:
        raise ValueError(f"{path_for_errors}: ## Lock missing 'Hash: ...'")

    input_body = sections.get("Input Artifacts", "").strip()
    if not input_body:
        raise ValueError(f"{path_for_errors}: missing required section: ## Input Artifacts")
    input_artifacts = _parse_bulleted_paths(input_body)
    if not input_artifacts:
        raise ValueError(f"{path_for_errors}: ## Input Artifacts must list at least one artifact path")

    updates_body = sections.get("Required Artifact Updates", "").strip()
    if not updates_body:
        raise ValueError(f"{path_for_errors}: missing required section: ## Required Artifact Updates")
    required_artifact_updates = _parse_bulleted_paths(updates_body)
    if not required_artifact_updates:
        raise ValueError(f"{path_for_errors}: ## Required Artifact Updates must list at least one artifact path")

    required_artifact_updates = sorted(set(required_artifact_updates))
    allowed_update_files = {"03-implementation-summary.md", "04-test-summary.md"}
    for p in required_artifact_updates:
        name = Path(p.strip().strip("`")).name
        if name not in allowed_update_files:
            raise ValueError(
                f"{path_for_errors}: ## Required Artifact Updates may only include: {', '.join(sorted(allowed_update_files))} (got: {p})"
            )

    # Ensure required updates are consistent with delegated phases.
    updates_joined = "\n".join(required_artifact_updates)
    if 3 in delegated_phases and "03-implementation-summary.md" not in updates_joined:
        raise ValueError(
            f"{path_for_errors}: Delegated Phases includes 3 but Required Artifact Updates does not include 03-implementation-summary.md"
        )
    if 4 in delegated_phases and "04-test-summary.md" not in updates_joined:
        raise ValueError(
            f"{path_for_errors}: Delegated Phases includes 4 but Required Artifact Updates does not include 04-test-summary.md"
        )

    # Validate required sections exist (bodies may be empty in rare cases, but headings must exist).
    required_sections = [
        "Required Artifact Updates",
        "Current Worktree State Rules",
        "Scope In",
        "Scope Out",
        "Required Verification",
        "Artifact Ownership",
        "Stop Conditions",
        "Completion Conditions",
    ]
    for s in required_sections:
        if s not in sections:
            raise ValueError(f"{path_for_errors}: missing required section heading: ## {s}")
        if not sections[s].strip():
            raise ValueError(f"{path_for_errors}: required section is empty: ## {s}")

    # For delegated Phase 4 testing, enforce an explicit evidence JSON path contract.
    if 4 in delegated_phases:
        required_verification = sections.get("Required Verification", "")
        m = _EVIDENCE_JSON_RE.search(required_verification or "")
        if not m:
            raise ValueError(
                f"{path_for_errors}: Delegated Phases includes 4 but ## Required Verification is missing an Evidence JSON line"
            )
        evidence_path = m.group(1).strip()
        normalized = evidence_path.lstrip("/\\").replace("\\", "/")
        expected_prefix = f".codex/rlm/{run_id}/"
        if expected_prefix not in normalized:
            raise ValueError(
                f"{path_for_errors}: Evidence JSON path must be under {expected_prefix} (got: {evidence_path})"
            )
        if not normalized.lower().endswith(".json"):
            raise ValueError(
                f"{path_for_errors}: Evidence JSON path must end with .json (got: {evidence_path})"
            )

    # Optional: required tracked worktree changes (paths are repo-relative).
    worktree_changes_body = sections.get("Required Worktree Changes", "").strip()
    required_worktree_changes: list[str] = []
    if worktree_changes_body:
        required_worktree_changes = _parse_bulleted_paths(worktree_changes_body)
        if not required_worktree_changes:
            raise ValueError(f"{path_for_errors}: ## Required Worktree Changes must list at least one path when present")
        required_worktree_changes = sorted(set(required_worktree_changes))

    return HandoffDoc(
        run_id=run_id,
        delegated_phases=delegated_phases,
        delegation_origin=delegation_origin,
        phase=phase,
        requirement_ids=requirement_ids,
        assigned_worktree_path=assigned_worktree_path,
        assigned_branch=assigned_branch,
        created_at=created_at,
        lock_algorithm=lock_algorithm,
        lock_hash=lock_hash,
        input_artifacts=input_artifacts,
        required_artifact_updates=required_artifact_updates,
        required_worktree_changes=required_worktree_changes,
        sections=sections,
    )


def read_handoff(path: Path) -> tuple[str, HandoffDoc]:
    content = path.read_text(encoding="utf-8")
    doc = parse_handoff_markdown(content, path_for_errors=str(path))
    return content, doc
