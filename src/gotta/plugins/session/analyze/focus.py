"""Shared focus-mode helpers for session analysis."""

from __future__ import annotations

from typing import Any


def focus_match_threshold(best_score: int) -> int:
    if best_score <= 0:
        return 0
    if best_score >= 4:
        return 2
    return best_score


def ordered_focus_scan_entries(
    scan_payload: dict[str, Any] | None,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(scan_payload, dict):
        return []
    entries = [
        dict(entry)
        for entry in scan_payload.get("entries") or []
        if isinstance(entry, dict)
    ]
    ordered = sorted(
        entries,
        key=lambda entry: str(
            entry.get("lastFetchedAt") or entry.get("fetched_at") or ""
        ),
        reverse=True,
    )
    ordered = sorted(
        ordered,
        key=lambda entry: int(entry.get("hitCount") or 0),
        reverse=True,
    )
    ordered = sorted(
        ordered,
        key=lambda entry: (
            str(entry.get("artifactKind") or entry.get("artifact_kind") or "")
            != "evidence"
        ),
    )
    return ordered[: max(limit, 0)]
