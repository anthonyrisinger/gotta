"""Interactive output budgeting for dispatch."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from gotta.dispatch.stream import _emit_captured
from gotta.project import looks_text


OUTPUT_BUDGET_LINE_LIMIT = 256
OUTPUT_BUDGET_BYTE_LIMIT = 9728
OUTPUT_BUDGET_HARD_BYTE_LIMIT = 10 * 1024
OUTPUT_BUDGET_FLEX_BYTE_LIMIT = min(
    OUTPUT_BUDGET_HARD_BYTE_LIMIT - OUTPUT_BUDGET_BYTE_LIMIT, 256
)
OUTPUT_EMIT_BYTE_LIMIT = OUTPUT_BUDGET_BYTE_LIMIT + OUTPUT_BUDGET_FLEX_BYTE_LIMIT
FOLLOW_COMMAND_CHAR_LIMIT = 192
JSON_PREVIEW_CHAR_LIMIT = 256


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


def _json_value(data: bytes) -> Any | None:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload


def requested_output_format(plugin: str, argv: list[str], data: bytes) -> str:
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
    if plugin == "read" and any(
        token in {"-h", "--help", "--help-all"} for token in argv
    ):
        return "text"
    if _json_value(data) is not None:
        return "json"
    return "text" if looks_text(data) else "raw"


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _determine_truncate_reason(
    lines: list[str], *, max_lines: int, max_bytes: int
) -> str:
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


def _display_follow_command(
    command: str, *, limit: int = FOLLOW_COMMAND_CHAR_LIMIT
) -> str:
    normalized = str(command or "").strip()
    if not normalized:
        return ""
    if len(normalized) <= limit:
        return normalized
    return ""


def _truncation_footer(reason: str, follow_command: str) -> str:
    clipped_follow = _display_follow_command(follow_command)
    follow = (
        f"follow: {clipped_follow}"
        if clipped_follow
        else "rerun the same command with --full-output"
    )
    return f"[output truncated by {reason} budget; {follow}]\n"


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


def _truncate_text_output(
    text: str, *, follow_command: str
) -> tuple[bytes, bool, str, int, int]:
    original_lines = _count_lines(text)
    original_bytes = len(text.encode("utf-8"))
    if (
        original_lines <= OUTPUT_BUDGET_LINE_LIMIT
        and original_bytes <= OUTPUT_BUDGET_BYTE_LIMIT
    ):
        return text.encode("utf-8"), False, "", original_bytes, original_lines
    lines = text.splitlines(keepends=True)
    reason = (
        _determine_truncate_reason(
            lines,
            max_lines=OUTPUT_BUDGET_LINE_LIMIT,
            max_bytes=OUTPUT_BUDGET_BYTE_LIMIT,
        )
        or "bytes"
    )
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
    return (
        payload[:OUTPUT_EMIT_BYTE_LIMIT],
        True,
        reason,
        original_bytes,
        original_lines,
    )


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
        data = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
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
    normalized = (
        output_format
        if output_format in {"json", "raw", "markdown", "mermaid"}
        else "text"
    )
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
        if (
            budget_output
            and len(compact) > OUTPUT_BUDGET_BYTE_LIMIT
            and payload is not None
        ):
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
        emitted, truncated, reason, _original_bytes, _original_lines = (
            _truncate_text_output(
                text,
                follow_command=follow_command,
            )
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
