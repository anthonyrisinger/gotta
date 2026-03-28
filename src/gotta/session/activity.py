"""Session activity, actor events, and note/evidence summaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

from gotta.compat import UTC, datetime
from gotta.content import (
    append_activity_event,
    current_actor,
    ensure_private_dir,
    PRIVATE_FILE_MODE,
    session_identity,
)
from gotta.friction import oops_log_path
from gotta.logs import append_log_record, logs_state_path
from gotta.notes import visible_actor_notes_records
from gotta.actor import (
    writer_role,
)
from gotta.todo import (
    todo_state_path,
)

from .charter import (
    _native_surface_follow_command,
    _native_surface_locator,
    _native_surface_preferred_name,
    _session_relative_locator,
    _surface_actor_scope,
)
from .registry import (
    ACTOR_STALL_SECONDS,
    WANT_FILE,
    _actor_events_path,
    _normalize_actor_name,
    _read_actor_state,
    _write_actor_state,
)


def _append_chunk(path: Path, chunk: str) -> None:
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, PRIVATE_FILE_MODE)
    try:
        os.write(fd, chunk.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        path.chmod(PRIVATE_FILE_MODE)
    except OSError:
        pass


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    _append_chunk(path, json.dumps(payload, sort_keys=True) + "\n")


def _record_session_activity(
    work_dir: Path,
    *,
    plugin: str,
    surface: str,
    action: str,
    actor: str = "",
    target_actor: str = "",
    target: Path | None = None,
    locator: str = "",
    preferred_name: str = "",
    follow_command: str = "",
    detail: str = "",
) -> None:
    if target is not None:
        resolved = target.resolve()
        actor_scope = _surface_actor_scope(work_dir)
        if resolved == (work_dir / WANT_FILE).resolve():
            resolved_locator = _native_surface_locator("want", actor_name=actor_scope)
            resolved_name = _native_surface_preferred_name(
                "want", actor_name=actor_scope
            )
            resolved_follow = _native_surface_follow_command(
                "want", actor_name=actor_scope
            )
        elif resolved == (work_dir / "GOAL.md").resolve():
            resolved_locator = _native_surface_locator("goal", actor_name=actor_scope)
            resolved_name = _native_surface_preferred_name(
                "goal", actor_name=actor_scope
            )
            resolved_follow = _native_surface_follow_command(
                "goal", actor_name=actor_scope
            )
        elif resolved == todo_state_path(work_dir).resolve():
            resolved_locator = _native_surface_locator("todo", actor_name=actor_scope)
            resolved_name = _native_surface_preferred_name(
                "todo", actor_name=actor_scope
            )
            resolved_follow = _native_surface_follow_command(
                "todo", actor_name=actor_scope
            )
        elif resolved == logs_state_path(work_dir).resolve():
            resolved_locator = _native_surface_locator("logs", actor_name=actor_scope)
            resolved_name = _native_surface_preferred_name(
                "logs", actor_name=actor_scope
            )
            resolved_follow = _native_surface_follow_command(
                "logs", actor_name=actor_scope
            )
        elif resolved == oops_log_path(work_dir).resolve():
            resolved_locator = _native_surface_locator("oops", actor_name=actor_scope)
            resolved_name = _native_surface_preferred_name(
                "oops", actor_name=actor_scope
            )
            resolved_follow = _native_surface_follow_command(
                "oops", actor_name=actor_scope
            )
        else:
            resolved_locator = _session_relative_locator(work_dir, resolved)
            resolved_name = resolved.name
            resolved_follow = f"gotta read {resolved_locator!r}"
    else:
        resolved_locator = locator.strip() or f"{plugin}:{surface}"
        resolved_name = preferred_name.strip() or resolved_locator
        resolved_follow = follow_command.strip()
    activity_actor = actor.strip() or current_actor(
        default_actor=session_identity(work_dir)
    )
    payload = {
        "plugin": plugin,
        "surface": surface,
        "action": action,
        "actor": activity_actor,
        "locator": resolved_locator,
        "preferred_name": preferred_name.strip() or resolved_name,
        "follow_command": follow_command.strip() or resolved_follow,
        "detail": detail,
        "time_field": "session_recorded_at",
    }
    normalized_target = (
        _normalize_actor_name(target_actor) if target_actor.strip() else ""
    )
    if normalized_target and normalized_target != activity_actor:
        payload["target_actor"] = normalized_target
    append_activity_event(work_dir, payload)


def _append_actor_event(
    work_dir: Path,
    actor_name: str,
    *,
    event: str,
    detail: str = "",
    extra: dict[str, object] | None = None,
    author: str = "",
) -> None:
    normalized_actor = _normalize_actor_name(actor_name)
    event_author = author.strip() or current_actor(default_actor=normalized_actor)
    payload: dict[str, object] = {
        "timestamp": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": normalized_actor,
        "author": event_author,
        "event": event,
        "detail": detail,
    }
    if extra:
        payload.update(extra)
    _append_jsonl(_actor_events_path(work_dir, actor_name), payload)
    if event != "heartbeat":
        _record_session_activity(
            work_dir,
            plugin="actor",
            surface="actor.lifecycle",
            action=event,
            actor=event_author,
            target_actor=normalized_actor,
            locator=f"actor:{payload['actor']}",
            preferred_name=str(payload["actor"]),
            follow_command=f"gotta actor status {payload['actor']}",
            detail=detail or event,
        )


def _actor_log_line(
    session_root: Path, actor_name: str, message: str, *, author: str = ""
) -> None:
    normalized_actor = _normalize_actor_name(actor_name)
    log_author = author.strip() or current_actor(default_actor=normalized_actor)
    if log_author == normalized_actor:
        rendered = f"[{normalized_actor}] {message}"
    else:
        rendered = f"[{log_author} -> {normalized_actor}] {message}"
    append_log_record(session_root, message=rendered, actor=log_author)


def _record_actor_surface_activity(
    session_root: Path,
    *,
    actor_name: str,
    surface: str,
    action: str,
    detail: str,
    actor: str = "",
) -> None:
    normalized_actor = _normalize_actor_name(actor_name)
    _record_session_activity(
        session_root,
        plugin="actor",
        surface=surface,
        action=action,
        actor=actor.strip() or current_actor(default_actor=normalized_actor),
        target_actor=normalized_actor,
        locator=_native_surface_locator(surface, actor_name=normalized_actor),
        preferred_name=_native_surface_preferred_name(
            surface, actor_name=normalized_actor
        ),
        follow_command=_native_surface_follow_command(
            surface, actor_name=normalized_actor
        ),
        detail=detail,
    )


def _actor_evidence_summary(work_dir: Path, actor_name: str) -> dict[str, object]:
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
    return {
        "artifact_count": len(entries),
        "last_artifact_at": str(ordered[0].get("fetched_at") or "").strip()
        if ordered
        else "",
        "recent_artifacts": [
            {
                "locator": str(
                    entry.get("canonical_locator") or entry.get("locator") or ""
                ).strip(),
                "preferred_name": str(entry.get("preferred_name") or "data").strip(),
                "fetched_at": str(entry.get("fetched_at") or "").strip(),
            }
            for entry in ordered[:5]
        ],
    }


def _actor_evidence_note(evidence: dict[str, object]) -> str:
    artifact_count = int(evidence.get("artifact_count") or 0)
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


def _actor_event_records(work_dir: Path, actor_name: str) -> list[dict[str, object]]:
    path = _actor_events_path(work_dir, actor_name)
    if not path.exists():
        return []
    normalized_actor = _normalize_actor_name(actor_name)
    events: list[dict[str, object]] = []
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
    return events


def _actor_recent_activity(
    work_dir: Path, actor_name: str, *, limit: int = 5
) -> dict[str, object]:
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
            int(item.get("_order") or 0),
        ),
        reverse=True,
    )
    lifecycle = [item for item in ordered if str(item.get("event") or "") != "note"]
    recent_activity = [
        {
            "timestamp": str(item.get("timestamp") or ""),
            "event": str(item.get("event") or ""),
            "author": str(item.get("author") or ""),
            "detail": str(item.get("detail") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for item in ordered[:limit]
    ]
    recent_lifecycle = [
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


def _actor_note_summary(work_dir: Path, actor_name: str) -> dict[str, object]:
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


def _actor_note_check_summary(work_dir: Path, actor_name: str) -> dict[str, object]:
    state = _read_actor_state(work_dir, actor_name)
    try:
        count = int(state.get("note_checks_since_update") or 0)
    except (TypeError, ValueError):
        count = 0
    return {
        "note_checks_since_update": max(count, 0),
        "last_note_check_at": str(state.get("last_note_check_at") or ""),
        "last_note_check_by": str(state.get("last_note_check_by") or ""),
    }


def _reset_note_check_feedback(work_dir: Path, actor_name: str) -> None:
    _write_actor_state(
        work_dir,
        actor_name,
        {
            "note_checks_since_update": 0,
            "last_note_check_at": None,
            "last_note_check_by": None,
        },
    )


def _record_note_check(work_dir: Path, actor_name: str, *, reader: str = "") -> None:
    normalized_actor = _normalize_actor_name(actor_name)
    normalized_reader = _normalize_actor_name(
        reader.strip() or current_actor(default_actor="")
    )
    if not normalized_reader or normalized_reader == normalized_actor:
        return
    summary = _actor_note_check_summary(work_dir, normalized_actor)
    _write_actor_state(
        work_dir,
        normalized_actor,
        {
            "note_checks_since_update": int(
                summary.get("note_checks_since_update") or 0
            )
            + 1,
            "last_note_check_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_note_check_by": normalized_reader,
        },
    )
