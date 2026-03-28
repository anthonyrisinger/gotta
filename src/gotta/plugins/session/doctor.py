"""Doctor surface for `gotta session`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypedDict

from gotta.content.context import current_context_binding
from gotta.content.env import SESSION_CREATED_ENV, load_state_env_at_root
from gotta.content.model import ResolvedDirs
from gotta.content.scope import session_is_initialized, shared_session_root
from gotta import topology

from .parse import require_started_session, session_dirs_for_read, session_scope_started
from .show import show_payload


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
    bindings: list[dict[str, object]]
    checks: DoctorChecks


def _binding_detail(record: dict[str, object]) -> str:
    context_id = str(record.get("contextId") or "").strip() or "unknown"
    context_source = str(record.get("contextSource") or "").strip() or "unknown"
    session_root = str(record.get("sessionRoot") or "").strip() or "unknown"
    return (
        f"{record.get('bindingId') or 'unknown'} -> {session_root} "
        f"({context_source}, {context_id})"
    )


def doctor_payload(dirs: ResolvedDirs) -> DoctorPayload:
    session_env = show_payload(dirs)
    state = load_state_env_at_root(dirs.session_dir)
    runtime = current_context_binding()
    runtime_root = topology.resolve_binding(runtime.binding_id)
    bindings = topology.binding_records_for_session(dirs.session_dir)
    target_session_id = str(session_env.get("GOTTA_SESSION_ID") or "")
    target_shared_root = shared_session_root(target_session_id).resolve()
    session_payload: DoctorSessionPayload = {
        "sessionId": target_session_id,
        "actor": str(session_env.get("GOTTA_SESSION_ACTOR") or ""),
        "sessionRoot": str(dirs.session_dir),
        "contentRoot": str(dirs.content_dir),
        "initialized": bool(session_is_initialized(dirs.session_dir)),
        "repo": str(state.get("GOTTA_SESSION_REPO") or ""),
        "createdAt": str(state.get(SESSION_CREATED_ENV) or ""),
    }
    runtime_payload: DoctorRuntimePayload = {
        "present": bool(runtime.context_id),
        "contextId": runtime.context_id,
        "contextSource": runtime.context_source,
        "bindingId": runtime.binding_id,
    }
    session_in_shared_topology = (
        topology.parse_grouped_session_root(dirs.session_dir.resolve()) is not None
        or topology.parse_shared_session_root(dirs.session_dir.resolve()) is not None
    )
    matching_runtime_binding = (
        runtime_root is not None
        and topology.shared_session_id(runtime_root) == target_session_id
    )
    bindings_match_target = all(
        topology.normalize_session_id(str(record.get("sessionId") or ""))
        == target_session_id
        and topology.shared_session_id(
            Path(str(record.get("sessionRoot") or target_shared_root))
        )
        == target_session_id
        for record in bindings
    )
    topology_consistent = (
        dirs.session_dir.exists()
        and dirs.content_dir.exists()
        and session_scope_started(dirs)
        and topology.shared_session_id(dirs.session_dir) == target_session_id
        and (
            not session_in_shared_topology
            or dirs.content_dir.resolve() == (target_shared_root / "content").resolve()
        )
        and bool(bindings)
        and bindings_match_target
    )
    checks: DoctorChecks = {
        "runtimeContextPresent": {
            "status": "ok" if runtime_payload["present"] else "missing",
            "detail": (
                f"{runtime.context_source}: {runtime.context_id}"
                if runtime_payload["present"]
                else "no active runtime context"
            ),
        },
        "durableBindingsPresent": {
            "status": "ok" if bindings else "missing",
            "detail": (
                ", ".join(_binding_detail(record) for record in bindings)
                if bindings
                else "no durable bindings target this session"
            ),
        },
        "runtimeBindingMatchesTarget": {
            "status": (
                "ok"
                if matching_runtime_binding
                else "mismatch"
                if runtime_payload["present"]
                else "unknown"
            ),
            "detail": (
                f"{runtime.binding_id} targets session "
                f"{topology.shared_session_id(runtime_root)} at {runtime_root}"
                if runtime_root is not None
                else "the active runtime binding has no durable target"
            )
            if runtime_payload["present"]
            else "no active runtime context",
        },
        "sessionTopologyConsistent": {
            "status": "ok" if topology_consistent else "broken",
            "detail": (
                "session root, content root, and binding records agree"
                if topology_consistent
                else "session root, content root, and durable binding records do not fully agree"
            ),
        },
    }
    return {
        "runtime": runtime_payload,
        "session": session_payload,
        "bindings": bindings,
        "checks": checks,
    }


def _print_doctor_summary(payload: DoctorPayload) -> int:
    runtime = payload["runtime"]
    session_payload = payload["session"]
    print(f"session: {session_payload['sessionRoot']}")
    print(f"session_id: {session_payload['sessionId']}")
    print(f"actor: {session_payload['actor']}")
    print(f"content: {session_payload['contentRoot']}")
    print(
        "runtime: "
        f"{runtime['contextSource'] or 'unknown'} "
        f"{runtime['contextId'] or 'unknown'} "
        f"({runtime['bindingId'] or 'unknown'})"
    )
    print(f"bindings: {len(payload['bindings'])}")
    for name in (
        "runtimeContextPresent",
        "durableBindingsPresent",
        "runtimeBindingMatchesTarget",
        "sessionTopologyConsistent",
    ):
        check = payload["checks"][name]
        print(f"- {name}: {check['status']} - {check['detail']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    payload = doctor_payload(dirs)
    if getattr(args, "print_format", "json") == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    return _print_doctor_summary(payload)
