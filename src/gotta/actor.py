"""Shared actor-session path helpers."""

from __future__ import annotations

import json
from pathlib import Path

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
        ". Stop new retrieval, append one final durable note, and sign off ASAP with "
        if (
            requested_mode == SUPERVISOR_GRACEFUL_STOP_MODE
            and requested_status == SUPERVISOR_GRACEFUL_STOP_STATUS
        )
        else ". Any further activity may be discarded. Stop new retrieval, append one "
        + "final durable note, and sign off ASAP with "
    )
    return message + suffix + f"`gotta actor signoff {normalize_actor_name(actor_name)} --summary ...`."
