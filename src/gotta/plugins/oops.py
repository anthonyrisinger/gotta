"""Top-level session-rooted friction capture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gotta.actor import require_writer, writer_name, writer_role
from gotta.content import (
    ContentError,
    CommonOptions,
    resolve_dirs,
    session_is_initialized,
    session_relative_path,
    stdin_has_readable_text,
)
from gotta.helptext import format_long_help, is_long_help_request
from gotta import session as session_plugin
from gotta.friction import (
    append_oops_record,
    filtered_oops_records,
    oops_log_path,
    oops_records,
    oops_summary,
    oops_surface_path,
)


def _read_text_source(
    *,
    session_root: Path,
    inline: str | None,
    from_file: str | None,
    use_stdin: bool,
    input_name: str,
) -> str:
    used = int(bool(inline)) + int(bool(from_file)) + int(bool(use_stdin))
    if used > 1:
        raise SystemExit(f"use only one {input_name} source")
    if from_file:
        if from_file == "-":
            return sys.stdin.read()
        return session_relative_path(session_root, from_file).read_text(encoding="utf-8")
    if use_stdin:
        return sys.stdin.read()
    if inline is not None:
        return inline
    if stdin_has_readable_text():
        return sys.stdin.read()
    raise SystemExit(
        f"missing {input_name}; pass inline text, use --stdin, use --from-file, or pipe stdin"
    )


def _read_text_items_source(
    *,
    session_root: Path,
    inline_items: list[str],
    from_file: str | None,
    use_stdin: bool,
    input_name: str,
) -> list[str]:
    used = int(bool(inline_items)) + int(bool(from_file)) + int(bool(use_stdin))
    if used > 1:
        raise SystemExit(f"use only one {input_name} source")
    if from_file:
        if from_file == "-":
            raw = sys.stdin.read()
        else:
            raw = session_relative_path(session_root, from_file).read_text(encoding="utf-8")
    elif use_stdin:
        raw = sys.stdin.read()
    elif inline_items:
        raw = "\n".join(inline_items)
    elif stdin_has_readable_text():
        raw = sys.stdin.read()
    else:
        raise SystemExit(
            f"missing {input_name}; pass one or more inline entries, use --stdin, use --from-file, or pipe stdin"
        )
    items: list[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        items.append(normalized)
    if not items:
        raise SystemExit(f"missing {input_name}")
    return items


def _normalize_text(text: str, *, input_name: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not normalized.strip():
        raise SystemExit(f"missing {input_name}")
    return normalized


def _resolve_session_root(*, explicit_session: str | None) -> Path:
    session_raw = explicit_session
    try:
        dirs = resolve_dirs(
            CommonOptions(
                session_dir=session_raw,
            ),
            create=False,
        )
    except ContentError as exc:
        raise SystemExit(str(exc)) from exc
    session_dir = dirs.session_dir
    if not session_is_initialized(session_dir):
        raise SystemExit(
            "start or bind a session first with `gotta ...` or bootstrap one "
            "manually with `gotta session init --session \"$WS\"`"
        )
    return session_dir


def build_parser(command_name: str = "gotta oops") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Append, extend, list, or summarize durable session speed bumps, "
            "including suspected gotta bugs. Read paths summarize all bound "
            "actor oops by default. "
            "For literal prose or Markdown, prefer stdin, --stdin, or --from-file."
        ),
    )
    parser.add_argument("action", nargs="?", choices=["append", "extend", "list", "summary"])
    parser.add_argument("value", nargs="*")
    session_plugin.add_target_args(parser)
    parser.add_argument(
        "--from-file",
        help="read friction text from a UTF-8 file instead of inline text; use '-' for stdin",
    )
    parser.add_argument(
        "--stdin",
        dest="use_stdin",
        action="store_true",
        help="read friction text from stdin explicitly",
    )
    parser.add_argument("--surface", help="surface where the friction occurred")
    parser.add_argument("--command", help="exact command or subcommand involved")
    parser.add_argument("--kind", help="blocker kind, e.g. auth, output, routing")
    parser.add_argument("--affordance", help="implicated affordance or contract seam")
    parser.add_argument("--workaround", help="workaround used to continue")
    parser.add_argument(
        "--severity",
        choices=["low", "medium", "high", "critical"],
        default="",
    )
    parser.add_argument(
        "--reproducibility",
        choices=["unknown", "intermittent", "reliable"],
        default="unknown",
    )
    parser.add_argument(
        "--resolution-state",
        choices=["open", "accepted", "resolved"],
        default="open",
    )
    parser.add_argument("--output", choices=["json", "text"], default="text")
    parser.add_argument("--limit", type=int, default=20)
    return parser


def session_access_mode(argv: list[str]) -> str:
    positionals = session_plugin.argv_positionals(
        argv,
        valued_flags=(
            "--session",
            "--actor",
            "--from-file",
            "--surface",
            "--command",
            "--kind",
            "--affordance",
            "--workaround",
            "--severity",
            "--reproducibility",
            "--resolution-state",
            "--output",
            "--limit",
        ),
    )
    action = positionals[0] if positionals else "summary"
    return "write" if action in {"append", "extend"} else "read"


def _aggregate_oops_records(
    work_dir: Path,
    *,
    actor_name: str | None = None,
    surface: str = "",
    command: str = "",
    kind: str = "",
    severity: str = "",
) -> tuple[list[str], list[dict[str, object]]]:
    actor_ids = list(session_plugin._target_actor_ids(work_dir, actor_name))
    records: list[dict[str, object]] = []
    for current_actor in actor_ids:
        actor_root = session_plugin._actor_session_dir(work_dir, current_actor)
        for record in filtered_oops_records(
            oops_records(actor_root),
            surface=surface,
            command=command,
            kind=kind,
            severity=severity,
        ):
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
    records = sorted(records, key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return actor_ids, records


def cmd_oops(args: argparse.Namespace) -> int:
    action = args.action or "summary"
    if action == "append":
        session_dir = session_plugin._session_dir(
            explicit_session=getattr(args, "session", None),
            explicit_actor=getattr(args, "actor", None),
        )
        writer = writer_name()
        actor_branch = session_dir.resolve().parent.name == "actors"
        target_actor = session_plugin.session_actor(session_dir) if actor_branch else ""
        if target_actor:
            require_writer(
                session_dir,
                target_actor,
                writer=writer,
                action="write into this actor branch",
            )
        payload = _read_text_source(
            session_root=session_dir,
            inline=(args.value[0] if args.value else None),
            from_file=args.from_file,
            use_stdin=args.use_stdin,
            input_name="oops entry text",
        )
        append_oops_record(
            session_dir,
            message=_normalize_text(payload, input_name="oops entry text"),
            actor=writer,
            surface=args.surface or "",
            command=args.command or "",
            kind=args.kind or "",
            affordance=args.affordance or "",
            workaround=args.workaround or "",
            severity=args.severity or "medium",
            reproducibility=args.reproducibility,
            resolution_state=args.resolution_state,
        )
        print(f"appended oops entry in {oops_surface_path(session_dir)}")
        return 0
    if action == "extend":
        session_dir = session_plugin._session_dir(
            explicit_session=getattr(args, "session", None),
            explicit_actor=getattr(args, "actor", None),
        )
        writer = writer_name()
        actor_branch = session_dir.resolve().parent.name == "actors"
        target_actor = session_plugin.session_actor(session_dir) if actor_branch else ""
        if target_actor:
            require_writer(
                session_dir,
                target_actor,
                writer=writer,
                action="write into this actor branch",
            )
        entries = _read_text_items_source(
            session_root=session_dir,
            inline_items=list(args.value or []),
            from_file=args.from_file,
            use_stdin=args.use_stdin,
            input_name="oops entry text",
        )
        for entry in entries:
            append_oops_record(
                session_dir,
                message=_normalize_text(entry, input_name="oops entry text"),
                actor=writer,
                surface=args.surface or "",
                command=args.command or "",
                kind=args.kind or "",
                affordance=args.affordance or "",
                workaround=args.workaround or "",
                severity=args.severity or "medium",
                reproducibility=args.reproducibility,
                resolution_state=args.resolution_state,
            )
        print(f"extended oops entries in {oops_surface_path(session_dir)}: {len(entries)} item(s)")
        return 0

    explicit_actor = getattr(args, "actor", None)
    if explicit_actor:
        session_dir = session_plugin._session_dir(
            explicit_session=getattr(args, "session", None),
            explicit_actor=explicit_actor,
        )
        actor_name = session_plugin.session_identity(session_dir)
        records = filtered_oops_records(
            oops_records(session_dir),
            surface=args.surface or "",
            command=args.command or "",
            kind=args.kind or "",
            severity=args.severity or "",
        )
        records = [
            record
            for record in records
            if writer_role(session_dir, actor_name, writer=str(record.get("actor") or "")) != "foreign"
        ]
        actor_ids = [session_plugin.session_identity(session_dir)]
    else:
        session_dir = session_plugin._shared_session_dir(
            explicit_session=getattr(args, "session", None),
        )
        actor_ids, records = _aggregate_oops_records(
            session_dir,
            surface=args.surface or "",
            command=args.command or "",
            kind=args.kind or "",
            severity=args.severity or "",
        )
        if not actor_ids:
            raise SystemExit(
                "no actors bound for this session; bind one intentionally with "
                + session_plugin._actor_bind_examples(prefix="gotta actor bind")
            )
    records = sorted(records, key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    if action == "list":
        limited = records[: max(args.limit, 0)]
        payload = {
            "session_root": str(session_dir),
            "actor_count": len(actor_ids),
            "actors": actor_ids,
            "shown_count": len(limited),
            "entries": limited,
            "entry_count": len(records),
        }
        if explicit_actor:
            payload["oops"] = str(oops_surface_path(session_dir))
            payload["oops_log"] = str(oops_log_path(session_dir))
        else:
            payload["oops_surfaces"] = {
                actor: str(oops_surface_path(session_plugin._actor_session_dir(session_dir, actor)))
                for actor in actor_ids
            }
            payload["oops_logs"] = {
                actor: str(oops_log_path(session_plugin._actor_session_dir(session_dir, actor)))
                for actor in actor_ids
            }
        if args.output == "json":
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if explicit_actor:
            print(f"oops: {oops_surface_path(session_dir)}")
        else:
            print(f"oops: session-wide across {len(actor_ids)} actor(s)")
        print(f"entries: {payload['entry_count']} (showing {payload['shown_count']})")
        for record in limited:
            actor = str(record.get("actor") or "unknown-actor")
            print(
                "- "
                f"{record.get('timestamp') or 'unknown-time'} "
                f"[{actor}/{record.get('severity') or 'unknown'}] "
                f"{record.get('kind') or 'general'} "
                f"{record.get('surface') or 'unspecified'} :: "
                f"{record.get('message') or ''}"
            )
        return 0

    payload = {
        "session_root": str(session_dir),
        "actor_count": len(actor_ids),
        "actors": actor_ids,
        **oops_summary(records),
    }
    if explicit_actor:
        payload["oops"] = str(oops_surface_path(session_dir))
        payload["oops_log"] = str(oops_log_path(session_dir))
    else:
        payload["oops_surfaces"] = {
            actor: str(oops_surface_path(session_plugin._actor_session_dir(session_dir, actor)))
            for actor in actor_ids
        }
        payload["oops_logs"] = {
            actor: str(oops_log_path(session_plugin._actor_session_dir(session_dir, actor)))
            for actor in actor_ids
        }
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if explicit_actor:
        print(f"oops: {oops_surface_path(session_dir)}")
    else:
        print(f"oops: session-wide across {len(actor_ids)} actor(s)")
    print(f"entries: {payload['entry_count']}")
    print(f"severity_counts: {payload['severity_counts']}")
    print(f"kind_counts: {payload['kind_counts']}")
    print(f"surface_counts: {payload['surface_counts']}")
    print(f"resolution_counts: {payload['resolution_counts']}")
    print(f"reproducibility_counts: {payload['reproducibility_counts']}")
    print(f"affordance_counts: {payload['affordance_counts']}")
    return 0


def run(argv: list[str], *, command_name: str = "gotta oops") -> int:
    if is_long_help_request(argv):
        sys.stdout.write(format_long_help(build_parser(command_name)))
        return 0
    parser = build_parser(command_name)
    args = parser.parse_args(argv)
    return cmd_oops(args)


def main(argv: list[str] | None = None) -> int:
    return run(list(sys.argv[1:] if argv is None else argv))
