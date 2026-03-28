"""Root session surfaces for `gotta session`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gotta.compat import UTC, datetime
from gotta import binding as binding_helpers
from gotta import topology
from gotta.content.context import current_context_binding
from gotta.content.env import (
    CONTENT_ENV,
    SESSION_ACTIVATION_ENV,
    SESSION_CREATED_ENV,
    SESSION_ENV,
    env_mapping,
    load_state_env_at_root,
    write_session_state,
)
from gotta.content.path import sh_quote
from gotta.content.scope import (
    resolve_dirs,
    session_identity,
    session_is_initialized,
    session_shared_id,
    shared_session_root,
)
from gotta.session import bootstrap as session_bootstrap
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope

from .parse import (
    options_from_args,
    require_started_session,
    session_dirs_for_read,
    session_scope_started,
)


def _print_dirs(args: argparse.Namespace, payload: dict[str, str]) -> int:
    print_format = getattr(args, "print_format", "path")
    if print_format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if print_format == "path":
        print(payload["GOTTA_SESSION_DIR"])
        return 0
    if print_format == "env":
        for key, value in payload.items():
            print(f"{key}={value}")
        return 0
    lines = [f"export {key}={sh_quote(value)}" for key, value in payload.items()]
    print("\n".join(lines))
    return 0


def _binding_detail(record: dict[str, object]) -> str:
    context_id = str(record.get("contextId") or "").strip() or "unknown"
    context_source = str(record.get("contextSource") or "").strip() or "unknown"
    session_root = str(record.get("sessionRoot") or "").strip() or "unknown"
    return (
        f"{record.get('bindingId') or 'unknown'} -> {session_root} "
        f"({context_source}, {context_id})"
    )


def show_payload(dirs) -> dict[str, str]:
    resolved = dirs.session_dir.resolve()
    if resolved.parent.name == "actors":
        actor_root = resolved
        session_root = session_registry._group_session_root(actor_root)
        primary_actor = session_identity(actor_root)
        state_dir = str(actor_root / "state")
    elif topology.parse_shared_session_root(resolved) is None:
        actor_root = resolved
        session_root = session_registry._group_session_root(resolved)
        primary_actor = ""
        state_dir = str(actor_root / "state")
    else:
        session_root = session_registry._group_session_root(resolved)
        primary_actor = session_scope._primary_actor_name(session_root) or ""
        actor_root = (
            session_registry._actor_session_dir(session_root, primary_actor)
            if primary_actor
            else session_root
        )
        state_dir = str(actor_root / "state") if primary_actor else ""
    return {
        SESSION_ENV: str(actor_root),
        "GOTTA_SESSION_ID": session_shared_id(session_root),
        CONTENT_ENV: str(dirs.content_dir),
        "GOTTA_SESSION_STATE_DIR": state_dir,
        "GOTTA_SESSION_ACTOR": primary_actor,
    }


def doctor_payload(dirs) -> dict[str, object]:
    session_env = show_payload(dirs)
    state = load_state_env_at_root(dirs.session_dir)
    runtime = current_context_binding()
    runtime_root = topology.resolve_binding(runtime.binding_id)
    bindings = topology.binding_records_for_session(dirs.session_dir)
    target_session_id = str(session_env.get("GOTTA_SESSION_ID") or "")
    target_shared_root = shared_session_root(target_session_id).resolve()
    session_payload = {
        "sessionId": target_session_id,
        "actor": str(session_env.get("GOTTA_SESSION_ACTOR") or ""),
        "sessionRoot": str(dirs.session_dir),
        "contentRoot": str(dirs.content_dir),
        "initialized": bool(session_is_initialized(dirs.session_dir)),
        "repo": state.get("GOTTA_SESSION_REPO", ""),
        "createdAt": state.get(SESSION_CREATED_ENV, ""),
    }
    runtime_payload = {
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
    checks = {
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


def _print_doctor_summary(payload: dict[str, object]) -> int:
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


def cmd_show(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    return _print_dirs(args, show_payload(dirs))


def cmd_bind(args: argparse.Namespace) -> int:
    return binding_helpers.bind_current_context(
        session_ref=getattr(args, "session_id", None),
        output=getattr(args, "output", "summary"),
    )


def cmd_init(args: argparse.Namespace) -> int:
    dirs = resolve_dirs(options_from_args(args), create=True)
    current = dirs.session_dir.resolve()
    write_session_state(
        dirs,
        {
            SESSION_CREATED_ENV: load_state_env_at_root(current).get(
                SESSION_CREATED_ENV, ""
            )
            or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            SESSION_ACTIVATION_ENV: "manual",
        },
    )
    session_bootstrap.scaffold_session(current)
    return _print_dirs(args, env_mapping(dirs))


def cmd_doctor(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    payload = doctor_payload(dirs)
    if getattr(args, "print_format", "json") == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    return _print_doctor_summary(payload)
