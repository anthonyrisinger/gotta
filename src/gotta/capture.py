"""Canonical capture objects for artifact-bearing retrieval."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from typing import Any, Callable, Literal


ArtifactKind = Literal["discovery", "evidence"]


@dataclass(frozen=True, slots=True)
class Capture:
    data: bytes
    preferred_name: str = ""
    content_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    view_data: dict[str, Any] = field(default_factory=dict)


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def capture_json_command(
    args: argparse.Namespace,
    runner: Callable[[argparse.Namespace], int],
    *,
    detail: str,
) -> bytes:
    from gotta.dispatch.stream import capture_stdout

    captured_args = argparse.Namespace(**vars(args))
    setattr(captured_args, "output", "json")
    with capture_stdout() as captured:
        code = runner(captured_args)
    if code != 0:
        message = captured.getvalue().decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or detail)
    return captured.getvalue()


__all__ = [
    "ArtifactKind",
    "Capture",
    "capture_json_command",
    "json_bytes",
]
