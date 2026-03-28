"""Actor bind and bind-receipt helpers."""

from __future__ import annotations

from pathlib import Path

from gotta.content.path import sh_quote
from gotta.session.activity import _actor_log_line, _append_actor_event
from gotta.session.bootstrap import _ensure_actor_surface
from gotta.session.registry import (
    WANT_FILE,
    _actor_charter_command,
    _actor_dir_path,
    _actor_is_selected,
    _actor_label,
    _bind_actor_identity,
    _read_actor_state,
    _write_actor_state,
)
from gotta.session.status.blocker import _actor_launch_blockers


def _actor_launch_command(work_dir: Path, actor_name: str) -> str:
    return f"gotta actor launch {actor_name} --session {sh_quote(str(work_dir))}"


def _render_actor_bind_message(payload: dict[str, object]) -> str:
    actor = str(payload.get("actor") or "").strip()
    label = str(payload.get("label") or actor).strip()
    actor_want = str(payload.get("wantPath") or "").strip()
    actor_goal = str(payload.get("goalPath") or "").strip()
    want_cmd = str(payload.get("wantCommand") or "").strip()
    goal_cmd = str(payload.get("goalCommand") or "").strip()
    todo_cmd = str(payload.get("todoCommand") or "").strip()
    launch_cmd = str(payload.get("launchCommand") or "").strip()
    raw_blockers = payload.get("blockers")
    actor_blockers = (
        [str(item).strip() for item in raw_blockers if str(item).strip()]
        if isinstance(raw_blockers, list)
        else []
    )
    if actor_blockers:
        suffix = (
            f"; not launched. This bind completed the addressable actor target, so rewrite `{actor_want}` and `{actor_goal}` for {label} with `{want_cmd}` and `{goal_cmd}` first. "
            f"Use `{todo_cmd}` to extend the minimal actor-local checklist before launch if useful, "
            f"then launch with `{launch_cmd}` when you actually want {label} to start"
        )
    else:
        suffix = (
            f"; not launched. `{actor_want}` and `{actor_goal}` are already real. "
            f"Use `{todo_cmd}` to extend the minimal actor-local checklist before launch if useful, "
            f"then launch with `{launch_cmd}` when you actually want {label} to start"
        )
    if bool(payload.get("alreadyBound")):
        return f"{actor} ({label}) already bound{suffix}"
    created_note = " [new actor]" if bool(payload.get("created")) else ""
    return (
        f"bound {actor} ({label}) session, seeded actor-local WANT/GOAL placeholders, minimal actor-local canonical state, and shared evidence access{created_note}"
        f"{suffix}"
    )


def _bind_actor(session_root: Path, actor_name: str) -> dict[str, object]:
    actor, created = _bind_actor_identity(session_root, actor_name)
    already_bound = _actor_is_selected(session_root, actor)
    _ensure_actor_surface(session_root, actor)
    label = _actor_label(actor, work_dir=session_root)
    current_status = str(
        _read_actor_state(session_root, actor).get("status") or "pending"
    )
    if current_status in {
        "",
        "pending",
        "completed",
        "failed",
        "incomplete",
        "rejected",
        "signed_off",
    }:
        _write_actor_state(session_root, actor, {"status": "bound"})
    launch_cmd = _actor_launch_command(session_root, actor)
    actor_want = _actor_dir_path(session_root, actor) / WANT_FILE
    actor_goal = _actor_dir_path(session_root, actor) / "GOAL.md"
    want_cmd = _actor_charter_command(actor, "want")
    goal_cmd = _actor_charter_command(actor, "goal")
    actor_blockers = _actor_launch_blockers(session_root, actor_name=actor)
    todo_cmd = f"gotta todo --actor {actor}"
    if already_bound:
        status = "already_bound"
    else:
        _append_actor_event(
            session_root, actor, event="bound", detail="bound actor session"
        )
        _actor_log_line(session_root, actor, "bound session")
        status = "bound"
    payload: dict[str, object] = {
        "actor": actor,
        "label": label,
        "created": created,
        "alreadyBound": already_bound,
        "status": status,
        "sessionRoot": str(session_root.resolve()),
        "actorRoot": str(_actor_dir_path(session_root, actor).resolve()),
        "wantPath": str(actor_want.resolve()),
        "goalPath": str(actor_goal.resolve()),
        "wantCommand": want_cmd,
        "goalCommand": goal_cmd,
        "todoCommand": todo_cmd,
        "launchCommand": launch_cmd,
        "blockers": actor_blockers,
    }
    payload["message"] = _render_actor_bind_message(payload)
    return payload
