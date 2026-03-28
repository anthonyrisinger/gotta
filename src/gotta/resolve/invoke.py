"""Invocation resolution and metadata derivation."""

from __future__ import annotations

from dataclasses import dataclass

from gotta.content.model import CommonOptions
from gotta.resolve.canon import canonical_locator as derive_canonical_locator
from gotta.resolve.canon import invocation_locator as derive_invocation_locator
from gotta.resolve.intent import (
    ArtifactIntent,
    HELP_TOKENS,
    artifact_intent,
    materialization_enabled,
)
from gotta.resolve.name import infer_content_type as derive_content_type
from gotta.resolve.name import preferred_name as derive_preferred_name
from gotta.resolve.read import resolve_read_target
from gotta.resolve.search import SearchRouteError, resolve_search_route


@dataclass(frozen=True, slots=True)
class ResolvedInvocation:
    entry_plugin: str
    entry_argv: list[str]
    resolved_plugin: str
    resolved_argv: list[str]
    canonical_locator: str
    preferred_name: str
    content_type: str
    artifact_intent: ArtifactIntent
    artifact_kind: str
    should_materialize: bool
    provider: str


def _artifact_kind(intent: ArtifactIntent) -> str:
    return "" if intent == "none" else intent


def resolve_invocation(
    plugin: str,
    argv: list[str],
    options: CommonOptions | None = None,
) -> ResolvedInvocation:
    options = options or CommonOptions()
    entry_plugin = plugin
    entry_argv = list(argv)
    resolved_plugin = plugin
    resolved_argv = list(argv)
    if any(arg in HELP_TOKENS for arg in argv):
        preferred = options.save_as or f"{plugin}.txt"
        return ResolvedInvocation(
            entry_plugin=entry_plugin,
            entry_argv=entry_argv,
            resolved_plugin=plugin,
            resolved_argv=resolved_argv,
            canonical_locator=f"{plugin}:help",
            preferred_name=preferred,
            content_type=derive_content_type(plugin, argv, preferred),
            artifact_intent="none",
            artifact_kind="",
            should_materialize=False,
            provider=plugin,
        )
    if plugin == "read":
        try:
            resolved_target = resolve_read_target(argv, options)
        except SystemExit:
            target = next((item for item in argv if not item.startswith("-")), "")
            preferred = options.save_as or "read.txt"
            return ResolvedInvocation(
                entry_plugin=entry_plugin,
                entry_argv=entry_argv,
                resolved_plugin="read",
                resolved_argv=resolved_argv,
                canonical_locator=target or "read",
                preferred_name=preferred,
                content_type=derive_content_type("read", resolved_argv, preferred),
                artifact_intent="none",
                artifact_kind="",
                should_materialize=False,
                provider="read",
            )
        if resolved_target.routed_plugin is not None:
            resolved_plugin = resolved_target.routed_plugin
            resolved_argv = list(resolved_target.routed_argv)
        provider = resolved_plugin
        canonical = resolved_target.canonical_locator
        preferred = resolved_target.preferred_name
        content_type = derive_content_type(resolved_plugin, resolved_argv, preferred)
        if resolved_target.routed_plugin is not None:
            routed_intent = artifact_intent(resolved_plugin, resolved_argv)
            intent: ArtifactIntent = (
                routed_intent if resolved_target.should_materialize else "none"
            )
        else:
            intent = "evidence" if resolved_target.should_materialize else "none"
        return ResolvedInvocation(
            entry_plugin=entry_plugin,
            entry_argv=entry_argv,
            resolved_plugin=resolved_plugin,
            resolved_argv=resolved_argv,
            canonical_locator=canonical,
            preferred_name=preferred,
            content_type=content_type,
            artifact_intent=intent,
            artifact_kind=_artifact_kind(intent),
            should_materialize=intent != "none" and materialization_enabled(),
            provider=provider,
        )
    if plugin == "search":
        try:
            search_route = resolve_search_route(argv)
        except SearchRouteError:
            preferred = options.save_as or "search.md"
            return ResolvedInvocation(
                entry_plugin=entry_plugin,
                entry_argv=entry_argv,
                resolved_plugin="search",
                resolved_argv=resolved_argv,
                canonical_locator="search",
                preferred_name=preferred,
                content_type="text/markdown",
                artifact_intent="none",
                artifact_kind="",
                should_materialize=False,
                provider="search",
            )
        canonical = derive_canonical_locator(
            search_route.provider,
            search_route.provider_argv,
        )
        preferred = derive_preferred_name(
            search_route.provider,
            search_route.provider_argv,
            options,
        )
        content_type = derive_content_type(
            search_route.provider,
            search_route.provider_argv,
            preferred,
        )
        intent: ArtifactIntent = "discovery"
        return ResolvedInvocation(
            entry_plugin=entry_plugin,
            entry_argv=entry_argv,
            resolved_plugin=search_route.provider,
            resolved_argv=search_route.provider_argv,
            canonical_locator=canonical,
            preferred_name=preferred,
            content_type=content_type,
            artifact_intent=intent,
            artifact_kind=_artifact_kind(intent),
            should_materialize=materialization_enabled(),
            provider=search_route.provider,
        )
    canonical = derive_canonical_locator(plugin, argv)
    preferred = derive_preferred_name(plugin, argv, options)
    content_type = derive_content_type(plugin, argv, preferred)
    intent = artifact_intent(plugin, argv)
    return ResolvedInvocation(
        entry_plugin=entry_plugin,
        entry_argv=entry_argv,
        resolved_plugin=plugin,
        resolved_argv=argv,
        canonical_locator=canonical,
        preferred_name=preferred,
        content_type=content_type,
        artifact_intent=intent,
        artifact_kind=_artifact_kind(intent),
        should_materialize=intent != "none" and materialization_enabled(),
        provider=plugin,
    )


def should_materialize(plugin: str, argv: list[str]) -> bool:
    return resolve_invocation(plugin, argv, CommonOptions()).should_materialize


def invocation_locator(plugin: str, argv: list[str]) -> str:
    return derive_invocation_locator(plugin, argv)


def canonical_locator(plugin: str, argv: list[str]) -> str:
    return resolve_invocation(plugin, argv, CommonOptions()).canonical_locator


def preferred_name(plugin: str, argv: list[str], options: CommonOptions) -> str:
    return resolve_invocation(plugin, argv, options).preferred_name


def infer_content_type(plugin: str, argv: list[str], name: str) -> str:
    resolved = resolve_invocation(plugin, argv, CommonOptions())
    if resolved.preferred_name == name:
        return resolved.content_type
    return derive_content_type(resolved.resolved_plugin, resolved.resolved_argv, name)
