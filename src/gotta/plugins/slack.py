#!/usr/bin/env python3
"""Read-only Slack CLI wrapper around slackdump."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import signal
import shlex
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any

from gotta.capture import Capture, capture_json_command, json_bytes
from gotta.dispatch.stream import capture_stdout
from gotta.helptext import is_long_help_request, print_long_help
from gotta.project import html_markdown, html_text, pretty_json
from gotta.resolve.route import query_route, strip_http_url_fragment
from gotta.source import (
    derive_source_metadata_from_payload,
    render_source_metadata_lines,
    render_visibility_metadata_lines,
    with_visibility_metadata,
)
from gotta.providers.slack import (
    default_workspace,
    ensure_live_search_auth,
    ensure_slackdump,
    ensure_workspace_auth,
    export_slack_auth_from_slackdump,
    known_workspaces,
    missing_workspace_message,
    persist_slack_auth_state,
    resolve_workspace,
    SlackError,
    slack_api_post,
    slack_auth_test,
    slack_status_payload,
    slack_web_get,
)

DEFAULT_RECENT_LOOKBACK = "3w"
MAX_SYNC_LOOKBACK = "6w"
DEFAULT_LIVE_SEARCH_TIMEOUT_SECONDS = 10
DEFAULT_OPPORTUNISTIC_TIMEOUT_SECONDS = 8
DEFAULT_THREAD_HYDRATION_TIMEOUT_SECONDS = 90
RETRIEVAL_HEARTBEAT_SECONDS = 15
DEFAULT_CHANNEL_DIRECTORY_REFRESH_TIMEOUT_SECONDS = 120
DEFAULT_USER_DIRECTORY_REFRESH_TIMEOUT_SECONDS = 120
DEFAULT_COVERAGE_FRESHNESS_SLOP_SECONDS = 3600
CANONICAL_ARCHIVE_DIRNAME = "archive"
MAX_SYNC_WINDOW = dt.timedelta(weeks=6)
THREAD_HYDRATION_HALF_WINDOW = dt.timedelta(weeks=3)
PERMALINK_RE = re.compile(
    r"https://(?P<workspace>[^/.]+)(?:\.enterprise)?\.slack\.com/archives/"
    r"(?P<channel>[A-Z0-9]+)(?:/p(?P<pnum>[0-9]{16}))?"
)
DOC_URL_RE = re.compile(
    r"https://(?P<workspace>[^/.]+)(?:\.enterprise)?\.slack\.com/docs/"
    r"(?P<team>[A-Z0-9]+)/(?P<doc>[A-Z0-9]+)"
)
ARCHIVE_PATH_RE = re.compile(
    r"/archives/(?P<channel>[A-Z0-9]+)(?:/p(?P<pnum>[0-9]{16}))?"
)
DOC_PATH_RE = re.compile(r"/docs/(?P<team>[A-Z0-9]+)/(?P<doc>[A-Z0-9]+)")
CHANNEL_ID_RE = re.compile(r"^[CDG][A-Z0-9]{8,}$")
THREAD_COLON_RE = re.compile(r"^(?P<channel>[A-Z0-9]+):(?P<ts>[0-9]{10}\.[0-9]{6})$")
THREAD_TS_RE = re.compile(r"^[0-9]{10}\.[0-9]{6}$")
LOOKBACK_RE = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>[smhdwy])$", re.IGNORECASE)
WORKSPACE_LIST_RE = re.compile(
    r"(?m)^\s*(?:=>\s+)?(?P<workspace>[A-Za-z0-9._-]+)\s+\(file:"
)
SEARCH_MODIFIER_RE = re.compile(
    r"^(?P<neg>-?)(?P<name>before|after|on|during|from|in|has|is|with|creator):(?P<value>\S+)$",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"^\d{4}$")
YEAR_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


ToolError = SlackError


@dataclass
class SlackRef:
    raw: str
    workspace: str
    kind: str
    channel_id: str
    thread_ts: str | None = None
    team_id: str | None = None
    doc_id: str | None = None
    url: str | None = None


@dataclass
class ArchiveResult:
    root_dir: Path
    db_path: Path
    log_path: Path


@dataclass
class ChannelWindow:
    since: dt.datetime | None
    until: dt.datetime | None
    lookback: str | None
    strict: bool

    @property
    def enabled(self) -> bool:
        return self.since is not None or self.until is not None


@dataclass
class SearchSpec:
    raw_query: str
    terms: list[str]
    match_mode: str
    modifiers: list[str]

    @property
    def is_multi_term(self) -> bool:
        return len(self.terms) > 1

    @property
    def has_terms(self) -> bool:
        return bool(self.terms)


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


DEFAULT_DIRECTORY_LIST_LIMIT = 100


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


def _paginate_directory_results(
    items: list[Any],
    *,
    offset: int,
    limit: int,
    include_all: bool,
) -> tuple[list[Any], dict[str, Any]]:
    total_count = len(items)
    if include_all:
        paged = items[offset:]
        applied_limit: int | None = None
    else:
        paged = items[offset : offset + limit]
        applied_limit = limit
    shown_count = len(paged)
    next_offset = offset + shown_count
    truncated = next_offset < total_count
    return paged, {
        "offset": offset,
        "limit": applied_limit,
        "totalCount": total_count,
        "shownCount": shown_count,
        "nextOffset": next_offset if truncated else None,
        "truncated": truncated,
    }


def _emit_directory_paging_notice(
    *,
    entity: str,
    total_count: int,
    shown_count: int,
    offset: int,
    next_offset: int | None,
) -> None:
    if next_offset is None:
        return
    print(
        (
            f"note: showing {shown_count} of {total_count} {entity} starting at offset {offset}; "
            f"pass --offset {next_offset} for the next page or --all for everything"
        ),
        file=sys.stderr,
    )


def emit_progress(message: str) -> None:
    print(message, file=sys.stderr)


def _with_progress_heartbeat(
    label: str,
    action,
    *,
    budget_seconds: int | None = None,
):
    stop_event = threading.Event()

    def beat() -> None:
        elapsed = RETRIEVAL_HEARTBEAT_SECONDS
        while not stop_event.wait(RETRIEVAL_HEARTBEAT_SECONDS):
            if budget_seconds is None:
                emit_progress(f"{label}; still hydrating after ~{elapsed}s")
            else:
                remaining = max(budget_seconds - elapsed, 0)
                emit_progress(
                    f"{label}; still hydrating after ~{elapsed}s "
                    f"({remaining}s left in this attempt)"
                )
            elapsed += RETRIEVAL_HEARTBEAT_SECONDS

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        return action()
    finally:
        stop_event.set()
        thread.join(timeout=1)


def _slug(value: str, *, fallback: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    return value.strip("-") or fallback


def _output_extension(output: str) -> str:
    return {
        "json": "json",
        "meta": "json",
        "messages": "json",
        "markdown": "md",
        "summary": "summary",
        "text": "txt",
        "titles": "txt",
        "links": "txt",
    }.get(output, "md")


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def canonical_locator(argv: list[str]) -> str:
    args = _parse_cli(argv)
    if args.command == "status":
        workspace = str(args.workspace or "").strip()
        return f"slack:workspace:{workspace}" if workspace else "slack:status"
    if args.command == "get":
        ref = resolve_slack_ref(args.ref, workspace=args.workspace)
        if ref.kind == "doc" and ref.team_id and ref.doc_id:
            return f"slack:doc:{ref.team_id}:{ref.doc_id}"
        if ref.kind == "thread" and ref.thread_ts:
            return f"slack:thread:{ref.channel_id}:{ref.thread_ts.replace('.', '')}"
        return f"slack:channel:{ref.channel_id}"
    return f"slack:{shlex.join(argv)}"


def preferred_name(argv: list[str], options: object) -> str:
    if getattr(options, "save_as", ""):
        return str(getattr(options, "save_as"))
    args = _parse_cli(argv)
    if args.command == "status":
        workspace = _slug(str(args.workspace or ""), fallback="slack")
        return f"slack-workspace-{workspace}.{_output_extension(args.output)}"
    if args.command == "get":
        ref = resolve_slack_ref(args.ref, workspace=args.workspace)
        if ref.kind == "doc" and ref.doc_id:
            return f"{ref.doc_id}.html"
        if ref.kind == "thread" and ref.thread_ts:
            return f"p{ref.thread_ts.replace('.', '')}.json"
        return f"{ref.channel_id}.json"
    if args.command == "search":
        prefix = "slack-search"
        workspace = str(args.workspace or "").strip()
        if workspace:
            prefix = f"{prefix}-{_slug(workspace, fallback='slack')}"
        return f"{prefix}-{_slug(args.query, fallback='slack')}.json"
    return f"slack.{_output_extension(getattr(args, 'output', 'text'))}"


def route_target(target: str) -> list[str] | None:
    if target.startswith("https://") and (
        ".slack.com/docs/" in target or ".enterprise.slack.com/docs/" in target
    ):
        if any(char.isspace() for char in target):
            return None
        return ["get", strip_http_url_fragment(target)]
    if (target.startswith("https://") and ".slack.com/archives/" in target) or (
        "enterprise.slack.com" in target and "archives" in target
    ):
        if any(char.isspace() for char in target):
            return None
        return ["get", strip_http_url_fragment(target)]
    if target.startswith("slack:thread:"):
        _, _, channel_id, thread_ref = target.split(":", 3)
        if thread_ref.isdigit() and len(thread_ref) == 16:
            thread_ref = f"{thread_ref[:10]}.{thread_ref[10:]}"
        return ["get", f"{channel_id}:{thread_ref}"]
    if target.startswith("slack:channel:"):
        _, _, channel_id = target.split(":", 2)
        return ["get", channel_id]
    if target.startswith("slack:doc:"):
        return ["get", target]
    if target.startswith("slack:search "):
        return query_route(
            "search",
            target.removeprefix("slack:search "),
            valued_flags=(
                "--channel",
                "--workspace",
                "--limit",
                "--output",
                "--source",
                "--match",
                "--live-timeout",
                "--pull-recent",
            ),
            boolean_flags=("--refresh",),
        )
    if target.startswith("slack:workspace:"):
        workspace = target.removeprefix("slack:workspace:").strip()
        if workspace:
            return ["status", "--workspace", workspace, "--output", "summary"]
    if target.startswith("slack:"):
        workspace = target.removeprefix("slack:").strip()
        if re.fullmatch(r"[A-Za-z0-9._-]+", workspace):
            return ["status", "--workspace", workspace, "--output", "summary"]
    return None


def _thread_archive_detail_indicates_inaccessible_channel(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in (
            "not accessible via slackdump archive",
            "likely in a private channel",
            "channel_not_found",
            "not_in_channel",
            "private channel",
        )
    )


def _thread_permalink_failure_message(
    ref: "SlackRef",
    *,
    permalink: str,
    since: dt.datetime,
    until: dt.datetime,
    detail: str,
    explicit_refresh: bool,
) -> str:
    if _thread_archive_detail_indicates_inaccessible_channel(detail):
        if explicit_refresh:
            attempt_text = (
                "The native read path attempted the centered six-week archive hydration "
                "and an explicit refresh, but the underlying channel is not readable "
                "through the archive path."
            )
        else:
            attempt_text = (
                "The native read path recognized the permalink, attempted the centered "
                "six-week hydration window, then retried with an explicit bounded refresh "
                "automatically, but the underlying channel is still not readable through "
                "the archive path."
            )
        return (
            "thread permalink retrieval could not read the underlying channel archive around "
            f"{ref.channel_id}:{ref.thread_ts}. {attempt_text} "
            "This points to a source-access limitation rather than a simple bounded-archive "
            f"coverage gap. permalink: {shlex.quote(permalink)}. "
            f"window: since={format_utc_iso(since)} until={format_utc_iso(until)}. "
            f"detail: {detail}"
        )
    if explicit_refresh:
        return (
            "thread permalink retrieval has a bounded-archive coverage gap around "
            f"{ref.channel_id}:{ref.thread_ts}. The native read path attempted the "
            "centered six-week archive hydration and an explicit refresh, but the "
            "required window is still not readable. This usually means archive coverage "
            "is still missing, workspace access is insufficient, or the underlying "
            f"hydrator failed. permalink: {shlex.quote(permalink)}. "
            f"window: since={format_utc_iso(since)} until={format_utc_iso(until)}. "
            f"detail: {detail}"
        )
    return (
        "thread permalink retrieval has a bounded-archive coverage gap around "
        f"{ref.channel_id}:{ref.thread_ts}. The native read path recognized the permalink, "
        "attempted the centered six-week hydration window, then retried with an explicit "
        "bounded refresh automatically, but the required archive window is still not "
        "readable. This usually means archive coverage is still missing, workspace access "
        "is insufficient, or the underlying hydrator failed. "
        f"permalink: {shlex.quote(permalink)}. "
        f"window: since={format_utc_iso(since)} until={format_utc_iso(until)}. "
        f"detail: {detail}"
    )


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def cache_root() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "slack-archive"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "slack-archive"
    return Path.home() / ".cache" / "slack-archive"


def workspace_cache_dir(workspace: str) -> Path:
    return cache_root() / workspace


def directory_db_path(workspace: str) -> Path:
    return workspace_cache_dir(workspace) / "_directory.sqlite"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def parse_slackdump_timestamp(value: str) -> dt.datetime | None:
    cleaned = value.strip()
    if cleaned.endswith(" UTC"):
        cleaned = cleaned[:-4]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return dt.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def open_directory_db(workspace: str) -> sqlite3.Connection:
    path = directory_db_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS directory_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channel_directory (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            lookup_name TEXT NOT NULL,
            type TEXT NOT NULL,
            is_private INTEGER NOT NULL DEFAULT 0,
            is_archived INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS channel_directory_lookup_name_idx
            ON channel_directory(lookup_name);

        CREATE TABLE IF NOT EXISTS user_directory (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            lookup_name TEXT NOT NULL,
            real_name TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0,
            is_bot INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS user_directory_lookup_name_idx
            ON user_directory(lookup_name);
        """
    )
    return conn


def directory_state_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM directory_state WHERE key = ?",
        (key,),
    ).fetchone()
    return None if row is None else str(row["value"])


def set_directory_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO directory_state(key, value)
        VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def directory_is_initialized(conn: sqlite3.Connection, key: str) -> bool:
    return bool(directory_state_value(conn, key))


def normalize_lookup_name(value: str) -> str:
    return value.strip().lower().lstrip("#")


def normalize_user_record(
    user_id: str, username: str, data: dict[str, Any]
) -> dict[str, Any]:
    profile = data.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    display_name = ""
    for candidate in (
        profile.get("display_name"),
        profile.get("real_name"),
        data.get("real_name"),
        data.get("name"),
        username,
        user_id,
    ):
        value = str(candidate or "").strip()
        if value:
            display_name = value
            break
    return {
        "id": user_id,
        "username": username,
        "displayName": display_name or user_id,
        "realName": str(
            profile.get("real_name")
            or data.get("real_name")
            or profile.get("display_name")
            or ""
        ),
        "profile": profile,
        "deleted": bool(data.get("deleted", False)),
        "isBot": bool(data.get("is_bot", False)),
        "raw": data,
    }


def refresh_directory(
    *,
    workspace: str,
    entity: str,
    force: bool = False,
    timeout_seconds: int | None = None,
    announce: bool = False,
) -> None:
    state_key = f"{entity}.updated_at"
    conn = open_directory_db(workspace)
    try:
        if not force and directory_is_initialized(conn, state_key):
            return
    finally:
        conn.close()

    if announce:
        print(
            f"note: refreshing Slack {entity} directory for {workspace}",
            file=sys.stderr,
        )

    if timeout_seconds is None:
        timeout_seconds = (
            DEFAULT_CHANNEL_DIRECTORY_REFRESH_TIMEOUT_SECONDS
            if entity == "channels"
            else DEFAULT_USER_DIRECTORY_REFRESH_TIMEOUT_SECONDS
        )
    proc = explicit_provider_list(
        workspace=workspace,
        entity=entity,
        fmt="json",
        timeout_seconds=timeout_seconds,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"slackdump list {entity} did not return valid JSON: {exc}. "
            f"retry 'gotta slack list-{entity} --refresh' and, if it still fails, run "
            f"'gotta slack auth --workspace {workspace}'."
        ) from exc
    if not isinstance(payload, list):
        raise ToolError(
            f"slackdump list {entity} returned unexpected payload. retry "
            f"'gotta slack list-{entity} --refresh' and verify workspace auth with "
            f"'gotta slack auth --workspace {workspace}'."
        )

    updated_at = iso_now()
    conn = open_directory_db(workspace)
    try:
        with conn:
            if entity == "channels":
                conn.execute("DELETE FROM channel_directory")
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    channel_id = str(item.get("id") or "").strip()
                    if not channel_id:
                        continue
                    name = str(item.get("name") or channel_id).strip() or channel_id
                    conn.execute(
                        """
                        INSERT INTO channel_directory(
                            id, name, lookup_name, type, is_private, is_archived, raw_json, updated_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            name = excluded.name,
                            lookup_name = excluded.lookup_name,
                            type = excluded.type,
                            is_private = excluded.is_private,
                            is_archived = excluded.is_archived,
                            raw_json = excluded.raw_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            channel_id,
                            name,
                            normalize_lookup_name(name),
                            channel_type(item, channel_id),
                            int(bool(item.get("is_private", False))),
                            int(bool(item.get("is_archived", False))),
                            json.dumps(item, sort_keys=True),
                            updated_at,
                        ),
                    )
            else:
                conn.execute("DELETE FROM user_directory")
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    user_id = str(item.get("id") or "").strip()
                    if not user_id:
                        continue
                    username = str(item.get("name") or "").strip()
                    normalized = normalize_user_record(user_id, username, item)
                    conn.execute(
                        """
                        INSERT INTO user_directory(
                            id, username, display_name, lookup_name, real_name, deleted, is_bot, raw_json, updated_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            username = excluded.username,
                            display_name = excluded.display_name,
                            lookup_name = excluded.lookup_name,
                            real_name = excluded.real_name,
                            deleted = excluded.deleted,
                            is_bot = excluded.is_bot,
                            raw_json = excluded.raw_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            user_id,
                            normalized["username"],
                            normalized["displayName"],
                            normalize_lookup_name(
                                normalized["displayName"]
                                or normalized["username"]
                                or user_id
                            ),
                            normalized["realName"],
                            int(normalized["deleted"]),
                            int(normalized["isBot"]),
                            json.dumps(item, sort_keys=True),
                            updated_at,
                        ),
                    )
            set_directory_state(conn, state_key, updated_at)
    finally:
        conn.close()


def seed_channel_directory_from_archive(workspace: str) -> None:
    result = workspace_archive_result(workspace)
    if not result.db_path.exists():
        return
    archive_conn = open_db(result.db_path)
    try:
        rows = archive_conn.execute(
            """
            SELECT id, name, data
            FROM (
              SELECT
                id,
                name,
                data,
                ROW_NUMBER() OVER (
                  PARTITION BY id
                  ORDER BY chunk_id DESC, idx DESC
                ) AS rn
              FROM channel
            )
            WHERE rn = 1
            """
        ).fetchall()
    finally:
        archive_conn.close()
    if not rows:
        return
    updated_at = iso_now()
    rows_to_upsert: list[tuple[str, str, str, int, int, str, str]] = []
    for row in rows:
        raw = parse_json_blob(row["data"])
        channel = normalize_directory_channel_item(
            raw,
            channel_id=str(row["id"] or ""),
            name=str(row["name"] or ""),
            row_type=channel_type(raw, str(row["id"] or "")),
            is_private=bool(raw.get("is_private", False)),
            is_archived=bool(raw.get("is_archived", False)),
        )
        rows_to_upsert.append(
            (
                channel["id"],
                channel["name"],
                channel["type"],
                int(bool(channel["is_private"])),
                int(bool(channel["is_archived"])),
                json.dumps(channel, sort_keys=True),
                updated_at,
            )
        )
    if not rows_to_upsert:
        return
    conn = open_directory_db(workspace)
    try:
        with conn:
            for (
                channel_id,
                name,
                kind,
                is_private,
                is_archived,
                raw_json,
                stamp,
            ) in rows_to_upsert:
                conn.execute(
                    """
                    INSERT INTO channel_directory(
                        id, name, lookup_name, type, is_private, is_archived, raw_json, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        lookup_name = excluded.lookup_name,
                        type = excluded.type,
                        is_private = excluded.is_private,
                        is_archived = excluded.is_archived,
                        raw_json = excluded.raw_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        channel_id,
                        name,
                        normalize_lookup_name(name),
                        kind,
                        is_private,
                        is_archived,
                        raw_json,
                        stamp,
                    ),
                )
    finally:
        conn.close()


def seed_user_directory_from_archive(workspace: str) -> None:
    result = workspace_archive_result(workspace)
    if not result.db_path.exists():
        return
    archive_conn = open_db(result.db_path)
    try:
        rows = archive_conn.execute(
            """
            SELECT id, username, data
            FROM (
              SELECT
                id,
                username,
                data,
                ROW_NUMBER() OVER (
                  PARTITION BY id
                  ORDER BY chunk_id DESC, idx DESC
                ) AS rn
              FROM s_user
            )
            WHERE rn = 1
            """
        ).fetchall()
    finally:
        archive_conn.close()
    if not rows:
        return
    updated_at = iso_now()
    conn = open_directory_db(workspace)
    try:
        with conn:
            for row in rows:
                normalized = normalize_user_record(
                    str(row["id"]),
                    str(row["username"] or ""),
                    parse_json_blob(row["data"]),
                )
                conn.execute(
                    """
                    INSERT INTO user_directory(
                        id, username, display_name, lookup_name, real_name, deleted, is_bot, raw_json, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        username = excluded.username,
                        display_name = excluded.display_name,
                        lookup_name = excluded.lookup_name,
                        real_name = excluded.real_name,
                        deleted = excluded.deleted,
                        is_bot = excluded.is_bot,
                        raw_json = excluded.raw_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized["id"],
                        normalized["username"],
                        normalized["displayName"],
                        normalize_lookup_name(
                            normalized["displayName"]
                            or normalized["username"]
                            or normalized["id"]
                        ),
                        normalized["realName"],
                        int(normalized["deleted"]),
                        int(normalized["isBot"]),
                        json.dumps(normalized["raw"], sort_keys=True),
                        updated_at,
                    ),
                )
    finally:
        conn.close()


def resolve_channel_name(
    workspace: str,
    name: str,
) -> str | None:
    lookup_name = normalize_lookup_name(name)
    if not lookup_name:
        return None

    seed_channel_directory_from_archive(workspace)

    def lookup() -> str | None:
        conn = open_directory_db(workspace)
        try:
            row = conn.execute(
                """
                SELECT id
                FROM channel_directory
                WHERE lookup_name = ?
                ORDER BY updated_at DESC, name ASC
                LIMIT 1
                """,
                (lookup_name,),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else str(row["id"])

    channel_id = lookup()
    return channel_id


def load_directory_users(
    workspace: str, user_ids: set[str]
) -> dict[str, dict[str, Any]]:
    if not user_ids:
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    conn = open_directory_db(workspace)
    try:
        rows = conn.execute(
            f"""
            SELECT id, username, raw_json
            FROM user_directory
            WHERE id IN ({placeholders})
            """,
            tuple(sorted(user_ids)),
        ).fetchall()
    finally:
        conn.close()
    users: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw = parse_json_blob(row["raw_json"])
        users[str(row["id"])] = normalize_user_record(
            str(row["id"]),
            str(row["username"] or ""),
            raw,
        )
    return users


def load_directory_channels(
    workspace: str,
    channel_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not channel_ids:
        return {}
    placeholders = ",".join("?" for _ in channel_ids)
    conn = open_directory_db(workspace)
    try:
        rows = conn.execute(
            f"""
            SELECT id, name, type, is_private, is_archived, raw_json
            FROM channel_directory
            WHERE id IN ({placeholders})
            """,
            tuple(sorted(channel_ids)),
        ).fetchall()
    finally:
        conn.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result[str(row["id"])] = normalize_directory_channel_item(
            parse_json_blob(row["raw_json"]),
            channel_id=str(row["id"] or ""),
            name=str(row["name"] or ""),
            row_type=str(row["type"] or ""),
            is_private=bool(row["is_private"]),
            is_archived=bool(row["is_archived"]),
        )
    return result


def ensure_user_directory_entries(
    workspace: str,
    user_ids: set[str],
) -> dict[str, dict[str, Any]]:
    seed_user_directory_from_archive(workspace)
    users = load_directory_users(workspace, user_ids)
    return users


def ensure_channel_directory_entries(
    workspace: str,
    channel_ids: set[str],
) -> dict[str, dict[str, Any]]:
    seed_channel_directory_from_archive(workspace)
    channels = load_directory_channels(workspace, channel_ids)
    return channels


def run_command(
    cmd: list[str],
    *,
    check: bool = True,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise ToolError(
            f"missing command: {cmd[0]}. install it or put it on PATH, then retry."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(
            f"{' '.join(cmd)} timed out after {timeout_seconds}s. retry with a narrower "
            "lookback or run an explicit 'gotta slack sync ... --lookback ...' first."
        ) from exc
    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit code {proc.returncode}"
        if cmd and cmd[0] == "slackdump" and "panic:" in detail.lower():
            action = cmd[1] if len(cmd) > 1 else "command"
            raise ToolError(
                f"slackdump {action} crashed while gotta was trying to update archive "
                "coverage. Retry the native Slack command with a narrower window or a "
                "more specific channel reference."
            )
        raise ToolError(f"{' '.join(cmd)} failed: {detail}")
    return proc


def parse_json_blob(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, memoryview):
        value = value.tobytes()
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def canonical_thread_url(
    workspace: str, channel_id: str, thread_ts: str | None = None
) -> str:
    if not thread_ts:
        return f"https://{workspace}.slack.com/archives/{channel_id}"
    pnum = thread_ts.replace(".", "")
    return f"https://{workspace}.slack.com/archives/{channel_id}/p{pnum}"


def canonical_message_url(
    workspace: str,
    channel_id: str,
    ts: str,
    *,
    thread_ts: str | None = None,
) -> str:
    permalink = canonical_thread_url(workspace, channel_id, ts)
    root_ts = str(thread_ts or "").strip()
    if not root_ts or root_ts == ts:
        return permalink
    query = urllib.parse.urlencode({"thread_ts": root_ts})
    return f"{permalink}?{query}"


def canonical_doc_url(workspace: str, team_id: str, doc_id: str) -> str:
    return f"https://{workspace}.slack.com/docs/{team_id}/{doc_id}"


def _render_doc_markdown(data: bytes) -> bytes:
    projected = html_markdown(data)
    if projected is not None:
        return projected
    return html_text(data)


def _doc_shell_reason(data: bytes, *, final_url: str) -> str:
    text = data.decode("utf-8", errors="ignore").casefold()
    url = final_url.casefold()
    markers = (
        ("unsupported browser", "Slack returned its unsupported-browser shell"),
        ("sign in to slack", "Slack returned its sign-in shell"),
        (
            "slack is your productivity platform",
            "Slack returned a workspace shell instead of document content",
        ),
        (
            "download slack for desktop",
            "Slack returned a browser/app shell instead of document content",
        ),
    )
    for needle, reason in markers:
        if needle in text:
            return reason
    if "unsupported_browser" in url or "/signin" in url:
        return "Slack redirected to a non-document shell"
    return ""


def _doc_download_url(file_payload: dict[str, Any]) -> str:
    for key in ("url_private_download", "url_private"):
        value = str(file_payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _fetch_slack_doc(
    ref: SlackRef,
    *,
    interactive_ok: bool,
    timeout_seconds: int = DEFAULT_LIVE_SEARCH_TIMEOUT_SECONDS,
) -> tuple[bytes, dict[str, Any]]:
    if not ref.team_id or not ref.doc_id:
        raise ToolError("invalid Slack doc locator; expected team and doc identifiers")
    auth_state, _path = ensure_live_search_auth(
        ref.workspace, interactive_ok=interactive_ok
    )
    file_payload = slack_api_post(
        ref.workspace,
        auth_state,
        "files.info",
        data={"file": ref.doc_id},
        timeout_seconds=timeout_seconds,
    ).get("file")
    if not isinstance(file_payload, dict):
        raise ToolError(
            f"Slack doc {ref.team_id}:{ref.doc_id} could not be resolved through files.info."
        )
    download_url = _doc_download_url(file_payload)
    if not download_url:
        raise ToolError(
            f"Slack doc {ref.team_id}:{ref.doc_id} does not expose a downloadable body."
        )
    url = ref.url or canonical_doc_url(ref.workspace, ref.team_id, ref.doc_id)
    payload = slack_web_get(
        ref.workspace,
        auth_state,
        download_url,
        timeout_seconds=timeout_seconds,
    )
    body = bytes(payload.get("body") or b"")
    final_url = str(payload.get("url") or download_url)
    shell_reason = _doc_shell_reason(body, final_url=final_url)
    if shell_reason:
        raise ToolError(
            f"Slack doc {ref.team_id}:{ref.doc_id} could not be rendered natively: "
            f"{shell_reason}."
        )
    return body, {
        "workspace": ref.workspace,
        "kind": "doc",
        "teamId": ref.team_id,
        "docId": ref.doc_id,
        "url": str(file_payload.get("permalink") or url),
        "contentType": str(payload.get("contentType") or ""),
        "retrieval": "live_auth_files_info_download",
    }


def _doc_meta_from_capture(capture: Capture) -> dict[str, Any]:
    return {
        "workspace": str(capture.meta.get("workspace") or ""),
        "kind": "doc",
        "teamId": str(capture.meta.get("team_id") or ""),
        "docId": str(capture.meta.get("doc_id") or ""),
        "url": str(capture.meta.get("url") or ""),
        "contentType": str(capture.meta.get("content_type") or ""),
        "retrieval": str(
            capture.meta.get("retrieval") or "live_auth_files_info_download"
        ),
    }


def normalize_pnum(pnum: str) -> str:
    if len(pnum) != 16:
        raise ToolError(
            f"invalid Slack permalink timestamp: {pnum}. use a canonical Slack permalink "
            "or CHANNEL:THREAD_TS."
        )
    return f"{pnum[:10]}.{pnum[10:]}"


def thread_ts_from_query(raw: str) -> str:
    parsed = urllib.parse.urlparse(raw)
    candidates = urllib.parse.parse_qs(parsed.query).get("thread_ts") or []
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        if THREAD_TS_RE.fullmatch(value):
            return value
    return ""


def parse_slack_ref(raw: str, *, workspace: str) -> SlackRef:
    value = raw.strip()
    if not value:
        raise ToolError(
            "missing Slack reference. pass a Slack permalink, channel ID, bare channel "
            "name, or CHANNEL:THREAD_TS."
        )

    if value.startswith("slack:thread:"):
        _prefix, _kind, channel_id, ts = (value.split(":", 3) + ["", "", "", ""])[:4]
        if CHANNEL_ID_RE.match(channel_id):
            if THREAD_TS_RE.fullmatch(ts):
                thread_ts = ts
            else:
                thread_ts = normalize_pnum(ts)
            return SlackRef(
                raw=raw,
                workspace=workspace,
                kind="thread",
                channel_id=channel_id,
                thread_ts=thread_ts,
                url=canonical_thread_url(workspace, channel_id, thread_ts),
            )
    if value.startswith("slack:doc:"):
        _prefix, _kind, team_id, doc_id = (value.split(":", 3) + ["", "", "", ""])[:4]
        if team_id and doc_id:
            return SlackRef(
                raw=raw,
                workspace=workspace,
                kind="doc",
                channel_id="",
                team_id=team_id,
                doc_id=doc_id,
                url=canonical_doc_url(workspace, team_id, doc_id),
            )

    match = THREAD_COLON_RE.match(value)
    if match:
        channel_id = match.group("channel")
        thread_ts = match.group("ts")
        return SlackRef(
            raw=raw,
            workspace=workspace,
            kind="thread",
            channel_id=channel_id,
            thread_ts=thread_ts,
            url=canonical_thread_url(workspace, channel_id, thread_ts),
        )

    if CHANNEL_ID_RE.match(value):
        return SlackRef(
            raw=raw,
            workspace=workspace,
            kind="channel",
            channel_id=value,
            url=canonical_thread_url(workspace, value),
        )

    candidates = [value]
    decoded = value
    for _ in range(3):
        decoded = urllib.parse.unquote(decoded)
        candidates.append(decoded)

    for candidate in candidates:
        query_thread_ts = thread_ts_from_query(candidate)
        doc_match = DOC_URL_RE.search(candidate)
        if doc_match:
            ref_workspace = doc_match.group("workspace")
            team_id = doc_match.group("team")
            doc_id = doc_match.group("doc")
            return SlackRef(
                raw=raw,
                workspace=ref_workspace,
                kind="doc",
                channel_id="",
                team_id=team_id,
                doc_id=doc_id,
                url=canonical_doc_url(ref_workspace, team_id, doc_id),
            )
        match = PERMALINK_RE.search(candidate)
        if match:
            ref_workspace = match.group("workspace")
            channel_id = match.group("channel")
            pnum = match.group("pnum")
            if pnum or query_thread_ts:
                thread_ts = query_thread_ts or normalize_pnum(pnum)
                return SlackRef(
                    raw=raw,
                    workspace=ref_workspace,
                    kind="thread",
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    url=canonical_thread_url(ref_workspace, channel_id, thread_ts),
                )
            return SlackRef(
                raw=raw,
                workspace=ref_workspace,
                kind="channel",
                channel_id=channel_id,
                url=canonical_thread_url(ref_workspace, channel_id),
            )
        path_match = ARCHIVE_PATH_RE.search(candidate)
        if path_match:
            channel_id = path_match.group("channel")
            pnum = path_match.group("pnum")
            if pnum or query_thread_ts:
                thread_ts = query_thread_ts or normalize_pnum(pnum)
                return SlackRef(
                    raw=raw,
                    workspace=workspace,
                    kind="thread",
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    url=canonical_thread_url(workspace, channel_id, thread_ts),
                )
            return SlackRef(
                raw=raw,
                workspace=workspace,
                kind="channel",
                channel_id=channel_id,
                url=canonical_thread_url(workspace, channel_id),
            )
        doc_path_match = DOC_PATH_RE.search(candidate)
        if doc_path_match:
            team_id = doc_path_match.group("team")
            doc_id = doc_path_match.group("doc")
            return SlackRef(
                raw=raw,
                workspace=workspace,
                kind="doc",
                channel_id="",
                team_id=team_id,
                doc_id=doc_id,
                url=canonical_doc_url(workspace, team_id, doc_id),
            )

    raise ToolError(
        f"unsupported Slack reference: {raw}. use a Slack permalink, channel ID, bare "
        "channel name, or CHANNEL:THREAD_TS."
    )


def resolve_slack_ref(
    raw: str,
    *,
    workspace: str,
) -> SlackRef:
    value = raw.strip()
    if not value:
        raise ToolError(
            "missing Slack reference. pass a Slack permalink, channel ID, bare channel "
            "name, or CHANNEL:THREAD_TS."
        )

    if ":" in value:
        lhs, rhs = value.split(":", 1)
        if THREAD_TS_RE.match(rhs):
            try:
                channel_ref = parse_slack_ref(lhs, workspace=workspace)
            except ToolError:
                channel_id = resolve_channel_name(workspace, lhs)
                if channel_id:
                    return SlackRef(
                        raw=raw,
                        workspace=workspace,
                        kind="thread",
                        channel_id=channel_id,
                        thread_ts=rhs,
                        url=canonical_thread_url(workspace, channel_id, rhs),
                    )
            else:
                return SlackRef(
                    raw=raw,
                    workspace=channel_ref.workspace,
                    kind="thread",
                    channel_id=channel_ref.channel_id,
                    thread_ts=rhs,
                    url=canonical_thread_url(
                        channel_ref.workspace,
                        channel_ref.channel_id,
                        rhs,
                    ),
                )

    try:
        return parse_slack_ref(raw, workspace=workspace)
    except ToolError:
        channel_id = resolve_channel_name(workspace, value)
        if channel_id:
            return SlackRef(
                raw=raw,
                workspace=workspace,
                kind="channel",
                channel_id=channel_id,
                url=canonical_thread_url(workspace, channel_id),
            )
        raise


def parse_lookback(spec: str) -> tuple[str, dt.timedelta]:
    match = LOOKBACK_RE.match(spec.strip())
    if not match:
        raise ToolError(
            f"invalid lookback '{spec}'; use forms like 1y, 7d, 3w, 48h, or 30m"
        )
    count = int(match.group("count"))
    unit = match.group("unit").lower()
    if unit == "s":
        delta = dt.timedelta(seconds=count)
    elif unit == "m":
        delta = dt.timedelta(minutes=count)
    elif unit == "h":
        delta = dt.timedelta(hours=count)
    elif unit == "d":
        delta = dt.timedelta(days=count)
    elif unit == "w":
        delta = dt.timedelta(weeks=count)
    else:
        delta = dt.timedelta(days=365 * count)
    return f"{count}{unit}", delta


def sqlite_table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        LIMIT 1
        """,
        (name,),
    ).fetchone()
    return row is not None


def parse_search_terms(query: str, *, match_mode: str) -> list[str]:
    stripped = query.strip()
    if not stripped:
        raise ToolError("missing Slack search query")
    if match_mode == "literal":
        return [stripped]
    try:
        parts = [part.strip() for part in shlex.split(stripped) if part.strip()]
    except ValueError:
        parts = [part.strip() for part in stripped.split() if part.strip()]
    return parts or [stripped]


def _parse_search_parts(query: str, *, match_mode: str) -> tuple[list[str], list[str]]:
    parts = parse_search_terms(query, match_mode=match_mode)
    if match_mode == "literal":
        return parts, []
    terms: list[str] = []
    modifiers: list[str] = []
    for part in parts:
        if SEARCH_MODIFIER_RE.fullmatch(part):
            modifiers.append(part)
        else:
            terms.append(part)
    return terms, modifiers


def search_spec(query: str, *, match_mode: str) -> SearchSpec:
    if match_mode not in {"literal", "all", "any"}:
        raise ToolError(f"unsupported Slack search match mode: {match_mode}")
    terms, modifiers = _parse_search_parts(query, match_mode=match_mode)
    return SearchSpec(
        raw_query=query,
        terms=terms,
        match_mode=match_mode,
        modifiers=modifiers,
    )


def slack_ts_key(value: str) -> int:
    cleaned = value.strip()
    if not cleaned:
        raise ToolError("missing Slack timestamp")
    seconds, dot, micros = cleaned.partition(".")
    if not seconds.isdigit():
        raise ToolError(f"invalid Slack timestamp: {value}")
    if dot:
        micros = (micros + "000000")[:6]
        if not micros.isdigit():
            raise ToolError(f"invalid Slack timestamp: {value}")
    else:
        micros = "000000"
    return int(seconds) * 1_000_000 + int(micros)


def slack_ts_to_datetime(value: str) -> dt.datetime:
    key = slack_ts_key(value)
    seconds, micros = divmod(key, 1_000_000)
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).replace(
        microsecond=micros
    )


def datetime_to_slack_key(value: dt.datetime) -> int:
    utc_value = value.astimezone(dt.timezone.utc)
    return int(utc_value.timestamp()) * 1_000_000 + utc_value.microsecond


def local_timezone() -> dt.tzinfo:
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def parse_time_arg(value: str, *, flag: str) -> dt.datetime:
    cleaned = value.strip()
    if not cleaned:
        raise ToolError(f"{flag} requires a value")
    if THREAD_TS_RE.match(cleaned):
        return slack_ts_to_datetime(cleaned)
    normalized = cleaned
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ToolError(
            f"invalid {flag} value '{value}'; use ISO 8601 or a Slack ts like 1772819749.912029"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_timezone())
    return parsed.astimezone(dt.timezone.utc)


def format_utc_iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def format_archive_bound(value: float | None) -> str | None:
    if value is None:
        return None
    return format_utc_iso(dt.datetime.fromtimestamp(value, dt.timezone.utc))


def _parse_search_date_range(value: str) -> tuple[dt.datetime, dt.datetime] | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if YEAR_RE.fullmatch(cleaned):
        start = dt.datetime(int(cleaned), 1, 1, tzinfo=local_timezone())
        end = dt.datetime(int(cleaned) + 1, 1, 1, tzinfo=local_timezone())
        return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)
    if YEAR_MONTH_RE.fullmatch(cleaned):
        year, month = (int(part) for part in cleaned.split("-", 1))
        next_year = year + 1 if month == 12 else year
        next_month = 1 if month == 12 else month + 1
        start = dt.datetime(year, month, 1, tzinfo=local_timezone())
        end = dt.datetime(next_year, next_month, 1, tzinfo=local_timezone())
        return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}$", cleaned):
        start = parse_time_arg(cleaned, flag="search qualifier")
        end = start + dt.timedelta(days=1)
        return start, end
    return None


def _archive_search_time_predicates(
    spec: SearchSpec,
) -> tuple[list[str], list[Any], list[str]]:
    predicates: list[str] = []
    params: list[Any] = []
    applied: list[str] = []
    for token in spec.modifiers:
        match = SEARCH_MODIFIER_RE.fullmatch(token)
        if not match:
            continue
        name = match.group("name").lower()
        value = match.group("value")
        if name not in {"before", "after", "on", "during"}:
            continue
        if match.group("neg"):
            raise ToolError(
                f"archive search does not support negated time qualifier `{token}`"
            )
        range_bounds = _parse_search_date_range(value)
        if name in {"on", "during"}:
            if range_bounds is None:
                raise ToolError(
                    f"archive search can only translate `{name}:` with YYYY, YYYY-MM, or YYYY-MM-DD values; got `{token}`"
                )
            start, end = range_bounds
            predicates.extend(
                [
                    "CAST(REPLACE(ts, '.', '') AS INTEGER) >= ?",
                    "CAST(REPLACE(ts, '.', '') AS INTEGER) < ?",
                ]
            )
            params.extend([datetime_to_slack_key(start), datetime_to_slack_key(end)])
            applied.append(token)
            continue
        if range_bounds is not None:
            start, end = range_bounds
            boundary = start if name == "before" else end
        else:
            boundary = parse_time_arg(value, flag=f"{name}:")
        if name == "before":
            predicates.append("CAST(REPLACE(ts, '.', '') AS INTEGER) < ?")
            params.append(datetime_to_slack_key(boundary))
        else:
            predicates.append("CAST(REPLACE(ts, '.', '') AS INTEGER) >= ?")
            params.append(datetime_to_slack_key(boundary))
        applied.append(token)
    return predicates, params, applied


def _cap_sync_window(
    *,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    lookback: dt.timedelta | None = None,
) -> None:
    if lookback is not None and lookback > MAX_SYNC_WINDOW:
        raise ToolError(
            f"Slack archive pulls are capped at {MAX_SYNC_LOOKBACK} per window; "
            f"requested {lookback}."
        )
    if since is not None and until is not None and (until - since) > MAX_SYNC_WINDOW:
        raise ToolError(
            f"Slack archive pulls are capped at {MAX_SYNC_LOOKBACK} per window; "
            f"requested window {format_utc_iso(since)} to {format_utc_iso(until)}."
        )


def _channel_ref(ref: SlackRef) -> SlackRef:
    return SlackRef(
        raw=ref.raw,
        workspace=ref.workspace,
        kind="channel",
        channel_id=ref.channel_id,
        url=canonical_thread_url(ref.workspace, ref.channel_id),
    )


def _permalink_token(thread_ts: str) -> str:
    return f"p{thread_ts.replace('.', '')}"


def _window_is_covered(
    result: ArchiveResult,
    *,
    channel_id: str,
    since: dt.datetime,
    until: dt.datetime,
) -> bool:
    oldest_ts, newest_ts = archive_coverage_for_channel(result, channel_id)
    if oldest_ts is None or newest_ts is None:
        return False
    return oldest_ts <= since.timestamp() and newest_ts >= until.timestamp()


def session_matches_thread(args: str, ref: SlackRef) -> bool:
    if ref.kind != "thread" or not ref.thread_ts:
        return False
    normalized_args = str(args or "")
    thread_url = ref.url or canonical_thread_url(
        ref.workspace,
        ref.channel_id,
        ref.thread_ts,
    )
    thread_path = f"/archives/{ref.channel_id}/{_permalink_token(ref.thread_ts)}"
    return any(
        marker in normalized_args
        for marker in (
            thread_url,
            thread_path,
            f"thread_ts={ref.thread_ts}",
        )
    )


def archive_coverage_for_thread(
    result: ArchiveResult,
    ref: SlackRef,
) -> tuple[float | None, float | None]:
    if ref.kind != "thread" or not ref.thread_ts:
        return (None, None)
    conn = open_db(result.db_path)
    try:
        session_rows = conn.execute(
            """
            SELECT from_ts, to_ts, args
            FROM session
            WHERE from_ts IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()
    session_starts = [
        parsed.timestamp()
        for row in session_rows
        if session_matches_thread(str(row["args"] or ""), ref)
        and (parsed := parse_slackdump_timestamp(str(row["from_ts"] or ""))) is not None
    ]
    session_ends = [
        parsed.timestamp()
        for row in session_rows
        if session_matches_thread(str(row["args"] or ""), ref)
        and (parsed := parse_slackdump_timestamp(str(row["to_ts"] or ""))) is not None
    ]
    if not session_starts:
        return (None, None)
    return (
        min(session_starts),
        max(session_ends) if session_ends else None,
    )


def thread_window_is_covered(
    result: ArchiveResult,
    ref: SlackRef,
    *,
    since: dt.datetime,
    until: dt.datetime,
) -> bool:
    oldest_ts, newest_ts = archive_coverage_for_thread(result, ref)
    if oldest_ts is None or newest_ts is None:
        return False
    return oldest_ts <= since.timestamp() and newest_ts >= until.timestamp()


def _run_bounded_archive_window(
    ref: SlackRef,
    *,
    since: dt.datetime,
    until: dt.datetime,
    refresh: bool,
    timeout_seconds: int | None = None,
) -> ArchiveResult:
    _cap_sync_window(since=since, until=until)
    result = workspace_archive_result(ref.workspace)
    channel_ref = _channel_ref(ref)
    if result.db_path.exists() and not refresh:
        if _window_is_covered(
            result, channel_id=ref.channel_id, since=since, until=until
        ):
            return result
        oldest_ts, newest_ts = archive_coverage_for_channel(result, ref.channel_id)
        if oldest_ts is None and newest_ts is None:
            return run_archive_into_workspace(
                channel_ref,
                result=result,
                time_from=to_slackdump_time(since),
                time_to=to_slackdump_time(until),
                timeout_seconds=timeout_seconds,
            )
        if oldest_ts is None or oldest_ts > since.timestamp():
            upper = until
            if oldest_ts is not None:
                upper = min(
                    until,
                    dt.datetime.fromtimestamp(oldest_ts, dt.timezone.utc),
                )
            if since < upper:
                result = run_archive_into_workspace(
                    channel_ref,
                    result=result,
                    time_from=to_slackdump_time(since),
                    time_to=to_slackdump_time(upper),
                    timeout_seconds=timeout_seconds,
                )
        if newest_ts is None or newest_ts < until.timestamp():
            lower = since
            if newest_ts is not None:
                lower = max(
                    since,
                    dt.datetime.fromtimestamp(newest_ts, dt.timezone.utc),
                )
            if lower < until:
                result = run_archive_into_workspace(
                    channel_ref,
                    result=result,
                    time_from=to_slackdump_time(lower),
                    time_to=to_slackdump_time(until),
                    timeout_seconds=timeout_seconds,
                )
        return result
    return run_archive_into_workspace(
        channel_ref,
        result=result,
        time_from=to_slackdump_time(since),
        time_to=to_slackdump_time(until),
        timeout_seconds=timeout_seconds,
    )


def thread_hydration_window(ref: SlackRef) -> tuple[dt.datetime, dt.datetime]:
    if ref.kind != "thread" or not ref.thread_ts:
        raise ToolError("thread-centered hydration requires a Slack thread locator")
    center = slack_ts_to_datetime(ref.thread_ts)
    since = center - THREAD_HYDRATION_HALF_WINDOW
    until = min(center + THREAD_HYDRATION_HALF_WINDOW, utc_now())
    return since, until


def ensure_thread_window_archive(
    ref: SlackRef,
    *,
    refresh: bool,
    timeout_seconds: int | None = None,
) -> ArchiveResult:
    since, until = thread_hydration_window(ref)
    _cap_sync_window(since=since, until=until)
    result = workspace_archive_result(ref.workspace)
    if (
        result.db_path.exists()
        and not refresh
        and thread_window_is_covered(result, ref, since=since, until=until)
    ):
        return result
    return run_archive_into_workspace(
        ref,
        result=result,
        time_from=to_slackdump_time(since),
        time_to=to_slackdump_time(until),
        timeout_seconds=timeout_seconds,
    )


def resolve_channel_window(args: argparse.Namespace) -> ChannelWindow:
    if args.lookback and args.since:
        raise ToolError("`--lookback` cannot be combined with `--since`")
    if args.strict_window and not (args.lookback or args.since or args.until):
        raise ToolError(
            "`--strict-window` requires `--lookback`, `--since`, or `--until`"
        )
    if args.lookback:
        lookback_token, lookback_delta = parse_lookback(args.lookback)
        until = parse_time_arg(args.until, flag="--until") if args.until else utc_now()
        since = until - lookback_delta
        return ChannelWindow(
            since=since,
            until=until,
            lookback=lookback_token,
            strict=args.strict_window,
        )
    since = parse_time_arg(args.since, flag="--since") if args.since else None
    until = parse_time_arg(args.until, flag="--until") if args.until else None
    if since and until and since > until:
        raise ToolError("`--since` must be earlier than or equal to `--until`")
    return ChannelWindow(
        since=since,
        until=until,
        lookback=None,
        strict=args.strict_window,
    )


def to_slackdump_time(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def workspace_archive_dir(workspace: str) -> Path:
    return workspace_cache_dir(workspace) / CANONICAL_ARCHIVE_DIRNAME


def archive_paths(root_dir: Path) -> ArchiveResult:
    return ArchiveResult(
        root_dir=root_dir,
        db_path=root_dir / "slackdump.sqlite",
        log_path=root_dir / "slackdump.log",
    )


def session_matches_channel(args: str, channel_id: str) -> bool:
    return (
        f"/archives/{channel_id}" in args
        or f"|{channel_id}|" in args
        or re.search(rf"(^|[|\s]){re.escape(channel_id)}($|[|\s])", args) is not None
    )


def archive_coverage_for_channel(
    result: ArchiveResult,
    channel_id: str,
) -> tuple[float | None, float | None]:
    conn = open_db(result.db_path)
    try:
        session_rows = conn.execute(
            """
            SELECT from_ts, to_ts, args
            FROM session
            WHERE from_ts IS NOT NULL
            """
        ).fetchall()
        session_starts = [
            parsed.timestamp()
            for row in session_rows
            if session_matches_channel(str(row["args"] or ""), channel_id)
            and (parsed := parse_slackdump_timestamp(str(row["from_ts"] or "")))
            is not None
        ]
        session_ends = [
            parsed.timestamp()
            for row in session_rows
            if session_matches_channel(str(row["args"] or ""), channel_id)
            and (parsed := parse_slackdump_timestamp(str(row["to_ts"] or "")))
            is not None
        ]
        if session_starts:
            return (
                min(session_starts),
                max(session_ends) if session_ends else None,
            )
        row = conn.execute(
            """
            SELECT
                MIN(CAST(REPLACE(ts, '.', '') AS INTEGER)),
                MAX(CAST(REPLACE(ts, '.', '') AS INTEGER))
            FROM message
            WHERE channel_id = ?
            """,
            (channel_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return (None, None)
    oldest = row[0]
    newest = row[1]
    return (
        int(oldest) / 1_000_000 if oldest is not None else None,
        int(newest) / 1_000_000 if newest is not None else None,
    )


def workspace_archive_result(workspace: str) -> ArchiveResult:
    return archive_paths(workspace_archive_dir(workspace))


def existing_channel_cache(workspace: str, channel_id: str) -> ArchiveResult | None:
    result = workspace_archive_result(workspace)
    if not result.db_path.exists():
        return None
    oldest_ts, newest_ts = archive_coverage_for_channel(result, channel_id)
    if oldest_ts is None and newest_ts is None:
        return None
    return result


def ensure_archive_exists(result: ArchiveResult, *, description: str) -> ArchiveResult:
    if result.db_path.exists():
        return result
    if result.log_path.exists():
        raise ToolError(
            f"{description} did not produce sqlite output; see log: {result.log_path}"
        )
    raise ToolError(f"{description} did not produce sqlite output")


def run_archive_into_workspace(
    ref: SlackRef,
    *,
    result: ArchiveResult,
    time_from: str | None,
    time_to: str | None,
    timeout_seconds: int | None = None,
) -> ArchiveResult:
    result.root_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "slackdump",
        "archive",
        "-enterprise",
        "-y",
        "-files=false",
        "-channel-users",
        "-workspace",
        ref.workspace,
        "-o",
        str(result.root_dir),
        "-log",
        str(result.log_path),
    ]
    if time_from:
        cmd.extend(["-time-from", time_from])
    if time_to:
        cmd.extend(["-time-to", time_to])
    target = ref.url if ref.url else ref.raw if ref.raw else ref.channel_id
    if ref.kind == "channel":
        target = ref.url or ref.channel_id
    cmd.append(target)
    run_command(cmd, timeout_seconds=timeout_seconds)
    return ensure_archive_exists(
        result,
        description=f"workspace archive update for {ref.workspace}/{ref.channel_id}",
    )


def run_sync_archive(
    ref: SlackRef,
    *,
    lookback: str,
    refresh: bool,
    timeout_seconds: int | None = None,
) -> ArchiveResult:
    channel_ref = _channel_ref(ref)
    lookback_token, lookback_delta = parse_lookback(lookback)
    _cap_sync_window(lookback=lookback_delta)
    result = workspace_archive_result(ref.workspace)
    now = dt.datetime.now(dt.timezone.utc)
    requested_from = now - lookback_delta
    if result.db_path.exists() and not refresh:
        oldest_ts, newest_ts = archive_coverage_for_channel(result, ref.channel_id)
        covers_oldest = (
            oldest_ts is not None and oldest_ts <= requested_from.timestamp()
        )
        covers_newest = (
            newest_ts is not None
            and newest_ts >= now.timestamp() - DEFAULT_COVERAGE_FRESHNESS_SLOP_SECONDS
        )
        if covers_oldest and covers_newest:
            return result
        if oldest_ts is None and newest_ts is None:
            return run_archive_into_workspace(
                channel_ref,
                result=result,
                time_from=to_slackdump_time(requested_from),
                time_to=None,
                timeout_seconds=timeout_seconds,
            )
        if not covers_oldest and oldest_ts is not None:
            oldest_dt = dt.datetime.fromtimestamp(oldest_ts, dt.timezone.utc)
            result = run_archive_into_workspace(
                channel_ref,
                result=result,
                time_from=to_slackdump_time(requested_from),
                time_to=to_slackdump_time(oldest_dt),
                timeout_seconds=timeout_seconds,
            )
        if not covers_newest:
            newest_dt = (
                dt.datetime.fromtimestamp(newest_ts, dt.timezone.utc)
                if newest_ts is not None
                else requested_from
            )
            result = run_archive_into_workspace(
                channel_ref,
                result=result,
                time_from=to_slackdump_time(newest_dt),
                time_to=None,
                timeout_seconds=timeout_seconds,
            )
        return result
    return run_archive_into_workspace(
        channel_ref,
        result=result,
        time_from=to_slackdump_time(dt.datetime.now(dt.timezone.utc) - lookback_delta),
        time_to=None,
        timeout_seconds=timeout_seconds,
    )


def try_opportunistic_sync(ref: SlackRef) -> ArchiveResult:
    print(
        (
            f"note: pulling recent Slack history for channel {ref.channel_id} "
            f"({DEFAULT_RECENT_LOOKBACK}, timeout {DEFAULT_OPPORTUNISTIC_TIMEOUT_SECONDS}s)"
        ),
        file=sys.stderr,
    )
    return run_sync_archive(
        ref,
        lookback=DEFAULT_RECENT_LOOKBACK,
        refresh=False,
        timeout_seconds=DEFAULT_OPPORTUNISTIC_TIMEOUT_SECONDS,
    )


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def open_readonly_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def open_workspace_db(
    workspace: str, *, database: str
) -> tuple[sqlite3.Connection, Path]:
    if database == "archive":
        result = ensure_archive_exists(
            workspace_archive_result(workspace),
            description=f"workspace archive for {workspace}",
        )
        return open_readonly_db(result.db_path), result.db_path
    path = directory_db_path(workspace)
    return open_directory_db(workspace), path


def sqlite_json_value(value: Any) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return value.hex()
    return value


def latest_channel_row(conn: sqlite3.Connection, channel_id: str) -> sqlite3.Row | None:
    if not sqlite_table_exists(conn, "channel"):
        return None
    return conn.execute(
        """
        SELECT id, name, data
        FROM channel
        WHERE id = ?
        ORDER BY chunk_id DESC, idx DESC
        LIMIT 1
        """,
        (channel_id,),
    ).fetchone()


def channel_window_coverage(
    result: ArchiveResult,
    channel_id: str,
    window: ChannelWindow,
) -> dict[str, Any]:
    archive_first_ts, archive_last_ts = archive_coverage_for_channel(result, channel_id)
    requested_since = window.since.timestamp() if window.since else None
    requested_until = window.until.timestamp() if window.until else None
    covers_since = requested_since is None or (
        archive_first_ts is not None and archive_first_ts <= requested_since
    )
    covers_until = requested_until is None or (
        archive_last_ts is not None and archive_last_ts >= requested_until
    )
    return {
        "since": format_utc_iso(window.since),
        "until": format_utc_iso(window.until),
        "lookback": window.lookback,
        "strict": window.strict,
        "archiveSince": format_archive_bound(archive_first_ts),
        "archiveUntil": format_archive_bound(archive_last_ts),
        "complete": bool(covers_since and covers_until),
    }


def tri_state_matches(mode: str, value: bool) -> bool:
    if mode == "any":
        return True
    if mode == "only":
        return value
    if mode == "exclude":
        return not value
    raise ToolError(f"unsupported filter mode: {mode}")


def channel_directory_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("id") or ""),
        str(item.get("name") or ""),
        str(item.get("name_normalized") or ""),
    ]
    purpose = item.get("purpose")
    if isinstance(purpose, dict):
        parts.append(str(purpose.get("value") or ""))
    topic = item.get("topic")
    if isinstance(topic, dict):
        parts.append(str(topic.get("value") or ""))
    return "\n".join(parts)


def normalize_directory_channel_item(
    item: dict[str, Any],
    *,
    channel_id: str = "",
    name: str = "",
    row_type: str = "",
    is_private: bool | None = None,
    is_archived: bool | None = None,
) -> dict[str, Any]:
    normalized = dict(item)
    normalized_id = str(
        normalized.get("id") or channel_id or normalized.get("channel_id") or ""
    ).strip()
    normalized_name = (
        str(normalized.get("name") or name or normalized_id).strip() or normalized_id
    )
    normalized["id"] = normalized_id
    normalized["name"] = normalized_name
    normalized["name_normalized"] = str(normalized.get("name_normalized") or "").strip()
    normalized["is_private"] = bool(
        normalized.get("is_private", False) if is_private is None else is_private
    )
    normalized["is_archived"] = bool(
        normalized.get("is_archived", False) if is_archived is None else is_archived
    )
    normalized["is_shared"] = bool(normalized.get("is_shared", False))
    normalized["is_ext_shared"] = bool(normalized.get("is_ext_shared", False))
    normalized["isPrivate"] = bool(normalized["is_private"])
    normalized["isArchived"] = bool(normalized["is_archived"])
    normalized["isShared"] = bool(normalized["is_shared"])
    normalized["isExtShared"] = bool(normalized["is_ext_shared"])
    normalized["type"] = row_type or str(
        normalized.get("type") or channel_type(normalized, normalized_id)
    )
    if not isinstance(normalized.get("purpose"), dict):
        normalized["purpose"] = {"value": ""}
    if not isinstance(normalized.get("topic"), dict):
        normalized["topic"] = {"value": ""}
    return with_visibility_metadata(normalized, provider="slack")


def latest_user_rows(
    conn: sqlite3.Connection, user_ids: set[str]
) -> dict[str, dict[str, Any]]:
    if not user_ids:
        return {}
    if not sqlite_table_exists(conn, "s_user"):
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    rows = conn.execute(
        f"""
        SELECT id, username, data
        FROM (
          SELECT
            id,
            username,
            data,
            ROW_NUMBER() OVER (
              PARTITION BY id
              ORDER BY chunk_id DESC, idx DESC
            ) AS rn
          FROM s_user
          WHERE id IN ({placeholders})
        )
        WHERE rn = 1
        """,
        tuple(sorted(user_ids)),
    ).fetchall()
    users: dict[str, dict[str, Any]] = {}
    for row in rows:
        user_id = str(row["id"])
        users[user_id] = normalize_user_record(
            user_id,
            str(row["username"] or ""),
            parse_json_blob(row["data"]),
        )
    return users


def user_display_name(user: dict[str, Any] | None) -> str:
    if user is None:
        return "unknown"
    for candidate in (
        user.get("displayName"),
        user.get("realName"),
        user.get("username"),
        user.get("id"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return "unknown"


def message_display_name(
    data: dict[str, Any],
    user: dict[str, Any] | None,
    user_id: str,
) -> str:
    if user is not None:
        display = user_display_name(user)
        if display != "unknown":
            return display
    for container_name in ("user_profile", "profile"):
        container = data.get(container_name)
        if isinstance(container, dict):
            for candidate in (
                container.get("real_name"),
                container.get("display_name"),
                container.get("name"),
            ):
                value = str(candidate or "").strip()
                if value:
                    return value
    for candidate in (data.get("username"), data.get("user_name"), user_id):
        value = str(candidate or "").strip()
        if value:
            return value
    return "unknown"


def resolve_mentions(
    text: str,
    users: dict[str, dict[str, Any]],
    channels: dict[str, dict[str, Any]],
) -> str:
    def repl_user(match: re.Match[str]) -> str:
        user_id = match.group(1)
        user = users.get(user_id)
        if user is None:
            return match.group(0)
        return "@" + user_display_name(user)

    def repl_channel(match: re.Match[str]) -> str:
        channel_id = match.group(1)
        channel = channels.get(channel_id)
        if channel is not None:
            return "#" + str(channel.get("name") or channel_id)
        fallback = str(match.group(2) or "").strip()
        if fallback:
            return "#" + fallback
        return match.group(0)

    resolved = re.sub(r"<@([A-Z0-9]+)>", repl_user, text)
    return re.sub(r"<#([A-Z0-9]+)(?:\|([^>]+))?>", repl_channel, resolved)


def format_local_ts(ts: str) -> str:
    stamp = ts
    try:
        stamp_dt = dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc).astimezone()
        stamp = stamp_dt.strftime("%Y-%m-%d %H:%M:%S")
        if "." in ts:
            stamp += "." + ts.split(".", 1)[1][:3]
    except ValueError:
        pass
    return stamp


def single_line_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " ")).strip()


def escape_like(query: str) -> str:
    return query.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def channel_type(channel_data: dict[str, Any], channel_id: str) -> str:
    if channel_data.get("is_mpim"):
        return "mpim"
    if channel_id.startswith("D"):
        return "im"
    if channel_data.get("is_private"):
        return "private_channel"
    return "public_channel"


def normalize_channel(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return with_visibility_metadata(
            {
                "id": "",
                "name": "",
                "type": "unknown",
                "isArchived": False,
                "isPrivate": False,
                "isShared": False,
                "isExtShared": False,
                "is_archived": False,
                "is_private": False,
                "is_shared": False,
                "is_ext_shared": False,
            },
            provider="slack",
        )
    data = parse_json_blob(row["data"])
    normalized = {
        "id": str(row["id"]),
        "name": str(row["name"] or data.get("name") or row["id"]),
        "isArchived": bool(data.get("is_archived", False)),
        "isPrivate": bool(data.get("is_private", False)),
        "isShared": bool(data.get("is_shared", False)),
        "isExtShared": bool(data.get("is_ext_shared", False)),
        "type": channel_type(data, str(row["id"])),
    }
    normalized["is_archived"] = bool(normalized["isArchived"])
    normalized["is_private"] = bool(normalized["isPrivate"])
    normalized["is_shared"] = bool(normalized["isShared"])
    normalized["is_ext_shared"] = bool(normalized["isExtShared"])
    return with_visibility_metadata(normalized, provider="slack")


def merge_channels(
    primary: dict[str, dict[str, Any]],
    secondary: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    merged = dict(secondary)
    for channel_id, channel in primary.items():
        if str(channel.get("id") or "").strip() or channel_id not in merged:
            merged[channel_id] = channel
    return merged


def merge_users(
    primary: dict[str, dict[str, Any]],
    secondary: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    merged = dict(secondary)
    merged.update(primary)
    return merged


def load_message_rows(
    conn: sqlite3.Connection,
    ref: SlackRef,
    *,
    window: ChannelWindow | None = None,
) -> list[sqlite3.Row]:
    if ref.kind == "thread":
        return conn.execute(
            """
            SELECT channel_id, ts, thread_ts, txt, data
            FROM (
              SELECT
                channel_id,
                ts,
                thread_ts,
                txt,
                data,
                ROW_NUMBER() OVER (
                  PARTITION BY channel_id, ts
                  ORDER BY chunk_id DESC, idx DESC
                ) AS rn
              FROM message
              WHERE channel_id = ?
                AND (ts = ? OR thread_ts = ?)
            )
            WHERE rn = 1
            ORDER BY CAST(REPLACE(ts, '.', '') AS INTEGER)
            """,
            (ref.channel_id, ref.thread_ts, ref.thread_ts),
        ).fetchall()
    where_parts = ["channel_id = ?"]
    params: list[Any] = [ref.channel_id]
    if window and window.since is not None:
        where_parts.append("CAST(REPLACE(ts, '.', '') AS INTEGER) >= ?")
        params.append(datetime_to_slack_key(window.since))
    if window and window.until is not None:
        where_parts.append("CAST(REPLACE(ts, '.', '') AS INTEGER) <= ?")
        params.append(datetime_to_slack_key(window.until))
    sql = f"""
        SELECT channel_id, ts, thread_ts, txt, data
        FROM (
          SELECT
            channel_id,
            ts,
            thread_ts,
            txt,
            data,
            ROW_NUMBER() OVER (
              PARTITION BY channel_id, ts
              ORDER BY chunk_id DESC, idx DESC
            ) AS rn
          FROM message
          WHERE {" AND ".join(where_parts)}
        )
        WHERE rn = 1
        ORDER BY CAST(REPLACE(ts, '.', '') AS INTEGER)
        """
    return conn.execute(sql, tuple(params)).fetchall()


def normalize_messages(
    rows: list[sqlite3.Row],
    *,
    workspace: str,
    users: dict[str, dict[str, Any]],
    channels: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for row in rows:
        data = parse_json_blob(row["data"])
        user_id = str(data.get("user") or "").strip()
        text = str(row["txt"] or data.get("text") or "")
        resolved = resolve_mentions(text, users, channels)
        user_record = users.get(user_id)
        display_name = message_display_name(data, user_record, user_id)
        messages.append(
            {
                "channelId": str(row["channel_id"]),
                "ts": str(row["ts"]),
                "threadTs": str(row["thread_ts"] or ""),
                "permalink": canonical_message_url(
                    workspace,
                    str(row["channel_id"]),
                    str(row["ts"]),
                    thread_ts=str(row["thread_ts"] or ""),
                ),
                "text": text,
                "textResolved": resolved,
                "user": {
                    "id": user_id,
                    "displayName": display_name,
                },
            }
        )
    return messages


def title_from_messages(messages: list[dict[str, Any]], channel_name: str) -> str:
    if not messages:
        return f"Slack Thread: #{channel_name or 'unknown-channel'}"
    seed = messages[0]["textResolved"] or messages[0]["text"]
    seed = re.sub(r"\s+", " ", str(seed)).strip()
    if not seed:
        seed = f"Thread in #{channel_name or 'unknown-channel'}"
    if len(seed) > 96:
        seed = seed[:93] + "..."
    return f"Slack Thread: {seed}"


def build_threads(
    *,
    workspace: str,
    channel: dict[str, Any],
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        key = str(message["threadTs"] or message["ts"])
        grouped.setdefault(key, []).append(message)
    threads: list[dict[str, Any]] = []
    for thread_ts, items in grouped.items():
        ordered = sorted(
            items,
            key=lambda item: int(str(item["ts"]).replace(".", "")),
        )
        channel_id = str(channel.get("id") or ordered[0].get("channelId") or "").strip()
        participants = sorted(
            {
                str(item["user"]["displayName"])
                for item in ordered
                if str(item["user"]["displayName"]).strip()
            }
        )
        threads.append(
            with_visibility_metadata(
                {
                    "threadTs": thread_ts,
                    "rootTs": ordered[0]["ts"],
                    "latestTs": ordered[-1]["ts"],
                    "permalink": canonical_thread_url(
                        workspace,
                        channel_id,
                        thread_ts,
                    ),
                    "title": title_from_messages(
                        ordered,
                        str(channel.get("name") or ""),
                    ),
                    "messageCount": len(ordered),
                    "participantCount": len(participants),
                    "participants": participants,
                    "messages": ordered,
                    "channel": channel,
                },
                provider="slack",
                locator=canonical_thread_url(
                    workspace,
                    channel_id,
                    thread_ts,
                ),
            )
        )
    threads.sort(
        key=lambda item: int(str(item["latestTs"]).replace(".", "")),
        reverse=True,
    )
    return threads


def build_envelope(
    conn: sqlite3.Connection,
    ref: SlackRef,
    *,
    window: ChannelWindow | None = None,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = load_message_rows(conn, ref, window=window)
    if not rows:
        if ref.kind == "thread":
            permalink = ref.url or canonical_thread_url(
                ref.workspace, ref.channel_id, ref.thread_ts or ""
            )
            raise ToolError(
                f"exact Slack thread target is absent from the hydrated bounded archive window for "
                f"{ref.raw}. The native read path recognized the permalink, materialized the "
                "centered six-week thread window, and then found that the exact target message is "
                "not present in that archive. This usually means a coverage hole, a deleted or "
                "inaccessible message, or a message/timestamp mismatch. Retry with "
                f"`gotta slack get {shlex.quote(permalink)} --refresh` if you want to rebuild that "
                "same bounded archive window."
            )
        if ref.kind == "channel" and window and window.enabled:
            requested = []
            if coverage and coverage.get("since"):
                requested.append(f"since {coverage['since']}")
            if coverage and coverage.get("until"):
                requested.append(f"until {coverage['until']}")
            detail = (
                f" in the requested window ({', '.join(requested)})"
                if requested
                else ""
            )
            raise ToolError(f"no messages found for {ref.raw}{detail}.")
        raise ToolError(
            f"no messages found for {ref.raw}. if you expected older history, run "
            f"'gotta slack sync {ref.channel_id} --lookback 3w' or retry with "
            "'--pull-recent 3w'."
        )
    user_ids = {
        str(parse_json_blob(row["data"]).get("user") or "").strip()
        for row in rows
        if str(parse_json_blob(row["data"]).get("user") or "").strip()
    }
    channel_mentions = {
        match.group(1)
        for row in rows
        for match in re.finditer(
            r"<#([A-Z0-9]+)(?:\|[^>]+)?>",
            str(row["txt"] or parse_json_blob(row["data"]).get("text") or ""),
        )
    }
    archive_users = latest_user_rows(conn, user_ids)
    directory_users = ensure_user_directory_entries(ref.workspace, user_ids)
    users = merge_users(archive_users, directory_users)
    archive_channel = normalize_channel(latest_channel_row(conn, ref.channel_id))
    directory_channels = ensure_channel_directory_entries(
        ref.workspace,
        channel_mentions | {ref.channel_id},
    )
    channel = merge_channels(
        {ref.channel_id: archive_channel},
        directory_channels,
    ).get(ref.channel_id, archive_channel)
    channels = merge_channels({ref.channel_id: channel}, directory_channels)
    messages = normalize_messages(
        rows,
        workspace=ref.workspace,
        users=users,
        channels=channels,
    )
    threads = build_threads(
        workspace=ref.workspace,
        channel=channel,
        messages=messages,
    )
    title = title_from_messages(messages, str(channel.get("name") or ""))
    envelope = with_visibility_metadata(
        {
            "workspace": ref.workspace,
            "ref": {
                "kind": ref.kind,
                "channelId": ref.channel_id,
                "threadTs": ref.thread_ts,
                "url": ref.url
                or canonical_thread_url(ref.workspace, ref.channel_id, ref.thread_ts),
            },
            "channel": channel,
            "title": title,
            "messageCount": len(messages),
            "threadCount": len(threads),
            "threads": threads,
            "messages": messages,
        },
        provider="slack",
        locator=ref.url
        or canonical_thread_url(ref.workspace, ref.channel_id, ref.thread_ts or ""),
    )
    if coverage is not None:
        envelope["window"] = coverage
    return envelope


def envelope_meta(envelope: dict[str, Any]) -> dict[str, Any]:
    messages = envelope.get("messages") or []
    first_ts = messages[0]["ts"] if messages else None
    last_ts = messages[-1]["ts"] if messages else None
    meta = {
        "workspace": envelope["workspace"],
        "ref": envelope["ref"],
        "channel": envelope["channel"],
        "title": envelope["title"],
        "messageCount": envelope["messageCount"],
        "threadCount": envelope.get("threadCount", 0),
        "firstTs": first_ts,
        "lastTs": last_ts,
        "retrieval": envelope_retrieval(envelope),
        "fidelity": envelope_fidelity(envelope),
    }
    meta.update(
        {key: value for key, value in envelope.items() if key.startswith("visibility_")}
    )
    if "window" in envelope:
        meta["window"] = envelope["window"]
    return meta


def envelope_retrieval(envelope: dict[str, Any]) -> dict[str, str]:
    payload = {"state": "materialized"}
    window = envelope.get("window") or {}
    if window.get("complete") is True:
        payload["coverage"] = "complete"
    elif window.get("complete") is False:
        payload["coverage"] = "gap"
    return payload


def envelope_fidelity(envelope: dict[str, Any]) -> dict[str, str]:
    ref_value = envelope.get("ref") or {}
    ref = ref_value if isinstance(ref_value, dict) else {}
    kind = str(ref.get("kind") or "").strip()
    if kind == "thread":
        return {
            "mode": "full",
            "detail": "full thread render from the hydrated bounded archive window",
        }
    window = envelope.get("window") or {}
    if window.get("since") or window.get("until"):
        coverage = (
            "complete"
            if window.get("complete") is True
            else "partial"
            if window.get("complete") is False
            else "unknown"
        )
        return {
            "mode": "bounded",
            "detail": f"bounded channel render over the requested archive window ({coverage} coverage)",
        }
    return {
        "mode": "bounded",
        "detail": "bounded channel render from locally cached archive coverage",
    }


def display_channel_label(envelope: dict[str, Any]) -> str:
    channel = envelope.get("channel") or {}
    name = str(channel.get("name") or "").strip()
    if name:
        return f"#{name}"
    channel_id = str(
        channel.get("id") or envelope.get("ref", {}).get("channelId") or ""
    ).strip()
    if channel_id:
        return f"#{channel_id}"
    workspace = str(envelope.get("workspace") or "").strip()
    if workspace:
        return f"#{workspace}:unknown-channel"
    return "#unknown-channel"


def render_markdown(envelope: dict[str, Any]) -> str:
    messages = envelope.get("messages") or []
    retrieval = envelope_retrieval(envelope)
    fidelity = envelope_fidelity(envelope)
    first_ts = str(messages[0].get("ts") or "") if messages else ""
    last_ts = str(messages[-1].get("ts") or "") if messages else ""
    try:
        created = format_archive_bound(float(first_ts)) if first_ts else ""
    except ValueError:
        created = ""
    try:
        updated = format_archive_bound(float(last_ts)) if last_ts else ""
    except ValueError:
        updated = ""
    channel_label = display_channel_label(envelope)
    if envelope["ref"]["kind"] == "channel":
        window = envelope.get("window") or {}
        lines = [
            f"### Slack Channel: {channel_label}",
            "",
            f"- _Source_: {envelope['ref']['url']}",
            f"- _Retrieval_: `{retrieval['state']}`",
            f"- _Fidelity_: `{fidelity['mode']}` ({fidelity['detail']})",
        ]
        lines.extend(render_visibility_metadata_lines(envelope))
        if created:
            lines.append(f"- Created: {created}")
        if updated:
            lines.append(f"- Updated: {updated}")
        lines.extend(
            [
                f"- _Threads_: {envelope.get('threadCount', 0)}",
                f"- _Messages_: {envelope['messageCount']}",
            ]
        )
        if window.get("since") or window.get("until"):
            lines.append(
                f"- _Window_: `{window.get('since') or '-inf'}` -> `{window.get('until') or '+inf'}`"
            )
            if window.get("complete") is not None:
                lines.append(
                    f"- _Coverage_: `{'complete' if window['complete'] else 'partial'}`"
                )
        lines.append("")
        for thread in envelope.get("threads", []):
            lines.extend(
                [
                    f"#### {thread['title']}",
                    "",
                    f"- _Thread_: {thread['permalink']}",
                    f"- _Messages_: {thread['messageCount']}",
                    "",
                ]
            )
            lines[-1:-1] = render_visibility_metadata_lines(thread)
            for message in thread["messages"]:
                stamp = format_local_ts(message["ts"])
                rendered = single_line_text(message["textResolved"])
                lines.append(
                    f"- _{stamp}_ **{message['user']['displayName']}**: {rendered}"
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
    lines = [
        f"### {envelope['title']}",
        "",
        f"- _Channel_: `{channel_label}`",
        f"- _Source_: {envelope['ref']['url']}",
        f"- _Retrieval_: `{retrieval['state']}`",
        f"- _Fidelity_: `{fidelity['mode']}` ({fidelity['detail']})",
        "",
    ]
    lines[-1:-1] = render_visibility_metadata_lines(envelope)
    if created:
        lines.insert(4, f"- Created: {created}")
    if updated:
        lines.insert(5 if created else 4, f"- Updated: {updated}")
    for message in envelope["messages"]:
        stamp = format_local_ts(message["ts"])
        rendered = single_line_text(message["textResolved"])
        lines.append(f"- _{stamp}_ **{message['user']['displayName']}**: {rendered}")
    return "\n".join(lines) + "\n"


def render_text(envelope: dict[str, Any]) -> str:
    retrieval = envelope_retrieval(envelope)
    fidelity = envelope_fidelity(envelope)
    if envelope["ref"]["kind"] == "channel":
        window = envelope.get("window") or {}
        lines = [
            f"# {envelope['channel'].get('name') or envelope['channel'].get('id')}",
            f"threads={envelope.get('threadCount', 0)} messages={envelope['messageCount']}",
            f"retrieval={retrieval['state']}",
            f"fidelity={fidelity['mode']} detail={fidelity['detail']}",
        ]
        if window.get("since") or window.get("until"):
            lines.append(
                f"window={window.get('since') or '-inf'}..{window.get('until') or '+inf'} "
                f"coverage={'complete' if window.get('complete') else 'partial'}"
            )
        lines.append("")
        for thread in envelope.get("threads", []):
            lines.append(
                f"[thread {format_local_ts(thread['latestTs'])}] {thread['title']}"
            )
            lines.append(thread["permalink"])
            for message in thread["messages"]:
                stamp = format_local_ts(message["ts"])
                rendered = single_line_text(message["textResolved"])
                lines.append(
                    f"  [{stamp}] {message['user']['displayName']}: {rendered}"
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
    lines: list[str] = [
        f"retrieval={retrieval['state']}",
        f"fidelity={fidelity['mode']} detail={fidelity['detail']}",
        "",
    ]
    for message in envelope["messages"]:
        stamp = format_local_ts(message["ts"])
        rendered = single_line_text(message["textResolved"])
        lines.append(f"[{stamp}] {message['user']['displayName']}: {rendered}")
    return "\n".join(lines) + ("\n" if lines else "")


def load_workspace_info(conn: sqlite3.Connection) -> dict[str, Any]:
    if not sqlite_table_exists(conn, "workspace"):
        return {}
    row = conn.execute(
        """
        SELECT team, team_id, url
        FROM workspace
        ORDER BY chunk_id DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return {}
    return {
        "team": row["team"],
        "teamId": row["team_id"],
        "url": row["url"],
    }


def load_sync_summary(
    conn: sqlite3.Connection,
    *,
    workspace: str,
    channel_id: str,
) -> dict[str, Any]:
    archive_channel = normalize_channel(latest_channel_row(conn, channel_id))
    directory_channels = ensure_channel_directory_entries(workspace, {channel_id})
    channel = merge_channels(
        {channel_id: archive_channel},
        directory_channels,
    ).get(channel_id, archive_channel)
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS message_count,
            COUNT(DISTINCT COALESCE(thread_ts, ts)) AS thread_count,
            MIN(ts) AS first_ts,
            MAX(ts) AS last_ts
        FROM (
            SELECT ts, thread_ts
            FROM (
                SELECT
                    ts,
                    thread_ts,
                    ROW_NUMBER() OVER (
                        PARTITION BY channel_id, ts
                        ORDER BY chunk_id DESC, idx DESC
                    ) AS rn
                FROM message
                WHERE channel_id = ?
            )
            WHERE rn = 1
        )
        """,
        (channel_id,),
    ).fetchone()
    return {
        "channel": channel,
        "messageCount": int(row["message_count"] or 0),
        "threadCount": int(row["thread_count"] or 0),
        "firstTs": row["first_ts"],
        "lastTs": row["last_ts"],
    }


def sync_result(
    result: ArchiveResult,
    ref: SlackRef,
    lookback: str | None = None,
) -> dict[str, Any]:
    conn = open_db(result.db_path)
    try:
        workspace = load_workspace_info(conn)
        summary = load_sync_summary(
            conn,
            workspace=ref.workspace,
            channel_id=ref.channel_id,
        )
        user_count = conn.execute("SELECT COUNT(DISTINCT id) FROM s_user").fetchone()[0]
        lookback_token = None
        if lookback:
            lookback_token, _ = parse_lookback(lookback)
    finally:
        conn.close()
    return {
        "workspace": ref.workspace,
        "channelId": ref.channel_id,
        "channel": summary["channel"],
        "lookback": lookback_token,
        "archivePath": str(result.db_path),
        "logPath": str(result.log_path),
        "messageCount": summary["messageCount"],
        "threadCount": summary["threadCount"],
        "firstTs": summary["firstTs"],
        "lastTs": summary["lastTs"],
        "userCount": int(user_count),
        "workspaceInfo": workspace,
    }


def search_follow_command(permalink: str) -> str:
    query_thread_ts = thread_ts_from_query(permalink)
    if query_thread_ts:
        match = PERMALINK_RE.search(permalink) or ARCHIVE_PATH_RE.search(permalink)
        if match:
            workspace = (
                match.groupdict().get("workspace")
                or urllib.parse.urlparse(permalink).netloc.split(".", 1)[0]
            )
            permalink = canonical_thread_url(
                workspace, match.group("channel"), query_thread_ts
            )
    return f"gotta read {shlex.quote(permalink)}"


def build_search_match_sql(
    spec: SearchSpec,
) -> tuple[str, list[str], list[dict[str, str]]]:
    if not spec.terms and not spec.modifiers:
        raise ToolError("missing Slack search query")
    if not spec.terms:
        return ("1 = 1", [], [])
    if spec.match_mode == "literal":
        return (
            "LOWER(COALESCE(txt, '')) LIKE LOWER(?) ESCAPE '\\'",
            [f"%{escape_like(spec.raw_query.strip())}%"],
            [],
        )
    operator = " AND " if spec.match_mode == "all" else " OR "
    clause = operator.join(
        "LOWER(COALESCE(txt, '')) LIKE LOWER(?) ESCAPE '\\'" for _ in spec.terms
    )
    params = [f"%{escape_like(term)}%" for term in spec.terms]
    suggestions: list[dict[str, str]] = []
    if spec.is_multi_term:
        suggestions.append(
            {
                "kind": "match",
                "label": "literal phrase",
                "command": f"gotta slack search {shlex.quote(spec.raw_query)} --match literal",
            }
        )
        if spec.match_mode == "all":
            suggestions.append(
                {
                    "kind": "match",
                    "label": "any term",
                    "command": f"gotta slack search {shlex.quote(spec.raw_query)} --match any",
                }
            )
        for term in spec.terms:
            suffix = f" {' '.join(spec.modifiers)}" if spec.modifiers else ""
            suggestions.append(
                {
                    "kind": "term",
                    "label": term,
                    "command": f"gotta slack search {shlex.quote(term + suffix)}",
                }
            )
    return clause, params, suggestions


def query_search(
    conn: sqlite3.Connection,
    *,
    query: str,
    limit: int,
    workspace: str,
    channel_id: str | None = None,
    match_mode: str = "all",
    source: str = "local-archive",
    source_detail: str | None = None,
) -> dict[str, Any]:
    spec = search_spec(query, match_mode=match_mode)
    inner_where_parts: list[str] = []
    inner_params: list[Any] = []
    if channel_id:
        inner_where_parts.append("channel_id = ?")
        inner_params.append(channel_id)
    inner_where = ""
    if inner_where_parts:
        inner_where = "WHERE " + " AND ".join(inner_where_parts)
    match_sql, match_params, suggestions = build_search_match_sql(spec)
    time_sql_parts, time_params, applied_modifiers = _archive_search_time_predicates(
        spec
    )
    where_parts = [f"({match_sql})", *time_sql_parts]
    where_sql = " AND ".join(where_parts)
    rows = conn.execute(
        f"""
        SELECT channel_id, ts, thread_ts, txt, data
        FROM (
          SELECT
            channel_id,
            ts,
            thread_ts,
            txt,
            data,
            ROW_NUMBER() OVER (
              PARTITION BY channel_id, ts
              ORDER BY chunk_id DESC, idx DESC
            ) AS rn
          FROM message
          {inner_where}
        )
        WHERE rn = 1
          AND {where_sql}
        ORDER BY CAST(REPLACE(ts, '.', '') AS INTEGER) DESC
        LIMIT ?
        """,
        (*inner_params, *match_params, *time_params, limit),
    ).fetchall()
    user_ids = {
        str(parse_json_blob(row["data"]).get("user") or "").strip()
        for row in rows
        if str(parse_json_blob(row["data"]).get("user") or "").strip()
    }
    result_channel_ids = {
        str(row["channel_id"]) for row in rows if str(row["channel_id"]).strip()
    }
    channel_mentions = {
        match.group(1)
        for row in rows
        for match in re.finditer(
            r"<#([A-Z0-9]+)(?:\|[^>]+)?>",
            str(row["txt"] or parse_json_blob(row["data"]).get("text") or ""),
        )
    }
    users = merge_users(
        latest_user_rows(conn, user_ids),
        ensure_user_directory_entries(workspace, user_ids),
    )
    directory_channels = ensure_channel_directory_entries(
        workspace,
        result_channel_ids | channel_mentions,
    )
    archive_channels = {
        current_channel_id: normalize_channel(
            latest_channel_row(conn, current_channel_id)
        )
        for current_channel_id in result_channel_ids
    }
    channels = merge_channels(archive_channels, directory_channels)
    search_channel = None
    if channel_id:
        archive_channel = normalize_channel(latest_channel_row(conn, channel_id))
        search_channel = merge_channels(
            {channel_id: archive_channel},
            directory_channels,
        ).get(channel_id, archive_channel)
    results: list[dict[str, Any]] = []
    for row in rows:
        data = parse_json_blob(row["data"])
        user_id = str(data.get("user") or "").strip()
        text = str(row["txt"] or data.get("text") or "")
        result_channel_id = str(row["channel_id"])
        result_channel = channels.get(result_channel_id, {})
        thread_ts = str(row["thread_ts"] or row["ts"])
        permalink = canonical_message_url(
            workspace,
            result_channel_id,
            str(row["ts"]),
            thread_ts=thread_ts,
        )
        results.append(
            with_visibility_metadata(
                {
                    "channelId": result_channel_id,
                    "channelName": result_channel.get("name"),
                    "ts": str(row["ts"]),
                    "threadTs": thread_ts,
                    "threadPermalink": canonical_thread_url(
                        workspace, result_channel_id, thread_ts
                    ),
                    "permalink": permalink,
                    "followCommand": search_follow_command(permalink),
                    "text": text,
                    "textResolved": resolve_mentions(text, users, channels),
                    "userId": user_id,
                    "userDisplayName": message_display_name(
                        data, users.get(user_id), user_id
                    ),
                    "channel": result_channel,
                },
                provider="slack",
                locator=permalink,
            )
        )
    threads: dict[str, dict[str, Any]] = {}
    for item in results:
        thread_key = f"{item['channelId']}:{item['threadTs']}"
        thread = threads.get(thread_key)
        if thread is None:
            title_seed = single_line_text(str(item["textResolved"] or item["text"]))
            if len(title_seed) > 96:
                title_seed = title_seed[:93] + "..."
            thread_permalink = item["threadPermalink"]
            threads[thread_key] = with_visibility_metadata(
                {
                    "threadTs": item["threadTs"],
                    "channelId": item["channelId"],
                    "channelName": item["channelName"],
                    "permalink": thread_permalink,
                    "followCommand": search_follow_command(thread_permalink),
                    "latestTs": item["ts"],
                    "matchCount": 1,
                    "title": title_seed
                    or f"Thread in #{item['channelName'] or item['channelId']}",
                    "results": [item],
                    "channel": item.get("channel") or {},
                },
                provider="slack",
                locator=thread_permalink,
            )
            continue
        thread["matchCount"] += 1
        thread["results"].append(item)
        if int(str(item["ts"]).replace(".", "")) > int(
            str(thread["latestTs"]).replace(".", "")
        ):
            thread["latestTs"] = item["ts"]
    thread_results = sorted(
        threads.values(),
        key=lambda item: int(str(item["latestTs"]).replace(".", "")),
        reverse=True,
    )
    matched_channels = sorted(
        [
            channel
            for channel_key, channel in channels.items()
            if channel_key in result_channel_ids
        ],
        key=lambda item: (
            str(item.get("name") or item.get("id") or "").lower(),
            str(item.get("id") or ""),
        ),
    )
    payload = with_visibility_metadata(
        {
            "workspace": workspace,
            "query": query,
            "terms": spec.terms,
            "modifiers": spec.modifiers,
            "matchMode": spec.match_mode,
            "scope": "channel" if channel_id else "workspace",
            "source": source,
            "channel": search_channel,
            "channelCount": len(result_channel_ids),
            "channels": matched_channels,
            "resultCount": len(results),
            "threadCount": len(thread_results),
            "threads": thread_results,
            "results": results,
        },
        provider="slack",
    )
    if applied_modifiers:
        payload["appliedModifiers"] = applied_modifiers
    if source_detail:
        payload["sourceDetail"] = source_detail
    if results:
        return payload
    if suggestions:
        payload["suggestions"] = suggestions
    return payload


def search_archive_payload(
    *,
    result: ArchiveResult,
    query: str,
    limit: int,
    workspace: str,
    channel_id: str | None = None,
    match_mode: str = "all",
) -> dict[str, Any]:
    conn = open_db(result.db_path)
    try:
        return query_search(
            conn,
            query=query,
            limit=limit,
            workspace=workspace,
            channel_id=channel_id,
            match_mode=match_mode,
            source="local-archive",
        )
    finally:
        conn.close()


def _normalize_live_channel(channel: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(channel.get("id") or "").strip()
    normalized = {
        "id": channel_id,
        "name": str(channel.get("name") or channel_id).strip() or channel_id,
        "type": channel_type(channel, channel_id),
        "isPrivate": bool(channel.get("is_private", False)),
        "isShared": bool(channel.get("is_shared", False)),
        "isExtShared": bool(channel.get("is_ext_shared", False)),
        "isArchived": bool(channel.get("is_archived", False)),
    }
    normalized["is_private"] = normalized["isPrivate"]
    normalized["is_shared"] = normalized["isShared"]
    normalized["is_ext_shared"] = normalized["isExtShared"]
    normalized["is_archived"] = normalized["isArchived"]
    return with_visibility_metadata(normalized, provider="slack")


def _live_search_queries(spec: SearchSpec) -> list[str]:
    if spec.match_mode == "literal":
        raw = spec.raw_query.strip()
        if not raw:
            return [raw]
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            return [raw]
        escaped = raw.replace('"', '\\"')
        return [f'"{escaped}"']
    if spec.match_mode == "any":
        if not spec.terms:
            return [spec.raw_query.strip()]
        suffix = f" {' '.join(spec.modifiers)}" if spec.modifiers else ""
        return [f"{term}{suffix}" for term in spec.terms]
    raw = spec.raw_query.strip()
    return [raw]


def _live_search_page_size(limit: int, *, channel_id: str | None) -> int:
    baseline = max(limit, 20)
    if channel_id:
        baseline = max(baseline * 3, 50)
    return min(baseline, 100)


def _live_search_channel(
    workspace: str,
    channel_id: str | None,
) -> dict[str, Any] | None:
    if not channel_id:
        return None
    channels = ensure_channel_directory_entries(workspace, {channel_id})
    if channel_id in channels:
        return channels[channel_id]
    return with_visibility_metadata(
        {
            "id": channel_id,
            "name": channel_id,
            "type": "unknown",
            "isPrivate": False,
            "isArchived": False,
            "isShared": False,
            "isExtShared": False,
            "is_private": False,
            "is_archived": False,
            "is_shared": False,
            "is_ext_shared": False,
        },
        provider="slack",
    )


def _normalize_live_search_match(
    workspace: str,
    raw: dict[str, Any],
) -> dict[str, Any]:
    channel = _normalize_live_channel(
        raw.get("channel") if isinstance(raw.get("channel"), dict) else {}
    )
    channel_id = str(channel.get("id") or "").strip()
    ts = str(raw.get("ts") or "").strip()
    thread_ts = str(raw.get("thread_ts") or "").strip()
    permalink = str(raw.get("permalink") or "").strip()
    if not thread_ts and permalink:
        thread_ts = thread_ts_from_query(permalink)
    if not thread_ts:
        thread_ts = ts
    if not permalink and channel_id and ts:
        permalink = canonical_message_url(
            workspace,
            channel_id,
            ts,
            thread_ts=thread_ts,
        )
    thread_permalink = permalink
    if channel_id and thread_ts:
        thread_permalink = canonical_thread_url(workspace, channel_id, thread_ts)
    text = str(raw.get("text") or "").strip()
    username = str(raw.get("username") or raw.get("user") or "").strip() or "unknown"
    return with_visibility_metadata(
        {
            "channelId": channel_id,
            "channelName": channel.get("name"),
            "ts": ts,
            "threadTs": thread_ts,
            "threadPermalink": thread_permalink,
            "permalink": permalink,
            "followCommand": search_follow_command(thread_permalink or permalink),
            "text": text,
            "textResolved": text,
            "userId": str(raw.get("user") or "").strip(),
            "userDisplayName": username,
            "channel": channel,
        },
        provider="slack",
        locator=thread_permalink or permalink,
    )


def _live_search_messages(
    *,
    workspace: str,
    auth_state: dict[str, Any],
    query: str,
    limit: int,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    payload = slack_api_post(
        workspace,
        auth_state,
        "search.messages",
        data={
            "query": query,
            "count": str(limit),
        },
        timeout_seconds=timeout_seconds,
    )
    messages = payload.get("messages")
    if not isinstance(messages, dict):
        raise ToolError("invalid Slack live search response: missing messages payload")
    matches = messages.get("matches")
    if not isinstance(matches, list):
        raise ToolError("invalid Slack live search response: missing match list")
    return [match for match in matches if isinstance(match, dict)]


def search_live_payload(
    *,
    workspace: str,
    query: str,
    limit: int,
    channel_id: str | None = None,
    match_mode: str = "all",
    timeout_seconds: int = DEFAULT_LIVE_SEARCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    auth_state, _ = ensure_live_search_auth(
        workspace,
        interactive_ok=is_interactive(),
    )
    spec = search_spec(query, match_mode=match_mode)
    results_by_key: dict[str, dict[str, Any]] = {}
    page_size = _live_search_page_size(limit, channel_id=channel_id)
    for wire_query in _live_search_queries(spec):
        for raw_match in _live_search_messages(
            workspace=workspace,
            auth_state=auth_state,
            query=wire_query,
            limit=page_size,
            timeout_seconds=timeout_seconds,
        ):
            item = _normalize_live_search_match(workspace, raw_match)
            if channel_id and item["channelId"] != channel_id:
                continue
            if not item["channelId"] or not item["ts"] or not item["permalink"]:
                continue
            results_by_key.setdefault(f"{item['channelId']}:{item['ts']}", item)
    results = list(results_by_key.values())[:limit]
    threads: list[dict[str, Any]] = []
    for item in results:
        summary = single_line_text(str(item["textResolved"] or item["text"]))
        if len(summary) > 96:
            summary = summary[:93] + "..."
        threads.append(
            with_visibility_metadata(
                {
                    "threadTs": item["threadTs"],
                    "channelId": item["channelId"],
                    "channelName": item["channelName"],
                    "permalink": item["threadPermalink"],
                    "followCommand": search_follow_command(item["threadPermalink"]),
                    "latestTs": item["ts"],
                    "matchCount": 1,
                    "title": summary
                    or f"Thread in #{item['channelName'] or item['channelId']}",
                    "results": [item],
                    "channel": item.get("channel") or {},
                },
                provider="slack",
                locator=str(item.get("threadPermalink") or ""),
            )
        )
    matched_channels = sorted(
        {
            (
                item["channelId"],
                json.dumps(item["channel"], sort_keys=True),
            )
            for item in results
        },
        key=lambda entry: entry[0],
    )
    channel_records = [json.loads(channel_json) for _, channel_json in matched_channels]
    payload = with_visibility_metadata(
        {
            "workspace": workspace,
            "query": query,
            "terms": spec.terms,
            "modifiers": spec.modifiers,
            "matchMode": spec.match_mode,
            "scope": "channel" if channel_id else "workspace",
            "source": "live-search",
            "sourceDetail": "native Slack search API",
            "channel": _live_search_channel(workspace, channel_id),
            "channelCount": len(channel_records),
            "channels": channel_records,
            "resultCount": len(results),
            "threadCount": len(threads),
            "threads": threads,
            "results": results,
        },
        provider="slack",
    )
    if spec.modifiers:
        payload["appliedModifiers"] = list(spec.modifiers)
    if results:
        return payload
    _, _, suggestions = build_search_match_sql(spec)
    if suggestions:
        payload["suggestions"] = suggestions
    return payload


def render_search_source(result: dict[str, Any]) -> str:
    source = str(result.get("source") or "local-archive")
    if source == "live-search":
        return "live Slack search"
    return "local archive"


def render_search_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"### Slack Search: {result['query']}",
        "",
        f"- _Scope_: `{result.get('scope') or 'channel'}`",
        f"- _Source_: {render_search_source(result)}",
        f"- _Match_: `{result.get('matchMode') or 'all'}`",
        f"- _Matches_: {result['resultCount']}",
        f"- _Matching Threads_: {result.get('threadCount', 0)}",
    ]
    if result.get("modifiers"):
        lines.insert(5, f"- _Modifiers_: `{', '.join(result['modifiers'])}`")
    if result.get("appliedModifiers"):
        lines.insert(
            6, f"- _Applied Filters_: `{', '.join(result['appliedModifiers'])}`"
        )
    lines.extend(render_visibility_metadata_lines(result))
    lines.extend(
        render_source_metadata_lines(derive_source_metadata_from_payload(result))
    )
    lines.append("")
    channel = result.get("channel")
    if isinstance(channel, dict) and channel:
        lines.insert(3, f"- _Channel_: `#{channel.get('name') or channel.get('id')}`")
    elif result.get("scope") == "workspace":
        lines.insert(3, f"- _Matching Channels_: {result.get('channelCount', 0)}")
    if result.get("sourceDetail"):
        lines.insert(-1, f"- _Detail_: {result['sourceDetail']}")
    if result.get("archiveStatus"):
        lines.insert(-1, f"- _Archive_: {result['archiveStatus']['detail']}")
    if result.get("suggestions"):
        lines.extend(["#### Next Likely Searches", ""])
        for suggestion in result["suggestions"]:
            lines.append(f"- `{suggestion['command']}`")
        lines.append("")
    for thread in result.get("threads", []):
        lines.extend(
            [
                f"#### {thread['title']}",
                "",
                f"- _Thread_: {thread['permalink']}",
                f"- _Channel_: `#{thread.get('channelName') or thread.get('channelId')}`",
                f"- _Matches_: {thread['matchCount']}",
                "",
            ]
        )
        lines[-1:-1] = render_visibility_metadata_lines(thread)
        for item in thread["results"]:
            lines.append(
                f"- _{format_local_ts(item['ts'])}_ **{item['userDisplayName']}**: "
                f"{single_line_text(item['textResolved'])} ({item['permalink']})"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def render_search_titles(result: dict[str, Any]) -> str:
    lines: list[str] = []
    if result.get("sourceDetail"):
        lines.append(f"# {result['sourceDetail']}")
    if result.get("archiveStatus"):
        lines.append(f"# {result['archiveStatus']['detail']}")
    for item in result["results"]:
        summary = single_line_text(str(item["textResolved"] or item["text"]))
        if len(summary) > 120:
            summary = summary[:117] + "..."
        lines.append(
            f"{item['channelName']} {item['ts']} {item['userDisplayName']}: {summary} ({item['permalink']})"
        )
    for suggestion in result.get("suggestions", []):
        lines.append(f"next\t{suggestion['command']}")
    return "\n".join(lines) + ("\n" if lines else "")


def render_search_links(result: dict[str, Any]) -> str:
    links = "".join(
        str(item.get("permalink") or "") + "\n"
        for item in result.get("results", [])
        if str(item.get("permalink") or "")
    )
    if links:
        return links
    return "".join(
        str(suggestion.get("command") or "") + "\n"
        for suggestion in result.get("suggestions", [])
        if str(suggestion.get("command") or "")
    )


def explicit_provider_list(
    *,
    workspace: str,
    entity: str,
    fmt: str,
    channel_types: str | None = None,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "slackdump",
        "list",
        entity,
        "-enterprise",
        "-workspace",
        workspace,
        "-no-json",
    ]
    if fmt == "json":
        cmd.extend(["-format", "JSON"])
    else:
        cmd.extend(["-format", "Text"])
    if entity == "channels":
        if channel_types:
            cmd.extend(["-chan-types", channel_types])
    return run_command(cmd, timeout_seconds=timeout_seconds)


def load_envelope_from_archive(
    result: ArchiveResult,
    ref: SlackRef,
    *,
    window: ChannelWindow | None = None,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conn = open_db(result.db_path)
    try:
        return build_envelope(conn, ref, window=window, coverage=coverage)
    finally:
        conn.close()


def maybe_load_from_cache(
    ref: SlackRef,
    *,
    window: ChannelWindow | None = None,
) -> dict[str, Any] | None:
    result = existing_channel_cache(ref.workspace, ref.channel_id)
    if result is None:
        return None
    coverage = (
        channel_window_coverage(result, ref.channel_id, window)
        if ref.kind == "channel" and window and window.enabled
        else None
    )
    if window and window.strict and coverage and not coverage["complete"]:
        raise ToolError(
            f"requested window is not fully cached for {ref.channel_id}. run "
            f"'gotta slack sync {ref.channel_id} --lookback {window.lookback or DEFAULT_RECENT_LOOKBACK}'."
        )
    try:
        return load_envelope_from_archive(result, ref, window=window, coverage=coverage)
    except ToolError:
        if coverage and coverage["complete"]:
            raise
        return None


def cmd_auth(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.workspace)
    if not workspace:
        raise ToolError(missing_workspace_message())
    ensure_workspace_auth(workspace, interactive_ok=True)
    auth_state = export_slack_auth_from_slackdump(workspace)
    auth_path = persist_slack_auth_state(workspace, auth_state)
    slack_auth_test(workspace, auth_state)
    print_json(
        {
            "authenticated": True,
            "auth_file": str(auth_path),
            "workspace": workspace,
            "known_workspaces": known_workspaces(),
        }
    )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    ensure_workspace_auth(args.workspace, interactive_ok=True)
    ref = resolve_slack_ref(args.target, workspace=args.workspace)
    ensure_workspace_auth(ref.workspace, interactive_ok=True)
    if args.lookback and args.since:
        raise ToolError("`--lookback` cannot be combined with `--since`")
    if args.until and not args.since:
        raise ToolError("`--until` requires `--since` for Slack sync")
    if args.since:
        since = parse_time_arg(args.since, flag="--since")
        until = parse_time_arg(args.until, flag="--until") if args.until else utc_now()
        if since > until:
            raise ToolError("`--since` must be earlier than or equal to `--until`")
        result = _run_bounded_archive_window(
            ref,
            since=since,
            until=until,
            refresh=args.refresh,
        )
        print_json(
            {
                **sync_result(result, ref, None),
                "window": {
                    "since": format_utc_iso(since),
                    "until": format_utc_iso(until),
                },
            }
        )
        return 0
    result = run_sync_archive(ref, lookback=args.lookback, refresh=args.refresh)
    print_json(sync_result(result, ref, args.lookback))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    ref = resolve_slack_ref(args.ref, workspace=args.workspace)
    if ref.kind == "doc":
        html_bytes, meta = _fetch_slack_doc(ref, interactive_ok=is_interactive())
        if args.output in {"json", "meta"}:
            print_json(meta)
        elif args.output == "messages":
            raise ToolError(
                "`gotta slack get ... --output messages` is only supported for channel and thread reads; "
                "Slack docs support markdown, text, json, or meta."
            )
        elif args.output == "markdown":
            sys.stdout.buffer.write(_render_doc_markdown(html_bytes))
        else:
            sys.stdout.buffer.write(html_text(html_bytes))
        return 0
    ensure_workspace_auth(ref.workspace, interactive_ok=is_interactive())
    window = resolve_channel_window(args)
    if ref.kind == "thread" and args.pull_recent:
        raise ToolError(
            "thread reads already hydrate a bounded six-week window centered on the "
            "thread timestamp; `--pull-recent` applies only to channel reads"
        )
    if args.refresh and not args.pull_recent and ref.kind != "thread":
        raise ToolError("`gotta slack get --refresh` requires `--pull-recent LOOKBACK`")
    if ref.kind == "thread" and (
        args.lookback or args.since or args.until or args.strict_window
    ):
        raise ToolError(
            "`--lookback`, `--since`, `--until`, and `--strict-window` apply only to channel reads"
        )

    envelope: dict[str, Any] | None = None
    result: ArchiveResult | None = None
    if ref.kind == "thread":
        since, until = thread_hydration_window(ref)
        emit_progress(
            f"retrieval state: queued slack thread {ref.channel_id}:{ref.thread_ts}"
        )
        emit_progress(
            "retrieval state: hydrating Slack thread through the native bounded archive window: "
            f"{ref.channel_id}:{ref.thread_ts} "
            f"(since={format_utc_iso(since)} until={format_utc_iso(until)}, "
            f"timeout={DEFAULT_THREAD_HYDRATION_TIMEOUT_SECONDS}s)"
        )

        def read_thread_attempt(
            *,
            refresh: bool,
        ) -> tuple[ArchiveResult, dict[str, Any]]:
            archive_result = _with_progress_heartbeat(
                (
                    f"retrieval state: hydrating slack thread {ref.channel_id}:{ref.thread_ts}"
                    + (" (refresh retry)" if refresh else "")
                ),
                lambda: ensure_thread_window_archive(
                    ref,
                    refresh=refresh,
                    timeout_seconds=DEFAULT_THREAD_HYDRATION_TIMEOUT_SECONDS,
                ),
                budget_seconds=DEFAULT_THREAD_HYDRATION_TIMEOUT_SECONDS,
            )
            emit_progress(
                "retrieval state: materialized bounded thread refresh; reading hydrated archive"
                if refresh
                else "retrieval state: materialized Slack thread archive; rendering cached archive"
            )
            return archive_result, load_envelope_from_archive(
                archive_result,
                ref,
                window=None,
                coverage=None,
            )

        try:
            result, envelope = read_thread_attempt(refresh=args.refresh)
        except ToolError as first_exc:
            exc: ToolError = first_exc
            if not args.refresh:
                emit_progress(
                    "retrieval state: hydrating slack thread retry with an explicit archive refresh "
                    f"(final attempt; up to {DEFAULT_THREAD_HYDRATION_TIMEOUT_SECONDS}s)"
                )
                try:
                    result, envelope = read_thread_attempt(refresh=True)
                except ToolError as retry_exc:
                    exc = retry_exc
            if envelope is None:
                permalink = ref.url or canonical_thread_url(
                    ref.workspace, ref.channel_id, ref.thread_ts or ""
                )
                if args.refresh:
                    emit_progress(
                        f"retrieval state: failed slack thread {ref.channel_id}:{ref.thread_ts}"
                    )
                    raise ToolError(
                        _thread_permalink_failure_message(
                            ref,
                            permalink=permalink,
                            since=since,
                            until=until,
                            detail=str(exc),
                            explicit_refresh=True,
                        )
                    ) from exc
            if envelope is None:
                permalink = ref.url or canonical_thread_url(
                    ref.workspace, ref.channel_id, ref.thread_ts or ""
                )
                emit_progress(
                    f"retrieval state: failed slack thread {ref.channel_id}:{ref.thread_ts}"
                )
                raise ToolError(
                    _thread_permalink_failure_message(
                        ref,
                        permalink=permalink,
                        since=since,
                        until=until,
                        detail=str(exc),
                        explicit_refresh=False,
                    )
                ) from exc
    elif args.pull_recent:
        result = run_sync_archive(ref, lookback=args.pull_recent, refresh=args.refresh)
        coverage = (
            channel_window_coverage(result, ref.channel_id, window)
            if ref.kind == "channel" and window.enabled
            else None
        )
        if window.strict and coverage and not coverage["complete"]:
            raise ToolError(
                f"requested window is not fully cached for {ref.channel_id}. run "
                f"'gotta slack sync {ref.channel_id} --lookback {window.lookback or args.pull_recent}'."
            )
        envelope = load_envelope_from_archive(
            result, ref, window=window, coverage=coverage
        )
    else:
        envelope = maybe_load_from_cache(ref, window=window)

        if envelope is None:
            if ref.kind == "channel":
                try:
                    result = try_opportunistic_sync(ref)
                except ToolError as exc:
                    raise ToolError(
                        "channel read could not complete from a local cache, and the bounded "
                        "opportunistic pull did not finish quickly. run "
                        f"'gotta slack sync {ref.channel_id} --lookback {DEFAULT_RECENT_LOOKBACK}' "
                        f"or 'gotta slack get {ref.channel_id} --pull-recent {DEFAULT_RECENT_LOOKBACK}'. "
                        f"channel URLs and bare channel refs require local archive coverage; "
                        f"detail: {exc}"
                    ) from exc
            else:
                raise ToolError(
                    "unsupported Slack get request for the resolved reference; use a thread permalink, "
                    "channel URL, channel ID, bare channel name, or CHANNEL:THREAD_TS."
                )
        if envelope is None:
            if result is None:
                raise ToolError(
                    "channel read has no cached archive coverage yet. run "
                    f"'gotta slack sync {ref.channel_id} --lookback {DEFAULT_RECENT_LOOKBACK}' "
                    f"or 'gotta slack get {ref.channel_id} --pull-recent {DEFAULT_RECENT_LOOKBACK}'."
                )
            coverage = (
                channel_window_coverage(result, ref.channel_id, window)
                if ref.kind == "channel" and window.enabled
                else None
            )
            if window.strict and coverage and not coverage["complete"]:
                raise ToolError(
                    f"requested window is not fully cached for {ref.channel_id}. run "
                    f"'gotta slack sync {ref.channel_id} --lookback {window.lookback or DEFAULT_RECENT_LOOKBACK}'."
                )
            envelope = load_envelope_from_archive(
                result, ref, window=window, coverage=coverage
            )

    envelope["retrieval"] = envelope_retrieval(envelope)
    envelope["fidelity"] = envelope_fidelity(envelope)
    if args.output == "json":
        print_json(envelope)
    elif args.output == "meta":
        print_json(envelope_meta(envelope))
    elif args.output == "messages":
        print_json(envelope["messages"])
    elif args.output == "markdown":
        sys.stdout.write(render_markdown(envelope))
    else:
        sys.stdout.write(render_text(envelope))
    return 0


def capture(argv: list[str], _options: object) -> Capture:
    args = _parse_cli(argv)
    if args.command != "get":
        if args.command == "search":
            payload = capture_json_command(
                args,
                cmd_search,
                detail="slack search capture failed",
            )
            return Capture(
                data=payload,
                name=preferred_name(argv, object()),
                type="application/json",
                meta={
                    "projector": "slack",
                    "slack_kind": "search",
                },
            )
        raise NotImplementedError("slack capture does not support this command")
    ref = resolve_slack_ref(args.ref, workspace=args.workspace)
    if ref.kind == "doc":
        html_bytes, meta = _fetch_slack_doc(ref, interactive_ok=is_interactive())
        return Capture(
            data=html_bytes,
            name=f"{ref.doc_id}.html" if ref.doc_id else preferred_name(argv, object()),
            type="text/html",
            meta={
                "projector": "slack",
                "slack_kind": "doc",
                "workspace": ref.workspace,
                "team_id": ref.team_id or "",
                "doc_id": ref.doc_id or "",
                "url": str(meta.get("url") or ref.url or ""),
                "content_type": str(meta.get("contentType") or ""),
                "retrieval": str(
                    meta.get("retrieval") or "live_auth_files_info_download"
                ),
            },
        )
    captured_args = argparse.Namespace(**vars(args))
    captured_args.output = "json"
    with capture_stdout() as captured:
        code = cmd_get(captured_args)
    if code != 0:
        detail = captured.getvalue().decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "slack get capture failed")
    name = (
        f"p{ref.thread_ts.replace('.', '')}.json"
        if ref.kind == "thread" and ref.thread_ts
        else f"{ref.channel_id}.json"
    )
    envelope = json.loads(captured.getvalue().decode("utf-8"))
    return Capture(
        data=captured.getvalue(),
        name=name,
        type="application/json",
        meta={
            "projector": "slack",
            "source_created_at": str(
                envelope.get("firstTsIso") or envelope.get("firstTs") or ""
            ),
            "source_updated_at": str(
                envelope.get("lastTsIso") or envelope.get("lastTs") or ""
            ),
        },
    )


def project(argv: list[str], capture: Capture) -> bytes:
    kind = str(capture.meta.get("slack_kind") or "get").strip()
    if kind == "search":
        payload = json.loads(capture.data.decode("utf-8"))
        if not argv:
            return render_search_markdown(payload).encode("utf-8")
        args = _parse_cli(argv)
        if args.command != "search":
            return capture.data
        if args.output == "json":
            return pretty_json(capture.data)
        if args.output == "titles":
            return render_search_titles(payload).encode("utf-8")
        if args.output == "links":
            return render_search_links(payload).encode("utf-8")
        return render_search_markdown(payload).encode("utf-8")
    if kind == "doc":
        if not argv:
            return _render_doc_markdown(capture.data)
        args = _parse_cli(argv)
        if args.command != "get":
            return capture.data
        if args.output in {"json", "meta"}:
            return json_bytes(_doc_meta_from_capture(capture))
        if args.output == "messages":
            raise ToolError(
                "`gotta slack get ... --output messages` is only supported for channel and thread reads; "
                "Slack docs support markdown, text, json, or meta."
            )
        if args.output == "markdown":
            return _render_doc_markdown(capture.data)
        return html_text(capture.data)
    envelope = json.loads(capture.data.decode("utf-8"))
    if not argv:
        return render_markdown(envelope).encode("utf-8")
    args = _parse_cli(argv)
    if args.command != "get":
        return capture.data
    if args.output == "json":
        return pretty_json(capture.data)
    if args.output == "meta":
        return json_bytes(envelope_meta(envelope))
    if args.output == "messages":
        return json_bytes(envelope.get("messages"))
    if args.output == "markdown":
        return render_markdown(envelope).encode("utf-8")
    return render_text(envelope).encode("utf-8")


def cmd_search(args: argparse.Namespace) -> int:
    ensure_workspace_auth(args.workspace, interactive_ok=is_interactive())
    channel_ref: SlackRef | None = None
    workspace = args.workspace
    channel_result: ArchiveResult | None = None
    if args.channel:
        channel_ref = resolve_slack_ref(args.channel, workspace=args.workspace)
        ensure_workspace_auth(channel_ref.workspace, interactive_ok=is_interactive())
        workspace = channel_ref.workspace
        if args.source == "live":
            if args.pull_recent or args.refresh:
                raise ToolError(
                    "`--pull-recent` and `--refresh` do not apply to Slack live search; "
                    "use `--source archive` for explicit bounded archive search"
                )
        elif args.refresh and not args.pull_recent:
            raise ToolError(
                "`gotta slack search --refresh` requires `--pull-recent LOOKBACK`"
            )
        elif args.pull_recent:
            channel_result = run_sync_archive(
                channel_ref, lookback=args.pull_recent, refresh=args.refresh
            )
        else:
            channel_result = existing_channel_cache(
                channel_ref.workspace, channel_ref.channel_id
            )
            if channel_result is None:
                try:
                    channel_result = try_opportunistic_sync(channel_ref)
                except ToolError as exc:
                    raise ToolError(
                        "no local archive exists for that channel, and the bounded opportunistic "
                        "pull did not finish quickly. run "
                        f"'gotta slack sync {channel_ref.channel_id} --lookback {DEFAULT_RECENT_LOOKBACK}' "
                        f"or use '--pull-recent {DEFAULT_RECENT_LOOKBACK}'. detail: {exc}"
                    ) from exc
    else:
        if args.source == "live" and (args.pull_recent or args.refresh):
            raise ToolError(
                "`--pull-recent` and `--refresh` do not apply to workspace live search; "
                "use `--source archive` for explicit bounded archive search"
            )
        if args.pull_recent or args.refresh:
            raise ToolError(
                "`--pull-recent` and `--refresh` require `--channel`; workspace search only reads the existing local archive"
            )
    payload: dict[str, Any]
    if args.source == "live":
        payload = search_live_payload(
            workspace=workspace,
            query=args.query,
            limit=args.limit,
            channel_id=channel_ref.channel_id if channel_ref else None,
            match_mode=args.match,
            timeout_seconds=args.live_timeout,
        )
    else:
        payload = {
            "workspace": workspace,
            "query": args.query,
            "terms": search_spec(args.query, match_mode=args.match).terms,
            "matchMode": args.match,
            "scope": "channel" if channel_ref else "workspace",
            "source": "local-archive",
            "channel": None,
            "channelCount": 0,
            "channels": [],
            "resultCount": 0,
            "threadCount": 0,
            "threads": [],
            "results": [],
        }
        try:
            result = (
                channel_result
                if channel_result is not None
                else ensure_archive_exists(
                    workspace_archive_result(args.workspace),
                    description=f"workspace archive for {args.workspace}",
                )
            )
        except ToolError:
            raise
        else:
            payload = search_archive_payload(
                result=result,
                query=args.query,
                limit=args.limit,
                workspace=workspace,
                channel_id=channel_ref.channel_id if channel_ref else None,
                match_mode=args.match,
            )
    if args.output == "json":
        print_json(payload)
    elif args.output == "markdown":
        sys.stdout.write(render_search_markdown(payload))
    elif args.output == "titles":
        sys.stdout.write(render_search_titles(payload))
    else:
        sys.stdout.write(render_search_links(payload))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    payload = slack_status_payload(args.workspace)
    workspace = str(payload.get("workspace") or "").strip()
    archive = workspace_archive_result(workspace) if workspace else None
    directory_path = directory_db_path(workspace) if workspace else None
    payload["archivePath"] = str(archive.db_path) if archive else ""
    payload["archiveExists"] = archive.db_path.exists() if archive else False
    payload["directoryPath"] = str(directory_path) if directory_path else ""
    payload["directoryExists"] = directory_path.exists() if directory_path else False
    if directory_path and directory_path.exists():
        conn = open_directory_db(workspace)
        try:
            payload["directoryCounts"] = {
                "channels": int(
                    conn.execute("SELECT COUNT(*) FROM channel_directory").fetchone()[0]
                ),
                "users": int(
                    conn.execute("SELECT COUNT(*) FROM user_directory").fetchone()[0]
                ),
            }
        finally:
            conn.close()
    if archive and archive.db_path.exists():
        conn = open_readonly_db(archive.db_path)
        try:
            payload["archiveCounts"] = {
                "messages": int(
                    conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
                ),
                "channels": int(
                    conn.execute("SELECT COUNT(*) FROM channel").fetchone()[0]
                ),
            }
        except sqlite3.Error as exc:
            payload["archiveError"] = str(exc)
        finally:
            conn.close()
    if args.output == "json":
        print_json(payload)
        return 0
    lines = [
        f"workspace\t{workspace}",
        f"selected_workspace\t{payload.get('selectedWorkspace') or ''}",
        f"known_workspaces\t{','.join(payload.get('knownWorkspaces') or [])}",
        f"slackdump_present\t{str(bool(payload['slackdumpPresent'])).lower()}",
        f"auth_configured\t{str(bool(payload['authConfigured'])).lower()}",
        f"archive_exists\t{str(bool(payload['archiveExists'])).lower()}",
        f"directory_exists\t{str(bool(payload['directoryExists'])).lower()}",
        f"config_file\t{payload.get('configPathDisplay') or ''}",
        f"next_step\t{payload['nextStep']}",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def cmd_workspaces(args: argparse.Namespace) -> int:
    payload = {
        "knownWorkspaces": known_workspaces(),
        "selectedWorkspace": default_workspace(),
    }
    if args.output == "json":
        print_json(payload)
        return 0
    for workspace in payload["knownWorkspaces"]:
        prefix = "* " if workspace == payload["selectedWorkspace"] else "- "
        sys.stdout.write(f"{prefix}{workspace}\n")
    return 0


def cmd_sql(args: argparse.Namespace) -> int:
    try:
        conn, db_path = open_workspace_db(args.workspace, database=args.database)
        try:
            cursor = conn.execute(args.query)
            columns = [item[0] for item in (cursor.description or [])]
            rows = [
                {column: sqlite_json_value(row[column]) for column in columns}
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ToolError(f"sqlite query failed: {exc}") from exc

    payload = {
        "workspace": args.workspace,
        "database": args.database,
        "dbPath": str(db_path),
        "query": args.query,
        "columns": columns,
        "rowCount": len(rows),
        "rows": rows,
    }
    if args.output == "json":
        print_json(payload)
        return 0
    if columns:
        sys.stdout.write("\t".join(columns) + "\n")
        for row in rows:
            sys.stdout.write(
                "\t".join(
                    "" if row[column] is None else str(row[column])
                    for column in columns
                )
                + "\n"
            )
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    conn, db_path = open_workspace_db(args.workspace, database=args.database)
    try:
        tables = conn.execute(
            """
            SELECT name, type
            FROM sqlite_master
            WHERE type IN ('table', 'view')
            AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        payload_tables: list[dict[str, Any]] = []
        for row in tables:
            name = str(row["name"])
            if args.table and name != args.table:
                continue
            error = ""
            try:
                columns = conn.execute(
                    "SELECT * FROM pragma_table_info(?)",
                    (name,),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                columns = []
                error = str(exc)
            payload_tables.append(
                {
                    "name": name,
                    "type": str(row["type"]),
                    "columns": [
                        {
                            "name": str(column["name"]),
                            "type": str(column["type"]),
                            "notNull": bool(column["notnull"]),
                            "default": sqlite_json_value(column["dflt_value"]),
                            "primaryKey": bool(column["pk"]),
                        }
                        for column in columns
                    ],
                    "error": error,
                }
            )
    finally:
        conn.close()
    payload = {
        "workspace": args.workspace,
        "database": args.database,
        "dbPath": str(db_path),
        "tables": payload_tables,
    }
    if args.output == "json":
        print_json(payload)
        return 0
    for table in payload_tables:
        sys.stdout.write(f"{table['type']}\t{table['name']}\n")
        for column in table["columns"]:
            sys.stdout.write(
                "\t".join(
                    [
                        "",
                        column["name"],
                        column["type"],
                        f"not_null={str(column['notNull']).lower()}",
                        f"primary_key={str(column['primaryKey']).lower()}",
                    ]
                )
                + "\n"
            )
        if table.get("error"):
            sys.stdout.write(f"\t<unavailable>\t{table['error']}\n")
    return 0


def cmd_list_channels(args: argparse.Namespace) -> int:
    ensure_workspace_auth(args.workspace, interactive_ok=is_interactive())
    if args.refresh:
        refresh_directory(
            workspace=args.workspace,
            entity="channels",
            force=True,
            announce=True,
        )
    else:
        seed_channel_directory_from_archive(args.workspace)
    conn = open_directory_db(args.workspace)
    try:
        rows = conn.execute(
            """
            SELECT id, name, type, is_private, is_archived, raw_json
            FROM channel_directory
            ORDER BY lookup_name ASC, id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    channel_types = None
    if args.channel_types:
        channel_types = {
            item.strip() for item in args.channel_types.split(",") if item.strip()
        }
    query = str(args.query or "").strip().lower()
    filtered: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    for row in rows:
        item = normalize_directory_channel_item(
            parse_json_blob(row["raw_json"]),
            channel_id=str(row["id"] or ""),
            name=str(row["name"] or ""),
            row_type=str(row["type"] or ""),
            is_private=bool(row["is_private"]),
            is_archived=bool(row["is_archived"]),
        )
        if channel_types and str(item.get("type") or "") not in channel_types:
            continue
        if not tri_state_matches(args.private, bool(item.get("is_private"))):
            continue
        if not tri_state_matches(args.shared, bool(item.get("is_shared"))):
            continue
        if not tri_state_matches(args.ext_shared, bool(item.get("is_ext_shared"))):
            continue
        if not tri_state_matches(args.archived, bool(item.get("is_archived"))):
            continue
        if query and query not in channel_directory_text(item).lower():
            continue
        filtered.append((row, item))
    paged, page = _paginate_directory_results(
        filtered,
        offset=args.offset,
        limit=args.limit,
        include_all=bool(args.all),
    )
    if args.output == "json":
        print_json(
            {
                "workspace": args.workspace,
                "source": "local-directory",
                "query": args.query or "",
                **page,
                "filters": {
                    "channelTypes": sorted(channel_types) if channel_types else [],
                    "private": args.private,
                    "shared": args.shared,
                    "extShared": args.ext_shared,
                    "archived": args.archived,
                },
                "results": [raw for _, raw in paged],
            }
        )
        return 0
    for row, item in paged:
        channel_id = str(item.get("id") or "")
        name = str(item.get("name") or channel_id)
        kind = str(row["type"] or channel_type(item, channel_id))
        summary = (
            str(
                (
                    (item.get("purpose") or {}).get("value")
                    or (item.get("topic") or {}).get("value")
                    or ""
                )
            )
            .replace("\t", " ")
            .replace("\n", " ")
            .strip()
        )
        sys.stdout.write(
            "\t".join(
                [
                    channel_id,
                    name,
                    kind,
                    f"private={str(bool(item.get('is_private'))).lower()}",
                    f"shared={str(bool(item.get('is_shared'))).lower()}",
                    f"ext_shared={str(bool(item.get('is_ext_shared'))).lower()}",
                    f"archived={str(bool(item.get('is_archived'))).lower()}",
                    summary,
                ]
            )
            + "\n"
        )
    _emit_directory_paging_notice(
        entity="channels",
        total_count=int(page["totalCount"]),
        shown_count=int(page["shownCount"]),
        offset=int(page["offset"]),
        next_offset=page["nextOffset"],
    )
    return 0


def cmd_list_users(args: argparse.Namespace) -> int:
    ensure_workspace_auth(args.workspace, interactive_ok=is_interactive())
    if args.refresh:
        refresh_directory(
            workspace=args.workspace,
            entity="users",
            force=True,
            announce=True,
        )
    else:
        seed_user_directory_from_archive(args.workspace)
    conn = open_directory_db(args.workspace)
    try:
        rows = conn.execute(
            """
            SELECT id, display_name, username, raw_json
            FROM user_directory
            ORDER BY lookup_name ASC, id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    paged, page = _paginate_directory_results(
        list(rows),
        offset=args.offset,
        limit=args.limit,
        include_all=bool(args.all),
    )
    if args.output == "json":
        print_json(
            {
                "workspace": args.workspace,
                "source": "local-directory",
                **page,
                "results": [parse_json_blob(row["raw_json"]) for row in paged],
            }
        )
        return 0
    for row in paged:
        sys.stdout.write(f"{row['id']}\t{row['display_name']}\t{row['username']}\n")
    _emit_directory_paging_notice(
        entity="users",
        total_count=int(page["totalCount"]),
        shown_count=int(page["shownCount"]),
        offset=int(page["offset"]),
        next_offset=page["nextOffset"],
    )
    return 0


def is_mcp_metadata_only_invocation(args: list[str]) -> bool:
    return any(arg in {"--help", "--version", "-h"} for arg in args)


def cmd_mcp(args: argparse.Namespace) -> int:
    ensure_slackdump()
    passthrough = list(args.mcp_args or [])
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]
    if is_mcp_metadata_only_invocation(passthrough):
        proc = subprocess.run(["slackdump", "mcp", *passthrough], check=False)
        return int(proc.returncode)
    workspace = resolve_workspace(str(args.workspace or ""))
    if not workspace:
        raise ToolError(missing_workspace_message())
    result = workspace_archive_result(workspace)
    archive_arg: list[str] = []
    if result.db_path.exists():
        archive_arg = [str(result.db_path)]
    else:
        print(
            (
                f"note: starting Slack MCP without a preloaded archive for {workspace}; "
                f"run 'gotta slack sync <channel> --workspace {workspace} --lookback {DEFAULT_RECENT_LOOKBACK}' "
                "to populate the shared archive, or let the MCP client call load_source"
            ),
            file=sys.stderr,
        )
    cmd = ["slackdump", "mcp", *passthrough, *archive_arg]
    proc = subprocess.run(cmd, check=False)
    return int(proc.returncode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta slack",
        description=(
            "Slack CLI with native live search and bounded archive integration. "
            "Explicit syncs enrich one bounded local SQLite archive; ordinary reads do not "
            "silently crawl the whole workspace."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "auth",
        help="low-level exact Slack auth surface; use `gotta config slack ...` for canonical durable setup",
    )
    p.add_argument("--workspace", default=default_workspace())
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser(
        "status",
        help="inspect Slack readiness; use `gotta config slack ...` to persist defaults and guide setup",
    )
    p.add_argument("--workspace", default=default_workspace())
    p.add_argument("--output", choices=["json", "summary"], default="summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "workspaces",
        help="list locally known Slack workspaces before choosing a durable default",
    )
    p.add_argument("--output", choices=["json", "text"], default="json")
    p.set_defaults(func=cmd_workspaces)

    p = sub.add_parser(
        "sync",
        help="reuse or enrich the durable workspace SQLite archive for one channel",
    )
    p.add_argument(
        "target", help="Slack channel URL, channel ID, or thread URL/notation"
    )
    p.add_argument("--workspace", default=default_workspace())
    p.add_argument(
        "--lookback",
        default="",
        help=f"bounded sync window ending now; capped at {MAX_SYNC_LOOKBACK}",
    )
    p.add_argument(
        "--since",
        help="explicit bounded sync start time; requires --until or defaults the end to now",
    )
    p.add_argument(
        "--until",
        help="explicit bounded sync end time; requires --since",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="force a re-pull of the requested lookback into the workspace archive",
    )
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser(
        "get",
        help="fetch one Slack thread/doc or read one channel from the native Slack surfaces",
    )
    p.add_argument(
        "ref",
        help="Slack thread/channel/doc URL, channel ID, canonical doc locator, or thread colon notation",
    )
    p.add_argument("--workspace", default=default_workspace())
    p.add_argument(
        "--output",
        choices=["json", "meta", "messages", "markdown", "text"],
        default="markdown",
        help="render format for get; defaults to markdown. Read fidelity is reported in-band as full or bounded.",
    )
    p.add_argument(
        "--pull-recent",
        metavar="LOOKBACK",
        help=f"explicitly sync a bounded recent-history window before channel reads, e.g. 3w; capped at {MAX_SYNC_LOOKBACK}",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="rebuild the pulled archive window; for thread reads, rebuild the native six-week centered window",
    )
    p.add_argument(
        "--lookback",
        help="display only the most recent bounded window for channel reads, e.g. 3d or 6h",
    )
    p.add_argument(
        "--since",
        help="display only messages at or after this time for channel reads; ISO 8601 or Slack ts",
    )
    p.add_argument(
        "--until",
        help="display only messages at or before this time for channel reads; ISO 8601 or Slack ts",
    )
    p.add_argument(
        "--strict-window",
        action="store_true",
        help="fail unless the local archive can prove it fully covers the requested window",
    )
    p.set_defaults(func=cmd_get)

    p = sub.add_parser(
        "search",
        help="search Slack live by default; use archive mode for explicit local-only search",
    )
    p.add_argument("query")
    p.add_argument(
        "--channel",
        help="channel URL/ID to search within the local workspace archive; omit for workspace-wide search",
    )
    p.add_argument("--workspace", default=default_workspace())
    p.add_argument("--limit", type=int, default=20)
    p.add_argument(
        "--output",
        choices=["json", "titles", "markdown", "links"],
        default="markdown",
    )
    p.add_argument(
        "--source",
        choices=["archive", "live"],
        default="live",
        help="search the native Slack search API or the explicit local archive",
    )
    p.add_argument(
        "--match",
        choices=["literal", "all", "any"],
        default="all",
        help="search semantics: exact phrase, all terms, or any term; native live search preserves these modes",
    )
    p.add_argument(
        "--live-timeout",
        type=int,
        default=DEFAULT_LIVE_SEARCH_TIMEOUT_SECONDS,
        help="timeout in seconds for native Slack live search",
    )
    p.add_argument(
        "--pull-recent",
        metavar="LOOKBACK",
        help=f"explicitly sync a bounded recent-history window before searching, e.g. 3w; capped at {MAX_SYNC_LOOKBACK}",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="rebuild the pulled archive window; requires --pull-recent",
    )
    p.set_defaults(func=cmd_search)

    p = sub.add_parser(
        "sql",
        help="run one raw read-only SQL statement against the shared workspace archive",
    )
    p.add_argument(
        "query", help="single SQLite statement to execute against slackdump.sqlite"
    )
    p.add_argument("--workspace", default=default_workspace())
    p.add_argument("--database", choices=["archive", "directory"], default="archive")
    p.add_argument("--output", choices=["json", "tsv"], default="json")
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser(
        "schema",
        help="inspect tables and columns in the shared workspace archive before writing SQL",
    )
    p.add_argument("--workspace", default=default_workspace())
    p.add_argument("--database", choices=["archive", "directory"], default="archive")
    p.add_argument("--table", help="restrict schema output to one table or view")
    p.add_argument("--output", choices=["json", "text"], default="text")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser(
        "list-channels",
        help="list channels from the local directory cache; refresh is explicit",
    )
    p.add_argument("--workspace", default=default_workspace())
    p.add_argument("--query", help="match channel name, ID, topic, or purpose text")
    p.add_argument(
        "--channel-types", help="filter cached channel rows by comma-separated types"
    )
    p.add_argument("--private", choices=["any", "only", "exclude"], default="any")
    p.add_argument("--shared", choices=["any", "only", "exclude"], default="any")
    p.add_argument("--ext-shared", choices=["any", "only", "exclude"], default="any")
    p.add_argument("--archived", choices=["any", "only", "exclude"], default="any")
    p.add_argument(
        "--limit",
        type=positive_int,
        default=DEFAULT_DIRECTORY_LIST_LIMIT,
        help="maximum channels to show after filtering; defaults to a bounded page",
    )
    p.add_argument(
        "--offset",
        type=nonnegative_int,
        default=0,
        help="skip the first N filtered channels before rendering the current page",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="show all filtered channels explicitly instead of the default bounded page",
    )
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--output", choices=["json", "text"], default="json")
    p.set_defaults(func=cmd_list_channels)

    p = sub.add_parser(
        "list-users",
        help="list users from the local directory cache; refresh is explicit",
    )
    p.add_argument("--workspace", default=default_workspace())
    p.add_argument(
        "--limit",
        type=positive_int,
        default=DEFAULT_DIRECTORY_LIST_LIMIT,
        help="maximum users to show; defaults to a bounded page",
    )
    p.add_argument(
        "--offset",
        type=nonnegative_int,
        default=0,
        help="skip the first N users before rendering the current page",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="show all users explicitly instead of the default bounded page",
    )
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--output", choices=["json", "text"], default="json")
    p.set_defaults(func=cmd_list_users)

    p = sub.add_parser(
        "mcp",
        help="run slackdump mcp against the shared workspace archive",
    )
    p.add_argument("--workspace", default=default_workspace())
    p.add_argument(
        "mcp_args",
        nargs=argparse.REMAINDER,
        help="arguments passed through to 'slackdump mcp'; prefix passthrough flags with '--'",
    )
    p.set_defaults(func=cmd_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if is_long_help_request(argv):
        return print_long_help(parser)
    try:
        args = parser.parse_args(argv)
        return int(args.func(args))
    except ToolError as exc:
        return die(str(exc), code=1)


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        raise SystemExit(main(sys.argv[1:]))
    except BrokenPipeError:
        raise SystemExit(0)
    except ToolError as exc:
        raise SystemExit(die(str(exc)))
