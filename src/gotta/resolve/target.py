"""Read-target branch selection and target synthesis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from gotta.resolve.route import resolve_routed_target


def _save_as(options: CommonOptions | Any | None) -> str:
    return str(getattr(options, "save_as", "") or "").strip()


def _build_target(
    request: ReadRequest,
    *,
    kind: str,
    canonical_locator: str,
    preferred_name: str,
    should_materialize: bool,
    path: Path | None = None,
    routed_plugin: str | None = None,
    routed_argv: list[str] | None = None,
) -> ReadTarget:
    return ReadTarget(
        request=request,
        kind=kind,
        path=path,
        routed_plugin=routed_plugin,
        routed_argv=routed_argv or [],
        canonical_locator=canonical_locator,
        preferred_name=preferred_name,
        should_materialize=should_materialize,
    )


def _direct_target(
    request: ReadRequest,
    target: str,
    *,
    save_as: str,
    session_root: str,
    content_root: str,
) -> ReadTarget | None:
    if target.startswith(("http://", "https://")):
        return _build_target(
            request,
            kind="remote_url",
            canonical_locator=target,
            preferred_name=save_as or url_name(target),
            should_materialize=True,
        )
    artifact_path = resolve_artifact_locator(target, content_root=content_root)
    if artifact_path is not None:
        return _build_target(
            request,
            kind="artifact_locator",
            path=artifact_path,
            canonical_locator=target,
            preferred_name=save_as or artifact_path.name or "read.txt",
            should_materialize=False,
        )
    local_path = resolve_local_target(
        target,
        session_root=session_root,
        content_root=content_root,
    )
    if local_path is not None:
        return _build_target(
            request,
            kind="local_dir" if local_path.is_dir() else "local_file",
            path=local_path,
            canonical_locator=target,
            preferred_name=save_as or local_path.name or "read.txt",
            should_materialize=False,
        )
    artifact_name_path = resolve_session_artifact_name(
        target,
        content_root=content_root,
    )
    if artifact_name_path is not None:
        return _build_target(
            request,
            kind="artifact_name",
            path=artifact_name_path,
            canonical_locator=target,
            preferred_name=save_as or artifact_name_path.name or "read.txt",
            should_materialize=False,
        )
    if ":" in target:
        return None
    expected_local = expected_local_target(
        target,
        session_root=session_root,
    )
    if expected_local is not None:
        return _build_target(
            request,
            kind="missing_local",
            path=expected_local,
            canonical_locator=target,
            preferred_name=save_as or expected_local.name or "read.txt",
            should_materialize=False,
        )
    if not Path(target).expanduser().is_absolute():
        return _build_target(
            request,
            kind="missing_session_relative",
            canonical_locator=target,
            preferred_name=save_as or "read.txt",
            should_materialize=False,
        )
    return None


def resolve_target(
    request: ReadRequest,
    options: CommonOptions | Any | None = None,
) -> ReadTarget:
    options = options or CommonOptions()
    session_root, content_root = explicit_session_context(options)
    target = (request.target or "").strip()
    save_as = _save_as(options)
    if not target:
        return _build_target(
            request,
            kind="stdin",
            canonical_locator="read",
            preferred_name=save_as or "read.txt",
            should_materialize=False,
        )
    if target == "-":
        return _build_target(
            request,
            kind="stdin",
            canonical_locator="-",
            preferred_name=save_as or "read.txt",
            should_materialize=False,
        )
    routed = resolve_routed_target(request, target, options, save_as=save_as)
    if routed is not None:
        return routed
    direct = _direct_target(
        request,
        target,
        save_as=save_as,
        session_root=session_root,
        content_root=content_root,
    )
    if direct is not None:
        return direct
    return _build_target(
        request,
        kind="unsupported",
        canonical_locator=target,
        preferred_name=save_as or f"{sanitize_name(target) or 'read'}.txt",
        should_materialize=False,
    )
