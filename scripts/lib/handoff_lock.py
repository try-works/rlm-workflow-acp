from __future__ import annotations

import hashlib
import re


_HASH_LINE_RE = re.compile(r"(?m)^[ \t]*Hash:.*(?:\n|$)")


def normalize_handoff_for_hash(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    # Per contract: remove the Hash line entirely before hashing.
    return _HASH_LINE_RE.sub("", normalized)


def compute_handoff_sha256(content: str) -> str:
    normalized = normalize_handoff_for_hash(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def verify_handoff_hash(content: str, expected_hash: str) -> tuple[bool, str]:
    actual = compute_handoff_sha256(content)
    ok = actual.lower() == expected_hash.strip().lower()
    return ok, actual

