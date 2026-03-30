"""Canonical locator derivation for provider invocations."""

from __future__ import annotations

from gotta.builtin import get_surface


def invocation_locator(plugin: str, argv: list[str]) -> str:
    surface = get_surface(plugin)
    if surface and surface.invocation_locator is not None:
        return surface.invocation_locator(argv)
    if not argv:
        return plugin
    return " ".join(argv)


def canonical_locator(plugin: str, argv: list[str]) -> str:
    surface = get_surface(plugin)
    if surface and surface.canonical_locator is not None:
        return surface.canonical_locator(argv)
    return f"{plugin}:{invocation_locator(plugin, argv)}"
