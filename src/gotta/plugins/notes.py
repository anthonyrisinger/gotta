"""Top-level session-rooted actor notes surface."""

from __future__ import annotations

import argparse
import json

from gotta.actor import require_writer, session_actor, writer_name
from gotta.content.context import current_actor
from gotta.helptext import format_long_help, is_long_help_request
from gotta.notes import (
    append_actor_note,
    actor_notes_log_path,
    actor_notes_payload,
    render_actor_notes_markdown,
)
from gotta.session.activity.note import (
    _record_note_check,
    _reset_note_check_feedback,
)
from gotta.session.activity.record import (
    _actor_log_line,
    _append_actor_event,
    _record_actor_surface_activity,
)
from gotta.session import bootstrap as session_bootstrap
from gotta.session import charter as session_charter
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope
from gotta.session.status.payload import _actor_status_payload


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    session_charter.add_target_args(parser)


def build_parser(command_name: str = "gotta notes") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Inspect or mutate canonical actor notes inside the active session surface. "
            "Notes are the canonical actor-authored narration surface, and short one-line notes are valid."
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["show", "append"],
        help="show one or all actor note surfaces, or append a actor note",
    )
    parser.add_argument("value", nargs="*")
    _add_root_args(parser)
    parser.add_argument(
        "--from-file",
        help="read actor note text from a UTF-8 file instead of inline text; use '-' for stdin",
    )
    parser.add_argument(
        "--stdin",
        dest="use_stdin",
        action="store_true",
        help="read actor note text from stdin explicitly",
    )
    parser.add_argument("--output", choices=["json", "text"], default="text")
    return parser


def session_access_mode(argv: list[str]) -> str:
    positionals = session_charter.argv_positionals(
        argv,
        valued_flags=(
            "--session",
            "--actor",
            "--from-file",
            "--output",
        ),
    )
    action = positionals[0] if positionals else "show"
    return "write" if action == "append" else "read"


def _normalize_args(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    first = argv[0]
    if first in {"show", "append"} or first.startswith("-"):
        return argv
    return ["show", *argv]


def _summary_payload(work_dir, *, actor_name: str) -> dict[str, object]:
    status = _actor_status_payload(work_dir, actor_name)
    return {
        "actor": actor_name,
        "label": session_registry._actor_label(actor_name, work_dir=work_dir),
        "state_path": str(actor_notes_log_path(work_dir, actor_name)),
        "locator": f"notes:actor:{actor_name}",
        "follow_command": f"gotta notes --actor {actor_name}",
        "status": str(status.get("status") or "pending"),
        "voice": str(status.get("voice") or "missing"),
        "notes_status": str(status.get("notes_status") or "empty"),
        "artifact_count": int(status.get("artifact_count") or 0),
        "next_step": str(status.get("next_step") or ""),
    }


def _bound_actors(work_dir) -> list[str]:
    return list(session_scope._selected_actor_ids(work_dir))


def _require_bound_actor(work_dir, actor_name: str) -> None:
    if actor_name not in session_scope._selected_actor_ids(work_dir):
        raise SystemExit(
            f"{actor_name} is not bound for this session; bind them first with "
            f"`gotta actor bind {session_registry._actor_label(actor_name, work_dir=work_dir)}`"
        )


def _append_actor_name(work_dir, *, explicit_actor: str = "") -> str:
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


def main(argv: list[str] | None = None) -> int:
    argv = _normalize_args(list(argv or []))
    if is_long_help_request(argv):
        print(format_long_help(build_parser()))
        return 0
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if int(exc.code or 0) == 0:
            return 0
        raise
    action = args.action or "show"

    if action == "show":
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
                    label=session_registry._actor_label(
                        scoped_actor, work_dir=work_dir
                    ),
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
            actor_payloads[actor_name] = actor_notes_payload(
                work_dir,
                actor_name,
                label=session_registry._actor_label(actor_name, work_dir=work_dir),
                status_payload=status,
            )
            for record in actor_payloads[actor_name]["entries"]:
                payload = dict(record)
                payload.setdefault("actor", actor_name)
                payload.setdefault(
                    "label",
                    session_registry._actor_label(actor_name, work_dir=work_dir),
                )
                entries.append(payload)
        entries = sorted(entries, key=lambda item: str(item.get("timestamp") or ""))
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
        print(f"notes: session-wide across {len(actor_ids)} actor(s)")
        print(f"entries: {len(entries)}")
        if not entries:
            print("- none yet")
            return 0
        for record in entries:
            actor_name = str(record.get("actor") or "unknown-actor")
            author = str(record.get("author") or actor_name)
            timestamp = str(record.get("timestamp") or "unknown-time")
            message = str(record.get("message") or "").strip() or "empty note"
            lines = message.splitlines() or ["empty note"]
            print(f"- `{timestamp}` [{actor_name}/{author}] {lines[0]}")
            for continuation in lines[1:]:
                print(f"  {continuation}")
        return 0

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
