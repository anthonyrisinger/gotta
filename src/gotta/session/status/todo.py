"""Actor-managed TODO synchronization helpers."""

from __future__ import annotations

from pathlib import Path

from gotta.todo import ensure_managed_todo_item, set_todo_checked, todo_items
from gotta.session.registry import _actor_label, _normalize_actor_name
from gotta.session.scope import _selected_actor_ids
from gotta.session.status.marker import FINAL_SIGNOFF_MARKER, _actor_todo_marker
from gotta.session.status.payload.main import _actor_status_payload
from gotta.session.status.payload.value import ACTOR_TERMINAL_STATUS


def _ensure_actor_todo_items(work_dir: Path, actor_name: str) -> None:
    actor = _normalize_actor_name(actor_name)
    label = _actor_label(actor, work_dir=work_dir)
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text=f"Initial actor pass collected from {label}",
        managed_key=_actor_todo_marker(actor, "initial"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text=f"{label} run materially complete",
        managed_key=_actor_todo_marker(actor, "complete"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text=f"{label} findings dispositioned",
        managed_key=_actor_todo_marker(actor, "dispositioned"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text="Final actor sign-off collected after edits for the chosen team",
        managed_key=FINAL_SIGNOFF_MARKER,
    )


def _sync_actor_todo_state(work_dir: Path) -> None:
    actor_ids = _selected_actor_ids(work_dir)
    actor_payloads = {
        actor: _actor_status_payload(work_dir, actor) for actor in actor_ids
    }
    launched_actor_ids = [
        actor
        for actor in actor_ids
        if str(actor_payloads[actor].get("status") or "pending")
        not in {"pending", "bound"}
    ]
    for actor_name in launched_actor_ids:
        _ensure_actor_todo_items(work_dir, actor_name)
    items_by_key = {
        str(item.get("managed_key") or ""): item
        for item in todo_items(work_dir)
        if item.get("managed_key")
    }
    for actor_name in launched_actor_ids:
        payload = actor_payloads[actor_name]
        materially_complete = bool(
            payload.get("notes_ready") or payload.get("evidence_live")
        )
        terminal = str(payload.get("status") or "") in ACTOR_TERMINAL_STATUS
        signed_off = str(payload.get("status") or "") == "signed_off"
        for marker, checked in (
            (_actor_todo_marker(actor_name, "initial"), materially_complete),
            (_actor_todo_marker(actor_name, "complete"), terminal),
            (_actor_todo_marker(actor_name, "dispositioned"), signed_off),
        ):
            item = items_by_key.get(marker)
            if item is None:
                continue
            updated = set_todo_checked(work_dir, str(item["id"]), checked=checked)
            if updated is not None:
                items_by_key[marker] = updated
    final_item = items_by_key.get(FINAL_SIGNOFF_MARKER)
    final_checked = bool(launched_actor_ids) and all(
        str(actor_payloads[actor].get("status") or "") == "signed_off"
        for actor in launched_actor_ids
    )
    if final_item is not None:
        set_todo_checked(work_dir, str(final_item["id"]), checked=final_checked)
