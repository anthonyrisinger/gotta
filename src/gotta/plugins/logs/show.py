"""Read paths for `gotta logs`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypeVar

from gotta.actor import writer_role
from gotta.content.scope import session_is_initialized
from gotta.logs import log_records, logs_state_path
from gotta.session import charter as session_charter
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope
from gotta import topology

from .model import (
    ActorLogsPayload,
    AggregateLogsPayload,
    SessionLogRecord,
    SessionLogsPayload,
)
from .render import render_follow_text, render_session_text

RecordT = TypeVar("RecordT")


def _int_value(value: object) -> int:
    return value if isinstance(value, int) else 0


def _limited_records(
    records: list[RecordT],
    *,
    limit: int,
) -> list[RecordT]:
    bounded = max(limit, 0)
    return records[-bounded:] if bounded > 0 else records


def _aggregate_logs(
    work_dir: Path,
    *,
    actor_name: str | None = None,
) -> tuple[list[str], list[SessionLogRecord]]:
    actor_ids = list(session_scope._target_actor_ids(work_dir, actor_name))
    records: list[SessionLogRecord] = []
    for current_actor in actor_ids:
        actor_root = session_registry._actor_session_dir(work_dir, current_actor)
        for record in log_records(actor_root):
            if (
                actor_root.resolve().parent.name == "actors"
                and writer_role(
                    actor_root, current_actor, writer=str(record.get("actor") or "")
                )
                == "foreign"
            ):
                continue
            payload: SessionLogRecord = {
                **record,
                "actor": record["actor"] or current_actor,
                "label": session_registry._actor_label(
                    current_actor, work_dir=work_dir
                ),
            }
            records.append(payload)
    records.sort(key=lambda item: str(item.get("timestamp") or ""))
    return actor_ids, records


def _is_exact_session_root(work_dir: Path) -> bool:
    resolved = work_dir.resolve()
    return (
        resolved.parent.name != "actors"
        and topology.parse_grouped_session_root(resolved) is None
        and topology.parse_shared_session_root(resolved) is None
        and session_is_initialized(resolved)
    )


def _actor_payload(
    work_dir: Path,
    *,
    actor_name: str,
    limit: int,
) -> ActorLogsPayload:
    visible = [
        record
        for record in log_records(work_dir)
        if writer_role(work_dir, actor_name, writer=str(record.get("actor") or ""))
        != "foreign"
    ]
    entries = sorted(visible, key=lambda item: str(item.get("timestamp") or ""))
    return {
        "state_path": str(logs_state_path(work_dir)),
        "locator": session_charter._native_surface_locator(
            "logs", actor_name=actor_name
        ),
        "follow_command": session_charter._native_surface_follow_command(
            "logs",
            actor_name=actor_name,
        ),
        "entry_count": len(entries),
        "entries": _limited_records(entries, limit=limit),
    }


def _session_payload(work_dir: Path, *, limit: int) -> SessionLogsPayload:
    entries = sorted(
        log_records(work_dir), key=lambda item: str(item.get("timestamp") or "")
    )
    return {
        "session_root": str(work_dir),
        "actor_count": 0,
        "actors": [],
        "entry_count": len(entries),
        "entries": _limited_records(entries, limit=limit),
        "state_path": str(logs_state_path(work_dir)),
        "locator": session_charter._native_surface_locator("logs"),
        "follow_command": session_charter._native_surface_follow_command("logs"),
    }


def _aggregate_payload(
    work_dir: Path,
    *,
    actor_ids: list[str],
    records: list[SessionLogRecord],
    limit: int,
) -> AggregateLogsPayload:
    return {
        "session_root": str(work_dir),
        "actor_count": len(actor_ids),
        "actors": actor_ids,
        "entry_count": len(records),
        "entries": _limited_records(records, limit=limit),
        "state_paths": {
            actor: str(
                logs_state_path(session_registry._actor_session_dir(work_dir, actor))
            )
            for actor in actor_ids
        },
        "locators": {
            actor: session_charter._native_surface_locator("logs", actor_name=actor)
            for actor in actor_ids
        },
        "follow_commands": {
            actor: session_charter._native_surface_follow_command(
                "logs", actor_name=actor
            )
            for actor in actor_ids
        },
    }


def show_logs(args: argparse.Namespace) -> int:
    work_dir, scoped_actor = session_scope._observation_scope(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    if scoped_actor:
        payload = _actor_payload(work_dir, actor_name=scoped_actor, limit=args.limit)
        if args.output == "json":
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(
            render_follow_text(
                follow_command=payload["follow_command"],
                entry_count=_int_value(payload.get("entry_count")),
                entries=payload["entries"],
                default_actor=scoped_actor,
            )
        )
        return 0
    actor_ids, records = _aggregate_logs(work_dir)
    if not actor_ids:
        if not _is_exact_session_root(work_dir):
            raise SystemExit(
                "no actors bound for this session; bind one intentionally with "
                + session_registry._actor_bind_examples(prefix="gotta actor bind")
            )
        payload = _session_payload(work_dir, limit=args.limit)
        if args.output == "json":
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(
            render_follow_text(
                follow_command=payload["follow_command"],
                entry_count=_int_value(payload.get("entry_count")),
                entries=payload["entries"],
                default_actor="session",
            )
        )
        return 0
    payload = _aggregate_payload(
        work_dir,
        actor_ids=actor_ids,
        records=records,
        limit=args.limit,
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        render_session_text(
            actor_count=_int_value(payload.get("actor_count")),
            entry_count=_int_value(payload.get("entry_count")),
            entries=payload["entries"],
        )
    )
    return 0
