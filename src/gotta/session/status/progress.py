"""Actor progress synthesis helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time

from gotta.compat import datetime
from gotta.friction import OOPS_CHANNEL, visible_channel_records
from gotta.notes.file import visible_actor_notes_records
from gotta.session.activity.summary import _actor_activity_summary
from gotta.session.registry import (
    ACTOR_STALL_SECONDS,
    _actor_session_dir,
    _normalize_actor_name,
)
from gotta.session.status.payload.model import LifecycleEntry, ProgressSummary


def _rank_value(value: object) -> int:
    try:
        return int(str(value or 0))
    except ValueError:
        return 0


@dataclass(frozen=True, slots=True)
class _ProgressEvent:
    timestamp: str
    event: str
    author: str
    detail: str
    summary: str
    priority: int
    order: int

    def rank_key(self) -> tuple[str, int, int]:
        return (
            self.timestamp,
            self.priority,
            self.order,
        )

    def lifecycle_entry(self) -> LifecycleEntry:
        return {
            "timestamp": self.timestamp,
            "event": self.event,
            "author": self.author,
            "detail": self.detail,
            "summary": self.summary,
        }


@dataclass(slots=True)
class _ActorProgressState:
    work_dir: Path
    actor_name: str
    actor_root: Path
    events: list[_ProgressEvent] = field(default_factory=list)

    @classmethod
    def for_actor(cls, work_dir: Path, actor_name: str) -> _ActorProgressState:
        normalized_actor = _normalize_actor_name(actor_name)
        return cls(
            work_dir=work_dir,
            actor_name=normalized_actor,
            actor_root=_actor_session_dir(work_dir, normalized_actor),
        )

    def append_event(
        self,
        *,
        timestamp: str,
        event: str,
        detail: str,
        summary: str,
        priority: int,
    ) -> None:
        cleaned_timestamp = timestamp.strip()
        cleaned_detail = detail.strip()
        if not cleaned_timestamp or not cleaned_detail:
            return
        self.events.append(
            _ProgressEvent(
                timestamp=cleaned_timestamp,
                event=event,
                author=self.actor_name,
                detail=cleaned_detail,
                summary=summary.strip() or cleaned_detail,
                priority=priority,
                order=len(self.events),
            )
        )

    def collect_notes(self) -> None:
        for record in visible_actor_notes_records(self.work_dir, self.actor_name):
            if str(record.get("author") or "").strip() != self.actor_name:
                continue
            message = str(record.get("message") or "").strip()
            self.append_event(
                timestamp=str(record.get("timestamp") or ""),
                event="note",
                detail=message,
                summary=_actor_activity_summary(
                    "note",
                    message,
                    author=self.actor_name,
                    target_actor=self.actor_name,
                ),
                priority=4,
            )

    def collect_oops(self) -> None:
        for record in visible_channel_records(self.actor_root, OOPS_CHANNEL):
            if str(record.get("actor") or "").strip() != self.actor_name:
                continue
            message = str(record.get("message") or "").strip()
            self.append_event(
                timestamp=str(record.get("timestamp") or ""),
                event="oops",
                detail=message,
                summary=_actor_activity_summary(
                    "oops",
                    message,
                    author=self.actor_name,
                    target_actor=self.actor_name,
                ),
                priority=3,
            )

    def collect_evidence(self) -> None:
        manifest_path = self.work_dir / "content" / "manifest.jsonl"
        if not manifest_path.exists():
            return
        for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("actor") or "").strip() != self.actor_name:
                continue
            locator = str(
                payload.get("canonical_locator")
                or payload.get("locator")
                or payload.get("preferred_name")
                or ""
            ).strip()
            self.append_event(
                timestamp=str(payload.get("fetched_at") or ""),
                event="evidence",
                detail=locator,
                summary=f"evidence: {locator}",
                priority=2,
            )

    def ordered_events(self) -> list[_ProgressEvent]:
        return sorted(self.events, key=lambda item: item.rank_key(), reverse=True)

    def render(self, *, limit: int) -> ProgressSummary:
        ordered = self.ordered_events()
        recent_progress = [item.lifecycle_entry() for item in ordered[:limit]]
        latest = recent_progress[0] if recent_progress else {}
        progress_kind = (
            "evidence"
            if any(item.event == "evidence" for item in ordered)
            else "narration"
            if ordered
            else "none"
        )
        progress_stale = False
        latest_timestamp = str(latest.get("timestamp") or "")
        if latest_timestamp:
            try:
                latest_dt = datetime.fromisoformat(
                    latest_timestamp.replace("Z", "+00:00")
                )
            except ValueError:
                latest_dt = None
            if latest_dt is not None:
                progress_stale = (
                    time.time() - latest_dt.timestamp()
                ) > ACTOR_STALL_SECONDS
        return {
            "recent_progress": recent_progress,
            "last_activity_at": str(latest.get("timestamp") or ""),
            "last_activity_summary": str(latest.get("summary") or ""),
            "progress_kind": progress_kind,
            "progress_stale": progress_stale,
        }


def _actor_progress_summary(
    work_dir: Path, actor_name: str, *, limit: int = 5
) -> ProgressSummary:
    state = _ActorProgressState.for_actor(work_dir, actor_name)
    state.collect_notes()
    state.collect_oops()
    state.collect_evidence()
    return state.render(limit=limit)
