"""Top-level session-rooted intent frame surface."""

from __future__ import annotations

from gotta import session as sessionlib


def main(argv: list[str] | None = None) -> int:
    return sessionlib.run_charter_surface(
        argv,
        command_name="gotta want",
        description=(
            "Inspect or rewrite the canonical session intent frame. "
            "Bare invocation shows current contents; use --stdin or --from-file to rewrite. "
            "Pass --session peers/<peer> to target a linked peer session."
        ),
        surface_name="WANT.md",
        plugin_name="want",
        value_name="want",
    )
