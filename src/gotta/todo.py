"""Canonical structured session checklist state and on-demand rendering."""

from __future__ import annotations

from collections import OrderedDict
import hashlib
from pathlib import Path
from typing import Literal, TypedDict
import uuid

from gotta.compat import UTC, datetime
from gotta.content.env import SESSION_REPO_ENV, load_state_env_at_root
from gotta.actor import SESSION_ACTOR_ENV
from gotta.projection import append_jsonl, read_jsonl_records


TODO_LOG_NAME = "todo.jsonl"
TODO_ID_PREFIX = "T"

DEFAULT_SECTION_ORDER = (
    "Status",
    "File Battery",
    "Drift / Truth Battery",
    "Mechanical / Safety Battery",
    "Actor Checklist",
    "Captured Work",
)


class TodoItem(TypedDict):
    id: str
    section: str
    text: str
    checked: bool
    managed_key: str
    created_at: str


class TodoCreateEvent(TypedDict):
    op: Literal["create"]
    id: str
    section: str
    text: str
    checked: bool
    managed_key: str
    timestamp: str


class TodoCheckEvent(TypedDict):
    op: Literal["check"]
    id: str
    checked: bool
    timestamp: str


TodoEvent = TodoCreateEvent | TodoCheckEvent


class TodoPayload(TypedDict):
    state_path: str
    locator: str
    follow_command: str
    entry_count: int
    open_count: int
    done_count: int
    entries: list[TodoItem]


def todo_state_path(work_dir: Path) -> Path:
    return work_dir / "state" / TODO_LOG_NAME


def is_todo_id(value: str) -> bool:
    return (
        len(value) == 9
        and value.startswith(TODO_ID_PREFIX)
        and all(ch in "0123456789ABCDEF" for ch in value[1:])
    )


def _repo_display_name(work_dir: Path) -> str:
    state = load_state_env_at_root(work_dir)
    repo_path = str(state.get(SESSION_REPO_ENV) or "").strip()
    if not repo_path:
        return "Session"
    return Path(repo_path).name.capitalize() or "Session"


def _session_actor_id(work_dir: Path) -> str:
    state = load_state_env_at_root(work_dir)
    return str(state.get(SESSION_ACTOR_ENV) or "").strip().lower()


def _next_todo_id(used_ids: set[str]) -> str:
    while True:
        candidate = f"{TODO_ID_PREFIX}{uuid.uuid4().hex[:8].upper()}"
        if candidate not in used_ids:
            return candidate


def managed_todo_id(managed_key: str) -> str:
    digest = hashlib.sha256(managed_key.encode("utf-8")).hexdigest()[:8].upper()
    return f"{TODO_ID_PREFIX}{digest}"


def _normalize_todo_event(value: object) -> TodoEvent | None:
    if not isinstance(value, dict):
        return None
    item_id = str(value.get("id") or "").upper()
    if not is_todo_id(item_id):
        return None
    timestamp = str(value.get("timestamp") or "").strip()
    op = str(value.get("op") or "").strip()
    if op == "create":
        return {
            "op": "create",
            "id": item_id,
            "section": str(value.get("section") or "Captured Work").strip()
            or "Captured Work",
            "text": str(value.get("text") or "").strip(),
            "checked": bool(value.get("checked")),
            "managed_key": str(value.get("managed_key") or "").strip(),
            "timestamp": timestamp,
        }
    if op == "check":
        return {
            "op": "check",
            "id": item_id,
            "checked": bool(value.get("checked")),
            "timestamp": timestamp,
        }
    return None


def todo_events(work_dir: Path) -> list[TodoEvent]:
    events: list[TodoEvent] = []
    for record in read_jsonl_records(todo_state_path(work_dir)):
        normalized = _normalize_todo_event(record)
        if normalized is not None:
            events.append(normalized)
    return events


def _todo_item_from_create_event(event: TodoCreateEvent) -> TodoItem:
    return {
        "id": event["id"],
        "section": event["section"],
        "text": event["text"],
        "checked": event["checked"],
        "managed_key": event["managed_key"],
        "created_at": event["timestamp"],
    }


def todo_items(work_dir: Path) -> list[TodoItem]:
    items: OrderedDict[str, TodoItem] = OrderedDict()
    for event in todo_events(work_dir):
        item_id = event["id"]
        if event["op"] == "create":
            if item_id in items:
                continue
            items[item_id] = _todo_item_from_create_event(event)
            continue
        if item_id in items:
            items[item_id]["checked"] = event["checked"]
    return list(items.values())


def todo_payload(
    work_dir: Path,
    *,
    status: str = "all",
    limit: int = 0,
) -> TodoPayload:
    items = todo_items(work_dir)
    if status == "open":
        filtered = [item for item in items if not item["checked"]]
    elif status == "done":
        filtered = [item for item in items if item["checked"]]
    else:
        filtered = list(items)
    if limit > 0:
        filtered = filtered[:limit]
    return {
        "state_path": str(todo_state_path(work_dir)),
        "locator": "todo:session",
        "follow_command": "gotta todo",
        "entry_count": len(items),
        "open_count": sum(1 for item in items if not item["checked"]),
        "done_count": sum(1 for item in items if item["checked"]),
        "entries": filtered,
    }


def render_todo_entry(
    text: str,
    *,
    checked: bool,
    item_id: str,
    managed_key: str = "",
) -> str:
    lines = text.splitlines() or [text]
    marker = "x" if checked else " "
    suffix = f" <!-- gotta:id:{item_id} -->"
    if managed_key:
        suffix += f" <!-- gotta:{managed_key} -->"
    head = f"- [{marker}] {lines[0].strip()}{suffix}"
    if len(lines) == 1:
        return head
    return "\n".join([head, *[f"  {line}" if line else "  " for line in lines[1:]]])


def _render_section_items(items: list[TodoItem]) -> list[str]:
    lines: list[str] = []
    for item in items:
        lines.extend(
            render_todo_entry(
                item["text"],
                checked=item["checked"],
                item_id=item["id"],
                managed_key=item["managed_key"],
            ).splitlines()
        )
    return lines


def render_todo_markdown(work_dir: Path, items: list[TodoItem]) -> str:
    from gotta.session.registry import (
        _actor_bind_examples,
        _actor_label,
        _default_actor_summary,
    )

    grouped: OrderedDict[str, list[TodoItem]] = OrderedDict()
    for section in DEFAULT_SECTION_ORDER:
        grouped[section] = []
    for item in items:
        section = item["section"] or "Captured Work"
        grouped.setdefault(section, [])
        grouped[section].append(item)

    lines = [
        f"# {_repo_display_name(work_dir)} Session TODO",
        "",
        f"> Generated automatically from `state/{TODO_LOG_NAME}`.",
        "> Rendered on demand from canonical state.",
        "",
        "Prefer `gotta todo ...` for routine mutation.",
        "The structured `todo` log is canonical.",
        "",
        "## Definition Of Done",
        "",
        "- Active targets examined against live local truth.",
        "- Truth conflicts corrected, removed, or explicitly parked with rationale.",
        "- Actor findings incorporated or explicitly dispositioned.",
        "- Final verification rerun against the active target set.",
        "- Final sign-off collected from the selected team.",
        "",
        "## Status",
        "",
    ]
    lines.extend(_render_section_items(grouped["Status"]) or ["- none"])
    if grouped["File Battery"]:
        lines.extend(["", "## File Battery", ""])
        lines.extend(_render_section_items(grouped["File Battery"]))
    lines.extend(
        [
            "",
            "## Drift / Truth Battery",
            "",
        ]
    )
    if grouped["Drift / Truth Battery"]:
        lines.extend(_render_section_items(grouped["Drift / Truth Battery"]))
    else:
        lines.extend(
            [
                "- Seed placeholder. Replace with concrete truth-mismatch session state as soon as it is",
                "  discovered.",
            ]
        )
    lines.extend(
        [
            "",
            "## Mechanical / Safety Battery",
            "",
        ]
    )
    if grouped["Mechanical / Safety Battery"]:
        lines.extend(_render_section_items(grouped["Mechanical / Safety Battery"]))
    else:
        lines.extend(
            [
                "- Seed placeholder. Replace with concrete mechanical, anchor, link, and",
                "  dangerous-guidance session state as soon as it is discovered.",
            ]
        )
    lines.extend(
        [
            "",
            "## Coordination Notes",
            "",
        ]
    )
    actor_actor = _session_actor_id(work_dir)
    if actor_actor:
        lines.extend(
            [
                f"- This session is already the {_actor_label(actor_actor)} actor root for this shared concern.",
                "- `WANT.md` and `GOAL.md` here are actor-local authored charters.",
                "- Use `gotta todo`, `gotta notes`, `gotta logs`, and `gotta oops` as the live readable surfaces.",
                "- Append running notes with `gotta notes append ...` from this actor root; add `--actor <actor>` only when targeting another bound actor intentionally.",
                "- Append at least one short note before requesting completion or sign-off so shared actor visibility lands before closure.",
                f"- When materially done, record actor completion through `gotta actor complete {actor_actor}`; use `gotta actor fail {actor_actor}` for actual failure, `gotta actor signoff {actor_actor} --summary ...` for durable acceptance, and `gotta actor stop {actor_actor}` only to terminate a live runtime that needs shutdown.",
            ]
        )
    else:
        lines.extend(
            [
                f"- Choose the team intentionally. Available actors today are {_default_actor_summary()}.",
                f"- `gotta actor bind <actor...>` binds sibling actor sessions inside this shared session and creates the actor-local WANT/GOAL targets; only after bind completes can `gotta want|goal --actor <actor>` address that actor. Use {_actor_bind_examples(prefix='gotta actor bind')}.",
                "- If you choose an actor, actually consult them with `gotta actor launch <actor>`.",
                "- Actor checklist pressure begins only after a launched actor starts real evidence collection.",
            ]
        )
    if grouped["Actor Checklist"]:
        lines.extend(["", "## Actor Checklist", ""])
        lines.extend(_render_section_items(grouped["Actor Checklist"]))
    lines.extend(["", "## Captured Work", ""])
    lines.extend(_render_section_items(grouped["Captured Work"]) or ["- none yet"])

    for section, section_items in grouped.items():
        if section in DEFAULT_SECTION_ORDER or not section_items:
            continue
        lines.extend(["", f"## {section}", ""])
        lines.extend(_render_section_items(section_items))
    return "\n".join(lines) + "\n"


def create_todo_item(
    work_dir: Path,
    *,
    section: str,
    text: str,
    checked: bool = False,
    managed_key: str = "",
    item_id: str = "",
    timestamp: str | None = None,
) -> TodoItem:
    current_items = todo_items(work_dir)
    used_ids = {item["id"] for item in current_items}
    normalized_id = item_id.upper().strip() if item_id else ""
    if not is_todo_id(normalized_id) or normalized_id in used_ids:
        normalized_id = _next_todo_id(used_ids)
    payload: TodoCreateEvent = {
        "op": "create",
        "id": normalized_id,
        "section": section.strip() or "Captured Work",
        "text": text.strip(),
        "checked": checked,
        "managed_key": managed_key.strip(),
        "timestamp": timestamp or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    append_jsonl(todo_state_path(work_dir), dict(payload))
    return _todo_item_from_create_event(payload)


def ensure_managed_todo_item(
    work_dir: Path,
    *,
    section: str,
    text: str,
    managed_key: str,
) -> TodoItem:
    current = todo_items(work_dir)
    for item in current:
        if item["managed_key"] == managed_key:
            return item
    return create_todo_item(
        work_dir,
        section=section,
        text=text,
        managed_key=managed_key,
        item_id=managed_todo_id(managed_key),
    )


def set_todo_checked(work_dir: Path, item_id: str, *, checked: bool) -> TodoItem | None:
    normalized = item_id.strip().upper()
    if not is_todo_id(normalized):
        raise SystemExit(f"invalid TODO item id: {item_id}")
    current = {item["id"]: item for item in todo_items(work_dir)}
    item = current.get(normalized)
    if item is None:
        raise SystemExit(f"no TODO item matched id: {normalized}")
    if item["checked"] == checked:
        return None
    payload: TodoCheckEvent = {
        "op": "check",
        "id": normalized,
        "checked": checked,
        "timestamp": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    append_jsonl(todo_state_path(work_dir), dict(payload))
    return {
        "id": item["id"],
        "section": item["section"],
        "text": item["text"],
        "checked": checked,
        "managed_key": item["managed_key"],
        "created_at": item["created_at"],
    }


def resolve_todo_item(work_dir: Path, *, item_id: str) -> TodoItem:
    normalized = item_id.strip().upper()
    if not is_todo_id(normalized):
        raise SystemExit(
            f"invalid TODO item id: {item_id}. run `gotta todo show` and pass printed ids"
        )
    for item in todo_items(work_dir):
        if item["id"] == normalized:
            return item
    raise SystemExit(f"no TODO item matched id: {normalized}")
