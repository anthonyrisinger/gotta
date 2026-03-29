"""Artifact materialization for dispatch."""

from __future__ import annotations

import os
from typing import Any

from gotta.actor import require_writer, session_actor, writer_name
from gotta.actors import ACTOR_SPEAKER_ENV
from gotta.capture import Capture
from gotta.content.env import ACTOR_ID_ENV
from gotta.content.model import (
    CommonOptions,
    ContentError,
    Materialization,
    ResolvedDirs,
)
from gotta.content.filesystem import FileSystemLedgerStore
from gotta.content.scope import session_identity
from gotta.dispatch.metadata import _derived_source_metadata
from gotta.resolve.intent import (
    SUPPRESS_MATERIALIZATION_ENV as INVOCATION_SUPPRESS_MATERIALIZATION_ENV,
)
from gotta.resolve.invoke import (
    ResolvedInvocation,
    invocation_locator,
    resolve_invocation,
)


SUPPRESS_MATERIALIZATION_ENV = INVOCATION_SUPPRESS_MATERIALIZATION_ENV


def _materialize_invocation(
    resolved_or_plugin: ResolvedInvocation | str,
    argv_or_data: list[str] | bytes,
    options: CommonOptions | None = None,
    data: bytes | None = None,
    capture: Capture | None = None,
    *,
    dirs: ResolvedDirs,
) -> Materialization | None:
    if isinstance(resolved_or_plugin, ResolvedInvocation):
        resolved = resolved_or_plugin
        payload = argv_or_data if isinstance(argv_or_data, bytes) else b""
    else:
        plugin = resolved_or_plugin
        argv = list(argv_or_data) if isinstance(argv_or_data, list) else []
        payload = data or b""
        resolved = resolve_invocation(plugin, argv, options or CommonOptions())
    if not payload:
        return None
    materialize_plugin = resolved.resolved_plugin
    materialize_argv = resolved.resolved_argv
    explicit_actor = os.environ.get(ACTOR_ID_ENV, "").strip()
    actor = explicit_actor or session_identity(dirs.session_dir)
    target_actor = explicit_actor or session_actor(dirs.session_dir) or actor
    resolved_session_root = dirs.session_dir.resolve()
    actor_branch = resolved_session_root.parent.name == "actors"
    if target_actor and actor_branch:
        writer = writer_name()
        if explicit_actor and not os.environ.get(ACTOR_SPEAKER_ENV, "").strip():
            writer = explicit_actor
        try:
            require_writer(
                dirs.session_dir,
                target_actor,
                writer=writer,
                action="attribute materialized artifacts to this actor branch",
            )
        except SystemExit as exc:
            raise ContentError(str(exc)) from exc
    metadata: dict[str, Any] = {
        "tool": "gotta",
        "plugin": materialize_plugin,
        "provider": resolved.provider,
        "artifact_kind": resolved.artifact_kind,
        "subcommand": materialize_argv[0] if materialize_argv else "",
        "argv": materialize_argv,
        "locator": invocation_locator(materialize_plugin, materialize_argv),
        "canonical_locator": resolved.canonical_locator,
        "source_kind": "stdin"
        if resolved.entry_plugin == "read" and resolved.entry_argv == ["-"]
        else "render",
        "content_type": capture.type
        if capture is not None and capture.type
        else resolved.content_type,
        "session_dir": str(dirs.session_dir),
        "content_dir": str(dirs.content_dir),
        "actor": actor,
    }
    if resolved.entry_plugin != materialize_plugin:
        metadata["entrypoint"] = resolved.entry_plugin
        metadata["entry_argv"] = resolved.entry_argv
        metadata["entry_locator"] = invocation_locator(
            resolved.entry_plugin, resolved.entry_argv
        )
        metadata["provider"] = resolved.provider
    actor_dir = os.environ.get("GOTTA_ACTOR_DIR", "").strip()
    if actor_dir:
        metadata["actor_dir"] = actor_dir
    invocation_id = os.environ.get("GOTTA_INVOCATION_ID", "").strip()
    if invocation_id:
        metadata["invocation_id"] = invocation_id
    metadata.update(
        _derived_source_metadata(
            materialize_plugin,
            materialize_argv,
            payload,
            provider=resolved.provider,
        )
    )
    if capture is not None:
        metadata.update(capture.meta)
    preferred_name = resolved.preferred_name
    if capture is not None and capture.name and not (options and options.save_as):
        preferred_name = capture.name
    return FileSystemLedgerStore.for_dirs(dirs).materialize_bytes(
        payload,
        preferred_name=preferred_name,
        metadata=metadata,
    )
