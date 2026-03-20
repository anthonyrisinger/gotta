"""Shared actor-session path helpers."""

from __future__ import annotations

from pathlib import Path

from gotta import content
from gotta import topology


SESSION_ACTOR_ENV = "GOTTA_SESSION_ACTOR"
SUPERVISOR_STOP_STATUS = "failed"
SUPERVISOR_GRACEFUL_STOP_MODE = "stop"
SUPERVISOR_GRACEFUL_STOP_STATUS = "signed_off"
SUPERVISOR_STOP_FALLBACK_STATUSES = {"starting", "active", "stalled"}


def normalize_actor_name(value: str) -> str:
    return content.sanitize_name(value.strip().lower())


def session_actor(root: Path) -> str:
    state = content.load_state_env_at_root(root)
    return normalize_actor_name(str(state.get(SESSION_ACTOR_ENV) or ""))


def actor_session_id(parent_root: Path, actor_name: str) -> str:
    return topology.default_actor_session_id(
        content.session_shared_id(parent_root),
        normalize_actor_name(actor_name),
    )


def actor_link_path(parent_root: Path, actor_name: str) -> Path:
    return topology.session_root_for(
        content.session_shared_id(parent_root),
        normalize_actor_name(actor_name),
    )


def actor_state_link_dir(parent_root: Path, actor_name: str) -> Path:
    return actor_link_path(parent_root, actor_name) / "state"


def actor_session_root(parent_root: Path, actor_name: str) -> Path:
    normalized = normalize_actor_name(actor_name)
    if session_actor(parent_root) == normalized:
        return parent_root.resolve()
    return topology.session_root_for(
        content.session_shared_id(parent_root),
        normalized,
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
        ". Stop new retrieval, append one final durable note, and sign off ASAP with "
        if (
            requested_mode == SUPERVISOR_GRACEFUL_STOP_MODE
            and requested_status == SUPERVISOR_GRACEFUL_STOP_STATUS
        )
        else ". Any further activity may be discarded. Stop new retrieval, append one "
        + "final durable note, and sign off ASAP with "
    )
    return message + suffix + f"`gotta actor signoff {normalize_actor_name(actor_name)} --summary ...`."
