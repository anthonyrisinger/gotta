#!/usr/bin/env python3
"""CLI session-root resolution and bootstrap policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gotta import binding as binding_helpers
from gotta.builtin import SessionAccessMode
import gotta.cli.argv as cli_argv
import gotta.cli.bind as cli_bind
import gotta.cli.select as cli_select
from gotta.content.scope import resolve_session_reference, session_is_initialized
from gotta import topology
from gotta.session import registry as session_registry


@dataclass(frozen=True, slots=True)
class ResolvedSessionTarget:
    root: Path | None
    requested_root: Path | None
    session_access: SessionAccessMode
    explicit_actor: str | None
    shared_root_command: bool
    created: bool
    bound_current_context: bool


def dispatches_without_session_management(argv: list[str]) -> bool:
    if not argv:
        return True
    plugin_name = argv[0]
    if plugin_name == "session" and len(argv) >= 2 and argv[1] in {"bind"}:
        return True
    return cli_argv._session_access_mode(argv) == "none"


def resolve_session_target(
    argv: list[str],
    *,
    context_id: str,
    context_source: str,
) -> ResolvedSessionTarget:
    plugin_name = argv[0] if argv else ""
    explicit_session = cli_argv._explicit_session_arg(argv)
    explicit_actor = cli_argv._explicit_actor_arg(argv)
    explicit_target = bool(explicit_session or explicit_actor)
    session_access = cli_argv._session_access_mode(argv)
    auto_bootstrap = cli_select._should_auto_bootstrap_session(
        argv=argv,
        context_source=context_source,
        explicit_session=explicit_session,
        session_access=session_access,
    )
    shared_root_command = cli_select._uses_shared_session_root(
        argv=argv,
        explicit_session=explicit_session,
        explicit_actor=explicit_actor,
        session_access=session_access,
    )
    init_command = plugin_name == "session" and len(argv) >= 2 and argv[1] == "init"

    root = _resolve_requested_root(
        argv,
        context_id=context_id,
        explicit_session=explicit_session,
        explicit_actor=explicit_actor,
        session_access=session_access,
        shared_root_command=shared_root_command,
        init_command=init_command,
    )
    created = False
    bound_current_context = False
    if root is None:
        root = cli_bind._resolve_existing_session_root(context_id)
    if root is None and (session_access == "write" or auto_bootstrap):
        root, created = cli_bind._bind_session_root(context_id, context_source)
        bound_current_context = True
    if root is None and session_access != "ambient":
        raise SystemExit(
            "this command requires an existing session; run `gotta session bind` "
            "first or pass `--session <session-id>`"
        )

    requested_root = root
    if (
        session_access == "ambient"
        and root is not None
        and not session_is_initialized(root)
        and not shared_root_command
    ):
        if explicit_target:
            raise SystemExit(
                "ambient retrieval requires an existing initialized actor root in the "
                "target session; bind an actor there first or pass `--actor <actor>`"
            )
        root = None

    root, scaffold_created, bound_current_context = _finalize_target_root(
        root,
        context_id=context_id,
        context_source=context_source,
        session_access=session_access,
        shared_root_command=shared_root_command,
        explicit_session=explicit_session,
        explicit_actor=explicit_actor,
        auto_bootstrap=auto_bootstrap,
        init_command=init_command,
        bound_current_context=bound_current_context,
    )
    return ResolvedSessionTarget(
        root=root,
        requested_root=requested_root,
        session_access=session_access,
        explicit_actor=explicit_actor,
        shared_root_command=shared_root_command,
        created=created or scaffold_created,
        bound_current_context=bound_current_context,
    )


def _resolve_requested_root(
    argv: list[str],
    *,
    context_id: str,
    explicit_session: str | None,
    explicit_actor: str | None,
    session_access: SessionAccessMode,
    shared_root_command: bool,
    init_command: bool,
) -> Path | None:
    if explicit_session:
        root = _resolve_explicit_session_root(
            argv,
            context_id=context_id,
            explicit_session=explicit_session,
            explicit_actor=explicit_actor,
            session_access=session_access,
            shared_root_command=shared_root_command,
            init_command=init_command,
        )
        if root is None:
            raise SystemExit(
                "session references must be an absolute path, a shared session id, "
                "or an explicit <session>/<actor> session reference"
            )
        return root
    if explicit_actor:
        current = cli_select._prefer_bound_session_root()
        if current is None:
            current = cli_bind._resolve_existing_session_root(context_id)
        if current is None:
            raise SystemExit(
                "explicit actor targeting requires an existing session; run "
                "`gotta session bind` first or pass `--session <session-id>`"
            )
        resolved_actor = session_registry._resolve_bound_actor_name(
            current,
            explicit_actor,
        )
        return session_registry._actor_session_dir(current, resolved_actor)
    return cli_select._prefer_bound_session_root()


def _resolve_explicit_session_root(
    argv: list[str],
    *,
    context_id: str,
    explicit_session: str,
    explicit_actor: str | None,
    session_access: SessionAccessMode,
    shared_root_command: bool,
    init_command: bool,
) -> Path | None:
    if init_command:
        target_identity = topology.normalize_identity(
            cli_select._active_identity(context_id)
        )
        return resolve_session_reference(
            explicit_session,
            identity=target_identity,
            allow_missing=True,
        )
    if shared_root_command:
        return cli_select._resolve_shared_explicit_session(explicit_session)
    if (
        session_access == "read"
        and not explicit_actor
        and cli_select._prefers_primary_actor_root(argv)
    ):
        shared_root = cli_select._resolve_shared_explicit_session(explicit_session)
        if shared_root is None or not cli_select._is_shared_session_root(shared_root):
            return shared_root
        primary_root = cli_select._resolve_primary_actor_root(shared_root)
        if primary_root is None:
            raise SystemExit(
                "this shared session does not resolve to one canonical actor root; "
                "pass `--actor <actor>` explicitly"
            )
        return primary_root
    target_identity = topology.normalize_identity(
        explicit_actor or cli_select._active_identity(context_id)
    )
    explicit_root = resolve_session_reference(
        explicit_session,
        identity=target_identity,
        allow_missing=False,
    )
    if explicit_root is not None and explicit_actor:
        resolved_actor = session_registry._resolve_bound_actor_name(
            explicit_root,
            explicit_actor,
        )
        return session_registry._actor_session_dir(explicit_root, resolved_actor)
    if explicit_root is not None:
        return explicit_root
    if explicit_actor:
        raise SystemExit(
            "explicit actor targeting requires an existing shared session and a bound actor"
        )
    return resolve_session_reference(
        explicit_session,
        identity=target_identity,
        allow_missing=True,
    )


def _finalize_target_root(
    root: Path | None,
    *,
    context_id: str,
    context_source: str,
    session_access: SessionAccessMode,
    shared_root_command: bool,
    explicit_session: str | None,
    explicit_actor: str | None,
    auto_bootstrap: bool,
    init_command: bool,
    bound_current_context: bool,
) -> tuple[Path | None, bool, bool]:
    scaffold_created = False
    if auto_bootstrap and root is not None and session_access in {"read", "ambient"}:
        root, scaffold_created = cli_bind._ensure_scaffolded_session(
            root,
            context_id=context_id,
            context_source=context_source,
        )
    if (
        session_access == "read"
        and root is not None
        and not session_is_initialized(root)
        and not shared_root_command
    ):
        existing = cli_select._existing_actor_root_for_session(
            root,
            preferred_identities=cli_select._preferred_read_only_session_identities(
                root,
                explicit_actor=explicit_actor,
            ),
        )
        if existing is not None:
            root = existing
    if session_access == "read":
        _require_initialized_read_target(
            root,
            explicit_session=explicit_session,
            shared_root_command=shared_root_command,
        )
        return root, scaffold_created, bound_current_context
    if session_access == "ambient":
        return root, False, bound_current_context
    assert root is not None
    root, created = cli_bind._ensure_scaffolded_session(
        root,
        context_id=context_id,
        context_source=context_source,
    )
    scaffold_created = scaffold_created or created
    if init_command and not bound_current_context:
        root = binding_helpers.bind_root_for_context(
            root,
            context_id=context_id,
            context_source=context_source,
        )
        bound_current_context = True
    return root, scaffold_created, bound_current_context


def _require_initialized_read_target(
    root: Path | None,
    *,
    explicit_session: str | None,
    shared_root_command: bool,
) -> None:
    if root is None:
        raise SystemExit(
            "explicit session inspection requires an initialized actor root in the "
            "target shared session; bind an actor there first or pass --actor"
        )
    if session_is_initialized(root) or shared_root_command:
        return
    if explicit_session and not cli_select._is_shared_session_root(root):
        raise SystemExit(
            "explicit session inspection requires an existing initialized session "
            "at that exact root"
        )
    raise SystemExit(
        "explicit session inspection requires an initialized actor root in the "
        "target shared session; bind an actor there first or pass --actor"
    )
