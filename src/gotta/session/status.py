"""Actor lifecycle, progress, and status synthesis helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time

from gotta.compat import datetime
from gotta.content import (
    sh_quote,
)
from gotta.friction import OOPS_CHANNEL, visible_channel_records
from gotta.notes import actor_notes_ready, actor_voice, visible_actor_notes_records
from gotta.actor import (
    requested_disposition_label,
)
from gotta.todo import (
    ensure_managed_todo_item,
    set_todo_checked,
    todo_items,
)

from .activity import (
    _actor_activity_summary,
    _actor_evidence_note,
    _actor_evidence_summary,
    _actor_log_line,
    _actor_note_check_summary,
    _actor_note_summary,
    _actor_recent_activity,
    _append_actor_event,
)
from .bootstrap import (
    _bootstrap_actor_goal,
    _bootstrap_actor_want,
    _bootstrap_want,
    _ensure_actor_surface,
)
from .registry import (
    ACTOR_STALL_SECONDS,
    WANT_FILE,
    _actor_charter_command,
    _actor_dir_path,
    _actor_events_path,
    _actor_goal_path,
    _actor_is_selected,
    _actor_label,
    _actor_session_dir,
    _actor_state_path,
    _actor_want_path,
    _bind_actor_identity,
    _normalize_actor_name,
    _read_actor_state,
    _resolve_bound_actor_name,
    _write_actor_state,
)
from .scope import _selected_actor_ids

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

FINAL_SIGNOFF_MARKER = "actors-final-signoff"


def _actor_todo_marker(actor_name: str, phase: str) -> str:
    return f"actor-{_normalize_actor_name(actor_name)}-{phase}"


def _actor_todo_redirect(actor_name: str, phase: str) -> str:
    actor = _normalize_actor_name(actor_name)
    label = _actor_label(actor)
    if phase == "initial":
        return (
            f"that TODO item is owned by {label} lifecycle; inspect with "
            f"`gotta actor status {actor}` and advance it through "
            f"`gotta actor complete {actor}` or `gotta actor signoff {actor} ...`"
        )
    if phase == "complete":
        return (
            f"that TODO item is owned by {label} lifecycle; use "
            f"`gotta actor complete {actor}` once the actor run has materially landed"
        )
    if phase == "dispositioned":
        return (
            f"that TODO item is owned by {label} disposition; use "
            f"`gotta actor signoff {actor} --summary ...` after review"
        )
    raise ValueError(f"unknown actor TODO phase: {phase}")


def _managed_todo_redirect(managed_key: str) -> str:
    if managed_key == FINAL_SIGNOFF_MARKER:
        return (
            "that TODO item is owned by final actor sign-off; inspect all actors with "
            "`gotta actor status` and sign off each actor through "
            "`gotta actor signoff ...`"
        )
    prefix = "actor-"
    suffixes = ("-initial", "-complete", "-dispositioned")
    if managed_key.startswith(prefix):
        for suffix in suffixes:
            if managed_key.endswith(suffix):
                actor = managed_key[len(prefix) : -len(suffix)]
                phase = suffix.removeprefix("-")
                return _actor_todo_redirect(actor, phase)
    return (
        "that TODO item is managed by native actor state; use "
        "`gotta actor ...` to advance it instead of "
        "`gotta todo check`"
    )


def _want_rewrite_pending(work_dir: Path) -> bool:
    want_path = work_dir / WANT_FILE
    if not want_path.is_file():
        return True
    current = want_path.read_text(encoding="utf-8").strip()
    return current == _bootstrap_want().strip()


def _goal_rewrite_pending(goal_path: Path) -> bool:
    if not goal_path.is_file():
        return True
    return (
        goal_path.read_text(encoding="utf-8")
        .strip()
        .startswith("# Seed Goal Placeholder")
    )


def _actor_want_rewrite_pending(work_root: Path, actor_name: str) -> bool:
    path = _actor_want_path(work_root, actor_name)
    if not path.is_file():
        return True
    current = path.read_text(encoding="utf-8").strip()
    return (
        current
        == _bootstrap_actor_want(
            actor_name=actor_name,
            label=_actor_label(actor_name, work_dir=work_root),
        ).strip()
    )


def _actor_goal_rewrite_pending(work_root: Path, actor_name: str) -> bool:
    path = _actor_goal_path(work_root, actor_name)
    if not path.is_file():
        return True
    return (
        path.read_text(encoding="utf-8").strip()
        == _bootstrap_actor_goal(
            actor_name=actor_name,
            label=_actor_label(actor_name, work_dir=work_root),
            actor_dir=_actor_session_dir(work_root, actor_name),
            work_dir=work_root,
        ).strip()
    )


def _actor_launch_blockers(work_root: Path, *, actor_name: str = "") -> list[str]:
    blockers: list[str] = []
    goal_path = work_root / "GOAL.md"
    if _want_rewrite_pending(work_root):
        blockers.append(f"rewrite `{work_root / WANT_FILE}` first")
    if _goal_rewrite_pending(goal_path):
        blockers.append(f"rewrite `{goal_path}` from the current moment before launch")
    if actor_name:
        actor_name = _resolve_bound_actor_name(work_root, actor_name)
        actor_want = _actor_dir_path(work_root, actor_name) / WANT_FILE
        actor_goal = _actor_dir_path(work_root, actor_name) / "GOAL.md"
        want_cmd = _actor_charter_command(actor_name, "want")
        goal_cmd = _actor_charter_command(actor_name, "goal")
        if _actor_want_rewrite_pending(work_root, actor_name):
            blockers.append(
                f"rewrite `{actor_want}` as the actor-local intent frame with `{want_cmd}` before launch"
            )
        if _actor_goal_rewrite_pending(work_root, actor_name):
            blockers.append(
                f"rewrite `{actor_goal}` as the actor-local goal with `{goal_cmd}` before launch"
            )
    return blockers


def _actor_progress_summary(
    work_dir: Path, actor_name: str, *, limit: int = 5
) -> dict[str, object]:
    normalized_actor = _normalize_actor_name(actor_name)
    actor_root = _actor_session_dir(work_dir, normalized_actor)
    events: list[dict[str, object]] = []
    order = 0

    def append_progress_event(
        *,
        timestamp: str,
        event: str,
        detail: str,
        summary: str,
        priority: int,
    ) -> None:
        nonlocal order
        cleaned_timestamp = timestamp.strip()
        cleaned_detail = detail.strip()
        if not cleaned_timestamp or not cleaned_detail:
            return
        events.append(
            {
                "timestamp": cleaned_timestamp,
                "event": event,
                "author": normalized_actor,
                "detail": cleaned_detail,
                "summary": summary.strip() or cleaned_detail,
                "_priority": priority,
                "_order": order,
            }
        )
        order += 1

    for record in visible_actor_notes_records(work_dir, normalized_actor):
        if str(record.get("author") or "").strip() != normalized_actor:
            continue
        message = str(record.get("message") or "").strip()
        append_progress_event(
            timestamp=str(record.get("timestamp") or ""),
            event="note",
            detail=message,
            summary=_actor_activity_summary(
                "note",
                message,
                author=normalized_actor,
                target_actor=normalized_actor,
            ),
            priority=4,
        )

    for record in visible_channel_records(actor_root, OOPS_CHANNEL):
        if str(record.get("actor") or "").strip() != normalized_actor:
            continue
        message = str(record.get("message") or "").strip()
        append_progress_event(
            timestamp=str(record.get("timestamp") or ""),
            event="oops",
            detail=message,
            summary=_actor_activity_summary(
                "oops",
                message,
                author=normalized_actor,
                target_actor=normalized_actor,
            ),
            priority=3,
        )

    manifest_path = work_dir / "content" / "manifest.jsonl"
    if manifest_path.exists():
        for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("actor") or "").strip() != normalized_actor:
                continue
            locator = str(
                payload.get("canonical_locator")
                or payload.get("locator")
                or payload.get("preferred_name")
                or ""
            ).strip()
            append_progress_event(
                timestamp=str(payload.get("fetched_at") or ""),
                event="evidence",
                detail=locator,
                summary=f"evidence: {locator}",
                priority=2,
            )

    ordered = sorted(
        events,
        key=lambda item: (
            str(item.get("timestamp") or ""),
            int(item.get("_priority") or 0),
            int(item.get("_order") or 0),
        ),
        reverse=True,
    )
    recent_progress = [
        {
            "timestamp": str(item.get("timestamp") or ""),
            "event": str(item.get("event") or ""),
            "author": str(item.get("author") or ""),
            "detail": str(item.get("detail") or ""),
            "summary": str(item.get("summary") or ""),
        }
        for item in ordered[:limit]
    ]
    latest = recent_progress[0] if recent_progress else {}
    progress_kind = (
        "evidence"
        if any(str(item.get("event") or "") == "evidence" for item in ordered)
        else "narration"
        if ordered
        else "none"
    )
    progress_stale = False
    latest_timestamp = str(latest.get("timestamp") or "")
    if latest_timestamp:
        try:
            latest_dt = datetime.fromisoformat(latest_timestamp.replace("Z", "+00:00"))
        except ValueError:
            latest_dt = None
        if latest_dt is not None:
            progress_stale = (time.time() - latest_dt.timestamp()) > ACTOR_STALL_SECONDS
    return {
        "recent_progress": recent_progress,
        "last_activity_at": str(latest.get("timestamp") or ""),
        "last_activity_summary": str(latest.get("summary") or ""),
        "progress_kind": progress_kind,
        "progress_stale": progress_stale,
    }


def _actor_status_payload(work_dir: Path, actor_name: str) -> dict[str, object]:
    actor_name = _resolve_bound_actor_name(work_dir, actor_name)
    state = _read_actor_state(work_dir, actor_name)
    status = str(state.get("status") or "pending")
    requested_status = str(state.get("requested_status") or "")
    requested_summary = str(state.get("requested_summary") or "")
    requested_label = requested_disposition_label(state)
    heartbeat_at = str(state.get("heartbeat_at") or "")
    derived_status = status
    heartbeat_stale = False
    runtime_live: bool | None = None
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0:
        try:
            os.kill(pid, 0)
        except OSError:
            runtime_live = False
        else:
            runtime_live = True
    if status in {"starting", "active"} and heartbeat_at:
        try:
            heartbeat_dt = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
        except ValueError:
            heartbeat_dt = None
        if heartbeat_dt is not None:
            age = time.time() - heartbeat_dt.timestamp()
            state["heartbeat_age_seconds"] = round(age, 1)
            if age > ACTOR_STALL_SECONDS:
                derived_status = "stalled"
                heartbeat_stale = True
    elif status in {"starting", "active"} and not heartbeat_at:
        started_at = str(state.get("started_at") or "")
        try:
            started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            started_dt = None
        if (
            started_dt is not None
            and (time.time() - started_dt.timestamp()) > ACTOR_STALL_SECONDS
        ):
            derived_status = "stalled"
            heartbeat_stale = True
    signoff_at = str(state.get("signoff_at") or "")
    actor_dir = _actor_session_dir(work_dir, actor_name)
    voice = actor_voice(work_dir, _normalize_actor_name(actor_name))
    notes_ready = voice == "present"
    evidence = _actor_evidence_summary(work_dir, actor_name)
    evidence_note = _actor_evidence_note(evidence)
    recent_activity = _actor_recent_activity(work_dir, actor_name)
    note_summary = _actor_note_summary(work_dir, actor_name)
    note_check_summary = _actor_note_check_summary(work_dir, actor_name)
    progress = _actor_progress_summary(work_dir, actor_name)
    lifecycle_entries = [
        dict(item) for item in recent_activity.get("recent_lifecycle", [])
    ]
    if (
        lifecycle_entries
        and str(lifecycle_entries[0].get("event") or "") == "runtime_exit"
    ):
        lifecycle_detail = str(lifecycle_entries[0].get("detail") or "")
        request_labels = {
            "stop_requested": "graceful stop request",
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
    evidence_live = int(evidence["artifact_count"]) > 0
    if signoff_at:
        derived_status = "signed_off"
    notes_status = (
        "present"
        if actor_notes_ready(work_dir, _normalize_actor_name(actor_name))
        else "empty"
    )
    runtime_note = ""
    if status in {"starting", "active"} and runtime_live is False:
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
        if requested_label == "stop":
            if derived_status == "stalled":
                request_note = (
                    " Operator already requested a graceful stop"
                    + (f" ({requested_summary})." if requested_summary else ".")
                    + " Because the heartbeat is stale, you can settle now with "
                    f"`gotta actor settle {_normalize_actor_name(actor_name)}`."
                )
            else:
                request_note = (
                    " Operator already requested a graceful stop"
                    + (f" ({requested_summary})." if requested_summary else ".")
                    + " The actor should wind down, append one final short note, and sign off."
                )
        elif derived_status == "stalled":
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
    last_note_at = str(note_summary.get("last_note_at") or "")
    last_artifact_at = str(evidence.get("last_artifact_at") or "")
    note_checks_since_update = int(
        note_check_summary.get("note_checks_since_update") or 0
    )
    needs_note_refresh = bool(
        evidence_live
        and last_artifact_at
        and (not last_note_at or last_artifact_at > last_note_at)
    )
    low_signal_progress = (
        bool(runtime_live)
        and progress_stale
        and int(evidence.get("artifact_count") or 0) == 0
    )
    if derived_status == "closing":
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


def _ensure_actor_todo_items(work_dir: Path, actor_name: str) -> None:
    actor = _normalize_actor_name(actor_name)
    label = _actor_label(actor, work_dir=work_dir)
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text=f"Initial actor pass collected from {label}",
        managed_key=_actor_todo_marker(actor, "initial"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text=f"{label} run materially complete",
        managed_key=_actor_todo_marker(actor, "complete"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text=f"{label} findings dispositioned",
        managed_key=_actor_todo_marker(actor, "dispositioned"),
    )
    ensure_managed_todo_item(
        work_dir,
        section="Actor Checklist",
        text="Final actor sign-off collected after edits for the chosen team",
        managed_key=FINAL_SIGNOFF_MARKER,
    )


def _sync_actor_todo_state(work_dir: Path) -> None:
    actor_ids = _selected_actor_ids(work_dir)
    actor_payloads = {
        actor: _actor_status_payload(work_dir, actor) for actor in actor_ids
    }
    launched_actor_ids = [
        actor
        for actor in actor_ids
        if str(actor_payloads[actor].get("status") or "pending")
        not in {"pending", "bound"}
    ]
    for actor_name in launched_actor_ids:
        _ensure_actor_todo_items(work_dir, actor_name)
    items_by_key = {
        str(item.get("managed_key") or ""): item
        for item in todo_items(work_dir)
        if item.get("managed_key")
    }
    for actor_name in launched_actor_ids:
        payload = actor_payloads[actor_name]
        materially_complete = bool(
            payload.get("notes_ready") or payload.get("evidence_live")
        )
        terminal = str(payload.get("status") or "") in {
            "completed",
            "failed",
            "rejected",
            "signed_off",
            "incomplete",
        }
        signed_off = str(payload.get("status") or "") == "signed_off"
        for marker, checked in (
            (_actor_todo_marker(actor_name, "initial"), materially_complete),
            (_actor_todo_marker(actor_name, "complete"), terminal),
            (_actor_todo_marker(actor_name, "dispositioned"), signed_off),
        ):
            item = items_by_key.get(marker)
            if item is None:
                continue
            updated = set_todo_checked(work_dir, str(item["id"]), checked=checked)
            if updated is not None:
                items_by_key[marker] = updated
    final_item = items_by_key.get(FINAL_SIGNOFF_MARKER)
    final_checked = bool(launched_actor_ids) and all(
        str(actor_payloads[actor].get("status") or "") == "signed_off"
        for actor in launched_actor_ids
    )
    if final_item is not None:
        set_todo_checked(work_dir, str(final_item["id"]), checked=final_checked)


def _actor_launch_command(work_dir: Path, actor_name: str) -> str:
    return f"gotta actor launch {actor_name} --session {sh_quote(str(work_dir))}"


def _render_actor_bind_message(payload: dict[str, object]) -> str:
    actor = str(payload.get("actor") or "").strip()
    label = str(payload.get("label") or actor).strip()
    actor_want = str(payload.get("wantPath") or "").strip()
    actor_goal = str(payload.get("goalPath") or "").strip()
    want_cmd = str(payload.get("wantCommand") or "").strip()
    goal_cmd = str(payload.get("goalCommand") or "").strip()
    todo_cmd = str(payload.get("todoCommand") or "").strip()
    launch_cmd = str(payload.get("launchCommand") or "").strip()
    actor_blockers = [
        str(item).strip()
        for item in (payload.get("blockers") or [])
        if str(item).strip()
    ]
    if actor_blockers:
        suffix = (
            f"; not launched. This bind completed the addressable actor target, so rewrite `{actor_want}` and `{actor_goal}` for {label} with `{want_cmd}` and `{goal_cmd}` first. "
            f"Use `{todo_cmd}` to extend the minimal actor-local checklist before launch if useful, "
            f"then launch with `{launch_cmd}` when you actually want {label} to start"
        )
    else:
        suffix = (
            f"; not launched. `{actor_want}` and `{actor_goal}` are already real. "
            f"Use `{todo_cmd}` to extend the minimal actor-local checklist before launch if useful, "
            f"then launch with `{launch_cmd}` when you actually want {label} to start"
        )
    if bool(payload.get("alreadyBound")):
        return f"{actor} ({label}) already bound{suffix}"
    created_note = " [new actor]" if bool(payload.get("created")) else ""
    return (
        f"bound {actor} ({label}) session, seeded actor-local WANT/GOAL placeholders, minimal actor-local canonical state, and shared evidence access{created_note}"
        f"{suffix}"
    )


def _bind_actor(session_root: Path, actor_name: str) -> dict[str, object]:
    actor, created = _bind_actor_identity(session_root, actor_name)
    already_bound = _actor_is_selected(session_root, actor)
    _ensure_actor_surface(session_root, actor)
    label = _actor_label(actor, work_dir=session_root)
    current_status = str(
        _read_actor_state(session_root, actor).get("status") or "pending"
    )
    if current_status in {
        "",
        "pending",
        "completed",
        "failed",
        "incomplete",
        "rejected",
        "signed_off",
    }:
        _write_actor_state(session_root, actor, {"status": "bound"})
    launch_cmd = _actor_launch_command(session_root, actor)
    actor_want = _actor_dir_path(session_root, actor) / WANT_FILE
    actor_goal = _actor_dir_path(session_root, actor) / "GOAL.md"
    want_cmd = _actor_charter_command(actor, "want")
    goal_cmd = _actor_charter_command(actor, "goal")
    actor_blockers = _actor_launch_blockers(session_root, actor_name=actor)
    todo_cmd = f"gotta todo --actor {actor}"
    if already_bound:
        status = "already_bound"
    else:
        _append_actor_event(
            session_root, actor, event="bound", detail="bound actor session"
        )
        _actor_log_line(session_root, actor, "bound session")
        status = "bound"
    payload: dict[str, object] = {
        "actor": actor,
        "label": label,
        "created": created,
        "alreadyBound": already_bound,
        "status": status,
        "sessionRoot": str(session_root.resolve()),
        "actorRoot": str(_actor_dir_path(session_root, actor).resolve()),
        "wantPath": str(actor_want.resolve()),
        "goalPath": str(actor_goal.resolve()),
        "wantCommand": want_cmd,
        "goalCommand": goal_cmd,
        "todoCommand": todo_cmd,
        "launchCommand": launch_cmd,
        "blockers": actor_blockers,
    }
    payload["message"] = _render_actor_bind_message(payload)
    return payload
