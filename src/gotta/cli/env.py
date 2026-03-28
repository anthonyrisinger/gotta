#!/usr/bin/env python3
"""CLI environment hydration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from gotta.actors import seed_actor_context
from gotta.dispatch.materialize import SUPPRESS_MATERIALIZATION_ENV
from gotta.content.env import (
    CONTENT_ENV,
    CONTEXT_ACTIVE_ENV,
    CONTEXT_ID_ENV,
    CONTEXT_SOURCE_ENV,
    SESSION_ACTIVATION_ENV,
    SESSION_ACTOR_ENV,
    SESSION_ENV,
    SESSION_ID_ENV,
    SESSION_REPO_ENV,
    load_state_env_at_root,
)
from gotta.content.scope import session_identity
from gotta.content.scope import session_actor_scope, session_id
from gotta import topology
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope

if TYPE_CHECKING:
    from gotta.cli.root import ResolvedSessionTarget

_AMBIENT_SESSIONLESS_ENV = "GOTTA_AMBIENT_SESSIONLESS"


def _explicit_session_content_root(root: Path) -> Path:
    return (session_registry._group_session_root(root) / "content").resolve()


def _hydrate_environment(root: Path, *, context_id: str, context_source: str) -> None:
    state = load_state_env_at_root(root)
    for key, value in state.items():
        os.environ[key] = value
    os.environ[SESSION_ENV] = str(root)
    os.environ[SESSION_ID_ENV] = str(state.get(SESSION_ID_ENV) or session_id(root))
    os.environ[CONTENT_ENV] = str(state.get(CONTENT_ENV) or (root / "content"))
    actor_scope = str(state.get(SESSION_ACTOR_ENV) or session_actor_scope(root))
    if actor_scope:
        os.environ[SESSION_ACTOR_ENV] = actor_scope
    else:
        os.environ.pop(SESSION_ACTOR_ENV, None)
    os.environ[CONTEXT_ACTIVE_ENV] = "1"
    os.environ[CONTEXT_ID_ENV] = context_id
    os.environ[CONTEXT_SOURCE_ENV] = context_source
    os.environ[SESSION_ACTIVATION_ENV] = "gotta"
    repo_root = state.get(SESSION_REPO_ENV, "").strip()
    if repo_root:
        os.environ[SESSION_REPO_ENV] = repo_root
        venv = Path(repo_root) / ".venv"
        venv_bin = venv / "bin"
        if venv_bin.is_dir():
            path_entries = [str(venv_bin)]
            current_path = os.environ.get("PATH", "")
            if current_path:
                path_entries.append(current_path)
            os.environ["PATH"] = ":".join(path_entries)
            os.environ["VIRTUAL_ENV"] = str(venv)


def _hydrate_shared_session_environment(
    root: Path,
    *,
    context_id: str,
    context_source: str,
) -> None:
    session_root = root.expanduser().resolve()
    state: dict[str, str] = {}
    primary = session_scope._primary_actor_name(session_root) or ""
    if primary:
        primary_root = session_registry._actor_session_dir(session_root, primary)
        state = load_state_env_at_root(primary_root)
    for key, value in state.items():
        os.environ[key] = value
    os.environ[SESSION_ENV] = str(session_root)
    os.environ[SESSION_ID_ENV] = topology.shared_session_id(session_root)
    os.environ[CONTENT_ENV] = str(_explicit_session_content_root(session_root))
    if primary:
        os.environ[SESSION_ACTOR_ENV] = primary
    else:
        os.environ.pop(SESSION_ACTOR_ENV, None)
    os.environ[CONTEXT_ACTIVE_ENV] = "1"
    os.environ[CONTEXT_ID_ENV] = context_id
    os.environ[CONTEXT_SOURCE_ENV] = context_source
    os.environ[SESSION_ACTIVATION_ENV] = "gotta"
    repo_root = str(state.get(SESSION_REPO_ENV) or "").strip()
    if repo_root:
        os.environ[SESSION_REPO_ENV] = repo_root
        venv_bin = Path(repo_root) / ".venv" / "bin"
        if venv_bin.is_dir():
            current_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ":".join(
                [str(venv_bin), current_path] if current_path else [str(venv_bin)]
            )
            os.environ["VIRTUAL_ENV"] = str(venv_bin.parent)


def activate_session_environment(
    target: ResolvedSessionTarget,
    *,
    context_id: str,
    context_source: str,
    acting_actor: str,
) -> None:
    root = target.root
    if root is None:
        if target.session_access == "ambient":
            os.environ[SUPPRESS_MATERIALIZATION_ENV] = "1"
            os.environ[_AMBIENT_SESSIONLESS_ENV] = "1"
        return
    if (
        target.shared_root_command
        and topology.parse_shared_session_root(root) is not None
    ):
        _hydrate_shared_session_environment(
            root,
            context_id=context_id,
            context_source=context_source,
        )
        return
    _hydrate_environment(root, context_id=context_id, context_source=context_source)
    seed_actor_context(acting_actor)
    if target.explicit_actor:
        os.environ[SESSION_ACTOR_ENV] = session_identity(root)
