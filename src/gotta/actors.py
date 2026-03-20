"""Runtime actor context for the `gotta ...` operator frame."""

from __future__ import annotations

from dataclasses import dataclass
import os


ACTOR_SPEAKER_ENV = "GOTTA_ACTOR_SPEAKER"
ACTOR_CALLEE_ENV = "GOTTA_ACTOR_CALLEE"

USER_ACTOR = "user"


@dataclass(frozen=True, slots=True)
class ActorContext:
    speaker: str
    callee: str


def resolve_actor_context(*, default_speaker: str = "") -> ActorContext:
    speaker = str(os.environ.get(ACTOR_SPEAKER_ENV) or "").strip() or default_speaker.strip()
    callee = str(os.environ.get(ACTOR_CALLEE_ENV) or "").strip() or USER_ACTOR
    return ActorContext(speaker=speaker, callee=callee)


def seed_actor_context(actor: str, *, callee: str = USER_ACTOR) -> None:
    if actor.strip():
        os.environ.setdefault(ACTOR_SPEAKER_ENV, actor.strip())
    os.environ.setdefault(ACTOR_CALLEE_ENV, callee)
