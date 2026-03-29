"""Surface registry, discovery, and built-in registrations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache
import importlib
from importlib.metadata import entry_points
import inspect
import sys
from typing import Any, Literal, SupportsInt, cast

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
CapabilityName = Literal[
    "route",
    "read",
    "search",
    "capture",
    "project",
    "mutate",
    "auth",
    "status",
]

DEFAULT_BINDING_GROUP = "gotta.plugins"
ASK_BINDING_GROUP = "gotta.ask"
DEFAULT_PLUGIN_GROUP = DEFAULT_BINDING_GROUP
ASK_PLUGIN_GROUP = ASK_BINDING_GROUP


@dataclass(frozen=True, slots=True)
class CommandPath:
    tokens: tuple[str, ...]

    def __str__(self) -> str:
        return " ".join(self.tokens)


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    name: CapabilityName


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    name: str
    capabilities: tuple[CapabilitySpec, ...] = ()


@dataclass(frozen=True, slots=True)
class PackageSpec:
    name: str
    vendor_family: str | None = None


@dataclass(frozen=True, slots=True)
class SurfaceSpec:
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
    provider_bundle: ProviderBundle | None = None
    capabilities: tuple[CapabilitySpec, ...] = ()


PluginSpec = SurfaceSpec


@dataclass(frozen=True, slots=True)
class SurfaceBinding:
    name: str
    command_path: CommandPath
    package: PackageSpec
    surface: SurfaceSpec
    auth_profile: str | None = None
    defaults: tuple[tuple[str, str], ...] = ()

    @property
    def description(self) -> str:
        return self.surface.description

    @property
    def runner(self) -> Runner:
        return self.surface.runner

    @property
    def route_target(self) -> RouteTarget | None:
        return self.surface.route_target

    @property
    def route_priority(self) -> int:
        return self.surface.route_priority

    @property
    def should_materialize(self) -> ShouldMaterialize | None:
        return self.surface.should_materialize

    @property
    def session_access(self) -> SessionAccessMode | ResolveSessionAccess | None:
        return self.surface.session_access

    @property
    def invocation_locator(self) -> InvocationLocator | None:
        return self.surface.invocation_locator

    @property
    def canonical_locator(self) -> CanonicalLocator | None:
        return self.surface.canonical_locator

    @property
    def preferred_name(self) -> PreferredName | None:
        return self.surface.preferred_name

    @property
    def content_type(self) -> ContentType | None:
        return self.surface.content_type

    @property
    def capture(self) -> CaptureHook | None:
        return self.surface.capture

    @property
    def project(self) -> ProjectHook | None:
        return self.surface.project


CORE_PLUGIN_DISTS = {"gotta"}
CORE_PACKAGE = PackageSpec(name="gotta")


def _capabilities(*names: CapabilityName) -> tuple[CapabilitySpec, ...]:
    return tuple(CapabilitySpec(name) for name in names)


def _command_path(name: str, group: str) -> CommandPath:
    if group == ASK_BINDING_GROUP:
        return CommandPath(("ask", name))
    return CommandPath((name,))


def _binding_from_surface(
    name: str,
    surface: SurfaceSpec,
    *,
    group: str,
    package: PackageSpec,
) -> SurfaceBinding:
    if surface.name != name:
        surface = replace(surface, name=name)
    return SurfaceBinding(
        name=name,
        command_path=_command_path(name, group),
        package=package,
        surface=surface,
    )


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


def _coerce_binding(
    name: str,
    loaded: object,
    *,
    group: str,
    package: PackageSpec,
) -> SurfaceBinding:
    if isinstance(loaded, SurfaceBinding):
        binding = loaded
    elif isinstance(loaded, SurfaceSpec):
        binding = _binding_from_surface(name, loaded, group=group, package=package)
    elif callable(loaded) and _is_zero_arg_factory(loaded):
        return _coerce_binding(name, loaded(), group=group, package=package)
    elif callable(loaded):
        binding = _binding_from_surface(
            name,
            SurfaceSpec(name=name, description="", runner=_coerced_runner(loaded)),
            group=group,
            package=package,
        )
    else:
        runner = getattr(loaded, "main", None)
        if callable(runner):
            binding = _binding_from_surface(
                name,
                SurfaceSpec(
                    name=name,
                    description="",
                    runner=_coerced_runner(runner),
                ),
                group=group,
                package=package,
            )
        else:
            raise RuntimeError(
                "surface entry point must load a SurfaceBinding, SurfaceSpec, "
                "zero-arg factory, callable runner, or an object with main()"
            )
    if binding.name != name:
        binding = replace(binding, name=name)
    if binding.command_path != _command_path(name, group):
        binding = replace(binding, command_path=_command_path(name, group))
    if binding.package.name != package.name:
        binding = replace(binding, package=package)
    if binding.surface.name != name:
        binding = replace(binding, surface=replace(binding.surface, name=name))
    return binding


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
def discovered_bindings(
    group: str = DEFAULT_BINDING_GROUP,
) -> dict[str, SurfaceBinding]:
    discovered: dict[str, SurfaceBinding] = (
        _builtin_core_bindings() if group == DEFAULT_BINDING_GROUP else {}
    )
    for entry_point in sorted(entry_points(group=group), key=_shadow_priority):
        if (
            group == DEFAULT_BINDING_GROUP
            and _dist_name(entry_point) in CORE_PLUGIN_DISTS
        ):
            # Source-defined core bindings are canonical. When running from source,
            # installed core metadata may lag behind and still advertise removed
            # bindings; skip all core entry-point metadata instead of warning on it.
            continue
        try:
            binding = _coerce_binding(
                entry_point.name,
                entry_point.load(),
                group=group,
                package=PackageSpec(name=_dist_name(entry_point) or entry_point.name),
            )
        except Exception as exc:
            # Stale or broken external entry-point metadata must not break the core CLI.
            _warn_broken_entry_point(entry_point, group, exc)
            continue
        discovered[binding.name] = binding
    return discovered


def discovered_surfaces(group: str = DEFAULT_BINDING_GROUP) -> dict[str, SurfaceSpec]:
    return {
        name: binding.surface for name, binding in discovered_bindings(group).items()
    }


def discovered_plugins(group: str = DEFAULT_PLUGIN_GROUP) -> dict[str, PluginSpec]:
    return discovered_surfaces(group)


def clear_binding_cache() -> None:
    discovered_bindings.cache_clear()


def clear_plugin_cache() -> None:
    clear_binding_cache()


def get_binding(
    name: str,
    *,
    group: str = DEFAULT_BINDING_GROUP,
) -> SurfaceBinding | None:
    return discovered_bindings(group).get(name)


def get_surface(
    name: str,
    *,
    group: str = DEFAULT_BINDING_GROUP,
) -> SurfaceSpec | None:
    binding = get_binding(name, group=group)
    return binding.surface if binding is not None else None


def get_plugin(name: str, *, group: str = DEFAULT_PLUGIN_GROUP) -> PluginSpec | None:
    return get_surface(name, group=group)


def available_bindings(*, group: str = DEFAULT_BINDING_GROUP) -> list[str]:
    return sorted(discovered_bindings(group))


def available_surfaces(*, group: str = DEFAULT_BINDING_GROUP) -> list[str]:
    return available_bindings(group=group)


def available_plugins(*, group: str = DEFAULT_PLUGIN_GROUP) -> list[str]:
    return available_surfaces(group=group)


def iter_bindings(*, group: str = DEFAULT_BINDING_GROUP) -> list[SurfaceBinding]:
    return [
        discovered_bindings(group)[name] for name in available_bindings(group=group)
    ]


def iter_surfaces(*, group: str = DEFAULT_BINDING_GROUP) -> list[SurfaceSpec]:
    return [binding.surface for binding in iter_bindings(group=group)]


def iter_plugins(*, group: str = DEFAULT_PLUGIN_GROUP) -> list[PluginSpec]:
    return iter_surfaces(group=group)


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
                return int(cast(Callable[[str], int | str], die)(str(exc)))
            raise

    return run


def _coerced_runner(callback: Callable[..., object]) -> Runner:
    def run(argv: list[str]) -> int:
        result = cast(Callable[[list[str]], object], callback)(argv)
        if isinstance(result, int):
            return result
        if isinstance(result, str):
            return int(result)
        return int(cast(SupportsInt, result))

    return run


def _module_attr(module_name: str, attr: str):
    def call(*args):
        module = importlib.import_module(module_name)
        return getattr(module, attr)(*args)

    return call


def _artifact_session_access(plugin_name: str):
    def resolve(argv: list[str]) -> SessionAccessMode:
        module = importlib.import_module("gotta.resolve.intent")
        return module.session_access_mode(plugin_name, argv)

    return resolve


def _artifact_should_materialize(plugin_name: str):
    def resolve(argv: list[str]) -> bool:
        module = importlib.import_module("gotta.resolve.invoke")
        return bool(module.should_materialize(plugin_name, argv))

    return resolve


def _provider_bundle(
    name: str,
    *capabilities: CapabilityName,
) -> ProviderBundle:
    return ProviderBundle(name=name, capabilities=_capabilities(*capabilities))


def _core_binding(
    name: str,
    description: str,
    runner: Runner,
    *,
    route_target: RouteTarget | None = None,
    route_priority: int = 100,
    should_materialize: ShouldMaterialize | None = None,
    session_access: SessionAccessMode | ResolveSessionAccess | None = None,
    invocation_locator: InvocationLocator | None = None,
    canonical_locator: CanonicalLocator | None = None,
    preferred_name: PreferredName | None = None,
    content_type: ContentType | None = None,
    capture: CaptureHook | None = None,
    project: ProjectHook | None = None,
    provider_bundle: ProviderBundle | None = None,
    capabilities: tuple[CapabilitySpec, ...] = (),
) -> SurfaceBinding:
    return SurfaceBinding(
        name=name,
        command_path=_command_path(name, DEFAULT_BINDING_GROUP),
        package=CORE_PACKAGE,
        surface=SurfaceSpec(
            name=name,
            description=description,
            runner=runner,
            route_target=route_target,
            route_priority=route_priority,
            should_materialize=should_materialize,
            session_access=session_access,
            invocation_locator=invocation_locator,
            canonical_locator=canonical_locator,
            preferred_name=preferred_name,
            content_type=content_type,
            capture=capture,
            project=project,
            provider_bundle=provider_bundle,
            capabilities=capabilities,
        ),
    )


def confluence_plugin() -> SurfaceBinding:
    return _core_binding(
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
        provider_bundle=_provider_bundle(
            "confluence",
            "route",
            "read",
            "search",
            "capture",
            "project",
            "mutate",
            "auth",
            "status",
        ),
        capabilities=_capabilities(
            "route",
            "read",
            "search",
            "capture",
            "project",
            "mutate",
            "auth",
            "status",
        ),
    )


def config_plugin() -> SurfaceBinding:
    return _core_binding(
        name="config",
        description="persist durable provider defaults and guide provider readiness",
        runner=_runner("gotta.plugins.config"),
        should_materialize=lambda argv: False,
        session_access="none",
    )


def ask_plugin() -> SurfaceBinding:
    return _core_binding(
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


def read_plugin() -> SurfaceBinding:
    return _core_binding(
        name="read",
        description="acquire one target through the right retrieval surface with session-aware storage",
        runner=_runner("gotta.plugins.read"),
        should_materialize=_module_attr("gotta.resolve.read", "should_materialize"),
        session_access="ambient",
        canonical_locator=_module_attr("gotta.resolve.read", "canonical_locator"),
        preferred_name=_module_attr("gotta.resolve.read", "preferred_name"),
        capture=_module_attr("gotta.plugins.read", "capture"),
        project=_module_attr("gotta.plugins.read", "project"),
        capabilities=_capabilities("read", "capture", "project"),
    )


def oops_plugin() -> SurfaceBinding:
    return _core_binding(
        name="oops",
        description="capture and inspect durable session friction",
        runner=_runner("gotta.plugins.oops"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.oops", "session_access_mode"),
    )


def todo_plugin() -> SurfaceBinding:
    return _core_binding(
        name="todo",
        description="inspect and mutate the canonical session checklist",
        runner=_runner("gotta.plugins.todo"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.todo", "session_access_mode"),
    )


def want_plugin() -> SurfaceBinding:
    return _core_binding(
        name="want",
        description="inspect or rewrite the canonical session intent frame",
        runner=_runner("gotta.plugins.want"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.want", "session_access_mode"),
    )


def logs_plugin() -> SurfaceBinding:
    return _core_binding(
        name="logs",
        description="inspect and mutate the canonical session procedural trace",
        runner=_runner("gotta.plugins.logs.main"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.logs.parse", "session_access_mode"),
    )


def goal_plugin() -> SurfaceBinding:
    return _core_binding(
        name="goal",
        description="inspect or rewrite the canonical session goal charter",
        runner=_runner("gotta.plugins.goal"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.goal", "session_access_mode"),
    )


def notes_plugin() -> SurfaceBinding:
    return _core_binding(
        name="notes",
        description="inspect and mutate canonical actor-authored notes inside the active session",
        runner=_runner("gotta.plugins.notes.main"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.notes.parse", "session_access_mode"),
    )


def actor_plugin() -> SurfaceBinding:
    return _core_binding(
        name="actor",
        description="configure, launch, and disposition sibling actor sessions inside the active session",
        runner=_runner("gotta.plugins.actor"),
        should_materialize=lambda argv: False,
        session_access=_module_attr("gotta.plugins.actor", "session_access_mode"),
    )


def gdocs_plugin() -> SurfaceBinding:
    return _core_binding(
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
        provider_bundle=_provider_bundle(
            "gdocs",
            "route",
            "read",
            "search",
            "capture",
            "project",
            "auth",
            "status",
        ),
        capabilities=_capabilities(
            "route",
            "read",
            "search",
            "capture",
            "project",
            "auth",
            "status",
        ),
    )


def grafana_plugin() -> SurfaceBinding:
    return _core_binding(
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
        provider_bundle=_provider_bundle(
            "grafana",
            "route",
            "read",
            "search",
            "capture",
            "project",
            "status",
        ),
        capabilities=_capabilities(
            "route",
            "read",
            "search",
            "capture",
            "project",
            "status",
        ),
    )


def granola_plugin() -> SurfaceBinding:
    return _core_binding(
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
        provider_bundle=_provider_bundle(
            "granola",
            "route",
            "read",
            "search",
            "capture",
            "project",
            "auth",
            "status",
        ),
        capabilities=_capabilities(
            "route",
            "read",
            "search",
            "capture",
            "project",
            "auth",
            "status",
        ),
    )


def gsheets_plugin() -> SurfaceBinding:
    return _core_binding(
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
        provider_bundle=_provider_bundle(
            "gsheets",
            "route",
            "read",
            "search",
            "capture",
            "project",
            "auth",
            "status",
        ),
        capabilities=_capabilities(
            "route",
            "read",
            "search",
            "capture",
            "project",
            "auth",
            "status",
        ),
    )


def gdrive_plugin() -> SurfaceBinding:
    return _core_binding(
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
        provider_bundle=_provider_bundle(
            "gdrive",
            "route",
            "read",
            "search",
            "capture",
            "project",
            "auth",
            "status",
        ),
        capabilities=_capabilities(
            "route",
            "read",
            "search",
            "capture",
            "project",
            "auth",
            "status",
        ),
    )


def github_plugin() -> SurfaceBinding:
    return _core_binding(
        name="github",
        description="render common GitHub URLs through the GitHub CLI",
        runner=_runner("gotta.plugins.github.main"),
        route_target=_module_attr("gotta.plugins.github.route", "route_target"),
        route_priority=10,
        session_access=_artifact_session_access("github"),
        canonical_locator=_module_attr(
            "gotta.plugins.github.parse", "canonical_locator"
        ),
        preferred_name=_module_attr("gotta.plugins.github.parse", "preferred_name"),
        capture=_module_attr("gotta.plugins.github.main", "capture"),
        project=_module_attr("gotta.plugins.github.main", "project"),
        provider_bundle=_provider_bundle(
            "github",
            "route",
            "read",
            "search",
            "capture",
            "project",
            "mutate",
            "auth",
            "status",
        ),
        capabilities=_capabilities(
            "route",
            "read",
            "search",
            "capture",
            "project",
            "mutate",
            "auth",
            "status",
        ),
    )


def jira_plugin() -> SurfaceBinding:
    return _core_binding(
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
        provider_bundle=_provider_bundle(
            "jira",
            "route",
            "read",
            "search",
            "capture",
            "project",
            "mutate",
            "auth",
            "status",
        ),
        capabilities=_capabilities(
            "route",
            "read",
            "search",
            "capture",
            "project",
            "mutate",
            "auth",
            "status",
        ),
    )


def session_plugin() -> SurfaceBinding:
    return _core_binding(
        name="session",
        description="inspect the active session-rooted content context",
        runner=_runner("gotta.plugins.session.main"),
        should_materialize=lambda argv: False,
        session_access=_module_attr(
            "gotta.plugins.session.parse", "session_access_mode"
        ),
    )


def slack_plugin() -> SurfaceBinding:
    return _core_binding(
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
        provider_bundle=_provider_bundle(
            "slack",
            "route",
            "read",
            "search",
            "capture",
            "project",
            "auth",
            "status",
        ),
        capabilities=_capabilities(
            "route",
            "read",
            "search",
            "capture",
            "project",
            "auth",
            "status",
        ),
    )


def search_plugin() -> SurfaceBinding:
    return _core_binding(
        name="search",
        description="route plain-text remote discovery through provider-native search surfaces",
        runner=_runner("gotta.plugins.search"),
        should_materialize=_artifact_should_materialize("search"),
        session_access=_artifact_session_access("search"),
        canonical_locator=_module_attr("gotta.plugins.search", "canonical_locator"),
        preferred_name=_module_attr("gotta.plugins.search", "preferred_name"),
        content_type=_module_attr("gotta.plugins.search", "content_type"),
        capture=_module_attr("gotta.plugins.search", "capture"),
        project=_module_attr("gotta.plugins.search", "project"),
        capabilities=_capabilities("search", "capture", "project"),
    )


def _builtin_core_bindings() -> dict[str, SurfaceBinding]:
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
    return {binding.name: binding for binding in (factory() for factory in factories)}
