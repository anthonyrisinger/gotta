"""Show surface for `gotta session`."""

from __future__ import annotations

import argparse
import json

from gotta.content.model import ResolvedDirs
from gotta.content.env import CONTENT_ENV, SESSION_ENV
from gotta.content.path import sh_quote
from gotta.content.scope import session_identity, session_shared_id
from gotta import topology
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope

from .parse import require_started_session, session_dirs_for_read

SessionEnvPayload = dict[str, str]


def print_session_env(
    args: argparse.Namespace,
    payload: SessionEnvPayload,
) -> int:
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
    print(
        "\n".join(f"export {key}={sh_quote(value)}" for key, value in payload.items())
    )
    return 0


def show_payload(dirs: ResolvedDirs) -> SessionEnvPayload:
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


def cmd_show(args: argparse.Namespace) -> int:
    dirs = session_dirs_for_read(args)
    require_started_session(dirs)
    return print_session_env(args, show_payload(dirs))
