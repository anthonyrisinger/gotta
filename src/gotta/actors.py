"""Runtime actor context for the `gotta ...` operator frame."""

from __future__ import annotations

from dataclasses import dataclass
import os


ACTOR_SPEAKER_ENV = "GOTTA_ACTOR_SPEAKER"
ACTOR_CALLEE_ENV = "GOTTA_ACTOR_CALLEE"

PRIMARY_ACTOR = "primary"
USER_ACTOR = "user"


@dataclass(frozen=True, slots=True)
class ActorContext:
    speaker: str
    callee: str


def resolve_actor_context() -> ActorContext:
    speaker = str(os.environ.get(ACTOR_SPEAKER_ENV) or "").strip() or PRIMARY_ACTOR
    callee = str(os.environ.get(ACTOR_CALLEE_ENV) or "").strip() or USER_ACTOR
    return ActorContext(speaker=speaker, callee=callee)


def seed_primary_actor_context() -> None:
    os.environ.setdefault(ACTOR_SPEAKER_ENV, PRIMARY_ACTOR)
    os.environ.setdefault(ACTOR_CALLEE_ENV, USER_ACTOR)
