"""Session and actor scope resolution helpers."""

from __future__ import annotations

import os
from pathlib import Path

from gotta.content import (
    SESSION_ENV,
    SESSION_ACTOR_ENV,
    CommonOptions,
    discover_state_env,
    resolve_session_reference,
    resolve_dirs,
    session_identity,
    session_is_initialized,
    session_shared_id,
    session_surface_initialized,
)
from gotta import topology
from gotta.actor import (
    session_actor,
)

from .registry import (
    _actor_is_selected,
    _actor_registry,
    _actor_session_dir,
    _group_session_root,
    _resolve_bound_actor_name,
)


def _current_session_dir(
    explicit_session: str | None,
    explicit_actor: str | None = None,
    *,
    include_context_session: bool = True,
) -> Path | None:
    discovered = discover_state_env(include_context_session=include_context_session)
    try:
        dirs = resolve_dirs(
            CommonOptions(
                session_dir=explicit_session
                or os.environ.get(SESSION_ENV, "").strip()
                or discovered.get(SESSION_ENV, "").strip(),
                actor=explicit_actor
                or os.environ.get(SESSION_ACTOR_ENV, "").strip()
                or discovered.get(SESSION_ACTOR_ENV, "").strip(),
            ),
            create=False,
        )
    except Exception:
        return None
    return dirs.session_dir


def _session_dir(
    *,
    explicit_session: str | None,
    explicit_actor: str | None = None,
) -> Path:
    current = (
        resolve_session_reference(explicit_session, allow_missing=False)
        if explicit_session
        else _current_session_dir(
            explicit_session,
            explicit_actor=None,
        )
    )
    if current is None:
        raise SystemExit(
            "start or bind a session first with `gotta ...`. Stable interactive "
            "contexts adopt and scaffold their deterministic session on first "
            'session-aware use. Use `gotta session init --session "$WS"` only '
            "when you intentionally want to scaffold one exact root."
        )
    if explicit_actor:
        session_root = _group_session_root(current)
        current = _actor_session_dir(
            session_root,
            _resolve_bound_actor_name(session_root, explicit_actor),
        )
    elif (
        explicit_session
        and (current / "actors").is_dir()
        and topology.parse_shared_session_root(current) is not None
    ):
        current = _actor_session_dir(current, _require_primary_actor_name(current))
    if not session_is_initialized(current):
        raise SystemExit(
            "start or bind a session first with `gotta ...`. Stable interactive "
            "contexts adopt and scaffold their deterministic session on first "
            'session-aware use. Use `gotta session init --session "$WS"` only '
            "when you intentionally want to scaffold one exact root."
        )
    if not session_surface_initialized(current):
        raise SystemExit(
            "this session exists but is not scaffolded yet. In stable interactive "
            "contexts, first session-aware use should scaffold automatically. "
            "Use `gotta session init` only when you intentionally want to "
            "scaffold one exact root."
        )
    return current


def _primary_actor_name(work_dir: Path) -> str | None:
    session_root = _group_session_root(work_dir)
    selected = list(_selected_actor_ids(session_root))
    if not selected:
        return None
    shared_id = topology.normalize_identity(session_shared_id(session_root))
    if shared_id and shared_id in selected:
        return shared_id
    if len(selected) == 1:
        return selected[0]
    return None


def _require_primary_actor_name(work_dir: Path) -> str:
    primary = _primary_actor_name(work_dir)
    if primary:
        return primary
    raise SystemExit(
        "this shared session does not resolve to one canonical actor root; "
        "pass `--actor <actor>` explicitly"
    )


def _shared_session_read_root(raw: str) -> Path | None:
    resolved = resolve_session_reference(raw, allow_missing=False)
    if resolved is None:
        return None
    return _group_session_root(resolved)


def _shared_session_dir(
    *,
    explicit_session: str | None,
) -> Path:
    current = (
        _shared_session_read_root(explicit_session)
        if explicit_session
        else _current_session_dir(
            explicit_session,
            explicit_actor=None,
        )
    )
    if current is None:
        raise SystemExit(
            "start or bind a session first with `gotta ...`. Stable interactive "
            "contexts adopt and scaffold their deterministic session on first "
            'session-aware use. Use `gotta session init --session "$WS"` only '
            "when you intentionally want to scaffold one exact root."
        )
    return _group_session_root(current)


def _read_scope(
    *,
    explicit_session: str | None,
) -> tuple[Path, str]:
    current = _session_dir(
        explicit_session=explicit_session,
        explicit_actor=None,
    ).resolve()
    if (
        current.parent.name == "actors"
        or topology.parse_grouped_session_root(current) is not None
    ):
        actor_name = session_identity(current)
        if actor_name:
            return current, actor_name
    return current, ""


def _observation_scope(
    *,
    explicit_session: str | None,
    explicit_actor: str | None = None,
) -> tuple[Path, str]:
    if explicit_actor:
        current = _session_dir(
            explicit_session=explicit_session,
            explicit_actor=explicit_actor,
        ).resolve()
        return current, session_identity(current)
    current = _session_dir(
        explicit_session=explicit_session,
        explicit_actor=None,
    ).resolve()
    grouped_root = _group_session_root(current)
    if grouped_root != current:
        return grouped_root, ""
    return current, session_identity(current) if current.parent.name == "actors" else ""


def _selected_actor_ids(work_dir: Path) -> tuple[str, ...]:
    registry = _actor_registry(work_dir)
    selected: list[str] = []
    for actor_id in registry:
        if _actor_is_selected(work_dir, actor_id):
            selected.append(actor_id)
    return tuple(selected)


def _target_actor_ids(work_dir: Path, actor_ref: str | None = None) -> tuple[str, ...]:
    selected = _selected_actor_ids(work_dir)
    if not selected and session_is_initialized(work_dir):
        rooted_actor = session_actor(work_dir)
        if rooted_actor:
            selected = (rooted_actor,)
    if actor_ref:
        resolved = _resolve_bound_actor_name(work_dir, actor_ref)
        if resolved not in selected:
            raise SystemExit(
                f"{resolved} is not bound for this session; bind them first with "
                f"`gotta actor bind {actor_ref}`"
            )
        return (resolved,)
    return selected
