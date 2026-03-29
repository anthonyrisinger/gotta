"""Next-step synthesis for pending and bound actor states."""

from __future__ import annotations

from .model import ActorStatusPayload


def pending_next_step(actor_name: str, payload: ActorStatusPayload) -> str:
    derived_status = str(payload.get("status") or "")
    notes_ready = bool(payload.get("notes_ready"))
    evidence_live = bool(payload.get("evidence_live"))
    evidence_note = str(payload.get("evidence_note") or "")
    voice = str(payload.get("voice") or "missing")
    voice_pulse = voice == "pulse"
    voice_setup = voice == "setup"
    needs_note_refresh = bool(payload.get("needs_note_refresh"))

    if (
        derived_status in {"pending", "bound"}
        and notes_ready
        and evidence_live
        and needs_note_refresh
    ):
        return (
            "actor already has actor-authored narration and shared evidence, but new evidence "
            "landed after the last short note. "
            + (evidence_note + " " if evidence_note else "")
            + "Land a short note now, then keep landing short notes after each substantive "
            "evidence wave so review, handoff, and session-wide inspection surfaces stay current."
        )
    if derived_status in {"pending", "bound"} and notes_ready and evidence_live:
        return (
            "actor already has actor-authored narration and shared evidence without an active runtime. "
            + (evidence_note + " " if evidence_note else "")
            + "Keep landing notes as the session evolves; when this actor's contribution is "
            "materially complete, record the authoritative close-out intentionally with "
            f"`gotta actor signoff {actor_name} --summary ...`."
        )
    if derived_status in {"pending", "bound"} and voice_pulse:
        return (
            "non-note signal is present through friction or shared evidence, but no short actor "
            "note has landed yet. "
            + (evidence_note + " " if evidence_note else "")
            + "Land one short actor-authored note now, then keep landing short notes after each "
            "material evidence wave so review, handoff, and session-wide inspection surfaces have "
            "continuous actor voice."
        )
    if derived_status in {"pending", "bound"} and notes_ready:
        return (
            "actor already has actor-authored narration but no shared evidence artifacts yet. "
            "Continue retrieval if more evidence should land, or close out intentionally once "
            f"the narrative is complete with `gotta actor signoff {actor_name} --summary ...`."
        )
    if derived_status in {"pending", "bound"} and voice_setup:
        return (
            "setup note is present, but actor voice has not landed yet. Continue retrieval until the "
            "actor writes a short note, or close this branch out intentionally only if setup-only "
            "state is truly sufficient."
        )
    if evidence_live and not notes_ready:
        return (
            "actor-attributed evidence is already live in the shared session web, but actor voice is "
            "still missing. "
            + (evidence_note + " " if evidence_note else "")
            + "Land one short actor-authored note now, then keep landing short notes as the story "
            "moves so review, handoff, and session-wide inspection surfaces have actor voice "
            "instead of evidence-only state."
        )
    return ""
