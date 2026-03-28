"""Activity, evidence, and note-state payload helpers."""

from __future__ import annotations

from pathlib import Path

from gotta.notes.voice import actor_notes_status, actor_voice
from gotta.session.activity.summary import (
    _actor_evidence_note,
    _actor_evidence_summary,
    _actor_note_check_summary,
    _actor_note_summary,
    _actor_recent_activity,
)
from gotta.session.registry import (
    _actor_session_dir,
    _normalize_actor_name,
)
from gotta.session.status.payload.value import (
    ACTOR_TERMINAL_STATUS,
    int_value,
    lifecycle_entries,
)
from gotta.session.status.progress import _actor_progress_summary


def _normalized_recent_activity(
    recent_activity: dict[str, object],
) -> dict[str, object]:
    payload = dict(recent_activity)
    entries = lifecycle_entries(payload.get("recent_lifecycle"))
    if entries and str(entries[0].get("event") or "") == "runtime_exit":
        lifecycle_detail = str(entries[0].get("detail") or "")
        request_labels = {
            "runtime_stop_requested": "runtime stop signal",
            "failed_requested": "failure request",
            "signoff_requested": "sign-off request",
            "complete_requested": "completion request",
        }
        honored = next(
            (
                request_labels.get(str(item.get("event") or ""))
                for item in entries[1:]
                if str(item.get("event") or "") in request_labels
            ),
            "",
        )
        if honored and "code 0" in lifecycle_detail:
            entries[0]["summary"] = (
                f"runtime exit: actor process exited cleanly after honoring {honored}"
            )
    if entries:
        payload["recent_lifecycle"] = entries
        payload["last_lifecycle_at"] = str(entries[0].get("timestamp") or "")
        payload["last_lifecycle_summary"] = str(entries[0].get("summary") or "")
    return payload


def activity_payload(work_dir: Path, actor_name: str) -> dict[str, object]:
    normalized_actor = _normalize_actor_name(actor_name)
    evidence = _actor_evidence_summary(work_dir, actor_name)
    notes_status = actor_notes_status(work_dir, normalized_actor)
    return {
        "actor_dir": _actor_session_dir(work_dir, actor_name),
        "voice": actor_voice(work_dir, normalized_actor),
        "notes_status": notes_status,
        "notes_ready": notes_status == "present",
        "evidence": evidence,
        "evidence_note": _actor_evidence_note(evidence),
        "evidence_live": int_value(evidence.get("artifact_count")) > 0,
        "recent_activity": _normalized_recent_activity(
            _actor_recent_activity(work_dir, actor_name)
        ),
        "note_summary": _actor_note_summary(work_dir, actor_name),
        "note_check_summary": _actor_note_check_summary(work_dir, actor_name),
        "progress": _actor_progress_summary(work_dir, actor_name),
    }


def note_check_next_step(
    *,
    note_checks_since_update: int,
    last_note_at: str,
    derived_status: str,
) -> str:
    if note_checks_since_update <= 0 or derived_status in ACTOR_TERMINAL_STATUS:
        return ""
    check_noun = "time" if note_checks_since_update == 1 else "times"
    if last_note_at:
        return (
            f"Supervisor has checked this actor's notes {note_checks_since_update} {check_noun} "
            "since the last note. Land one short note now if real progress exists."
        )
    return (
        f"Supervisor has checked this actor's notes {note_checks_since_update} {check_noun} "
        "and no first short note has landed yet. Land one short note now if real progress exists."
    )
