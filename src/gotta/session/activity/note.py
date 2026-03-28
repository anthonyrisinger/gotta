"""Actor note-check feedback state helpers."""

from __future__ import annotations

from pathlib import Path

from gotta.compat import UTC, datetime
from gotta.content.context import current_actor

from gotta.session.registry import (
    _normalize_actor_name,
    _write_actor_state,
)

from .summary import _actor_note_check_summary, _int_value


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
            "note_checks_since_update": _int_value(
                summary.get("note_checks_since_update")
            )
            + 1,
            "last_note_check_at": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_note_check_by": normalized_reader,
        },
    )
