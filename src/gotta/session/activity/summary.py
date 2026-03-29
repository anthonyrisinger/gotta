"""Actor evidence, lifecycle, and note summary helpers."""

from __future__ import annotations

import json
from pathlib import Path
import time

from gotta.actor import writer_role
from gotta.compat import datetime
from gotta.content.scope import session_identity
from gotta.notes.file import visible_actor_notes_records

from gotta.session.registry import (
    ACTOR_STALL_SECONDS,
    _actor_events_path,
    _normalize_actor_name,
    _read_actor_state,
)
from gotta.session.status.payload.model import (
    EvidenceArtifact,
    EvidenceSummary,
    LifecycleEntry,
    NoteCheckSummary,
    NoteSummary,
    RecentActivityPayload,
)


class _OrderedLifecycleEntry(LifecycleEntry):
    _order: int


def _int_value(value: object, *, default: int = 0) -> int:
    try:
        return int(str(value or default))
    except ValueError:
        return default


def _actor_evidence_summary(work_dir: Path, actor_name: str) -> EvidenceSummary:
    manifest_path = work_dir / "content" / "manifest.jsonl"
    entries: list[dict[str, object]] = []
    if manifest_path.exists():
        for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and str(
                payload.get("actor") or session_identity(work_dir)
            ) == _normalize_actor_name(actor_name):
                entries.append(payload)
    ordered = sorted(
        entries,
        key=lambda entry: (
            str(entry.get("fetched_at") or ""),
            str(entry.get("checksum") or ""),
        ),
        reverse=True,
    )
    recent_artifacts: list[EvidenceArtifact] = [
        {
            "locator": str(
                entry.get("canonical_locator") or entry.get("locator") or ""
            ).strip(),
            "preferred_name": str(entry.get("preferred_name") or "data").strip(),
            "fetched_at": str(entry.get("fetched_at") or "").strip(),
        }
        for entry in ordered[:5]
    ]
    return {
        "artifact_count": len(entries),
        "last_artifact_at": str(ordered[0].get("fetched_at") or "").strip()
        if ordered
        else "",
        "recent_artifacts": recent_artifacts,
    }


def _actor_evidence_note(evidence: EvidenceSummary) -> str:
    artifact_count = _int_value(evidence.get("artifact_count"))
    if artifact_count <= 0:
        return ""
    noun = "artifact" if artifact_count == 1 else "artifacts"
    return (
        f"{artifact_count} actor-attributed {noun} already live in session manifest, "
        "timeline, leads, and graph."
    )


def _actor_activity_summary(
    event: str,
    detail: str,
    *,
    author: str = "",
    target_actor: str = "",
) -> str:
    cleaned_detail = detail.strip()
    author_prefix = ""
    if author and target_actor and author != target_actor:
        author_prefix = f"{author}: "
    if event == "note":
        return (
            (author_prefix + cleaned_detail)
            if cleaned_detail
            else (author_prefix + "note").strip()
        )
    label = event.replace("_", " ")
    if cleaned_detail:
        return f"{label}: {author_prefix}{cleaned_detail}".strip()
    return f"{label}: {author_prefix}".strip(": ")


def _actor_event_records(work_dir: Path, actor_name: str) -> list[LifecycleEntry]:
    path = _actor_events_path(work_dir, actor_name)
    if not path.exists():
        return []
    normalized_actor = _normalize_actor_name(actor_name)
    events: list[_OrderedLifecycleEntry] = []
    for index, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event = str(payload.get("event") or "").strip()
        if not event or event == "heartbeat":
            continue
        timestamp = str(payload.get("timestamp") or "").strip()
        detail = str(payload.get("detail") or "").strip()
        author = str(payload.get("author") or "").strip()
        if (
            writer_role(work_dir, normalized_actor, writer=author or normalized_actor)
            == "foreign"
        ):
            continue
        events.append(
            {
                "timestamp": timestamp,
                "event": event,
                "author": author,
                "detail": detail,
                "summary": _actor_activity_summary(
                    event,
                    detail,
                    author=author,
                    target_actor=normalized_actor,
                ),
                "_order": index,
            }
        )
    return [
        {
            "timestamp": str(item.get("timestamp") or ""),
            "event": str(item.get("event") or ""),
            "author": str(item.get("author") or ""),
            "detail": str(item.get("detail") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for item in events
    ]


def _actor_recent_activity(
    work_dir: Path, actor_name: str, *, limit: int = 5
) -> RecentActivityPayload:
    events = _actor_event_records(work_dir, actor_name)
    if not events:
        return {
            "recent_activity": [],
            "recent_lifecycle": [],
            "last_lifecycle_at": "",
            "last_lifecycle_summary": "",
        }
    ordered = sorted(
        events,
        key=lambda item: (
            str(item.get("timestamp") or ""),
            _int_value(item.get("_order")),
        ),
        reverse=True,
    )
    lifecycle = [item for item in ordered if str(item.get("event") or "") != "note"]
    recent_activity: list[LifecycleEntry] = [
        {
            "timestamp": str(item.get("timestamp") or ""),
            "event": str(item.get("event") or ""),
            "author": str(item.get("author") or ""),
            "detail": str(item.get("detail") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for item in ordered[:limit]
    ]
    recent_lifecycle: list[LifecycleEntry] = [
        {
            "timestamp": str(item.get("timestamp") or ""),
            "event": str(item.get("event") or ""),
            "author": str(item.get("author") or ""),
            "detail": str(item.get("detail") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for item in lifecycle[:limit]
    ]
    latest = recent_lifecycle[0] if recent_lifecycle else {}
    return {
        "recent_activity": recent_activity,
        "recent_lifecycle": recent_lifecycle,
        "last_lifecycle_at": str(latest.get("timestamp") or ""),
        "last_lifecycle_summary": str(latest.get("summary") or ""),
    }


def _actor_note_summary(work_dir: Path, actor_name: str) -> NoteSummary:
    normalized_actor = _normalize_actor_name(actor_name)
    notes: list[dict[str, str]] = []
    for record in visible_actor_notes_records(work_dir, normalized_actor):
        if str(record.get("author") or "").strip() != normalized_actor:
            continue
        timestamp = str(record.get("timestamp") or "").strip()
        message = str(record.get("message") or "").strip()
        if not timestamp or not message:
            continue
        first_line = message.splitlines()[0] if message.splitlines() else message
        notes.append(
            {
                "timestamp": timestamp,
                "summary": first_line.strip() or message,
            }
        )
    ordered = sorted(
        notes, key=lambda item: str(item.get("timestamp") or ""), reverse=True
    )
    latest = ordered[0] if ordered else {}
    notes_stale = False
    latest_timestamp = str(latest.get("timestamp") or "")
    if latest_timestamp:
        try:
            latest_dt = datetime.fromisoformat(latest_timestamp.replace("Z", "+00:00"))
        except ValueError:
            latest_dt = None
        if latest_dt is not None:
            notes_stale = (time.time() - latest_dt.timestamp()) > ACTOR_STALL_SECONDS
    return {
        "last_note_at": latest_timestamp,
        "last_note_summary": str(latest.get("summary") or ""),
        "notes_stale": notes_stale,
    }


def _actor_note_check_summary(work_dir: Path, actor_name: str) -> NoteCheckSummary:
    state = _read_actor_state(work_dir, actor_name)
    count = _int_value(state.get("note_checks_since_update"))
    return {
        "note_checks_since_update": max(count, 0),
        "last_note_check_at": str(state.get("last_note_check_at") or ""),
        "last_note_check_by": str(state.get("last_note_check_by") or ""),
    }
