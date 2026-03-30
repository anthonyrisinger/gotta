#!/usr/bin/env python3
"""Explicit local command-evidence surface."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
from typing import Any

from gotta.capture import Capture, json_bytes
from gotta.helptext import is_long_help_request
from gotta.projection import Projection, projection_bytes
from gotta.project import looks_text


HELP_TOKENS = {"-h", "--help", "--help-all", "help-all"}
USAGE = """usage: gotta exec -- <command> [args...]

Execute one local command explicitly and capture canonical execution evidence.

Examples:
  gotta exec -- git status
  gotta exec -- python -c 'print("hello")'
  printf 'hello\n' | gotta exec -- cat
"""


class ExecSurfaceError(RuntimeError):
    """Raised when the exec surface contract cannot be satisfied."""


@dataclass(frozen=True, slots=True)
class ExecResult:
    argv: list[str]
    cwd: str
    exit_status: int
    started_at: str
    finished_at: str
    duration_seconds: float
    stdin_provenance: str
    environment_policy: str
    stdout: bytes
    stderr: bytes


def _iso_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _explicit_command(argv: list[str]) -> list[str]:
    if not argv or argv[0] != "--" or len(argv) == 1:
        raise ExecSurfaceError(
            "`gotta exec` requires an explicit command after `--`; use "
            "`gotta exec -- <command> [args...]`"
        )
    return list(argv[1:])


def _stream_payload(data: bytes) -> dict[str, Any]:
    payload: dict[str, Any] = {"byte_count": len(data)}
    if not data:
        payload["encoding"] = "utf-8"
        payload["content_type"] = "text/plain"
        payload["text"] = ""
        return payload
    if looks_text(data):
        payload["encoding"] = "utf-8"
        payload["content_type"] = "text/plain"
        payload["text"] = data.decode("utf-8", errors="replace")
        return payload
    payload["encoding"] = "base64"
    payload["content_type"] = "application/octet-stream"
    payload["base64"] = base64.b64encode(data).decode("ascii")
    return payload


def _stream_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    return str(text) if isinstance(text, str) else ""


def _render_stream(payload: object) -> str:
    if not isinstance(payload, dict):
        return "[unavailable]\n"
    text = _stream_text(payload)
    if text:
        return text
    if int(payload.get("byte_count") or 0) == 0:
        return "(empty)\n"
    content_type = str(payload.get("content_type") or "application/octet-stream")
    encoding = str(payload.get("encoding") or "unknown")
    byte_count = int(payload.get("byte_count") or 0)
    return (
        f"[binary output omitted: {byte_count} bytes, "
        f"content_type={content_type}, encoding={encoding}]\n"
    )


def _stdin_provenance() -> str:
    stream = sys.stdin
    try:
        if stream.isatty():
            return "tty"
    except OSError:
        return "unknown"
    try:
        mode = os.fstat(stream.fileno()).st_mode
    except (AttributeError, OSError, ValueError):
        return "unknown"
    if stat.S_ISFIFO(mode):
        return "pipe"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISSOCK(mode):
        return "socket"
    return "stream"


def _exec_error_status(exc: OSError) -> int:
    if isinstance(exc, FileNotFoundError):
        return 127
    if isinstance(exc, PermissionError):
        return 126
    return 126


def _exec_error_message(command: list[str], exc: OSError) -> bytes:
    program = command[0] if command else "<missing>"
    if isinstance(exc, FileNotFoundError):
        return f"gotta exec: command not found: {program}\n".encode("utf-8")
    if isinstance(exc, PermissionError):
        return f"gotta exec: permission denied: {program}\n".encode("utf-8")
    return f"gotta exec: failed to execute {program}: {exc}\n".encode("utf-8")


def _run_command(argv: list[str]) -> ExecResult:
    command = _explicit_command(argv)
    cwd = str(Path.cwd().resolve())
    started = datetime.now(tz=timezone.utc)
    stdin_provenance = _stdin_provenance()
    environment_policy = "inherit"
    try:
        result = subprocess.run(
            command,
            check=False,
            cwd=cwd,
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout = bytes(result.stdout or b"")
        stderr = bytes(result.stderr or b"")
        exit_status = int(result.returncode)
    except OSError as exc:
        stdout = b""
        stderr = _exec_error_message(command, exc)
        exit_status = _exec_error_status(exc)
    finished = datetime.now(tz=timezone.utc)
    return ExecResult(
        argv=command,
        cwd=cwd,
        exit_status=exit_status,
        started_at=_iso_timestamp(started),
        finished_at=_iso_timestamp(finished),
        duration_seconds=round((finished - started).total_seconds(), 6),
        stdin_provenance=stdin_provenance,
        environment_policy=environment_policy,
        stdout=stdout,
        stderr=stderr,
    )


def _result_payload(result: ExecResult) -> dict[str, Any]:
    return {
        "kind": "exec",
        "command": shlex.join(result.argv),
        "argv": list(result.argv),
        "cwd": result.cwd,
        "exit_status": result.exit_status,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration_seconds": result.duration_seconds,
        "stdin_provenance": result.stdin_provenance,
        "environment_policy": result.environment_policy,
        "stdout": _stream_payload(result.stdout),
        "stderr": _stream_payload(result.stderr),
    }


def _result_capture(argv: list[str], options: object) -> Capture:
    result = _run_command(argv)
    return Capture(
        data=json_bytes(_result_payload(result)),
        preferred_name=preferred_name(argv, options),
        content_type="application/json",
        metadata={
            "projector": "exec",
            "source_kind": "exec",
            "source_created_at": result.started_at,
            "source_updated_at": result.finished_at,
            "exit_status": result.exit_status,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "duration_seconds": result.duration_seconds,
            "stdin_provenance": result.stdin_provenance,
            "environment_policy": result.environment_policy,
            "cwd": result.cwd,
            "command": shlex.join(result.argv),
        },
        exit_status=result.exit_status,
    )


def _decode_payload(data: bytes) -> dict[str, Any]:
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ExecSurfaceError("stored exec artifact is not an object payload")
    return payload


def _render_payload(payload: dict[str, Any]) -> bytes:
    lines = [
        f"command: {payload.get('command') or ''}",
        f"cwd: {payload.get('cwd') or ''}",
        f"exit_status: {payload.get('exit_status')}",
        f"started_at: {payload.get('started_at') or ''}",
        f"finished_at: {payload.get('finished_at') or ''}",
        f"duration_seconds: {payload.get('duration_seconds')}",
        f"stdin_provenance: {payload.get('stdin_provenance') or ''}",
        f"environment_policy: {payload.get('environment_policy') or ''}",
        "",
        "stdout:",
        _render_stream(payload.get("stdout")).rstrip("\n"),
        "",
        "stderr:",
        _render_stream(payload.get("stderr")).rstrip("\n"),
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def artifact_intent(argv: list[str]) -> str:
    if any(token in HELP_TOKENS for token in argv):
        return "none"
    try:
        _explicit_command(argv)
    except ExecSurfaceError:
        return "none"
    return "evidence"


def canonical_locator(argv: list[str]) -> str:
    if any(token in HELP_TOKENS for token in argv):
        return "exec:help"
    command = _explicit_command(argv)
    cwd = str(Path.cwd().resolve())
    return f"exec:{cwd}::{shlex.join(command)}"


def invocation_locator(argv: list[str]) -> str:
    if any(token in HELP_TOKENS for token in argv):
        return "help"
    return shlex.join(_explicit_command(argv))


def preferred_name(argv: list[str], options: object) -> str:
    save_as = str(getattr(options, "save_as", "") or "").strip()
    if save_as:
        return save_as
    if any(token in HELP_TOKENS for token in argv):
        return "exec.txt"
    command = _explicit_command(argv)
    command_name = Path(command[0]).name.strip() or "command"
    normalized = "".join(
        character if character.isalnum() else "-" for character in command_name.lower()
    ).strip("-")
    return f"exec-{normalized or 'command'}.json"


def content_type(argv: list[str], name: str) -> str:
    del argv, name
    return "application/json"


def should_materialize(argv: list[str]) -> bool:
    return artifact_intent(argv) == "evidence"


def capture(argv: list[str], options: object) -> Capture:
    return _result_capture(argv, options)


def project(argv: list[str], capture: Capture) -> Projection:
    del argv
    payload = _decode_payload(capture.data)
    return projection_bytes(_render_payload(payload), content_type="text/plain")


def main(argv: list[str] | None = None) -> int:
    args = list(argv or [])
    if is_long_help_request(args) or any(token in {"-h", "--help"} for token in args):
        print(USAGE, end="")
        return 0
    try:
        captured = _result_capture(args, object())
    except ExecSurfaceError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    projection = project(args, captured)
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(projection.data)
        sys.stdout.flush()
    else:
        sys.stdout.write(projection.data.decode("utf-8", errors="replace"))
        sys.stdout.flush()
    return int(captured.exit_status)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
