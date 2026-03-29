"""Typed payload shapes for actor status synthesis."""

from __future__ import annotations

from typing import TypedDict


ActorVoice = str
ActorNotesStatus = str
ActorProgressKind = str


class LifecycleEntry(TypedDict):
    timestamp: str
    event: str
    author: str
    detail: str
    summary: str


class EvidenceArtifact(TypedDict):
    locator: str
    preferred_name: str
    fetched_at: str


class EvidenceSummary(TypedDict):
    artifact_count: int
    last_artifact_at: str
    recent_artifacts: list[EvidenceArtifact]


class RecentActivityPayload(TypedDict):
    recent_activity: list[LifecycleEntry]
    recent_lifecycle: list[LifecycleEntry]
    last_lifecycle_at: str
    last_lifecycle_summary: str


class NoteSummary(TypedDict):
    last_note_at: str
    last_note_summary: str
    notes_stale: bool


class NoteCheckSummary(TypedDict):
    note_checks_since_update: int
    last_note_check_at: str
    last_note_check_by: str


class ProgressSummary(TypedDict):
    recent_progress: list[LifecycleEntry]
    last_activity_at: str
    last_activity_summary: str
    progress_kind: ActorProgressKind
    progress_stale: bool


class RequestStatePayload(TypedDict):
    requested_status: str
    requested_summary: str
    requested_label: str
    requested_pending: bool
    request_note: str


class RuntimeStatePayload(TypedDict):
    derived_status: str
    heartbeat_stale: bool
    runtime_live: bool | None
    started_age_seconds: float | None
    runtime_issue_kind: str
    runtime_issue_summary: str
    runtime_issue_count: int
    runtime_stdout_at: str
    runtime_stderr_at: str
    runtime_stop_signal: str
    runtime_stop_signal_at: str
    runtime_note: str


class RuntimeSignalPayload(TypedDict):
    low_signal_progress: bool
    runtime_broken: bool


class ActorActivityPayload(TypedDict):
    actor_dir: str
    voice: ActorVoice
    notes_status: ActorNotesStatus
    notes_ready: bool
    evidence: EvidenceSummary
    evidence_note: str
    evidence_live: bool
    recent_activity: RecentActivityPayload
    note_summary: NoteSummary
    note_check_summary: NoteCheckSummary
    progress: ProgressSummary


class ActorStatusPayload(TypedDict, total=False):
    actor: str
    label: str
    status: str
    derived_status: str
    summary: str
    signoff_summary: str
    state_path: str
    events_path: str
    actor_dir: str
    notes_status: ActorNotesStatus
    notes_ready: bool
    voice: ActorVoice
    evidence_live: bool
    evidence_note: str
    artifact_count: int
    last_artifact_at: str
    recent_artifacts: list[EvidenceArtifact]
    requested_status: str
    requested_summary: str
    requested_label: str
    requested_pending: bool
    request_note: str
    still_running: bool
    runtime_live: bool | None
    runtime_issue_kind: str
    runtime_issue_summary: str
    runtime_issue_count: int
    runtime_broken: bool
    runtime_stop_signal: str
    runtime_stop_signal_at: str
    runtime_note: str
    review_ready: bool
    next_step: str
    last_note_at: str
    last_note_summary: str
    notes_stale: bool
    note_checks_since_update: int
    last_note_check_at: str
    last_note_check_by: str
    recent_progress: list[LifecycleEntry]
    last_activity_at: str
    last_activity_summary: str
    progress_kind: ActorProgressKind
    progress_stale: bool
    recent_activity: list[LifecycleEntry]
    recent_lifecycle: list[LifecycleEntry]
    last_lifecycle_at: str
    last_lifecycle_summary: str
    needs_note_refresh: bool
    heartbeat_stale: bool
    started_age_seconds: float | None
    runtime_stdout_at: str
    runtime_stderr_at: str
    low_signal_progress: bool
