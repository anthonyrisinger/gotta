"""Top-level session-rooted friction capture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gotta.actor import require_writer, session_actor, writer_name, writer_role
from gotta.content import (
    ContentError,
    CommonOptions,
    resolve_dirs,
    session_is_initialized,
    session_relative_path,
    stdin_has_meaningful_text,
)
from gotta.helptext import format_long_help, is_long_help_request
from gotta import topology
from gotta.friction import (
    append_oops_record,
    filtered_oops_records,
    oops_log_path,
    oops_records,
    oops_summary,
)
from gotta.session import charter as session_charter
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope

OOPS_ACTIONS = {"show", "append", "extend"}
OOPS_LEGACY_READ_ACTIONS = {"list", "summary"}
_APPEND_METADATA_FLAGS = (
    "--surface",
    "--command",
    "--kind",
    "--affordance",
    "--workaround",
    "--severity",
    "--reproducibility",
    "--resolution-state",
)


def _has_explicit_write_source(argv: list[str]) -> bool:
    return any(
        token == "--stdin" or token == "--from-file" or token.startswith("--from-file=")
        for token in argv
    )


def _argv_has_append_metadata(argv: list[str]) -> bool:
    for index, token in enumerate(argv):
        if any(token.startswith(f"{flag}=") for flag in _APPEND_METADATA_FLAGS):
            return True
        if token in _APPEND_METADATA_FLAGS and index + 1 < len(argv):
            return True
    return False


def _has_append_metadata(args: argparse.Namespace) -> bool:
    return any(
        (
            str(args.surface or "").strip(),
            str(args.command or "").strip(),
            str(args.kind or "").strip(),
            str(args.affordance or "").strip(),
            str(args.workaround or "").strip(),
            str(args.severity or "").strip(),
            str(args.reproducibility or "").strip() != "unknown",
            str(args.resolution_state or "").strip() != "open",
        )
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
        return session_relative_path(session_root, from_file).read_text(
            encoding="utf-8"
        )
    if use_stdin:
        return sys.stdin.read()
    if inline is not None:
        return inline
    if stdin_has_meaningful_text():
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
            raw = session_relative_path(session_root, from_file).read_text(
                encoding="utf-8"
            )
    elif use_stdin:
        raw = sys.stdin.read()
    elif inline_items:
        raw = "\n".join(inline_items)
    elif stdin_has_meaningful_text():
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
            "start or bind a session first with `gotta ...`. Stable interactive "
            "contexts adopt and scaffold their deterministic session on first "
            'session-aware use. Use `gotta session init --session "$WS"` only '
            "when you intentionally want to scaffold one exact root."
        )
    return session_dir


def build_parser(command_name: str = "gotta oops") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Show or record durable session speed bumps, including suspected gotta bugs. "
            "Bare `gotta oops` shows the friction ledger across the active session scope. "
            "Bare multiword prose, real piped stdin, `--stdin`, and `--from-file` imply "
            "`append` when no read action is named. "
            "Use `gotta oops show` for explicit reads and `gotta oops append ...` when you "
            "want write intent to be unambiguous."
        ),
    )
    parser.add_argument("action", nargs="?")
    parser.add_argument("value", nargs="*")
    session_charter.add_target_args(parser)
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
    positionals = session_charter.argv_positionals(
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
    action = positionals[0] if positionals else ""
    if action in {"append", "extend"}:
        return "write"
    if action in {"show", "list", "summary"}:
        return "read"
    if not action:
        if _has_explicit_write_source(argv) or stdin_has_meaningful_text():
            return "write"
        return "read"
    if (
        _has_explicit_write_source(argv)
        or stdin_has_meaningful_text()
        or len(positionals) > 1
        or (_argv_has_append_metadata(argv) and bool(positionals))
    ):
        return "write"
    return "read"


def _aggregate_oops_records(
    work_dir: Path,
    *,
    actor_name: str | None = None,
    surface: str = "",
    command: str = "",
    kind: str = "",
    severity: str = "",
) -> tuple[list[str], list[dict[str, object]]]:
    actor_ids = list(session_scope._target_actor_ids(work_dir, actor_name))
    records: list[dict[str, object]] = []
    for current_actor in actor_ids:
        actor_root = session_registry._actor_session_dir(work_dir, current_actor)
        for record in filtered_oops_records(
            oops_records(actor_root),
            surface=surface,
            command=command,
            kind=kind,
            severity=severity,
        ):
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
    records = sorted(
        records, key=lambda item: str(item.get("timestamp") or ""), reverse=True
    )
    return actor_ids, records


def _explicit_read_redirect(action_token: str) -> SystemExit:
    return SystemExit(
        f"`gotta oops {action_token}` has been folded into `gotta oops show`; "
        "use `gotta oops` or `gotta oops show`"
    )


def _read_write_conflict() -> SystemExit:
    return SystemExit(
        "`gotta oops show` is read-only; remove stdin/file input and inline prose, "
        "or use `gotta oops append ...` to write"
    )


def _unknown_action(action_token: str) -> SystemExit:
    return SystemExit(
        f"unknown gotta oops action `{action_token}`; use `show`, `append`, or `extend`, "
        "or pass multiword prose / real stdin to imply append"
    )


def _limited_records(
    records: list[dict[str, object]], *, limit: int
) -> list[dict[str, object]]:
    bounded = max(limit, 0)
    if bounded == 0:
        return list(records)
    return records[:bounded]


def _read_payload(
    session_dir: Path,
    actor_ids: list[str],
    records: list[dict[str, object]],
    *,
    explicit_actor: str | None,
    scoped_actor: str | None,
    limit: int,
) -> dict[str, object]:
    limited = _limited_records(records, limit=limit)
    payload: dict[str, object] = {
        "session_root": str(session_dir),
        "actor_count": len(actor_ids),
        "actors": actor_ids,
        "shown_count": len(limited),
        "entries": limited,
        **oops_summary(records),
    }
    if explicit_actor or scoped_actor:
        payload["state_path"] = str(oops_log_path(session_dir))
        payload["locator"] = session_charter._native_surface_locator(
            "oops",
            actor_name=scoped_actor or session_actor(session_dir),
        )
        payload["follow_command"] = session_charter._native_surface_follow_command(
            "oops",
            actor_name=scoped_actor or session_actor(session_dir),
        )
    else:
        payload["state_paths"] = {
            actor: str(
                oops_log_path(session_registry._actor_session_dir(session_dir, actor))
            )
            for actor in actor_ids
        }
        payload["locators"] = {
            actor: session_charter._native_surface_locator("oops", actor_name=actor)
            for actor in actor_ids
        }
        payload["follow_commands"] = {
            actor: session_charter._native_surface_follow_command(
                "oops", actor_name=actor
            )
            for actor in actor_ids
        }
    return payload


def _is_exact_session_root(work_dir: Path) -> bool:
    resolved = work_dir.resolve()
    return (
        resolved.parent.name != "actors"
        and topology.parse_grouped_session_root(resolved) is None
        and topology.parse_shared_session_root(resolved) is None
        and session_is_initialized(resolved)
    )


def _resolved_action(args: argparse.Namespace) -> tuple[str, list[str]]:
    values = list(args.value or [])
    action_token = str(args.action or "").strip()
    explicit_write_source = bool(args.from_file or args.use_stdin)
    piped_text = stdin_has_meaningful_text()
    has_inline_prose = bool(values)
    has_append_metadata = _has_append_metadata(args) and bool(action_token or values)
    if action_token in OOPS_LEGACY_READ_ACTIONS:
        raise _explicit_read_redirect(action_token)
    if action_token in {"show"}:
        if explicit_write_source or piped_text or values:
            raise _read_write_conflict()
        return action_token, values
    if action_token in OOPS_ACTIONS:
        return action_token, values
    if action_token and (
        explicit_write_source or has_append_metadata or has_inline_prose
    ):
        values = [action_token, *values]
        return "append", values
    if not action_token and (explicit_write_source or piped_text):
        return "append", values
    if not action_token:
        return "show", values
    raise _unknown_action(action_token)


def cmd_oops(args: argparse.Namespace) -> int:
    action, values = _resolved_action(args)
    if action == "append":
        session_dir = session_scope._session_dir(
            explicit_session=getattr(args, "session", None),
            explicit_actor=getattr(args, "actor", None),
        )
        writer = writer_name()
        actor_branch = session_dir.resolve().parent.name == "actors"
        target_actor = session_actor(session_dir) if actor_branch else ""
        if target_actor:
            require_writer(
                session_dir,
                target_actor,
                writer=writer,
                action="write into this actor branch",
            )
        payload = _read_text_source(
            session_root=session_dir,
            inline=(" ".join(values) if values else None),
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
        print("appended oops entry")
        return 0
    if action == "extend":
        session_dir = session_scope._session_dir(
            explicit_session=getattr(args, "session", None),
            explicit_actor=getattr(args, "actor", None),
        )
        writer = writer_name()
        actor_branch = session_dir.resolve().parent.name == "actors"
        target_actor = session_actor(session_dir) if actor_branch else ""
        if target_actor:
            require_writer(
                session_dir,
                target_actor,
                writer=writer,
                action="write into this actor branch",
            )
        entries = _read_text_items_source(
            session_root=session_dir,
            inline_items=values,
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
        print(f"extended oops entries: {len(entries)} item(s)")
        return 0

    session_dir, scoped_actor = session_scope._observation_scope(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    explicit_actor = getattr(args, "actor", None)
    if scoped_actor:
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
            if writer_role(
                session_dir, scoped_actor, writer=str(record.get("actor") or "")
            )
            != "foreign"
        ]
        actor_ids = [scoped_actor]
    else:
        actor_ids, records = _aggregate_oops_records(
            session_dir,
            surface=args.surface or "",
            command=args.command or "",
            kind=args.kind or "",
            severity=args.severity or "",
        )
        if not actor_ids:
            if not _is_exact_session_root(session_dir):
                raise SystemExit(
                    "no actors bound for this session; bind one intentionally with "
                    + session_registry._actor_bind_examples(prefix="gotta actor bind")
                )
            records = filtered_oops_records(
                oops_records(session_dir),
                surface=args.surface or "",
                command=args.command or "",
                kind=args.kind or "",
                severity=args.severity or "",
            )
            payload = {
                "session_root": str(session_dir),
                "actor_count": 0,
                "actors": [],
                "shown_count": len(_limited_records(records, limit=args.limit)),
                "entries": _limited_records(records, limit=args.limit),
                "state_path": str(oops_log_path(session_dir)),
                "locator": session_charter._native_surface_locator("oops"),
                "follow_command": session_charter._native_surface_follow_command(
                    "oops"
                ),
                **oops_summary(records),
            }
            if args.output == "json":
                print(json.dumps(payload, indent=2, sort_keys=True))
                return 0
            print(f"oops: {payload['follow_command']}")
            print(
                f"entries: {payload['entry_count']} (showing {payload['shown_count']})"
            )
            print(f"severity_counts: {payload['severity_counts']}")
            print(f"kind_counts: {payload['kind_counts']}")
            print(f"surface_counts: {payload['surface_counts']}")
            print(f"resolution_counts: {payload['resolution_counts']}")
            print(f"reproducibility_counts: {payload['reproducibility_counts']}")
            print(f"affordance_counts: {payload['affordance_counts']}")
            for record in payload["entries"]:
                timestamp = str(record.get("timestamp") or "unknown-time")
                actor = str(record.get("actor") or "session")
                message = (
                    str(record.get("message") or "").strip() or "unspecified oops entry"
                )
                print(f"- `{timestamp}` [{actor}] {message}")
            return 0
    payload = {
        **_read_payload(
            session_dir,
            actor_ids,
            records,
            explicit_actor=explicit_actor,
            scoped_actor=scoped_actor,
            limit=args.limit,
        )
    }
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if explicit_actor or scoped_actor:
        print(f"oops: {payload['follow_command']}")
    else:
        print(f"oops: session-wide across {len(actor_ids)} actor(s)")
    print(f"entries: {payload['entry_count']} (showing {payload['shown_count']})")
    print(f"severity_counts: {payload['severity_counts']}")
    print(f"kind_counts: {payload['kind_counts']}")
    print(f"surface_counts: {payload['surface_counts']}")
    print(f"resolution_counts: {payload['resolution_counts']}")
    print(f"reproducibility_counts: {payload['reproducibility_counts']}")
    print(f"affordance_counts: {payload['affordance_counts']}")
    for record in payload["entries"]:
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


def run(argv: list[str], *, command_name: str = "gotta oops") -> int:
    if is_long_help_request(argv):
        sys.stdout.write(format_long_help(build_parser(command_name)))
        return 0
    parser = build_parser(command_name)
    args = parser.parse_args(argv)
    return cmd_oops(args)


def main(argv: list[str] | None = None) -> int:
    return run(list(sys.argv[1:] if argv is None else argv))
