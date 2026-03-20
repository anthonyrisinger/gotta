"""Top-level session-rooted durable execution log surface."""

from __future__ import annotations

import argparse
import json

from gotta.helptext import format_long_help, is_long_help_request
from gotta.logs import append_log_record, logs_payload, logs_surface_path
from gotta import session as session_plugin


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    session_plugin.add_target_args(parser)


def build_parser(command_name: str = "gotta logs") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Inspect or mutate the canonical session execution log. "
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
    work_dir = session_plugin._session_dir(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    action = args.action or "show"
    log_path = logs_surface_path(work_dir)
    if action == "show":
        payload = logs_payload(work_dir, limit=max(args.limit, 0))
        if args.output == "json":
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"logs: {payload['logs']}")
        print(f"entries: {payload['entry_count']}")
        for record in payload["entries"]:
            timestamp = str(record.get("timestamp") or "unknown-time")
            actor = str(record.get("actor") or session_plugin.session_identity(work_dir))
            message = str(record.get("message") or "").strip() or "unspecified log entry"
            message_lines = message.splitlines() or ["unspecified log entry"]
            print(f"- `{timestamp}` [{actor}] {message_lines[0]}")
            for continuation in message_lines[1:]:
                print(f"  {continuation}")
        return 0
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
            )
        session_plugin._record_session_activity(
            work_dir,
            plugin="logs",
            surface="logs",
            action="extend",
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
    )
    session_plugin._record_session_activity(
        work_dir,
        plugin="logs",
        surface="logs",
        action="append",
        target=log_path,
        detail="appended 1 logs entry",
    )
    print(f"appended logs entry in {log_path}")
    return 0
