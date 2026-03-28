#!/usr/bin/env python3
"""CLI environment hydration."""

from __future__ import annotations

import os
from pathlib import Path

from gotta import content
from gotta import topology
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope

_AMBIENT_SESSIONLESS_ENV = "GOTTA_AMBIENT_SESSIONLESS"


def _explicit_session_content_root(root: Path) -> Path:
    return (session_registry._group_session_root(root) / "content").resolve()


def _hydrate_environment(root: Path, *, context_id: str, context_source: str) -> None:
    state = content.load_state_env_at_root(root)
    for key, value in state.items():
        os.environ[key] = value
    os.environ[content.SESSION_ENV] = str(root)
    os.environ[content.SESSION_ID_ENV] = str(
        state.get(content.SESSION_ID_ENV) or content.session_id(root)
    )
    os.environ[content.CONTENT_ENV] = str(
        state.get(content.CONTENT_ENV) or (root / "content")
    )
    actor_scope = str(
        state.get(content.SESSION_ACTOR_ENV) or content.session_actor_scope(root)
    )
    if actor_scope:
        os.environ[content.SESSION_ACTOR_ENV] = actor_scope
    else:
        os.environ.pop(content.SESSION_ACTOR_ENV, None)
    os.environ[content.CONTEXT_ACTIVE_ENV] = "1"
    os.environ[content.CONTEXT_ID_ENV] = context_id
    os.environ[content.CONTEXT_SOURCE_ENV] = context_source
    os.environ[content.SESSION_ACTIVATION_ENV] = "gotta"
    repo_root = state.get(content.SESSION_REPO_ENV, "").strip()
    if repo_root:
        os.environ[content.SESSION_REPO_ENV] = repo_root
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
        state = content.load_state_env_at_root(primary_root)
    for key, value in state.items():
        os.environ[key] = value
    os.environ[content.SESSION_ENV] = str(session_root)
    os.environ[content.SESSION_ID_ENV] = topology.shared_session_id(session_root)
    os.environ[content.CONTENT_ENV] = str(_explicit_session_content_root(session_root))
    if primary:
        os.environ[content.SESSION_ACTOR_ENV] = primary
    else:
        os.environ.pop(content.SESSION_ACTOR_ENV, None)
    os.environ[content.CONTEXT_ACTIVE_ENV] = "1"
    os.environ[content.CONTEXT_ID_ENV] = context_id
    os.environ[content.CONTEXT_SOURCE_ENV] = context_source
    os.environ[content.SESSION_ACTIVATION_ENV] = "gotta"
    repo_root = str(state.get(content.SESSION_REPO_ENV) or "").strip()
    if repo_root:
        os.environ[content.SESSION_REPO_ENV] = repo_root
        venv_bin = Path(repo_root) / ".venv" / "bin"
        if venv_bin.is_dir():
            current_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ":".join(
                [str(venv_bin), current_path] if current_path else [str(venv_bin)]
            )
            os.environ["VIRTUAL_ENV"] = str(venv_bin.parent)
