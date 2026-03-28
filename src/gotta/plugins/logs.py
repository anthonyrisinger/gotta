"""Top-level session-rooted procedural trace surface."""

from __future__ import annotations

import argparse
import json

from gotta.actor import require_writer, session_actor, writer_name, writer_role
from gotta.content.scope import session_is_initialized
from gotta.helptext import format_long_help, is_long_help_request
from gotta.logs import append_log_record, log_records, logs_state_path
from gotta.session.activity.record import _record_session_activity
from gotta.session import charter as session_charter
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope
from gotta import topology


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    session_charter.add_target_args(parser)


def build_parser(command_name: str = "gotta logs") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Inspect or mutate the canonical session procedural trace. "
            "Shared-session read paths show all bound actor logs by default. "
            "Use notes for actor-authored narration; use logs for chronology and system/runtime trace."
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["show", "append", "extend"],
        help="show entries, append one entry, or extend with many entries",
    )
    parser.add_argument(
        "value",
        nargs="*",
        help="inline log text for append/extend",
    )
    _add_root_args(parser)
    parser.add_argument(
        "--from-file",
        help="read log text from a UTF-8 file instead of inline text; use '-' for stdin",
    )
    parser.add_argument(
        "--stdin",
        dest="use_stdin",
        action="store_true",
        help="read log text from stdin explicitly",
    )
    parser.add_argument("--output", choices=["json", "text"], default="text")
    parser.add_argument("--limit", type=int, default=0)
    return parser


def session_access_mode(argv: list[str]) -> str:
    positionals = session_charter.argv_positionals(
        argv,
        valued_flags=(
            "--session",
            "--actor",
            "--from-file",
            "--output",
            "--limit",
        ),
    )
    action = positionals[0] if positionals else "show"
    return "write" if action in {"append", "extend"} else "read"


def _aggregate_logs(
    work_dir,
    *,
    actor_name: str | None = None,
) -> tuple[list[str], list[dict[str, object]]]:
    actor_ids = list(session_scope._target_actor_ids(work_dir, actor_name))
    records: list[dict[str, object]] = []
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
            payload = dict(record)
            payload.setdefault("actor", current_actor)
            payload.setdefault(
                "label", session_registry._actor_label(current_actor, work_dir=work_dir)
            )
            records.append(payload)
    records = sorted(records, key=lambda item: str(item.get("timestamp") or ""))
    return actor_ids, records


def _is_exact_session_root(work_dir) -> bool:
    resolved = work_dir.resolve()
    return (
        resolved.parent.name != "actors"
        and topology.parse_grouped_session_root(resolved) is None
        and topology.parse_shared_session_root(resolved) is None
        and session_is_initialized(resolved)
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
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
            visible = [
                record
                for record in log_records(work_dir)
                if writer_role(
                    work_dir, scoped_actor, writer=str(record.get("actor") or "")
                )
                != "foreign"
            ]
            entries = sorted(visible, key=lambda item: str(item.get("timestamp") or ""))
            limited = (
                entries[-max(args.limit, 0) :] if max(args.limit, 0) > 0 else entries
            )
            locator = session_charter._native_surface_locator(
                "logs", actor_name=scoped_actor
            )
            payload = {
                "state_path": str(logs_state_path(work_dir)),
                "locator": locator,
                "follow_command": session_charter._native_surface_follow_command(
                    "logs",
                    actor_name=scoped_actor,
                ),
                "entry_count": len(entries),
                "entries": limited,
            }
            if args.output == "json":
                print(json.dumps(payload, indent=2, sort_keys=True))
                return 0
            print(f"logs: {payload['follow_command']}")
            print(f"entries: {payload['entry_count']}")
            for record in payload["entries"]:
                timestamp = str(record.get("timestamp") or "unknown-time")
                actor = str(record.get("actor") or scoped_actor)
                message = (
                    str(record.get("message") or "").strip() or "unspecified log entry"
                )
                message_lines = message.splitlines() or ["unspecified log entry"]
                print(f"- `{timestamp}` [{actor}] {message_lines[0]}")
                for continuation in message_lines[1:]:
                    print(f"  {continuation}")
            return 0
        actor_ids, records = _aggregate_logs(work_dir)
        if not actor_ids:
            if not _is_exact_session_root(work_dir):
                raise SystemExit(
                    "no actors bound for this session; bind one intentionally with "
                    + session_registry._actor_bind_examples(prefix="gotta actor bind")
                )
            entries = sorted(
                log_records(work_dir), key=lambda item: str(item.get("timestamp") or "")
            )
            limited = (
                entries[-max(args.limit, 0) :] if max(args.limit, 0) > 0 else entries
            )
            payload = {
                "session_root": str(work_dir),
                "actor_count": 0,
                "actors": [],
                "entry_count": len(entries),
                "entries": limited,
                "state_path": str(logs_state_path(work_dir)),
                "locator": session_charter._native_surface_locator("logs"),
                "follow_command": session_charter._native_surface_follow_command(
                    "logs"
                ),
            }
            if args.output == "json":
                print(json.dumps(payload, indent=2, sort_keys=True))
                return 0
            print(f"logs: {payload['follow_command']}")
            print(f"entries: {payload['entry_count']}")
            for record in payload["entries"]:
                timestamp = str(record.get("timestamp") or "unknown-time")
                actor = str(record.get("actor") or "session")
                message = (
                    str(record.get("message") or "").strip() or "unspecified log entry"
                )
                message_lines = message.splitlines() or ["unspecified log entry"]
                print(f"- `{timestamp}` [{actor}] {message_lines[0]}")
                for continuation in message_lines[1:]:
                    print(f"  {continuation}")
            return 0
        entries = records[-max(args.limit, 0) :] if max(args.limit, 0) > 0 else records
        payload = {
            "session_root": str(work_dir),
            "actor_count": len(actor_ids),
            "actors": actor_ids,
            "entry_count": len(records),
            "entries": entries,
            "state_paths": {
                actor: str(
                    logs_state_path(
                        session_registry._actor_session_dir(work_dir, actor)
                    )
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
        if args.output == "json":
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"logs: session-wide across {len(actor_ids)} actor(s)")
        print(f"entries: {payload['entry_count']}")
        for record in payload["entries"]:
            timestamp = str(record.get("timestamp") or "unknown-time")
            actor = str(record.get("actor") or "unknown-actor")
            message = (
                str(record.get("message") or "").strip() or "unspecified log entry"
            )
            message_lines = message.splitlines() or ["unspecified log entry"]
            print(f"- `{timestamp}` [{actor}] {message_lines[0]}")
            for continuation in message_lines[1:]:
                print(f"  {continuation}")
        return 0
    work_dir = session_scope._session_dir(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    writer = writer_name()
    actor_branch = work_dir.resolve().parent.name == "actors"
    target_actor = session_actor(work_dir) if actor_branch else ""
    if target_actor:
        require_writer(
            work_dir,
            target_actor,
            writer=writer,
            action="write into this actor branch",
        )
    record_actor = writer or ""
    if action == "extend":
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
