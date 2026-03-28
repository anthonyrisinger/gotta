"""Mutation paths for `gotta logs`."""

from __future__ import annotations

import argparse
from pathlib import Path

from gotta.actor import require_writer, session_actor, writer_name
from gotta.logs import append_log_record, logs_state_path
from gotta.session import charter as session_charter
from gotta.session import scope as session_scope
from gotta.session.activity.record import _record_session_activity


def _write_target(args: argparse.Namespace) -> tuple[Path, str]:
    work_dir = session_scope._session_dir(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    writer = writer_name()
    target_actor = (
        session_actor(work_dir) if work_dir.resolve().parent.name == "actors" else ""
    )
    if target_actor:
        require_writer(
            work_dir,
            target_actor,
            writer=writer,
            action="write into this actor branch",
        )
    return work_dir, writer or ""


def extend_logs(args: argparse.Namespace) -> int:
    work_dir, record_actor = _write_target(args)
    entries = session_charter._read_text_items_source(
        session_root=work_dir,
        inline_items=list(args.value or []),
        from_file=args.from_file,
        use_stdin=args.use_stdin,
        input_name="log entry text",
    )
    for entry in entries:
        append_log_record(
            work_dir,
            message=session_charter._normalize_entry_text(
                entry, input_name="log entry text"
            ),
            actor=record_actor,
        )
    _record_session_activity(
        work_dir,
        plugin="logs",
        surface="logs",
        action="extend",
        actor=record_actor,
        target=logs_state_path(work_dir),
        detail=f"extended logs with {len(entries)} item(s)",
    )
    print(f"extended logs entries: {len(entries)} item(s)")
    return 0


def append_logs(args: argparse.Namespace) -> int:
    work_dir, record_actor = _write_target(args)
    payload = session_charter._read_text_source(
        session_root=work_dir,
        inline=(args.value[0] if args.value else None),
        from_file=args.from_file,
        use_stdin=args.use_stdin,
        input_name="log entry text",
    )
    append_log_record(
        work_dir,
        message=session_charter._normalize_entry_text(
            payload, input_name="log entry text"
        ),
        actor=record_actor,
    )
    _record_session_activity(
        work_dir,
        plugin="logs",
        surface="logs",
        action="append",
        actor=record_actor,
        target=logs_state_path(work_dir),
        detail="appended 1 logs entry",
    )
    print("appended logs entry")
    return 0
