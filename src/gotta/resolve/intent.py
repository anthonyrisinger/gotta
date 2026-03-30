"""Artifact intent and session-access policy for invocations."""

from __future__ import annotations

import os

from gotta.builtin import SurfaceArtifactIntent, get_surface
from gotta.content.model import CommonOptions
from gotta.resolve.read import resolve_read_target
from gotta.resolve.search import SearchRouteError, resolve_search_route


SUPPRESS_MATERIALIZATION_ENV = "GOTTA_SUPPRESS_MATERIALIZATION"
HELP_TOKENS = {"-h", "--help", "--help-all", "help-all"}
ArtifactIntent = SurfaceArtifactIntent


def materialization_enabled() -> bool:
    return os.environ.get(SUPPRESS_MATERIALIZATION_ENV) != "1"


def _generic_artifact_intent(plugin: str, argv: list[str]) -> ArtifactIntent:
    surface = get_surface(plugin)
    hook = getattr(surface, "artifact_intent", None)
    if hook is not None:
        return hook(argv)
    should_materialize = getattr(surface, "should_materialize", None)
    if should_materialize is not None:
        return "evidence" if bool(should_materialize(argv)) else "none"
    if plugin == "session":
        return "none"
    if not argv:
        return "none"
    subcommand = argv[0]
    if subcommand == "status":
        return "none"
    return "none" if subcommand.startswith("-") else "evidence"


def _artifact_intent(plugin: str, argv: list[str]) -> ArtifactIntent:
    if any(arg in HELP_TOKENS for arg in argv):
        return "none"
    return _generic_artifact_intent(plugin, argv)


def artifact_intent(plugin: str, argv: list[str]) -> ArtifactIntent:
    if plugin == "search":
        try:
            resolve_search_route(argv)
        except SearchRouteError:
            return "none"
        return "discovery"
    if plugin == "read":
        try:
            target = resolve_read_target(argv, CommonOptions())
        except SystemExit:
            return "none"
        return "evidence" if target.should_materialize else "none"
    return _artifact_intent(plugin, argv)


def session_access_mode(plugin: str, argv: list[str]) -> str:
    if plugin == "search":
        return "ambient" if artifact_intent(plugin, argv) != "none" else "none"
    if plugin == "read":
        return "ambient"
    return "ambient" if artifact_intent(plugin, argv) != "none" else "none"
