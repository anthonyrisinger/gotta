#!/usr/bin/env python3
"""CLI session-root resolution and bootstrap policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gotta.builtin import SessionAccessMode
import gotta.cli.argv as cli_argv
import gotta.cli.bind as cli_bind
import gotta.cli.select as cli_select


@dataclass(frozen=True, slots=True)
class ResolvedSessionTarget:
    root: Path | None
    requested_root: Path | None
    session_access: SessionAccessMode
    explicit_actor: str | None
    shared_root_command: bool
    created: bool
    bound_current_context: bool


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

    root = cli_select.resolve_requested_root(
        argv,
        context_id=context_id,
        explicit_session=explicit_session,
        explicit_actor=explicit_actor,
        session_access=session_access,
        shared_root_command=shared_root_command,
        init_command=init_command,
    )
    root, created, bound_current_context = cli_bind.resolve_target_root(
        root,
        context_id=context_id,
        context_source=context_source,
        session_access=session_access,
        auto_bootstrap=auto_bootstrap,
    )
    if root is None and session_access != "ambient":
        raise SystemExit(
            "this command requires an existing session; run `gotta session bind` "
            "first or pass `--session <session-id>`"
        )

    requested_root = root
    root = cli_select.ambient_target_root(
        root,
        session_access=session_access,
        shared_root_command=shared_root_command,
        explicit_target=explicit_target,
    )
    root, scaffold_created, bound_current_context = cli_bind.finalize_target_root(
        root,
        context_id=context_id,
        context_source=context_source,
        session_access=session_access,
        auto_bootstrap=auto_bootstrap,
        init_command=init_command,
        bound_current_context=bound_current_context,
    )
    if session_access == "read":
        root = cli_select.finalize_read_target_root(
            root,
            explicit_session=explicit_session,
            explicit_actor=explicit_actor,
            shared_root_command=shared_root_command,
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
