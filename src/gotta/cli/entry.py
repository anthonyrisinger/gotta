#!/usr/bin/env python3
"""Canonical gotta CLI entrypoint."""

from __future__ import annotations

import os
import sys

from gotta.actors import resolve_actor_context
import gotta.cli.argv as cli_argv
import gotta.cli.env as cli_env
import gotta.cli.notice as cli_notice
import gotta.cli.root as cli_root
import gotta.cli.select as cli_select
from gotta.content.context import current_context_binding


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    normalized = cli_argv.normalize_help_aliases(args)
    effective = cli_argv._argv_without_global_flags(normalized)
    try:
        if cli_argv._is_nonbinding_help(effective) or cli_argv._is_version_request(
            effective
        ):
            return cli_argv._gotta_main(normalized)
        if cli_root.dispatches_without_session_management(effective):
            return cli_argv._gotta_main(normalized)
        context = current_context_binding()
        context_id = context.context_id
        context_source = context.context_source
        try:
            target = cli_root.resolve_session_target(
                effective,
                context_id=context_id,
                context_source=context_source,
            )
        except SystemExit as exc:
            if isinstance(exc.code, int):
                return int(exc.code)
            return cli_notice.die(str(exc))
        original_env = os.environ.copy()
        try:
            acting_actor = resolve_actor_context(
                default_speaker=cli_select._active_identity(context_id)
            ).speaker or cli_select._active_identity(context_id)
            cli_env.activate_session_environment(
                target,
                context_id=context_id,
                context_source=context_source,
                acting_actor=acting_actor,
            )
            cli_notice.emit_session_notices(
                target,
                argv=effective,
                context_id=context_id,
                context_source=context_source,
                acting_actor=acting_actor,
            )
            return cli_argv._gotta_main(normalized)
        finally:
            os.environ.clear()
            os.environ.update(original_env)
    except BrokenPipeError:
        cli_notice._silence_stdout()
        return 0
