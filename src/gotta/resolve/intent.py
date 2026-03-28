"""Artifact intent and session-access policy for invocations."""

from __future__ import annotations

import os
from typing import Literal

from gotta.builtin import get_plugin
from gotta.content import CommonOptions
from gotta.resolve.read import resolve_read_target
from gotta.resolve.search import SearchRouteError, resolve_search_route


SUPPRESS_MATERIALIZATION_ENV = "GOTTA_SUPPRESS_MATERIALIZATION"
HELP_TOKENS = {"-h", "--help", "--help-all", "help-all"}
ArtifactIntent = Literal["none", "discovery", "evidence"]

CONTROL_SUBCOMMANDS = {
    "confluence": {"auth", "mcp", "status"},
    "grafana": {"auth", "status"},
    "gdrive": {"auth", "status"},
    "gdocs": {"auth", "status"},
    "gsheets": {"auth", "status"},
    "github": {"status"},
    "jira": {"auth", "mcp", "status"},
    "slack": {"auth", "mcp", "status", "workspaces"},
}

DISCOVERY_SUBCOMMANDS: dict[str, set[str]] = {
    "confluence": {"search", "cql"},
    "gdocs": {"search"},
    "gdrive": {"search"},
    "grafana": {"search"},
    "granola": {"list", "search", "search-transcript"},
    "gsheets": {"search"},
    "jira": {"search", "jql"},
    "slack": {"search"},
}

EVIDENCE_SUBCOMMANDS: dict[str, set[str]] = {
    "confluence": {"get"},
    "gdocs": {"get"},
    "gdrive": {"get"},
    "grafana": {"get"},
    "granola": {"get", "transcript"},
    "gsheets": {"get"},
    "jira": {"get"},
    "slack": {"get"},
}

NON_ARTIFACT_SUBCOMMANDS: dict[str, set[str]] = {
    "confluence": {
        "auth",
        "batch",
        "create-page",
        "find",
        "mcp",
        "render-markdown",
        "replace",
        "replace-section",
        "resolve-page",
        "status",
        "update-body",
    },
    "gdocs": {"auth", "status"},
    "gdrive": {"auth", "status"},
    "grafana": {"auth", "datasources", "query", "status"},
    "granola": {"export", "status"},
    "gsheets": {"auth", "status"},
    "jira": {
        "add-to-sprint",
        "auth",
        "comment",
        "create",
        "fields",
        "issue-types",
        "link",
        "link-types",
        "mcp",
        "projects",
        "sprints",
        "status",
        "transition",
        "transitions",
        "update",
    },
    "slack": {
        "auth",
        "list-channels",
        "list-users",
        "mcp",
        "schema",
        "sql",
        "status",
        "sync",
        "workspaces",
    },
}


def materialization_enabled() -> bool:
    return os.environ.get(SUPPRESS_MATERIALIZATION_ENV) != "1"


def _generic_artifact_intent(plugin: str, argv: list[str]) -> ArtifactIntent:
    spec = get_plugin(plugin)
    if spec and spec.should_materialize is not None:
        return "evidence" if bool(spec.should_materialize(argv)) else "none"
    if plugin == "session":
        return "none"
    if not argv:
        return "evidence"
    subcommand = argv[0]
    if subcommand == "status":
        return "none"
    return (
        "none" if subcommand in CONTROL_SUBCOMMANDS.get(plugin, set()) else "evidence"
    )


def _artifact_intent(plugin: str, argv: list[str]) -> ArtifactIntent:
    if any(arg in HELP_TOKENS for arg in argv):
        return "none"
    if plugin == "github":
        if not argv or argv[0] == "status":
            return "none"
        if argv[0] == "search":
            return "discovery"
        return "evidence"
    subcommand = argv[0] if argv else ""
    if subcommand in NON_ARTIFACT_SUBCOMMANDS.get(plugin, set()):
        return "none"
    if subcommand in DISCOVERY_SUBCOMMANDS.get(plugin, set()):
        return "discovery"
    if subcommand in EVIDENCE_SUBCOMMANDS.get(plugin, set()):
        return "evidence"
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
