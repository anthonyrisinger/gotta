"""Typed payload shapes for actor note surfaces."""

from __future__ import annotations

from typing import TypedDict

from gotta.session.status.payload.model import ActorStatusPayload

from .file import ActorNoteRecord


class ActorNotesPayload(TypedDict):
    actor: str
    label: str
    state_path: str
    locator: str
    follow_command: str
    status: ActorStatusPayload
    entry_count: int
    entries: list[ActorNoteRecord]


class SessionNoteRecord(ActorNoteRecord, total=False):
    label: str


class SessionNotesPayload(TypedDict):
    session_root: str
    actor_count: int
    actors: dict[str, ActorNotesPayload]
    entry_count: int
    entries: list[SessionNoteRecord]
