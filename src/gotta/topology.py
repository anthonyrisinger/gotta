"""Shared-session topology helpers."""

from __future__ import annotations

from pathlib import Path
import os

from gotta.config import user_data_dir


DEFAULT_SESSIONS_ROOT = user_data_dir() / "sessions"
DEFAULT_BINDINGS_ROOT = user_data_dir() / "bindings"
LEGACY_PRIMARY_IDENTITY = "primary"
PLACEHOLDER_IDENTITY = "actor"


def sanitize_token(value: str, *, fallback: str) -> str:
    stripped = value.strip().replace("/", "-").replace("\\", "-")
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in stripped)
    cleaned = cleaned.strip("-.")
    return cleaned or fallback


def normalize_identity(value: str) -> str:
    return sanitize_token(value.strip().lower(), fallback=PLACEHOLDER_IDENTITY)


def is_legacy_primary_identity(value: str) -> bool:
    return value.strip().lower() == LEGACY_PRIMARY_IDENTITY


def is_placeholder_identity(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {LEGACY_PRIMARY_IDENTITY, PLACEHOLDER_IDENTITY}


def normalize_session_id(value: str) -> str:
    return sanitize_token(value.strip().lower(), fallback="session")


def sessions_root() -> Path:
    try:
        from gotta import content as content_module

        return Path(content_module.DEFAULT_SESSION_ROOT).expanduser().resolve()
    except Exception:
        return DEFAULT_SESSIONS_ROOT.expanduser().resolve()


def shared_sessions_root() -> Path:
    return sessions_root()


def bindings_root() -> Path:
    return sessions_root().parent / "bindings"


def session_root_for(session_id: str, identity: str) -> Path:
    return (
        shared_sessions_root()
        / normalize_session_id(session_id)
        / "actors"
        / normalize_identity(identity)
    )


def shared_session_root_for(session_id: str) -> Path:
    return shared_sessions_root() / normalize_session_id(session_id)


def binding_path_for(binding_id: str) -> Path:
    token = sanitize_token(binding_id, fallback="binding")
    return bindings_root() / token


def default_actor_session_id(session_id: str, identity: str) -> str:
    return f"{normalize_session_id(session_id)}-{normalize_identity(identity)}"


def parse_grouped_session_root(root: Path) -> tuple[str, str] | None:
    resolved = root.expanduser().resolve()
    root_base = sessions_root()
    try:
        relative = resolved.relative_to(root_base)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) != 3 or parts[1] != "actors":
        return None
    session_id, _actors_dir, identity = parts
    if not session_id or not identity:
        return None
    return session_id, identity


def parse_shared_session_root(root: Path) -> str | None:
    resolved = root.expanduser().resolve()
    root_base = sessions_root()
    try:
        relative = resolved.relative_to(root_base)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) != 1:
        return None
    session_id = parts[0]
    if not session_id:
        return None
    return normalize_session_id(session_id)


def shared_session_id(root: Path) -> str:
    parsed = parse_grouped_session_root(root)
    if parsed is not None:
        return parsed[0]
    shared = parse_shared_session_root(root)
    if shared is not None:
        return shared
    resolved = root.expanduser().resolve()
    parent = resolved.parent
    if parent.name == "actors" and parent.parent != resolved:
        return normalize_session_id(parent.parent.name)
    return normalize_session_id(resolved.name)


def session_identity(root: Path) -> str:
    parsed = parse_grouped_session_root(root)
    if parsed is not None:
        return parsed[1]
    resolved = root.expanduser().resolve()
    if resolved.parent.name == "actors":
        return normalize_identity(resolved.name)
    return ""


def iter_session_roots() -> list[Path]:
    base = sessions_root()
    if not base.exists():
        return []
    roots: list[Path] = []
    for session_dir in base.iterdir():
        if not session_dir.is_dir():
            continue
        actors_dir = session_dir / "actors"
        if not actors_dir.is_dir():
            continue
        for actor_dir in actors_dir.iterdir():
            if actor_dir.is_dir():
                roots.append(actor_dir.resolve())
    return sorted(roots)


def resolve_session_root_by_id(session_ref: str) -> Path | None:
    normalized = session_ref.strip()
    if not normalized:
        return None
    candidate = Path(normalized).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    if "/" in normalized:
        session_id, identity = normalized.split("/", 1)
        root = session_root_for(session_id, identity)
        if root.exists() or root.is_symlink():
            return root.resolve()
    return None


def session_metadata_path_for(session_id: str) -> Path:
    return shared_session_root_for(session_id) / "session.json"


def resolve_binding(binding_id: str) -> Path | None:
    path = binding_path_for(binding_id)
    if not path.exists() and not path.is_symlink():
        return None
    return path.resolve()


def write_binding(binding_id: str, session_root: Path) -> Path:
    path = binding_path_for(binding_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    desired = os.path.relpath(session_root.expanduser().resolve(), start=path.parent)
    if path.is_symlink():
        current = os.readlink(path)
        if current == desired:
            return path
        path.unlink()
    elif path.exists():
        path.unlink()
    path.symlink_to(desired)
    return path
