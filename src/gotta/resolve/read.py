"""Shared parsing and resolution for `gotta read` targets."""

from __future__ import annotations

from typing import Any

from gotta.content.model import CommonOptions
from gotta.resolve.model import ReadRequest, ReadTarget
from gotta.resolve.parse import build_parser, parse_args
from gotta.resolve.route import discover_plugin_route, discover_surface_route
from gotta.resolve.target import resolve_target

__all__ = [
    "ReadRequest",
    "ReadTarget",
    "build_parser",
    "parse_args",
    "discover_surface_route",
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
    return resolve_target(parse_args(argv), options or CommonOptions())


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
