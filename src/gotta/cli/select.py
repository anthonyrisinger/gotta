#!/usr/bin/env python3
"""CLI session/root selection policy."""

from __future__ import annotations

import os
from pathlib import Path

from gotta.builtin import SessionAccessMode
import gotta.cli.env as cli_env
from gotta import content
from gotta import topology
from gotta.session import registry as session_registry
from gotta.session import scope as session_scope

_AUTO_BOOTSTRAP_CONTEXT_SOURCES = {"codex_thread", "terminal_session"}


def _resolve_shared_explicit_session(raw: str) -> Path | None:
    normalized = raw.strip()
    if not normalized:
        return None
    candidate = Path(normalized).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if topology.parse_shared_session_root(resolved) is not None:
            return resolved
        if topology.parse_grouped_session_root(resolved) is not None:
            return resolved
        if (resolved / "state" / "env").exists():
            return resolved
        return None
    if "/" in normalized:
        root = content.resolve_session_reference(normalized, allow_missing=False)
        if root is None:
            return None
        return root
    shared_root = topology.shared_session_root_for(normalized)
    if shared_root.exists() or shared_root.is_symlink():
        return shared_root.resolve()
    return None


def _prefer_bound_session_root() -> Path | None:
    explicit = os.environ.get(content.SESSION_ENV, "").strip()
    if explicit:
        root = content.resolve_session_reference(explicit, allow_missing=False)
        if root is not None and content.session_is_initialized(root):
            return root
    root = topology.resolve_binding(content.current_context_binding().binding_id)
    if root is not None and content.session_is_initialized(root):
        return root
    return None


def _active_identity(context_id: str) -> str:
    current = _prefer_bound_session_root()
    if current is not None:
        current_identity = topology.session_identity(current)
        if current_identity and not topology.is_placeholder_identity(current_identity):
            return current_identity
    explicit = os.environ.get(content.SESSION_ACTOR_ENV, "").strip()
    if explicit and not topology.is_placeholder_identity(explicit):
        return topology.normalize_identity(explicit)
    return content.default_session_id(context_id)


def _should_auto_bootstrap_session(
    *,
    argv: list[str],
    context_source: str,
    explicit_session: str | None,
    session_access: SessionAccessMode,
) -> bool:
    if explicit_session:
        return False
    if session_access == "none":
        return False
    if context_source not in _AUTO_BOOTSTRAP_CONTEXT_SOURCES:
        return False
    if argv[:1] != ["read"]:
        return True
    try:
        from gotta.resolve.read import resolve_read_target

        resolved = resolve_read_target(argv[1:])
    except SystemExit:
        return False
    return resolved.should_materialize


def _existing_actor_root_for_session(
    root: Path,
    *,
    preferred_identities: list[str],
) -> Path | None:
    actors_dir = session_registry._group_session_root(root) / "actors"
    if not actors_dir.is_dir():
        return None
    initialized: dict[str, Path] = {}
    for actor_dir in sorted(actors_dir.iterdir()):
        if not actor_dir.is_dir():
            continue
        resolved = actor_dir.resolve()
        if not content.session_is_initialized(resolved):
            continue
        identity = topology.session_identity(resolved)
        if not identity or topology.is_placeholder_identity(identity):
            continue
        initialized[identity] = resolved
    for identity in preferred_identities:
        normalized = topology.normalize_identity(identity)
        match = initialized.get(normalized)
        if match is not None:
            return match
    return next(iter(initialized.values()), None)


def _preferred_read_only_session_identities(
    root: Path,
    *,
    explicit_actor: str | None,
) -> list[str]:
    preferred: list[str] = []
    if explicit_actor:
        preferred.append(explicit_actor)
    current = _prefer_bound_session_root()
    if current is None:
        return preferred
    if topology.shared_session_id(current) != topology.shared_session_id(root):
        return preferred
    current_identity = topology.session_identity(current)
    if current_identity and not topology.is_placeholder_identity(current_identity):
        preferred.append(current_identity)
    return preferred


def _is_shared_session_root(root: Path | None) -> bool:
    if root is None:
        return False
    resolved = root.expanduser().resolve()
    return topology.parse_shared_session_root(resolved) is not None


def _looks_like_stored_read_target(argv: list[str], explicit_session: str) -> bool:
    if argv[:1] != ["read"]:
        return False
    shared_root = _resolve_shared_explicit_session(explicit_session)
    if shared_root is None:
        return False
    try:
        from gotta.resolve.read import resolve_read_target

        resolved = resolve_read_target(
            argv[1:],
            content.CommonOptions(
                session_dir=str(shared_root),
                content_dir=str(cli_env._explicit_session_content_root(shared_root)),
            ),
        )
    except SystemExit:
        return False
    target = str(resolved.request.target or "").strip()
    if resolved.kind in {"artifact_locator", "artifact_name"}:
        return True
    if target.startswith("content:"):
        return True
    return len(target) == 64 and all(ch in "0123456789abcdef" for ch in target)


def _uses_shared_session_root(
    *,
    argv: list[str],
    explicit_session: str | None,
    explicit_actor: str | None,
    session_access: SessionAccessMode,
) -> bool:
    if not explicit_session or explicit_actor:
        return False
    if argv[:1] == ["session"]:
        return True
    if argv[:1] == ["notes"]:
        return True
    if session_access == "ambient":
        return _looks_like_stored_read_target(argv, explicit_session)
    return False


def _prefers_primary_actor_root(argv: list[str]) -> bool:
    if not argv:
        return False
    plugin = argv[0]
    if plugin in {"want", "goal"}:
        return True
    if plugin == "session" and len(argv) >= 2 and argv[1] == "show":
        return True
    if plugin == "actor" and len(argv) >= 2 and argv[1] == "status":
        return True
    return False


def _resolve_primary_actor_root(root: Path) -> Path | None:
    session_root = root.expanduser().resolve()
    if not _is_shared_session_root(session_root):
        return session_root
    primary = session_scope._primary_actor_name(session_root)
    if not primary:
        return None
    return session_registry._actor_session_dir(session_root, primary)
