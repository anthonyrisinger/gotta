#!/usr/bin/env python3
"""Read-only Granola retrieval through browser OAuth and legacy local sessions."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import datetime as dt
from email.utils import parsedate_to_datetime
import gzip
import html
import io
import json
import re
import shlex
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from gotta.capture import Capture, capture_json_command, json_bytes
from gotta.config import display_path, user_state_dir
from gotta.content.file import write_text_atomic
from gotta.projection import Projection, projection_bytes
from gotta.helptext import is_long_help_request, print_long_help
from gotta.project import pretty_json
from gotta.resolve.route import split_locator_tail
from gotta.resolve.search import plain_text_search_route
from gotta.source.render import render_source_metadata_lines
from gotta.vault import load_secret_json_object, write_secret_json_atomic


DEFAULT_SUPABASE = (
    Path.home() / "Library" / "Application Support" / "Granola" / "supabase.json"
)
DEFAULT_API_URL = "https://api.granola.ai/v2/get-documents"
DEFAULT_TRANSCRIPT_API_URL = "https://api.granola.ai/v1/get-document-transcript"
DEFAULT_REFRESH_API_URL = "https://api.granola.ai/v1/refresh-access-token"
DEFAULT_MCP_URL = "https://mcp.granola.ai/mcp"
DEFAULT_MCP_OAUTH_ISSUER = "https://mcp-auth.granola.ai"
DEFAULT_MCP_OAUTH_SCOPE = "openid profile email offline_access"
DEFAULT_MCP_REDIRECT_URI = "http://127.0.0.1:8765/callback"
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_OAUTH_DIR = user_state_dir() / "auth" / "granola"
MCP_OAUTH_FILE = MCP_OAUTH_DIR / "oauth.json"
MCP_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_LIST_LIMIT = 10
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_EXPORT_LIMIT = 20
DEFAULT_NOTE_TIME_RANGE = "last_90_days"
DEFAULT_TRANSCRIPT_SEARCH_TIME_RANGE = "last_30_days"
USER_ACTOR = "Granola/5.354.0"
CLIENT_VERSION = "5.354.0"
LEGACY_DOCUMENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
MCP_MEETING_DATE_RE = re.compile(
    r"^(?P<month>[A-Za-z]{3}) (?P<day>\d{1,2}), (?P<year>\d{4}) "
    r"(?P<time>\d{1,2}:\d{2}) (?P<ampm>AM|PM) (?P<timezone>[A-Za-z0-9_+:/-]+)$"
)
MCP_TIMEZONE_OFFSETS = {
    "UTC": 0,
    "GMT": 0,
    "EST": -5,
    "EDT": -4,
    "CST": -6,
    "CDT": -5,
    "MST": -7,
    "MDT": -6,
    "PST": -8,
    "PDT": -7,
}


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


@dataclass(frozen=True)
class GranolaSession:
    mode: str
    token: str = ""
    documents_url: str = ""
    transcript_url: str = ""


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
    return bool(LEGACY_DOCUMENT_ID_RE.fullmatch(raw.strip()))


DISCOVERY_COMMANDS = {"list", "search", "search-transcript"}
EVIDENCE_COMMANDS = {"get", "transcript"}
NON_ARTIFACT_COMMANDS = {"auth", "export", "status"}


def artifact_intent(argv: list[str]) -> str:
    if not argv:
        return "none"
    command = argv[0]
    if command in NON_ARTIFACT_COMMANDS:
        return "none"
    if command in DISCOVERY_COMMANDS:
        return "discovery"
    if command in EVIDENCE_COMMANDS:
        return "evidence"
    return "none"


def _search_get_target(subject: str) -> str:
    subject = subject.strip()
    if not subject:
        return ""
    try:
        return canonical_locator(["get", subject])
    except Exception:
        return f"granola:{subject}"


def _search_transcript_target(subject: str) -> str:
    subject = subject.strip()
    if not subject:
        return ""
    try:
        return canonical_locator(["transcript", subject])
    except Exception:
        return f"granola:transcript {subject}"


def search_route(raw_tail: str) -> list[str]:
    return plain_text_search_route(
        "granola",
        raw_tail,
        specialized_commands={
            "list": "granola list",
            "search-transcript": "granola search-transcript",
        },
        read_redirects={
            "get": _search_get_target,
            "transcript": _search_transcript_target,
        },
    )


def canonical_locator(argv: list[str]) -> str:
    args = _parse_cli(argv)
    if args.command == "status":
        return "granola:status"
    if args.command == "auth":
        return f"granola:{shlex.join(argv)}"
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


def _workos_tokens(wrapper: dict[str, Any], supabase_path: Path) -> dict[str, Any]:
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
    if not isinstance(tokens, dict):
        raise ToolError(
            f"Granola local session token blob is not a JSON object: {supabase_path}"
        )
    return tokens


def _session_token(wrapper: dict[str, Any], supabase_path: Path, key: str) -> str:
    tokens = _workos_tokens(wrapper, supabase_path)
    return str(tokens.get(key) or "").strip()


def load_access_token(supabase_path: Path) -> str:
    wrapper = _read_json(supabase_path)
    token = _session_token(wrapper, supabase_path, "access_token")
    if not token:
        raise ToolError(
            f"Granola local session does not contain an access token: {supabase_path}"
        )
    return token


def load_refresh_token(supabase_path: Path) -> str:
    wrapper = _read_json(supabase_path)
    token = _session_token(wrapper, supabase_path, "refresh_token")
    if not token:
        raise ToolError(
            f"Granola local session does not contain a refresh token: {supabase_path}"
        )
    return token


def ensure_access_token(supabase_path: Path, refresh_api_url: str) -> str:
    token = load_access_token(supabase_path)
    if _session_is_current(token):
        return token
    refresh_session_payload(
        supabase_path=supabase_path,
        refresh_api_url=refresh_api_url,
    )
    return load_access_token(supabase_path)


def _iso_file_mtime(path: Path) -> str:
    try:
        stamp = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
    except OSError:
        return ""
    return stamp.isoformat().replace("+00:00", "Z")


def _jwt_timestamp(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, int | float):
        return ""
    stamp = dt.datetime.fromtimestamp(value, dt.timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        decoded = json.loads(payload)
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def access_token_metadata_for_token(token: str) -> dict[str, Any]:
    payload = _decode_jwt_payload(token)
    if not payload:
        return {}
    expires_at = _jwt_timestamp(payload, "exp")
    issued_at = _jwt_timestamp(payload, "iat")
    metadata: dict[str, Any] = {
        "accessTokenIssuedAt": issued_at,
        "accessTokenExpiresAt": expires_at,
    }
    exp = payload.get("exp")
    if isinstance(exp, int | float):
        metadata["accessTokenExpired"] = utc_now().timestamp() >= float(exp)
    return metadata


def access_token_metadata(supabase_path: Path) -> dict[str, Any]:
    try:
        token = load_access_token(supabase_path)
    except ToolError:
        return {}
    return access_token_metadata_for_token(token)


def encrypted_session_metadata(supabase_path: Path) -> dict[str, Any]:
    encrypted_path = supabase_path.with_name(f"{supabase_path.name}.enc")
    plaintext_mtime = _iso_file_mtime(supabase_path)
    encrypted_mtime = _iso_file_mtime(encrypted_path)
    metadata: dict[str, Any] = {
        "encryptedSessionFile": str(encrypted_path),
        "encryptedSessionPresent": encrypted_path.exists(),
        "plaintextSessionModifiedAt": plaintext_mtime,
        "encryptedSessionModifiedAt": encrypted_mtime,
        "encryptedSessionNewer": False,
    }
    try:
        metadata["encryptedSessionNewer"] = (
            encrypted_path.exists()
            and supabase_path.exists()
            and encrypted_path.stat().st_mtime > supabase_path.stat().st_mtime
        )
    except OSError:
        metadata["encryptedSessionNewer"] = False
    return metadata


def _mcp_login_setup_step() -> str:
    return "run `gotta granola auth login` to sign in through your browser"


def _encrypted_session_unavailable_message(supabase_path: Path) -> str:
    encrypted_path = supabase_path.with_name(f"{supabase_path.name}.enc")
    return (
        "Granola desktop session state is application-protected, so gotta cannot "
        f"read {encrypted_path}; {_mcp_login_setup_step()}."
    )


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


def ensure_mcp_oauth_dir() -> None:
    MCP_OAUTH_DIR.mkdir(parents=True, exist_ok=True)
    try:
        MCP_OAUTH_DIR.chmod(0o700)
    except OSError:
        pass


def load_mcp_oauth_state() -> dict[str, Any] | None:
    if not MCP_OAUTH_FILE.exists():
        return None
    try:
        state, _recovered = load_secret_json_object(MCP_OAUTH_FILE)
    except (OSError, ValueError) as exc:
        raise ToolError(
            f"invalid Granola OAuth state file {MCP_OAUTH_FILE}: {exc}"
        ) from exc
    return state


def persist_mcp_oauth_state(state: dict[str, Any]) -> dict[str, Any]:
    write_secret_json_atomic(
        MCP_OAUTH_FILE,
        state,
        ensure_dir=ensure_mcp_oauth_dir,
        indent=2,
        sort_keys=True,
        trailing_newline=True,
    )
    return state


def _json_object_from_bytes(raw: bytes, *, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        detail = raw.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ToolError(f"{context} returned invalid JSON{suffix}") from exc
    if not isinstance(payload, dict):
        raise ToolError(f"{context} returned an unexpected payload shape")
    return payload


def oauth_get_json(url: str, *, context: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "gotta/granola-plugin")
    try:
        with urllib.request.urlopen(
            request, timeout=MCP_REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ToolError(f"{context} failed with HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"{context} failed: {exc.reason}") from exc
    return _json_object_from_bytes(raw, context=context)


def oauth_post_json(
    url: str,
    payload: dict[str, Any],
    *,
    context: str,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "gotta/granola-plugin")
    try:
        with urllib.request.urlopen(
            request, timeout=MCP_REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()
        try:
            error_payload = _json_object_from_bytes(detail, context=context)
        except ToolError:
            text = detail.decode("utf-8", errors="replace").strip()
            suffix = f": {text}" if text else ""
            raise ToolError(f"{context} failed with HTTP {exc.code}{suffix}") from exc
        error = str(
            error_payload.get("error_description") or error_payload.get("error") or ""
        ).strip()
        suffix = f": {error}" if error else ""
        raise ToolError(f"{context} failed with HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"{context} failed: {exc.reason}") from exc
    return _json_object_from_bytes(raw, context=context)


def oauth_post_form_json(
    url: str,
    payload: dict[str, str],
    *,
    context: str,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("User-Agent", "gotta/granola-plugin")
    try:
        with urllib.request.urlopen(
            request, timeout=MCP_REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return _json_object_from_bytes(raw, context=context)
        except ToolError:
            detail = raw.decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise ToolError(f"{context} failed with HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"{context} failed: {exc.reason}") from exc
    return _json_object_from_bytes(raw, context=context)


def mcp_oauth_metadata_url(issuer: str) -> str:
    return f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server"


def mcp_protected_resource_metadata_url(mcp_url: str) -> str:
    parsed = urllib.parse.urlparse(mcp_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolError(f"invalid Granola MCP URL: {mcp_url}")
    resource_path = parsed.path.rstrip("/")
    metadata_path = f"/.well-known/oauth-protected-resource{resource_path}"
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, metadata_path, "", "", "")
    )


def discover_mcp_authorization_server(
    *,
    mcp_url: str,
    preferred_issuer: str,
) -> str:
    metadata = oauth_get_json(
        mcp_protected_resource_metadata_url(mcp_url),
        context="Granola MCP protected-resource discovery",
    )
    resource = str(metadata.get("resource") or "").rstrip("/")
    if resource and resource != mcp_url.rstrip("/"):
        raise ToolError(
            "Granola MCP protected-resource metadata does not match the MCP URL"
        )
    raw_servers = metadata.get("authorization_servers")
    servers = (
        [
            str(server).rstrip("/")
            for server in raw_servers
            if isinstance(server, str) and server.strip()
        ]
        if isinstance(raw_servers, list)
        else []
    )
    if not servers:
        raise ToolError(
            "Granola MCP protected-resource metadata has no authorization server"
        )
    preferred = preferred_issuer.rstrip("/")
    if preferred and preferred in servers:
        return preferred
    if preferred and preferred != DEFAULT_MCP_OAUTH_ISSUER:
        raise ToolError(
            f"configured Granola OAuth issuer {preferred!r} is not authorized "
            "by the MCP protected resource"
        )
    return servers[0]


def _oauth_error(payload: dict[str, Any], *, context: str) -> str:
    code = str(payload.get("error") or "").strip()
    detail = str(payload.get("error_description") or "").strip()
    if code and detail:
        return f"{context} failed: {code}: {detail}"
    if code:
        return f"{context} failed: {code}"
    return ""


def ensure_mcp_oauth_client(
    *,
    issuer: str,
    scope: str = DEFAULT_MCP_OAUTH_SCOPE,
    redirect_uri: str = DEFAULT_MCP_REDIRECT_URI,
) -> dict[str, Any]:
    state = load_mcp_oauth_state() or {}
    stored_issuer = str(state.get("authorization_server") or "").rstrip("/")
    normalized_issuer = issuer.rstrip("/")
    client_expires_at = state.get("client_id_expires_at")
    client_expired = (
        isinstance(client_expires_at, int | float)
        and float(client_expires_at) > 0
        and time.time() >= float(client_expires_at)
    )
    if (stored_issuer and stored_issuer != normalized_issuer) or client_expired:
        for key in (
            "client_id",
            "client_id_issued_at",
            "client_id_expires_at",
            "client_secret",
            "client_secret_expires_at",
            "registration_access_token",
            "registration_client_uri",
            "access_token",
            "refresh_token",
            "token_type",
            "expires_at",
            "resource",
        ):
            state.pop(key, None)
    metadata = oauth_get_json(
        mcp_oauth_metadata_url(issuer),
        context="Granola OAuth metadata discovery",
    )
    token_endpoint = str(metadata.get("token_endpoint") or "").strip()
    device_endpoint = str(metadata.get("device_authorization_endpoint") or "").strip()
    registration_endpoint = str(metadata.get("registration_endpoint") or "").strip()
    if not token_endpoint or not device_endpoint or not registration_endpoint:
        raise ToolError(
            "Granola OAuth metadata is missing token, device, or registration endpoints"
        )
    state.update(
        {
            "authorization_server": normalized_issuer,
            "registration_endpoint": registration_endpoint,
            "device_authorization_endpoint": device_endpoint,
            "token_endpoint": token_endpoint,
            "scope": scope,
        }
    )
    client_id = str(state.get("client_id") or "").strip()
    if not client_id:
        registration = oauth_post_json(
            registration_endpoint,
            {
                "client_name": "gotta local CLI",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": scope,
            },
            context="Granola OAuth client registration",
        )
        error = _oauth_error(registration, context="Granola OAuth client registration")
        if error:
            raise ToolError(error)
        client_id = str(registration.get("client_id") or "").strip()
        if not client_id:
            raise ToolError(
                "Granola OAuth client registration did not return a client id"
            )
        state.update(registration)
        state["client_id"] = client_id
    return persist_mcp_oauth_state(state)


def _store_mcp_token_response(
    state: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    access_token = str(response.get("access_token") or "").strip()
    if not access_token:
        raise ToolError("Granola OAuth token response did not include an access token")
    refresh_token = str(
        response.get("refresh_token") or state.get("refresh_token") or ""
    ).strip()
    expires_in = response.get("expires_in")
    expires_at = None
    if isinstance(expires_in, int | float):
        expires_at = time.time() + float(expires_in)
    state.update(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": str(
                response.get("token_type") or state.get("token_type") or "Bearer"
            ),
            "scope": str(response.get("scope") or state.get("scope") or ""),
            "expires_at": expires_at,
        }
    )
    return persist_mcp_oauth_state(state)


def mcp_oauth_token_is_expired(
    state: dict[str, Any],
    *,
    skew_seconds: int = 60,
) -> bool:
    expires_at = state.get("expires_at")
    if isinstance(expires_at, int | float):
        return time.time() + skew_seconds >= float(expires_at)
    token = str(state.get("access_token") or "").strip()
    metadata = access_token_metadata_for_token(token)
    return metadata.get("accessTokenExpired") is True


def run_mcp_oauth_login(
    *,
    mcp_url: str,
    issuer: str,
    scope: str = DEFAULT_MCP_OAUTH_SCOPE,
    open_browser: bool = True,
) -> dict[str, Any]:
    discovered_issuer = discover_mcp_authorization_server(
        mcp_url=mcp_url,
        preferred_issuer=issuer,
    )
    state = ensure_mcp_oauth_client(issuer=discovered_issuer, scope=scope)
    client_id = str(state.get("client_id") or "").strip()
    device_endpoint = str(state.get("device_authorization_endpoint") or "").strip()
    token_endpoint = str(state.get("token_endpoint") or "").strip()
    response = oauth_post_form_json(
        device_endpoint,
        {
            "client_id": client_id,
            "scope": scope,
            "resource": mcp_url,
        },
        context="Granola device authorization",
    )
    error = _oauth_error(response, context="Granola device authorization")
    if error:
        raise ToolError(error)
    device_code = str(response.get("device_code") or "").strip()
    user_code = str(response.get("user_code") or "").strip()
    verification_url = str(
        response.get("verification_uri_complete")
        or response.get("verification_uri")
        or ""
    ).strip()
    if not device_code or not verification_url:
        raise ToolError(
            "Granola device authorization did not return a verification URL and code"
        )
    expires_in = response.get("expires_in")
    lifetime = float(expires_in) if isinstance(expires_in, int | float) else 300.0
    interval_value = response.get("interval")
    interval = (
        max(float(interval_value), 1.0)
        if isinstance(interval_value, int | float)
        else 5.0
    )
    print(
        "Granola browser authorization is required.",
        file=sys.stderr,
        flush=True,
    )
    print(
        f"verification URL: {verification_url}",
        file=sys.stderr,
        flush=True,
    )
    if user_code:
        print(f"user code: {user_code}", file=sys.stderr, flush=True)
    if open_browser and not webbrowser.open(verification_url):
        print(
            "failed to auto-open browser; open the verification URL manually",
            file=sys.stderr,
            flush=True,
        )

    deadline = time.monotonic() + lifetime
    while time.monotonic() < deadline:
        time.sleep(interval)
        token_response = oauth_post_form_json(
            token_endpoint,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
                "resource": mcp_url,
            },
            context="Granola OAuth token exchange",
        )
        error_code = str(token_response.get("error") or "").strip()
        if error_code == "authorization_pending":
            continue
        if error_code == "slow_down":
            interval += 5.0
            continue
        if error_code:
            raise ToolError(
                _oauth_error(
                    token_response,
                    context="Granola OAuth token exchange",
                )
            )
        state["resource"] = mcp_url
        return _store_mcp_token_response(state, token_response)
    raise ToolError("timed out waiting for Granola browser authorization")


def refresh_mcp_oauth_state(
    state: dict[str, Any],
    *,
    mcp_url: str,
) -> dict[str, Any]:
    refresh_token = str(state.get("refresh_token") or "").strip()
    client_id = str(state.get("client_id") or "").strip()
    token_endpoint = str(state.get("token_endpoint") or "").strip()
    if not refresh_token:
        raise ToolError(
            "cached Granola browser authorization is missing a refresh token; "
            "run `gotta granola auth login`"
        )
    if not client_id or not token_endpoint:
        raise ToolError(
            "cached Granola browser authorization is incomplete; "
            "run `gotta granola auth login`"
        )
    response = oauth_post_form_json(
        token_endpoint,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": mcp_url,
        },
        context="Granola OAuth token refresh",
    )
    error = _oauth_error(response, context="Granola OAuth token refresh")
    if error:
        raise ToolError(f"{error}; run `gotta granola auth login`")
    return _store_mcp_token_response(state, response)


def ensure_mcp_access_token(*, mcp_url: str) -> str:
    state = load_mcp_oauth_state()
    if not state:
        raise ToolError(
            "Granola browser authorization is not configured; "
            "run `gotta granola auth login`"
        )
    access_token = str(state.get("access_token") or "").strip()
    if not access_token:
        raise ToolError(
            "Granola browser authorization is incomplete; "
            "run `gotta granola auth login`"
        )
    if mcp_oauth_token_is_expired(state):
        state = refresh_mcp_oauth_state(state, mcp_url=mcp_url)
        access_token = str(state.get("access_token") or "").strip()
    return access_token


def clear_mcp_oauth_tokens() -> bool:
    state = load_mcp_oauth_state()
    if not state:
        return False
    removed = False
    for key in (
        "access_token",
        "refresh_token",
        "token_type",
        "expires_at",
        "resource",
    ):
        removed = state.pop(key, None) is not None or removed
    persist_mcp_oauth_state(state)
    return removed


def request_json(
    url: str,
    token: str,
    payload: dict[str, Any],
    *,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "*/*")
    request.add_header("Accept-Encoding", "gzip")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Actor", USER_ACTOR)
    request.add_header("X-Client-Version", CLIENT_VERSION)
    for key, value in (extra_headers or {}).items():
        request.add_header(key, value)
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


def post_json(
    url: str,
    token: str,
    payload: dict[str, Any],
    *,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = request_json(url, token, payload, extra_headers=extra_headers)
    if not isinstance(response, dict):
        raise ToolError("Granola API returned an unexpected payload shape")
    return response


def _parse_mcp_sse(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace")
    payloads: list[dict[str, Any]] = []
    for event in re.split(r"\r?\n\r?\n", text):
        data = "\n".join(
            line.removeprefix("data:").lstrip()
            for line in event.splitlines()
            if line.startswith("data:")
        ).strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ToolError("Granola MCP returned an invalid event stream") from exc
        if isinstance(payload, dict):
            payloads.append(payload)
    if not payloads:
        raise ToolError("Granola MCP returned an empty event stream")
    return payloads[-1]


def _parse_mcp_http_body(raw: bytes, content_type: str) -> dict[str, Any] | None:
    if not raw.strip():
        return None
    if "text/event-stream" in content_type:
        return _parse_mcp_sse(raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError("Granola MCP returned invalid JSON") from exc
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ToolError("Granola MCP returned an unexpected payload shape")
    return payload


def mcp_http_request(
    *,
    mcp_url: str,
    token: str,
    payload: dict[str, Any],
    session_id: str = "",
    protocol_version: str = "",
) -> tuple[dict[str, Any] | None, str]:
    request = urllib.request.Request(
        mcp_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json, text/event-stream")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "gotta/granola-plugin")
    if session_id:
        request.add_header("Mcp-Session-Id", session_id)
    if protocol_version:
        request.add_header("MCP-Protocol-Version", protocol_version)
    try:
        with urllib.request.urlopen(
            request, timeout=MCP_REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw = response.read()
            content_type = str(response.headers.get("Content-Type") or "")
            response_session_id = str(
                response.headers.get("Mcp-Session-Id") or session_id
            ).strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ToolError(
            f"Granola MCP request failed with HTTP {exc.code}{suffix}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"Granola MCP request failed: {exc.reason}") from exc
    return _parse_mcp_http_body(raw, content_type), response_session_id


class GranolaMcpClient:
    def __init__(self, mcp_url: str, token: str) -> None:
        self.mcp_url = mcp_url
        self.token = token
        self.session_id = ""
        self.protocol_version = ""
        self.next_request_id = 1
        self.initialized = False

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        notification: bool = False,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = self.next_request_id
            self.next_request_id += 1
        response, response_session_id = mcp_http_request(
            mcp_url=self.mcp_url,
            token=self.token,
            payload=payload,
            session_id=self.session_id,
            protocol_version=self.protocol_version,
        )
        if response_session_id:
            self.session_id = response_session_id
        if notification:
            return None
        if response is None:
            raise ToolError(f"Granola MCP returned no response for {method}")
        error = response.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "").strip()
            message = str(error.get("message") or "").strip()
            detail = ": ".join(part for part in (code, message) if part)
            raise ToolError(
                f"Granola MCP {method} failed" + (f": {detail}" if detail else "")
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise ToolError(f"Granola MCP returned an invalid result for {method}")
        return result

    def initialize(self) -> None:
        if self.initialized:
            return
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "gotta",
                    "version": "local",
                },
            },
        )
        if not isinstance(result, dict):
            raise ToolError("Granola MCP initialization returned no result")
        self.protocol_version = str(
            result.get("protocolVersion") or MCP_PROTOCOL_VERSION
        ).strip()
        self._request("notifications/initialized", notification=True)
        self.initialized = True

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        result = self._request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
        )
        if not isinstance(result, dict):
            raise ToolError(f"Granola MCP tool {name} returned no result")
        if result.get("isError"):
            detail = mcp_tool_text(result).strip()
            raise ToolError(
                f"Granola MCP tool {name} failed" + (f": {detail}" if detail else "")
            )
        return result


def call_mcp_tool(
    mcp_url: str,
    token: str,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return GranolaMcpClient(mcp_url, token).call_tool(name, arguments)


def mcp_tool_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        raise ToolError("Granola MCP tool result is missing content")
    chunks = [
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and str(item.get("text") or "").strip()
    ]
    if not chunks:
        raise ToolError("Granola MCP tool result does not contain text")
    return "\n".join(chunks)


def _mcp_xml_root(text: str) -> ET.Element:
    start = text.find("<meetings_data")
    end_tag = "</meetings_data>"
    end = text.rfind(end_tag)
    if start < 0 or end < start:
        raise ToolError("Granola MCP response is missing meetings data")
    fragment = text[start : end + len(end_tag)]
    try:
        return ET.fromstring(fragment)
    except ET.ParseError as exc:
        raise ToolError("Granola MCP returned invalid meetings data") from exc


def _mcp_meeting_timestamp(raw: str) -> str:
    value = raw.strip()
    match = MCP_MEETING_DATE_RE.fullmatch(value)
    if not match:
        return value
    parts = match.groupdict()
    local_text = (
        f"{parts['month']} {parts['day']} {parts['year']} "
        f"{parts['time']} {parts['ampm']}"
    )
    timezone_offset = MCP_TIMEZONE_OFFSETS.get(parts["timezone"].upper())
    if timezone_offset is not None:
        try:
            parsed = dt.datetime.strptime(local_text, "%b %d %Y %I:%M %p")
        except ValueError:
            return value
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=timezone_offset)))
        return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    normalized = (
        f"{parts['day']} {parts['month']} {parts['year']} "
        f"{parts['time']} {parts['ampm']} {parts['timezone']}"
    )
    try:
        parsed = parsedate_to_datetime(normalized)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = dt.datetime.strptime(local_text, "%b %d %Y %I:%M %p")
        except ValueError:
            return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _mcp_participants(raw: str) -> list[dict[str, str]]:
    people: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?P<label>[^,\n]+?)\s*<(?P<email>[^<>]+)>", raw):
        email = match.group("email").strip()
        label = re.sub(
            r"\s*\(note creator\)\s*",
            " ",
            match.group("label"),
            flags=re.IGNORECASE,
        ).strip()
        name = re.split(r"\s+from\s+", label, maxsplit=1, flags=re.IGNORECASE)[0]
        key = email.casefold() or name.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        person = {"email": email}
        if name:
            person["name"] = name
        people.append(person)
    return people


def normalize_mcp_meeting(meeting: ET.Element) -> dict[str, Any]:
    meeting_id = str(meeting.attrib.get("id") or "").strip()
    title = str(meeting.attrib.get("title") or "Untitled").strip()
    raw_date = str(meeting.attrib.get("date") or "").strip()
    timestamp = _mcp_meeting_timestamp(raw_date)
    participants_element = meeting.find("known_participants")
    participants = (
        "".join(participants_element.itertext()).strip()
        if participants_element is not None
        else ""
    )
    summary_element = meeting.find("summary")
    summary = (
        "".join(summary_element.itertext()).strip()
        if summary_element is not None
        else ""
    )
    normalized: dict[str, Any] = {
        "id": meeting_id,
        "title": title,
        "created_at": timestamp,
        "updated_at": timestamp,
        "meeting_date": raw_date,
        "people": _mcp_participants(participants),
        "notes_markdown": summary,
        "web_url": f"https://notes.granola.ai/d/{meeting_id}" if meeting_id else "",
        "_granola_api": "mcp",
    }
    for key in (
        "captured_by_me",
        "listed_as_participant",
        "is_workspace_visible",
    ):
        if key in meeting.attrib:
            normalized[key] = str(meeting.attrib[key]).strip().lower() == "true"
    return normalized


def parse_mcp_meetings(text: str) -> list[dict[str, Any]]:
    root = _mcp_xml_root(text)
    return [normalize_mcp_meeting(meeting) for meeting in root.findall("meeting")]


def _mcp_list_arguments(
    *,
    created_after: str,
    created_before: str,
) -> dict[str, Any]:
    if not created_after and not created_before:
        return {"time_range": "last_30_days"}
    start = (
        (dt.date.fromisoformat(created_after[:10]) - dt.timedelta(days=1)).isoformat()
        if created_after
        else "1970-01-01"
    )
    end = (
        (dt.date.fromisoformat(created_before[:10]) + dt.timedelta(days=1)).isoformat()
        if created_before
        else (utc_now().date() + dt.timedelta(days=1)).isoformat()
    )
    return {
        "time_range": "custom",
        "custom_start": start,
        "custom_end": end,
    }


def fetch_mcp_documents(
    mcp_url: str,
    token: str,
    limit: int | None = None,
    *,
    created_after: str = "",
    created_before: str = "",
) -> list[dict[str, Any]]:
    result = call_mcp_tool(
        mcp_url,
        token,
        "list_meetings",
        _mcp_list_arguments(
            created_after=created_after,
            created_before=created_before,
        ),
    )
    documents = parse_mcp_meetings(mcp_tool_text(result))
    return documents if limit is None else documents[:limit]


def fetch_mcp_meetings(
    mcp_url: str,
    token: str,
    meeting_ids: list[str],
) -> list[dict[str, Any]]:
    if not meeting_ids:
        return []
    if len(meeting_ids) > 10:
        raise ToolError("Granola MCP can fetch at most 10 meetings per request")
    result = call_mcp_tool(
        mcp_url,
        token,
        "get_meetings",
        {"meeting_ids": meeting_ids},
    )
    return parse_mcp_meetings(mcp_tool_text(result))


def fetch_mcp_document(
    mcp_url: str,
    token: str,
    document_id: str,
) -> dict[str, Any]:
    documents = fetch_mcp_meetings(mcp_url, token, [document_id])
    for document in documents:
        if str(document.get("id") or "") == document_id:
            return document
    raise ToolError(f"Granola MCP could not find meeting {document_id!r}")


def _mcp_transcript_payload(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ToolError("Granola MCP transcript response is missing JSON data")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ToolError("Granola MCP returned an invalid transcript payload") from exc
    if not isinstance(payload, dict):
        raise ToolError("Granola MCP returned an unexpected transcript payload")
    return payload


def normalize_mcp_transcript(
    document_id: str,
    transcript: str,
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    current_speaker = ""
    current_lines: list[str] = []

    def append_segment() -> None:
        if not current_speaker or not any(line.strip() for line in current_lines):
            return
        index = len(segments) + 1
        segments.append(
            {
                "id": f"{document_id}:{index}",
                "document_id": document_id,
                "speaker": {"name": current_speaker},
                "source": "speaker",
                "text": "\n".join(current_lines).strip(),
                "is_final": True,
                "start_timestamp": "",
                "end_timestamp": "",
            }
        )

    for line in transcript.splitlines():
        match = re.match(r"^(?P<speaker>[^:\n]{1,80}):\s*(?P<text>.*)$", line)
        if match:
            append_segment()
            current_speaker = match.group("speaker").strip()
            current_lines = [match.group("text")]
        elif current_speaker:
            current_lines.append(line)
    append_segment()
    if not segments and transcript.strip():
        segments.append(
            {
                "id": f"{document_id}:1",
                "document_id": document_id,
                "speaker": {"name": "Unknown"},
                "source": "speaker",
                "text": transcript.strip(),
                "is_final": True,
                "start_timestamp": "",
                "end_timestamp": "",
            }
        )
    return segments


def fetch_mcp_transcript(
    mcp_url: str,
    token: str,
    document_id: str,
) -> list[dict[str, Any]]:
    result = call_mcp_tool(
        mcp_url,
        token,
        "get_meeting_transcript",
        {"meeting_id": document_id},
    )
    payload = _mcp_transcript_payload(mcp_tool_text(result))
    transcript = str(payload.get("transcript") or "")
    return normalize_mcp_transcript(document_id, transcript)


def _workos_token_blob_was_string(wrapper: dict[str, Any]) -> bool:
    return isinstance(wrapper.get("workos_tokens"), str)


def _store_workos_tokens(
    wrapper: dict[str, Any],
    tokens: dict[str, Any],
    *,
    preserve_string_blob: bool,
) -> None:
    if preserve_string_blob:
        wrapper["workos_tokens"] = json.dumps(tokens, separators=(",", ":"))
    else:
        wrapper["workos_tokens"] = tokens
    session_id = str(tokens.get("session_id") or "").strip()
    if session_id:
        wrapper["session_id"] = session_id


def _session_is_current(access_token: str) -> bool:
    if not access_token:
        return False
    metadata = access_token_metadata_for_token(access_token)
    return metadata.get("accessTokenExpired") is not True


def refresh_session_payload(
    *,
    supabase_path: Path,
    refresh_api_url: str,
    force: bool = False,
) -> dict[str, Any]:
    wrapper = _read_json(supabase_path)
    tokens = _workos_tokens(wrapper, supabase_path)
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    token_metadata = access_token_metadata_for_token(access_token)
    payload: dict[str, Any] = {
        "surface": "granola",
        "command": "auth refresh",
        "supabaseFile": str(supabase_path),
        "refreshApiUrl": refresh_api_url,
        "refreshed": False,
        "forced": force,
        "accessTokenPresent": bool(access_token),
        "refreshTokenPresent": bool(refresh_token),
        "accessTokenExpiresAt": token_metadata.get("accessTokenExpiresAt", ""),
        "accessTokenExpired": bool(token_metadata.get("accessTokenExpired")),
        "status": "skipped",
        "nextStep": "ready",
    }
    if not access_token:
        raise ToolError(
            f"Granola local session does not contain an access token: {supabase_path}"
        )
    if not refresh_token:
        raise ToolError(
            f"Granola local session does not contain a refresh token: {supabase_path}"
        )
    if not force and _session_is_current(access_token):
        payload["reason"] = "plaintext session already has a non-expired access token"
        return payload

    response = post_json(
        refresh_api_url,
        access_token,
        {"refresh_token": refresh_token},
    )
    refreshed_access_token = str(response.get("access_token") or "").strip()
    if not refreshed_access_token:
        raise ToolError("Granola auth refresh did not return an access token")
    refreshed_tokens = {**tokens, **response}
    refreshed_tokens["access_token"] = refreshed_access_token
    refreshed_tokens["refresh_token"] = str(
        refreshed_tokens.get("refresh_token") or refresh_token
    ).strip()
    refreshed_tokens["obtained_at"] = int(utc_now().timestamp() * 1000)
    _store_workos_tokens(
        wrapper,
        refreshed_tokens,
        preserve_string_blob=_workos_token_blob_was_string(wrapper),
    )
    write_text_atomic(
        supabase_path, json.dumps(wrapper, indent=2, sort_keys=True) + "\n"
    )
    refreshed_metadata = access_token_metadata_for_token(refreshed_access_token)
    payload.update(
        {
            "refreshed": True,
            "status": "refreshed",
            "accessTokenExpiresAt": refreshed_metadata.get("accessTokenExpiresAt", ""),
            "accessTokenExpired": bool(refreshed_metadata.get("accessTokenExpired")),
            "nextStep": "rerun `gotta granola status` or the original Granola command",
        }
    )
    return payload


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
            for key in (
                "name",
                "display_name",
                "displayName",
                "full_name",
                "fullName",
                "email",
            ):
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

    summary_markdown = str(document.get("summary_markdown") or "").strip()
    if summary_markdown:
        return RenderedNote(summary_markdown + "\n", "summary_markdown")

    summary_text = str(document.get("summary_text") or "").strip()
    if summary_text:
        return RenderedNote(summary_text + "\n", "summary_text")

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
    for field in (
        "notes_markdown",
        "notes_plain",
        "summary_markdown",
        "summary_text",
        "content",
    ):
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
    session: GranolaSession,
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
        segments = _fetch_session_transcript(session, document_id)
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
    speaker = segment.get("speaker")
    if isinstance(speaker, dict):
        for key in ("name", "diarization_label"):
            value = str(speaker.get(key) or "").strip()
            if value:
                return value
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


def _load_session(args: argparse.Namespace) -> GranolaSession:
    oauth_state = load_mcp_oauth_state()
    if oauth_state and str(oauth_state.get("access_token") or "").strip():
        return GranolaSession(
            mode="mcp_oauth",
            token=ensure_mcp_access_token(mcp_url=args.mcp_url),
            documents_url=args.mcp_url,
            transcript_url=args.mcp_url,
        )
    try:
        token = ensure_access_token(args.supabase, args.refresh_api_url)
    except ToolError as exc:
        encrypted_path = args.supabase.with_name(f"{args.supabase.name}.enc")
        if encrypted_path.exists() and not args.supabase.exists():
            raise ToolError(
                _encrypted_session_unavailable_message(args.supabase)
            ) from exc
        if not args.supabase.exists():
            raise ToolError(
                f"Granola authentication is not configured; {_mcp_login_setup_step()}."
            ) from exc
        raise
    return GranolaSession(
        mode="desktop_session",
        token=token,
        documents_url=args.api_url,
        transcript_url=args.transcript_api_url,
    )


def _fetch_session_document(
    session: GranolaSession,
    document_id: str,
) -> dict[str, Any]:
    if session.mode == "mcp_oauth":
        return fetch_mcp_document(
            session.documents_url,
            session.token,
            document_id,
        )
    documents = fetch_documents(session.documents_url, session.token, limit=None)
    return select_document(documents, document_id)


def _hydrate_session_documents(
    session: GranolaSession,
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if session.mode != "mcp_oauth":
        return documents
    document_ids = [
        str(document.get("id") or "").strip()
        for document in documents
        if str(document.get("id") or "").strip()
    ]
    detailed_by_id: dict[str, dict[str, Any]] = {}
    for index in range(0, len(document_ids), 10):
        for document in fetch_mcp_meetings(
            session.documents_url,
            session.token,
            document_ids[index : index + 10],
        ):
            document_id = str(document.get("id") or "").strip()
            if document_id:
                detailed_by_id[document_id] = document
    return [
        detailed_by_id[document_id]
        for document_id in document_ids
        if document_id in detailed_by_id
    ]


def _fetch_session_documents(
    session: GranolaSession,
    *,
    limit: int | None,
    window: WindowSpec | None = None,
    details: bool = False,
) -> list[dict[str, Any]]:
    if session.mode == "desktop_session":
        return fetch_documents(session.documents_url, session.token, limit=limit)
    created_after = (
        window.start.isoformat().replace("+00:00", "Z")
        if window and window.start
        else ""
    )
    created_before = (
        window.end.isoformat().replace("+00:00", "Z") if window and window.end else ""
    )
    documents = fetch_mcp_documents(
        session.documents_url,
        session.token,
        limit=limit,
        created_after=created_after,
        created_before=created_before,
    )
    if not details:
        return documents
    return _hydrate_session_documents(session, documents)


def _fetch_session_transcript(
    session: GranolaSession, document_id: str
) -> list[dict[str, Any]]:
    if session.mode == "desktop_session":
        return fetch_transcript(session.transcript_url, session.token, document_id)
    return fetch_mcp_transcript(
        session.transcript_url,
        session.token,
        document_id,
    )


def _load_selected_document(
    args: argparse.Namespace,
) -> tuple[GranolaSession, dict[str, Any]]:
    session = _load_session(args)
    selector = args.selector.strip()
    direct_id = session.mode == "mcp_oauth" and LEGACY_DOCUMENT_ID_RE.fullmatch(
        selector
    )
    if direct_id:
        return session, _fetch_session_document(session, selector)
    documents = _fetch_session_documents(session, limit=None)
    selected = select_document(documents, selector)
    if session.mode == "mcp_oauth":
        selected = _fetch_session_document(
            session,
            str(selected.get("id") or ""),
        )
    return session, selected


def _load_selected_document_transcript(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    session = _load_session(args)
    selector = args.selector.strip()
    if session.mode == "mcp_oauth":
        if LEGACY_DOCUMENT_ID_RE.fullmatch(selector):
            document_id = selector
        else:
            summaries = _fetch_session_documents(session, limit=None)
            document_id = str(select_document(summaries, selector).get("id") or "")
        document = _fetch_session_document(session, document_id)
        return document, _fetch_session_transcript(session, document_id)
    documents = _fetch_session_documents(session, limit=None)
    document = select_document(documents, selector)
    return document, _fetch_session_transcript(session, str(document.get("id") or ""))


def granola_status_payload(
    supabase_path: Path,
    api_url: str,
    *,
    mcp_url: str = DEFAULT_MCP_URL,
) -> dict[str, Any]:
    oauth_state: dict[str, Any] | None = None
    oauth_error = ""
    try:
        oauth_state = load_mcp_oauth_state()
    except ToolError as exc:
        oauth_error = str(exc)
    mcp_access_token_present = bool(
        oauth_state and str(oauth_state.get("access_token") or "").strip()
    )
    auth_mode = (
        "mcp_oauth"
        if mcp_access_token_present or oauth_error
        else "desktop_session"
        if supabase_path.exists()
        else "none"
    )
    oauth_expires_at = oauth_state.get("expires_at") if oauth_state else None
    oauth_expires_at_text = ""
    if isinstance(oauth_expires_at, int | float):
        oauth_expires_at_text = (
            dt.datetime.fromtimestamp(float(oauth_expires_at), dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    payload: dict[str, Any] = {
        "surface": "granola",
        "supabaseFile": str(supabase_path),
        "apiUrl": mcp_url if mcp_access_token_present else api_url,
        "authMode": auth_mode,
        "mcpUrl": mcp_url,
        "mcpOAuthFile": str(MCP_OAUTH_FILE),
        "mcpOAuthFilePresent": MCP_OAUTH_FILE.exists(),
        "mcpClientRegistered": bool(
            oauth_state and str(oauth_state.get("client_id") or "").strip()
        ),
        "mcpOAuthConfigured": mcp_access_token_present,
        "mcpRefreshTokenPresent": bool(
            oauth_state and str(oauth_state.get("refresh_token") or "").strip()
        ),
        "mcpTokenExpiresAt": oauth_expires_at_text,
        "mcpTokenExpired": bool(
            oauth_state
            and mcp_access_token_present
            and mcp_oauth_token_is_expired(oauth_state, skew_seconds=0)
        ),
        "localSessionPresent": supabase_path.exists(),
        "sessionStatus": "missing",
        "documentAccess": False,
        "nextStep": _mcp_login_setup_step(),
    }
    payload.update(encrypted_session_metadata(supabase_path))
    if oauth_error:
        payload["sessionStatus"] = "invalid"
        payload["error"] = oauth_error
        payload["nextStep"] = (
            f"remove or repair {display_path(MCP_OAUTH_FILE)}, then "
            f"{_mcp_login_setup_step()}"
        )
        return payload
    if mcp_access_token_present:
        try:
            token = ensure_mcp_access_token(mcp_url=mcp_url)
            documents = fetch_mcp_documents(mcp_url, token, limit=1)
        except ToolError as exc:
            payload["sessionStatus"] = "invalid"
            payload["error"] = str(exc)
            payload["nextStep"] = _mcp_login_setup_step()
            return payload
        refreshed_state = load_mcp_oauth_state() or {}
        refreshed_expires_at = refreshed_state.get("expires_at")
        if isinstance(refreshed_expires_at, int | float):
            payload["mcpTokenExpiresAt"] = (
                dt.datetime.fromtimestamp(float(refreshed_expires_at), dt.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        payload["mcpTokenExpired"] = mcp_oauth_token_is_expired(
            refreshed_state,
            skew_seconds=0,
        )
        payload["sessionStatus"] = "ready"
        payload["documentAccess"] = True
        payload["sampleDocumentCount"] = len(documents)
        payload["nextStep"] = "ready"
        return payload
    if not supabase_path.exists():
        if payload.get("encryptedSessionPresent"):
            payload["nextStep"] = _encrypted_session_unavailable_message(supabase_path)
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
        payload["refreshTokenPresent"] = bool(load_refresh_token(supabase_path))
    except ToolError:
        payload["refreshTokenPresent"] = False
    payload.update(access_token_metadata(supabase_path))
    try:
        documents = fetch_documents(api_url, token, limit=1)
    except ToolError as exc:
        payload["sessionStatus"] = "invalid"
        payload["error"] = str(exc)
        if payload.get("accessTokenExpired") and payload.get("refreshTokenPresent"):
            payload["nextStep"] = (
                "run `gotta granola auth refresh`, then rerun `gotta granola status`"
            )
        elif payload.get("accessTokenExpired") and payload.get("encryptedSessionNewer"):
            payload["nextStep"] = (
                "Granola has newer encrypted session state, but gotta currently reads "
                "the stale plaintext supabase.json token; run "
                "`gotta granola auth refresh` or expose a fresh plaintext session "
                "before retrying"
            )
        elif payload.get("accessTokenExpired"):
            payload["nextStep"] = (
                "open Granola or sign in again to refresh the expired local session, "
                "then rerun `gotta granola status`"
            )
        else:
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
    args: argparse.Namespace,
    *,
    limit: int | None,
    window: WindowSpec | None = None,
    details: bool = False,
) -> list[dict[str, Any]]:
    session = _load_session(args)
    return _fetch_session_documents(
        session,
        limit=limit,
        window=window,
        details=details,
    )


def cmd_status(args: argparse.Namespace) -> int:
    payload = granola_status_payload(
        args.supabase,
        args.api_url,
        mcp_url=args.mcp_url,
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    lines = [
        "surface\tgranola",
        f"supabase_file\t{payload['supabaseFile']}",
        f"auth_mode\t{payload.get('authMode') or ''}",
        f"mcp_oauth_configured\t{str(bool(payload.get('mcpOAuthConfigured'))).lower()}",
        f"mcp_refresh_token_present\t{str(bool(payload.get('mcpRefreshTokenPresent'))).lower()}",
        f"session_status\t{payload['sessionStatus']}",
        f"local_session_present\t{str(bool(payload.get('localSessionPresent'))).lower()}",
        f"encrypted_session_present\t{str(bool(payload.get('encryptedSessionPresent'))).lower()}",
        f"encrypted_session_newer\t{str(bool(payload.get('encryptedSessionNewer'))).lower()}",
        f"document_access\t{str(bool(payload.get('documentAccess'))).lower()}",
        f"next_step\t{payload.get('nextStep') or ''}",
    ]
    if payload.get("accessTokenExpiresAt"):
        lines.append(f"access_token_expires_at\t{payload['accessTokenExpiresAt']}")
    if "accessTokenExpired" in payload:
        lines.append(
            f"access_token_expired\t{str(bool(payload.get('accessTokenExpired'))).lower()}"
        )
    if payload.get("mcpTokenExpiresAt"):
        lines.append(f"mcp_token_expires_at\t{payload['mcpTokenExpiresAt']}")
    if payload.get("mcpOAuthConfigured"):
        lines.append(
            f"mcp_token_expired\t{str(bool(payload.get('mcpTokenExpired'))).lower()}"
        )
    if payload.get("error"):
        lines.append(f"error\t{payload['error']}")
    print("\n".join(lines))
    return 0


def _print_auth_result(payload: dict[str, Any]) -> int:
    if payload.get("output") == "json":
        output = {key: value for key, value in payload.items() if key != "output"}
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    lines = [
        "surface\tgranola",
        f"command\t{payload.get('command') or ''}",
        f"auth_mode\t{payload.get('authMode') or ''}",
        f"status\t{payload.get('status') or 'unknown'}",
    ]
    for key, label in (
        ("authorized", "authorized"),
        ("loggedOut", "logged_out"),
        ("refreshed", "refreshed"),
        ("forced", "forced"),
        ("accessTokenExpired", "access_token_expired"),
        ("refreshTokenPresent", "refresh_token_present"),
    ):
        if key in payload:
            lines.append(f"{label}\t{str(bool(payload.get(key))).lower()}")
    if payload.get("accessTokenExpiresAt"):
        lines.append(f"access_token_expires_at\t{payload['accessTokenExpiresAt']}")
    if payload.get("reason"):
        lines.append(f"reason\t{payload['reason']}")
    lines.append(f"next_step\t{payload.get('nextStep') or ''}")
    print("\n".join(lines))
    return 0


def cmd_auth_login(args: argparse.Namespace) -> int:
    state = run_mcp_oauth_login(
        mcp_url=args.mcp_url,
        issuer=args.oauth_issuer,
        open_browser=not args.no_browser,
    )
    access_token = str(state.get("access_token") or "").strip()
    documents = fetch_mcp_documents(args.mcp_url, access_token, limit=1)
    expires_at = state.get("expires_at")
    expires_at_text = ""
    if isinstance(expires_at, int | float):
        expires_at_text = (
            dt.datetime.fromtimestamp(float(expires_at), dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return _print_auth_result(
        {
            "surface": "granola",
            "command": "auth login",
            "authMode": "mcp_oauth",
            "status": "authorized",
            "authorized": True,
            "refreshTokenPresent": bool(str(state.get("refresh_token") or "").strip()),
            "accessTokenExpiresAt": expires_at_text,
            "sampleDocumentCount": len(documents),
            "nextStep": "ready",
            "output": args.output,
        }
    )


def cmd_auth_logout(args: argparse.Namespace) -> int:
    removed = clear_mcp_oauth_tokens()
    return _print_auth_result(
        {
            "surface": "granola",
            "command": "auth logout",
            "authMode": "mcp_oauth",
            "status": "logged_out" if removed else "not_configured",
            "loggedOut": removed,
            "nextStep": _mcp_login_setup_step(),
            "output": args.output,
        }
    )


def cmd_auth_refresh(args: argparse.Namespace) -> int:
    oauth_state = load_mcp_oauth_state()
    if oauth_state and str(oauth_state.get("access_token") or "").strip():
        expired = mcp_oauth_token_is_expired(oauth_state, skew_seconds=0)
        if not args.force and not expired:
            payload = {
                "surface": "granola",
                "command": "auth refresh",
                "authMode": "mcp_oauth",
                "status": "skipped",
                "refreshed": False,
                "forced": False,
                "accessTokenExpired": False,
                "reason": "browser authorization already has a non-expired access token",
                "nextStep": "ready",
            }
        else:
            refreshed = refresh_mcp_oauth_state(
                oauth_state,
                mcp_url=args.mcp_url,
            )
            expires_at = refreshed.get("expires_at")
            expires_at_text = ""
            if isinstance(expires_at, int | float):
                expires_at_text = (
                    dt.datetime.fromtimestamp(float(expires_at), dt.timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            payload = {
                "surface": "granola",
                "command": "auth refresh",
                "authMode": "mcp_oauth",
                "status": "refreshed",
                "refreshed": True,
                "forced": args.force,
                "accessTokenExpired": False,
                "accessTokenExpiresAt": expires_at_text,
                "nextStep": "ready",
            }
    else:
        encrypted_path = args.supabase.with_name(f"{args.supabase.name}.enc")
        if encrypted_path.exists() and not args.supabase.exists():
            raise ToolError(_encrypted_session_unavailable_message(args.supabase))
        payload = refresh_session_payload(
            supabase_path=args.supabase,
            refresh_api_url=args.refresh_api_url,
            force=args.force,
        )
        payload["authMode"] = "desktop_session"
    payload["output"] = args.output
    return _print_auth_result(payload)


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
        filter_documents_by_window(
            _load_recent_documents(args, limit=None, window=window), window
        ),
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
    window = _resolve_note_window(args)
    session = _load_session(args)
    documents = _fetch_session_documents(
        session,
        limit=None,
        window=window,
        details=session.mode == "mcp_oauth" and args.mode != "title",
    )
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
    _, document = _load_selected_document(args)
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
        _, document = _load_selected_document(args)
        selector = args.selector.strip()
        base = (
            selector
            if _is_document_id(selector)
            else _slug(selector, fallback="granola")
        )
        return Capture(
            data=json_bytes(document),
            preferred_name=f"{base}.json",
            content_type="application/json",
            metadata=_capture_meta(document),
        )
    if args.command == "transcript":
        document, segments = _load_selected_document_transcript(args)
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
            preferred_name=f"{base}.json",
            content_type="application/json",
            metadata=_capture_meta(document),
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
            preferred_name=preferred_name(argv, object()),
            content_type="application/json",
            metadata={
                "projector": "granola",
                "granola_kind": args.command,
            },
        )
    raise NotImplementedError("granola capture does not support this command")


def project(argv: list[str], capture: Capture) -> Projection:
    kind = str(capture.metadata.get("granola_kind") or "").strip()
    if kind in {"search", "list", "search-transcript"}:
        payload = json.loads(capture.data.decode("utf-8"))
        if not argv:
            if kind == "list":
                return projection_bytes(
                    render_list_markdown(payload).encode("utf-8"),
                    content_type="text/markdown",
                )
            if kind == "search-transcript":
                return projection_bytes(
                    render_transcript_search_markdown(payload).encode("utf-8"),
                    content_type="text/markdown",
                )
            return projection_bytes(
                render_search_markdown(payload).encode("utf-8"),
                content_type="text/markdown",
            )
        args = _parse_cli(argv)
        if args.command != kind:
            return projection_bytes(capture.data, content_type=capture.content_type)
        if args.output == "json":
            return projection_bytes(
                pretty_json(capture.data),
                content_type="application/json",
            )
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
                return projection_bytes(
                    ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"),
                    content_type="text/plain",
                )
            return projection_bytes(
                render_list_markdown(payload).encode("utf-8"),
                content_type="text/markdown",
            )
        if kind == "search-transcript":
            return projection_bytes(
                render_transcript_search_markdown(payload).encode("utf-8"),
                content_type="text/markdown",
            )
        return projection_bytes(
            render_search_markdown(payload).encode("utf-8"),
            content_type="text/markdown",
        )
    payload = json.loads(capture.data.decode("utf-8"))
    if not argv:
        if "segmentCount" in payload:
            document = payload.get("document")
            segments = payload.get("segments")
            if isinstance(document, dict) and isinstance(segments, list):
                return projection_bytes(
                    format_transcript_markdown(document, segments).encode("utf-8"),
                    content_type="text/markdown",
                )
        if isinstance(payload, dict):
            note = best_note_body(payload)
            return projection_bytes(
                format_markdown_document(payload, note).encode("utf-8"),
                content_type="text/markdown",
            )
        return projection_bytes(capture.data, content_type=capture.content_type)
    args = _parse_cli(argv)
    if args.command == "get":
        if args.output == "json":
            return projection_bytes(
                pretty_json(capture.data),
                content_type="application/json",
            )
        if args.output == "meta":
            if isinstance(payload, dict):
                return projection_bytes(
                    json_bytes(document_meta_payload(payload)),
                    content_type="application/json",
                )
            return projection_bytes(capture.data, content_type=capture.content_type)
        if isinstance(payload, dict):
            return projection_bytes(
                format_markdown_document(payload, best_note_body(payload)).encode(
                    "utf-8"
                ),
                content_type="text/markdown",
            )
        return projection_bytes(capture.data, content_type=capture.content_type)
    if args.command == "transcript":
        if args.output == "json":
            return projection_bytes(
                pretty_json(capture.data),
                content_type="application/json",
            )
        if args.output == "summary":
            document = payload.get("document") if isinstance(payload, dict) else {}
            if not isinstance(document, dict):
                document = {}
            line = (
                f"{document.get('id') or ''}\t{payload.get('segmentCount') or 0}\t"
                f"{document.get('title') or 'Untitled'}\n"
            )
            return projection_bytes(line.encode("utf-8"), content_type="text/plain")
        document = payload.get("document") if isinstance(payload, dict) else {}
        segments = payload.get("segments") if isinstance(payload, dict) else []
        if isinstance(document, dict) and isinstance(segments, list):
            return projection_bytes(
                format_transcript_markdown(document, segments).encode("utf-8"),
                content_type="text/markdown",
            )
    return projection_bytes(capture.data, content_type=capture.content_type)


def cmd_transcript(args: argparse.Namespace) -> int:
    document, segments = _load_selected_document_transcript(args)
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
    window = _resolve_transcript_search_window(args)
    session = _load_session(args)
    documents = _fetch_session_documents(session, limit=None, window=window)
    payload = search_transcripts(
        session=session,
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
    session = _load_session(args)
    documents = sort_documents(
        filter_documents_by_window(
            _fetch_session_documents(
                session,
                limit=None,
                window=window,
            ),
            window,
        ),
        sort_by=args.sort,
        order=args.order,
    )[args.offset : args.offset + args.limit]
    if session.mode == "mcp_oauth":
        documents = _hydrate_session_documents(session, documents)
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
            "Read-only Granola note retrieval through browser-authorized MCP, "
            "with legacy local-session compatibility."
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
    parser.add_argument(
        "--refresh-api-url",
        default=DEFAULT_REFRESH_API_URL,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--mcp-url",
        default=DEFAULT_MCP_URL,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--oauth-issuer",
        default=DEFAULT_MCP_OAUTH_ISSUER,
        help=argparse.SUPPRESS,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="inspect Granola authentication readiness")
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_status)

    auth = sub.add_parser("auth", help="manage Granola authentication")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    p = auth_sub.add_parser(
        "login",
        help="authorize Granola in a browser without an API key",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="print the verification URL without opening it automatically",
    )
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_auth_login)

    p = auth_sub.add_parser(
        "logout",
        help="remove cached Granola browser tokens",
    )
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_auth_logout)

    p = auth_sub.add_parser(
        "refresh",
        help="refresh cached browser or legacy Granola access",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="refresh even when the current plaintext access token is not expired",
    )
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_auth_refresh)

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
    except KeyboardInterrupt:
        return die("Granola command canceled.", code=130)
    except ToolError as exc:
        return die(str(exc), code=1)


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main(sys.argv[1:]))
