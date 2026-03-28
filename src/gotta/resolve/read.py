"""Shared parsing and resolution for `gotta read` targets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gotta.builtin import get_plugin
from gotta.content.model import CommonOptions
from gotta.content.path import sanitize_name
from gotta.resolve.locate import (
    expected_local_target,
    explicit_session_context,
    resolve_artifact_locator,
    resolve_local_target,
    resolve_session_artifact_name,
    url_name,
)
from gotta.resolve.model import ReadRequest, ReadTarget
from gotta.resolve.parse import build_parser, parse_args
from gotta.resolve.route import discover_plugin_route

__all__ = [
    "ReadRequest",
    "ReadTarget",
    "build_parser",
    "parse_args",
    "discover_plugin_route",
    "resolve_read_target",
    "should_materialize",
    "canonical_locator",
    "preferred_name",
]


def resolve_read_target(
    argv: list[str],
    options: CommonOptions | Any | None = None,
) -> ReadTarget:
    options = options or CommonOptions()
    request = parse_args(argv)
    explicit_session_root, explicit_content_root = explicit_session_context(options)
    target = (request.target or "").strip()
    save_as = str(getattr(options, "save_as", "") or "").strip()
    if not target:
        return ReadTarget(
            request=request,
            kind="stdin",
            path=None,
            routed_plugin=None,
            routed_argv=[],
            canonical_locator="read",
            preferred_name=save_as or "read.txt",
            should_materialize=False,
        )
    if target == "-":
        return ReadTarget(
            request=request,
            kind="stdin",
            path=None,
            routed_plugin=None,
            routed_argv=[],
            canonical_locator="-",
            preferred_name=save_as or "read.txt",
            should_materialize=False,
        )
    if request.routed_plugin and request.routed_argv:
        plugin = request.routed_plugin
        plugin_argv = list(request.routed_argv)
        spec = get_plugin(plugin)
        canonical = (
            spec.canonical_locator(plugin_argv)
            if spec and spec.canonical_locator
            else target
        )
        preferred = save_as or (
            spec.preferred_name(plugin_argv, options)
            if spec and spec.preferred_name
            else f"{sanitize_name(target) or plugin}.txt"
        )
        return ReadTarget(
            request=request,
            kind="routed",
            path=None,
            routed_plugin=plugin,
            routed_argv=plugin_argv,
            canonical_locator=canonical,
            preferred_name=preferred,
            should_materialize=True,
        )
    routed = discover_plugin_route(target)
    if routed is not None:
        plugin, plugin_argv = routed
        spec = get_plugin(plugin)
        canonical = (
            spec.canonical_locator(plugin_argv)
            if spec and spec.canonical_locator
            else target
        )
        preferred = save_as or (
            spec.preferred_name(plugin_argv, options)
            if spec and spec.preferred_name
            else f"{sanitize_name(target) or plugin}.txt"
        )
        return ReadTarget(
            request=request,
            kind="routed",
            path=None,
            routed_plugin=plugin,
            routed_argv=plugin_argv,
            canonical_locator=canonical,
            preferred_name=preferred,
            should_materialize=True,
        )
    if target.startswith(("http://", "https://")):
        return ReadTarget(
            request=request,
            kind="remote_url",
            path=None,
            routed_plugin=None,
            routed_argv=[],
            canonical_locator=target,
            preferred_name=save_as or url_name(target),
            should_materialize=True,
        )
    artifact_path = resolve_artifact_locator(target, content_root=explicit_content_root)
    if artifact_path is not None:
        return ReadTarget(
            request=request,
            kind="artifact_locator",
            path=artifact_path,
            routed_plugin=None,
            routed_argv=[],
            canonical_locator=target,
            preferred_name=save_as or artifact_path.name or "read.txt",
            should_materialize=False,
        )
    local_path = resolve_local_target(
        target,
        session_root=explicit_session_root,
        content_root=explicit_content_root,
    )
    if local_path is not None:
        return ReadTarget(
            request=request,
            kind="local_dir" if local_path.is_dir() else "local_file",
            path=local_path,
            routed_plugin=None,
            routed_argv=[],
            canonical_locator=target,
            preferred_name=save_as or local_path.name or "read.txt",
            should_materialize=False,
        )
    artifact_name_path = resolve_session_artifact_name(
        target,
        content_root=explicit_content_root,
    )
    if artifact_name_path is not None:
        return ReadTarget(
            request=request,
            kind="artifact_name",
            path=artifact_name_path,
            routed_plugin=None,
            routed_argv=[],
            canonical_locator=target,
            preferred_name=save_as or artifact_name_path.name or "read.txt",
            should_materialize=False,
        )
    if ":" not in target:
        expected_local = expected_local_target(
            target,
            session_root=explicit_session_root,
        )
        if expected_local is not None:
            return ReadTarget(
                request=request,
                kind="missing_local",
                path=expected_local,
                routed_plugin=None,
                routed_argv=[],
                canonical_locator=target,
                preferred_name=save_as or expected_local.name or "read.txt",
                should_materialize=False,
            )
        if not Path(target).expanduser().is_absolute():
            return ReadTarget(
                request=request,
                kind="missing_session_relative",
                path=None,
                routed_plugin=None,
                routed_argv=[],
                canonical_locator=target,
                preferred_name=save_as or "read.txt",
                should_materialize=False,
            )
    return ReadTarget(
        request=request,
        kind="unsupported",
        path=None,
        routed_plugin=None,
        routed_argv=[],
        canonical_locator=target,
        preferred_name=save_as or f"{sanitize_name(target) or 'read'}.txt",
        should_materialize=False,
    )


def should_materialize(argv: list[str]) -> bool:
    try:
        return resolve_read_target(argv).should_materialize
    except SystemExit:
        return False


def canonical_locator(argv: list[str]) -> str:
    try:
        return resolve_read_target(argv).canonical_locator
    except SystemExit:
        return "read"


def preferred_name(argv: list[str], options: CommonOptions | Any) -> str:
    try:
        return resolve_read_target(argv, options).preferred_name
    except SystemExit:
        save_as = str(getattr(options, "save_as", "") or "").strip()
        return save_as or "read.txt"
