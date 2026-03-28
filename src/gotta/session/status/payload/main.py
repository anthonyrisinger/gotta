"""Actor lifecycle status payload synthesis."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from gotta.session.registry import (
    _actor_events_path,
    _actor_label,
    _actor_state_path,
    _read_actor_state,
    _resolve_bound_actor_name,
)
from gotta.session.status.payload.activity import activity_payload
from gotta.session.status.payload.request import (
    apply_requested_closing,
    request_state_payload,
)
from gotta.session.status.payload.runtime import (
    runtime_signal_payload,
    runtime_state_payload,
)
from gotta.session.status.payload.step import actor_next_step


def _actor_status_payload(work_dir: Path, actor_name: str) -> dict[str, object]:
    actor_name = _resolve_bound_actor_name(work_dir, actor_name)
    state = _read_actor_state(work_dir, actor_name)
    status = str(state.get("status") or "pending")
    requested_status = str(state.get("requested_status") or "")
    derived_status = str(status)

    runtime = runtime_state_payload(
        state,
        status=status,
        requested_status=requested_status,
    )
    derived_status = str(runtime["derived_status"])

    activity = activity_payload(work_dir, actor_name)
    evidence_live = bool(activity["evidence_live"])
    if str(state.get("signoff_at") or ""):
        derived_status = "signed_off"
    if evidence_live and derived_status in {"starting", "active"}:
        derived_status = "producing_evidence"
    derived_status = apply_requested_closing(derived_status, requested_status)

    request = request_state_payload(
        state,
        actor_name=actor_name,
        derived_status=derived_status,
    )
    progress = cast(dict[str, object], activity["progress"])
    runtime_signal = runtime_signal_payload(
        runtime_live=cast(bool | None, runtime["runtime_live"]),
        started_age_seconds=cast(float | None, runtime["started_age_seconds"]),
        runtime_issue_kind=str(runtime["runtime_issue_kind"]),
        runtime_issue_count=cast(int, runtime["runtime_issue_count"]),
        runtime_stdout_at=str(runtime["runtime_stdout_at"]),
        runtime_stderr_at=str(runtime["runtime_stderr_at"]),
        voice=str(activity["voice"]),
        progress_kind=str(progress.get("progress_kind") or "none").strip(),
        progress_stale=bool(progress.get("progress_stale")),
        evidence_live=evidence_live,
    )
    note_summary = cast(dict[str, object], activity["note_summary"])
    note_check_summary = cast(dict[str, object], activity["note_check_summary"])
    evidence = cast(dict[str, object], activity["evidence"])
    recent_activity = cast(dict[str, object], activity["recent_activity"])

    draft = {
        **state,
        **request,
        **runtime,
        **runtime_signal,
        **note_summary,
        **note_check_summary,
        **progress,
        **recent_activity,
        **evidence,
        "status": derived_status,
        "notes_status": activity["notes_status"],
        "notes_ready": activity["notes_ready"],
        "voice": activity["voice"],
        "evidence_live": evidence_live,
        "evidence_note": activity["evidence_note"],
        "request_note": request["request_note"],
        "runtime_note": runtime["runtime_note"],
        "needs_note_refresh": bool(
            evidence_live
            and str(evidence.get("last_artifact_at") or "")
            and (
                not str(note_summary.get("last_note_at") or "")
                or str(evidence.get("last_artifact_at") or "")
                > str(note_summary.get("last_note_at") or "")
            )
        ),
    }
    next_step = actor_next_step(actor_name, draft)
    return {
        **state,
        "label": _actor_label(actor_name, work_dir=work_dir),
        "status": derived_status,
        "state_path": str(_actor_state_path(work_dir, actor_name)),
        "events_path": str(_actor_events_path(work_dir, actor_name)),
        "actor_dir": str(activity["actor_dir"]),
        "notes_status": activity["notes_status"],
        "notes_ready": activity["notes_ready"],
        "voice": activity["voice"],
        "evidence_live": evidence_live,
        "evidence_note": activity["evidence_note"],
        "requested_status": request["requested_status"],
        "requested_summary": request["requested_summary"],
        "requested_label": request["requested_label"],
        "requested_pending": request["requested_pending"],
        "still_running": bool(
            derived_status in {"starting", "active", "closing", "producing_evidence"}
            and not runtime["heartbeat_stale"]
        ),
        "runtime_live": runtime["runtime_live"],
        "runtime_issue_kind": runtime["runtime_issue_kind"],
        "runtime_issue_summary": runtime["runtime_issue_summary"],
        "runtime_issue_count": runtime["runtime_issue_count"],
        "runtime_broken": runtime_signal["runtime_broken"],
        "runtime_stop_signal": runtime["runtime_stop_signal"],
        "runtime_stop_signal_at": runtime["runtime_stop_signal_at"],
        "review_ready": bool(
            derived_status in {"completed", "signed_off"}
            and (bool(activity["notes_ready"]) or evidence_live)
        ),
        "next_step": next_step,
        **note_summary,
        **note_check_summary,
        **progress,
        **recent_activity,
        **evidence,
    }
