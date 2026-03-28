"""Read paths for `gotta notes`."""

from __future__ import annotations

import argparse
import json

from gotta.notes.render import actor_notes_payload, render_actor_notes_markdown
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope
from gotta.session.activity.note import _record_note_check
from gotta.session.status.payload.main import _actor_status_payload

from .render import render_session_text


def _entry_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def show_notes(args: argparse.Namespace) -> int:
    work_dir, scoped_actor = session_scope._observation_scope(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    if scoped_actor:
        _record_note_check(work_dir, scoped_actor)
        status = _actor_status_payload(work_dir, scoped_actor)
        payload = actor_notes_payload(
            work_dir,
            scoped_actor,
            label=session_registry._actor_label(scoped_actor, work_dir=work_dir),
            status_payload=status,
        )
        if args.output == "json":
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(
            render_actor_notes_markdown(
                work_dir,
                scoped_actor,
                label=session_registry._actor_label(scoped_actor, work_dir=work_dir),
                status_payload=status,
            ),
            end="",
        )
        return 0
    actor_ids = list(session_scope._target_actor_ids(work_dir))
    if not actor_ids:
        raise SystemExit(
            "no actors bound for this session; bind one intentionally with "
            + session_registry._actor_bind_examples(prefix="gotta actor bind")
        )
    actor_payloads: dict[str, dict[str, object]] = {}
    entries: list[dict[str, object]] = []
    for actor_name in actor_ids:
        status = _actor_status_payload(work_dir, actor_name)
        actor_payload = actor_notes_payload(
            work_dir,
            actor_name,
            label=session_registry._actor_label(actor_name, work_dir=work_dir),
            status_payload=status,
        )
        actor_payloads[actor_name] = actor_payload
        for record in _entry_records(actor_payload.get("entries")):
            payload = dict(record)
            payload.setdefault("actor", actor_name)
            payload.setdefault(
                "label",
                session_registry._actor_label(actor_name, work_dir=work_dir),
            )
            entries.append(payload)
    entries.sort(key=lambda item: str(item.get("timestamp") or ""))
    payload = {
        "session_root": str(work_dir),
        "actor_count": len(actor_ids),
        "actors": actor_payloads,
        "entry_count": len(entries),
        "entries": entries,
    }
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        render_session_text(
            actor_count=len(actor_ids),
            entry_count=len(entries),
            entries=entries,
        )
    )
    return 0
