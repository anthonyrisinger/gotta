"""Next-step synthesis for actor status payloads."""

from __future__ import annotations

from gotta.session.status.payload.active import active_next_step
from gotta.session.status.payload.activity import note_check_next_step
from gotta.session.status.payload.closing import closing_next_step
from gotta.session.status.payload.model import ActorStatusPayload
from gotta.session.status.payload.pending import pending_next_step
from gotta.session.registry import _normalize_actor_name
from gotta.session.status.payload.value import int_value
from gotta.session.status.payload.runtime import low_signal_next_step, runtime_next_step


def actor_next_step(actor_name: str, payload: ActorStatusPayload) -> str:
    normalized_actor = _normalize_actor_name(actor_name)
    derived_status = str(payload.get("status") or "")
    next_step = runtime_next_step(normalized_actor, payload)
    if not next_step:
        next_step = closing_next_step(normalized_actor, payload)
    if not next_step:
        next_step = active_next_step(normalized_actor, payload)
    if not next_step:
        next_step = pending_next_step(normalized_actor, payload)
    low_signal_step = low_signal_next_step(payload)
    if low_signal_step:
        next_step = low_signal_step
    pulse_next_step = note_check_next_step(
        note_checks_since_update=int_value(payload.get("note_checks_since_update")),
        last_note_at=str(payload.get("last_note_at") or ""),
        derived_status=derived_status,
    )
    if pulse_next_step:
        next_step = f"{pulse_next_step} {next_step}".strip()
    return next_step
