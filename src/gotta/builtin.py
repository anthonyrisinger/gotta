"""Built-in plugin contract, discovery, and registrations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
import importlib
from importlib.metadata import entry_points
import inspect
import sys
from typing import Any, Literal

from gotta.capture import Capture


Runner = Callable[[list[str]], int]
RouteTarget = Callable[[str], list[str] | None]
ShouldMaterialize = Callable[[list[str]], bool]
SessionAccessMode = Literal["none", "read", "write", "ambient"]
ResolveSessionAccess = Callable[[list[str]], SessionAccessMode]
InvocationLocator = Callable[[list[str]], str]
CanonicalLocator = Callable[[list[str]], str]
PreferredName = Callable[[list[str], Any], str]
ContentType = Callable[[list[str], str], str]
CaptureHook = Callable[[list[str], Any], Capture]
ProjectHook = Callable[[list[str], Capture], bytes]

DEFAULT_PLUGIN_GROUP = "gotta.plugins"
ASK_PLUGIN_GROUP = "gotta.ask"


@dataclass(frozen=True, slots=True)
class PluginSpec:
    name: str
    description: str
    runner: Runner
    route_target: RouteTarget | None = None
    route_priority: int = 100
    should_materialize: ShouldMaterialize | None = None
    session_access: SessionAccessMode | ResolveSessionAccess | None = None
    invocation_locator: InvocationLocator | None = None
    canonical_locator: CanonicalLocator | None = None
    preferred_name: PreferredName | None = None
    content_type: ContentType | None = None
    capture: CaptureHook | None = None
    project: ProjectHook | None = None


CORE_PLUGIN_DISTS = {"gotta"}


def _is_zero_arg_factory(value: Callable[..., object]) -> bool:
    signature = inspect.signature(value)
    required = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.default is inspect.Signature.empty
        and parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    return not required


def _coerce_plugin(name: str, loaded: object) -> PluginSpec:
    if isinstance(loaded, PluginSpec):
        return loaded
    if callable(loaded) and _is_zero_arg_factory(loaded):
        produced = loaded()
        if isinstance(produced, PluginSpec):
            return produced
    if callable(loaded):
        return PluginSpec(name=name, description="", runner=loaded)
    runner = getattr(loaded, "main", None)
    if callable(runner):
        return PluginSpec(name=name, description="", runner=runner)
    raise RuntimeError(
        f"plugin '{name}' entry point must load a PluginSpec, a factory returning "
        "PluginSpec, a callable runner, or an object with main()"
    )


def _dist_name(entry_point: Any) -> str:
    dist = getattr(entry_point, "dist", None)
    name = getattr(dist, "name", "")
    return str(name or "")


def _shadow_priority(entry_point: Any) -> tuple[int, str, str]:
    dist_name = _dist_name(entry_point)
    is_external = 1 if dist_name not in CORE_PLUGIN_DISTS else 0
    return (is_external, dist_name, str(getattr(entry_point, "value", "")))


def _warn_broken_entry_point(entry_point: Any, group: str, exc: Exception) -> None:
    dist_name = _dist_name(entry_point) or "<unknown-dist>"
    print(
        "warning: ignoring broken plugin entry point "
        f"{group}:{entry_point.name} from {dist_name}: "
        f"{type(exc).__name__}: {exc}",
        file=sys.stderr,
    )


@cache
def discovered_plugins(group: str = DEFAULT_PLUGIN_GROUP) -> dict[str, PluginSpec]:
    discovered: dict[str, PluginSpec] = (
        _builtin_core_plugins() if group == DEFAULT_PLUGIN_GROUP else {}
    )
    for entry_point in sorted(entry_points(group=group), key=_shadow_priority):
        if (
            group == DEFAULT_PLUGIN_GROUP
            and _dist_name(entry_point) in CORE_PLUGIN_DISTS
        ):
            # Source-defined core plugins are canonical. When running from source,
            # installed core metadata may lag behind and still advertise removed
            # plugins; skip all core entry-point metadata instead of warning on it.
            continue
        try:
            plugin = _coerce_plugin(entry_point.name, entry_point.load())
        except Exception as exc:
            # Stale or broken external entry-point metadata must not break the core CLI.
            _warn_broken_entry_point(entry_point, group, exc)
            continue
        if plugin.name != entry_point.name:
            plugin = PluginSpec(
                name=entry_point.name,
                description=plugin.description,
                runner=plugin.runner,
                route_target=plugin.route_target,
                route_priority=plugin.route_priority,
                should_materialize=plugin.should_materialize,
                session_access=plugin.session_access,
                invocation_locator=plugin.invocation_locator,
                canonical_locator=plugin.canonical_locator,
                preferred_name=plugin.preferred_name,
                content_type=plugin.content_type,
                capture=plugin.capture,
                project=plugin.project,
            )
        discovered[plugin.name] = plugin
    return discovered


def clear_plugin_cache() -> None:
    discovered_plugins.cache_clear()


def get_plugin(name: str, *, group: str = DEFAULT_PLUGIN_GROUP) -> PluginSpec | None:
    return discovered_plugins(group).get(name)


def available_plugins(*, group: str = DEFAULT_PLUGIN_GROUP) -> list[str]:
    return sorted(discovered_plugins(group))


def iter_plugins(*, group: str = DEFAULT_PLUGIN_GROUP) -> list[PluginSpec]:
    return [discovered_plugins(group)[name] for name in available_plugins(group=group)]


def _runner(module_name: str):
    def run(argv: list[str]) -> int:
        module = importlib.import_module(module_name)
        tool_error = getattr(module, "ToolError", None)
        die = getattr(module, "die", None)
        try:
            return int(module.main(argv))
        except BrokenPipeError:
            raise
        except Exception as exc:
            if (
                isinstance(tool_error, type)
                and isinstance(exc, tool_error)
                and callable(die)
            ):
                return int(die(str(exc)))
            raise

    return run


def _module_attr(module_name: str, attr: str):
    def call(*args):
        module = importlib.import_module(module_name)
        return getattr(module, attr)(*args)

    return call


def _artifact_session_access(plugin_name: str):
    def resolve(argv: list[str]) -> SessionAccessMode:
        module = importlib.import_module("gotta.invocation")
        return module.session_access_mode(plugin_name, argv)

    return resolve


def confluence_plugin() -> PluginSpec:
    return PluginSpec(
        name="confluence",
        description="read and edit Confluence pages with durable OAuth-backed access",
        runner=_runner("gotta.plugins.confluence"),
        route_target=_module_attr("gotta.plugins.confluence", "route_target"),
        route_priority=20,
        session_access=_artifact_session_access("confluence"),
        canonical_locator=_module_attr("gotta.plugins.confluence", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.confluence", "preferred_name"),
        capture=_module_attr("gotta.plugins.confluence", "capture"),
        project=_module_attr("gotta.plugins.confluence", "project"),
    )


def config_plugin() -> PluginSpec:
    return PluginSpec(
        name="config",
        description="persist durable provider defaults and guide provider readiness",
        runner=_runner("gotta.plugins.config"),
        should_materialize=lambda argv: False,
        session_access="none",
    )


def ask_plugin() -> PluginSpec:
    return PluginSpec(
        name="ask",
        description="dispatch to installed ask-family extensions",
        runner=_runner("gotta.plugins.ask"),
        should_materialize=_module_attr("gotta.plugins.ask", "should_materialize"),
        session_access="none",
        invocation_locator=_module_attr("gotta.plugins.ask", "invocation_locator"),
        canonical_locator=_module_attr("gotta.plugins.ask", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.ask", "preferred_name"),
        content_type=_module_attr("gotta.plugins.ask", "content_type"),
    )


def read_plugin() -> PluginSpec:
    return PluginSpec(
        name="read",
        description="acquire one target through the right retrieval surface with session-aware storage",
        runner=_runner("gotta.plugins.read"),
        should_materialize=_module_attr("gotta.target", "should_materialize"),
        session_access="ambient",
        canonical_locator=_module_attr("gotta.target", "canonical_locator"),
        preferred_name=_module_attr("gotta.target", "preferred_name"),
        capture=_module_attr("gotta.plugins.read", "capture"),
        project=_module_attr("gotta.plugins.read", "project"),
    )


def oops_plugin() -> PluginSpec:
    return PluginSpec(
        name="oops",
        description="capture and inspect durable session friction",
        runner=_runner("gotta.plugins.oops"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.oops", "session_access_mode"),
    )


def todo_plugin() -> PluginSpec:
    return PluginSpec(
        name="todo",
        description="inspect and mutate the canonical session checklist",
        runner=_runner("gotta.plugins.todo"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.todo", "session_access_mode"),
    )


def want_plugin() -> PluginSpec:
    return PluginSpec(
        name="want",
        description="inspect or rewrite the canonical session intent frame",
        runner=_runner("gotta.plugins.want"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.want", "session_access_mode"),
    )


def logs_plugin() -> PluginSpec:
    return PluginSpec(
        name="logs",
        description="inspect and mutate the canonical session procedural trace",
        runner=_runner("gotta.plugins.logs"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.logs", "session_access_mode"),
    )


def goal_plugin() -> PluginSpec:
    return PluginSpec(
        name="goal",
        description="inspect or rewrite the canonical session goal charter",
        runner=_runner("gotta.plugins.goal"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.goal", "session_access_mode"),
    )


def notes_plugin() -> PluginSpec:
    return PluginSpec(
        name="notes",
        description="inspect and mutate canonical actor-authored notes inside the active session",
        runner=_runner("gotta.plugins.notes"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.notes", "session_access_mode"),
    )


def actor_plugin() -> PluginSpec:
    return PluginSpec(
        name="actor",
        description="configure, launch, and disposition sibling actor sessions inside the active session",
        runner=_runner("gotta.plugins.actor"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.actor", "session_access_mode"),
    )


def gdocs_plugin() -> PluginSpec:
    return PluginSpec(
        name="gdocs",
        description="read and search Google Docs through the Drive/Docs APIs",
        runner=_runner("gotta.plugins.gdocs"),
        route_target=_module_attr("gotta.plugins.gdocs", "route_target"),
        route_priority=50,
        session_access=_artifact_session_access("gdocs"),
        canonical_locator=_module_attr("gotta.plugins.gdocs", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.gdocs", "preferred_name"),
        capture=_module_attr("gotta.plugins.gdocs", "capture"),
        project=_module_attr("gotta.plugins.gdocs", "project"),
    )


def grafana_plugin() -> PluginSpec:
    return PluginSpec(
        name="grafana",
        description="read-only Grafana dashboard discovery through the HTTP API",
        runner=_runner("gotta.plugins.grafana"),
        route_target=_module_attr("gotta.plugins.grafana", "route_target"),
        route_priority=70,
        session_access=_artifact_session_access("grafana"),
        canonical_locator=_module_attr("gotta.plugins.grafana", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.grafana", "preferred_name"),
        capture=_module_attr("gotta.plugins.grafana", "capture"),
        project=_module_attr("gotta.plugins.grafana", "project"),
    )


def granola_plugin() -> PluginSpec:
    return PluginSpec(
        name="granola",
        description="read personal Granola notes through the local desktop session",
        runner=_runner("gotta.plugins.granola"),
        route_target=_module_attr("gotta.plugins.granola", "route_target"),
        route_priority=65,
        session_access=_artifact_session_access("granola"),
        canonical_locator=_module_attr("gotta.plugins.granola", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.granola", "preferred_name"),
        capture=_module_attr("gotta.plugins.granola", "capture"),
        project=_module_attr("gotta.plugins.granola", "project"),
    )


def gsheets_plugin() -> PluginSpec:
    return PluginSpec(
        name="gsheets",
        description="read and search Google Sheets through the Sheets/Drive APIs",
        runner=_runner("gotta.plugins.gsheets"),
        route_target=_module_attr("gotta.plugins.gsheets", "route_target"),
        route_priority=55,
        session_access=_artifact_session_access("gsheets"),
        canonical_locator=_module_attr("gotta.plugins.gsheets", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.gsheets", "preferred_name"),
        capture=_module_attr("gotta.plugins.gsheets", "capture"),
        project=_module_attr("gotta.plugins.gsheets", "project"),
    )


def gdrive_plugin() -> PluginSpec:
    return PluginSpec(
        name="gdrive",
        description="inspect and fetch Google Drive files through the Drive API",
        runner=_runner("gotta.plugins.gdrive"),
        route_target=_module_attr("gotta.plugins.gdrive", "route_target"),
        route_priority=60,
        session_access=_artifact_session_access("gdrive"),
        canonical_locator=_module_attr("gotta.plugins.gdrive", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.gdrive", "preferred_name"),
        capture=_module_attr("gotta.plugins.gdrive", "capture"),
        project=_module_attr("gotta.plugins.gdrive", "project"),
    )


def github_plugin() -> PluginSpec:
    return PluginSpec(
        name="github",
        description="render common GitHub URLs through the GitHub CLI",
        runner=_runner("gotta.plugins.github"),
        route_target=_module_attr("gotta.plugins.github", "route_target"),
        route_priority=10,
        session_access=_artifact_session_access("github"),
        canonical_locator=_module_attr("gotta.plugins.github", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.github", "preferred_name"),
        capture=_module_attr("gotta.plugins.github", "capture"),
        project=_module_attr("gotta.plugins.github", "project"),
    )


def jira_plugin() -> PluginSpec:
    return PluginSpec(
        name="jira",
        description="discover, read, and author Jira issues through Atlassian OAuth",
        runner=_runner("gotta.plugins.jira"),
        route_target=_module_attr("gotta.plugins.jira", "route_target"),
        route_priority=30,
        session_access=_artifact_session_access("jira"),
        canonical_locator=_module_attr("gotta.plugins.jira", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.jira", "preferred_name"),
        capture=_module_attr("gotta.plugins.jira", "capture"),
        project=_module_attr("gotta.plugins.jira", "project"),
    )


def session_plugin() -> PluginSpec:
    return PluginSpec(
        name="session",
        description="inspect the active session-rooted content context",
        runner=_runner("gotta.plugins.session.main"),
        should_materialize=lambda argv: False,
        session_access=_module_attr(
            "gotta.plugins.session.parse", "session_access_mode"
        ),
    )


def slack_plugin() -> PluginSpec:
    return PluginSpec(
        name="slack",
        description="query the bounded local Slack archive and explicit sync surface",
        runner=_runner("gotta.plugins.slack"),
        route_target=_module_attr("gotta.plugins.slack", "route_target"),
        route_priority=40,
        session_access=_artifact_session_access("slack"),
        canonical_locator=_module_attr("gotta.plugins.slack", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.slack", "preferred_name"),
        capture=_module_attr("gotta.plugins.slack", "capture"),
        project=_module_attr("gotta.plugins.slack", "project"),
    )


def search_plugin() -> PluginSpec:
    return PluginSpec(
        name="search",
        description="route plain-text remote discovery through provider-native search surfaces",
        runner=_runner("gotta.plugins.search"),
        should_materialize=_module_attr("gotta.invocation", "should_materialize"),
        session_access=_artifact_session_access("search"),
        canonical_locator=_module_attr("gotta.plugins.search", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.search", "preferred_name"),
        content_type=_module_attr("gotta.plugins.search", "content_type"),
        capture=_module_attr("gotta.plugins.search", "capture"),
        project=_module_attr("gotta.plugins.search", "project"),
    )


def _builtin_core_plugins() -> dict[str, PluginSpec]:
    factories = (
        ask_plugin,
        config_plugin,
        confluence_plugin,
        grafana_plugin,
        gdocs_plugin,
        gdrive_plugin,
        goal_plugin,
        granola_plugin,
        github_plugin,
        gsheets_plugin,
        jira_plugin,
        logs_plugin,
        notes_plugin,
        oops_plugin,
        actor_plugin,
        read_plugin,
        search_plugin,
        session_plugin,
        slack_plugin,
        todo_plugin,
        want_plugin,
    )
    return {plugin.name: plugin for plugin in (factory() for factory in factories)}
