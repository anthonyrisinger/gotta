#!/usr/bin/env python3
"""Render local or remote targets into a useful terminal-friendly form."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from html import unescape
import urllib.error
import urllib.request

from gotta.builtin import get_plugin
from gotta.capture import Capture
from gotta.content import CommonOptions
from gotta.dispatch import (
    SUPPRESS_MATERIALIZATION_ENV,
    SUPPRESS_RECEIPTS_ENV,
    capture_stdout,
    run_plugin,
)
from gotta.helptext import is_long_help_request, print_long_help
from gotta.project import html_markdown, looks_text, pretty_json
from gotta.target import build_parser, parse_args, resolve_read_target


USAGE = """usage: gotta read [--recursive] [--max-depth N] [<target>|-]

Render one target through the most obvious retrieval surface.

Routing:
  GitHub URLs          -> gotta github
  Confluence page URLs -> gotta confluence get
  Jira issue URLs      -> gotta jira get
  Slack thread URLs    -> gotta slack get
  Google Sheets URLs   -> gotta gsheets get
  Google Drive URLs    -> gotta gdrive get
  Google Docs URLs     -> gotta gdocs get
  other HTTP(S) URLs  -> direct fetch, with HTML converted through pandoc when possible
  local files / stdin -> direct rendering
  local directories   -> native listing, with optional recursive traversal

  Materialization:
  routed provider fetches and direct remote reads store durable evidence
  only when an initialized session is already active or passed explicitly.
  `--head`, `--tail`, and `--section` only trim what is shown to the operator;
  the full canonical remote/provider payload still lands underneath when the
  read materializes. Local files, local directories, and session-owned artifact
  rereads stay as non-materializing views so reopening evidence does not fork
  the evidence graph. Use session manifest, session leads <artifact>, or
  session analyze to continue from the content store rather than re-fetching
  blindly.

  Attribution:
  pass `--actor <actor>` to attribute any materialized artifact from this read
  to one bound actor inside the selected/ambient session.
"""

REMOTE_FETCH_TIMEOUT_SECONDS = 15
REMOTE_FETCH_MAX_BYTES = 2 * 1024 * 1024
REMOTE_FETCH_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ReadExecution:
    code: int
    canonical_bytes: bytes
    display_bytes: bytes


_REMOTE_EXTENSION_BY_TYPE = {
    "text/html": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/json": ".json",
    "text/json": ".json",
    "text/csv": ".csv",
    "application/xml": ".xml",
    "text/xml": ".xml",
}


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def print_usage() -> int:
    return die(USAGE)


def guess_lang_from_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".html": "html",
        ".htm": "html",
        ".md": "markdown",
        ".markdown": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".xml": "xml",
        ".css": "css",
        ".js": "javascript",
        ".toml": "toml",
        ".sh": "bash",
        ".py": "python",
        ".go": "go",
        ".tf": "hcl",
        ".tfvars": "hcl",
    }.get(suffix, "txt")


def guess_lang_from_content_type(content_type: str) -> str:
    kind = content_type.split(";", 1)[0].strip().lower()
    return {
        "text/html": "html",
        "text/markdown": "markdown",
        "application/json": "json",
        "text/json": "json",
        "application/yaml": "yaml",
        "application/x-yaml": "yaml",
        "text/yaml": "yaml",
        "text/x-yaml": "yaml",
        "application/xml": "xml",
        "text/xml": "xml",
        "text/css": "css",
        "application/javascript": "javascript",
        "text/javascript": "javascript",
    }.get(kind, "txt" if kind.startswith("text/") else "")


def render_file(path: Path, language: str = "txt") -> None:
    bat = shutil.which("bat")
    if bat and sys.stdout.isatty():
        env = os.environ.copy()
        env.setdefault("BAT_THEME", "ansi")
        result = subprocess.run(
            [
                bat,
                "--paging=never",
                "--style=plain",
                f"--language={language}",
                "--italic-text=always",
                str(path),
            ],
            check=False,
            env=env,
        )
        if result.returncode == 0:
            return
    with path.open("rb") as handle:
        shutil.copyfileobj(handle, sys.stdout.buffer)


def render_bytes(data: bytes, language: str = "txt") -> None:
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        tmp_path = Path(handle.name)
        handle.write(data)
    try:
        render_file(tmp_path, language)
    finally:
        tmp_path.unlink(missing_ok=True)


def _read_response_body(response, *, max_bytes: int = REMOTE_FETCH_MAX_BYTES) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        read_size = REMOTE_FETCH_CHUNK_BYTES if total < max_bytes else 1
        chunk = response.read(read_size)
        if not chunk:
            break
        remaining = max_bytes - total
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            total += remaining
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


def _remote_timeout_message() -> str:
    return f"download timed out after {REMOTE_FETCH_TIMEOUT_SECONDS}s"


def fetch_url(target: str) -> tuple[bytes, str, bool]:
    request = urllib.request.Request(target, headers={"User-Actor": "gotta-read"})
    try:
        with urllib.request.urlopen(request, timeout=REMOTE_FETCH_TIMEOUT_SECONDS) as response:
            data, truncated = _read_response_body(response)
            return data, response.headers.get("Content-Type", ""), truncated
    except urllib.error.HTTPError as exc:
        data, _truncated = _read_response_body(exc)
        body = data.decode("utf-8", errors="replace")
        detail = _summarize_http_error(exc.code, body, exc.headers.get("Content-Type", ""))
        raise RuntimeError(detail) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise RuntimeError(_remote_timeout_message()) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise RuntimeError(_remote_timeout_message()) from exc
        raise RuntimeError(f"download failed: {exc.reason}") from exc


def _emit_truncation_note() -> None:
    print(
        f"note: truncated remote body at {REMOTE_FETCH_MAX_BYTES} bytes",
        file=sys.stderr,
    )


def _summarize_http_error(status: int, body: str, content_type: str) -> str:
    normalized_body = body.strip()
    lowered = normalized_body.lower()
    if "text/html" in content_type.lower() or "<html" in lowered:
        title_match = re.search(r"<title[^>]*>(?P<title>.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
        if title_match:
            title = re.sub(r"\s+", " ", unescape(title_match.group("title"))).strip()
            if title:
                return f"download failed with {status}: {title}"
        return f"download failed with {status}: remote server returned an HTML error page"
    first_lines = [line.strip() for line in normalized_body.splitlines() if line.strip()]
    preview = " ".join(first_lines[:3]).strip()
    if preview:
        preview = re.sub(r"\s+", " ", preview)
        if len(preview) > 240:
            preview = preview[:237].rstrip() + "..."
        return f"download failed with {status}: {preview}"
    return f"download failed with {status}"


def extract_main_html(html: str) -> str:
    for pattern in (
        r"<main\b[^>]*>.*?</main>",
        r"<article\b[^>]*>.*?</article>",
    ):
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(0)
    return html


def strip_html_chrome(html: str) -> str:
    text = extract_main_html(html)
    patterns = [
        r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>",
        r"<(nav|header|footer|aside)\b[^>]*>.*?</\1>",
        (
            r"<(?P<tag>div|section|ul)\b[^>]*(?:id|class)=\"[^\"]*"
            r"(?:nav|menu|breadcrumb|sidebar|toc|table-of-contents|on-this-page|site-header|site-footer)"
            r"[^\"]*\"[^>]*>.*?</(?P=tag)>"
        ),
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    return text


def html_as_markdown_bytes(data: bytes) -> bytes | None:
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return None
    cleaned = strip_html_chrome(data.decode("utf-8", errors="replace")).encode("utf-8")
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        tmp_path = Path(handle.name)
        handle.write(cleaned)
    try:
        proc = subprocess.run(
            [pandoc, "-f", "html", "-t", "gfm", "--wrap=none", str(tmp_path)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "pandoc failed to convert HTML") from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    return proc.stdout


def delegate(plugin: str, argv: list[str]) -> int:
    previous = os.environ.get(SUPPRESS_MATERIALIZATION_ENV)
    previous_receipts = os.environ.get(SUPPRESS_RECEIPTS_ENV)
    os.environ[SUPPRESS_MATERIALIZATION_ENV] = "1"
    os.environ[SUPPRESS_RECEIPTS_ENV] = "1"
    try:
        return run_plugin(plugin, argv)
    finally:
        if previous is None:
            os.environ.pop(SUPPRESS_MATERIALIZATION_ENV, None)
        else:
            os.environ[SUPPRESS_MATERIALIZATION_ENV] = previous
        if previous_receipts is None:
            os.environ.pop(SUPPRESS_RECEIPTS_ENV, None)
        else:
            os.environ[SUPPRESS_RECEIPTS_ENV] = previous_receipts


def _directory_listing_text(path: Path, *, recursive: bool, max_depth: int) -> str:
    root = path.resolve()
    entries = _directory_entries(root, recursive=recursive, max_depth=max_depth)
    lines = [
        f"# Directory: {root}",
        "",
        "- type: directory",
        f"- recursive: {str(recursive).lower()}",
        f"- max_depth: {max_depth}",
        f"- entry_count: {len(entries)}",
        "",
        "## Entries",
        "",
    ]
    for depth, rel in entries:
        target = root / rel
        name = rel.as_posix()
        suffix = "/" if target.is_dir() else "@"
        if target.is_file():
            suffix = ""
        indent = "  " * depth
        lines.append(f"{indent}- `{name}{suffix}`")
    return "\n".join(lines) + "\n"


def _extract_section(text: str, needle: str) -> str:
    lines = text.splitlines()
    if not needle.strip():
        return text
    pattern = needle.strip().casefold()
    start_index = -1
    heading_level = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            continue
        if pattern in match.group(2).casefold():
            start_index = index
            heading_level = len(match.group(1))
            break
    if start_index < 0:
        raise SystemExit(f"no section heading matched: {needle}")
    end_index = len(lines)
    for index in range(start_index + 1, len(lines)):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", lines[index])
        if match and len(match.group(1)) <= heading_level:
            end_index = index
            break
    return "\n".join(lines[start_index:end_index]).rstrip() + "\n"


def _apply_text_view(
    text: str,
    *,
    head: int,
    tail: int,
    section: str,
) -> str:
    viewed = _extract_section(text, section) if section else text
    lines = viewed.splitlines()
    if head > 0:
        lines = lines[:head]
    if tail > 0:
        lines = lines[-tail:]
    return "\n".join(lines) + ("\n" if lines else "")


def _render_viewed_bytes(data: bytes, *, language: str, head: int, tail: int, section: str) -> None:
    text = data.decode("utf-8", errors="replace")
    sys.stdout.write(_apply_text_view(text, head=head, tail=tail, section=section))


def _view_bytes(data: bytes, *, head: int, tail: int, section: str) -> bytes:
    text = data.decode("utf-8", errors="replace")
    return _apply_text_view(text, head=head, tail=tail, section=section).encode("utf-8")


def _remote_canonical_bytes(target: str) -> bytes:
    data, _content_type, truncated = fetch_url(target)
    if truncated:
        _emit_truncation_note()
    return data


def _remote_content_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _remote_extension(content_type: str) -> str:
    return _REMOTE_EXTENSION_BY_TYPE.get(_remote_content_type(content_type), "")


def _canonical_remote_name(target: str, content_type: str) -> str:
    parsed = urllib.parse.urlparse(target)
    raw_name = Path(parsed.path.rstrip("/")).name or parsed.netloc or "read"
    expected = _remote_extension(content_type)
    current = Path(raw_name).suffix.lower()
    if expected:
        if not current:
            return f"{raw_name}{expected}"
        if current != expected:
            return f"{Path(raw_name).stem}{expected}"
    if current:
        return raw_name
    return f"{raw_name}.bin"


def _project_html(data: bytes) -> bytes:
    try:
        markdown = html_markdown(data)
    except RuntimeError:
        markdown = None
    return markdown if markdown is not None else data


def _project_canonical(capture: Capture) -> bytes:
    content_type = capture.type.split(";", 1)[0].strip().lower()
    if content_type == "text/html":
        return _project_html(capture.data)
    if content_type in {"application/json", "text/json"}:
        return pretty_json(capture.data)
    if content_type.startswith("text/") or looks_text(capture.data):
        return capture.data
    lines = [
        "# Binary Content",
        "",
        f"- Content Type: `{capture.type or 'application/octet-stream'}`",
        f"- Bytes: {len(capture.data)}",
        "",
        "Use the provider-native surface or a raw file tool if you need the uninterpreted bytes.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _display_projection(capture: Capture) -> bytes:
    stored_projector = str(capture.meta.get("projector") or "").strip()
    if stored_projector:
        spec = get_plugin(stored_projector)
        if spec and spec.project is not None:
            try:
                return spec.project([], capture)
            except RuntimeError:
                pass
    return _project_canonical(capture)


def _stored_capture(path: Path) -> Capture:
    meta_path = path.parent / "meta.json"
    meta: dict[str, object] = {}
    if meta_path.exists():
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            meta = payload
    return Capture(
        data=path.read_bytes(),
        name=str(meta.get("original_name") or meta.get("preferred_name") or path.name or ""),
        type=str(meta.get("content_type") or ""),
        meta=dict(meta),
    )


def stored_display(path: Path) -> tuple[bytes, str]:
    if not _has_stored_metadata(path):
        data = path.read_bytes()
        return data, guess_lang_from_path(path.name)
    stored = _stored_capture(path)
    display = _display_projection(stored)
    language = (
        "markdown"
        if stored.type.split(";", 1)[0].strip().lower() == "text/html" and display != stored.data
        else guess_lang_from_content_type(stored.type) or guess_lang_from_path(path.name)
    )
    return display, language


def _has_stored_metadata(path: Path) -> bool:
    return (path.parent / "meta.json").exists()


def capture(argv: list[str], options: object) -> Capture:
    request = parse_args(argv)
    target = request.target
    try:
        resolved = resolve_read_target(argv, options)
    except TypeError:
        resolved = resolve_read_target(argv)
    if resolved.kind == "routed":
        assert resolved.routed_plugin is not None
        spec = get_plugin(resolved.routed_plugin)
        if spec and spec.capture is not None:
            return spec.capture(resolved.routed_argv, options)
        with capture_stdout() as captured:
            code = delegate(resolved.routed_plugin, resolved.routed_argv)
        if code != 0:
            detail = captured.getvalue().decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or f"`{resolved.routed_plugin}` capture failed")
        return Capture(data=captured.getvalue())
    if resolved.kind == "remote_url":
        assert target is not None
        data, content_type, truncated = fetch_url(target)
        if truncated:
            _emit_truncation_note()
        return Capture(
            data=data,
            name=_canonical_remote_name(target, content_type),
            type=content_type or "application/octet-stream",
        )
    raise RuntimeError(f"read target kind `{resolved.kind}` does not support canonical acquisition")


def project(argv: list[str], capture: Capture) -> bytes:
    request = parse_args(argv)
    try:
        resolved = resolve_read_target(
            argv,
            CommonOptions(
                session_dir=request.session or None,
                actor=request.actor or None,
            ),
        )
    except TypeError:
        resolved = resolve_read_target(argv)
    if resolved.kind == "routed":
        assert resolved.routed_plugin is not None
        spec = get_plugin(resolved.routed_plugin)
        if spec and spec.project is not None:
            projected = spec.project(resolved.routed_argv, capture)
        else:
            projected = capture.data
    else:
        projected = _project_canonical(capture)
    if request.head > 0 or request.tail > 0 or request.section:
        return _view_bytes(
            projected,
            head=max(0, request.head),
            tail=max(0, request.tail),
            section=request.section,
        )
    return projected


def execute_materializing_read(argv: list[str]) -> ReadExecution:
    request = parse_args(argv)
    captured = capture(
        argv,
        CommonOptions(
            session_dir=request.session or None,
            actor=request.actor or None,
        ),
    )
    return ReadExecution(
        code=0,
        canonical_bytes=captured.data,
        display_bytes=project(argv, captured),
    )


def _delegate_with_view(
    plugin: str,
    argv: list[str],
    *,
    head: int,
    tail: int,
    section: str,
) -> int:
    with capture_stdout() as capture:
        code = delegate(plugin, argv)
    data = capture.getvalue()
    if code != 0:
        sys.stdout.buffer.write(data)
        return code
    if head > 0 or tail > 0 or section:
        _render_viewed_bytes(data, language="markdown", head=head, tail=tail, section=section)
        return code
    sys.stdout.buffer.write(data)
    return code


def _directory_entries(path: Path, *, recursive: bool, max_depth: int) -> list[tuple[int, Path]]:
    entries: list[tuple[int, Path]] = []
    root = path.resolve()

    def walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            return
        for child in sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
            rel = child.relative_to(root)
            entries.append((depth, rel))
            if recursive and child.is_dir():
                walk(child, depth + 1)

    walk(root, 0)
    return entries


def render_directory(path: Path, *, recursive: bool, max_depth: int) -> None:
    sys.stdout.write(_directory_listing_text(path, recursive=recursive, max_depth=max_depth))


def main(argv: list[str]) -> int:
    if is_long_help_request(argv):
        return print_long_help(build_parser())
    if any(token in {"-h", "--help"} for token in argv):
        print(build_parser().format_help(), end="")
        return 0
    request = parse_args(argv)
    target = request.target

    if target is None:
        if sys.stdin.isatty():
            return print_usage()
        target = "-"

    if target == "-":
        data = sys.stdin.buffer.read()
        if request.head > 0 or request.tail > 0 or request.section:
            _render_viewed_bytes(
                data,
                language="txt",
                head=request.head,
                tail=request.tail,
                section=request.section,
            )
            return 0
        render_bytes(data, "txt")
        return 0

    try:
        resolved = resolve_read_target(
            argv,
            CommonOptions(
                session_dir=request.session or None,
                actor=request.actor or None,
            ),
        )
    except TypeError:
        resolved = resolve_read_target(argv)
    if resolved.kind == "routed":
        assert resolved.routed_plugin is not None
        return _delegate_with_view(
            resolved.routed_plugin,
            resolved.routed_argv,
            head=max(0, request.head),
            tail=max(0, request.tail),
            section=request.section,
        )
    if resolved.kind == "remote_url":
        try:
            data, content_type, truncated = fetch_url(target)
        except RuntimeError as exc:
            return die(str(exc), code=1)
        if truncated:
            _emit_truncation_note()
        display = _display_projection(
            Capture(data=data, type=content_type or "application/octet-stream")
        )
        language = guess_lang_from_content_type(content_type) or guess_lang_from_path(
            target.split("?", 1)[0]
        )
        if _remote_content_type(content_type) == "text/html" and display != data:
            language = "markdown"
        if request.head > 0 or request.tail > 0 or request.section:
            _render_viewed_bytes(
                display,
                language=language or "txt",
                head=max(0, request.head),
                tail=max(0, request.tail),
                section=request.section,
            )
            return 0
        render_bytes(display, language or "txt")
        return 0

    path = resolved.path
    if path is not None and path.is_file():
        display, language = stored_display(path)
        if request.head > 0 or request.tail > 0 or request.section:
            _render_viewed_bytes(
                display,
                language=language,
                head=max(0, request.head),
                tail=max(0, request.tail),
                section=request.section,
            )
            return 0
        render_bytes(display, language)
        return 0
    if path is not None and path.is_dir():
        if request.head > 0 or request.tail > 0 or request.section:
            sys.stdout.write(
                _apply_text_view(
                    _directory_listing_text(
                        path,
                        recursive=request.recursive,
                        max_depth=max(0, request.max_depth),
                    ),
                    head=max(0, request.head),
                    tail=max(0, request.tail),
                    section=request.section,
                )
            )
            return 0
        render_directory(path, recursive=request.recursive, max_depth=max(0, request.max_depth))
        return 0
    if resolved.kind == "missing_local" and path is not None:
        detail = (
            f"error: local target '{target}' does not exist at `{path}` under the current "
            "session context"
        )
        return die(detail, code=2)
    if resolved.kind == "missing_session_relative":
        return die(
            f"error: relative local target '{target}' requires an active or explicit session root; "
            "bind one with `gotta ...`, pass `--session <absolute-root>`, or use an "
            "absolute path directly"
        )
    return die(
        f"error: unsupported target '{target}'. no native local or routed provider path was found; "
        "record the limitation instead of working around it if this should have been followable "
        "through gotta"
    )


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main(sys.argv[1:]))
