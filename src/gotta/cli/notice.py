#!/usr/bin/env python3
"""CLI notices and operator-facing receipts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from gotta.actor import (
    session_actor,
    supervisor_note_check_message,
    supervisor_stop_message,
    supervisor_stop_pending,
)
from gotta import topology
from gotta.content.path import sh_quote
from gotta.session.status.payload.main import _actor_status_payload


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def _silence_stdout() -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(devnull, sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        pass
    finally:
        os.close(devnull)


def _actor_stop_warning(root: Path) -> str:
    actor_name = session_actor(root)
    if not actor_name:
        return ""
    path = root / "state" / "actor.json"
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    if not supervisor_stop_pending(payload):
        return ""
    return supervisor_stop_message(
        actor_name,
        status_payload=payload,
    )


def _actor_note_check_warning(root: Path) -> str:
    actor_name = session_actor(root)
    if not actor_name:
        return ""
    try:
        payload = _actor_status_payload(root, actor_name)
    except SystemExit:
        return ""
    return supervisor_note_check_message(actor_name, status_payload=payload)


def _should_emit_actor_note_check_warning(
    *,
    argv: list[str],
    root: Path,
    requested_root: Path | None,
    acting_actor: str,
    explicit_actor: str | None,
) -> bool:
    actor_name = session_actor(root)
    if not actor_name:
        return False
    if topology.normalize_identity(acting_actor) != actor_name:
        return False
    plugin_name = argv[0] if argv else ""
    if plugin_name == "session":
        return False
    if explicit_actor:
        return topology.normalize_identity(explicit_actor) == actor_name
    if requested_root is not None:
        if topology.parse_shared_session_root(requested_root) is not None:
            return False
        if (requested_root / "actors").is_dir():
            return False
    return True


def _creation_receipt_lines(
    root: Path,
    *,
    context_id: str,
    context_source: str,
    bound: bool,
) -> list[str]:
    shared_session_id = topology.parse_grouped_session_root(root)
    shared_id = (
        shared_session_id[0]
        if shared_session_id is not None
        else topology.parse_shared_session_root(root)
    )
    shared_topology = bool(shared_id)
    bind_target = shared_id or sh_quote(str(root))
    reuse_flag = "<shared-session-id>" if shared_topology else "<session-root>"
    lines = [
        "created a new gotta session:",
        f"- session root: {root}",
        f"- context id: {context_id}",
        f"- context source: {context_source}",
    ]
    bind_command = f"`gotta session bind {bind_target}`"
    if bound:
        lines.extend(
            [
                "- next: this context is now bound to that session root",
                "  same-context fresh-process commands should resolve here automatically",
                f"  to reuse from another context, pass `--session {reuse_flag}` or run",
                f"  {bind_command}",
            ]
        )
        return lines
    lines.extend(
        [
            "- next: this context is not yet bound to the created session",
            f"  future commands should pass `--session {reuse_flag}` or run",
            f"  {bind_command} to adopt it ambiently",
        ]
    )
    return lines
