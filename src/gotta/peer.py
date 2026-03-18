"""Shared peer-session path helpers."""

from __future__ import annotations

from pathlib import Path

from gotta import content


PEER_SESSION_ACTOR_ENV = "GOTTA_WORK_SESSION_ACTOR"
SUPERVISOR_STOP_STATUS = "failed"
SUPERVISOR_GRACEFUL_STOP_MODE = "stop"
SUPERVISOR_GRACEFUL_STOP_STATUS = "signed_off"
SUPERVISOR_STOP_FALLBACK_STATUSES = {"starting", "active", "stalled"}


def normalize_peer_name(value: str) -> str:
    return content.sanitize_name(value.strip().lower())


def session_actor(root: Path) -> str:
    state = content.load_state_env_at_root(root)
    return normalize_peer_name(str(state.get(PEER_SESSION_ACTOR_ENV) or ""))


def peer_session_id(parent_root: Path, peer_name: str) -> str:
    return content.sanitize_name(
        f"{content.session_id(parent_root)}-{normalize_peer_name(peer_name)}"
    )


def peer_link_path(parent_root: Path, peer_name: str) -> Path:
    return parent_root / "peers" / normalize_peer_name(peer_name)


def peer_state_link_dir(parent_root: Path, peer_name: str) -> Path:
    return parent_root / "state" / "peers" / normalize_peer_name(peer_name)


def peer_session_root(parent_root: Path, peer_name: str) -> Path:
    normalized = normalize_peer_name(peer_name)
    if session_actor(parent_root) == normalized:
        return parent_root.resolve()
    link = peer_link_path(parent_root, normalized)
    if link.exists() or link.is_symlink():
        return link.resolve()
    return content.DEFAULT_SESSION_ROOT.expanduser().resolve() / peer_session_id(
        parent_root,
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
    peer_name: str,
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
        else ". Any further work may be discarded. Stop new retrieval, append one "
        + "final durable note, and sign off ASAP with "
    )
    return message + suffix + f"`gotta peer signoff {normalize_peer_name(peer_name)} --summary ...`."
