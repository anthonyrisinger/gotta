"""Init surface for `gotta session`."""

from __future__ import annotations

import argparse

from gotta.compat import UTC, datetime
from gotta.content.env import (
    SESSION_ACTIVATION_ENV,
    SESSION_CREATED_ENV,
    env_mapping,
    load_state_env_at_root,
    write_session_state,
)
from gotta.content.scope import resolve_dirs
from gotta.session import bootstrap as session_bootstrap

from .parse import options_from_args
from .show import print_session_env


def cmd_init(args: argparse.Namespace) -> int:
    dirs = resolve_dirs(options_from_args(args), create=True)
    current = dirs.session_dir.resolve()
    write_session_state(
        dirs,
        {
            SESSION_CREATED_ENV: str(
                load_state_env_at_root(current).get(SESSION_CREATED_ENV) or ""
            )
            or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            SESSION_ACTIVATION_ENV: "manual",
        },
    )
    session_bootstrap.scaffold_session(current)
    return print_session_env(args, env_mapping(dirs))
