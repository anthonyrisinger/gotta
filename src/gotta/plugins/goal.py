"""Top-level session-rooted goal charter surface."""

from __future__ import annotations

from gotta.session import charter as session_charter


def session_access_mode(argv: list[str]) -> str:
    return session_charter.charter_session_access_mode(list(argv or []))


def main(argv: list[str] | None = None) -> int:
    return session_charter.run_charter_surface(
        argv,
        command_name="gotta goal",
        description=(
            "Inspect or rewrite the canonical session goal charter. "
            "Bare invocation shows all bound actor goal charters; use --stdin or --from-file to rewrite. "
            "Pass --actor <actor> to narrow inspection or target one actor for rewrite."
        ),
        surface_name="GOAL.md",
        plugin_name="goal",
        value_name="goal",
    )
