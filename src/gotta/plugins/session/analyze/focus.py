"""Shared focus-mode helpers for session analysis."""

from __future__ import annotations

from .model import AnalyzeScanEntry, AnalyzeScanPayload


def focus_match_threshold(best_score: int) -> int:
    if best_score <= 0:
        return 0
    if best_score >= 4:
        return 2
    return best_score


def ordered_focus_scan_entries(
    scan_payload: AnalyzeScanPayload | None,
    *,
    limit: int,
) -> list[AnalyzeScanEntry]:
    if scan_payload is None:
        return []
    entries = [dict(entry) for entry in scan_payload.get("entries") or []]
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
