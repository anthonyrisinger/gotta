"""Requested-disposition payload helpers."""

from __future__ import annotations

from gotta.actor import requested_disposition_label
from gotta.session.registry import _normalize_actor_name
from gotta.session.status.payload.value import ACTOR_TERMINAL_STATUS


def apply_requested_closing(derived_status: str, requested_status: str) -> str:
    if requested_status and derived_status in {
        "starting",
        "active",
        "producing_evidence",
    }:
        return "closing"
    return derived_status


def request_state_payload(
    state: dict[str, object],
    *,
    actor_name: str,
    derived_status: str,
) -> dict[str, object]:
    requested_status = str(state.get("requested_status") or "")
    requested_summary = str(state.get("requested_summary") or "")
    requested_label = requested_disposition_label(state)
    request_note = ""
    if requested_status and derived_status not in ACTOR_TERMINAL_STATUS:
        if derived_status == "stalled":
            request_note = (
                f" Operator already requested `{requested_label}`"
                + (f" ({requested_summary})." if requested_summary else ".")
                + " Because the heartbeat is stale, you can settle now with "
                f"`gotta actor settle {_normalize_actor_name(actor_name)}`."
            )
        else:
            request_note = (
                f" Operator already requested `{requested_label}`"
                + (f" ({requested_summary})." if requested_summary else ".")
                + " That pending disposition will become authoritative automatically "
                "when the actor runtime exits."
            )
    return {
        "requested_status": requested_status,
        "requested_summary": requested_summary,
        "requested_label": requested_label,
        "requested_pending": bool(
            requested_status and derived_status not in ACTOR_TERMINAL_STATUS
        ),
        "request_note": request_note,
    }
