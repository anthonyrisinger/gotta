"""Actor lifecycle status payload synthesis."""

from __future__ import annotations

import os
from pathlib import Path
import time

from gotta.actor import requested_disposition_label
from gotta.compat import datetime
from gotta.notes import actor_notes_status, actor_voice
from gotta.session.activity.summary import (
    _actor_evidence_note,
    _actor_evidence_summary,
    _actor_note_check_summary,
    _actor_note_summary,
    _actor_recent_activity,
)
from gotta.session.registry import (
    ACTOR_STALL_SECONDS,
    _actor_events_path,
    _actor_label,
    _actor_session_dir,
    _actor_state_path,
    _normalize_actor_name,
    _read_actor_state,
    _resolve_bound_actor_name,
)
from gotta.session.status.progress import _actor_progress_summary


ACTOR_RUNNING_STATUS = {
    "starting",
    "active",
    "closing",
    "producing_evidence",
}

ACTOR_TERMINAL_STATUS = {
    "completed",
    "failed",
    "incomplete",
    "rejected",
    "signed_off",
}

ACTOR_STARTUP_GRACE_SECONDS = 30


def _int_value(value: object, *, default: int = 0) -> int:
    try:
        return int(str(value or default))
    except ValueError:
        return default


def _lifecycle_entries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _iso_age_seconds(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return time.time() - parsed.timestamp()


def _actor_status_payload(work_dir: Path, actor_name: str) -> dict[str, object]:
    actor_name = _resolve_bound_actor_name(work_dir, actor_name)
    state = _read_actor_state(work_dir, actor_name)
    status = str(state.get("status") or "pending")
    requested_status = str(state.get("requested_status") or "")
    requested_summary = str(state.get("requested_summary") or "")
    requested_label = requested_disposition_label(state)
    heartbeat_at = str(state.get("heartbeat_at") or "")
    started_at = str(state.get("started_at") or "")
    derived_status = status
    heartbeat_stale = False
    runtime_live: bool | None = None
    try:
        pid = _int_value(state.get("pid"))
    except (TypeError, ValueError):
        pid = 0
    if pid > 0:
        try:
            os.kill(pid, 0)
        except OSError:
            runtime_live = False
        else:
            runtime_live = True
    if (
        runtime_live is None
        and pid <= 0
        and (
            str(state.get("finished_at") or "").strip()
            or state.get("exit_code") is not None
        )
    ):
        runtime_live = False
    started_age_seconds = _iso_age_seconds(started_at)
    if status in {"starting", "active"} and heartbeat_at:
        heartbeat_age_seconds = _iso_age_seconds(heartbeat_at)
        if heartbeat_age_seconds is not None:
            state["heartbeat_age_seconds"] = round(heartbeat_age_seconds, 1)
            if heartbeat_age_seconds > ACTOR_STALL_SECONDS:
                derived_status = "stalled"
                heartbeat_stale = True
    elif status in {"starting", "active"} and not heartbeat_at:
        if (
            started_age_seconds is not None
            and started_age_seconds > ACTOR_STALL_SECONDS
        ):
            derived_status = "stalled"
            heartbeat_stale = True
    signoff_at = str(state.get("signoff_at") or "")
    actor_dir = _actor_session_dir(work_dir, actor_name)
    voice = actor_voice(work_dir, _normalize_actor_name(actor_name))
    notes_status = actor_notes_status(work_dir, _normalize_actor_name(actor_name))
    notes_ready = notes_status == "present"
    evidence = _actor_evidence_summary(work_dir, actor_name)
    evidence_note = _actor_evidence_note(evidence)
    recent_activity = _actor_recent_activity(work_dir, actor_name)
    note_summary = _actor_note_summary(work_dir, actor_name)
    note_check_summary = _actor_note_check_summary(work_dir, actor_name)
    progress = _actor_progress_summary(work_dir, actor_name)
    lifecycle_entries = _lifecycle_entries(recent_activity.get("recent_lifecycle"))
    if (
        lifecycle_entries
        and str(lifecycle_entries[0].get("event") or "") == "runtime_exit"
    ):
        lifecycle_detail = str(lifecycle_entries[0].get("detail") or "")
        request_labels = {
            "runtime_stop_requested": "runtime stop signal",
            "failed_requested": "failure request",
            "signoff_requested": "sign-off request",
            "complete_requested": "completion request",
        }
        honored = next(
            (
                request_labels.get(str(item.get("event") or ""))
                for item in lifecycle_entries[1:]
                if str(item.get("event") or "") in request_labels
            ),
            "",
        )
        if honored and "code 0" in lifecycle_detail:
            lifecycle_entries[0]["summary"] = (
                f"runtime exit: actor process exited cleanly after honoring {honored}"
            )
    if lifecycle_entries:
        recent_activity["recent_lifecycle"] = lifecycle_entries
        recent_activity["last_lifecycle_at"] = str(
            lifecycle_entries[0].get("timestamp") or ""
        )
        recent_activity["last_lifecycle_summary"] = str(
            lifecycle_entries[0].get("summary") or ""
        )
    evidence_live = _int_value(evidence.get("artifact_count")) > 0
    if signoff_at:
        derived_status = "signed_off"
    runtime_issue_kind = str(state.get("runtime_issue_kind") or "").strip()
    runtime_issue_summary = str(state.get("runtime_issue_summary") or "").strip()
    runtime_issue_count = _int_value(state.get("runtime_issue_count"))
    runtime_stdout_at = str(state.get("runtime_stdout_at") or "").strip()
    runtime_stderr_at = str(state.get("runtime_stderr_at") or "").strip()
    runtime_stop_signal = str(state.get("runtime_stop_signal") or "").strip()
    runtime_stop_signal_at = str(state.get("runtime_stop_signal_at") or "").strip()
    runtime_note = ""
    if runtime_stop_signal_at and runtime_live:
        runtime_note = (
            f" Shutdown signal `{runtime_stop_signal or 'SIGTERM'}` was already sent at "
            f"{runtime_stop_signal_at}. Wait for runtime exit, then record the "
            "authoritative disposition explicitly."
        )
    if status in ACTOR_RUNNING_STATUS and runtime_live is False:
        if requested_status in {"completed", "failed", "signed_off"}:
            derived_status = requested_status
        else:
            derived_status = "awaiting_disposition"
        runtime_note = (
            " Actor runtime is no longer live, so this is awaiting an explicit "
            "completion, failure, or sign-off record."
        )
    if derived_status in {"starting", "active"} and evidence_live:
        derived_status = "producing_evidence"
    if requested_status and derived_status in {
        "starting",
        "active",
        "producing_evidence",
    }:
        derived_status = "closing"
    still_running = derived_status in ACTOR_RUNNING_STATUS and not heartbeat_stale
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
    voice_missing = voice == "missing"
    voice_setup = voice == "setup"
    voice_pulse = voice == "pulse"
    progress_stale = bool(progress.get("progress_stale"))
    progress_kind = str(progress.get("progress_kind") or "none").strip()
    last_note_at = str(note_summary.get("last_note_at") or "")
    last_artifact_at = str(evidence.get("last_artifact_at") or "")
    note_checks_since_update = _int_value(
        note_check_summary.get("note_checks_since_update")
    )
    needs_note_refresh = bool(
        evidence_live
        and last_artifact_at
        and (not last_note_at or last_artifact_at > last_note_at)
    )
    low_signal_progress = (
        bool(runtime_live)
        and progress_stale
        and _int_value(evidence.get("artifact_count")) == 0
    )
    stdout_age_seconds = _iso_age_seconds(runtime_stdout_at)
    stderr_age_seconds = _iso_age_seconds(runtime_stderr_at)
    stdout_quiet = (
        stdout_age_seconds is None or stdout_age_seconds >= ACTOR_STARTUP_GRACE_SECONDS
    )
    setup_only_live = (
        bool(runtime_live)
        and voice in {"missing", "setup"}
        and progress_kind == "none"
        and not evidence_live
    )
    runtime_broken = (
        setup_only_live
        and runtime_issue_kind == "upstream_retry_loop"
        and runtime_issue_count >= 2
        and stdout_quiet
        and stderr_age_seconds is not None
        and (started_age_seconds or 0) >= ACTOR_STARTUP_GRACE_SECONDS
    )
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
                    f"Stop the runtime with `gotta actor stop {_normalize_actor_name(actor_name)} --summary ...`."
                    + f" Pending `{requested_label}` disposition will remain authoritative when the runtime exits."
                    if requested_status
                    else f"Record `gotta actor fail {_normalize_actor_name(actor_name)} --summary ...` now, "
                    + f"then stop the runtime with `gotta actor stop {_normalize_actor_name(actor_name)} --summary ...`."
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
                f"then recheck `gotta actor status {_normalize_actor_name(actor_name)}` shortly."
                + request_note
                + runtime_note
            )
        elif notes_ready:
            next_step = (
                "actor is still active and producing evidence artifacts. "
                + (evidence_note + " " if evidence_note else "")
                + "Use `gotta notes` for live actor visibility; recheck `gotta actor status "
                f"{_normalize_actor_name(actor_name)}` shortly before closing the actor out."
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
                f"then recheck `gotta actor status {_normalize_actor_name(actor_name)}` shortly."
                + request_note
                + runtime_note
            )
        else:
            next_step = (
                "actor is still active and producing evidence artifacts, but actor voice is still "
                "missing. "
                + (evidence_note + " " if evidence_note else "")
                + "Append a short actor-authored note before requesting completion or sign-off, "
                f"then recheck `gotta actor status {_normalize_actor_name(actor_name)}` shortly."
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
            f"{_normalize_actor_name(actor_name)}` shortly before closing the actor out."
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
            f"`gotta actor settle {_normalize_actor_name(actor_name)}`"
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
                f"`gotta actor signoff {_normalize_actor_name(actor_name)} --summary ...`."
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
            f"`gotta actor signoff {_normalize_actor_name(actor_name)} --summary ...`."
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
            f"the narrative is complete with `gotta actor signoff {_normalize_actor_name(actor_name)} --summary ...`."
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
    return {
        **state,
        "label": _actor_label(actor_name, work_dir=work_dir),
        "status": derived_status,
        "state_path": str(_actor_state_path(work_dir, actor_name)),
        "events_path": str(_actor_events_path(work_dir, actor_name)),
        "actor_dir": str(actor_dir),
        "notes_status": notes_status,
        "notes_ready": notes_ready,
        "voice": voice,
        "evidence_live": bool(evidence_live),
        "evidence_note": evidence_note,
        "requested_status": requested_status,
        "requested_summary": requested_summary,
        "requested_label": requested_label,
        "requested_pending": bool(
            requested_status and derived_status not in ACTOR_TERMINAL_STATUS
        ),
        "still_running": still_running,
        "runtime_live": runtime_live,
        "runtime_issue_kind": runtime_issue_kind,
        "runtime_issue_summary": runtime_issue_summary,
        "runtime_issue_count": runtime_issue_count,
        "runtime_broken": runtime_broken,
        "runtime_stop_signal": runtime_stop_signal,
        "runtime_stop_signal_at": runtime_stop_signal_at,
        "review_ready": bool(
            derived_status in {"completed", "signed_off"}
            and (notes_ready or evidence_live)
        ),
        "next_step": next_step,
        **note_summary,
        **note_check_summary,
        **progress,
        **recent_activity,
        **evidence,
    }
