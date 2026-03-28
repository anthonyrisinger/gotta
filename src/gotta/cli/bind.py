#!/usr/bin/env python3
"""CLI session binding and bootstrap helpers."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import time

from gotta.compat import UTC, datetime
from gotta import topology
from gotta.content.context import default_session_id
from gotta.content.env import (
    SESSION_ACTOR_ENV,
    SESSION_CREATED_ENV,
    SESSION_ID_ENV,
    SESSION_INITIALIZED_ENV,
    SESSION_REPO_ENV,
    load_state_env_at_root,
    write_session_state,
)
from gotta.content.file import ensure_private_dir, write_text_atomic
from gotta.content.model import CommonOptions
from gotta.content.scope import (
    DEFAULT_SESSION_ROOT,
    resolve_dirs,
    session_identity,
    session_is_initialized,
)
from gotta.session import bootstrap as session_bootstrap
from gotta.session import registry as session_registry

_SESSION_LOCK_TIMEOUT_SECONDS = 5.0
_SESSION_LOCK_SLEEP_SECONDS = 0.05


def _session_token(context_id: str) -> str:
    return default_session_id(context_id)


@contextmanager
def _session_creation_lock(base_dir: Path, context_id: str):
    ensure_private_dir(base_dir)
    lock_path = base_dir / f".{_session_token(context_id)}.lock"
    deadline = time.monotonic() + _SESSION_LOCK_TIMEOUT_SECONDS
    fd: int | None = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"timed out waiting for gotta session bind lock: {lock_path}"
                )
            time.sleep(_SESSION_LOCK_SLEEP_SECONDS)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _discover_repo_root() -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _create_session_root(
    root: Path,
    *,
    context_id: str,
    context_source: str,
) -> tuple[Path, bool]:
    del context_source
    resolved_root = root.expanduser().resolve()
    current_session_id = topology.shared_session_id(resolved_root)
    actor = session_identity(resolved_root)
    actor_branch = (
        resolved_root.parent.name == "actors"
        or topology.parse_grouped_session_root(resolved_root) is not None
    )
    session_dir = session_registry._group_session_root(resolved_root)
    content_dir = session_dir / "content"
    ensure_private_dir(session_dir)
    ensure_private_dir(content_dir)
    dirs = resolve_dirs(
        CommonOptions(
            session_dir=str(resolved_root),
            content_dir=str(content_dir),
            actor=actor,
        ),
        create=True,
    )
    repo_root = _discover_repo_root()
    write_session_state(
        dirs,
        {
            SESSION_CREATED_ENV: load_state_env_at_root(root).get(
                SESSION_CREATED_ENV, ""
            )
            or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            SESSION_REPO_ENV: str(repo_root) if repo_root is not None else "",
            SESSION_ID_ENV: current_session_id,
            SESSION_ACTOR_ENV: actor,
        },
    )
    content_link = dirs.session_dir / "content"
    if content_link.resolve() != content_dir.resolve():
        if content_link.is_symlink() or content_link.is_file():
            content_link.unlink(missing_ok=True)
        elif content_link.exists():
            os.rmdir(content_link)
        content_link.symlink_to(os.path.relpath(content_dir, start=content_link.parent))
    session_link = dirs.session_dir / "session"
    if session_link.is_symlink() or session_link.is_file():
        session_link.unlink(missing_ok=True)
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
        topology.normalize_identity(str(item)) for item in members if str(item).strip()
    ]
    if actor_branch and actor and actor not in normalized_members:
        normalized_members.append(actor)
    actors = payload.get("actors")
    if not isinstance(actors, dict):
        actors = {}
    if actor_branch and actor:
        actors.setdefault(
            actor,
            {
                "label": actor,
                "model": session_registry.ACTOR_DEFAULT_MODEL,
                "resume_uuid": "",
                "template": "",
            },
        )
    payload["session_id"] = current_session_id
    payload.setdefault(
        "created_at", datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    payload["updated_at"] = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["members"] = sorted(dict.fromkeys(normalized_members))
    payload["actors"] = actors
    write_text_atomic(
        metadata_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return dirs.session_dir.resolve(), True


def _bind_session_root(context_id: str, context_source: str) -> tuple[Path, bool]:
    fingerprint = _session_token(context_id)
    current_session_id = fingerprint
    root = topology.session_root_for(current_session_id, fingerprint)
    ensure_private_dir(topology.shared_session_root_for(current_session_id))
    created = False
    with _session_creation_lock(
        DEFAULT_SESSION_ROOT.expanduser().resolve(), context_id
    ):
        if not session_is_initialized(root):
            root, _created = _create_session_root(
                root,
                context_id=context_id,
                context_source=context_source,
            )
            created = True
        dirs = resolve_dirs(CommonOptions(session_dir=str(root)), create=False)
        write_session_state(
            dirs,
            {
                SESSION_ID_ENV: current_session_id,
                SESSION_ACTOR_ENV: fingerprint,
            },
        )
        now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        existing = topology.load_binding_record(fingerprint) or {}
        topology.write_binding(
            fingerprint,
            root,
            context_id=context_id,
            context_source=context_source,
            session_id=current_session_id,
            actor=fingerprint,
            created_at=str(existing.get("createdAt") or now),
            updated_at=now,
        )
    return root, created


def _resolve_existing_session_root(context_id: str) -> Path | None:
    binding = topology.resolve_binding(_session_token(context_id))
    if binding is not None and session_is_initialized(binding):
        return binding
    return None


def _ensure_scaffolded_session(
    root: Path,
    *,
    context_id: str,
    context_source: str,
) -> tuple[Path, bool]:
    shared_session = topology.parse_shared_session_root(root)
    if shared_session is not None:
        raise RuntimeError(f"shared session roots require an actor: {root}")
    created = False
    if not session_is_initialized(root):
        root, created = _create_session_root(
            root,
            context_id=context_id,
            context_source=context_source,
        )
    state = load_state_env_at_root(root)
    if state.get(SESSION_INITIALIZED_ENV, "").strip() != "1":
        session_bootstrap.scaffold_session(root)
    return root, created
