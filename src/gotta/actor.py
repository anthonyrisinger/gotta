"""Shared actor-session path helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

from gotta.actors import resolve_actor_context
from gotta import content
from gotta import topology


SESSION_ACTOR_ENV = "GOTTA_SESSION_ACTOR"
ACTOR_ID_ENV = content.ACTOR_ID_ENV
ACTOR_LABEL_ENV = content.ACTOR_LABEL_ENV
SUPERVISOR_STOP_STATUS = "failed"
SUPERVISOR_GRACEFUL_STOP_MODE = "stop"
SUPERVISOR_GRACEFUL_STOP_STATUS = "signed_off"
SUPERVISOR_STOP_FALLBACK_STATUSES = {"starting", "active", "stalled"}


def normalize_actor_name(value: str) -> str:
    return content.sanitize_name(value.strip().lower())


def _shared_root(parent_root: Path) -> Path:
    resolved = parent_root.expanduser().resolve()
    if (resolved / "actors").is_dir():
        return resolved
    if resolved.parent.name == "actors":
        return resolved.parent.parent
    return topology.shared_session_root_for(content.session_shared_id(parent_root))


def session_actor(root: Path) -> str:
    state = content.load_state_env_at_root(root)
    return normalize_actor_name(str(state.get(SESSION_ACTOR_ENV) or ""))


def resolve_actor_identity(parent_root: Path, actor_name: str) -> str:
    normalized = normalize_actor_name(actor_name)
    if not normalized:
        return normalized
    if session_actor(parent_root) == normalized:
        return normalized
    metadata_path = _shared_root(parent_root) / "session.json"
    if not metadata_path.exists():
        return normalized
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return normalized
    if not isinstance(payload, dict):
        return normalized
    raw_actors = payload.get("actors")
    if not isinstance(raw_actors, dict):
        return normalized
    if normalized in raw_actors:
        return topology.normalize_identity(normalized)
    for actor_id, actor_payload in raw_actors.items():
        canonical = topology.normalize_identity(str(actor_id))
        if not canonical or topology.is_placeholder_identity(canonical):
            continue
        if not isinstance(actor_payload, dict):
            continue
        label = normalize_actor_name(str(actor_payload.get("label") or ""))
        template = normalize_actor_name(str(actor_payload.get("template") or ""))
        if normalized in {label, template}:
            return canonical
    return normalized


def actor_session_id(parent_root: Path, actor_name: str) -> str:
    return topology.default_actor_session_id(
        content.session_shared_id(parent_root),
        resolve_actor_identity(parent_root, actor_name),
    )


def actor_link_path(parent_root: Path, actor_name: str) -> Path:
    return _shared_root(parent_root) / "actors" / resolve_actor_identity(parent_root, actor_name)


def actor_state_link_dir(parent_root: Path, actor_name: str) -> Path:
    return actor_link_path(parent_root, actor_name) / "state"


def actor_session_root(parent_root: Path, actor_name: str) -> Path:
    normalized = resolve_actor_identity(parent_root, actor_name)
    if session_actor(parent_root) == normalized:
        return parent_root.resolve()
    return _shared_root(parent_root) / "actors" / normalized


def bound_actors(parent_root: Path) -> tuple[str, ...]:
    shared = _shared_root(parent_root)
    actor_ids: set[str] = set()
    current = session_actor(parent_root)
    if current:
        actor_ids.add(topology.normalize_identity(current))
    actors_dir = shared / "actors"
    if actors_dir.is_dir():
        for actor_dir in sorted(actors_dir.iterdir()):
            if not actor_dir.is_dir():
                continue
            resolved = actor_dir.resolve()
            if not content.session_is_initialized(resolved):
                continue
            identity = topology.session_identity(resolved)
            if not identity or topology.is_placeholder_identity(identity):
                continue
            actor_ids.add(identity)
    return tuple(sorted(actor_ids))


def writer_name() -> str:
    candidates = (
        str(resolve_actor_context(default_speaker="").speaker or "").strip(),
        os.environ.get(ACTOR_ID_ENV, "").strip(),
        os.environ.get(SESSION_ACTOR_ENV, "").strip(),
        str(content.current_context_binding().binding_id or "").strip(),
    )
    for candidate in candidates:
        normalized = topology.normalize_identity(candidate)
        if normalized and not topology.is_placeholder_identity(normalized):
            return normalized
    return ""


def writer_role(parent_root: Path, actor_name: str, *, writer: str) -> str:
    raw_writer = str(writer or "").strip()
    if raw_writer == "launcher":
        return "launcher"
    normalized_writer = topology.normalize_identity(raw_writer)
    if not normalized_writer or topology.is_placeholder_identity(normalized_writer):
        return "foreign"
    normalized_actor = topology.normalize_identity(resolve_actor_identity(parent_root, actor_name))
    if normalized_writer == normalized_actor:
        return "self"
    if normalized_writer in bound_actors(parent_root):
        return "peer"
    return "foreign"


def writer_ok(parent_root: Path, actor_name: str, *, writer: str) -> bool:
    return writer_role(parent_root, actor_name, writer=writer) != "foreign"


def require_writer(
    parent_root: Path,
    actor_name: str,
    *,
    writer: str,
    action: str,
) -> None:
    if writer_ok(parent_root, actor_name, writer=writer):
        return
    normalized_actor = resolve_actor_identity(parent_root, actor_name)
    rendered_writer = str(writer or "").strip() or "unknown writer"
    raise SystemExit(
        f"{rendered_writer} is not a bound actor in this session; bind and launch a sibling "
        f"actor instead of using it to {action} for {normalized_actor}"
    )


def supervisor_stop_pending(status_payload: dict[str, object]) -> bool:
    requested_status = str(status_payload.get("requested_status") or "").strip()
    requested_mode = str(status_payload.get("requested_mode") or "").strip()
    if requested_status != SUPERVISOR_STOP_STATUS and not (
        requested_mode == SUPERVISOR_GRACEFUL_STOP_MODE
        and requested_status == SUPERVISOR_GRACEFUL_STOP_STATUS
    ):
        return False
    explicit_pending = status_payload.get("requested_pending")
    if explicit_pending is not None:
        return bool(explicit_pending)
    return (
        str(status_payload.get("status") or "").strip()
        in SUPERVISOR_STOP_FALLBACK_STATUSES
    )


def requested_disposition_label(status_payload: dict[str, object]) -> str:
    requested_status = str(status_payload.get("requested_status") or "").strip()
    requested_mode = str(status_payload.get("requested_mode") or "").strip()
    if (
        requested_mode == SUPERVISOR_GRACEFUL_STOP_MODE
        and requested_status == SUPERVISOR_GRACEFUL_STOP_STATUS
    ):
        return "stop"
    return requested_status.replace("_", " ")


def supervisor_stop_message(
    actor_name: str,
    *,
    status_payload: dict[str, object] | None = None,
    summary: str = "",
) -> str:
    payload = status_payload or {}
    requested_status = str(payload.get("requested_status") or "").strip()
    requested_mode = str(payload.get("requested_mode") or "").strip()
    message = (
        "Supervisor requested a graceful stop"
        if (
            requested_mode == SUPERVISOR_GRACEFUL_STOP_MODE
            and requested_status == SUPERVISOR_GRACEFUL_STOP_STATUS
        )
        else "Supervisor requested `failed`"
    )
    cleaned_summary = summary.strip() or str(payload.get("requested_summary") or "").strip()
    if cleaned_summary:
        message += f" ({cleaned_summary})"
    suffix = (
        ". Stop new retrieval, append one final short note, and sign off ASAP with "
        if (
            requested_mode == SUPERVISOR_GRACEFUL_STOP_MODE
            and requested_status == SUPERVISOR_GRACEFUL_STOP_STATUS
        )
        else ". Any further activity may be discarded. Stop new retrieval, append one "
        + "final short note, and sign off ASAP with "
    )
    return message + suffix + f"`gotta actor signoff {normalize_actor_name(actor_name)} --summary ...`."


def supervisor_note_check_message(
    actor_name: str,
    *,
    status_payload: dict[str, object] | None = None,
) -> str:
    payload = status_payload or {}
    try:
        checks = int(payload.get("note_checks_since_update") or 0)
    except (TypeError, ValueError):
        checks = 0
    if checks <= 0:
        return ""
    status = str(payload.get("status") or "").strip()
    if status in {"completed", "failed", "incomplete", "rejected", "signed_off"}:
        return ""
    count_word = "time" if checks == 1 else "times"
    if str(payload.get("last_note_at") or "").strip():
        return (
            f"Supervisor has checked your notes {checks} {count_word} since your last note. "
            "If you have real progress, land one short note now."
        )
    return (
        f"Supervisor has checked your notes {checks} {count_word} and no first note has landed yet. "
        "If you have real progress, land one short note now."
    )
