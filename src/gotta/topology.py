"""Shared-session topology helpers."""

from __future__ import annotations

import json
from pathlib import Path
import os
from typing import TypedDict
from typing import cast

from gotta.config import user_data_dir


DEFAULT_SESSIONS_ROOT = user_data_dir() / "sessions"
DEFAULT_BINDINGS_ROOT = user_data_dir() / "bindings"
LEGACY_PRIMARY_IDENTITY = "primary"
PLACEHOLDER_IDENTITY = "actor"


class BindingRecord(TypedDict, total=False):
    bindingId: str
    contextId: str
    contextSource: str
    sessionId: str
    actor: str
    sessionRoot: str
    createdAt: str
    updatedAt: str


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
        from gotta.content.scope import DEFAULT_SESSION_ROOT

        return Path(DEFAULT_SESSION_ROOT).expanduser().resolve()
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


def binding_record_path_for(binding_id: str) -> Path:
    return binding_path_for(binding_id) / "binding.json"


def binding_root_path_for(binding_id: str) -> Path:
    return binding_path_for(binding_id) / "root"


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
    if path.is_dir():
        root_link = binding_root_path_for(binding_id)
        if not root_link.exists() and not root_link.is_symlink():
            return None
        return root_link.resolve()
    if not path.exists() and not path.is_symlink():
        return None
    return path.resolve()


def load_binding_record(binding_id: str) -> BindingRecord | None:
    path = binding_path_for(binding_id)
    if not path.is_dir():
        return None
    record_path = binding_record_path_for(binding_id)
    if not record_path.exists():
        return None
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return cast(BindingRecord, payload)


def binding_targets_session(binding_id: str, session_root: Path) -> bool:
    resolved_target = resolve_binding(binding_id)
    if resolved_target is None:
        return False
    target = session_root.expanduser().resolve()
    in_shared_topology = (
        parse_grouped_session_root(target) is not None
        or parse_shared_session_root(target) is not None
    )
    if in_shared_topology:
        return shared_session_id(resolved_target) == shared_session_id(target)
    return resolved_target == target


def binding_records_for_session(session_root: Path) -> list[BindingRecord]:
    root = bindings_root()
    if not root.exists():
        return []
    records: list[BindingRecord] = []
    for candidate in sorted(root.iterdir()):
        binding_id = candidate.name
        if not binding_targets_session(binding_id, session_root):
            continue
        record = load_binding_record(binding_id)
        if not record:
            continue
        records.append(record)
    return records


def write_binding(
    binding_id: str,
    session_root: Path,
    *,
    context_id: str,
    context_source: str,
    session_id: str,
    actor: str,
    created_at: str,
    updated_at: str,
) -> Path:
    from gotta.content.file import ensure_private_dir, write_text_atomic

    path = binding_path_for(binding_id)
    resolved_root = session_root.expanduser().resolve()
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    ensure_private_dir(path)
    root_link = binding_root_path_for(binding_id)
    desired = os.path.relpath(resolved_root, start=root_link.parent)
    if root_link.is_symlink():
        current = os.readlink(root_link)
        if current != desired:
            root_link.unlink()
            root_link.symlink_to(desired)
    elif root_link.exists():
        root_link.unlink()
        root_link.symlink_to(desired)
    else:
        root_link.symlink_to(desired)
    record: BindingRecord = {
        "bindingId": binding_id,
        "contextId": context_id,
        "contextSource": context_source,
        "sessionId": normalize_session_id(session_id),
        "actor": normalize_identity(actor) if actor.strip() else "",
        "sessionRoot": str(resolved_root),
        "createdAt": created_at,
        "updatedAt": updated_at,
    }
    write_text_atomic(
        binding_record_path_for(binding_id),
        json.dumps(record, indent=2, sort_keys=True) + "\n",
    )
    return path
