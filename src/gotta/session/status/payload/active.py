"""Next-step synthesis for live actor runs."""

from __future__ import annotations


def active_next_step(actor_name: str, payload: dict[str, object]) -> str:
    derived_status = str(payload.get("status") or "")
    notes_ready = bool(payload.get("notes_ready"))
    evidence_note = str(payload.get("evidence_note") or "")
    request_note = str(payload.get("request_note") or "")
    runtime_note = str(payload.get("runtime_note") or "")
    voice = str(payload.get("voice") or "missing")
    voice_pulse = voice == "pulse"
    voice_setup = voice == "setup"
    needs_note_refresh = bool(payload.get("needs_note_refresh"))

    if derived_status == "producing_evidence":
        if notes_ready and needs_note_refresh:
            return (
                "actor is still active and producing evidence artifacts, and new evidence landed "
                "after the last short note. "
                + (evidence_note + " " if evidence_note else "")
                + "Land a short note now so the latest evidence wave has durable actor narration, "
                f"then recheck `gotta actor status {actor_name}` shortly."
                + request_note
                + runtime_note
            )
        if notes_ready:
            return (
                "actor is still active and producing evidence artifacts. "
                + (evidence_note + " " if evidence_note else "")
                + "Use `gotta notes` for live actor visibility; recheck `gotta actor status "
                f"{actor_name}` shortly before closing the actor out."
                + request_note
                + runtime_note
            )
        if voice_pulse:
            return (
                "actor is still active and producing evidence artifacts. Non-note signal is "
                "already present through friction or shared evidence, but the first short actor "
                "note has not landed yet. "
                + (evidence_note + " " if evidence_note else "")
                + "Let the current evidence wave finish, then append a short actor-authored note "
                "before requesting completion or sign-off."
                + request_note
                + runtime_note
            )
        if voice_setup:
            return (
                "actor is still active and producing evidence artifacts. Setup note is present, "
                "but actor voice has not landed yet. "
                + (evidence_note + " " if evidence_note else "")
                + "Append a short actor-authored note before requesting completion or sign-off, "
                f"then recheck `gotta actor status {actor_name}` shortly."
                + request_note
                + runtime_note
            )
        return (
            "actor is still active and producing evidence artifacts, but actor voice is still "
            "missing. "
            + (evidence_note + " " if evidence_note else "")
            + "Append a short actor-authored note before requesting completion or sign-off, "
            f"then recheck `gotta actor status {actor_name}` shortly."
            + request_note
            + runtime_note
        )

    if derived_status not in {"starting", "active"}:
        return ""
    if notes_ready and needs_note_refresh:
        return (
            "actor is still active and new actor-attributed evidence landed after the last short "
            "note. "
            + (evidence_note + " " if evidence_note else "")
            + "Land a short note now so the current evidence wave is narrated before close-out."
            + request_note
            + runtime_note
        )
    if notes_ready:
        return (
            "actor is still active and actor voice is present. "
            "Use `gotta notes` for live actor visibility; recheck `gotta actor status "
            f"{actor_name}` shortly before closing the actor out."
            + request_note
            + runtime_note
        )
    if voice_pulse:
        return (
            "actor is live and non-note signal is already landing through friction or shared "
            "evidence, but the first short actor note has not landed yet. Give the runtime a brief "
            "window to turn that signal into a short note before treating this as a "
            "visibility failure." + request_note + runtime_note
        )
    if voice_setup:
        return (
            "setup note is present, but actor voice is still missing. Give the runtime a brief "
            "startup window to land the first short actor-authored note before treating this as a "
            "visibility failure. If actor voice is still missing after one heartbeat interval or "
            "after the first materialized artifact, intervene and recheck actor status."
            + request_note
            + runtime_note
        )
    return (
        "actor is live, but actor voice is still missing. Give the runtime a brief startup "
        "window to land the first short actor-authored note before treating this as a "
        "visibility failure. If actor voice is still missing after one heartbeat interval or "
        "after the first materialized artifact, intervene and recheck actor status."
        + request_note
        + runtime_note
    )
