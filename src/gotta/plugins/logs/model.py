"""Typed payload shapes for `gotta logs`."""

from __future__ import annotations

from typing import TypedDict

from gotta.logs import LogRecord, LogsPayload


class SessionLogRecord(LogRecord, total=False):
    label: str


class SessionLogsPayload(TypedDict):
    session_root: str
    actor_count: int
    actors: list[str]
    entry_count: int
    entries: list[LogRecord]
    state_path: str
    locator: str
    follow_command: str


class AggregateLogsPayload(TypedDict):
    session_root: str
    actor_count: int
    actors: list[str]
    entry_count: int
    entries: list[SessionLogRecord]
    state_paths: dict[str, str]
    locators: dict[str, str]
    follow_commands: dict[str, str]


ActorLogsPayload = LogsPayload
