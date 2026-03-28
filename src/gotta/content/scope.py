from __future__ import annotations

import os
from pathlib import Path

from gotta.content.context import current_actor, current_context_binding
from gotta.content.env import (
    ACTOR_ID_ENV,
    CONTENT_ENV,
    SESSION_ACTOR_ENV,
    SESSION_ENV,
    SESSION_ID_ENV,
    SESSION_INITIALIZED_ENV,
    discover_state_env,
    load_state_env_at_root,
)
from gotta.content.file import ensure_private_dir
from gotta.content.model import CommonOptions, ContentError, ResolvedDirs
from gotta import topology

DEFAULT_SESSION_ROOT = topology.DEFAULT_SESSIONS_ROOT


def session_is_initialized(root: Path) -> bool:
    if topology.parse_shared_session_root(root) is not None:
        return False
    from gotta.content.env import state_env_path

    return state_env_path(root).exists()


def session_id(root: Path) -> str:
    state = load_state_env_at_root(root)
    explicit = str(state.get(SESSION_ID_ENV) or "").strip()
    if explicit:
        return explicit
    return topology.shared_session_id(root)


def resolve_session_root_by_id(session_ref: str) -> Path | None:
    root = topology.resolve_session_root_by_id(session_ref)
    if root is None:
        return None
    if session_is_initialized(root):
        return root.resolve()
    return root.resolve()


def resolve_session_reference(
    raw: str,
    *,
    identity: str | None = None,
    allow_missing: bool = False,
) -> Path | None:
    normalized = raw.strip()
    if not normalized:
        return None
    candidate = Path(normalized).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        shared_id = topology.parse_shared_session_root(resolved)
        if shared_id is not None and identity:
            return topology.session_root_for(shared_id, identity).resolve()
        return resolved
    by_id = resolve_session_root_by_id(normalized)
    if by_id is not None:
        shared_id = topology.parse_shared_session_root(by_id)
        if shared_id is not None and identity:
            return topology.session_root_for(shared_id, identity).resolve()
        return by_id
    if "/" in normalized:
        raw_session_id, session_identity = normalized.split("/", 1)
        root = topology.session_root_for(raw_session_id, session_identity)
        if allow_missing or root.exists() or root.is_symlink():
            return root.resolve()
    if identity:
        shared_root = topology.shared_session_root_for(normalized)
        if allow_missing or shared_root.exists() or shared_root.is_symlink():
            return topology.session_root_for(normalized, identity).resolve()
    return None


def context_bound_session_root() -> Path | None:
    binding = current_context_binding()
    root = topology.resolve_binding(binding.binding_id)
    if root is None:
        legacy = (
            DEFAULT_SESSION_ROOT.expanduser().resolve()
            / binding.binding_id
            / "actors"
            / binding.binding_id
        )
        if session_is_initialized(legacy):
            return legacy
        return None
    if session_is_initialized(root):
        return root
    return None


def bound_session_root(*, include_context_session: bool = True) -> Path | None:
    discovered = discover_state_env(include_context_session=include_context_session)
    identity_raw = (
        os.environ.get(ACTOR_ID_ENV, "").strip()
        or os.environ.get(SESSION_ACTOR_ENV, "").strip()
        or str(discovered.get(SESSION_ACTOR_ENV) or "").strip()
    )
    identity = (
        topology.normalize_identity(identity_raw)
        if identity_raw and not topology.is_placeholder_identity(identity_raw)
        else ""
    )
    explicit = os.environ.get(SESSION_ENV, "").strip()
    if explicit:
        candidate = resolve_session_reference(
            explicit, identity=identity, allow_missing=False
        )
        if candidate is not None and session_is_initialized(candidate):
            return candidate
    discovered_root = str(discovered.get(SESSION_ENV) or "").strip()
    if discovered_root:
        candidate = resolve_session_reference(
            discovered_root,
            identity=identity,
            allow_missing=False,
        )
        if candidate is not None and session_is_initialized(candidate):
            return candidate
    for raw_id in (
        os.environ.get(SESSION_ID_ENV, "").strip(),
        str(discovered.get(SESSION_ID_ENV) or "").strip(),
    ):
        if not raw_id:
            continue
        if identity:
            sibling = topology.session_root_for(raw_id, identity)
            if session_is_initialized(sibling):
                return sibling
        candidate = resolve_session_root_by_id(raw_id)
        if candidate is not None and session_is_initialized(candidate):
            return candidate
    if include_context_session:
        return context_bound_session_root()
    return None


def resolve_dirs(options: CommonOptions, *, create: bool) -> ResolvedDirs:
    discovered = discover_state_env()
    explicit_session = bool(options.session_dir)
    session_raw = (
        options.session_dir
        or os.environ.get(SESSION_ENV, "").strip()
        or discovered.get(SESSION_ENV, "").strip()
    )
    content_raw = (
        options.content_dir
        or ("" if explicit_session else os.environ.get(CONTENT_ENV, "").strip())
        or ("" if explicit_session else discovered.get(CONTENT_ENV, "").strip())
    )
    session_id_raw = (
        options.session_id
        or os.environ.get(SESSION_ID_ENV, "").strip()
        or discovered.get(SESSION_ID_ENV, "").strip()
    )
    identity_raw = (
        options.actor
        or os.environ.get(ACTOR_ID_ENV, "").strip()
        or os.environ.get(SESSION_ACTOR_ENV, "").strip()
        or discovered.get(SESSION_ACTOR_ENV, "").strip()
    )
    if not identity_raw or topology.is_placeholder_identity(identity_raw):
        identity_raw = current_context_binding().binding_id

    session: Path | None = (
        resolve_session_reference(
            session_raw,
            identity=identity_raw,
            allow_missing=create,
        )
        if session_raw
        else None
    )
    if session_raw and session is None:
        normalized_session_id = topology.normalize_session_id(session_raw)
        identity = topology.normalize_identity(identity_raw)
        session = topology.session_root_for(normalized_session_id, identity)
        if create:
            ensure_private_dir(shared_session_root(normalized_session_id))
        content_raw = str(shared_session_root(normalized_session_id) / "content")
    if session is None and session_id_raw:
        normalized_session_id = topology.normalize_session_id(session_id_raw)
        identity = topology.normalize_identity(identity_raw)
        session = topology.session_root_for(normalized_session_id, identity)
        if create:
            ensure_private_dir(shared_session_root(normalized_session_id))
        content_raw = str(shared_session_root(normalized_session_id) / "content")
    if not content_raw and session is not None:
        parsed = topology.parse_grouped_session_root(session)
        if parsed is not None:
            content_raw = str(shared_session_root(parsed[0]) / "content")
    content = Path(content_raw).expanduser() if content_raw else None

    if session is None and content is not None:
        session = content.parent
    if session is not None:
        content = content or (session / "content")

    if session is None or content is None:
        raise ContentError(
            "missing shared content context; gotta needs a session root and content root. "
            "Set GOTTA_SESSION_DIR / GOTTA_SESSION_CONTENT_DIR, pass --session/--content-dir, "
            "or use `gotta ...` so gotta can bind or scaffold the correct session "
            'for you. Use `gotta session init "$WS"` only when you intentionally '
            "want to scaffold one exact root."
        )

    if create:
        session = ensure_private_dir(session.resolve())
        content = ensure_private_dir(content.resolve())
    else:
        session = session.resolve()
        content = content.resolve()

    return ResolvedDirs(session, content)


def shared_session_root(session_id: str) -> Path:
    return topology.shared_session_root_for(session_id)


def session_shared_id(root: Path) -> str:
    state = load_state_env_at_root(root)
    explicit = str(state.get(SESSION_ID_ENV) or "").strip()
    if explicit:
        return topology.normalize_session_id(explicit)
    return topology.shared_session_id(root)


def session_actor_scope(root: Path) -> str:
    derived = topology.session_identity(root)
    if derived and not topology.is_placeholder_identity(derived):
        return topology.normalize_identity(derived)
    return ""


def session_identity(root: Path) -> str:
    state = load_state_env_at_root(root)
    explicit = str(
        state.get(SESSION_ACTOR_ENV) or state.get("GOTTA_SESSION_ACTOR") or ""
    ).strip()
    if explicit and not topology.is_placeholder_identity(explicit):
        return topology.normalize_identity(explicit)
    derived = topology.session_identity(root)
    if derived and not topology.is_placeholder_identity(derived):
        return derived
    resolved = root.expanduser().resolve()
    if topology.parse_shared_session_root(resolved) is not None:
        return current_actor()
    fallback = topology.normalize_identity(resolved.name)
    if topology.is_placeholder_identity(fallback):
        return current_context_binding().binding_id
    return fallback


def session_surface_initialized(root: Path) -> bool:
    state = load_state_env_at_root(root)
    if state.get(SESSION_INITIALIZED_ENV, "").strip() == "1":
        return True
    required = ("WANT.md", "GOAL.md")
    return all((root / name).exists() for name in required)
