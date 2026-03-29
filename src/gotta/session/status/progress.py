"""Actor progress synthesis helpers."""

from __future__ import annotations

import json
from pathlib import Path
import time

from gotta.compat import datetime
from gotta.friction import OOPS_CHANNEL, visible_channel_records
from gotta.notes.file import visible_actor_notes_records
from gotta.session.activity.summary import _actor_activity_summary
from gotta.session.registry import (
    ACTOR_STALL_SECONDS,
    _actor_session_dir,
    _normalize_actor_name,
)
from gotta.session.status.payload.model import LifecycleEntry, ProgressSummary


def _rank_value(value: object) -> int:
    try:
        return int(str(value or 0))
    except ValueError:
        return 0


def _actor_progress_summary(
    work_dir: Path, actor_name: str, *, limit: int = 5
) -> ProgressSummary:
    normalized_actor = _normalize_actor_name(actor_name)
    actor_root = _actor_session_dir(work_dir, normalized_actor)
    events: list[dict[str, object]] = []
    order = 0

    def append_progress_event(
        *,
        timestamp: str,
        event: str,
        detail: str,
        summary: str,
        priority: int,
    ) -> None:
        nonlocal order
        cleaned_timestamp = timestamp.strip()
        cleaned_detail = detail.strip()
        if not cleaned_timestamp or not cleaned_detail:
            return
        events.append(
            {
                "timestamp": cleaned_timestamp,
                "event": event,
                "author": normalized_actor,
                "detail": cleaned_detail,
                "summary": summary.strip() or cleaned_detail,
                "_priority": priority,
                "_order": order,
            }
        )
        order += 1

    for record in visible_actor_notes_records(work_dir, normalized_actor):
        if str(record.get("author") or "").strip() != normalized_actor:
            continue
        message = str(record.get("message") or "").strip()
        append_progress_event(
            timestamp=str(record.get("timestamp") or ""),
            event="note",
            detail=message,
            summary=_actor_activity_summary(
                "note",
                message,
                author=normalized_actor,
                target_actor=normalized_actor,
            ),
            priority=4,
        )

    for record in visible_channel_records(actor_root, OOPS_CHANNEL):
        if str(record.get("actor") or "").strip() != normalized_actor:
            continue
        message = str(record.get("message") or "").strip()
        append_progress_event(
            timestamp=str(record.get("timestamp") or ""),
            event="oops",
            detail=message,
            summary=_actor_activity_summary(
                "oops",
                message,
                author=normalized_actor,
                target_actor=normalized_actor,
            ),
            priority=3,
        )

    manifest_path = work_dir / "content" / "manifest.jsonl"
    if manifest_path.exists():
        for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("actor") or "").strip() != normalized_actor:
                continue
            locator = str(
                payload.get("canonical_locator")
                or payload.get("locator")
                or payload.get("preferred_name")
                or ""
            ).strip()
            append_progress_event(
                timestamp=str(payload.get("fetched_at") or ""),
                event="evidence",
                detail=locator,
                summary=f"evidence: {locator}",
                priority=2,
            )

    ordered = sorted(
        events,
        key=lambda item: (
            str(item.get("timestamp") or ""),
            _rank_value(item.get("_priority")),
            _rank_value(item.get("_order")),
        ),
        reverse=True,
    )
    recent_progress: list[LifecycleEntry] = [
        {
            "timestamp": str(item.get("timestamp") or ""),
            "event": str(item.get("event") or ""),
            "author": str(item.get("author") or ""),
            "detail": str(item.get("detail") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for item in ordered[:limit]
    ]
    latest = recent_progress[0] if recent_progress else {}
    progress_kind = (
        "evidence"
        if any(str(item.get("event") or "") == "evidence" for item in ordered)
        else "narration"
        if ordered
        else "none"
    )
    progress_stale = False
    latest_timestamp = str(latest.get("timestamp") or "")
    if latest_timestamp:
        try:
            latest_dt = datetime.fromisoformat(latest_timestamp.replace("Z", "+00:00"))
        except ValueError:
            latest_dt = None
        if latest_dt is not None:
            progress_stale = (time.time() - latest_dt.timestamp()) > ACTOR_STALL_SECONDS
    return {
        "recent_progress": recent_progress,
        "last_activity_at": str(latest.get("timestamp") or ""),
        "last_activity_summary": str(latest.get("summary") or ""),
        "progress_kind": progress_kind,
        "progress_stale": progress_stale,
    }
