"""Canonical structured session checklist state and Markdown projection."""

from __future__ import annotations

from collections import OrderedDict
import hashlib
from pathlib import Path
import uuid

from gotta.compat import UTC, datetime
from gotta.content import SESSION_REPO_ENV, load_state_env_at_root, write_text_atomic
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


def todo_state_path(work_dir: Path) -> Path:
    return work_dir / "state" / TODO_LOG_NAME


def todo_surface_path(work_dir: Path) -> Path:
    return work_dir / "TODO.md"


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


def todo_events(work_dir: Path) -> list[dict[str, object]]:
    return read_jsonl_records(todo_state_path(work_dir))


def todo_items(work_dir: Path) -> list[dict[str, object]]:
    items: OrderedDict[str, dict[str, object]] = OrderedDict()
    for event in todo_events(work_dir):
        op = str(event.get("op") or "")
        item_id = str(event.get("id") or "").upper()
        if not is_todo_id(item_id):
            continue
        if op == "create":
            if item_id in items:
                continue
            items[item_id] = {
                "id": item_id,
                "section": str(event.get("section") or "Captured Work").strip() or "Captured Work",
                "text": str(event.get("text") or "").strip(),
                "checked": bool(event.get("checked")),
                "managed_key": str(event.get("managed_key") or "").strip(),
                "created_at": str(event.get("timestamp") or ""),
            }
            continue
        if op == "check" and item_id in items:
            items[item_id]["checked"] = bool(event.get("checked"))
    return list(items.values())


def todo_payload(
    work_dir: Path,
    *,
    status: str = "all",
    limit: int = 0,
) -> dict[str, object]:
    items = todo_items(work_dir)
    if status == "open":
        filtered = [item for item in items if not bool(item["checked"])]
    elif status == "done":
        filtered = [item for item in items if bool(item["checked"])]
    else:
        filtered = list(items)
    if limit > 0:
        filtered = filtered[:limit]
    return {
        "todo": str(todo_surface_path(work_dir)),
        "todo_log": str(todo_state_path(work_dir)),
        "entry_count": len(items),
        "open_count": sum(1 for item in items if not bool(item["checked"])),
        "done_count": sum(1 for item in items if bool(item["checked"])),
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


def _render_section_items(items: list[dict[str, object]]) -> list[str]:
    lines: list[str] = []
    for item in items:
        lines.extend(
            render_todo_entry(
                str(item["text"]),
                checked=bool(item["checked"]),
                item_id=str(item["id"]),
                managed_key=str(item.get("managed_key") or ""),
            ).splitlines()
        )
    return lines


def render_todo_markdown(work_dir: Path, items: list[dict[str, object]]) -> str:
    from gotta.session import (
        _default_actor_summary,
        _actor_label,
        _actor_bind_examples,
    )

    grouped: OrderedDict[str, list[dict[str, object]]] = OrderedDict()
    for section in DEFAULT_SECTION_ORDER:
        grouped[section] = []
    for item in items:
        section = str(item.get("section") or "Captured Work")
        grouped.setdefault(section, [])
        grouped[section].append(item)

    lines = [
        f"# {_repo_display_name(work_dir)} Session TODO",
        "",
        f"> Generated automatically from `state/{TODO_LOG_NAME}`.",
        "> This is a human-readable projection; the structured `todo` log is canonical.",
        "",
        "Prefer `gotta todo ...` for routine mutation.",
        "",
        "This file is the definition of done for this session as projected from canonical state.",
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
                "- `WANT.md`, `GOAL.md`, and `TODO.md` here are actor-local session surfaces.",
                "- `LOGS.md` and `OOPS.md` here are actor-local continuous surfaces.",
                f"- Append running notes with `gotta notes append --actor {actor_actor} ...`.",
                "- Append at least one durable note before requesting completion or sign-off so shared actor visibility lands before closure.",
                f"- When materially done, record actor completion through `gotta actor complete {actor_actor}`; use `gotta actor stop {actor_actor}` for a graceful operator-directed wind-down, `gotta actor fail {actor_actor}` for actual failure, and finish durable review with `gotta actor signoff {actor_actor} --summary ...`.",
            ]
        )
    else:
        lines.extend(
            [
                f"- Choose the team intentionally. Available actors today are {_default_actor_summary()}.",
                f"- `gotta actor bind <actor...>` binds sibling actor sessions inside this shared session; use {_actor_bind_examples(prefix='gotta actor bind')}.",
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


def sync_todo_projection(work_dir: Path) -> None:
    write_text_atomic(
        todo_surface_path(work_dir),
        render_todo_markdown(work_dir, todo_items(work_dir)),
    )


def create_todo_item(
    work_dir: Path,
    *,
    section: str,
    text: str,
    checked: bool = False,
    managed_key: str = "",
    item_id: str = "",
    timestamp: str | None = None,
) -> dict[str, object]:
    current_items = todo_items(work_dir)
    used_ids = {str(item["id"]).upper() for item in current_items}
    normalized_id = item_id.upper().strip() if item_id else ""
    if not is_todo_id(normalized_id) or normalized_id in used_ids:
        normalized_id = _next_todo_id(used_ids)
    payload: dict[str, object] = {
        "op": "create",
        "id": normalized_id,
        "section": section.strip() or "Captured Work",
        "text": text.strip(),
        "checked": checked,
        "managed_key": managed_key.strip(),
        "timestamp": timestamp or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    append_jsonl(todo_state_path(work_dir), payload)
    sync_todo_projection(work_dir)
    return payload


def ensure_managed_todo_item(
    work_dir: Path,
    *,
    section: str,
    text: str,
    managed_key: str,
) -> dict[str, object]:
    current = todo_items(work_dir)
    for item in current:
        if str(item.get("managed_key") or "") == managed_key:
            return item
    return create_todo_item(
        work_dir,
        section=section,
        text=text,
        managed_key=managed_key,
        item_id=managed_todo_id(managed_key),
    )


def set_todo_checked(work_dir: Path, item_id: str, *, checked: bool) -> dict[str, object] | None:
    normalized = item_id.strip().upper()
    if not is_todo_id(normalized):
        raise SystemExit(f"invalid TODO item id: {item_id}")
    current = {str(item["id"]).upper(): item for item in todo_items(work_dir)}
    item = current.get(normalized)
    if item is None:
        raise SystemExit(f"no TODO item matched id: {normalized}")
    if bool(item["checked"]) == checked:
        return None
    payload: dict[str, object] = {
        "op": "check",
        "id": normalized,
        "checked": checked,
        "timestamp": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    append_jsonl(todo_state_path(work_dir), payload)
    sync_todo_projection(work_dir)
    updated = dict(item)
    updated["checked"] = checked
    return updated


def resolve_todo_item(work_dir: Path, *, item_id: str) -> dict[str, object]:
    normalized = item_id.strip().upper()
    if not is_todo_id(normalized):
        raise SystemExit(
            f"invalid TODO item id: {item_id}. run `gotta todo show` and pass printed ids"
        )
    for item in todo_items(work_dir):
        if str(item["id"]).upper() == normalized:
            return item
    raise SystemExit(f"no TODO item matched id: {normalized}")
