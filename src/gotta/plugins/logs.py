"""Top-level session-rooted durable execution log surface."""

from __future__ import annotations

import argparse
import json

from gotta.actor import require_writer, writer_name, writer_role
from gotta.helptext import format_long_help, is_long_help_request
from gotta.logs import append_log_record, log_records, logs_state_path, logs_surface_path
from gotta import session as session_plugin


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    session_plugin.add_target_args(parser)


def build_parser(command_name: str = "gotta logs") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Inspect or mutate the canonical session execution log. "
            "Shared-session read paths show all bound actor logs by default. "
            "For prose or Markdown, prefer stdin, --stdin, or --from-file."
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
    positionals = session_plugin.argv_positionals(
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
    actor_ids = list(session_plugin._target_actor_ids(work_dir, actor_name))
    records: list[dict[str, object]] = []
    for current_actor in actor_ids:
        actor_root = session_plugin._actor_session_dir(work_dir, current_actor)
        for record in log_records(actor_root):
            if (
                actor_root.resolve().parent.name == "actors"
                and writer_role(actor_root, current_actor, writer=str(record.get("actor") or ""))
                == "foreign"
            ):
                continue
            payload = dict(record)
            payload.setdefault("actor", current_actor)
            payload.setdefault("label", session_plugin._actor_label(current_actor, work_dir=work_dir))
            records.append(payload)
    records = sorted(records, key=lambda item: str(item.get("timestamp") or ""))
    return actor_ids, records


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
        work_dir, scoped_actor = session_plugin._observation_scope(
            explicit_session=getattr(args, "session", None),
            explicit_actor=getattr(args, "actor", None),
        )
        if scoped_actor:
            visible = [
                record
                for record in log_records(work_dir)
                if writer_role(work_dir, scoped_actor, writer=str(record.get("actor") or ""))
                != "foreign"
            ]
            entries = sorted(visible, key=lambda item: str(item.get("timestamp") or ""))
            limited = entries[-max(args.limit, 0) :] if max(args.limit, 0) > 0 else entries
            payload = {
                "logs": str(logs_surface_path(work_dir)),
                "logs_log": str(logs_state_path(work_dir)),
                "entry_count": len(entries),
                "entries": limited,
            }
            if args.output == "json":
                print(json.dumps(payload, indent=2, sort_keys=True))
                return 0
            print(f"logs: {payload['logs']}")
            print(f"entries: {payload['entry_count']}")
            for record in payload["entries"]:
                timestamp = str(record.get("timestamp") or "unknown-time")
                actor = str(record.get("actor") or scoped_actor)
                message = str(record.get("message") or "").strip() or "unspecified log entry"
                message_lines = message.splitlines() or ["unspecified log entry"]
                print(f"- `{timestamp}` [{actor}] {message_lines[0]}")
                for continuation in message_lines[1:]:
                    print(f"  {continuation}")
            return 0
        actor_ids, records = _aggregate_logs(work_dir)
        if not actor_ids:
            raise SystemExit(
                "no actors bound for this session; bind one intentionally with "
                + session_plugin._actor_bind_examples(prefix="gotta actor bind")
            )
        entries = records[-max(args.limit, 0) :] if max(args.limit, 0) > 0 else records
        payload = {
            "session_root": str(work_dir),
            "actor_count": len(actor_ids),
            "actors": actor_ids,
            "entry_count": len(records),
            "entries": entries,
            "logs_surfaces": {
                actor: str(logs_surface_path(session_plugin._actor_session_dir(work_dir, actor)))
                for actor in actor_ids
            },
            "logs_logs": {
                actor: str(logs_state_path(session_plugin._actor_session_dir(work_dir, actor)))
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
            message = str(record.get("message") or "").strip() or "unspecified log entry"
            message_lines = message.splitlines() or ["unspecified log entry"]
            print(f"- `{timestamp}` [{actor}] {message_lines[0]}")
            for continuation in message_lines[1:]:
                print(f"  {continuation}")
        return 0
    work_dir = session_plugin._session_dir(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    writer = writer_name()
    actor_branch = work_dir.resolve().parent.name == "actors"
    target_actor = session_plugin.session_actor(work_dir) if actor_branch else ""
    if target_actor:
        require_writer(
            work_dir,
            target_actor,
            writer=writer,
            action="write into this actor branch",
        )
    log_path = logs_surface_path(work_dir)
    record_actor = writer or ""
    if action == "extend":
        entries = session_plugin._read_text_items_source(
            session_root=work_dir,
            inline_items=list(args.value or []),
            from_file=args.from_file,
            use_stdin=args.use_stdin,
            input_name="log entry text",
        )
        for entry in entries:
            append_log_record(
                work_dir,
                message=session_plugin._normalize_entry_text(entry, input_name="log entry text"),
                actor=record_actor,
            )
        session_plugin._record_session_activity(
            work_dir,
            plugin="logs",
            surface="logs",
            action="extend",
            actor=record_actor,
            target=log_path,
            detail=f"extended logs with {len(entries)} item(s)",
        )
        print(f"extended logs entries in {log_path}: {len(entries)} item(s)")
        return 0
    payload = session_plugin._read_text_source(
        session_root=work_dir,
        inline=(args.value[0] if args.value else None),
        from_file=args.from_file,
        use_stdin=args.use_stdin,
        input_name="log entry text",
    )
    append_log_record(
        work_dir,
        message=session_plugin._normalize_entry_text(payload, input_name="log entry text"),
        actor=record_actor,
    )
    session_plugin._record_session_activity(
        work_dir,
        plugin="logs",
        surface="logs",
        action="append",
        actor=record_actor,
        target=log_path,
        detail="appended 1 logs entry",
    )
    print(f"appended logs entry in {log_path}")
    return 0
