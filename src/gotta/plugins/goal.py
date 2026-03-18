"""Top-level session-rooted goal charter surface."""

from __future__ import annotations

from gotta import session as sessionlib


def main(argv: list[str] | None = None) -> int:
    return sessionlib.run_charter_surface(
        argv,
        command_name="gotta goal",
        description=(
            "Inspect or rewrite the canonical session goal charter. "
            "Bare invocation shows current contents; use --stdin or --from-file to rewrite. "
            "Pass --session peers/<peer> to target a linked peer session."
        ),
        surface_name="GOAL.md",
        plugin_name="goal",
        value_name="goal",
    )
