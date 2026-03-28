"""Top-level session-rooted checklist surface."""

from __future__ import annotations

import argparse
import json
import re

from gotta.actor import session_actor
from gotta.helptext import format_long_help, is_long_help_request
from gotta.todo import (
    create_todo_item,
    is_todo_id,
    resolve_todo_item,
    set_todo_checked,
    todo_payload,
    todo_state_path,
)
from gotta.session import activity as session_activity
from gotta.session import charter as session_charter
from gotta.session import scope as session_scope
from gotta.session import status as session_status


TODO_LINE_RE = re.compile(r"^(?P<indent>\s*)- \[(?P<checked>[ xX])\] (?P<text>.+?)\s*$")
TODO_HEADING_INPUT_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
TODO_TASK_BULLET_INPUT_RE = re.compile(
    r"^(?P<indent>\s*)[-*+]\s+\[(?P<checked>[ xX])\]\s+(?P<body>.+?)\s*$"
)
TODO_BULLET_INPUT_RE = re.compile(r"^(?P<indent>\s*)[-*+]\s+(?P<body>.+?)\s*$")


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    session_charter.add_target_args(parser)


def build_parser(command_name: str = "gotta todo") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description=(
            "Inspect or mutate the canonical session TODO checklist. "
            "Actor-managed checklist items advance through `gotta actor ...`; "
            "for prose or Markdown, prefer stdin, --stdin, or --from-file."
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["show", "append", "extend", "check"],
        help="show entries, append one item, extend with many items, or check one or more items",
    )
    parser.add_argument(
        "value",
        nargs="*",
        help="inline TODO text for append/extend, or printed item ids for check",
    )
    _add_root_args(parser)
    parser.add_argument(
        "--from-file",
        help="read TODO text from a UTF-8 file instead of inline text; use '-' for stdin",
    )
    parser.add_argument(
        "--stdin",
        dest="use_stdin",
        action="store_true",
        help="read TODO text from stdin explicitly",
    )
    parser.add_argument("--output", choices=["json", "text"], default="text")
    parser.add_argument("--status", choices=["all", "open", "done"], default="all")
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
            "--status",
            "--limit",
        ),
    )
    action = positionals[0] if positionals else "show"
    return "write" if action in {"append", "extend", "check"} else "read"


def _format_markdown_bullet(body: str, *, prefix: str = "- ") -> str:
    lines = session_charter._normalize_entry_text(body, input_name="entry text").split(
        "\n"
    )
    first = f"{prefix}{lines[0].strip()}"
    if len(lines) == 1:
        return first
    return "\n".join([first, *[f"  {line}" if line else "  " for line in lines[1:]]])


def _todo_markdown_block_entries(
    text: str, *, input_name: str
) -> tuple[list[str], int]:
    lines: list[str] = []
    item_count = 0
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if TODO_HEADING_INPUT_RE.match(line):
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(stripped)
            continue
        task_match = TODO_TASK_BULLET_INPUT_RE.match(line)
        if task_match:
            marker = "x" if task_match.group("checked").lower() == "x" else " "
            lines.append(
                f"{task_match.group('indent')}- [{marker}] {task_match.group('body').strip()}"
            )
            item_count += 1
            continue
        bullet_match = TODO_BULLET_INPUT_RE.match(line)
        if bullet_match:
            lines.append(
                f"{bullet_match.group('indent')}- [ ] {bullet_match.group('body').strip()}"
            )
            item_count += 1
            continue
        lines.append(line)
    entry = session_charter._normalize_entry_text(
        "\n".join(lines), input_name=input_name
    )
    if entry.splitlines() and TODO_HEADING_INPUT_RE.match(entry.splitlines()[0]):
        entry = "\n" + entry
    return [entry], max(item_count, 1)


def _todo_extend_entries_source(
    *,
    session_root,
    inline_items: list[str],
    from_file: str | None,
    use_stdin: bool,
    input_name: str,
) -> tuple[list[str], int]:
    if inline_items:
        entries = [
            _format_markdown_bullet(
                session_charter._normalize_entry_text(item, input_name=input_name),
                prefix="- [ ] ",
            )
            for item in inline_items
        ]
        return entries, len(entries)

    payload = session_charter._read_text_source(
        session_root=session_root,
        inline=None,
        from_file=from_file,
        use_stdin=use_stdin,
        input_name=input_name,
    )
    normalized = session_charter._normalize_entry_text(payload, input_name=input_name)
    if any(TODO_HEADING_INPUT_RE.match(line) for line in normalized.splitlines()):
        return _todo_markdown_block_entries(normalized, input_name=input_name)

    items: list[str] = []
    for line in normalized.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        task_match = TODO_TASK_BULLET_INPUT_RE.match(candidate)
        if task_match:
            items.append(task_match.group("body").strip())
            continue
        bullet_match = TODO_BULLET_INPUT_RE.match(candidate)
        if bullet_match:
            items.append(bullet_match.group("body").strip())
            continue
        items.append(candidate)
    entries = [
        _format_markdown_bullet(
            session_charter._normalize_entry_text(item, input_name=input_name),
            prefix="- [ ] ",
        )
        for item in items
    ]
    return entries, len(entries)


def _todo_items_from_markdown_entry(
    entry: str,
    *,
    default_section: str,
) -> list[dict[str, object]]:
    current_section = default_section
    items: list[dict[str, object]] = []
    pending: dict[str, object] | None = None
    for raw_line in entry.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            if pending is not None:
                pending["text"] = str(pending["text"]).rstrip()
            continue
        heading_match = TODO_HEADING_INPUT_RE.match(stripped)
        if heading_match:
            current_section = re.sub(r"^\s{0,3}#{1,6}\s+", "", stripped).strip()
            continue
        todo_match = TODO_LINE_RE.match(line)
        if todo_match:
            if pending is not None:
                items.append(pending)
            pending = {
                "section": current_section,
                "text": todo_match.group("text").strip(),
                "checked": todo_match.group("checked").lower() == "x",
            }
            continue
        if pending is not None:
            pending["text"] = f"{pending['text']}\n{stripped}"
            continue
        pending = {
            "section": current_section,
            "text": stripped,
            "checked": False,
        }
    if pending is not None:
        items.append(pending)
    return items


def _check_todo_item(work_dir, *, item_id: str) -> dict[str, object]:
    current = resolve_todo_item(work_dir, item_id=item_id)
    managed_key = str(current.get("managed_key") or "")
    if managed_key and not bool(current.get("checked")):
        raise SystemExit(session_status._managed_todo_redirect(managed_key))
    updated = set_todo_checked(work_dir, item_id, checked=True)
    if updated is None:
        updated = current
    if not isinstance(updated, dict):
        raise SystemExit(f"invalid TODO item id: {item_id}")
    return updated


def _check_todo_items(work_dir, *, item_ids: list[str]) -> list[dict[str, object]]:
    if not item_ids:
        raise SystemExit(
            "missing TODO item ids; pass one or more printed ids or pipe ids through stdin"
        )
    updated: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in item_ids:
        normalized = value.strip().upper()
        if normalized in seen:
            continue
        seen.add(normalized)
        if not is_todo_id(normalized):
            raise SystemExit(
                f"invalid TODO item id: {value}. run `gotta todo show` and pass printed ids"
            )
        updated.append(_check_todo_item(work_dir, item_id=value))
    return updated


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
    work_dir = session_scope._session_dir(
        explicit_session=getattr(args, "session", None),
        explicit_actor=getattr(args, "actor", None),
    )
    session_status._sync_actor_todo_state(work_dir)
    action = args.action or "show"
    if action == "show":
        payload = todo_payload(work_dir, status=args.status, limit=max(args.limit, 0))
        actor_scope = (
            session_actor(work_dir)
            if work_dir.resolve().parent.name == "actors"
            else ""
        )
        payload["locator"] = session_charter._native_surface_locator(
            "todo", actor_name=actor_scope
        )
        payload["follow_command"] = session_charter._native_surface_follow_command(
            "todo",
            actor_name=actor_scope,
        )
        if args.output == "json":
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"todo: {payload['follow_command']}")
        print(
            f"entries: {payload['entry_count']} "
            f"(open: {payload['open_count']}, done: {payload['done_count']})"
        )
        for item in payload["entries"]:
            mark = "x" if bool(item["checked"]) else " "
            print(f"- {item['id']} [{mark}] {item['section']} :: {item['text']}")
        return 0
    if action == "append":
        payload = session_charter._read_text_source(
            session_root=work_dir,
            inline=(args.value[0] if args.value else None),
            from_file=args.from_file,
            use_stdin=args.use_stdin,
            input_name="TODO item text",
        )
        create_todo_item(
            work_dir,
            section="Captured Work",
            text=session_charter._normalize_entry_text(
                payload, input_name="TODO item text"
            ),
        )
        session_activity._record_session_activity(
            work_dir,
            plugin="todo",
            surface="todo",
            action="append",
            target=todo_state_path(work_dir),
            detail="appended 1 TODO item",
        )
        print("appended TODO item")
        return 0
    if action == "extend":
        entries, item_count = _todo_extend_entries_source(
            session_root=work_dir,
            inline_items=list(args.value or []),
            from_file=args.from_file,
            use_stdin=args.use_stdin,
            input_name="TODO item text",
        )
        for entry in entries:
            for item in _todo_items_from_markdown_entry(
                entry, default_section="Captured Work"
            ):
                create_todo_item(
                    work_dir,
                    section=str(item["section"]),
                    text=str(item["text"]),
                    checked=bool(item["checked"]),
                )
        session_activity._record_session_activity(
            work_dir,
            plugin="todo",
            surface="todo",
            action="extend",
            target=todo_state_path(work_dir),
            detail=f"extended TODO with {item_count} item(s)",
        )
        print(f"extended TODO items: {item_count} item(s)")
        return 0
    if action == "check":
        updated_items = _check_todo_items(
            work_dir,
            item_ids=session_charter._read_text_items_source(
                session_root=work_dir,
                inline_items=list(args.value or []),
                from_file=args.from_file,
                use_stdin=args.use_stdin,
                input_name="TODO item ids",
            ),
        )
        if len(updated_items) == 1:
            updated = updated_items[0]
            session_activity._record_session_activity(
                work_dir,
                plugin="todo",
                surface="todo",
                action="check",
                target=todo_state_path(work_dir),
                detail=f"checked TODO item {updated['id']}",
            )
            print(
                f"checked TODO item: {updated['id']} {updated['section']} :: {updated['text']}"
            )
            return 0
        session_activity._record_session_activity(
            work_dir,
            plugin="todo",
            surface="todo",
            action="check",
            target=todo_state_path(work_dir),
            detail=f"checked {len(updated_items)} TODO items",
        )
        print(f"checked TODO items: {len(updated_items)} item(s)")
        for updated in updated_items:
            print(f"- {updated['id']} {updated['section']} :: {updated['text']}")
        return 0
    raise SystemExit(f"unsupported TODO action: {action}")
