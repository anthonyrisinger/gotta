"""Top-level session-rooted intent frame surface."""

from __future__ import annotations

from gotta import session as sessionlib


def session_access_mode(argv: list[str]) -> str:
    return sessionlib.charter_session_access_mode(list(argv or []))


def main(argv: list[str] | None = None) -> int:
    return sessionlib.run_charter_surface(
        argv,
        command_name="gotta want",
        description=(
            "Inspect or rewrite the canonical session intent frame. "
            "Bare invocation shows all bound actor intent frames; use --stdin or --from-file to rewrite. "
            "Pass --actor <actor> to narrow inspection or target one actor for rewrite."
        ),
        surface_name="WANT.md",
        plugin_name="want",
        value_name="want",
    )
