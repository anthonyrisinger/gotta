#!/usr/bin/env python3
"""Read-only Granola note retrieval through the local app session."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import gzip
import html
import io
import json
import re
import shlex
import signal
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from gotta.capture import Capture, capture_json_command, json_bytes
from gotta.helptext import is_long_help_request, print_long_help
from gotta.project import pretty_json
from gotta.resolve.route import split_locator_tail
from gotta.source import render_source_metadata_lines


DEFAULT_SUPABASE = (
    Path.home() / "Library" / "Application Support" / "Granola" / "supabase.json"
)
DEFAULT_API_URL = "https://api.granola.ai/v2/get-documents"
DEFAULT_TRANSCRIPT_API_URL = "https://api.granola.ai/v1/get-document-transcript"
DEFAULT_LIST_LIMIT = 10
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_EXPORT_LIMIT = 20
DEFAULT_NOTE_TIME_RANGE = "last_90_days"
DEFAULT_TRANSCRIPT_SEARCH_TIME_RANGE = "last_30_days"
USER_ACTOR = "Granola/5.354.0"
CLIENT_VERSION = "5.354.0"
DOCUMENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class ToolError(RuntimeError):
    """Granola surface error."""


@dataclass(frozen=True)
class RenderedNote:
    body: str
    source: str


@dataclass(frozen=True)
class WindowSpec:
    start: dt.datetime | None
    end: dt.datetime | None
    time_range: str
    description: str


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def _slug(value: str, *, fallback: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    return value.strip("-") or fallback


def _output_extension(output: str) -> str:
    return {
        "json": "json",
        "markdown": "md",
        "meta": "json",
        "summary": "summary",
    }.get(output, "md")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _is_document_id(raw: str) -> bool:
    return bool(DOCUMENT_ID_RE.fullmatch(raw.strip()))


def canonical_locator(argv: list[str]) -> str:
    args = _parse_cli(argv)
    if args.command == "status":
        return "granola:status"
    if args.command == "transcript":
        selector = args.selector.strip()
        if (
            _is_document_id(selector)
            and not str(getattr(args, "query", "") or "").strip()
        ):
            return f"granola:transcript {selector}"
        return f"granola:{shlex.join(argv)}"
    if args.command == "get":
        selector = args.selector.strip()
        if _is_document_id(selector):
            return f"granola:{selector}"
        return f"granola:{shlex.join(['get', selector])}"
    return f"granola:{shlex.join(argv)}"


def preferred_name(argv: list[str], options: object) -> str:
    if getattr(options, "save_as", ""):
        return str(getattr(options, "save_as"))
    args = _parse_cli(argv)
    if args.command == "get":
        selector = args.selector.strip()
        base = (
            selector
            if _is_document_id(selector)
            else _slug(selector, fallback="granola")
        )
        return f"{base}.json"
    if args.command == "transcript":
        selector = args.selector.strip()
        base = (
            f"{selector}-transcript"
            if _is_document_id(selector)
            else _slug(f"{selector}-transcript", fallback="granola-transcript")
        )
        if args.query:
            base = f"{base}-query-{_slug(args.query, fallback='query')}"
        return f"{base}.json"
    if args.command == "search":
        suffix: list[str] = []
        if args.mode != "auto":
            suffix.append(args.mode)
        suffix.extend(
            _window_name_parts(args, default_time_range=DEFAULT_NOTE_TIME_RANGE)
        )
        suffix_text = f"-{'-'.join(suffix)}" if suffix else ""
        return (
            f"granola-search-{_slug(args.query, fallback='granola')}{suffix_text}.json"
        )
    if args.command == "search-transcript":
        suffix = _window_name_parts(
            args, default_time_range=DEFAULT_TRANSCRIPT_SEARCH_TIME_RANGE
        )
        suffix_text = f"-{'-'.join(suffix)}" if suffix else ""
        return f"granola-transcript-search-{_slug(args.query, fallback='granola')}{suffix_text}.json"
    if args.command == "list":
        suffix = _window_name_parts(args, default_time_range=DEFAULT_NOTE_TIME_RANGE)
        if args.sort != "updated":
            suffix.append(args.sort)
        if args.order != "desc":
            suffix.append(args.order)
        if args.offset:
            suffix.append(f"offset-{args.offset}")
        suffix_text = f"-{'-'.join(suffix)}" if suffix else ""
        return f"granola-list{suffix_text}.json"
    if args.command == "status":
        return f"granola.{_output_extension(args.output)}"
    return "granola.txt"


def _route_subcommand(subcommand: str, tail: str) -> list[str] | None:
    parts = split_locator_tail(tail)
    argv = [subcommand, *parts]
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            build_parser().parse_args(argv)
    except SystemExit:
        return None
    return argv


def route_target(target: str) -> list[str] | None:
    if target == "granola:status":
        return ["status"]
    if target == "granola:list":
        return ["list"]
    if target.startswith("granola:list "):
        return _route_subcommand("list", target.removeprefix("granola:list "))
    if target.startswith("granola:search "):
        return _route_subcommand("search", target.removeprefix("granola:search "))
    if target.startswith("granola:search-transcript "):
        return _route_subcommand(
            "search-transcript",
            target.removeprefix("granola:search-transcript "),
        )
    if target.startswith("granola:get "):
        return _route_subcommand("get", target.removeprefix("granola:get "))
    if target.startswith("granola:transcript "):
        return _route_subcommand(
            "transcript", target.removeprefix("granola:transcript ")
        )
    if not target.startswith("granola:"):
        return None
    selector = target.removeprefix("granola:").strip()
    if not selector or selector in {"status", "list"}:
        return None
    if _is_document_id(selector):
        return ["get", selector]
    return None


def positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer, got: {raw}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return value


def nonnegative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer, got: {raw}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return value


def iso_date(raw: str) -> str:
    cleaned = raw.strip()
    try:
        dt.date.fromisoformat(cleaned)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got: {raw}") from exc
    return cleaned


def parse_document_timestamp(raw: object) -> dt.datetime | None:
    cleaned = str(raw or "").strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def document_window_timestamp(document: dict[str, Any]) -> dt.datetime | None:
    return parse_document_timestamp(document.get("created_at"))


def _time_range_start(now: dt.datetime, time_range: str) -> dt.datetime | None:
    if time_range == "all":
        return None
    if time_range == "this_week":
        start_date = now.date() - dt.timedelta(days=now.weekday())
        return dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc)
    if time_range == "last_week":
        current_week_start = now.date() - dt.timedelta(days=now.weekday())
        start_date = current_week_start - dt.timedelta(days=7)
        return dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc)
    if time_range == "last_30_days":
        return now - dt.timedelta(days=30)
    if time_range == "last_90_days":
        return now - dt.timedelta(days=90)
    raise ToolError(f"unsupported Granola time range: {time_range}")


def _time_range_end(now: dt.datetime, time_range: str) -> dt.datetime | None:
    if time_range == "last_week":
        current_week_start = now.date() - dt.timedelta(days=now.weekday())
        return dt.datetime.combine(
            current_week_start,
            dt.time.min,
            tzinfo=dt.timezone.utc,
        ) - dt.timedelta(microseconds=1)
    return now if time_range != "all" else None


def _date_floor(raw: str) -> dt.datetime:
    day = dt.date.fromisoformat(raw)
    return dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)


def _date_ceiling(raw: str) -> dt.datetime:
    day = dt.date.fromisoformat(raw)
    return dt.datetime.combine(day, dt.time.max, tzinfo=dt.timezone.utc)


def resolve_window(
    *,
    time_range: str,
    after: str | None,
    before: str | None,
    default_time_range: str,
) -> WindowSpec:
    selected_time_range = time_range or default_time_range
    now = utc_now()
    start = _time_range_start(now, selected_time_range)
    end = _time_range_end(now, selected_time_range)
    if after:
        after_start = _date_floor(after)
        start = after_start if start is None else max(start, after_start)
    if before:
        before_end = _date_ceiling(before)
        end = before_end if end is None else min(end, before_end)
    if start and end and start > end:
        raise ToolError(
            "Granola time window is empty; adjust --after, --before, or --time-range"
        )

    parts: list[str] = []
    if selected_time_range != "all":
        parts.append(selected_time_range.replace("_", " "))
    if after:
        parts.append(f"after {after}")
    if before:
        parts.append(f"before {before}")
    description = ", ".join(parts) if parts else "all notes"
    return WindowSpec(
        start=start, end=end, time_range=selected_time_range, description=description
    )


def filter_documents_by_window(
    documents: list[dict[str, Any]],
    window: WindowSpec,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for document in documents:
        stamp = document_window_timestamp(document)
        if stamp is None:
            continue
        if window.start and stamp < window.start:
            continue
        if window.end and stamp > window.end:
            continue
        filtered.append(document)
    return filtered


def window_payload(window: WindowSpec) -> dict[str, Any]:
    return {
        "timeRange": window.time_range,
        "after": window.start.isoformat().replace("+00:00", "Z")
        if window.start
        else "",
        "before": window.end.isoformat().replace("+00:00", "Z") if window.end else "",
        "description": window.description,
        "field": "created_at",
    }


def _window_name_parts(
    args: argparse.Namespace, *, default_time_range: str
) -> list[str]:
    suffix: list[str] = []
    time_range = str(getattr(args, "time_range", "") or "")
    after = str(getattr(args, "after", "") or "")
    before = str(getattr(args, "before", "") or "")
    if getattr(args, "all", False):
        time_range = "all"
    if time_range and time_range != default_time_range:
        suffix.append(time_range.replace("_", "-"))
    if after:
        suffix.append(f"after-{after}")
    if before:
        suffix.append(f"before-{before}")
    if getattr(args, "all", False) and "all" not in suffix:
        suffix.append("all")
    return suffix


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ToolError(f"Granola local session file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"Granola local session file is not valid JSON: {path}"
        ) from exc


def load_access_token(supabase_path: Path) -> str:
    wrapper = _read_json(supabase_path)
    raw_tokens = wrapper.get("workos_tokens")
    if isinstance(raw_tokens, str):
        try:
            tokens = json.loads(raw_tokens)
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"Granola local session token blob is not valid JSON: {supabase_path}"
            ) from exc
    elif isinstance(raw_tokens, dict):
        tokens = raw_tokens
    else:
        raise ToolError(
            f"Granola local session is missing workos_tokens: {supabase_path}"
        )
    token = str(tokens.get("access_token") or "").strip()
    if not token:
        raise ToolError(
            f"Granola local session does not contain an access token: {supabase_path}"
        )
    return token


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        payload = exc.read()
        if payload:
            if payload[:2] == b"\x1f\x8b":
                payload = gzip.decompress(payload)
            detail = payload.decode("utf-8", errors="replace").strip()
    except OSError:
        detail = ""
    base = f"Granola API request failed: HTTP {exc.code}"
    return f"{base}: {detail}" if detail else base


def request_json(url: str, token: str, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "*/*")
    request.add_header("Accept-Encoding", "gzip")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Actor", USER_ACTOR)
    request.add_header("X-Client-Version", CLIENT_VERSION)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            if (
                response.headers.get("Content-Encoding") == "gzip"
                or raw[:2] == b"\x1f\x8b"
            ):
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as exc:
        raise ToolError(_http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"Granola API request failed: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError("Granola API returned invalid JSON") from exc


def post_json(url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = request_json(url, token, payload)
    if not isinstance(response, dict):
        raise ToolError("Granola API returned an unexpected payload shape")
    return response


def fetch_documents(
    api_url: str, token: str, limit: int | None = None
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    offset = 0
    page_size = 100 if limit is None else min(max(limit, 1), 100)
    while True:
        payload = post_json(
            api_url,
            token,
            {
                "limit": page_size,
                "offset": offset,
                "include_last_viewed_panel": True,
            },
        )
        docs = payload.get("docs")
        if not isinstance(docs, list):
            raise ToolError("Granola documents API payload is missing a docs list")
        documents.extend(item for item in docs if isinstance(item, dict))
        if limit is not None and len(documents) >= limit:
            return documents[:limit]
        if len(docs) < page_size:
            return documents
        offset += page_size


def fetch_transcript(
    api_url: str, token: str, document_id: str
) -> list[dict[str, Any]]:
    payload = request_json(api_url, token, {"document_id": document_id})
    if not isinstance(payload, list):
        raise ToolError("Granola transcript API returned an unexpected payload shape")
    segments = [item for item in payload if isinstance(item, dict)]
    segments.sort(
        key=lambda item: (
            str(item.get("start_timestamp") or ""),
            str(item.get("id") or ""),
        )
    )
    return segments


def sort_documents(
    documents: list[dict[str, Any]],
    *,
    sort_by: str = "updated",
    order: str = "desc",
) -> list[dict[str, Any]]:
    reverse = order == "desc"

    def sort_key(document: dict[str, Any]) -> str:
        if sort_by == "created":
            return str(document.get("created_at") or document.get("updated_at") or "")
        return str(document.get("updated_at") or document.get("created_at") or "")

    return sorted(
        documents,
        key=sort_key,
        reverse=reverse,
    )


def document_has_text(node: Any) -> bool:
    if isinstance(node, str):
        return bool(node.strip())
    if isinstance(node, dict):
        if node.get("type") == "text":
            return bool(str(node.get("text") or "").strip())
        return any(document_has_text(child) for child in node.get("content", []))
    if isinstance(node, list):
        return any(document_has_text(item) for item in node)
    return False


def render_marks(text: str, marks: list[dict[str, Any]] | None) -> str:
    result = text
    for mark in marks or []:
        mark_type = mark.get("type")
        attrs = mark.get("attrs") or {}
        if mark_type == "strong":
            result = f"**{result}**"
        elif mark_type == "em":
            result = f"*{result}*"
        elif mark_type == "code":
            result = f"`{result}`"
        elif mark_type == "link":
            href = attrs.get("href")
            if href:
                result = f"[{result}]({href})"
    return result


def prose_to_markdown(node: Any, indent: int = 0) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(prose_to_markdown(item, indent) for item in node)
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    content = node.get("content") or []
    attrs = node.get("attrs") or {}

    if node_type == "doc":
        parts = [prose_to_markdown(child, indent) for child in content]
        return "".join(parts).strip() + "\n"
    if node_type == "text":
        return render_marks(str(node.get("text") or ""), node.get("marks"))
    if node_type == "paragraph":
        text = "".join(prose_to_markdown(child, indent) for child in content).strip()
        return f"{text}\n\n" if text else ""
    if node_type == "heading":
        level = int(attrs.get("level") or 1)
        text = "".join(prose_to_markdown(child, indent) for child in content).strip()
        return f"{'#' * max(1, min(level, 6))} {text}\n\n" if text else ""
    if node_type == "bulletList":
        return "".join(prose_to_markdown(child, indent) for child in content) + "\n"
    if node_type == "orderedList":
        parts = []
        for index, child in enumerate(content, start=1):
            if isinstance(child, dict):
                child = {**child, "_order": index}
            parts.append(prose_to_markdown(child, indent))
        return "".join(parts) + "\n"
    if node_type == "listItem":
        body = "".join(
            prose_to_markdown(child, indent + 1) for child in content
        ).strip()
        if not body:
            return ""
        body = re.sub(r"\n{3,}", "\n\n", body)
        lines = body.splitlines()
        order = attrs.get("_order", node.get("_order"))
        prefix = f"{order}. " if order else "- "
        first = f"{'  ' * indent}{prefix}{lines[0]}"
        rest = [
            f"{'  ' * indent}  {line}" if line.strip() else "" for line in lines[1:]
        ]
        return "\n".join([first, *rest]) + "\n"
    if node_type == "blockquote":
        text = "".join(prose_to_markdown(child, indent) for child in content).strip()
        if not text:
            return ""
        return (
            "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
            + "\n\n"
        )
    if node_type == "codeBlock":
        text = "".join(prose_to_markdown(child, indent) for child in content).rstrip()
        return f"```\n{text}\n```\n\n"
    if node_type == "hardBreak":
        return "\n"
    return "".join(prose_to_markdown(child, indent) for child in content)


def prose_to_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(
            part for part in (prose_to_text(item) for item in node) if part
        ).strip()
    if not isinstance(node, dict):
        return ""
    node_type = str(node.get("type") or "")
    if node_type == "text":
        return str(node.get("text") or "")
    parts = [prose_to_text(child) for child in node.get("content", [])]
    return " ".join(part for part in parts if part).strip()


class _HtmlToMarkdown(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.href_stack: list[str | None] = []
        self.list_stack: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag == "p":
            self.parts.append("\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "a":
            self.href_stack.append(attrs_dict.get("href"))
            self.parts.append("[")
        elif tag == "ul":
            self.list_stack.append(("ul", 0))
            self.parts.append("\n")
        elif tag == "ol":
            self.list_stack.append(("ol", 0))
            self.parts.append("\n")
        elif tag == "li":
            depth = max(len(self.list_stack) - 1, 0)
            if self.list_stack:
                list_type, index = self.list_stack[-1]
                if list_type == "ol":
                    index += 1
                    self.list_stack[-1] = (list_type, index)
                    marker = f"{index}. "
                else:
                    marker = "- "
            else:
                marker = "- "
            self.parts.append("\n" + "  " * depth + marker)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p"}:
            self.parts.append("\n")
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "a":
            href = self.href_stack.pop() if self.href_stack else None
            self.parts.append(f"]({href})" if href else "]")
        elif tag in {"ul", "ol"} and self.list_stack:
            self.list_stack.pop()
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(html.unescape(data))

    def to_markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def html_to_markdown(raw_html: str) -> str:
    parser = _HtmlToMarkdown()
    parser.feed(raw_html)
    return parser.to_markdown()


def extract_people(document: dict[str, Any]) -> list[str]:
    people = document.get("people")
    if not isinstance(people, list):
        return []
    names: list[str] = []
    for item in people:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            value = ""
            for key in ("name", "display_name", "displayName", "full_name", "fullName"):
                candidate = str(item.get(key) or "").strip()
                if candidate:
                    value = candidate
                    break
        else:
            value = ""
        if value and value not in names:
            names.append(value)
    return names


def best_note_body(document: dict[str, Any]) -> RenderedNote:
    notes_markdown = str(document.get("notes_markdown") or "").strip()
    if notes_markdown:
        return RenderedNote(notes_markdown + "\n", "notes_markdown")

    notes_plain = str(document.get("notes_plain") or "").strip()
    if notes_plain:
        return RenderedNote(notes_plain + "\n", "notes_plain")

    notes = document.get("notes")
    if document_has_text(notes):
        return RenderedNote(prose_to_markdown(notes), "notes")

    panel = document.get("last_viewed_panel") or {}
    panel_content = panel.get("content")
    if document_has_text(panel_content):
        return RenderedNote(
            prose_to_markdown(panel_content), "last_viewed_panel.content"
        )

    original_content = str(panel.get("original_content") or "").strip()
    if original_content:
        return RenderedNote(
            html_to_markdown(original_content),
            "last_viewed_panel.original_content",
        )

    for field in ("summary", "overview"):
        value = document.get(field)
        if isinstance(value, str) and value.strip():
            return RenderedNote(value.strip() + "\n", field)
        if document_has_text(value):
            return RenderedNote(prose_to_markdown(value), field)

    content = str(document.get("content") or "").strip()
    if content:
        return RenderedNote(content + "\n", "content")

    return RenderedNote("", "none")


def searchable_document_text(document: dict[str, Any]) -> str:
    chunks: list[str] = []
    chunks.extend(extract_people(document))
    for field in ("notes_markdown", "notes_plain", "content"):
        value = str(document.get(field) or "").strip()
        if value:
            chunks.append(value)
    for field in ("notes", "summary", "overview"):
        value = document.get(field)
        if document_has_text(value):
            chunks.append(prose_to_text(value))
        elif isinstance(value, str) and value.strip():
            chunks.append(value.strip())
    panel = document.get("last_viewed_panel") or {}
    panel_content = panel.get("content")
    if document_has_text(panel_content):
        chunks.append(prose_to_text(panel_content))
    original_content = str(panel.get("original_content") or "").strip()
    if original_content:
        chunks.append(html_to_markdown(original_content).strip())
    combined = " ".join(part for part in chunks if part).strip()
    return re.sub(r"\s+", " ", combined)


def document_locator(document: dict[str, Any]) -> str:
    return f"granola:{str(document.get('id') or '').strip()}"


def document_meta_payload(document: dict[str, Any]) -> dict[str, Any]:
    document_id = str(document.get("id") or "")
    note = best_note_body(document)
    return {
        "id": document_id,
        "title": str(document.get("title") or "Untitled"),
        "locator": document_locator(document),
        "transcriptLocator": f"granola:transcript {document_id}" if document_id else "",
        "createdAt": str(document.get("created_at") or ""),
        "updatedAt": str(document.get("updated_at") or ""),
        "people": extract_people(document),
        "bodySource": note.source,
        "bodyAvailable": bool(note.body.strip()),
    }


def select_document(documents: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    for document in documents:
        if str(document.get("id") or "").strip() == selector:
            return document
    exact_title_matches = [
        doc for doc in documents if str(doc.get("title") or "") == selector
    ]
    if len(exact_title_matches) == 1:
        return exact_title_matches[0]
    if len(exact_title_matches) > 1:
        chosen = sort_documents(exact_title_matches)[0]
        print(
            (
                f"warning: multiple exact title matches for {selector!r}; "
                f"using most recent id {chosen.get('id')}"
            ),
            file=sys.stderr,
        )
        return chosen
    raise ToolError(
        f"no Granola document found for {selector!r}; use `gotta granola list` or "
        "`gotta granola search` to discover note ids"
    )


def excerpt_for_query(text: str, query: str, *, width: int = 180) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return ""
    lowered = normalized.casefold()
    needle = query.casefold().strip()
    if not needle:
        return normalized[:width].strip()
    position = lowered.find(needle)
    if position < 0:
        return normalized[:width].strip()
    start = max(position - width // 3, 0)
    end = min(position + len(needle) + width // 2, len(normalized))
    excerpt = normalized[start:end].strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(normalized):
        excerpt = excerpt + "..."
    return excerpt


def search_documents(
    documents: list[dict[str, Any]],
    query: str,
    *,
    limit: int,
    mode: str,
    window: WindowSpec,
) -> dict[str, Any]:
    query_text = query.strip()
    lowered = query_text.casefold()
    results: list[dict[str, Any]] = []
    for document in filter_documents_by_window(documents, window):
        title = str(document.get("title") or "")
        title_match = lowered in title.casefold() if query_text else False
        body_text = searchable_document_text(document)
        body_match = lowered in body_text.casefold() if query_text else False
        if mode == "title" and not title_match:
            continue
        if mode == "body" and not body_match:
            continue
        if mode == "auto" and not (title_match or body_match):
            continue
        matched_by: list[str] = []
        if title_match:
            matched_by.append("title")
        if body_match:
            matched_by.append("body")
        meta = document_meta_payload(document)
        results.append(
            {
                **meta,
                "matchedBy": matched_by,
                "excerpt": excerpt_for_query(body_text, query_text),
            }
        )

    def sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
        matched_by = set(str(value) for value in item.get("matchedBy") or [])
        if matched_by == {"title", "body"}:
            rank = 2
        elif "title" in matched_by:
            rank = 1
        else:
            rank = 0
        return (
            rank,
            str(item.get("updatedAt") or ""),
            str(item.get("title") or "").lower(),
        )

    ordered = sorted(results, key=sort_key, reverse=True)[:limit]
    return {
        "surface": "granola",
        "query": query_text,
        "mode": mode,
        "source": "bounded local note-body/metadata search over fetched Granola documents",
        "window": window_payload(window),
        "resultCount": len(ordered),
        "results": ordered,
    }


def filter_transcript_segments(
    segments: list[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    query_text = query.strip()
    if not query_text:
        return segments
    lowered = query_text.casefold()
    return [
        segment
        for segment in segments
        if lowered in str(segment.get("text") or "").casefold()
    ]


def transcript_match_excerpt(segment: dict[str, Any], query: str) -> str:
    return excerpt_for_query(str(segment.get("text") or ""), query, width=140)


def search_transcripts(
    *,
    api_url: str,
    transcript_api_url: str,
    token: str,
    query: str,
    documents: list[dict[str, Any]],
    limit: int,
    window: WindowSpec,
) -> dict[str, Any]:
    query_text = query.strip()
    in_scope = sort_documents(
        filter_documents_by_window(documents, window), sort_by="created", order="desc"
    )
    results: list[dict[str, Any]] = []
    scanned = 0
    for document in in_scope:
        if len(results) >= limit:
            break
        document_id = str(document.get("id") or "").strip()
        if not document_id:
            continue
        scanned += 1
        segments = fetch_transcript(transcript_api_url, token, document_id)
        matches = filter_transcript_segments(segments, query_text)
        if not matches:
            continue
        meta = document_meta_payload(document)
        excerpts = [
            {
                "timestamp": _transcript_timestamp(segment),
                "speaker": _transcript_speaker(segment),
                "excerpt": transcript_match_excerpt(segment, query_text),
            }
            for segment in matches[:5]
        ]
        results.append(
            {
                **meta,
                "matchCount": len(matches),
                "matches": excerpts,
            }
        )
    return {
        "surface": "granola",
        "query": query_text,
        "source": "bounded live Granola transcript sweep over in-scope notes",
        "window": window_payload(window),
        "scannedNotes": scanned,
        "resultCount": len(results),
        "results": results,
    }


def format_markdown_document(document: dict[str, Any], note: RenderedNote) -> str:
    document_id = str(document.get("id") or "")
    title = str(document.get("title") or "Untitled")
    lines = [
        f"# {title}",
        "",
        f"- Locator: `{document_locator(document)}`",
        f"- Document ID: {document_id}",
    ]
    if document_id:
        lines.append(f"- Transcript Locator: `granola:transcript {document_id}`")
    lines.extend(
        render_source_metadata_lines(
            {
                "source_created_at": str(document.get("created_at") or ""),
                "source_updated_at": str(document.get("updated_at") or ""),
            }
        )
    )
    lines.append(f"- Body Source: `{note.source}`")
    people = extract_people(document)
    if people:
        lines.append(f"- People: {', '.join(people)}")
    lines.extend(["", "---", ""])
    if note.body.strip():
        lines.append(note.body.rstrip())
        lines.append("")
    return "\n".join(lines)


def _transcript_speaker(segment: dict[str, Any]) -> str:
    source = str(segment.get("source") or "").strip().lower()
    if source == "system":
        return "System"
    if source == "microphone":
        return "Microphone"
    if source == "speaker":
        return "Speaker"
    if source:
        return source.replace("_", " ").title()
    return "Unknown"


def _transcript_timestamp(segment: dict[str, Any]) -> str:
    start = str(segment.get("start_timestamp") or "").strip()
    end = str(segment.get("end_timestamp") or "").strip()
    if start and end:
        return f"{start} -> {end}"
    return start or end


def transcript_payload(
    document: dict[str, Any], segments: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "surface": "granola",
        "locator": f"granola:transcript {document.get('id') or ''}",
        "document": document_meta_payload(document),
        "segmentCount": len(segments),
        "segments": segments,
    }


def format_transcript_markdown(
    document: dict[str, Any], segments: list[dict[str, Any]]
) -> str:
    title = str(document.get("title") or "Untitled")
    lines = [
        f"# Transcript: {title}",
        "",
        f"- Locator: `granola:transcript {document.get('id') or ''}`",
        f"- Document ID: {document.get('id') or ''}",
        f"- Segment Count: {len(segments)}",
    ]
    lines.extend(
        render_source_metadata_lines(
            {
                "source_created_at": str(document.get("created_at") or ""),
                "source_updated_at": str(document.get("updated_at") or ""),
            }
        )
    )
    people = extract_people(document)
    if people:
        lines.append(f"- People: {', '.join(people)}")
    lines.extend(["", "---", ""])
    if not segments:
        lines.append("_No transcript segments available._")
        lines.append("")
        return "\n".join(lines)
    for segment in segments:
        speaker = _transcript_speaker(segment)
        timestamp = _transcript_timestamp(segment)
        text = str(segment.get("text") or "").strip()
        status_bits: list[str] = []
        if timestamp:
            status_bits.append(timestamp)
        if segment.get("is_final") is False:
            status_bits.append("partial")
        header = speaker
        if status_bits:
            header += f" ({', '.join(status_bits)})"
        lines.append(f"## {header}")
        lines.append("")
        lines.append(text or "_Empty segment_")
        lines.append("")
    return "\n".join(lines)


def render_list_markdown(payload: dict[str, Any]) -> str:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return "### Granola Notes\n\n- _Surface_: `granola`\n- _Matches_: 0\n\nNo notes found.\n"
    lines = [
        "### Granola Notes",
        "",
        "- _Surface_: `granola`",
        f"- _Source_: {payload.get('source') or 'bounded local note listing over fetched Granola documents'}",
        f"- _Window_: `{((payload.get('window') or {}).get('description') or 'all notes')}`",
        f"- _Sort_: `{payload.get('sort') or 'updated'} {payload.get('order') or 'desc'}`",
        f"- _Matches_: {payload.get('resultCount') or len(results)}",
        "",
    ]
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled")
        locator = str(item.get("locator") or "")
        line = f"- [{title}]({locator})"
        details: list[str] = []
        updated = str(item.get("updatedAt") or "")
        created = str(item.get("createdAt") or "")
        primary_sort = str(payload.get("sort") or "updated")
        if primary_sort == "created":
            if created:
                details.append(f"created `{created}`")
            if updated and updated != created:
                details.append(f"updated `{updated}`")
        else:
            if updated:
                details.append(f"updated `{updated}`")
            elif created:
                details.append(f"created `{created}`")
        source = str(item.get("bodySource") or "")
        if source and source != "none":
            details.append(f"body `{source}`")
        people = [
            str(value) for value in item.get("people") or [] if str(value).strip()
        ]
        if people:
            details.append(f"people `{', '.join(people)}`")
        if details:
            line += " - " + ", ".join(details)
        lines.append(line)
    return "\n".join(lines) + "\n"


def render_search_markdown(payload: dict[str, Any]) -> str:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return (
            f"### Granola Search: {payload.get('query') or ''}\n\n"
            "- _Surface_: `granola`\n"
            f"- _Source_: {payload.get('source') or 'bounded local note-body/metadata search over fetched Granola documents'}\n"
            f"- _Window_: `{((payload.get('window') or {}).get('description') or 'all notes')}`\n"
            f"- _Mode_: `{payload.get('mode') or 'auto'}`\n"
            "- _Matches_: 0\n\n"
            "No notes matched.\n"
        )
    lines = [
        f"### Granola Search: {payload.get('query') or ''}",
        "",
        "- _Surface_: `granola`",
        f"- _Source_: {payload.get('source') or 'bounded local note-body/metadata search over fetched Granola documents'}",
        f"- _Window_: `{((payload.get('window') or {}).get('description') or 'all notes')}`",
        f"- _Mode_: `{payload.get('mode') or 'auto'}`",
        f"- _Matches_: {payload.get('resultCount') or len(results)}",
        "",
    ]
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled")
        locator = str(item.get("locator") or "")
        line = f"- [{title}]({locator})"
        details: list[str] = []
        matched_by = [str(value) for value in item.get("matchedBy") or [] if str(value)]
        if matched_by:
            details.append(f"matched by `{'+'.join(matched_by)}`")
        updated = str(item.get("updatedAt") or "")
        if updated:
            details.append(f"updated `{updated}`")
        if details:
            line += " - " + ", ".join(details)
        lines.append(line)
        excerpt = str(item.get("excerpt") or "").strip()
        if excerpt:
            lines.append(f"  - Excerpt: {excerpt}")
    return "\n".join(lines) + "\n"


def render_transcript_search_markdown(payload: dict[str, Any]) -> str:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return (
            f"### Granola Transcript Search: {payload.get('query') or ''}\n\n"
            "- _Surface_: `granola`\n"
            f"- _Source_: {payload.get('source') or 'bounded live Granola transcript sweep over in-scope notes'}\n"
            f"- _Window_: `{((payload.get('window') or {}).get('description') or 'all notes')}`\n"
            "- _Matches_: 0\n\n"
            "No transcript matches found.\n"
        )
    lines = [
        f"### Granola Transcript Search: {payload.get('query') or ''}",
        "",
        "- _Surface_: `granola`",
        f"- _Source_: {payload.get('source') or 'bounded live Granola transcript sweep over in-scope notes'}",
        f"- _Window_: `{((payload.get('window') or {}).get('description') or 'all notes')}`",
        f"- _Notes Scanned_: {payload.get('scannedNotes') or 0}",
        f"- _Matching Notes_: {payload.get('resultCount') or len(results)}",
        "",
    ]
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled")
        locator = str(item.get("transcriptLocator") or item.get("locator") or "")
        lines.append(f"#### [{title}]({locator})")
        lines.append("")
        lines.append(f"- _Matches_: {item.get('matchCount') or 0}")
        note_locator = str(item.get("locator") or "")
        if note_locator:
            lines.append(f"- _Note_: `{note_locator}`")
        for match in item.get("matches") or []:
            if not isinstance(match, dict):
                continue
            timestamp = str(match.get("timestamp") or "").strip()
            speaker = str(match.get("speaker") or "").strip()
            excerpt = str(match.get("excerpt") or "").strip()
            prefix = "  -"
            details = " ".join(
                part
                for part in (speaker, f"({timestamp})" if timestamp else "")
                if part
            ).strip()
            if details:
                lines.append(f"{prefix} `{details}` {excerpt}".rstrip())
            else:
                lines.append(f"{prefix} {excerpt}".rstrip())
        lines.append("")
    return "\n".join(lines)


def granola_status_payload(supabase_path: Path, api_url: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "surface": "granola",
        "supabaseFile": str(supabase_path),
        "apiUrl": api_url,
        "localSessionPresent": supabase_path.exists(),
        "sessionStatus": "missing",
        "documentAccess": False,
        "nextStep": (
            "open Granola and sign in locally; gotta reads the local Granola session "
            "from supabase.json"
        ),
    }
    if not supabase_path.exists():
        return payload
    try:
        token = load_access_token(supabase_path)
    except ToolError as exc:
        payload["sessionStatus"] = "invalid"
        payload["error"] = str(exc)
        payload["nextStep"] = (
            "open Granola to refresh the local session, then rerun `gotta granola status`"
        )
        return payload
    payload["accessTokenPresent"] = bool(token)
    try:
        documents = fetch_documents(api_url, token, limit=1)
    except ToolError as exc:
        payload["sessionStatus"] = "invalid"
        payload["error"] = str(exc)
        payload["nextStep"] = (
            "open Granola to refresh the local session, then rerun `gotta granola status`"
        )
        return payload
    payload["sessionStatus"] = "ready"
    payload["documentAccess"] = True
    payload["sampleDocumentCount"] = len(documents)
    payload["nextStep"] = "ready"
    return payload


def _load_recent_documents(
    args: argparse.Namespace, *, limit: int | None
) -> list[dict[str, Any]]:
    token = load_access_token(args.supabase)
    return fetch_documents(args.api_url, token, limit=limit)


def cmd_status(args: argparse.Namespace) -> int:
    payload = granola_status_payload(args.supabase, args.api_url)
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    lines = [
        "surface\tgranola",
        f"supabase_file\t{payload['supabaseFile']}",
        f"session_status\t{payload['sessionStatus']}",
        f"local_session_present\t{str(bool(payload.get('localSessionPresent'))).lower()}",
        f"document_access\t{str(bool(payload.get('documentAccess'))).lower()}",
        f"next_step\t{payload.get('nextStep') or ''}",
    ]
    if payload.get("error"):
        lines.append(f"error\t{payload['error']}")
    print("\n".join(lines))
    return 0


def _resolve_note_window(args: argparse.Namespace) -> WindowSpec:
    return resolve_window(
        time_range=str(getattr(args, "time_range", "") or ""),
        after=str(getattr(args, "after", "") or ""),
        before=str(getattr(args, "before", "") or ""),
        default_time_range=DEFAULT_NOTE_TIME_RANGE,
    )


def _resolve_transcript_search_window(args: argparse.Namespace) -> WindowSpec:
    time_range = str(getattr(args, "time_range", "") or "")
    if getattr(args, "all", False):
        time_range = "all"
    return resolve_window(
        time_range=time_range,
        after=str(getattr(args, "after", "") or ""),
        before=str(getattr(args, "before", "") or ""),
        default_time_range=DEFAULT_TRANSCRIPT_SEARCH_TIME_RANGE,
    )


def cmd_list(args: argparse.Namespace) -> int:
    window = _resolve_note_window(args)
    documents = sort_documents(
        filter_documents_by_window(_load_recent_documents(args, limit=None), window),
        sort_by=args.sort,
        order=args.order,
    )[args.offset : args.offset + args.limit]
    payload = {
        "surface": "granola",
        "source": "bounded local note listing over fetched Granola documents",
        "window": window_payload(window),
        "sort": args.sort,
        "order": args.order,
        "offset": args.offset,
        "resultCount": len(documents),
        "results": [document_meta_payload(document) for document in documents],
    }
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.output == "summary":
        for item in payload["results"]:
            primary_time = (
                item.get("createdAt")
                if args.sort == "created"
                else item.get("updatedAt") or item.get("createdAt")
            )
            print(
                f"{primary_time or ''}\t"
                f"{item.get('id') or ''}\t"
                f"{item.get('title') or 'Untitled'}"
            )
        return 0
    sys.stdout.write(render_list_markdown(payload))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    token = load_access_token(args.supabase)
    documents = fetch_documents(args.api_url, token)
    window = _resolve_note_window(args)
    payload = search_documents(
        documents,
        args.query,
        limit=args.limit,
        mode=args.mode,
        window=window,
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    sys.stdout.write(render_search_markdown(payload))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    documents = _load_recent_documents(args, limit=None)
    document = select_document(documents, args.selector)
    if args.output == "json":
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0
    if args.output == "meta":
        print(json.dumps(document_meta_payload(document), indent=2, sort_keys=True))
        return 0
    note = best_note_body(document)
    sys.stdout.write(format_markdown_document(document, note))
    return 0


def _capture_meta(document: dict[str, Any]) -> dict[str, object]:
    return {
        "projector": "granola",
        "source_created_at": str(document.get("created_at") or ""),
        "source_updated_at": str(document.get("updated_at") or ""),
    }


def capture(argv: list[str], _options: object) -> Capture:
    args = _parse_cli(argv)
    if args.command == "get":
        documents = _load_recent_documents(args, limit=None)
        document = select_document(documents, args.selector)
        selector = args.selector.strip()
        base = (
            selector
            if _is_document_id(selector)
            else _slug(selector, fallback="granola")
        )
        return Capture(
            data=json_bytes(document),
            name=f"{base}.json",
            type="application/json",
            meta=_capture_meta(document),
        )
    if args.command == "transcript":
        token = load_access_token(args.supabase)
        documents = fetch_documents(args.api_url, token, limit=None)
        document = select_document(documents, args.selector)
        segments = fetch_transcript(
            args.transcript_api_url, token, str(document.get("id") or "")
        )
        filtered_segments = filter_transcript_segments(segments, args.query)
        payload = transcript_payload(document, filtered_segments)
        if args.query:
            payload["query"] = args.query
            payload["totalSegmentCount"] = len(segments)
            payload["source"] = (
                "direct live Granola transcript retrieval with local in-note query filter"
            )
        else:
            payload["source"] = "direct live Granola transcript retrieval"
        selector = args.selector.strip()
        base = (
            f"{selector}-transcript"
            if _is_document_id(selector)
            else _slug(f"{selector}-transcript", fallback="granola-transcript")
        )
        if args.query:
            base = f"{base}-query-{_slug(args.query, fallback='query')}"
        return Capture(
            data=json_bytes(payload),
            name=f"{base}.json",
            type="application/json",
            meta=_capture_meta(document),
        )
    if args.command in {"search", "list", "search-transcript"}:
        runner = {
            "search": cmd_search,
            "list": cmd_list,
            "search-transcript": cmd_search_transcript,
        }[args.command]
        payload = capture_json_command(
            args,
            runner,
            detail=f"granola {args.command} capture failed",
        )
        return Capture(
            data=payload,
            name=preferred_name(argv, object()),
            type="application/json",
            meta={
                "projector": "granola",
                "granola_kind": args.command,
            },
        )
    raise NotImplementedError("granola capture does not support this command")


def project(argv: list[str], capture: Capture) -> bytes:
    kind = str(capture.meta.get("granola_kind") or "").strip()
    if kind in {"search", "list", "search-transcript"}:
        payload = json.loads(capture.data.decode("utf-8"))
        if not argv:
            if kind == "list":
                return render_list_markdown(payload).encode("utf-8")
            if kind == "search-transcript":
                return render_transcript_search_markdown(payload).encode("utf-8")
            return render_search_markdown(payload).encode("utf-8")
        args = _parse_cli(argv)
        if args.command != kind:
            return capture.data
        if args.output == "json":
            return pretty_json(capture.data)
        if kind == "list":
            if args.output == "summary":
                lines: list[str] = []
                results = payload.get("results") if isinstance(payload, dict) else []
                if not isinstance(results, list):
                    results = []
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    primary_time = (
                        item.get("createdAt")
                        if getattr(args, "sort", "updated") == "created"
                        else item.get("updatedAt") or item.get("createdAt")
                    )
                    lines.append(
                        f"{primary_time or ''}\t{item.get('id') or ''}\t{item.get('title') or 'Untitled'}"
                    )
                return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
            return render_list_markdown(payload).encode("utf-8")
        if kind == "search-transcript":
            return render_transcript_search_markdown(payload).encode("utf-8")
        return render_search_markdown(payload).encode("utf-8")
    payload = json.loads(capture.data.decode("utf-8"))
    if not argv:
        if "segmentCount" in payload:
            document = payload.get("document")
            segments = payload.get("segments")
            if isinstance(document, dict) and isinstance(segments, list):
                return format_transcript_markdown(document, segments).encode("utf-8")
        if isinstance(payload, dict):
            note = best_note_body(payload)
            return format_markdown_document(payload, note).encode("utf-8")
        return capture.data
    args = _parse_cli(argv)
    if args.command == "get":
        if args.output == "json":
            return pretty_json(capture.data)
        if args.output == "meta":
            if isinstance(payload, dict):
                return json_bytes(document_meta_payload(payload))
            return capture.data
        if isinstance(payload, dict):
            return format_markdown_document(payload, best_note_body(payload)).encode(
                "utf-8"
            )
        return capture.data
    if args.command == "transcript":
        if args.output == "json":
            return pretty_json(capture.data)
        if args.output == "summary":
            document = payload.get("document") if isinstance(payload, dict) else {}
            if not isinstance(document, dict):
                document = {}
            line = (
                f"{document.get('id') or ''}\t{payload.get('segmentCount') or 0}\t"
                f"{document.get('title') or 'Untitled'}\n"
            )
            return line.encode("utf-8")
        document = payload.get("document") if isinstance(payload, dict) else {}
        segments = payload.get("segments") if isinstance(payload, dict) else []
        if isinstance(document, dict) and isinstance(segments, list):
            return format_transcript_markdown(document, segments).encode("utf-8")
    return capture.data


def cmd_transcript(args: argparse.Namespace) -> int:
    token = load_access_token(args.supabase)
    documents = fetch_documents(args.api_url, token, limit=None)
    document = select_document(documents, args.selector)
    segments = fetch_transcript(
        args.transcript_api_url, token, str(document.get("id") or "")
    )
    filtered_segments = filter_transcript_segments(segments, args.query)
    payload = transcript_payload(document, filtered_segments)
    if args.query:
        payload["query"] = args.query
        payload["totalSegmentCount"] = len(segments)
        payload["source"] = (
            "direct live Granola transcript retrieval with local in-note query filter"
        )
    else:
        payload["source"] = "direct live Granola transcript retrieval"
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.output == "summary":
        print(
            f"{document.get('id') or ''}\t{payload['segmentCount']}\t"
            f"{document.get('title') or 'Untitled'}"
        )
        return 0
    markdown = format_transcript_markdown(document, filtered_segments)
    if args.query:
        source_lines = [
            f"- Query: `{args.query}`",
            f"- Source: {payload['source']}",
            f"- Matching Segments: {payload['segmentCount']}",
            f"- Total Segments: {payload.get('totalSegmentCount') or len(segments)}",
        ]
        insert_at = markdown.find("\n\n---\n\n")
        if insert_at >= 0:
            prefix = markdown[:insert_at]
            suffix = markdown[insert_at:]
            markdown = prefix + "\n" + "\n".join(source_lines) + suffix
    sys.stdout.write(markdown)
    return 0


def cmd_search_transcript(args: argparse.Namespace) -> int:
    token = load_access_token(args.supabase)
    documents = fetch_documents(args.api_url, token, limit=None)
    window = _resolve_transcript_search_window(args)
    payload = search_transcripts(
        api_url=args.api_url,
        transcript_api_url=args.transcript_api_url,
        token=token,
        query=args.query,
        documents=documents,
        limit=args.limit,
        window=window,
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    sys.stdout.write(render_transcript_search_markdown(payload))
    return 0


def _export_filename(document: dict[str, Any]) -> str:
    updated = str(document.get("updated_at") or document.get("created_at") or "")
    day = updated[:10] if updated else "undated"
    title = _slug(str(document.get("title") or ""), fallback="granola")
    return f"{day}-{title}-{document.get('id') or 'document'}.md"


def cmd_export(args: argparse.Namespace) -> int:
    window = _resolve_note_window(args)
    documents = sort_documents(
        filter_documents_by_window(_load_recent_documents(args, limit=None), window),
        sort_by=args.sort,
        order=args.order,
    )[args.offset : args.offset + args.limit]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    exported = 0
    for document in documents:
        note = best_note_body(document)
        if not note.body.strip():
            continue
        path = args.output_dir / _export_filename(document)
        path.write_text(format_markdown_document(document, note), encoding="utf-8")
        print(path)
        exported += 1
    print(f"exported {exported} notes", file=sys.stderr)
    return 0


def add_window_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_time_range: str,
    allow_all_flag: bool = False,
) -> None:
    parser.add_argument(
        "--time-range",
        choices=["this_week", "last_week", "last_30_days", "last_90_days", "all"],
        default=default_time_range,
        help=(
            "bound note discovery by note creation time; defaults to "
            f"{default_time_range.replace('_', ' ')}"
        ),
    )
    parser.add_argument(
        "--after",
        type=iso_date,
        default=None,
        help="bound note discovery to items created on or after YYYY-MM-DD",
    )
    parser.add_argument(
        "--before",
        type=iso_date,
        default=None,
        help="bound note discovery to items created on or before YYYY-MM-DD",
    )
    if allow_all_flag:
        parser.add_argument(
            "--all",
            action="store_true",
            help="search across all notes explicitly instead of the default bounded window",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta granola",
        description=(
            "Read-only Granola note retrieval through the local desktop session and "
            "Granola's documents API."
        ),
    )
    parser.add_argument(
        "--supabase",
        type=Path,
        default=DEFAULT_SUPABASE,
        help=f"path to Granola supabase.json (default: {DEFAULT_SUPABASE})",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--transcript-api-url",
        default=DEFAULT_TRANSCRIPT_API_URL,
        help=argparse.SUPPRESS,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="inspect local Granola session readiness")
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="list recent Granola notes")
    p.add_argument("--limit", type=positive_int, default=DEFAULT_LIST_LIMIT)
    p.add_argument(
        "--offset",
        type=nonnegative_int,
        default=0,
        help="skip the first N notes after sorting; useful for paging through older notes",
    )
    p.add_argument(
        "--sort",
        choices=["updated", "created"],
        default="created",
        help="sort locally by created or updated timestamp; defaults to created",
    )
    p.add_argument(
        "--order",
        choices=["desc", "asc"],
        default="desc",
        help="sort descending or ascending after fetching notes; defaults to desc",
    )
    add_window_arguments(p, default_time_range=DEFAULT_NOTE_TIME_RANGE)
    p.add_argument(
        "--output", choices=["markdown", "summary", "json"], default="markdown"
    )
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("search", help="search Granola note titles and bodies")
    p.add_argument("query", help="plain-text query")
    p.add_argument("--limit", type=positive_int, default=DEFAULT_SEARCH_LIMIT)
    p.add_argument(
        "--mode",
        choices=["auto", "title", "body"],
        default="auto",
        help="search titles only, note bodies only, or both; defaults to auto",
    )
    add_window_arguments(p, default_time_range=DEFAULT_NOTE_TIME_RANGE)
    p.add_argument("--output", choices=["markdown", "json"], default="markdown")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("get", help="fetch one Granola note by id or exact title")
    p.add_argument("selector", help="Granola document id or exact title")
    p.add_argument("--output", choices=["markdown", "meta", "json"], default="markdown")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser(
        "transcript", help="fetch one Granola transcript by note id or exact title"
    )
    p.add_argument("selector", help="Granola document id or exact title")
    p.add_argument(
        "--query",
        default="",
        help="filter the fetched transcript locally to matching segments",
    )
    p.add_argument(
        "--output", choices=["markdown", "summary", "json"], default="markdown"
    )
    p.set_defaults(func=cmd_transcript)

    p = sub.add_parser(
        "search-transcript",
        help="search Granola transcripts across a bounded live slice of notes",
    )
    p.add_argument("query", help="plain-text query")
    p.add_argument("--limit", type=positive_int, default=DEFAULT_SEARCH_LIMIT)
    add_window_arguments(
        p,
        default_time_range=DEFAULT_TRANSCRIPT_SEARCH_TIME_RANGE,
        allow_all_flag=True,
    )
    p.add_argument("--output", choices=["markdown", "json"], default="markdown")
    p.set_defaults(func=cmd_search_transcript)

    p = sub.add_parser("export", help="export recent Granola notes as Markdown files")
    p.add_argument(
        "output_dir", type=Path, help="directory to write Markdown exports into"
    )
    p.add_argument("--limit", type=positive_int, default=DEFAULT_EXPORT_LIMIT)
    p.add_argument(
        "--offset",
        type=nonnegative_int,
        default=0,
        help="skip the first N bounded notes after sorting before exporting",
    )
    p.add_argument(
        "--sort",
        choices=["updated", "created"],
        default="created",
        help="sort locally by created or updated timestamp before export; defaults to created",
    )
    p.add_argument(
        "--order",
        choices=["desc", "asc"],
        default="desc",
        help="sort descending or ascending after fetching notes; defaults to desc",
    )
    add_window_arguments(p, default_time_range=DEFAULT_NOTE_TIME_RANGE)
    p.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    if is_long_help_request(argv):
        return print_long_help(parser)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:
        return 0
    except ToolError as exc:
        return die(str(exc), code=1)


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main(sys.argv[1:]))
