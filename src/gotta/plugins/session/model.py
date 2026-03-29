"""Typed payload shapes for top-level `gotta session` surfaces."""

from __future__ import annotations

from typing import TypedDict

from gotta.topology import BindingRecord


class SessionEnvPayload(TypedDict):
    GOTTA_SESSION_DIR: str
    GOTTA_SESSION_ID: str
    GOTTA_SESSION_CONTENT_DIR: str
    GOTTA_SESSION_STATE_DIR: str
    GOTTA_SESSION_ACTOR: str


class DoctorRuntimePayload(TypedDict):
    present: bool
    contextId: str
    contextSource: str
    bindingId: str


class DoctorSessionPayload(TypedDict):
    sessionId: str
    actor: str
    sessionRoot: str
    contentRoot: str
    initialized: bool
    repo: str
    createdAt: str


class DoctorCheck(TypedDict):
    status: str
    detail: str


class DoctorChecks(TypedDict):
    runtimeContextPresent: DoctorCheck
    durableBindingsPresent: DoctorCheck
    runtimeBindingMatchesTarget: DoctorCheck
    sessionTopologyConsistent: DoctorCheck


class DoctorPayload(TypedDict):
    runtime: DoctorRuntimePayload
    session: DoctorSessionPayload
    bindings: list[BindingRecord]
    checks: DoctorChecks
