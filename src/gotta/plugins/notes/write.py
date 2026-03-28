"""Mutation paths for `gotta notes`."""

from __future__ import annotations

import argparse
from pathlib import Path

from gotta.actor import require_writer, session_actor, writer_name
from gotta.content.context import current_actor
from gotta.notes.file import append_actor_note
from gotta.session import bootstrap as session_bootstrap
from gotta.session import charter as session_charter
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope
from gotta.session.activity.note import _reset_note_check_feedback
from gotta.session.activity.record import (
    _actor_log_line,
    _append_actor_event,
    _record_actor_surface_activity,
)


def _require_bound_actor(work_dir: Path, actor_name: str) -> None:
    if actor_name not in session_scope._selected_actor_ids(work_dir):
        raise SystemExit(
            f"{actor_name} is not bound for this session; bind them first with "
            f"`gotta actor bind {session_registry._actor_label(actor_name, work_dir=work_dir)}`"
        )


def _append_actor_name(work_dir: Path, *, explicit_actor: str = "") -> str:
    if explicit_actor:
        actor_name = session_registry._resolve_bound_actor_name(
            work_dir, explicit_actor
        )
        _require_bound_actor(work_dir, actor_name)
        return actor_name
    actor_ids = list(session_scope._target_actor_ids(work_dir))
    if not actor_ids:
        raise SystemExit(
            "no actor is in scope here; bind or enter one first, or pass "
            "`--actor <actor>` explicitly"
        )
    rooted_actor = session_actor(work_dir)
    if rooted_actor and rooted_actor in actor_ids:
        return rooted_actor
    ambient_actor = current_actor(default_actor="")
    if ambient_actor and ambient_actor in actor_ids:
        return ambient_actor
    if len(actor_ids) > 1:
        raise SystemExit(
            "actor note append is ambiguous across multiple bound actors; pass "
            "`--actor <actor>` explicitly"
        )
    return actor_ids[0]


def _append_author_name(
    actor_name: str, *, explicit_actor: str = "", writer: str = ""
) -> str:
    if explicit_actor:
        return writer or actor_name
    return actor_name


def append_note(args: argparse.Namespace) -> int:
    session_dir = session_scope._session_dir(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    work_dir = session_dir.resolve()
    actor_name = _append_actor_name(
        work_dir,
        explicit_actor=str(getattr(args, "actor", None) or ""),
    )
    actor_writer = writer_name()
    author_name = _append_author_name(
        actor_name,
        explicit_actor=str(getattr(args, "actor", None) or ""),
        writer=actor_writer,
    )
    require_writer(
        work_dir,
        actor_name,
        writer=actor_writer,
        action="write into this actor branch",
    )
    session_bootstrap._ensure_actor_surface(work_dir, actor_name)
    message = session_charter._normalize_entry_text(
        session_charter._read_text_source(
            session_root=session_dir,
            inline=(args.value[0] if args.value else None),
            from_file=args.from_file,
            use_stdin=args.use_stdin,
            input_name="actor note",
        ),
        input_name="actor note",
    )
    append_actor_note(work_dir, actor_name, message=message, author=author_name)
    _reset_note_check_feedback(work_dir, actor_name)
    _append_actor_event(
        work_dir,
        actor_name,
        event="note",
        detail=message.splitlines()[0],
        author=author_name,
    )
    _actor_log_line(
        work_dir,
        actor_name,
        f"noted: {message.splitlines()[0]}",
        author=author_name,
    )
    _record_actor_surface_activity(
        work_dir,
        actor_name=actor_name,
        surface="notes",
        action="append",
        detail="appended actor note",
        actor=author_name,
    )
    print(f"appended actor note for {actor_name}")
    return 0
