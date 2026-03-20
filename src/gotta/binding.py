"""Session binding helpers for the current fingerprint context."""

from __future__ import annotations

import json
import os
from pathlib import Path

from gotta.content import (
    CONTENT_ENV,
    SESSION_CREATED_ENV,
    SESSION_ACTOR_ENV,
    SESSION_ID_ENV,
    CommonOptions,
    current_context_binding,
    iso_utc,
    load_state_env_at_root,
    resolve_dirs,
    session_id,
    session_identity,
    session_token,
    session_surface_initialized,
    shared_session_root,
    write_session_state,
)
from gotta import session as session_plugin
from gotta import topology


def _ensure_link(path: Path, target: Path) -> None:
    desired = os.path.relpath(target, start=path.parent)
    if path.is_symlink():
        current = os.readlink(path)
        if current == desired:
            return
        path.unlink()
    elif path.exists():
        if path.is_file():
            path.unlink()
        else:
            raise SystemExit(f"refusing to replace existing directory: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(desired)


def _update_session_metadata(session_dir: Path, *, session_id: str, actor: str) -> None:
    metadata_path = session_dir / "session.json"
    payload: dict[str, object]
    if metadata_path.exists():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        payload = existing if isinstance(existing, dict) else {}
    else:
        payload = {}
    members = payload.get("members")
    if not isinstance(members, list):
        members = []
    normalized_members = [
        topology.normalize_identity(str(item))
        for item in members
        if str(item).strip()
    ]
    if actor not in normalized_members:
        normalized_members.append(actor)
    payload["session_id"] = session_id
    payload.setdefault("created_at", iso_utc())
    payload["updated_at"] = iso_utc()
    payload["members"] = sorted(dict.fromkeys(normalized_members))
    actors = payload.get("actors")
    if not isinstance(actors, dict):
        actors = {}
    actors.setdefault(
        actor,
        {
            "label": actor,
            "model": session_plugin.ACTOR_DEFAULT_MODEL,
            "resume_uuid": "",
            "template": "",
        },
    )
    payload["actors"] = actors
    metadata_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def ensure_actor_session(
    root: Path,
    *,
    context_id: str,
    context_source: str,
    activation: str,
) -> tuple[Path, bool]:
    resolved = root.expanduser().resolve()
    current_session_id = topology.shared_session_id(resolved)
    actor = topology.session_identity(resolved)
    shared_session_dir = shared_session_root(current_session_id)
    content_dir = shared_session_dir / "content"
    shared_session_dir.mkdir(parents=True, exist_ok=True)
    content_dir.mkdir(parents=True, exist_ok=True)
    dirs = resolve_dirs(
        CommonOptions(
            session_dir=str(resolved),
            content_dir=str(content_dir),
            actor=actor,
        ),
        create=True,
    )
    dirs.session_dir.joinpath("bin").mkdir(parents=True, exist_ok=True)
    state = load_state_env_at_root(dirs.session_dir)
    write_session_state(
        dirs,
        {
            SESSION_CREATED_ENV: str(state.get(SESSION_CREATED_ENV) or "") or iso_utc(),
            SESSION_ID_ENV: current_session_id,
            SESSION_ACTOR_ENV: actor,
        },
    )
    _ensure_link(dirs.session_dir / "content", content_dir)
    session_link = dirs.session_dir / "session"
    if session_link.is_symlink() or session_link.is_file():
        session_link.unlink(missing_ok=True)
    _update_session_metadata(
        shared_session_dir,
        session_id=current_session_id,
        actor=actor,
    )
    created = not session_surface_initialized(dirs.session_dir)
    if created:
        session_plugin.scaffold_session(dirs.session_dir)
    return dirs.session_dir.resolve(), created


def _resolved_payload(root: Path) -> dict[str, str]:
    resolved = root.resolve()
    state = load_state_env_at_root(resolved)
    return {
        "sessionRoot": str(resolved),
        "sessionId": session_id(resolved),
        "sessionDir": str(shared_session_root(session_id(resolved))),
        "actor": session_identity(resolved),
        "content": str(state.get(CONTENT_ENV) or (resolved / "content")),
    }


def print_payload(payload: dict[str, str], *, output: str) -> int:
    if output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if output == "path":
        print(payload["sessionRoot"])
        return 0
    print(f"session\t{payload['sessionId']}")
    print(f"actor\t{payload['actor']}")
    print(f"root\t{payload['sessionRoot']}")
    print(f"content\t{payload['content']}")
    return 0


def bind_current_context(
    *,
    session_ref: str | None,
    output: str,
) -> int:
    context_id, context_source = current_context_binding()
    binding_id = topology.normalize_identity(session_id_from_context(context_id))
    current_session_id = topology.normalize_session_id(session_ref or binding_id)
    identity = binding_id
    root = topology.session_root_for(current_session_id, identity)
    root, _created = ensure_actor_session(
        root,
        context_id=context_id,
        context_source=context_source,
        activation="session.bind",
    )
    topology.write_binding(binding_id, root)
    return print_payload(_resolved_payload(root), output=output)


def session_id_from_context(context_id: str) -> str:
    return session_token(context_id)
