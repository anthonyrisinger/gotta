"""Next-step synthesis for closing and terminal actor states."""

from __future__ import annotations


def closing_next_step(actor_name: str, payload: dict[str, object]) -> str:
    derived_status = str(payload.get("status") or "")
    requested_status = str(payload.get("requested_status") or "")
    requested_label = str(payload.get("requested_label") or "")
    notes_ready = bool(payload.get("notes_ready"))
    evidence_live = bool(payload.get("evidence_live"))
    evidence_note = str(payload.get("evidence_note") or "")
    request_note = str(payload.get("request_note") or "")
    runtime_note = str(payload.get("runtime_note") or "")
    voice = str(payload.get("voice") or "missing")
    voice_pulse = voice == "pulse"
    voice_setup = voice == "setup"
    voice_missing = voice == "missing"
    needs_note_refresh = bool(payload.get("needs_note_refresh"))

    if derived_status == "closing":
        if notes_ready and needs_note_refresh:
            return (
                "actor close-out is pending while the runtime is still live, and new "
                "actor-attributed evidence landed after the last short note. "
                + (evidence_note + " " if evidence_note else "")
                + "Land one final short note now so the close-out reflects the latest evidence, "
                "then wait for runtime exit before treating the terminal disposition as authoritative."
                + request_note
                + runtime_note
            )
        if notes_ready:
            return (
                "actor close-out is pending while the runtime is still live. "
                + (evidence_note + " " if evidence_note else "")
                + "Let the actor finish the current wave, then wait for runtime exit before "
                "treating the terminal disposition as authoritative."
                + request_note
                + runtime_note
            )
        if voice_pulse:
            return (
                "actor close-out is pending while the runtime is still live. Non-note signal is "
                "already landing through friction or shared evidence, but the final short actor "
                "note is still missing. "
                + (evidence_note + " " if evidence_note else "")
                + "Land one final short actor-authored note before runtime exit so the close-out "
                "has durable voice, then recheck actor status."
                + request_note
                + runtime_note
            )
        if voice_setup:
            return (
                "actor close-out is pending while the runtime is still live. Setup note is present, "
                "but actor voice is still missing. "
                + (evidence_note + " " if evidence_note else "")
                + "Land one final short actor-authored note before runtime exit so the close-out "
                "has real voice, then recheck actor status."
                + request_note
                + runtime_note
            )
        return (
            "actor close-out is pending while the runtime is still live, but actor voice is "
            "still missing. "
            + (evidence_note + " " if evidence_note else "")
            + "Land one final short actor-authored note before runtime exit so the close-out "
            "has voice, then recheck actor status." + request_note + runtime_note
        )

    if derived_status == "awaiting_disposition":
        return (
            "actor runtime is no longer running, but no durable terminal lifecycle was recorded yet. "
            + (evidence_note + " " if evidence_note else "")
            + "Inspect `gotta notes` plus the shared evidence web, then settle with "
            f"`gotta actor settle {actor_name}`"
            + (
                f" to honor the pending `{requested_label}` request."
                if requested_status
                else " to record the authoritative terminal disposition."
            )
            + request_note
        )
    if derived_status == "completed" and (notes_ready or evidence_live):
        if notes_ready and needs_note_refresh:
            return (
                "actor run is complete, but new actor-attributed evidence landed after the last "
                "short note. Land one short note now, then record durable sign-off intentionally."
            )
        if notes_ready:
            return (
                "actor run is complete; inspect `gotta notes` plus the shared evidence web, then record "
                "durable sign-off with "
                f"`gotta actor signoff {actor_name} --summary ...`."
            )
        if voice_pulse:
            return (
                "actor run is complete and non-note signal landed through friction or shared "
                "evidence, but the final short note is still missing. Add one short actor-authored "
                "note now, then sign off intentionally."
            )
        return (
            "actor run is complete and evidence landed, but actor voice is still missing. Wait "
            "for a short actor-authored note or sign off intentionally only if you are "
            "explicitly accepting an evidence-only actor contribution."
        )
    if derived_status == "incomplete":
        return (
            "actor finished without material notes or evidence. Decide whether to relaunch, "
            "fail, or sign off intentionally."
        )
    if derived_status == "failed" and evidence_note:
        return (
            "actor was manually marked failed, but evidence already landed in shared state. "
            + evidence_note
            + " Keep or reject that evidence intentionally instead of assuming it vanished."
        )
    if derived_status == "stalled" and (not voice_missing or evidence_live):
        return (
            "actor heartbeat is stale, but material actor state already exists in `gotta notes` or the "
            "shared evidence web. "
            + (evidence_note + " " if evidence_note else "")
            + "Inspect the notes and decide whether to wait, relaunch, or disposition manually."
            + request_note
        )
    return ""
