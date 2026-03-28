"""Next-step synthesis for actor status payloads."""

from __future__ import annotations

from gotta.session.registry import _normalize_actor_name
from gotta.session.status.payload.value import ACTOR_TERMINAL_STATUS, int_value


def actor_next_step(actor_name: str, payload: dict[str, object]) -> str:
    normalized_actor = _normalize_actor_name(actor_name)
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
    runtime_broken = bool(payload.get("runtime_broken"))
    runtime_stop_signal_at = str(payload.get("runtime_stop_signal_at") or "")
    runtime_stop_signal = str(payload.get("runtime_stop_signal") or "SIGTERM")
    runtime_issue_summary = str(payload.get("runtime_issue_summary") or "")
    note_checks_since_update = int_value(payload.get("note_checks_since_update"))
    last_note_at = str(payload.get("last_note_at") or "")
    low_signal_progress = bool(payload.get("low_signal_progress"))

    if runtime_broken:
        issue_clause = (
            runtime_issue_summary + " "
            if runtime_issue_summary
            else "Upstream provider failures are keeping the runtime in a retry loop. "
        )
        if runtime_stop_signal_at:
            next_step = (
                "actor runtime is broken and still has not produced actor-authored voice, "
                "progress, or evidence. "
                + issue_clause
                + f"Shutdown signal `{runtime_stop_signal or 'SIGTERM'}` was already sent at "
                f"{runtime_stop_signal_at}. "
                + (
                    f"Pending `{requested_label}` disposition remains authoritative when the runtime exits."
                    if requested_status
                    else "Recheck actor status shortly and intervene at the OS level only if the process still refuses to exit."
                )
            )
        else:
            next_step = (
                "actor runtime is broken and still has not produced actor-authored voice, "
                "progress, or evidence. "
                + issue_clause
                + "This is not normal warmup. "
                + (
                    f"Stop the runtime with `gotta actor stop {normalized_actor} --summary ...`."
                    + f" Pending `{requested_label}` disposition will remain authoritative when the runtime exits."
                    if requested_status
                    else f"Record `gotta actor fail {normalized_actor} --summary ...` now, "
                    + f"then stop the runtime with `gotta actor stop {normalized_actor} --summary ...`."
                )
            )
    elif derived_status == "closing":
        if notes_ready and needs_note_refresh:
            next_step = (
                "actor close-out is pending while the runtime is still live, and new "
                "actor-attributed evidence landed after the last short note. "
                + (evidence_note + " " if evidence_note else "")
                + "Land one final short note now so the close-out reflects the latest evidence, "
                "then wait for runtime exit before treating the terminal disposition as authoritative."
                + request_note
                + runtime_note
            )
        elif notes_ready:
            next_step = (
                "actor close-out is pending while the runtime is still live. "
                + (evidence_note + " " if evidence_note else "")
                + "Let the actor finish the current wave, then wait for runtime exit before "
                "treating the terminal disposition as authoritative."
                + request_note
                + runtime_note
            )
        elif voice_pulse:
            next_step = (
                "actor close-out is pending while the runtime is still live. Non-note signal is "
                "already landing through friction or shared evidence, but the final short actor "
                "note is still missing. "
                + (evidence_note + " " if evidence_note else "")
                + "Land one final short actor-authored note before runtime exit so the close-out "
                "has durable voice, then recheck actor status."
                + request_note
                + runtime_note
            )
        elif voice_setup:
            next_step = (
                "actor close-out is pending while the runtime is still live. Setup note is present, "
                "but actor voice is still missing. "
                + (evidence_note + " " if evidence_note else "")
                + "Land one final short actor-authored note before runtime exit so the close-out "
                "has real voice, then recheck actor status."
                + request_note
                + runtime_note
            )
        else:
            next_step = (
                "actor close-out is pending while the runtime is still live, but actor voice is "
                "still missing. "
                + (evidence_note + " " if evidence_note else "")
                + "Land one final short actor-authored note before runtime exit so the close-out "
                "has voice, then recheck actor status." + request_note + runtime_note
            )
    elif derived_status == "producing_evidence":
        if notes_ready and needs_note_refresh:
            next_step = (
                "actor is still active and producing evidence artifacts, and new evidence landed "
                "after the last short note. "
                + (evidence_note + " " if evidence_note else "")
                + "Land a short note now so the latest evidence wave has durable actor narration, "
                f"then recheck `gotta actor status {normalized_actor}` shortly."
                + request_note
                + runtime_note
            )
        elif notes_ready:
            next_step = (
                "actor is still active and producing evidence artifacts. "
                + (evidence_note + " " if evidence_note else "")
                + "Use `gotta notes` for live actor visibility; recheck `gotta actor status "
                f"{normalized_actor}` shortly before closing the actor out."
                + request_note
                + runtime_note
            )
        elif voice_pulse:
            next_step = (
                "actor is still active and producing evidence artifacts. Non-note signal is "
                "already present through friction or shared evidence, but the first short actor "
                "note has not landed yet. "
                + (evidence_note + " " if evidence_note else "")
                + "Let the current evidence wave finish, then append a short actor-authored note "
                "before requesting completion or sign-off."
                + request_note
                + runtime_note
            )
        elif voice_setup:
            next_step = (
                "actor is still active and producing evidence artifacts. Setup note is present, "
                "but actor voice has not landed yet. "
                + (evidence_note + " " if evidence_note else "")
                + "Append a short actor-authored note before requesting completion or sign-off, "
                f"then recheck `gotta actor status {normalized_actor}` shortly."
                + request_note
                + runtime_note
            )
        else:
            next_step = (
                "actor is still active and producing evidence artifacts, but actor voice is still "
                "missing. "
                + (evidence_note + " " if evidence_note else "")
                + "Append a short actor-authored note before requesting completion or sign-off, "
                f"then recheck `gotta actor status {normalized_actor}` shortly."
                + request_note
                + runtime_note
            )
    elif (
        derived_status in {"starting", "active"} and notes_ready and needs_note_refresh
    ):
        next_step = (
            "actor is still active and new actor-attributed evidence landed after the last short "
            "note. "
            + (evidence_note + " " if evidence_note else "")
            + "Land a short note now so the current evidence wave is narrated before close-out."
            + request_note
            + runtime_note
        )
    elif derived_status in {"starting", "active"} and notes_ready:
        next_step = (
            "actor is still active and actor voice is present. "
            "Use `gotta notes` for live actor visibility; recheck `gotta actor status "
            f"{normalized_actor}` shortly before closing the actor out."
            + request_note
            + runtime_note
        )
    elif derived_status in {"starting", "active"} and voice_pulse:
        next_step = (
            "actor is live and non-note signal is already landing through friction or shared "
            "evidence, but the first short actor note has not landed yet. Give the runtime a brief "
            "window to turn that signal into a short note before treating this as a "
            "visibility failure." + request_note + runtime_note
        )
    elif derived_status in {"starting", "active"} and voice_setup:
        next_step = (
            "setup note is present, but actor voice is still missing. Give the runtime a brief "
            "startup window to land the first short actor-authored note before treating this as a "
            "visibility failure. If actor voice is still missing after one heartbeat interval or "
            "after the first materialized artifact, intervene and recheck actor status."
            + request_note
            + runtime_note
        )
    elif derived_status in {"starting", "active"}:
        next_step = (
            "actor is live, but actor voice is still missing. Give the runtime a brief startup "
            "window to land the first short actor-authored note before treating this as a "
            "visibility failure. If actor voice is still missing after one heartbeat interval or "
            "after the first materialized artifact, intervene and recheck actor status."
            + request_note
            + runtime_note
        )
    elif derived_status == "awaiting_disposition":
        next_step = (
            "actor runtime is no longer running, but no durable terminal lifecycle was recorded yet. "
            + (evidence_note + " " if evidence_note else "")
            + "Inspect `gotta notes` plus the shared evidence web, then settle with "
            f"`gotta actor settle {normalized_actor}`"
            + (
                f" to honor the pending `{requested_label}` request."
                if requested_status
                else " to record the authoritative terminal disposition."
            )
            + request_note
        )
    elif derived_status == "stalled" and (not voice_missing or evidence_live):
        next_step = (
            "actor heartbeat is stale, but material actor state already exists in `gotta notes` or the "
            "shared evidence web. "
            + (evidence_note + " " if evidence_note else "")
            + "Inspect the notes and decide whether to wait, relaunch, or disposition manually."
            + request_note
        )
    elif derived_status == "completed" and (notes_ready or evidence_live):
        if notes_ready and needs_note_refresh:
            next_step = (
                "actor run is complete, but new actor-attributed evidence landed after the last "
                "short note. Land one short note now, then record durable sign-off intentionally."
            )
        elif notes_ready:
            next_step = (
                "actor run is complete; inspect `gotta notes` plus the shared evidence web, then record "
                "durable sign-off with "
                f"`gotta actor signoff {normalized_actor} --summary ...`."
            )
        elif voice_pulse:
            next_step = (
                "actor run is complete and non-note signal landed through friction or shared "
                "evidence, but the final short note is still missing. Add one short actor-authored "
                "note now, then sign off intentionally."
            )
        else:
            next_step = (
                "actor run is complete and evidence landed, but actor voice is still missing. Wait "
                "for a short actor-authored note or sign off intentionally only if you are "
                "explicitly accepting an evidence-only actor contribution."
            )
    elif derived_status == "incomplete":
        next_step = (
            "actor finished without material notes or evidence. Decide whether to relaunch, "
            "fail, or sign off intentionally."
        )
    elif derived_status == "failed" and evidence_note:
        next_step = (
            "actor was manually marked failed, but evidence already landed in shared state. "
            + evidence_note
            + " Keep or reject that evidence intentionally instead of assuming it vanished."
        )
    elif (
        derived_status in {"pending", "bound"}
        and notes_ready
        and evidence_live
        and needs_note_refresh
    ):
        next_step = (
            "actor already has actor-authored narration and shared evidence, but new evidence "
            "landed after the last short note. "
            + (evidence_note + " " if evidence_note else "")
            + "Land a short note now, then keep landing short notes after each substantive "
            "evidence wave so review, handoff, and session-wide inspection surfaces stay current."
        )
    elif derived_status in {"pending", "bound"} and notes_ready and evidence_live:
        next_step = (
            "actor already has actor-authored narration and shared evidence without an active runtime. "
            + (evidence_note + " " if evidence_note else "")
            + "Keep landing notes as the session evolves; when this actor's contribution is "
            "materially complete, record the authoritative close-out intentionally with "
            f"`gotta actor signoff {normalized_actor} --summary ...`."
        )
    elif derived_status in {"pending", "bound"} and voice_pulse:
        next_step = (
            "non-note signal is present through friction or shared evidence, but no short actor "
            "note has landed yet. "
            + (evidence_note + " " if evidence_note else "")
            + "Land one short actor-authored note now, then keep landing short notes after each "
            "material evidence wave so review, handoff, and session-wide inspection surfaces have "
            "continuous actor voice."
        )
    elif derived_status in {"pending", "bound"} and notes_ready:
        next_step = (
            "actor already has actor-authored narration but no shared evidence artifacts yet. "
            "Continue retrieval if more evidence should land, or close out intentionally once "
            f"the narrative is complete with `gotta actor signoff {normalized_actor} --summary ...`."
        )
    elif derived_status in {"pending", "bound"} and voice_setup:
        next_step = (
            "setup note is present, but actor voice has not landed yet. Continue retrieval until the "
            "actor writes a short note, or close this branch out intentionally only if setup-only "
            "state is truly sufficient."
        )
    elif evidence_live and not notes_ready:
        next_step = (
            "actor-attributed evidence is already live in the shared session web, but actor voice is "
            "still missing. "
            + (evidence_note + " " if evidence_note else "")
            + "Land one short actor-authored note now, then keep landing short notes as the story "
            "moves so review, handoff, and session-wide inspection surfaces have actor voice "
            "instead of evidence-only state."
        )
    else:
        next_step = ""
    pulse_next_step = ""
    if note_checks_since_update > 0 and derived_status not in ACTOR_TERMINAL_STATUS:
        check_noun = "time" if note_checks_since_update == 1 else "times"
        if last_note_at:
            pulse_next_step = (
                f"Supervisor has checked this actor's notes {note_checks_since_update} {check_noun} "
                "since the last note. Land one short note now if real progress exists."
            )
        else:
            pulse_next_step = (
                f"Supervisor has checked this actor's notes {note_checks_since_update} {check_noun} "
                "and no first short note has landed yet. Land one short note now if real progress exists."
            )
    if low_signal_progress:
        next_step = (
            "actor runtime is still live, but actor-authored progress is stale and no "
            "actor-attributed evidence has landed yet. Treat this as a low-signal run until "
            "fresh actor-authored progress or evidence appears."
            + request_note
            + runtime_note
        )
    if pulse_next_step:
        next_step = f"{pulse_next_step} {next_step}".strip()
    return next_step
