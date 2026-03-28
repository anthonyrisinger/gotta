"""Receipt emission and follow-command helpers for dispatch."""

from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Any

from gotta.content import Materialization, artifact_locator, content_locator
from gotta.dispatch.budget import EmittedOutput, _output_budget_descriptor


SUPPRESS_RECEIPTS_ENV = "GOTTA_SUPPRESS_RECEIPTS"
_HELP_TOKENS = {"-h", "--help", "--help-all"}


def _result_follow_command(result: Materialization | None) -> str:
    if result is None:
        return ""
    locator = content_locator(result.digest)
    return shlex.join(["gotta", "read", locator])


def _rerun_full_output_command(plugin: str, argv: list[str]) -> str:
    return shlex.join(["gotta", plugin, *argv, "--full-output"])


def _receipt_payload(
    *,
    emitted: EmittedOutput,
    result: Materialization | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if emitted.output_truncated:
        payload.update(
            {
                "outputFormat": emitted.format,
                "outputBudgetApplied": emitted.output_budget_applied,
                "outputTruncated": True,
                "truncateReason": emitted.truncate_reason or None,
                "budget": _output_budget_descriptor(),
                "originalBytes": emitted.original_bytes,
                "emittedBytes": emitted.emitted_bytes,
            }
        )
        if emitted.original_lines is not None:
            payload["originalLines"] = emitted.original_lines
        if emitted.emitted_lines is not None:
            payload["emittedLines"] = emitted.emitted_lines
    if result is not None:
        payload["artifactKind"] = str(result.artifact_kind or "").strip() or "content"
        payload["artifactLocator"] = artifact_locator(
            result.name_link.name, result.digest
        )
        payload["contentLocator"] = content_locator(result.digest)
        payload["followCommand"] = _result_follow_command(result)
    if extra:
        payload.update(extra)
    return payload


def _emit_receipt(payload: dict[str, Any], *, quiet: bool) -> None:
    if not payload or quiet or os.environ.get(SUPPRESS_RECEIPTS_ENV) == "1":
        return
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=sys.stderr)


def _should_emit_receipt(plugin: str, argv: list[str]) -> bool:
    if os.environ.get(SUPPRESS_RECEIPTS_ENV) == "1":
        return False
    if any(token in _HELP_TOKENS for token in argv):
        return False
    if plugin in {"ask"}:
        return False
    return True


def _receipt_extra(
    plugin: str,
    argv: list[str],
    *,
    dirs: object | None,
) -> dict[str, Any]:
    return {}
