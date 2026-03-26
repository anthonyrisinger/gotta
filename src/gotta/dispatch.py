"""Shared dispatcher and runtime helpers for packaged gotta commands."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
import io
import json
import os
import re
import shlex
import sys
from typing import Any

from gotta.capture import Capture
from gotta.content import (
    ACTOR_ID_ENV,
    CONTENT_ENV,
    ContentError,
    CommonOptions,
    Materialization,
    ResolvedDirs,
    SESSION_ENV,
    artifact_locator,
    content_locator,
    materialize_bytes,
    resolve_dirs,
    session_identity,
    session_is_initialized,
)
from gotta.builtin import (
    PluginSpec,
    available_plugins as discovered_plugin_names,
    get_plugin,
)
from gotta.actor import require_writer, session_actor, writer_name
from gotta.actors import ACTOR_SPEAKER_ENV
from gotta.invocation import (
    ResolvedInvocation,
    SUPPRESS_MATERIALIZATION_ENV as INVOCATION_SUPPRESS_MATERIALIZATION_ENV,
    artifact_intent as resolve_artifact_intent,
    canonical_locator as resolve_canonical_locator,
    infer_content_type as resolve_content_type,
    invocation_locator as resolve_invocation_locator,
    preferred_name as resolve_preferred_name,
    resolve_invocation,
    session_access_mode as resolve_session_access_mode,
    should_materialize as resolve_should_materialize,
)
from gotta.source import (
    classify_visibility_metadata,
    derive_source_metadata_from_payload,
    extract_visibility_metadata_from_markdown,
    normalize_source_timestamp,
    slack_timestamp_to_iso,
)
from gotta.project import looks_text


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def system_exit_status(exc: SystemExit, *, emit: bool = True) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    message = str(code).strip()
    if emit and message:
        print(message, file=sys.stderr)
    return 1


SUPPRESS_MATERIALIZATION_ENV = INVOCATION_SUPPRESS_MATERIALIZATION_ENV
SUPPRESS_RECEIPTS_ENV = "GOTTA_SUPPRESS_RECEIPTS"
OUTPUT_BUDGET_LINE_LIMIT = 256
OUTPUT_BUDGET_BYTE_LIMIT = 9728
OUTPUT_BUDGET_HARD_BYTE_LIMIT = 10 * 1024
OUTPUT_BUDGET_FLEX_BYTE_LIMIT = min(OUTPUT_BUDGET_HARD_BYTE_LIMIT - OUTPUT_BUDGET_BYTE_LIMIT, 256)
OUTPUT_EMIT_BYTE_LIMIT = OUTPUT_BUDGET_BYTE_LIMIT + OUTPUT_BUDGET_FLEX_BYTE_LIMIT
FOLLOW_COMMAND_CHAR_LIMIT = 192
JSON_PREVIEW_CHAR_LIMIT = 256
_HELP_TOKENS = {"-h", "--help", "--help-all"}


def available_plugins() -> list[str]:
    return discovered_plugin_names()


def plugin_spec(plugin: str) -> PluginSpec | None:
    return get_plugin(plugin)


def print_usage() -> int:
    print("usage: gotta <plugin> [args...]", file=sys.stderr)
    print("", file=sys.stderr)
    print("canonical session-binding path: `gotta ...`", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "builtin non-session surfaces: `gotta version`, `gotta --version`, `gotta search`",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(
        "session investigative surfaces live under `gotta session`: "
        "`manifest`, `timeline`, `graph`, `leads`, `analyze`, `scan`",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("global flags: `--quiet`, `--full-output`", file=sys.stderr)
    print("", file=sys.stderr)
    print("use `gotta --help-all` for recursive command help", file=sys.stderr)
    print("", file=sys.stderr)
    print("available plugins:", file=sys.stderr)
    for plugin in available_plugins():
        spec = plugin_spec(plugin)
        description = spec.description if spec else ""
        if description:
            print(f"  - {plugin:<10} {description}", file=sys.stderr)
            continue
        print(f"  - {plugin}", file=sys.stderr)
    return 0


def load_plugin_runner(plugin: str) -> Callable[[list[str]], int]:
    spec = plugin_spec(plugin)
    if spec is None:
        raise KeyError(plugin)
    return spec.runner


def split_common_options(
    argv: list[str],
    *,
    strip_actor: bool = False,
) -> tuple[CommonOptions, list[str]]:
    session_dir: str | None = None
    content_dir: str | None = None
    actor: str | None = None
    save_as: str | None = None

    cleaned: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--":
            cleaned.extend(argv[index:])
            break
        if item.startswith("--session="):
            session_dir = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--session":
            if index + 1 >= len(argv):
                raise ContentError("--session requires a value")
            session_dir = argv[index + 1]
            index += 2
            continue
        if item.startswith("--content-dir="):
            content_dir = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--content-dir":
            if index + 1 >= len(argv):
                raise ContentError("--content-dir requires a value")
            content_dir = argv[index + 1]
            index += 2
            continue
        if strip_actor and item.startswith("--actor="):
            actor = item.split("=", 1)[1]
            index += 1
            continue
        if strip_actor and item == "--actor":
            if index + 1 >= len(argv):
                raise ContentError("--actor requires a value")
            actor = argv[index + 1]
            index += 2
            continue
        if item.startswith("--save-as="):
            save_as = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--save-as":
            if index + 1 >= len(argv):
                raise ContentError("--save-as requires a value")
            save_as = argv[index + 1]
            index += 2
            continue
        cleaned.append(item)
        index += 1

    return CommonOptions(
        session_dir=session_dir,
        content_dir=content_dir,
        actor=actor,
        save_as=save_as,
    ), cleaned


def should_materialize(plugin: str, argv: list[str]) -> bool:
    return resolve_should_materialize(plugin, argv)


def invocation_locator(plugin: str, argv: list[str]) -> str:
    return resolve_invocation_locator(plugin, argv)


def canonical_locator(plugin: str, argv: list[str]) -> str:
    return resolve_canonical_locator(plugin, argv)


def derive_preferred_name(plugin: str, argv: list[str], options: CommonOptions) -> str:
    return resolve_preferred_name(plugin, argv, options)


def infer_content_type(plugin: str, argv: list[str], name: str) -> str:
    return resolve_content_type(plugin, argv, name)


def artifact_intent(plugin: str, argv: list[str]) -> str:
    return resolve_artifact_intent(plugin, argv)


def session_access_mode(plugin: str, argv: list[str]) -> str:
    return resolve_session_access_mode(plugin, argv)


class _CapturedBuffer(io.RawIOBase):
    def __init__(self, backing: io.BytesIO, passthrough: Any | None = None) -> None:
        self._backing = backing
        self._passthrough = passthrough

    def write(self, data: bytes | bytearray) -> int:
        payload = bytes(data)
        self._backing.write(payload)
        if self._passthrough is not None:
            self._passthrough.write(payload)
            self._passthrough.flush()
        return len(payload)

    def flush(self) -> None:
        if self._passthrough is not None:
            self._passthrough.flush()


class CapturedStream(io.TextIOBase):
    """Capture text and binary writes while optionally mirroring to the real stream."""

    def __init__(
        self,
        stream: Any,
        *,
        preserve_tty: bool = False,
        passthrough: bool = False,
    ) -> None:
        self._stream = stream
        self._encoding = getattr(stream, "encoding", None) or "utf-8"
        self._isatty = bool(stream.isatty()) if preserve_tty and hasattr(stream, "isatty") else False
        self._buffer = io.BytesIO()
        passthrough_buffer = getattr(stream, "buffer", None) if passthrough else None
        self.buffer = _CapturedBuffer(self._buffer, passthrough_buffer)
        self._passthrough = stream if passthrough else None

    @property
    def encoding(self) -> str:
        return self._encoding

    def write(self, data: str) -> int:
        payload = data.encode(self._encoding)
        self._buffer.write(payload)
        if self._passthrough is not None:
            self._passthrough.write(data)
            self._passthrough.flush()
        return len(data)

    def flush(self) -> None:
        if self._passthrough is not None:
            self._passthrough.flush()

    def isatty(self) -> bool:
        return self._isatty

    def getvalue(self) -> bytes:
        return self._buffer.getvalue()


@dataclass(frozen=True, slots=True)
class EmittedOutput:
    data: bytes
    format: str
    output_budget_applied: bool
    output_truncated: bool
    truncate_reason: str
    original_bytes: int
    emitted_bytes: int
    original_lines: int | None
    emitted_lines: int | None


@contextmanager
def capture_stdout(*, preserve_tty: bool = False, passthrough: bool = False) -> Any:
    original_stdout = sys.stdout
    capture = CapturedStream(
        original_stdout,
        preserve_tty=preserve_tty,
        passthrough=passthrough,
    )
    sys.stdout = capture
    try:
        yield capture
    finally:
        sys.stdout = original_stdout


@contextmanager
def capture_stderr(*, preserve_tty: bool = False, passthrough: bool = False) -> Any:
    original_stderr = sys.stderr
    capture = CapturedStream(
        original_stderr,
        preserve_tty=preserve_tty,
        passthrough=passthrough,
    )
    sys.stderr = capture
    try:
        yield capture
    finally:
        sys.stderr = original_stderr


def _strip_quiet_flag(argv: list[str]) -> tuple[bool, list[str]]:
    quiet = False
    cleaned: list[str] = []
    for token in argv:
        if token == "--quiet":
            quiet = True
            continue
        cleaned.append(token)
    return quiet, cleaned


def _strip_full_output_flag(argv: list[str]) -> tuple[bool, list[str]]:
    full_output = False
    cleaned: list[str] = []
    for token in argv:
        if token == "--full-output":
            full_output = True
            continue
        cleaned.append(token)
    return full_output, cleaned


def _requested_output_format(plugin: str, argv: list[str], data: bytes) -> str:
    mapping = {
        "json": "json",
        "meta": "json",
        "messages": "json",
        "adf": "json",
        "markdown": "markdown",
        "md": "markdown",
        "mermaid": "mermaid",
        "text": "text",
        "summary": "text",
        "path": "text",
        "env": "text",
        "sh": "text",
        "titles": "text",
        "links": "text",
        "body": "text",
        "html": "text",
        "raw": "raw",
    }
    for index, token in enumerate(argv):
        if token.startswith("--output="):
            value = token.split("=", 1)[1].strip().lower()
            if value:
                return mapping.get(value, "text")
        if token in {"--output", "--print"} and index + 1 < len(argv):
            value = str(argv[index + 1] or "").strip().lower()
            if value:
                return mapping.get(value, "text")
    if plugin == "read" and any(token in {"-h", "--help", "--help-all"} for token in argv):
        return "text"
    if _json_value(data) is not None:
        return "json"
    return "text" if looks_text(data) else "raw"


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _determine_truncate_reason(lines: list[str], *, max_lines: int, max_bytes: int) -> str:
    count = 0
    total_bytes = 0
    for line in lines:
        encoded = line.encode("utf-8")
        if count + 1 > max_lines:
            return "lines"
        if total_bytes + len(encoded) > max_bytes:
            return "bytes"
        count += 1
        total_bytes += len(encoded)
    return ""


def _truncation_footer(reason: str, follow_command: str) -> str:
    clipped_follow = _display_follow_command(follow_command)
    follow = (
        f"follow: {clipped_follow}"
        if clipped_follow
        else "rerun the same command with --full-output"
    )
    return f"[output truncated by {reason} budget; {follow}]\n"


def _display_follow_command(command: str, *, limit: int = FOLLOW_COMMAND_CHAR_LIMIT) -> str:
    normalized = str(command or "").strip()
    if not normalized:
        return ""
    if len(normalized) <= limit:
        return normalized
    return ""


def _decode_utf8_prefix(data: bytes) -> str:
    candidate = data
    while candidate:
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError as exc:
            candidate = candidate[: exc.start]
    return ""


def _find_boundary(
    data: bytes,
    *,
    start: int,
    stop: int,
    reverse: bool = False,
) -> int:
    if stop <= start:
        return -1
    window = data[start:stop]
    pairs: tuple[tuple[bytes, int], ...] = ((b"\n\n", 2), (b"\n", 1))
    for marker, width in pairs:
        index = window.rfind(marker) if reverse else window.find(marker)
        if index != -1:
            return start + index + width
    indexes = range(len(window) - 1, -1, -1) if reverse else range(len(window))
    for index in indexes:
        byte = window[index]
        if chr(byte).isspace():
            return start + index + 1
    return -1


def _select_text_cutoff(data: bytes, *, soft_limit: int, hard_limit: int) -> int:
    if len(data) <= soft_limit:
        return len(data)
    stop = min(len(data), hard_limit)
    forward = _find_boundary(data, start=soft_limit, stop=stop, reverse=False)
    if forward != -1:
        return forward
    backward_start = max(0, soft_limit - OUTPUT_BUDGET_FLEX_BYTE_LIMIT)
    backward = _find_boundary(data, start=backward_start, stop=soft_limit, reverse=True)
    if backward != -1:
        return backward
    return min(len(data), soft_limit)


def _truncate_text_output(text: str, *, follow_command: str) -> tuple[bytes, bool, str, int, int]:
    original_lines = _count_lines(text)
    original_bytes = len(text.encode("utf-8"))
    if original_lines <= OUTPUT_BUDGET_LINE_LIMIT and original_bytes <= OUTPUT_BUDGET_BYTE_LIMIT:
        return text.encode("utf-8"), False, "", original_bytes, original_lines
    lines = text.splitlines(keepends=True)
    reason = _determine_truncate_reason(
        lines,
        max_lines=OUTPUT_BUDGET_LINE_LIMIT,
        max_bytes=OUTPUT_BUDGET_BYTE_LIMIT,
    ) or "bytes"
    footer = _truncation_footer(reason, follow_command)
    allowed_lines = max(OUTPUT_BUDGET_LINE_LIMIT - 1, 0)
    footer_bytes = len(footer.encode("utf-8"))
    soft_body_bytes = max(OUTPUT_BUDGET_BYTE_LIMIT - footer_bytes, 0)
    hard_body_bytes = max(OUTPUT_EMIT_BYTE_LIMIT - footer_bytes, 0)
    candidate = "".join(lines[:allowed_lines])
    candidate_bytes = candidate.encode("utf-8")
    cutoff = _select_text_cutoff(
        candidate_bytes,
        soft_limit=soft_body_bytes,
        hard_limit=hard_body_bytes,
    )
    body = _decode_utf8_prefix(candidate_bytes[:cutoff])
    payload = (body + footer).encode("utf-8")
    return payload[:OUTPUT_EMIT_BYTE_LIMIT], True, reason, original_bytes, original_lines


def _json_preview_summary(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        keys = list(payload)[:8]
        return {
            "type": "object",
            "keyCount": len(payload),
            "keys": keys,
        }
    if isinstance(payload, list):
        return {
            "type": "array",
            "itemCount": len(payload),
        }
    return {"type": type(payload).__name__}


def _output_budget_descriptor() -> dict[str, int]:
    return {
        "lineLimit": OUTPUT_BUDGET_LINE_LIMIT,
        "byteLimit": OUTPUT_BUDGET_BYTE_LIMIT,
        "byteFlexLimit": OUTPUT_BUDGET_FLEX_BYTE_LIMIT,
    }


def _json_preview_envelope(
    payload: Any,
    *,
    follow_command: str,
) -> bytes:
    compact = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    preview = compact[:JSON_PREVIEW_CHAR_LIMIT]
    if len(compact) > JSON_PREVIEW_CHAR_LIMIT:
        preview += "..."
    summary = _json_preview_summary(payload)
    clipped_follow = _display_follow_command(follow_command)
    envelope: dict[str, Any] = {
        "outputTruncated": True,
        "requestedFormat": "json",
        "truncateReason": "bytes",
        "budget": _output_budget_descriptor(),
        "summary": summary,
        "preview": preview,
    }
    if clipped_follow:
        envelope["followCommand"] = clipped_follow
    variants = (
        envelope,
        {**envelope, "preview": ""},
        {
            "outputTruncated": True,
            "requestedFormat": "json",
            "truncateReason": "bytes",
            "budget": _output_budget_descriptor(),
            "summary": {"type": str(summary.get("type") or type(payload).__name__)},
            "preview": "",
        },
    )
    for candidate in variants:
        data = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(data) <= OUTPUT_BUDGET_BYTE_LIMIT:
            return data
    fallback = {
        "budget": _output_budget_descriptor(),
        "outputTruncated": True,
        "requestedFormat": "json",
        "summary": {"type": "json"},
        "truncateReason": "bytes",
    }
    data = json.dumps(fallback, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) <= OUTPUT_BUDGET_BYTE_LIMIT:
        return data
    raise AssertionError("minimal JSON preview envelope exceeded output budget")


def emit_budgeted_output(
    data: bytes,
    *,
    output_format: str,
    budget_output: bool,
    follow_command: str = "",
) -> EmittedOutput:
    normalized = output_format if output_format in {"json", "raw", "markdown", "mermaid"} else "text"
    if normalized == "raw":
        _emit_captured(data)
        return EmittedOutput(
            data=data,
            format=normalized,
            output_budget_applied=False,
            output_truncated=False,
            truncate_reason="",
            original_bytes=len(data),
            emitted_bytes=len(data),
            original_lines=None,
            emitted_lines=None,
        )
    if normalized == "json":
        payload = _json_value(data)
        compact = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if budget_output and payload is not None
            else data
        )
        if budget_output and len(compact) > OUTPUT_BUDGET_BYTE_LIMIT and payload is not None:
            emitted = _json_preview_envelope(payload, follow_command=follow_command)
            _emit_captured(emitted)
            return EmittedOutput(
                data=emitted,
                format=normalized,
                output_budget_applied=True,
                output_truncated=True,
                truncate_reason="bytes",
                original_bytes=len(compact),
                emitted_bytes=len(emitted),
                original_lines=None,
                emitted_lines=None,
            )
        _emit_captured(compact)
        return EmittedOutput(
            data=compact,
            format=normalized,
            output_budget_applied=budget_output,
            output_truncated=False,
            truncate_reason="",
            original_bytes=len(compact),
            emitted_bytes=len(compact),
            original_lines=None,
            emitted_lines=None,
        )
    if not looks_text(data):
        _emit_captured(data)
        return EmittedOutput(
            data=data,
            format="raw",
            output_budget_applied=False,
            output_truncated=False,
            truncate_reason="",
            original_bytes=len(data),
            emitted_bytes=len(data),
            original_lines=None,
            emitted_lines=None,
        )
    text = data.decode("utf-8", errors="replace")
    original_lines = _count_lines(text)
    original_bytes = len(text.encode("utf-8"))
    if budget_output:
        emitted, truncated, reason, _original_bytes, _original_lines = _truncate_text_output(
            text,
            follow_command=follow_command,
        )
    else:
        emitted = data
        truncated = False
        reason = ""
    _emit_captured(emitted)
    return EmittedOutput(
        data=emitted,
        format=normalized,
        output_budget_applied=budget_output,
        output_truncated=truncated,
        truncate_reason=reason,
        original_bytes=original_bytes,
        emitted_bytes=len(emitted),
        original_lines=original_lines,
        emitted_lines=_count_lines(emitted.decode("utf-8", errors="replace")),
    )


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
        payload["artifactLocator"] = artifact_locator(result.name_link.name, result.digest)
        payload["contentLocator"] = content_locator(result.digest)
        payload["followCommand"] = _result_follow_command(result)
    if extra:
        payload.update(extra)
    return payload


def _emit_receipt(payload: dict[str, Any], *, quiet: bool) -> None:
    if not payload or quiet or os.environ.get(SUPPRESS_RECEIPTS_ENV) == "1":
        return
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=sys.stderr)


@contextmanager
def scoped_runtime_env(dirs: ResolvedDirs) -> Any:
    previous = {
        SESSION_ENV: os.environ.get(SESSION_ENV),
        CONTENT_ENV: os.environ.get(CONTENT_ENV),
    }
    os.environ[SESSION_ENV] = str(dirs.session_dir)
    os.environ[CONTENT_ENV] = str(dirs.content_dir)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _emit_captured(data: bytes) -> None:
    if not data:
        return
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        sys.stdout.flush()
        return
    sys.stdout.write(data.decode("utf-8", errors="replace"))
    sys.stdout.flush()


def _emit_captured_stderr(data: bytes) -> None:
    if not data:
        return
    buffer = getattr(sys.stderr, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        sys.stderr.flush()
        return
    sys.stderr.write(data.decode("utf-8", errors="replace"))
    sys.stderr.flush()


def _json_value(data: bytes) -> Any | None:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload


_MARKDOWN_SOURCE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^\s*-\s*(?:\*\*)?Created:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE),
        "source_created_at",
    ),
    (
        re.compile(r"^\s*-\s*(?:\*\*)?Updated:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE),
        "source_updated_at",
    ),
    (
        re.compile(r"^\s*-\s*(?:\*\*)?Modified:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE),
        "source_updated_at",
    ),
    (
        re.compile(r"^\s*-\s*(?:\*\*)?Published:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE),
        "source_published_at",
    ),
    (
        re.compile(r"^\s*-\s*(?:\*\*)?Authored:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE),
        "source_created_at",
    ),
)


def _json_nested(payload: dict[str, Any], *path: str) -> str:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "").strip()


def _extract_source_metadata_from_json(payload: Any) -> dict[str, Any]:
    metadata = derive_source_metadata_from_payload(payload)
    if not isinstance(payload, dict):
        return metadata
    candidates = (
        ("source_published_at", _json_nested(payload, "published_at")),
        ("source_published_at", _json_nested(payload, "created_at")),
        ("source_updated_at", _json_nested(payload, "updatedAt")),
        ("source_created_at", _json_nested(payload, "createdAt")),
        ("source_updated_at", _json_nested(payload, "updated")),
        ("source_created_at", _json_nested(payload, "created")),
        ("source_updated_at", _json_nested(payload, "modifiedTime")),
        ("source_created_at", _json_nested(payload, "createdTime")),
        ("source_created_at", _json_nested(payload, "commit", "author", "date")),
        ("source_created_at", _json_nested(payload, "author", "date")),
    )
    for key, value in candidates:
        parsed = normalize_source_timestamp(value) or str(value or "").strip()
        if parsed and key not in metadata:
            metadata[key] = parsed
    return metadata


def _extract_source_metadata_from_markdown(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    metadata: dict[str, str] = {}
    for pattern, key in _MARKDOWN_SOURCE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = match.group("value").strip()
        parsed = normalize_source_timestamp(value) or value
        if parsed and key not in metadata:
            metadata[key] = parsed
    authored_match = re.search(r"\bauthored (?P<value>\d{4}-\d{2}-\d{2}T\S+Z?)\b", text)
    if authored_match and "source_created_at" not in metadata:
        metadata["source_created_at"] = authored_match.group("value")
    return metadata


def _derived_source_metadata(
    plugin: str,
    argv: list[str],
    data: bytes,
    *,
    provider: str = "",
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    canonical = canonical_locator(plugin, argv)
    defaults: dict[str, str] = {}
    if canonical.startswith("slack:thread:"):
        thread_ts = canonical.rsplit(":", 1)[-1]
        source_time = slack_timestamp_to_iso(thread_ts)
        if source_time:
            defaults["source_created_at"] = source_time
            defaults["source_updated_at"] = source_time
    if plugin != "slack" or not argv or argv[0] != "get":
        payload = _json_value(data)
        if payload is not None:
            metadata.update(_extract_source_metadata_from_json(payload))
            metadata.update(
                classify_visibility_metadata(
                    payload,
                    provider=provider,
                    plugin=plugin,
                    subcommand=argv[0] if argv else "",
                    locator=canonical,
                )
            )
        metadata.update(
            {
                key: value
                for key, value in _extract_source_metadata_from_markdown(data).items()
                if key not in metadata
            }
        )
        metadata.update(
            {
                key: value
                for key, value in extract_visibility_metadata_from_markdown(data).items()
                if key not in metadata
            }
        )
        if "visibility_level" not in metadata:
            metadata.update(
                classify_visibility_metadata(
                    {},
                    provider=provider,
                    plugin=plugin,
                    subcommand=argv[0] if argv else "",
                    locator=canonical,
                )
            )
        for key, value in defaults.items():
            metadata.setdefault(key, value)
        return metadata
    payload = _json_value(data)
    if isinstance(payload, dict):
        first_ts = slack_timestamp_to_iso(str(payload.get("firstTs") or ""))
        last_ts = slack_timestamp_to_iso(str(payload.get("lastTs") or ""))
        if first_ts:
            metadata["source_created_at"] = first_ts
        if last_ts:
            metadata["source_updated_at"] = last_ts
        metadata.update(
            classify_visibility_metadata(
                payload,
                provider=provider,
                plugin=plugin,
                subcommand=argv[0] if argv else "",
                locator=canonical,
            )
        )
    metadata.update(
        {
            key: value
            for key, value in _extract_source_metadata_from_markdown(data).items()
            if key not in metadata
        }
    )
    metadata.update(
        {
            key: value
            for key, value in extract_visibility_metadata_from_markdown(data).items()
            if key not in metadata
        }
    )
    if "visibility_level" not in metadata:
        metadata.update(
            classify_visibility_metadata(
                {},
                provider=provider,
                plugin=plugin,
                subcommand=argv[0] if argv else "",
                locator=canonical,
            )
        )
    for key, value in defaults.items():
        metadata.setdefault(key, value)
    return metadata


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
    metadata = {
        "tool": "gotta",
        "plugin": materialize_plugin,
        "provider": resolved.provider,
        "artifact_kind": resolved.artifact_kind,
        "subcommand": materialize_argv[0] if materialize_argv else "",
        "argv": materialize_argv,
        "locator": invocation_locator(materialize_plugin, materialize_argv),
        "canonical_locator": resolved.canonical_locator,
        "source_kind": "stdin" if resolved.entry_plugin == "read" and resolved.entry_argv == ["-"] else "render",
        "content_type": capture.type if capture is not None and capture.type else resolved.content_type,
        "session_dir": str(dirs.session_dir),
        "content_dir": str(dirs.content_dir),
        "actor": actor,
    }
    if resolved.entry_plugin != materialize_plugin:
        metadata["entrypoint"] = resolved.entry_plugin
        metadata["entry_argv"] = resolved.entry_argv
        metadata["entry_locator"] = invocation_locator(resolved.entry_plugin, resolved.entry_argv)
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
    return materialize_bytes(
        payload,
        dirs=dirs,
        preferred_name=preferred_name,
        metadata=metadata,
    )


def _emit_materialization_receipt(result: Materialization | None) -> None:
    if result is None:
        return
    artifact_kind = str(result.artifact_kind or "content").strip() or "content"
    print(
        f"stored {artifact_kind} artifact: {result.name_link} (data: {result.data_path})",
        file=sys.stderr,
    )
    print(
        "locators: "
        f"{artifact_locator(result.name_link.name, result.digest)}, "
        f"{content_locator(result.digest)}",
        file=sys.stderr,
    )


def _run_callable(func: Callable[[list[str]], int], argv: list[str]) -> int:
    try:
        return int(func(argv))
    except SystemExit as exc:
        return system_exit_status(exc)


def _captured_execution(
    plugin: str,
    argv: list[str],
    options: CommonOptions,
) -> tuple[Capture, bytes]:
    spec = plugin_spec(plugin)
    if spec is None or spec.capture is None or spec.project is None:
        raise RuntimeError(f"plugin `{plugin}` does not support canonical capture")
    capture = spec.capture(argv, options)
    display = spec.project(argv, capture)
    return capture, display


def require_operational_session(dirs: ResolvedDirs) -> None:
    if not session_is_initialized(dirs.session_dir):
        raise ContentError(
            "start or bind a session first with `gotta ...` before running "
            "operational commands. Stable interactive contexts adopt and "
            "scaffold their deterministic session on first session-aware use. "
            "Use `gotta session init --session <root>` only when you "
            "intentionally want to scaffold one exact root."
        )


def _runtime_dirs(options: CommonOptions, *, access: str) -> ResolvedDirs:
    if access == "ambient" and os.environ.get(SESSION_ENV, "").strip():
        return resolve_dirs(CommonOptions(), create=False)
    return resolve_dirs(options, create=False)


def _sessionless_notice_bytes(resolved: ResolvedInvocation) -> bytes:
    if os.environ.get("GOTTA_AMBIENT_SESSIONLESS", "") != "1":
        return b""
    if not sys.stderr.isatty():
        return b""
    if resolved.artifact_intent == "discovery":
        return (
            "ran sessionless; bind or pass `--session <session-id>` to store this as a "
            "discovery artifact\n"
        ).encode("utf-8")
    if resolved.artifact_intent == "evidence":
        return (
            "ran sessionless; bind or pass `--session <session-id>` to store this as an "
            "evidence artifact\n"
        ).encode("utf-8")
    return b""


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
    dirs: ResolvedDirs | None,
) -> dict[str, Any]:
    return {}


def _streams_live(plugin: str, argv: list[str]) -> bool:
    return plugin == "actor" and bool(argv) and argv[0] == "launch"


def run_plugin(plugin: str, argv: list[str]) -> int:
    quiet, argv = _strip_quiet_flag(argv)
    full_output, argv = _strip_full_output_flag(argv)
    if plugin == "session":
        options = CommonOptions()
        cleaned = argv
    else:
        try:
            options, cleaned = split_common_options(
                argv,
                strip_actor=plugin in {"read", "confluence", "gdocs", "gdrive", "github", "grafana", "granola", "gsheets", "jira", "slack"},
            )
        except ContentError as exc:
            return die(str(exc))

    try:
        runner = load_plugin_runner(plugin)
    except KeyError:
        plugins = ", ".join(available_plugins())
        return die(f"unknown gotta plugin: {plugin}. available plugins: {plugins}")
    except RuntimeError as exc:
        return die(str(exc), code=1)

    try:
        resolved = resolve_invocation(plugin, cleaned, options)
    except SystemExit as exc:
        return system_exit_status(exc)
    except ContentError as exc:
        return die(str(exc))
    except RuntimeError as exc:
        return die(str(exc), code=1)
    access = session_access_mode(plugin, cleaned)
    spec = plugin_spec(plugin)
    follow_command = ""
    rerun_command = _rerun_full_output_command(plugin, cleaned)
    runtime_dirs: ResolvedDirs | None = None
    budget_output = not full_output

    def emit_success(
        stdout_data: bytes,
        *,
        stderr_data: bytes = b"",
        result: Materialization | None = None,
        dirs: ResolvedDirs | None = None,
    ) -> int:
        nonlocal follow_command
        follow_command = _result_follow_command(result)
        emitted = emit_budgeted_output(
            stdout_data,
            output_format=_requested_output_format(plugin, cleaned, stdout_data),
            budget_output=budget_output,
            follow_command=rerun_command,
        )
        if stderr_data and not quiet:
            _emit_captured_stderr(stderr_data)
        if _should_emit_receipt(plugin, cleaned):
            _emit_receipt(
                _receipt_payload(
                    emitted=emitted,
                    result=result,
                    extra=_receipt_extra(plugin, cleaned, dirs=dirs),
                ),
                quiet=quiet,
            )
        return 0

    def replay_stdout(stdout_data: bytes) -> None:
        if not stdout_data:
            return
        emit_budgeted_output(
            stdout_data,
            output_format=_requested_output_format(plugin, cleaned, stdout_data),
            budget_output=budget_output,
            follow_command=rerun_command,
        )

    if not resolved.should_materialize and _streams_live(plugin, cleaned):
        if access != "none" and (options.session_dir or options.content_dir or options.actor):
            try:
                runtime_dirs = _runtime_dirs(options, access=access)
                require_operational_session(runtime_dirs)
            except ContentError as exc:
                return die(str(exc))
            with scoped_runtime_env(runtime_dirs):
                return _run_callable(runner, cleaned)
        return _run_callable(runner, cleaned)

    if not resolved.should_materialize:
        if access != "none" and (options.session_dir or options.content_dir or options.actor):
            try:
                runtime_dirs = _runtime_dirs(options, access=access)
                require_operational_session(runtime_dirs)
            except ContentError as exc:
                return die(str(exc))
            with scoped_runtime_env(runtime_dirs):
                with capture_stdout(preserve_tty=True) as stdout_capture, capture_stderr(
                    preserve_tty=True
                ) as stderr_capture:
                    code = _run_callable(runner, cleaned)
        else:
            with capture_stdout(preserve_tty=True) as stdout_capture, capture_stderr(
                preserve_tty=True
            ) as stderr_capture:
                code = _run_callable(runner, cleaned)
        stderr_data = stderr_capture.getvalue()
        stdout_data = stdout_capture.getvalue()
        if code == 0:
            if not quiet:
                stderr_data += _sessionless_notice_bytes(resolved)
            return emit_success(stdout_data, stderr_data=stderr_data, dirs=runtime_dirs)
        replay_stdout(stdout_data)
        _emit_captured_stderr(stderr_data)
        return code

    try:
        runtime_dirs = _runtime_dirs(options, access=access)
        require_operational_session(runtime_dirs)
    except ContentError as exc:
        return die(str(exc))

    if (
        spec
        and spec.capture is not None
        and spec.project is not None
        and resolved.artifact_intent in {"evidence", "discovery"}
    ):
        try:
            with scoped_runtime_env(runtime_dirs):
                with capture_stderr(
                    preserve_tty=True
                ) as stderr_capture:
                    capture, display = _captured_execution(plugin, cleaned, options)
        except NotImplementedError:
            capture = None
            display = None
        except SystemExit as exc:
            return system_exit_status(exc)
        except (ContentError, RuntimeError) as exc:
            return die(str(exc), code=1)
        if capture is not None and display is not None:
            try:
                result = _materialize_invocation(
                    resolved,
                    capture.data,
                    options=options,
                    capture=capture,
                    dirs=runtime_dirs,
                )
            except ContentError as exc:
                return die(str(exc), code=1)
            return emit_success(
                display,
                stderr_data=stderr_capture.getvalue(),
                result=result,
                dirs=runtime_dirs,
            )

    with scoped_runtime_env(runtime_dirs):
        with capture_stdout(preserve_tty=True) as stdout_capture, capture_stderr(
            preserve_tty=True
        ) as stderr_capture:
            code = _run_callable(runner, cleaned)
    data = stdout_capture.getvalue()
    stderr_data = stderr_capture.getvalue()
    if code == 0:
        try:
            result = _materialize_invocation(resolved, data, options=options, dirs=runtime_dirs)
        except ContentError as exc:
            return die(str(exc), code=1)
        return emit_success(data, stderr_data=stderr_data, result=result, dirs=runtime_dirs)
    replay_stdout(data)
    _emit_captured_stderr(stderr_data)
    return code
