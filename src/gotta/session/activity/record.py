"""Session activity and actor event recording helpers."""

from __future__ import annotations

from pathlib import Path

from gotta.compat import UTC, datetime
from gotta.content.activity import append_activity_event
from gotta.content.context import current_actor
from gotta.content.scope import session_identity
from gotta.friction import oops_log_path
from gotta.logs import append_log_record, logs_state_path
from gotta.todo import todo_state_path

from gotta.session.charter import (
    _native_surface_follow_command,
    _native_surface_locator,
    _native_surface_preferred_name,
    _session_relative_locator,
    _surface_actor_scope,
)
from gotta.session.registry import (
    WANT_FILE,
    _actor_events_path,
    _normalize_actor_name,
)

from .file import _append_jsonl


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
