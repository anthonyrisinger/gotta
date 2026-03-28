"""Runtime-derived actor status helpers."""

from __future__ import annotations

import os

from gotta.session.registry import ACTOR_STALL_SECONDS
from gotta.session.status.payload.value import (
    ACTOR_RUNNING_STATUS,
    ACTOR_STARTUP_GRACE_SECONDS,
    int_value,
    iso_age_seconds,
)


def runtime_state_payload(
    state: dict[str, object],
    *,
    status: str,
    requested_status: str,
) -> dict[str, object]:
    heartbeat_at = str(state.get("heartbeat_at") or "")
    started_at = str(state.get("started_at") or "")
    derived_status = status
    heartbeat_stale = False
    runtime_live: bool | None = None
    pid = int_value(state.get("pid"))
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
    started_age_seconds = iso_age_seconds(started_at)
    if status in {"starting", "active"} and heartbeat_at:
        heartbeat_age_seconds = iso_age_seconds(heartbeat_at)
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
    runtime_issue_kind = str(state.get("runtime_issue_kind") or "").strip()
    runtime_issue_summary = str(state.get("runtime_issue_summary") or "").strip()
    runtime_issue_count = int_value(state.get("runtime_issue_count"))
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
    return {
        "derived_status": derived_status,
        "heartbeat_stale": heartbeat_stale,
        "runtime_live": runtime_live,
        "started_age_seconds": started_age_seconds,
        "runtime_issue_kind": runtime_issue_kind,
        "runtime_issue_summary": runtime_issue_summary,
        "runtime_issue_count": runtime_issue_count,
        "runtime_stdout_at": runtime_stdout_at,
        "runtime_stderr_at": runtime_stderr_at,
        "runtime_stop_signal": runtime_stop_signal,
        "runtime_stop_signal_at": runtime_stop_signal_at,
        "runtime_note": runtime_note,
    }


def runtime_signal_payload(
    *,
    runtime_live: bool | None,
    started_age_seconds: float | None,
    runtime_issue_kind: str,
    runtime_issue_count: int,
    runtime_stdout_at: str,
    runtime_stderr_at: str,
    voice: str,
    progress_kind: str,
    progress_stale: bool,
    evidence_live: bool,
) -> dict[str, object]:
    stdout_age_seconds = iso_age_seconds(runtime_stdout_at)
    stderr_age_seconds = iso_age_seconds(runtime_stderr_at)
    stdout_quiet = (
        stdout_age_seconds is None or stdout_age_seconds >= ACTOR_STARTUP_GRACE_SECONDS
    )
    stderr_retry_live = (
        stderr_age_seconds is not None
        and stderr_age_seconds <= ACTOR_STARTUP_GRACE_SECONDS
    )
    setup_only_live = (
        bool(runtime_live)
        and voice in {"missing", "setup"}
        and progress_kind == "none"
        and not evidence_live
    )
    return {
        "low_signal_progress": bool(runtime_live)
        and progress_stale
        and not evidence_live,
        "runtime_broken": (
            setup_only_live
            and runtime_issue_kind == "upstream_retry_loop"
            and runtime_issue_count >= 2
            and stdout_quiet
            and stderr_retry_live
            and (started_age_seconds or 0) >= ACTOR_STARTUP_GRACE_SECONDS
        ),
    }


def runtime_next_step(actor_name: str, payload: dict[str, object]) -> str:
    requested_status = str(payload.get("requested_status") or "")
    requested_label = str(payload.get("requested_label") or "")
    evidence_live = bool(payload.get("evidence_live"))
    evidence_note = str(payload.get("evidence_note") or "")
    request_note = str(payload.get("request_note") or "")
    runtime_broken = bool(payload.get("runtime_broken"))
    runtime_stop_signal_at = str(payload.get("runtime_stop_signal_at") or "")
    runtime_stop_signal = str(payload.get("runtime_stop_signal") or "SIGTERM")
    runtime_issue_summary = str(payload.get("runtime_issue_summary") or "")
    voice = str(payload.get("voice") or "missing")
    voice_missing = voice == "missing"

    if runtime_broken:
        issue_clause = (
            runtime_issue_summary + " "
            if runtime_issue_summary
            else "Upstream provider failures are keeping the runtime in a retry loop. "
        )
        if runtime_stop_signal_at:
            return (
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
        return (
            "actor runtime is broken and still has not produced actor-authored voice, "
            "progress, or evidence. "
            + issue_clause
            + "This is not normal warmup. "
            + (
                f"Stop the runtime with `gotta actor stop {actor_name} --summary ...`."
                + f" Pending `{requested_label}` disposition will remain authoritative when the runtime exits."
                if requested_status
                else f"Record `gotta actor fail {actor_name} --summary ...` now, "
                + f"then stop the runtime with `gotta actor stop {actor_name} --summary ...`."
            )
        )
    if str(payload.get("status") or "") == "stalled" and (
        not voice_missing or evidence_live
    ):
        return (
            "actor heartbeat is stale, but material actor state already exists in `gotta notes` or the "
            "shared evidence web. "
            + (evidence_note + " " if evidence_note else "")
            + "Inspect the notes and decide whether to wait, relaunch, or disposition manually."
            + request_note
        )
    return ""


def low_signal_next_step(payload: dict[str, object]) -> str:
    if not bool(payload.get("low_signal_progress")):
        return ""
    request_note = str(payload.get("request_note") or "")
    runtime_note = str(payload.get("runtime_note") or "")
    return (
        "actor runtime is still live, but actor-authored progress is stale and no "
        "actor-attributed evidence has landed yet. Treat this as a low-signal run until "
        "fresh actor-authored progress or evidence appears."
        + request_note
        + runtime_note
    )
