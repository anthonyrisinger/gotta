"""Shared parsing and resolution for `gotta read` targets."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
import urllib.parse

from gotta.builtin import PluginSpec, available_plugins, get_plugin
from gotta.content import (
    CONTENT_ENV,
    SESSION_ENV,
    CommonOptions,
    artifact_locator,
    is_sha256_digest,
    load_state_env_at_root,
    sanitize_name,
    scan_content_store,
)
from gotta import topology


@dataclass(frozen=True, slots=True)
class ReadRequest:
    target: str | None
    recursive: bool
    max_depth: int
    head: int
    tail: int
    section: str
    session: str = ""
    actor: str = ""
    routed_plugin: str | None = None
    routed_argv: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadTarget:
    request: ReadRequest
    kind: str
    path: Path | None
    routed_plugin: str | None
    routed_argv: list[str]
    canonical_locator: str
    preferred_name: str
    should_materialize: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta read",
        description=(
            "Render one local or remote target through the native retrieval surface. "
            "Remote/provider reads store durable evidence only when an "
            "initialized session is already in play or passed explicitly; "
            "`--head`, `--tail`, and `--section` only change what is shown to "
            "the operator, while local/session-owned rereads stay as "
            "non-materializing views."
        ),
    )
    parser.add_argument("target", nargs="?")
    parser.add_argument("--session", help=argparse.SUPPRESS)
    parser.add_argument(
        "--actor",
        help="attribute any materialized artifact from this read to the selected bound actor",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="traverse local directories recursively instead of listing only one level",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="maximum directory traversal depth when rendering local directories",
    )
    parser.add_argument(
        "--head", type=int, default=0, help="show only the first N lines"
    )
    parser.add_argument(
        "--tail", type=int, default=0, help="show only the last N lines"
    )
    parser.add_argument(
        "--section",
        help="show only the markdown section whose heading contains this text",
    )
    return parser


def parse_args(argv: list[str]) -> ReadRequest:
    parser = build_parser()
    if any(token in {"-h", "--help"} for token in argv):
        return ReadRequest(
            target=None,
            recursive=False,
            max_depth=3,
            head=0,
            tail=0,
            section="",
            session="",
            actor="",
            routed_plugin=None,
            routed_argv=(),
        )
    values: dict[str, object] = {
        "recursive": False,
        "max_depth": 3,
        "head": 0,
        "tail": 0,
        "section": "",
    }
    flags = {"--recursive": "recursive"}
    int_fields = {"--max-depth": "max_depth", "--head": "head", "--tail": "tail"}
    str_fields = {
        "--section": "section",
        "--session": "session",
        "--actor": "actor",
    }
    residual: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            residual.extend(argv[index + 1 :])
            break
        name, has_inline, inline_value = token.partition("=")
        if name in flags and not has_inline:
            values[flags[name]] = True
            index += 1
            continue
        if name in int_fields or name in str_fields:
            if not has_inline:
                if index + 1 >= len(argv):
                    parser.error(f"argument {name}: expected one argument")
                inline_value = argv[index + 1]
                index += 1
            field = int_fields.get(name) or str_fields.get(name) or ""
            try:
                values[field] = (
                    int(inline_value) if name in int_fields else inline_value
                )
            except ValueError as exc:
                raise SystemExit(f"argument {name}: invalid int value") from exc
            index += 1
            continue
        residual.append(token)
        index += 1
    target: str | None = None
    routed_plugin: str | None = None
    routed_argv: tuple[str, ...] = ()
    routed = _partition_routed_target_tokens(residual)
    if routed is not None:
        routed_plugin, target, routed_argv = routed
    elif residual:
        flagged = next((token for token in residual if token.startswith("-")), "")
        if flagged:
            parser.error(f"unrecognized arguments: {flagged}")
        target = (
            " ".join(part.strip() for part in residual if part.strip()).strip() or None
        )
    return ReadRequest(
        target=target,
        recursive=bool(values["recursive"]),
        max_depth=int(values["max_depth"]),
        head=int(values["head"]),
        tail=int(values["tail"]),
        section=str(values["section"] or "").strip(),
        session=str(values.get("session") or "").strip(),
        actor=str(values.get("actor") or "").strip(),
        routed_plugin=routed_plugin,
        routed_argv=routed_argv,
    )


def _routed_plugins() -> list[PluginSpec]:
    return sorted(
        [
            spec
            for spec in (get_plugin(name) for name in available_plugins())
            if spec
            and spec.name not in {"read", "session"}
            and spec.route_target is not None
        ],
        key=lambda item: (item.route_priority, item.name),
    )


def _discover_plugin_route(target: str) -> tuple[str, list[str]] | None:
    for plugin in _routed_plugins():
        try:
            argv = plugin.route_target(target) if plugin.route_target else None
        except ValueError:
            argv = None
        if argv is not None:
            return plugin.name, argv
    return None


def discover_plugin_route(target: str) -> tuple[str, list[str]] | None:
    return _discover_plugin_route(target)


def _partition_routed_target_tokens(
    tokens: list[str],
) -> tuple[str, str, tuple[str, ...]] | None:
    best: tuple[int, int, str, str, tuple[str, ...]] | None = None
    token_count = len(tokens)
    for start in range(token_count):
        for end in range(token_count, start, -1):
            candidate = " ".join(
                part.strip() for part in tokens[start:end] if part.strip()
            ).strip()
            if not candidate:
                continue
            routed = _discover_plugin_route(candidate)
            if routed is None:
                continue
            plugin, plugin_argv = routed
            provider_argv = tuple(tokens[:start] + plugin_argv + tokens[end:])
            score = (end - start, -len(tokens[:start] + tokens[end:]))
            if best is None or score > (best[0], best[1]):
                best = (score[0], score[1], plugin, candidate, provider_argv)
    if best is None:
        return None
    return best[2], best[3], best[4]


def _nearby_session_context() -> tuple[str, str]:
    session_root = os.environ.get(SESSION_ENV, "").strip()
    content_root = os.environ.get(CONTENT_ENV, "").strip()
    if session_root and content_root:
        return session_root, content_root
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        data = load_state_env_at_root(parent)
        if not data:
            continue
        session_root = session_root or str(data.get(SESSION_ENV, "")).strip()
        content_root = content_root or str(data.get(CONTENT_ENV, "")).strip()
        if session_root or content_root:
            break
    if session_root and not content_root:
        content_root = str((Path(session_root).expanduser() / "content").resolve())
    return session_root, content_root


def _explicit_session_context(options: CommonOptions | Any | None) -> tuple[str, str]:
    if options is None:
        return "", ""
    session_root = str(getattr(options, "session_dir", "") or "").strip()
    content_root = str(getattr(options, "content_dir", "") or "").strip()
    if session_root and not content_root:
        session_path = Path(session_root).expanduser().resolve()
        in_shared_topology = (
            topology.parse_grouped_session_root(session_path) is not None
            or topology.parse_shared_session_root(session_path) is not None
        )
        if in_shared_topology:
            shared_id = topology.shared_session_id(session_path)
            content_root = str(
                (topology.shared_session_root_for(shared_id) / "content").resolve()
            )
        else:
            content_root = str((session_path / "content").resolve())
    return session_root, content_root


def _resolve_local_target(
    target: str,
    *,
    session_root: str = "",
    content_root: str = "",
) -> Path | None:
    candidate = Path(target).expanduser()
    session_root = session_root or _nearby_session_context()[0]
    content_root = content_root or _nearby_session_context()[1]
    digest_target = (
        target.removeprefix("content:") if target.startswith("content:") else target
    )
    if content_root and is_sha256_digest(digest_target):
        digest_candidate = (
            Path(content_root).expanduser() / digest_target / "data"
        ).resolve()
        if digest_candidate.exists():
            return digest_candidate
    if candidate.is_absolute():
        return candidate.resolve() if candidate.exists() else None
    if session_root:
        session_candidate = (Path(session_root).expanduser() / target).resolve()
        if session_candidate.exists():
            return session_candidate
    return None


def _expected_local_target(target: str, *, session_root: str = "") -> Path | None:
    candidate = Path(target).expanduser()
    session_root = session_root or _nearby_session_context()[0]
    if candidate.is_absolute():
        return candidate.resolve()
    if session_root:
        return (Path(session_root).expanduser() / target).resolve()
    return None


def _resolve_session_artifact_name(
    target: str,
    *,
    content_root: str = "",
) -> Path | None:
    content_root = content_root or _nearby_session_context()[1]
    if not content_root:
        return None
    root = Path(content_root).expanduser()
    if not root.exists():
        return None
    matches = [
        snapshot for snapshot in scan_content_store(root) if target in snapshot.names
    ]
    if not matches:
        return None
    if len(matches) > 1:
        suggestions = ", ".join(
            artifact_locator(target, snapshot.digest) for snapshot in matches[:5]
        )
        raise SystemExit(
            f"ambiguous stored artifact name '{target}' in the active session content store; "
            f"use `artifact:{sanitize_name(target)}@<digest12>` or `content:<digest>` instead. "
            f"matching artifact locators: {suggestions}"
        )
    return matches[0].data_path


def _resolve_artifact_locator(target: str, *, content_root: str = "") -> Path | None:
    if not target.startswith("artifact:"):
        return None
    content_root = content_root or _nearby_session_context()[1]
    if not content_root:
        raise SystemExit(
            f"artifact locator '{target}' requires an active or discoverable session content store"
        )
    root = Path(content_root).expanduser()
    if not root.exists():
        return None
    raw = target.removeprefix("artifact:")
    if "@" in raw:
        name_part, digest_hint = raw.rsplit("@", 1)
    else:
        name_part, digest_hint = raw, ""
    desired_name = sanitize_name(name_part)
    matches = [
        snapshot
        for snapshot in scan_content_store(root)
        if desired_name in snapshot.names
        and (not digest_hint or snapshot.digest.startswith(digest_hint))
    ]
    if not matches:
        return None
    if len(matches) > 1:
        suggestions = ", ".join(
            artifact_locator(desired_name, snapshot.digest) for snapshot in matches[:5]
        )
        raise SystemExit(
            f"ambiguous artifact locator '{target}' in the active session content store; "
            f"disambiguate with one of: {suggestions}"
        )
    return matches[0].data_path


def _url_name(target: str) -> str:
    parsed = urllib.parse.urlparse(target)
    name = Path(parsed.path.rstrip("/")).name or parsed.netloc or "read"
    if "." not in name:
        name = f"{name}.md"
    return name


def resolve_read_target(
    argv: list[str],
    options: CommonOptions | Any | None = None,
) -> ReadTarget:
    options = options or CommonOptions()
    request = parse_args(argv)
    explicit_session_root, explicit_content_root = _explicit_session_context(options)
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
    routed = _discover_plugin_route(target)
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
            preferred_name=save_as or _url_name(target),
            should_materialize=True,
        )
    artifact_path = _resolve_artifact_locator(
        target, content_root=explicit_content_root
    )
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
    local_path = _resolve_local_target(
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
    artifact_name_path = _resolve_session_artifact_name(
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
        expected_local = _expected_local_target(
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
