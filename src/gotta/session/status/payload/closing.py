"""Next-step synthesis for closing and terminal actor states."""

from __future__ import annotations

from dataclasses import dataclass

from .model import ActorStatusPayload


@dataclass(frozen=True, slots=True)
class _ClosingState:
    actor_name: str
    derived_status: str
    requested_status: str
    requested_label: str
    notes_ready: bool
    evidence_live: bool
    evidence_note: str
    request_note: str
    runtime_note: str
    voice: str
    needs_note_refresh: bool

    @classmethod
    def from_payload(
        cls,
        actor_name: str,
        payload: ActorStatusPayload,
    ) -> _ClosingState:
        return cls(
            actor_name=actor_name,
            derived_status=str(payload.get("status") or ""),
            requested_status=str(payload.get("requested_status") or ""),
            requested_label=str(payload.get("requested_label") or ""),
            notes_ready=bool(payload.get("notes_ready")),
            evidence_live=bool(payload.get("evidence_live")),
            evidence_note=str(payload.get("evidence_note") or ""),
            request_note=str(payload.get("request_note") or ""),
            runtime_note=str(payload.get("runtime_note") or ""),
            voice=str(payload.get("voice") or "missing"),
            needs_note_refresh=bool(payload.get("needs_note_refresh")),
        )

    def next_step(self) -> str:
        if self.derived_status == "closing":
            return self._closing_step()
        if self.derived_status == "awaiting_disposition":
            return self._awaiting_disposition_step()
        if self.derived_status == "completed" and (
            self.notes_ready or self.evidence_live
        ):
            return self._completed_step()
        if self.derived_status == "incomplete":
            return (
                "actor finished without material notes or evidence. Decide whether to relaunch, "
                "fail, or sign off intentionally."
            )
        if self.derived_status == "failed" and self.evidence_note:
            return (
                "actor was manually marked failed, but evidence already landed in shared state. "
                + self.evidence_note
                + " Keep or reject that evidence intentionally instead of assuming it vanished."
            )
        if self.derived_status == "stalled" and (
            not self.voice_missing or self.evidence_live
        ):
            return (
                "actor heartbeat is stale, but material actor state already exists in `gotta notes` "
                "or the shared evidence web. "
                + self.evidence_prefix
                + "Inspect the notes and decide whether to wait, relaunch, or disposition manually."
                + self.request_note
            )
        return ""

    @property
    def evidence_prefix(self) -> str:
        return f"{self.evidence_note} " if self.evidence_note else ""

    @property
    def voice_pulse(self) -> bool:
        return self.voice == "pulse"

    @property
    def voice_setup(self) -> bool:
        return self.voice == "setup"

    @property
    def voice_missing(self) -> bool:
        return self.voice == "missing"

    def _closing_step(self) -> str:
        if self.notes_ready and self.needs_note_refresh:
            return (
                "actor close-out is pending while the runtime is still live, and new "
                "actor-attributed evidence landed after the last short note. "
                + self.evidence_prefix
                + "Land one final short note now so the close-out reflects the latest evidence, "
                "then wait for runtime exit before treating the terminal disposition as authoritative."
                + self.request_note
                + self.runtime_note
            )
        if self.notes_ready:
            return (
                "actor close-out is pending while the runtime is still live. "
                + self.evidence_prefix
                + "Let the actor finish the current wave, then wait for runtime exit before "
                "treating the terminal disposition as authoritative."
                + self.request_note
                + self.runtime_note
            )
        if self.voice_pulse:
            missing_voice = "has durable voice"
            missing_voice_prefix = (
                "actor close-out is pending while the runtime is still live. Non-note signal is "
                "already landing through friction or shared evidence, but the final short actor "
                "note is still missing. "
            )
        elif self.voice_setup:
            missing_voice = "has real voice"
            missing_voice_prefix = (
                "actor close-out is pending while the runtime is still live. Setup note is present, "
                "but actor voice is still missing. "
            )
        else:
            missing_voice = "has voice"
            missing_voice_prefix = (
                "actor close-out is pending while the runtime is still live, but actor voice is "
                "still missing. "
            )
        return (
            missing_voice_prefix
            + self.evidence_prefix
            + "Land one final short actor-authored note before runtime exit so the close-out "
            + f"{missing_voice}, then recheck actor status."
            + self.request_note
            + self.runtime_note
        )

    def _awaiting_disposition_step(self) -> str:
        settle_detail = (
            f" to honor the pending `{self.requested_label}` request."
            if self.requested_status
            else " to record the authoritative terminal disposition."
        )
        return (
            "actor runtime is no longer running, but no durable terminal lifecycle was recorded yet. "
            + self.evidence_prefix
            + "Inspect `gotta notes` plus the shared evidence web, then settle with "
            f"`gotta actor settle {self.actor_name}`"
            + settle_detail
            + self.request_note
        )

    def _completed_step(self) -> str:
        if self.notes_ready and self.needs_note_refresh:
            return (
                "actor run is complete, but new actor-attributed evidence landed after the last "
                "short note. Land one short note now, then record durable sign-off intentionally."
            )
        if self.notes_ready:
            return (
                "actor run is complete; inspect `gotta notes` plus the shared evidence web, then record "
                "durable sign-off with "
                f"`gotta actor signoff {self.actor_name} --summary ...`."
            )
        if self.voice_pulse:
            return (
                "actor run is complete and non-note signal landed through friction or shared "
                "evidence, but the final short note is still missing. Add one short actor-authored "
                "note now, then sign off intentionally."
            )
        return (
            "actor run is complete and evidence landed, but actor voice is still missing. Wait "
            "for a short actor-authored note or sign off intentionally only if you are "
            "explicitly accepting an evidence-only actor contribution."
        )


def closing_next_step(actor_name: str, payload: ActorStatusPayload) -> str:
    return _ClosingState.from_payload(actor_name, payload).next_step()
