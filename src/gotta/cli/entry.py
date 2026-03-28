#!/usr/bin/env python3
"""Canonical gotta CLI entrypoint."""

from __future__ import annotations

import os
import sys

from gotta.actors import resolve_actor_context, seed_actor_context
from gotta.actor import SESSION_ACTOR_ENV
import gotta.cli.argv as cli_argv
import gotta.cli.bind as cli_bind
import gotta.cli.env as cli_env
import gotta.cli.notice as cli_notice
import gotta.cli.select as cli_select
from gotta.dispatch.materialize import SUPPRESS_MATERIALIZATION_ENV
from gotta import binding as binding_helpers
from gotta import content
from gotta import topology
from gotta.session import registry as session_registry


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    normalized = cli_argv.normalize_help_aliases(args)
    effective = cli_argv._argv_without_global_flags(normalized)
    try:
        if cli_argv._is_nonbinding_help(effective) or cli_argv._is_version_request(
            effective
        ):
            return cli_argv._gotta_main(normalized)
        plugin_name = effective[0] if effective else ""
        context = content.current_context_binding()
        context_id = context.context_id
        context_source = context.context_source
        explicit_session = cli_argv._explicit_session_arg(effective)
        explicit_actor = cli_argv._explicit_actor_arg(effective)
        explicit_target = bool(explicit_session or explicit_actor)
        session_access = cli_argv._session_access_mode(effective)
        auto_bootstrap = cli_select._should_auto_bootstrap_session(
            argv=effective,
            context_source=context_source,
            explicit_session=explicit_session,
            session_access=session_access,
        )
        shared_root_command = cli_select._uses_shared_session_root(
            argv=effective,
            explicit_session=explicit_session,
            explicit_actor=explicit_actor,
            session_access=session_access,
        )
        created = False
        bound_current_context = False
        init_command = (
            plugin_name == "session" and len(effective) >= 2 and effective[1] == "init"
        )
        if (
            plugin_name == "session"
            and len(effective) >= 2
            and effective[1] in {"bind"}
        ):
            return cli_argv._gotta_main(normalized)
        if session_access == "none":
            return cli_argv._gotta_main(normalized)
        if explicit_session:
            if init_command:
                target_identity = topology.normalize_identity(
                    cli_select._active_identity(context_id)
                )
                root = content.resolve_session_reference(
                    explicit_session,
                    identity=target_identity,
                    allow_missing=True,
                )
            elif shared_root_command:
                root = cli_select._resolve_shared_explicit_session(explicit_session)
            elif (
                session_access == "read"
                and not explicit_actor
                and cli_select._prefers_primary_actor_root(effective)
            ):
                shared_root = cli_select._resolve_shared_explicit_session(
                    explicit_session
                )
                if shared_root is not None and cli_select._is_shared_session_root(
                    shared_root
                ):
                    primary_root = cli_select._resolve_primary_actor_root(shared_root)
                    if primary_root is None:
                        return cli_notice.die(
                            "this shared session does not resolve to one canonical actor root; "
                            "pass `--actor <actor>` explicitly"
                        )
                    root = primary_root
                else:
                    root = shared_root
            else:
                target_identity = topology.normalize_identity(
                    explicit_actor or cli_select._active_identity(context_id)
                )
                explicit_root = content.resolve_session_reference(
                    explicit_session,
                    identity=target_identity,
                    allow_missing=False,
                )
                if explicit_root is not None and explicit_actor:
                    resolved_actor = session_registry._resolve_bound_actor_name(
                        explicit_root,
                        explicit_actor,
                    )
                    root = session_registry._actor_session_dir(
                        explicit_root, resolved_actor
                    )
                elif explicit_root is not None:
                    root = explicit_root
                else:
                    if explicit_actor:
                        return cli_notice.die(
                            "explicit actor targeting requires an existing shared session and a bound actor"
                        )
                    root = content.resolve_session_reference(
                        explicit_session,
                        identity=target_identity,
                        allow_missing=True,
                    )
            if root is None:
                return cli_notice.die(
                    "session references must be an absolute path, a shared session id, "
                    "or an explicit <session>/<actor> session reference"
                )
        elif explicit_actor:
            current = cli_select._prefer_bound_session_root()
            if current is None:
                current = cli_bind._resolve_existing_session_root(context_id)
            if current is None:
                return cli_notice.die(
                    "explicit actor targeting requires an existing session; run "
                    "`gotta session bind` first or pass `--session <session-id>`"
                )
            resolved_actor = session_registry._resolve_bound_actor_name(
                current,
                explicit_actor,
            )
            root = session_registry._actor_session_dir(current, resolved_actor)
        else:
            root = cli_select._prefer_bound_session_root()
        if root is None:
            root = cli_bind._resolve_existing_session_root(context_id)
        if root is None and (session_access == "write" or auto_bootstrap):
            root, created = cli_bind._bind_session_root(context_id, context_source)
            bound_current_context = True
        if root is None and session_access != "ambient":
            return cli_notice.die(
                "this command requires an existing session; run `gotta session bind` "
                "first or pass `--session <session-id>`"
            )
        requested_root = root
        if (
            session_access == "ambient"
            and root is not None
            and not content.session_is_initialized(root)
            and not shared_root_command
        ):
            if explicit_target:
                return cli_notice.die(
                    "ambient retrieval requires an existing initialized actor root in the "
                    "target session; bind an actor there first or pass `--actor <actor>`"
                )
            root = None
        scaffold_created = False
        if (
            auto_bootstrap
            and root is not None
            and session_access in {"read", "ambient"}
        ):
            root, scaffold_created = cli_bind._ensure_scaffolded_session(
                root,
                context_id=context_id,
                context_source=context_source,
            )
        if (
            session_access == "read"
            and root is not None
            and not content.session_is_initialized(root)
        ):
            if not shared_root_command:
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
            if root is None:
                return cli_notice.die(
                    "explicit session inspection requires an initialized actor root in the "
                    "target shared session; bind an actor there first or pass --actor"
                )
            if not content.session_is_initialized(root) and not shared_root_command:
                if explicit_session and not cli_select._is_shared_session_root(root):
                    return cli_notice.die(
                        "explicit session inspection requires an existing initialized session "
                        "at that exact root"
                    )
                return cli_notice.die(
                    "explicit session inspection requires an initialized actor root in the "
                    "target shared session; bind an actor there first or pass --actor"
                )
        elif session_access == "ambient":
            scaffold_created = False
        else:
            assert root is not None
            root, scaffold_created = cli_bind._ensure_scaffolded_session(
                root,
                context_id=context_id,
                context_source=context_source,
            )
            if init_command and root is not None and not bound_current_context:
                root = binding_helpers.bind_root_for_context(
                    root,
                    context_id=context_id,
                    context_source=context_source,
                )
                bound_current_context = True
        created = created or scaffold_created
        original_env = os.environ.copy()
        try:
            acting_actor = resolve_actor_context(
                default_speaker=cli_select._active_identity(context_id)
            ).speaker or cli_select._active_identity(context_id)
            if root is not None:
                if shared_root_command and cli_select._is_shared_session_root(root):
                    cli_env._hydrate_shared_session_environment(
                        root,
                        context_id=context_id,
                        context_source=context_source,
                    )
                else:
                    cli_env._hydrate_environment(
                        root, context_id=context_id, context_source=context_source
                    )
                    seed_actor_context(acting_actor)
                    if explicit_actor:
                        os.environ[SESSION_ACTOR_ENV] = content.session_identity(root)
                if created:
                    print(
                        "\n".join(
                            cli_notice._creation_receipt_lines(
                                root,
                                context_id=context_id,
                                context_source=context_source,
                                bound=bound_current_context,
                            )
                        ),
                        file=sys.stderr,
                    )
                warning = cli_notice._actor_stop_warning(root)
                if warning:
                    print(warning, file=sys.stderr)
                elif cli_notice._should_emit_actor_note_check_warning(
                    argv=effective,
                    root=root,
                    requested_root=requested_root,
                    acting_actor=acting_actor,
                    explicit_actor=explicit_actor,
                ):
                    pulse_warning = cli_notice._actor_note_check_warning(root)
                    if pulse_warning:
                        print(pulse_warning, file=sys.stderr)
            elif session_access == "ambient":
                os.environ[SUPPRESS_MATERIALIZATION_ENV] = "1"
                os.environ[cli_env._AMBIENT_SESSIONLESS_ENV] = "1"
            return cli_argv._gotta_main(normalized)
        finally:
            os.environ.clear()
            os.environ.update(original_env)
    except BrokenPipeError:
        cli_notice._silence_stdout()
        return 0
