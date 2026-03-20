"""Top-level session-rooted actor notes surface."""

from __future__ import annotations

import argparse
import json

from gotta.helptext import format_long_help, is_long_help_request
from gotta.notes import (
    append_actor_note,
    actor_notes_log_path,
    actor_notes_payload,
    actor_notes_surface_path,
    render_actor_notes_markdown,
)
from gotta import session as session_plugin
from gotta.session import (
    _actor_label,
)


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    session_plugin.add_target_args(parser)


def build_parser(command_name: str = "gotta notes") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Inspect or mutate canonical actor notes inside the active session surface. "
            "Use this for live actor visibility instead of waiting on a separate summary artifact."
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
    positionals = session_plugin.argv_positionals(
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
    status = session_plugin._actor_status_payload(work_dir, actor_name)
    return {
        "actor": actor_name,
        "label": _actor_label(actor_name, work_dir=work_dir),
        "notes": str(actor_notes_surface_path(work_dir, actor_name)),
        "notes_log": str(actor_notes_log_path(work_dir, actor_name)),
        "status": str(status.get("status") or "pending"),
        "notes_status": str(status.get("notes_status") or "empty"),
        "artifact_count": int(status.get("artifact_count") or 0),
        "next_step": str(status.get("next_step") or ""),
    }


def _bound_actors(work_dir) -> list[str]:
    return list(session_plugin._selected_actor_ids(work_dir))


def _require_bound_actor(work_dir, actor_name: str) -> None:
    if actor_name not in session_plugin._selected_actor_ids(work_dir):
        raise SystemExit(
            f"{actor_name} is not bound for this session; bind them first with "
            f"`gotta actor bind {_actor_label(actor_name, work_dir=work_dir)}`"
        )


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
        if args.actor:
            session_dir = session_plugin._session_dir(
                explicit_session=getattr(args, "session", None),
                explicit_actor=getattr(args, "actor", None),
            )
            work_dir = session_dir.resolve()
            actor_name = session_plugin._resolve_bound_actor_name(work_dir, args.actor)
            _require_bound_actor(work_dir, actor_name)
            status = session_plugin._actor_status_payload(work_dir, actor_name)
            payload = actor_notes_payload(
                work_dir,
                actor_name,
                label=_actor_label(actor_name, work_dir=work_dir),
                status_payload=status,
            )
            if args.output == "json":
                print(json.dumps(payload, indent=2, sort_keys=True))
                return 0
            print(
                render_actor_notes_markdown(
                    work_dir,
                    actor_name,
                    label=_actor_label(actor_name, work_dir=work_dir),
                    status_payload=status,
                ),
                end="",
            )
            return 0

        work_dir = session_plugin._shared_session_dir(
            explicit_session=getattr(args, "session", None),
        )
        actor_ids = list(session_plugin._target_actor_ids(work_dir))
        if not actor_ids:
            raise SystemExit(
                "no actors bound for this session; bind one intentionally with "
                + session_plugin._actor_bind_examples(prefix="gotta actor bind")
            )
        actor_payloads: dict[str, dict[str, object]] = {}
        entries: list[dict[str, object]] = []
        for actor_name in actor_ids:
            status = session_plugin._actor_status_payload(work_dir, actor_name)
            actor_payloads[actor_name] = actor_notes_payload(
                work_dir,
                actor_name,
                label=_actor_label(actor_name, work_dir=work_dir),
                status_payload=status,
            )
            for record in actor_payloads[actor_name]["entries"]:
                payload = dict(record)
                payload.setdefault("actor", actor_name)
                payload.setdefault("label", _actor_label(actor_name, work_dir=work_dir))
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

    if not args.actor:
        raise SystemExit("missing actor; use `gotta notes append --actor <actor> ...`")
    session_dir = session_plugin._session_dir(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    work_dir = session_dir.resolve()
    actor_name = session_plugin._resolve_bound_actor_name(work_dir, args.actor or "")
    _require_bound_actor(work_dir, actor_name)
    session_plugin._ensure_actor_surface(work_dir, actor_name)
    message = session_plugin._normalize_entry_text(
        session_plugin._read_text_source(
            session_root=session_dir,
            inline=(args.value[0] if args.value else None),
            from_file=args.from_file,
            use_stdin=args.use_stdin,
            input_name="actor note",
        ),
        input_name="actor note",
    )
    append_actor_note(work_dir, actor_name, message=message)
    session_plugin._append_actor_event(work_dir, actor_name, event="note", detail=message.splitlines()[0])
    session_plugin._actor_log_line(work_dir, actor_name, f"noted: {message.splitlines()[0]}")
    session_plugin._sync_actor_projection_surfaces(work_dir, actor_name)
    session_plugin._record_actor_projection_activity(
        work_dir,
        actor_name=actor_name,
        surface="notes",
        action="append",
        log_path=actor_notes_log_path(work_dir, actor_name),
        projection_path=actor_notes_surface_path(work_dir, actor_name),
        detail="appended actor note",
    )
    print(f"appended actor note for {actor_name}")
    return 0
