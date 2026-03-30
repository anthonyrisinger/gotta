#!/usr/bin/env python3
"""Standalone Jira discovery, read, and authoring toolbelt."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any, Mapping

from gotta.capture import Capture, capture_json_command, json_bytes
from gotta.projection import Projection, projection_bytes
from gotta.config import set_provider_env_values
from gotta.helptext import is_long_help_request, print_long_help
from gotta.project import pretty_json
from gotta.resolve.route import query_route, strip_http_url_fragment
from gotta.resolve.search import plain_text_search_route
from gotta.source.render import (
    render_source_metadata_lines,
    render_visibility_metadata_lines,
)
from gotta.source.stamp import derive_source_metadata_from_payload
from gotta.source.visibility import (
    unknown_visibility,
    visibility_metadata,
    with_visibility_metadata,
)
from gotta.providers import atlassian as atl

OAUTH_DIR = atl.OAUTH_DIR
TOKEN_FILE = atl.TOKEN_FILE
CLOUD_ID_FILE = atl.CLOUD_ID_FILE
DEFAULT_JIRA_BASE_URL = ""
JIRA_BASE_URL_ENV = "GOTTA_JIRA_BASE_URL"
DEFAULT_GET_FIELDS = [
    "summary",
    "status",
    "issuetype",
    "security",
    "assignee",
    "reporter",
    "priority",
    "description",
    "labels",
    "created",
    "updated",
    "project",
]
DEFAULT_SEARCH_FIELDS = [
    "summary",
    "status",
    "issuetype",
    "security",
    "assignee",
    "priority",
    "labels",
    "updated",
    "project",
]
DISALLOWED_MCP_PASSTHROUGH_FLAGS = atl.DISALLOWED_MCP_PASSTHROUGH_FLAGS
ISSUE_KEY_RE = re.compile(r"^(?P<project>[A-Za-z][A-Za-z0-9_]*)-(?P<number>\d+)$")
OBVIOUS_JQL_FIELDS = (
    "affectedVersion",
    "assignee",
    "component",
    "created",
    "creator",
    "description",
    "fixVersion",
    "issue",
    "issuekey",
    "issuetype",
    "key",
    "labels",
    "parent",
    "priority",
    "project",
    "reporter",
    "resolution",
    "resolved",
    "sprint",
    "status",
    "summary",
    "text",
    "type",
    "updated",
)
OBVIOUS_JQL_FIELD_PATTERN = r"(?:%s)" % "|".join(
    re.escape(field) for field in OBVIOUS_JQL_FIELDS
)

ToolError = atl.AtlassianError


@dataclass
class IssueRef:
    issue_key: str
    base_url: str


@dataclass
class Session:
    token: str
    cloud_id: str
    base_url: str


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


DEFAULT_LIST_LIMIT = 100


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


load_atlassian_config_env = atl.load_atlassian_config_env
is_interactive = atl.is_interactive
load_oauth_runtime_config = atl.load_oauth_runtime_config
api_json = atl.api_json
site_root = atl.site_root
read_text = atl.read_text
token_preflight_status = atl.token_preflight_status
load_cloud_id = atl.load_cloud_id
atlassian_status_payload = atl.atlassian_status_payload


DISCOVERY_COMMANDS = {"search", "jql"}
EVIDENCE_COMMANDS = {"get"}
NON_ARTIFACT_COMMANDS = {
    "add-to-sprint",
    "auth",
    "comment",
    "create",
    "fields",
    "issue-types",
    "link",
    "link-types",
    "mcp",
    "projects",
    "sprints",
    "status",
    "transition",
    "transitions",
    "update",
}


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


def _search_read_target(subject: str) -> str:
    subject = subject.strip()
    if not subject:
        return ""
    try:
        return canonical_locator(["get", subject])
    except Exception:
        return f"jira:{subject}"


def search_route(raw_tail: str) -> list[str]:
    return plain_text_search_route(
        "jira",
        raw_tail,
        specialized_commands={"jql": "jira jql"},
        read_redirects={"get": _search_read_target},
    )


def discover_cloud_id(token: str, base_url: str) -> str:
    return atl.discover_cloud_id(
        token,
        base_url,
        base_url_env=JIRA_BASE_URL_ENV,
    )


def run_oauth_bootstrap(*, base_url: str = "") -> dict[str, Any]:
    return atl.run_oauth_bootstrap(
        base_url=base_url,
        base_url_env=JIRA_BASE_URL_ENV,
    )


def load_token(base_url: str = "") -> str:
    return atl.load_token(
        base_url=base_url,
        auth_command="jira",
        base_url_env=JIRA_BASE_URL_ENV,
    )


def load_session(base_url: str, *, allow_reauth: bool = True) -> Session:
    token = load_token(base_url)
    try:
        if base_url:
            cloud_id = discover_cloud_id(token, base_url)
            resolved_base_url = site_root(base_url)
        else:
            cloud_id, resolved_base_url = atl.resolve_accessible_resource(
                token,
                "",
                base_url_env=JIRA_BASE_URL_ENV,
                cloud_id=load_cloud_id(),
            )
        return Session(token=token, cloud_id=cloud_id, base_url=resolved_base_url)
    except ToolError:
        if (
            allow_reauth
            and is_interactive()
            and token_preflight_status(token) == "invalid"
        ):
            run_oauth_bootstrap(base_url=base_url)
            return load_session(base_url, allow_reauth=False)
        raise


def default_base_url() -> str:
    config_env = load_atlassian_config_env()
    return site_root(
        atl.env_or_config(
            config_env, JIRA_BASE_URL_ENV, default=DEFAULT_JIRA_BASE_URL
        ).strip()
    )


def _slug(value: str, *, fallback: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    return value.strip("-") or fallback


def _output_extension(output: str) -> str:
    return {
        "markdown": "md",
        "meta": "json",
        "json": "json",
        "adf": "json",
        "summary": "summary",
        "text": "txt",
    }.get(output, "md")


def _list_window_suffix(*, limit: int, offset: int, include_all: bool) -> str:
    parts: list[str] = []
    if include_all:
        parts.append("all")
    elif limit != DEFAULT_LIST_LIMIT:
        parts.append(f"limit-{limit}")
    if offset:
        parts.append(f"offset-{offset}")
    return ("-" + "-".join(parts)) if parts else ""


def _append_list_window_args(parts: list[str], args: argparse.Namespace) -> None:
    if getattr(args, "all", False):
        parts.append("--all")
    elif getattr(args, "limit", DEFAULT_LIST_LIMIT) != DEFAULT_LIST_LIMIT:
        parts.extend(["--limit", str(args.limit)])
    if getattr(args, "offset", 0):
        parts.extend(["--offset", str(args.offset)])


def _paginate_items(
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


def _listing_count_line(
    label: str,
    *,
    total_count: int,
    shown_count: int,
    offset: int,
    next_offset: int | None,
) -> str:
    if shown_count == total_count and offset == 0 and next_offset is None:
        return f"{label}: {total_count}"
    details = [f"showing {shown_count}", f"offset {offset}"]
    if next_offset is not None:
        details.append(f"next {next_offset}")
    return f"{label}: {total_count} total ({', '.join(details)})"


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _issue_key_for_locator(raw: str) -> str:
    candidate = raw.strip()
    if ISSUE_KEY_RE.fullmatch(candidate):
        return normalize_issue_key(candidate)
    return parse_issue_ref(candidate).issue_key


def _search_issue_key(query: str) -> str:
    candidate = query.strip()
    if ISSUE_KEY_RE.fullmatch(candidate):
        return normalize_issue_key(candidate)
    return ""


def _search_payload_for_issue(
    envelope: dict[str, Any], *, query: str
) -> dict[str, Any]:
    key = str(envelope.get("key") or "")
    return _with_jira_visibility(
        {
            "query": query,
            "limit": 1,
            "requestedNext": "",
            "next": "",
            "size": 1,
            "results": [
                _with_jira_visibility(
                    {
                        "key": key,
                        "summary": envelope.get("summary"),
                        "status": envelope.get("status"),
                        "issueType": envelope.get("issueType"),
                        "security": envelope.get("security"),
                        "project": envelope.get("project"),
                        "priority": envelope.get("priority"),
                        "assignee": envelope.get("assignee"),
                        "labels": envelope.get("labels") or [],
                        "updated": envelope.get("updated") or "",
                        "issueUrl": envelope.get("issueUrl") or "",
                        "siteUrl": envelope.get("siteUrl") or "",
                    },
                    subcommand="search",
                    locator=_issue_locator(key),
                )
            ],
            "source_created_at": envelope.get("created") or "",
            "source_updated_at": envelope.get("updated") or "",
        },
        subcommand="search",
        locator=_search_locator("search", query),
    )


def canonical_locator(argv: list[str]) -> str:
    args = _parse_cli(argv)
    if args.command == "get":
        return f"jira:{_issue_key_for_locator(args.issue)}"
    if args.command == "projects":
        parts = ["projects"]
        _append_list_window_args(parts, args)
        return f"jira:{shlex.join(parts)}"
    if args.command == "sprints":
        if args.board is not None:
            parts = ["sprints", "--board", str(args.board)]
        else:
            parts = ["sprints", "--project", args.project]
        _append_list_window_args(parts, args)
        return f"jira:{shlex.join(parts)}"
    if args.command == "add-to-sprint":
        parts = ["add-to-sprint", _issue_key_for_locator(args.issue)]
        if args.current:
            parts.append("--current")
        if args.project:
            parts.extend(["--project", args.project])
        if args.board is not None:
            parts.extend(["--board", str(args.board)])
        if args.sprint is not None:
            parts.extend(["--sprint", str(args.sprint)])
        return f"jira:{shlex.join(parts)}"
    if args.command == "issue-types":
        parts = ["issue-types", "--project", args.project]
        _append_list_window_args(parts, args)
        return f"jira:{shlex.join(parts)}"
    if args.command == "fields":
        if args.issue:
            return f"jira:{shlex.join(['fields', _issue_key_for_locator(args.issue)])}"
        return f"jira:{shlex.join(['fields', '--project', args.project, '--type', args.type])}"
    if args.command == "link-types":
        parts = ["link-types"]
        _append_list_window_args(parts, args)
        return f"jira:{shlex.join(parts)}"
    if args.command == "transitions":
        parts = ["transitions", _issue_key_for_locator(args.issue)]
        _append_list_window_args(parts, args)
        return f"jira:{shlex.join(parts)}"
    if args.command == "search":
        return f"jira:search {args.query}"
    if args.command == "jql":
        return f"jira:jql {args.query}"
    if args.command == "status":
        return "jira:status"
    return f"jira:{shlex.join(argv)}"


def preferred_name(argv: list[str], options: object) -> str:
    if getattr(options, "save_as", ""):
        return str(getattr(options, "save_as"))
    args = _parse_cli(argv)
    if args.command == "get":
        issue_key = _issue_key_for_locator(args.issue)
        return f"{issue_key}.json"
    if args.command == "projects":
        suffix = _list_window_suffix(
            limit=args.limit, offset=args.offset, include_all=bool(args.all)
        )
        return f"jira-projects{suffix}.{_output_extension(args.output)}"
    if args.command == "sprints":
        suffix = _list_window_suffix(
            limit=args.limit, offset=args.offset, include_all=bool(args.all)
        )
        if args.board is not None:
            return f"jira-sprints-board-{args.board}{suffix}.{_output_extension(args.output)}"
        return f"jira-sprints-{_slug(args.project, fallback='jira')}{suffix}.{_output_extension(args.output)}"
    if args.command == "add-to-sprint":
        issue_key = _issue_key_for_locator(args.issue)
        return f"{issue_key}-add-to-sprint.{_output_extension(args.output)}"
    if args.command == "issue-types":
        suffix = _slug(args.project, fallback="jira")
        window_suffix = _list_window_suffix(
            limit=args.limit, offset=args.offset, include_all=bool(args.all)
        )
        return (
            f"jira-issue-types-{suffix}{window_suffix}.{_output_extension(args.output)}"
        )
    if args.command == "fields":
        if args.issue:
            issue_key = _issue_key_for_locator(args.issue)
            return f"jira-fields-{issue_key}.{_output_extension(args.output)}"
        suffix = _slug(f"{args.project}-{args.type}", fallback="jira")
        return f"jira-fields-{suffix}.{_output_extension(args.output)}"
    if args.command == "link-types":
        suffix = _list_window_suffix(
            limit=args.limit, offset=args.offset, include_all=bool(args.all)
        )
        return f"jira-link-types{suffix}.{_output_extension(args.output)}"
    if args.command == "transitions":
        issue_key = _issue_key_for_locator(args.issue)
        suffix = _list_window_suffix(
            limit=args.limit, offset=args.offset, include_all=bool(args.all)
        )
        return f"jira-transitions-{issue_key}{suffix}.{_output_extension(args.output)}"
    if args.command == "create":
        suffix = _slug(f"{args.project}-{args.title}", fallback="jira")
        return f"jira-create-{suffix}.{_output_extension(args.output)}"
    if args.command in {"update", "comment", "link", "transition"}:
        issue_key = _issue_key_for_locator(args.issue)
        return f"{issue_key}-{args.command}.{_output_extension(args.output)}"
    if args.command in {"search", "jql"}:
        return f"jira-{args.command}-{_slug(args.query, fallback='jira')}.json"
    if args.command == "status":
        return f"jira.{_output_extension(args.output)}"
    return "jira.txt"


def route_target(target: str) -> list[str] | None:
    if target.startswith("https://") and ".atlassian.net/browse/" in target:
        if any(char.isspace() for char in target):
            return None
        return ["get", strip_http_url_fragment(target)]
    if target.startswith("jira:search "):
        return query_route(
            "search",
            target.removeprefix("jira:search "),
            valued_flags=("--base-url", "--limit", "--next", "--output"),
        )
    if target.startswith("jira:jql "):
        return query_route(
            "jql",
            target.removeprefix("jira:jql "),
            valued_flags=("--base-url", "--limit", "--next", "--output"),
        )
    if target.startswith("jira:"):
        payload = target.removeprefix("jira:")
        if payload == "status":
            return [payload]
        try:
            argv = shlex.split(payload)
        except ValueError:
            return None
        if not argv:
            return None
        if len(argv) == 1 and ISSUE_KEY_RE.fullmatch(argv[0]):
            return ["get", normalize_issue_key(argv[0])]
        if argv[0] in {
            "get",
            "projects",
            "issue-types",
            "fields",
            "link-types",
            "transitions",
            "sprints",
            "add-to-sprint",
        }:
            return argv
        return None
    return None


def persist_selected_base_urls(base_url: str) -> None:
    site_url = site_root(base_url)
    if not site_url:
        return
    set_provider_env_values(
        "atlassian",
        {
            JIRA_BASE_URL_ENV: site_url,
            "GOTTA_CONFLUENCE_BASE_URL": f"{site_url}/wiki",
        },
    )


def normalize_issue_key(raw: str) -> str:
    match = ISSUE_KEY_RE.fullmatch(raw.strip())
    if not match:
        raise ToolError(
            f"invalid Jira issue reference: {raw}. Expected ISSUE-123 or a Jira issue URL"
        )
    return f"{match.group('project').upper()}-{match.group('number')}"


def parse_issue_ref(raw: str, *, base_url_override: str = "") -> IssueRef:
    candidate = raw.strip()
    if not candidate:
        raise ToolError("missing Jira issue reference")
    match = ISSUE_KEY_RE.fullmatch(candidate)
    if match:
        resolved_base_url = site_root(base_url_override or default_base_url())
        if not resolved_base_url:
            raise ToolError(
                "missing Jira base URL for bare issue key; pass --base-url, set "
                f"{JIRA_BASE_URL_ENV}, or use a full Jira issue URL"
            )
        return IssueRef(
            issue_key=normalize_issue_key(candidate),
            base_url=resolved_base_url,
        )
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolError(
            f"invalid Jira issue reference: {raw}. Expected ISSUE-123 or a Jira issue URL"
        )
    path = parsed.path.rstrip("/")
    url_match = re.search(r"/browse/([A-Za-z][A-Za-z0-9_]*-\d+)$", path)
    if not url_match:
        url_match = re.search(r"/issues/([A-Za-z][A-Za-z0-9_]*-\d+)$", path)
    if not url_match:
        raise ToolError(
            f"could not parse Jira issue key from URL: {raw}. Expected /browse/ISSUE-123 "
            "or /jira/.../issues/ISSUE-123"
        )
    base_url = site_root(f"{parsed.scheme}://{parsed.netloc}")
    return IssueRef(
        issue_key=normalize_issue_key(url_match.group(1)), base_url=base_url
    )


def _normalize_lookup(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def read_explicit_body(
    inline: str | None,
    file_path: str | None,
    *,
    stdin_flag: bool = False,
    required: bool,
    label: str,
) -> str:
    source_count = sum(
        1 for item in (inline is not None, file_path is not None, stdin_flag) if item
    )
    if source_count > 1:
        raise ToolError(f"use exactly one of inline {label}, --body-file, or --stdin")
    if file_path is not None:
        return read_text(Path(file_path))
    if inline is not None:
        return inline
    if stdin_flag:
        return sys.stdin.read()
    if required:
        raise ToolError(f"missing {label}; pass inline text, --body-file, or --stdin")
    return ""


def jira_projects_api_url(
    session: Session, *, start_at: int = 0, max_results: int = 100
) -> str:
    query = urllib.parse.urlencode({"startAt": start_at, "maxResults": max_results})
    return f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/project/search?{query}"


def jira_project_api_url(session: Session, project_key: str) -> str:
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/project/"
        f"{urllib.parse.quote(project_key)}"
    )


def jira_create_issue_api_url(session: Session) -> str:
    return f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/issue"


def jira_create_issue_types_api_url(session: Session, project_key: str) -> str:
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/issue/createmeta/"
        f"{urllib.parse.quote(project_key)}/issuetypes"
    )


def jira_create_fields_api_url(
    session: Session, project_key: str, issue_type_id: str
) -> str:
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/issue/createmeta/"
        f"{urllib.parse.quote(project_key)}/issuetypes/{urllib.parse.quote(issue_type_id)}"
    )


def jira_editmeta_api_url(session: Session, issue_key: str) -> str:
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/issue/"
        f"{urllib.parse.quote(issue_key)}/editmeta"
    )


def jira_comment_api_url(session: Session, issue_key: str) -> str:
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/issue/"
        f"{urllib.parse.quote(issue_key)}/comment"
    )


def jira_transitions_api_url(
    session: Session, issue_key: str, *, expand_fields: bool = False
) -> str:
    params = ""
    if expand_fields:
        params = "?" + urllib.parse.urlencode({"expand": "transitions.fields"})
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/issue/"
        f"{urllib.parse.quote(issue_key)}/transitions{params}"
    )


def jira_issue_link_types_api_url(session: Session) -> str:
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/issueLinkType"
    )


def jira_issue_link_api_url(session: Session) -> str:
    return f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/issueLink"


def jira_remote_link_api_url(session: Session, issue_key: str) -> str:
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/issue/"
        f"{urllib.parse.quote(issue_key)}/remotelink"
    )


def jira_boards_api_url(
    session: Session,
    *,
    start_at: int = 0,
    max_results: int = 50,
    project_key_or_id: str = "",
    board_type: str = "",
) -> str:
    params: dict[str, str | int] = {"startAt": start_at, "maxResults": max_results}
    if project_key_or_id:
        params["projectKeyOrId"] = project_key_or_id
    if board_type:
        params["type"] = board_type
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/agile/1.0/board?"
        f"{urllib.parse.urlencode(params)}"
    )


def jira_board_api_url(session: Session, board_id: int) -> str:
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/agile/1.0/board/"
        f"{board_id}"
    )


def jira_board_sprints_api_url(
    session: Session,
    board_id: int,
    *,
    start_at: int = 0,
    max_results: int = 50,
    state: str = "",
) -> str:
    params: dict[str, str | int] = {"startAt": start_at, "maxResults": max_results}
    if state:
        params["state"] = state
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/agile/1.0/board/"
        f"{board_id}/sprint?{urllib.parse.urlencode(params)}"
    )


def jira_sprint_api_url(session: Session, sprint_id: int) -> str:
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/agile/1.0/sprint/"
        f"{sprint_id}"
    )


def jira_add_issues_to_sprint_api_url(session: Session, sprint_id: int) -> str:
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/agile/1.0/sprint/"
        f"{sprint_id}/issue"
    )


def normalize_allowed_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key in ("id", "key", "name", "value", "displayName"):
        item = value.get(key)
        if isinstance(item, str) and item:
            normalized[key] = item
    if isinstance(value.get("accountId"), str) and value.get("accountId"):
        normalized["accountId"] = value["accountId"]
    return normalized


def normalize_field_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key in ("type", "system", "custom", "customId", "items"):
        value = schema.get(key)
        if value not in (None, ""):
            normalized[key] = value
    return normalized


def normalize_field_metadata(field_id: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    allowed_values = payload.get("allowedValues")
    if not isinstance(allowed_values, list):
        allowed_values = []
    operations = payload.get("operations")
    if not isinstance(operations, list):
        operations = []
    return {
        "id": field_id,
        "name": str(payload.get("name") or field_id),
        "required": bool(payload.get("required")),
        "hasDefaultValue": bool(payload.get("hasDefaultValue")),
        "operations": [item for item in operations if isinstance(item, str)],
        "schema": normalize_field_schema(payload.get("schema")),
        "allowedValues": [
            normalize_allowed_value(item)
            for item in allowed_values
            if isinstance(item, dict)
        ],
    }


def resolve_project(base_url: str, project: str) -> tuple[Session, dict[str, Any]]:
    session = load_session(site_root(base_url))
    payload = api_json("GET", jira_project_api_url(session, project), session.token)
    if not isinstance(payload, dict):
        raise ToolError("unexpected Jira project response")
    return session, payload


def fetch_projects(base_url: str) -> tuple[Session, list[dict[str, Any]]]:
    session = load_session(site_root(base_url))
    payload = api_json("GET", jira_projects_api_url(session), session.token)
    if not isinstance(payload, dict):
        raise ToolError("unexpected Jira projects response")
    values = payload.get("values")
    if not isinstance(values, list):
        values = payload.get("projects")
    if not isinstance(values, list):
        values = []
    projects: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        projects.append(
            {
                "id": str(item.get("id") or ""),
                "key": str(item.get("key") or ""),
                "name": str(item.get("name") or ""),
                "projectTypeKey": str(item.get("projectTypeKey") or ""),
                "simplified": bool(item.get("simplified")),
            }
        )
    return session, projects


def fetch_create_issue_types(
    session: Session, project_key: str
) -> list[dict[str, Any]]:
    payload = api_json(
        "GET", jira_create_issue_types_api_url(session, project_key), session.token
    )
    if not isinstance(payload, dict):
        raise ToolError("unexpected Jira issue type metadata response")
    values = payload.get("issueTypes")
    if not isinstance(values, list):
        values = payload.get("values")
    if not isinstance(values, list):
        values = []
    issue_types: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        issue_types.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "subtask": bool(item.get("subtask")),
                "hierarchyLevel": item.get("hierarchyLevel"),
            }
        )
    return issue_types


def resolve_issue_type(session: Session, project_key: str, raw: str) -> dict[str, Any]:
    lookup = _normalize_lookup(raw)
    for item in fetch_create_issue_types(session, project_key):
        if (
            str(item.get("id") or "") == raw
            or _normalize_lookup(str(item.get("name") or "")) == lookup
        ):
            return item
    raise ToolError(
        f"unknown Jira issue type for project {project_key}: {raw}. "
        f"Likely next step: run `gotta jira issue-types --project {project_key}`"
    )


def fetch_create_fields(
    session: Session, project_key: str, issue_type_id: str
) -> dict[str, dict[str, Any]]:
    payload = api_json(
        "GET",
        jira_create_fields_api_url(session, project_key, issue_type_id),
        session.token,
    )
    if not isinstance(payload, dict):
        raise ToolError("unexpected Jira create field metadata response")
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    return {
        field_id: normalize_field_metadata(field_id, meta)
        for field_id, meta in fields.items()
    }


def fetch_edit_fields(issue_ref: IssueRef) -> tuple[Session, dict[str, dict[str, Any]]]:
    session = load_session(issue_ref.base_url)
    payload = api_json(
        "GET", jira_editmeta_api_url(session, issue_ref.issue_key), session.token
    )
    if not isinstance(payload, dict):
        raise ToolError("unexpected Jira edit metadata response")
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    return session, {
        field_id: normalize_field_metadata(field_id, meta)
        for field_id, meta in fields.items()
    }


def fetch_issue_link_types(base_url: str) -> tuple[Session, list[dict[str, Any]]]:
    session = load_session(site_root(base_url))
    payload = api_json("GET", jira_issue_link_types_api_url(session), session.token)
    if not isinstance(payload, dict):
        raise ToolError("unexpected Jira issue link type response")
    values = payload.get("issueLinkTypes")
    if not isinstance(values, list):
        values = []
    link_types: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        link_types.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "inward": str(item.get("inward") or ""),
                "outward": str(item.get("outward") or ""),
            }
        )
    return session, link_types


def fetch_transitions(issue_ref: IssueRef) -> tuple[Session, list[dict[str, Any]]]:
    session = load_session(issue_ref.base_url)
    payload = api_json(
        "GET",
        jira_transitions_api_url(session, issue_ref.issue_key, expand_fields=True),
        session.token,
    )
    if not isinstance(payload, dict):
        raise ToolError("unexpected Jira transitions response")
    values = payload.get("transitions")
    if not isinstance(values, list):
        values = []
    transitions: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields")
        if not isinstance(fields, dict):
            fields = {}
        transitions.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "to": named_object(item.get("to")),
                "fields": {
                    field_id: normalize_field_metadata(field_id, meta)
                    for field_id, meta in fields.items()
                    if isinstance(meta, dict)
                },
            }
        )
    return session, transitions


def normalize_board(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        "id": str(payload.get("id") or ""),
        "name": str(payload.get("name") or ""),
        "type": str(payload.get("type") or ""),
    }


def normalize_sprint(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        "id": str(payload.get("id") or ""),
        "name": str(payload.get("name") or ""),
        "state": str(payload.get("state") or ""),
        "goal": str(payload.get("goal") or ""),
        "startDate": str(payload.get("startDate") or ""),
        "endDate": str(payload.get("endDate") or ""),
        "completeDate": str(payload.get("completeDate") or ""),
        "originBoardId": str(payload.get("originBoardId") or ""),
    }


def fetch_scrum_boards(
    base_url: str,
    *,
    project_key_or_id: str = "",
) -> tuple[Session, list[dict[str, Any]]]:
    session = load_session(site_root(base_url))
    start_at = 0
    boards: list[dict[str, Any]] = []
    while True:
        payload = api_json(
            "GET",
            jira_boards_api_url(
                session,
                start_at=start_at,
                max_results=50,
                project_key_or_id=project_key_or_id,
                board_type="scrum",
            ),
            session.token,
        )
        if not isinstance(payload, dict):
            raise ToolError("unexpected Jira board response")
        values = payload.get("values")
        if not isinstance(values, list):
            values = []
        boards.extend(
            normalize_board(item) for item in values if isinstance(item, dict)
        )
        if bool(payload.get("isLast")):
            break
        start_at += int(payload.get("maxResults") or len(values) or 50)
        if not values:
            break
    return session, [board for board in boards if board.get("id")]


def fetch_board(session: Session, board_id: int) -> dict[str, Any]:
    payload = api_json("GET", jira_board_api_url(session, board_id), session.token)
    if not isinstance(payload, dict):
        raise ToolError("unexpected Jira board response")
    board = normalize_board(payload)
    if board.get("type") and board["type"] != "scrum":
        raise ToolError(
            f"board {board_id} is type {board['type']}; sprint operations require a scrum board"
        )
    return board


def fetch_board_sprints(
    session: Session,
    board_id: int,
    *,
    state: str = "",
) -> list[dict[str, Any]]:
    start_at = 0
    sprints: list[dict[str, Any]] = []
    while True:
        payload = api_json(
            "GET",
            jira_board_sprints_api_url(
                session,
                board_id,
                start_at=start_at,
                max_results=50,
                state=state,
            ),
            session.token,
        )
        if not isinstance(payload, dict):
            raise ToolError("unexpected Jira sprint response")
        values = payload.get("values")
        if not isinstance(values, list):
            values = []
        sprints.extend(
            normalize_sprint(item) for item in values if isinstance(item, dict)
        )
        if bool(payload.get("isLast")):
            break
        start_at += int(payload.get("maxResults") or len(values) or 50)
        if not values:
            break
    return [sprint for sprint in sprints if sprint.get("id")]


def fetch_sprint(session: Session, sprint_id: int) -> dict[str, Any]:
    payload = api_json("GET", jira_sprint_api_url(session, sprint_id), session.token)
    if not isinstance(payload, dict):
        raise ToolError("unexpected Jira sprint response")
    sprint = normalize_sprint(payload)
    if not sprint.get("id"):
        raise ToolError(f"unexpected Jira sprint response for sprint {sprint_id}")
    return sprint


def collect_board_sprints(
    base_url: str,
    *,
    project_key_or_id: str = "",
    board_id: int | None = None,
    state: str = "",
) -> tuple[Session, list[dict[str, Any]]]:
    if board_id is not None:
        session = load_session(site_root(base_url))
        board = fetch_board(session, board_id)
        boards = [board]
        if project_key_or_id:
            _, project_boards = fetch_scrum_boards(
                base_url, project_key_or_id=project_key_or_id
            )
            if board["id"] not in {item["id"] for item in project_boards}:
                raise ToolError(
                    f"board {board_id} is not a scrum board for project {project_key_or_id}"
                )
    else:
        session, boards = fetch_scrum_boards(
            base_url, project_key_or_id=project_key_or_id
        )
        if project_key_or_id and not boards:
            raise ToolError(f"no scrum boards found for project {project_key_or_id}")
    board_sprints: list[dict[str, Any]] = []
    for board in boards:
        sprints = fetch_board_sprints(session, int(board["id"]), state=state)
        board_sprints.append({**board, "sprints": sprints})
    return session, board_sprints


def format_board_label(board: dict[str, Any]) -> str:
    return f"{board.get('name') or ''} ({board.get('id') or ''})".strip()


def format_sprint_label(sprint: dict[str, Any]) -> str:
    return f"{sprint.get('name') or ''} ({sprint.get('id') or ''})".strip()


def resolve_current_sprint(
    base_url: str,
    *,
    project_key_or_id: str,
    board_id: int | None = None,
) -> tuple[Session, dict[str, Any], dict[str, Any]]:
    session, board_sprints = collect_board_sprints(
        base_url,
        project_key_or_id=project_key_or_id,
        board_id=board_id,
        state="active",
    )
    active_pairs = [
        (board, sprint)
        for board in board_sprints
        for sprint in board.get("sprints", [])
        if isinstance(sprint, dict)
        and str(sprint.get("state") or "").casefold() == "active"
    ]
    if not active_pairs:
        if board_id is not None:
            raise ToolError(f"no active sprint found for board {board_id}")
        raise ToolError(f"no active sprint found for project {project_key_or_id}")
    if len(active_pairs) > 1:
        rendered = "; ".join(
            f"{format_board_label(board)} -> {format_sprint_label(sprint)}"
            for board, sprint in active_pairs
        )
        raise ToolError(
            "multiple active sprints match the requested context: "
            + rendered
            + ". Likely next step: rerun with --board or --sprint"
        )
    board, sprint = active_pairs[0]
    return session, board, sprint


def resolve_assignment_sprint(
    base_url: str,
    *,
    project_key_or_id: str = "",
    board_id: int | None = None,
    sprint_id: int | None = None,
    current: bool = False,
) -> tuple[Session, dict[str, Any], dict[str, Any]]:
    if current:
        if not project_key_or_id and board_id is None:
            raise ToolError("--current requires --project or --board")
        return resolve_current_sprint(
            base_url,
            project_key_or_id=project_key_or_id,
            board_id=board_id,
        )
    if sprint_id is None:
        raise ToolError("choose exactly one of --current or --sprint")
    session = load_session(site_root(base_url))
    sprint = fetch_sprint(session, sprint_id)
    origin_board_id = int(str(sprint.get("originBoardId") or "0") or "0")
    if not origin_board_id:
        raise ToolError(f"sprint {sprint_id} did not report an origin board")
    board = fetch_board(session, origin_board_id)
    if board_id is not None and str(board_id) != board["id"]:
        raise ToolError(
            f"sprint {sprint_id} belongs to board {board['id']}, not board {board_id}"
        )
    if project_key_or_id:
        _, project_boards = fetch_scrum_boards(
            base_url, project_key_or_id=project_key_or_id
        )
        if board["id"] not in {item["id"] for item in project_boards}:
            raise ToolError(
                f"sprint {sprint_id} does not belong to a scrum board for project {project_key_or_id}"
            )
    return session, board, sprint


def field_lookup_map(fields: dict[str, dict[str, Any]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for field_id, meta in fields.items():
        lookup[field_id] = field_id
        lookup[_normalize_lookup(field_id)] = field_id
        name = str(meta.get("name") or "")
        if name:
            lookup[_normalize_lookup(name)] = field_id
    return lookup


def resolve_field_id(fields: dict[str, dict[str, Any]], raw: str) -> str:
    lookup = field_lookup_map(fields)
    field_id = lookup.get(raw) or lookup.get(_normalize_lookup(raw))
    if field_id:
        return field_id
    raise ToolError(
        f"unknown Jira field: {raw}. Likely next step: run the relevant `gotta jira fields ...` command"
    )


def parse_field_assignments(values: list[str] | None) -> dict[str, list[str]]:
    assignments: dict[str, list[str]] = {}
    for item in values or []:
        if "=" not in item:
            raise ToolError(f"invalid --field assignment: {item}. Expected FIELD=VALUE")
        key, value = item.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key:
            raise ToolError(f"invalid --field assignment: {item}. Missing field name")
        assignments.setdefault(normalized_key, []).append(value)
    return assignments


def select_allowed_value(
    field_name: str, raw: str, allowed_values: list[dict[str, Any]]
) -> dict[str, Any]:
    candidate = _normalize_lookup(raw)
    for item in allowed_values:
        for key in ("id", "key", "name", "value", "displayName", "accountId"):
            value = str(item.get(key) or "")
            if value and (_normalize_lookup(value) == candidate or value == raw):
                return item
    options = [
        item.get("name") or item.get("value") or item.get("key") or item.get("id")
        for item in allowed_values
    ]
    rendered = ", ".join(str(option) for option in options if option)
    raise ToolError(
        f"invalid value for Jira field {field_name}: {raw}"
        + (f". Allowed values: {rendered}" if rendered else "")
    )


def coerce_field_value(
    field_id: str, meta: dict[str, Any], raw_values: list[str]
) -> Any:
    schema = meta.get("schema")
    if not isinstance(schema, dict):
        schema = {}
    field_name = str(meta.get("name") or field_id)
    allowed_values = meta.get("allowedValues")
    if not isinstance(allowed_values, list):
        allowed_values = []

    field_type = str(schema.get("type") or "")
    item_type = str(schema.get("items") or "")
    system = str(schema.get("system") or "")

    if field_id == "assignee" or system == "assignee":
        return {"accountId": raw_values[-1]}
    if field_id == "parent" or system == "parent":
        return {"key": normalize_issue_key(raw_values[-1])}
    if field_id == "labels" or (field_type == "array" and item_type == "string"):
        values: list[str] = []
        for raw in raw_values:
            values.extend([item.strip() for item in raw.split(",") if item.strip()])
        return values
    if field_id == "components" or (field_type == "array" and item_type == "component"):
        items: list[dict[str, Any]] = []
        for raw in raw_values:
            for candidate in [item.strip() for item in raw.split(",") if item.strip()]:
                if allowed_values:
                    selected = select_allowed_value(
                        field_name, candidate, allowed_values
                    )
                    if selected.get("id"):
                        items.append({"id": selected["id"]})
                    elif selected.get("name"):
                        items.append({"name": selected["name"]})
                else:
                    items.append({"name": candidate})
        return items
    if field_type == "array":
        split_values: list[str] = []
        for raw in raw_values:
            split_values.extend(
                [item.strip() for item in raw.split(",") if item.strip()]
            )
        if item_type == "option":
            if not allowed_values:
                return [{"value": candidate} for candidate in split_values]
            return [
                {"id": selected["id"]}
                if selected.get("id")
                else {
                    "value": selected.get("value") or selected.get("name") or candidate
                }
                for candidate in split_values
                for selected in [
                    select_allowed_value(field_name, candidate, allowed_values)
                ]
            ]
        return split_values
    if field_id == "priority" or system == "priority" or field_type == "priority":
        if not allowed_values:
            return {"name": raw_values[-1]}
        selected = select_allowed_value(field_name, raw_values[-1], allowed_values)
        if selected.get("id"):
            return {"id": selected["id"]}
        return {"name": selected.get("name") or raw_values[-1]}
    if field_type in {"issuetype", "issueType"} or system == "issuetype":
        if not allowed_values:
            return {"name": raw_values[-1]}
        selected = select_allowed_value(field_name, raw_values[-1], allowed_values)
        return {"id": selected.get("id") or raw_values[-1]}
    if field_type == "option":
        if not allowed_values:
            return {"value": raw_values[-1]}
        selected = select_allowed_value(field_name, raw_values[-1], allowed_values)
        if selected.get("id"):
            return {"id": selected["id"]}
        if selected.get("value"):
            return {"value": selected["value"]}
        return {"value": raw_values[-1]}
    if field_type == "user":
        return {"accountId": raw_values[-1]}
    if field_type == "number":
        try:
            return int(raw_values[-1])
        except ValueError:
            try:
                return float(raw_values[-1])
            except ValueError as exc:
                raise ToolError(
                    f"invalid numeric value for Jira field {field_name}: {raw_values[-1]}"
                ) from exc
    if field_type == "string":
        return raw_values[-1]
    candidate = raw_values[-1].strip()
    if candidate.startswith("{") or candidate.startswith("["):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return raw_values[-1]
    return raw_values[-1]


def normalize_api_error_message(exc: ToolError) -> str:
    message = str(exc)
    match = re.search(r"failed with \d+: (.+)$", message)
    if not match:
        return message
    body = match.group(1).strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return message
    details: list[str] = []
    error_messages = payload.get("errorMessages")
    if isinstance(error_messages, list):
        details.extend(
            str(item) for item in error_messages if isinstance(item, str) and item
        )
    errors = payload.get("errors")
    if isinstance(errors, dict):
        for field, detail in errors.items():
            if not detail:
                continue
            details.append(f"{field}: {detail}")
    if not details:
        return message
    return "; ".join(details)


def named_object(value: Any, *, include_key: bool = False) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    name = value.get("name")
    if isinstance(name, str) and name:
        result["name"] = name
    if include_key:
        key = value.get("key")
        if isinstance(key, str) and key:
            result["key"] = key
    return result or None


def normalize_person(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    display_name = value.get("displayName")
    account_id = value.get("accountId")
    if isinstance(display_name, str) and display_name:
        result["displayName"] = display_name
    if isinstance(account_id, str) and account_id:
        result["accountId"] = account_id
    return result or None


def adf_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text") or "")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "mention":
        attrs = node.get("attrs")
        if isinstance(attrs, dict):
            text = attrs.get("text")
            if isinstance(text, str) and text:
                return text
        return ""
    if node_type == "emoji":
        attrs = node.get("attrs")
        if isinstance(attrs, dict):
            text = attrs.get("text") or attrs.get("shortName")
            if isinstance(text, str):
                return text
        return ""
    parts: list[str] = []
    for child in (
        node.get("content", []) if isinstance(node.get("content"), list) else []
    ):
        parts.append(adf_text(child))
    text = "".join(parts)
    if node_type in {"paragraph", "heading", "blockquote", "codeBlock"} and text:
        return text + "\n\n"
    if node_type in {"listItem"}:
        return text
    if node_type in {"bulletList", "orderedList", "panel"} and text:
        return text + "\n"
    return text


def apply_text_marks(text: str, marks: Any) -> str:
    if not isinstance(marks, list):
        return text
    rendered = text
    link_href = ""
    for mark in marks:
        if not isinstance(mark, dict):
            continue
        mark_type = mark.get("type")
        if mark_type == "strong":
            rendered = f"**{rendered}**"
        elif mark_type == "em":
            rendered = f"*{rendered}*"
        elif mark_type == "code":
            rendered = f"`{rendered}`"
        elif mark_type == "link":
            attrs = mark.get("attrs")
            if isinstance(attrs, dict):
                href = attrs.get("href")
                if isinstance(href, str) and href:
                    link_href = href
    if link_href:
        return f"[{rendered}]({link_href})"
    return rendered


def render_adf_inline(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "text":
        return apply_text_marks(str(node.get("text") or ""), node.get("marks"))
    if node_type == "hardBreak":
        return "  \n"
    if node_type == "mention":
        attrs = node.get("attrs")
        if isinstance(attrs, dict):
            text = attrs.get("text")
            if isinstance(text, str) and text:
                return text
        return "@mention"
    if node_type == "emoji":
        attrs = node.get("attrs")
        if isinstance(attrs, dict):
            text = attrs.get("text") or attrs.get("shortName")
            if isinstance(text, str):
                return text
        return ":emoji:"
    if node_type == "inlineCard":
        attrs = node.get("attrs")
        if isinstance(attrs, dict):
            url = attrs.get("url")
            if isinstance(url, str) and url:
                return f"<{url}>"
    if node_type == "codeBlock":
        content = "".join(render_adf_inline(child) for child in node.get("content", []))
        return f"\n```\n{content}\n```\n"
    return "".join(render_adf_inline(child) for child in node.get("content", []))


def render_list_item(node: Any, *, ordered: bool, index: int, level: int) -> str:
    prefix = f"{index}. " if ordered else "- "
    indent = "  " * level
    lines: list[str] = []
    children = node.get("content", []) if isinstance(node, dict) else []
    if not isinstance(children, list):
        children = []
    first_block = True
    for child in children:
        if not isinstance(child, dict):
            continue
        child_type = child.get("type")
        if child_type == "paragraph":
            text = "".join(render_adf_inline(part) for part in child.get("content", []))
            if text.strip():
                if first_block:
                    lines.append(f"{indent}{prefix}{text.strip()}")
                    first_block = False
                else:
                    lines.append(f"{indent}  {text.strip()}")
        elif child_type == "bulletList":
            nested = render_adf_block(child, level=level + 1).rstrip()
            if nested:
                lines.append(nested)
        elif child_type == "orderedList":
            nested = render_adf_block(child, level=level + 1).rstrip()
            if nested:
                lines.append(nested)
        else:
            block = render_adf_block(child, level=level + 1).rstrip()
            if block:
                if first_block:
                    lines.append(f"{indent}{prefix}{block}")
                    first_block = False
                else:
                    lines.append(block)
    if not lines:
        lines.append(f"{indent}{prefix}")
    return "\n".join(lines)


def render_adf_block(node: Any, *, level: int = 0) -> str:
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    content = node.get("content", [])
    if not isinstance(content, list):
        content = []
    if node_type == "doc":
        blocks = [render_adf_block(child, level=level).rstrip() for child in content]
        return "\n\n".join(block for block in blocks if block).strip() + "\n"
    if node_type == "paragraph":
        text = "".join(render_adf_inline(child) for child in content).strip()
        return text
    if node_type == "heading":
        attrs = node.get("attrs")
        depth = 2
        if isinstance(attrs, dict):
            maybe_level = attrs.get("level")
            if isinstance(maybe_level, int):
                depth = max(1, min(6, maybe_level))
        text = "".join(render_adf_inline(child) for child in content).strip()
        return f"{'#' * depth} {text}".rstrip()
    if node_type == "bulletList":
        return "\n".join(
            render_list_item(child, ordered=False, index=index, level=level)
            for index, child in enumerate(content, start=1)
            if isinstance(child, dict)
        )
    if node_type == "orderedList":
        return "\n".join(
            render_list_item(child, ordered=True, index=index, level=level)
            for index, child in enumerate(content, start=1)
            if isinstance(child, dict)
        )
    if node_type == "blockquote":
        inner = []
        for child in content:
            block = render_adf_block(child, level=level).strip()
            if block:
                inner.extend(
                    f"> {line}" if line else ">" for line in block.splitlines()
                )
        return "\n".join(inner)
    if node_type == "codeBlock":
        attrs = node.get("attrs")
        language = ""
        if isinstance(attrs, dict):
            maybe_language = attrs.get("language")
            if isinstance(maybe_language, str):
                language = maybe_language
        text = "".join(adf_text(child) for child in content).rstrip("\n")
        return f"```{language}\n{text}\n```"
    if node_type == "panel":
        inner = []
        for child in content:
            block = render_adf_block(child, level=level).strip()
            if block:
                inner.extend(
                    f"> {line}" if line else ">" for line in block.splitlines()
                )
        return "\n".join(inner)
    return "".join(render_adf_inline(child) for child in content).strip()


def adf_doc(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": blocks}


def adf_text_node(
    text: str, *, marks: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def _clone_mark(mark: dict[str, Any]) -> dict[str, Any]:
    cloned: dict[str, Any] = {"type": mark["type"]}
    attrs = mark.get("attrs")
    if isinstance(attrs, dict):
        cloned["attrs"] = dict(attrs)
    return cloned


def _apply_mark(
    nodes: list[dict[str, Any]], mark: dict[str, Any]
) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("type") != "text":
            applied.append(node)
            continue
        marks = node.get("marks")
        if not isinstance(marks, list):
            marks = []
        applied.append(
            {
                "type": "text",
                "text": node.get("text") or "",
                "marks": [*_clone_mark_list(marks), _clone_mark(mark)],
            }
        )
    return applied


def _clone_mark_list(marks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _clone_mark(mark)
        for mark in marks
        if isinstance(mark, dict) and mark.get("type")
    ]


def parse_markdown_inline(text: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        if text.startswith("`", index):
            end = text.find("`", index + 1)
            if end != -1:
                nodes.append(
                    adf_text_node(text[index + 1 : end], marks=[{"type": "code"}])
                )
                index = end + 1
                continue
        link_match = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", text[index:])
        if link_match:
            inner_nodes = parse_markdown_inline(link_match.group(1))
            nodes.extend(
                _apply_mark(
                    inner_nodes,
                    {"type": "link", "attrs": {"href": link_match.group(2)}},
                )
            )
            index += len(link_match.group(0))
            continue
        strong_match = re.match(r"\*\*([^*]+)\*\*", text[index:])
        if strong_match:
            nodes.extend(
                _apply_mark(
                    parse_markdown_inline(strong_match.group(1)), {"type": "strong"}
                )
            )
            index += len(strong_match.group(0))
            continue
        em_match = re.match(r"(?<!\*)\*([^*]+)\*(?!\*)", text[index:])
        if em_match:
            nodes.extend(
                _apply_mark(parse_markdown_inline(em_match.group(1)), {"type": "em"})
            )
            index += len(em_match.group(0))
            continue
        underline_match = re.match(r"_([^_]+)_", text[index:])
        if underline_match:
            nodes.extend(
                _apply_mark(
                    parse_markdown_inline(underline_match.group(1)), {"type": "em"}
                )
            )
            index += len(underline_match.group(0))
            continue
        next_specials = [
            pos
            for pos in (
                text.find("`", index + 1),
                text.find("[", index + 1),
                text.find("*", index + 1),
                text.find("_", index + 1),
            )
            if pos != -1
        ]
        next_index = min(next_specials) if next_specials else len(text)
        nodes.append(adf_text_node(text[index:next_index]))
        index = next_index
    return [node for node in nodes if node.get("text") not in {"", None}]


def paragraph_from_markdown(text: str) -> dict[str, Any]:
    lines = text.split("\n")
    content: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        content.extend(parse_markdown_inline(line))
        if index < len(lines) - 1:
            content.append({"type": "hardBreak"})
    return {"type": "paragraph", "content": content or [adf_text_node("")]}


def code_block_from_markdown(text: str, *, language: str = "") -> dict[str, Any]:
    node: dict[str, Any] = {"type": "codeBlock", "content": [adf_text_node(text)]}
    if language:
        node["attrs"] = {"language": language}
    return node


def list_block_from_markdown(lines: list[str], *, ordered: bool) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    pattern = r"^\s*\d+[.)]\s+" if ordered else r"^\s*[-*+]\s+"
    for raw in lines:
        body = re.sub(pattern, "", raw, count=1).strip()
        content.append(
            {
                "type": "listItem",
                "content": [paragraph_from_markdown(body)],
            }
        )
    return {"type": "orderedList" if ordered else "bulletList", "content": content}


def markdown_to_adf(markdown: str) -> dict[str, Any]:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        heading_match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
        if heading_match:
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": len(heading_match.group(1))},
                    "content": parse_markdown_inline(
                        re.sub(r"[ \t]+#+\s*$", "", heading_match.group(2)).strip()
                    ),
                }
            )
            index += 1
            continue
        if index + 1 < len(lines) and re.match(r"^\s*=+\s*$", lines[index + 1]):
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": 1},
                    "content": parse_markdown_inline(line.strip()),
                }
            )
            index += 2
            continue
        if index + 1 < len(lines) and re.match(r"^\s*-+\s*$", lines[index + 1]):
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": parse_markdown_inline(line.strip()),
                }
            )
            index += 2
            continue
        fence_match = re.match(r"^\s*```([A-Za-z0-9_-]+)?\s*$", line)
        if fence_match:
            language = str(fence_match.group(1) or "")
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not re.match(r"^\s*```\s*$", lines[index]):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(
                code_block_from_markdown("\n".join(code_lines), language=language)
            )
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            list_lines: list[str] = []
            while index < len(lines) and re.match(r"^\s*[-*+]\s+", lines[index]):
                list_lines.append(lines[index])
                index += 1
            blocks.append(list_block_from_markdown(list_lines, ordered=False))
            continue
        if re.match(r"^\s*\d+[.)]\s+", line):
            list_lines = []
            while index < len(lines) and re.match(r"^\s*\d+[.)]\s+", lines[index]):
                list_lines.append(lines[index])
                index += 1
            blocks.append(list_block_from_markdown(list_lines, ordered=True))
            continue
        paragraph_lines = [line]
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if not candidate.strip():
                break
            if re.match(r"^\s{0,3}(#{1,6})\s+", candidate):
                break
            if re.match(r"^\s*```", candidate):
                break
            if re.match(r"^\s*[-*+]\s+", candidate):
                break
            if re.match(r"^\s*\d+[.)]\s+", candidate):
                break
            if index + 1 < len(lines) and re.match(
                r"^\s*(=+|-+)\s*$", lines[index + 1]
            ):
                break
            paragraph_lines.append(candidate)
            index += 1
        blocks.append(paragraph_from_markdown("\n".join(paragraph_lines)))
    return adf_doc(blocks)


def normalize_heading_title(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def heading_line_parts(line: str, next_line: str = "") -> tuple[int, str, int] | None:
    atx = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
    if atx:
        title = re.sub(r"[ \t]+#+\s*$", "", atx.group(2)).strip()
        return len(atx.group(1)), title, 1
    if next_line and re.match(r"^\s*=+\s*$", next_line):
        return 1, line.strip(), 2
    if next_line and re.match(r"^\s*-+\s*$", next_line):
        return 2, line.strip(), 2
    return None


def find_markdown_section(markdown: str, heading: str) -> tuple[int, int, int] | None:
    lines = markdown.splitlines()
    target = normalize_heading_title(heading)
    index = 0
    while index < len(lines):
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        heading_info = heading_line_parts(lines[index], next_line)
        if heading_info is None:
            index += 1
            continue
        level, title, consumed = heading_info
        if normalize_heading_title(title) != target:
            index += consumed
            continue
        end = index + consumed
        while end < len(lines):
            next_candidate = lines[end + 1] if end + 1 < len(lines) else ""
            next_heading = heading_line_parts(lines[end], next_candidate)
            if next_heading is not None and next_heading[0] <= level:
                break
            end += 1
        return index, end, level
    return None


def render_markdown_section(heading: str, body: str, *, level: int = 2) -> str:
    normalized_body = body.strip()
    if normalized_body:
        return f"{'#' * level} {heading}\n\n{normalized_body}\n"
    return f"{'#' * level} {heading}\n"


def append_markdown_section(markdown: str, heading: str, body: str) -> str:
    base = markdown.rstrip()
    section = render_markdown_section(heading, body)
    if not base:
        return section
    return f"{base}\n\n{section}"


def prepend_markdown_section(markdown: str, heading: str, body: str) -> str:
    section = render_markdown_section(heading, body).rstrip()
    base = markdown.lstrip()
    if not base:
        return section + "\n"
    return f"{section}\n\n{base}"


def upsert_markdown_section(markdown: str, heading: str, body: str) -> str:
    section = find_markdown_section(markdown, heading)
    if section is None:
        return append_markdown_section(markdown, heading, body)
    start, end, level = section
    lines = markdown.splitlines()
    replacement = (
        render_markdown_section(heading, body, level=level).rstrip("\n").splitlines()
    )
    updated_lines = [*lines[:start], *replacement, *lines[end:]]
    return "\n".join(updated_lines).strip() + "\n"


def issue_url(base_url: str, issue_key: str) -> str:
    return f"{site_root(base_url)}/browse/{issue_key}"


def _issue_locator(issue_key: str) -> str:
    key = str(issue_key or "").strip().upper()
    return f"jira:{key}" if key else ""


def _search_locator(subcommand: str, query: str) -> str:
    command = str(subcommand or "search").strip().lower() or "search"
    text = " ".join(str(query or "").split()).strip()
    return f"jira:{command} {text}".strip()


def _with_jira_visibility(
    payload: dict[str, Any],
    *,
    subcommand: str,
    locator: str = "",
) -> dict[str, Any]:
    return with_visibility_metadata(
        dict(payload),
        provider="jira",
        subcommand=subcommand,
        locator=locator,
    )


def _visibility_security_name(payload: Mapping[str, Any]) -> str:
    for key in ("security", "securityLevel"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            name = str(value.get("name") or value.get("id") or "").strip()
            if name:
                return name
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _visibility_issue_like_payload(payload: Mapping[str, Any]) -> bool:
    if str(payload.get("issueUrl") or "").strip():
        return True
    if (
        str(payload.get("siteUrl") or "").strip()
        and str(payload.get("key") or "").strip()
    ):
        return True
    project = payload.get("project")
    if isinstance(project, Mapping) and str(project.get("key") or "").strip():
        return True
    return False


def classify_visibility(
    payload: Any,
    subcommand: str,
    locator: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        payload = {}
    security_name = _visibility_security_name(payload)
    if security_name:
        return visibility_metadata(
            level="restricted",
            boundary="same_company",
            confidence="high",
            basis=[
                "provider=jira",
                f"issue.security={security_name}",
            ],
        )
    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, Mapping):
                continue
            security_name = _visibility_security_name(item)
            if security_name:
                return visibility_metadata(
                    level="restricted",
                    boundary="same_company",
                    confidence="high",
                    basis=[
                        "provider=jira",
                        f"search.result.security={security_name}",
                    ],
                )
        if results:
            return visibility_metadata(
                level="restricted",
                boundary="same_company",
                confidence="medium",
                basis=[
                    "provider=jira",
                    "search.results=present",
                    "classification=authenticated_jira_surface",
                ],
            )
    if _visibility_issue_like_payload(payload):
        return visibility_metadata(
            level="restricted",
            boundary="same_company",
            confidence="medium",
            basis=[
                "provider=jira",
                "issue.url=present",
                "classification=authenticated_jira_surface",
            ],
        )
    if subcommand in {"get", "search", "jql"} or locator.startswith("jira:"):
        return visibility_metadata(
            level="restricted",
            boundary="same_company",
            confidence="medium",
            basis=[
                "provider=jira",
                f"subcommand={subcommand or 'default'}",
                "classification=authenticated_jira_surface",
            ],
        )
    return unknown_visibility(provider="jira")


def normalize_issue(
    payload: dict[str, Any], *, base_url: str, include_description: bool
) -> dict[str, Any]:
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    description_adf = fields.get("description") if include_description else None
    description_text = adf_text(description_adf).strip() if include_description else ""
    return {
        "siteUrl": site_root(base_url),
        "issueUrl": issue_url(base_url, str(payload.get("key") or "")),
        "id": str(payload.get("id") or ""),
        "key": str(payload.get("key") or ""),
        "summary": str(fields.get("summary") or ""),
        "status": named_object(fields.get("status")),
        "issueType": named_object(fields.get("issuetype")),
        "security": named_object(fields.get("security")),
        "project": named_object(fields.get("project"), include_key=True),
        "priority": named_object(fields.get("priority")),
        "assignee": normalize_person(fields.get("assignee")),
        "reporter": normalize_person(fields.get("reporter")),
        "labels": [item for item in fields.get("labels", []) if isinstance(item, str)],
        "created": str(fields.get("created") or ""),
        "updated": str(fields.get("updated") or ""),
        "descriptionText": description_text,
        "descriptionAdf": description_adf if include_description else None,
    }


def meta_issue(envelope: dict[str, Any]) -> dict[str, Any]:
    key = str(envelope.get("key") or "")
    return _with_jira_visibility(
        {
            "siteUrl": envelope.get("siteUrl"),
            "issueUrl": envelope.get("issueUrl"),
            "id": envelope.get("id"),
            "key": key,
            "summary": envelope.get("summary"),
            "status": envelope.get("status"),
            "issueType": envelope.get("issueType"),
            "security": envelope.get("security"),
            "project": envelope.get("project"),
            "priority": envelope.get("priority"),
            "assignee": envelope.get("assignee"),
            "reporter": envelope.get("reporter"),
            "labels": envelope.get("labels"),
            "created": envelope.get("created"),
            "updated": envelope.get("updated"),
        },
        subcommand="get",
        locator=_issue_locator(key),
    )


def jira_issue_api_url(session: Session, issue_key: str, fields: list[str]) -> str:
    query = urllib.parse.urlencode({"fields": ",".join(fields)})
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/"
        f"issue/{urllib.parse.quote(issue_key)}?{query}"
    )


def jira_issue_mutation_api_url(session: Session, issue_key: str) -> str:
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/"
        f"issue/{urllib.parse.quote(issue_key)}"
    )


def jira_search_api_url(session: Session) -> str:
    return f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/search/jql"


def fetch_issue(issue_ref: IssueRef, *, fields: list[str]) -> dict[str, Any]:
    session = load_session(issue_ref.base_url)
    payload = api_json(
        "GET",
        jira_issue_api_url(session, issue_ref.issue_key, fields),
        session.token,
    )
    if not isinstance(payload, dict):
        raise ToolError("unexpected Jira issue response")
    return _with_jira_visibility(
        normalize_issue(payload, base_url=session.base_url, include_description=True),
        subcommand="get",
        locator=_issue_locator(issue_ref.issue_key),
    )


def jql_string_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_plaintext_jql(query: str) -> str:
    literal = jql_string_literal(query)
    return f"(summary ~ {literal} OR text ~ {literal}) ORDER BY updated DESC"


def looks_like_raw_jql(query: str) -> bool:
    normalized = " ".join(query.strip().split())
    if not normalized:
        return False
    if re.search(r"\bORDER\s+BY\b", normalized, re.IGNORECASE):
        return True
    if re.search(
        rf"\b{OBVIOUS_JQL_FIELD_PATTERN}\b\s*(?:=|!=|>=|<=|>|<|~|!~)\s*\S",
        normalized,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        rf"\b{OBVIOUS_JQL_FIELD_PATTERN}\b\s+(?:NOT\s+)?IN\s*\(",
        normalized,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        rf"\b{OBVIOUS_JQL_FIELD_PATTERN}\b\s+IS\s+(?:NOT\s+)?(?:EMPTY|NULL)\b",
        normalized,
        re.IGNORECASE,
    ):
        return True
    return False


def search_jira(
    *,
    base_url: str,
    jql: str,
    limit: int,
    cursor: str | None,
    fields: list[str],
    subcommand: str = "search",
) -> dict[str, Any]:
    session = load_session(base_url)
    payload = {
        "jql": jql,
        "maxResults": limit,
        "fields": fields,
    }
    if cursor:
        payload["nextPageToken"] = cursor
    response = api_json(
        "POST", jira_search_api_url(session), session.token, payload=payload
    )
    if not isinstance(response, dict):
        raise ToolError("unexpected Jira search response")
    issues = response.get("issues")
    if not isinstance(issues, list):
        issues = []
    results = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        normalized = {
            key: value
            for key, value in normalize_issue(
                item, base_url=session.base_url, include_description=False
            ).items()
            if key
            in {
                "siteUrl",
                "issueUrl",
                "id",
                "key",
                "summary",
                "status",
                "issueType",
                "security",
                "project",
                "priority",
                "assignee",
                "labels",
                "updated",
            }
        }
        results.append(
            _with_jira_visibility(
                normalized,
                subcommand=subcommand,
                locator=_issue_locator(str(normalized.get("key") or "")),
            )
        )
    return _with_jira_visibility(
        {
            "query": jql,
            "limit": limit,
            "requestedNext": cursor,
            "next": str(response.get("nextPageToken") or ""),
            "size": len(results),
            "results": results,
        },
        subcommand=subcommand,
        locator=_search_locator(subcommand, jql),
    )


def default_field_metadata(field_id: str) -> dict[str, Any]:
    defaults: dict[str, dict[str, Any]] = {
        "summary": {
            "id": "summary",
            "name": "Summary",
            "schema": {"type": "string", "system": "summary"},
        },
        "priority": {
            "id": "priority",
            "name": "Priority",
            "schema": {"type": "priority", "system": "priority"},
        },
        "assignee": {
            "id": "assignee",
            "name": "Assignee",
            "schema": {"type": "user", "system": "assignee"},
        },
        "parent": {
            "id": "parent",
            "name": "Parent",
            "schema": {"type": "issuelink", "system": "parent"},
        },
        "labels": {
            "id": "labels",
            "name": "Labels",
            "schema": {"type": "array", "system": "labels", "items": "string"},
        },
        "components": {
            "id": "components",
            "name": "Components",
            "schema": {"type": "array", "system": "components", "items": "component"},
        },
    }
    payload = defaults.get(
        field_id, {"id": field_id, "name": field_id, "schema": {"type": "string"}}
    )
    return normalize_field_metadata(field_id, payload)


def metadata_for_field(
    fields: dict[str, dict[str, Any]], field_id: str
) -> dict[str, Any]:
    return fields.get(field_id) or default_field_metadata(field_id)


def apply_named_field(
    payload_fields: dict[str, Any],
    fields: dict[str, dict[str, Any]],
    *,
    field_id: str,
    raw_values: list[str],
) -> None:
    payload_fields[field_id] = coerce_field_value(
        field_id, metadata_for_field(fields, field_id), raw_values
    )


def apply_generic_fields(
    payload_fields: dict[str, Any],
    fields: dict[str, dict[str, Any]],
    assignments: dict[str, list[str]],
) -> None:
    for raw_field, raw_values in assignments.items():
        field_id = resolve_field_id(fields, raw_field)
        payload_fields[field_id] = coerce_field_value(
            field_id, fields[field_id], raw_values
        )


def render_projects_summary(
    projects: list[dict[str, Any]],
    *,
    total_count: int,
    shown_count: int,
    offset: int,
    next_offset: int | None,
) -> str:
    lines = [
        _listing_count_line(
            "projects",
            total_count=total_count,
            shown_count=shown_count,
            offset=offset,
            next_offset=next_offset,
        )
    ]
    for project in projects:
        key = project.get("key") or ""
        name = project.get("name") or ""
        project_type = project.get("projectTypeKey") or ""
        line = f"- {key}"
        details = [name] if name else []
        if project_type:
            details.append(f"type `{project_type}`")
        if details:
            line += " - " + ", ".join(details)
        lines.append(line)
    return "\n".join(lines) + "\n"


def render_issue_types_summary(
    project_key: str,
    issue_types: list[dict[str, Any]],
    *,
    total_count: int,
    shown_count: int,
    offset: int,
    next_offset: int | None,
) -> str:
    lines = [
        f"project: {project_key}",
        _listing_count_line(
            "issue_types",
            total_count=total_count,
            shown_count=shown_count,
            offset=offset,
            next_offset=next_offset,
        ),
    ]
    for item in issue_types:
        line = f"- {item.get('name') or ''}"
        details = [f"id `{item.get('id') or ''}`"] if item.get("id") else []
        if item.get("subtask"):
            details.append("subtask")
        if item.get("description"):
            details.append(str(item["description"]))
        if details:
            line += " - " + ", ".join(details)
        lines.append(line)
    return "\n".join(lines) + "\n"


def render_fields_summary(fields: dict[str, dict[str, Any]], *, context: str) -> str:
    lines = [f"context: {context}", f"fields: {len(fields)}"]
    for field_id, meta in sorted(
        fields.items(), key=lambda item: str(item[1].get("name") or item[0]).casefold()
    ):
        line = f"- {meta.get('name') or field_id} - id `{field_id}`"
        details: list[str] = []
        if meta.get("required"):
            details.append("required")
        schema = meta.get("schema")
        if isinstance(schema, dict):
            schema_type = str(schema.get("type") or "")
            if schema_type:
                details.append(f"type `{schema_type}`")
            items = str(schema.get("items") or "")
            if items:
                details.append(f"items `{items}`")
        allowed = meta.get("allowedValues")
        if isinstance(allowed, list) and allowed:
            labels = [
                str(
                    item.get("name")
                    or item.get("value")
                    or item.get("displayName")
                    or item.get("id")
                    or ""
                )
                for item in allowed[:8]
                if isinstance(item, dict)
            ]
            rendered = ", ".join(label for label in labels if label)
            if rendered:
                more = len(allowed) - len(labels)
                details.append(
                    "allowed " + rendered + (f" (+{more} more)" if more > 0 else "")
                )
        if details:
            line += " - " + "; ".join(details)
        lines.append(line)
    return "\n".join(lines) + "\n"


def render_transitions_summary(
    issue_key: str,
    transitions: list[dict[str, Any]],
    *,
    total_count: int,
    shown_count: int,
    offset: int,
    next_offset: int | None,
) -> str:
    lines = [
        f"issue: {issue_key}",
        _listing_count_line(
            "transitions",
            total_count=total_count,
            shown_count=shown_count,
            offset=offset,
            next_offset=next_offset,
        ),
    ]
    for item in transitions:
        line = f"- {item.get('name') or ''} - id `{item.get('id') or ''}`"
        to_name = (item.get("to") or {}).get("name") or ""
        if to_name:
            line += f", to `{to_name}`"
        fields = item.get("fields")
        if isinstance(fields, dict) and fields:
            required = [
                str(meta.get("name") or field_id)
                for field_id, meta in fields.items()
                if isinstance(meta, dict) and meta.get("required")
            ]
            if required:
                line += f", required fields: {', '.join(required)}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def render_sprints_summary(
    *,
    project_key_or_id: str = "",
    board_sprints: list[dict[str, Any]],
    paging_unit: str,
    total_count: int,
    shown_count: int,
    offset: int,
    next_offset: int | None,
) -> str:
    state_order = {"active": 0, "future": 1, "closed": 2}
    lines: list[str] = []
    if project_key_or_id:
        lines.append(f"project: {project_key_or_id}")
    lines.append(
        _listing_count_line(
            paging_unit,
            total_count=total_count,
            shown_count=shown_count,
            offset=offset,
            next_offset=next_offset,
        )
    )
    for board in board_sprints:
        lines.append(f"- board {board.get('id') or ''}: {board.get('name') or ''}")
        sprints = board.get("sprints")
        if not isinstance(sprints, list) or not sprints:
            lines.append("  - no sprints")
            continue
        for sprint in sorted(
            sprints,
            key=lambda item: (
                state_order.get(str(item.get("state") or "").lower(), 9),
                str(item.get("name") or "").casefold(),
            ),
        ):
            if not isinstance(sprint, dict):
                continue
            state = str(sprint.get("state") or "").lower()
            state_label = state or "unknown"
            line = (
                f"  - sprint {sprint.get('id') or ''}: {sprint.get('name') or ''}"
                f" [{state_label}]"
            )
            goal = str(sprint.get("goal") or "")
            if goal:
                line += f" - goal {goal}"
            lines.append(line)
    return "\n".join(lines) + "\n"


def add_list_paging_arguments(parser: argparse.ArgumentParser, *, noun: str) -> None:
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=DEFAULT_LIST_LIMIT,
        help=f"maximum {noun} to show in one page; defaults to a bounded page",
    )
    parser.add_argument(
        "--offset",
        type=nonnegative_int,
        default=0,
        help=f"skip the first N {noun} before rendering the current page",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"show all {noun} explicitly instead of the default bounded page",
    )


def required_missing_fields(
    fields: dict[str, dict[str, Any]], payload_fields: dict[str, Any]
) -> list[str]:
    missing: list[str] = []
    for field_id, meta in fields.items():
        if not meta.get("required"):
            continue
        if field_id in payload_fields:
            continue
        missing.append(f"{meta.get('name') or field_id} ({field_id})")
    return missing


def resolve_link_type(base_url: str, raw: str) -> tuple[Session, dict[str, Any]]:
    session, link_types = fetch_issue_link_types(base_url)
    candidate = _normalize_lookup(raw)
    for item in link_types:
        for key in ("id", "name", "inward", "outward"):
            value = str(item.get(key) or "")
            if value and (_normalize_lookup(value) == candidate or value == raw):
                return session, item
    available = ", ".join(
        str(item.get("name") or "") for item in link_types if item.get("name")
    )
    raise ToolError(
        f"unknown Jira link type: {raw}"
        + (f". Allowed values: {available}" if available else "")
    )


def resolve_transition(issue_ref: IssueRef, raw: str) -> tuple[Session, dict[str, Any]]:
    session, transitions = fetch_transitions(issue_ref)
    candidate = _normalize_lookup(raw)
    for item in transitions:
        if (
            str(item.get("id") or "") == raw
            or _normalize_lookup(str(item.get("name") or "")) == candidate
        ):
            return session, item
    available = ", ".join(
        str(item.get("name") or "") for item in transitions if item.get("name")
    )
    raise ToolError(
        f"unknown Jira transition for {issue_ref.issue_key}: {raw}"
        + (f". Allowed values: {available}" if available else "")
    )


def remote_link_global_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    system = (
        f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else url
    )
    return f"system={system}&url={url}"


def create_remote_link_payload(
    url: str, *, relationship: str, title: str = "", summary: str = ""
) -> dict[str, Any]:
    object_payload: dict[str, Any] = {"url": url, "title": title or url}
    if summary:
        object_payload["summary"] = summary
    return {
        "globalId": remote_link_global_id(url),
        "relationship": relationship,
        "object": object_payload,
    }


def preview_markdown_block(markdown: str, *, limit: int = 800) -> str:
    normalized = markdown.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "\n..."


def add_issues_to_sprint(
    session: Session, sprint_id: int, *, issue_keys: list[str]
) -> None:
    try:
        api_json(
            "POST",
            jira_add_issues_to_sprint_api_url(session, sprint_id),
            session.token,
            payload={"issues": issue_keys},
        )
    except ToolError as exc:
        raise ToolError(normalize_api_error_message(exc)) from exc


def create_issue(
    session: Session,
    *,
    payload_fields: dict[str, Any],
) -> dict[str, Any]:
    response = api_json(
        "POST",
        jira_create_issue_api_url(session),
        session.token,
        payload={"fields": payload_fields},
    )
    if not isinstance(response, dict):
        raise ToolError("unexpected Jira create issue response")
    issue_key = str(response.get("key") or "")
    if not issue_key:
        raise ToolError("Jira create issue response did not include an issue key")
    return fetch_issue(
        IssueRef(issue_key=issue_key, base_url=session.base_url),
        fields=DEFAULT_GET_FIELDS,
    )


def update_issue_fields(
    session: Session, issue_key: str, *, payload_fields: dict[str, Any]
) -> dict[str, Any]:
    try:
        api_json(
            "PUT",
            jira_issue_mutation_api_url(session, issue_key),
            session.token,
            payload={"fields": payload_fields},
        )
    except ToolError as exc:
        raise ToolError(normalize_api_error_message(exc)) from exc
    return fetch_issue(
        IssueRef(issue_key=issue_key, base_url=session.base_url),
        fields=DEFAULT_GET_FIELDS,
    )


def create_issue_comment(
    session: Session, issue_key: str, *, body_adf: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = api_json(
            "POST",
            jira_comment_api_url(session, issue_key),
            session.token,
            payload={"body": body_adf},
        )
    except ToolError as exc:
        raise ToolError(normalize_api_error_message(exc)) from exc
    if not isinstance(response, dict):
        raise ToolError("unexpected Jira comment response")
    return response


def create_issue_link(
    session: Session,
    *,
    source_issue: str,
    target_issue: str,
    link_type: dict[str, Any],
) -> None:
    payload = {
        "type": {"name": str(link_type.get("name") or "")},
        "inwardIssue": {"key": normalize_issue_key(source_issue)},
        "outwardIssue": {"key": normalize_issue_key(target_issue)},
    }
    try:
        api_json(
            "POST", jira_issue_link_api_url(session), session.token, payload=payload
        )
    except ToolError as exc:
        raise ToolError(normalize_api_error_message(exc)) from exc


def create_remote_link(
    session: Session, issue_key: str, *, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = api_json(
            "POST",
            jira_remote_link_api_url(session, issue_key),
            session.token,
            payload=payload,
        )
    except ToolError as exc:
        raise ToolError(normalize_api_error_message(exc)) from exc
    if not isinstance(response, dict):
        return {}
    return response


def transition_issue(
    session: Session,
    issue_key: str,
    *,
    transition_id: str,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"transition": {"id": transition_id}}
    if fields:
        payload["fields"] = fields
    try:
        api_json(
            "POST",
            jira_transitions_api_url(session, issue_key),
            session.token,
            payload=payload,
        )
    except ToolError as exc:
        raise ToolError(normalize_api_error_message(exc)) from exc
    return fetch_issue(
        IssueRef(issue_key=issue_key, base_url=session.base_url),
        fields=DEFAULT_GET_FIELDS,
    )


def markdown_issue(envelope: dict[str, Any]) -> str:
    envelope = _with_jira_visibility(
        envelope,
        subcommand="get",
        locator=_issue_locator(str(envelope.get("key") or "")),
    )
    lines = [
        f"# {envelope.get('key')}: {envelope.get('summary')}",
        "",
        f"- Status: {(envelope.get('status') or {}).get('name') or 'Unknown'}",
        f"- Type: {(envelope.get('issueType') or {}).get('name') or 'Unknown'}",
        f"- Project: {((envelope.get('project') or {}).get('key') or '')} {((envelope.get('project') or {}).get('name') or '')}".strip(),
        f"- Priority: {(envelope.get('priority') or {}).get('name') or 'Unspecified'}",
        f"- Assignee: {(envelope.get('assignee') or {}).get('displayName') or 'Unassigned'}",
        f"- Reporter: {(envelope.get('reporter') or {}).get('displayName') or 'Unknown'}",
        f"- Labels: {', '.join(envelope.get('labels') or []) or 'None'}",
        f"- Created: {envelope.get('created') or ''}",
        f"- Updated: {envelope.get('updated') or ''}",
        f"- URL: {envelope.get('issueUrl') or ''}",
        "",
        "## Description",
        "",
    ]
    lines[12:12] = render_visibility_metadata_lines(envelope)
    description_adf = envelope.get("descriptionAdf")
    description_md = ""
    if isinstance(description_adf, dict):
        description_md = render_adf_block(description_adf).strip()
    if description_md:
        lines.append(description_md)
    else:
        lines.append("_No description_")
    lines.append("")
    return "\n".join(lines)


def render_search_markdown(payload: dict[str, Any]) -> str:
    payload = _with_jira_visibility(
        payload,
        subcommand="search",
        locator=_search_locator("search", str(payload.get("query") or "")),
    )
    lines = [
        "# Jira Search",
        "",
        f"- Query: `{payload.get('query') or ''}`",
        f"- Results: {payload.get('size') or 0}",
    ]
    lines.extend(render_visibility_metadata_lines(payload))
    lines.extend(
        render_source_metadata_lines(derive_source_metadata_from_payload(payload))
    )
    next_token = str(payload.get("next") or "")
    if next_token:
        lines.append(f"- Next: `{next_token}`")
    lines.append("")
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        item = _with_jira_visibility(
            item,
            subcommand="search",
            locator=_issue_locator(str(item.get("key") or "")),
        )
        lines.extend(
            [
                f"## {item.get('key')}: {item.get('summary') or ''}",
                "",
                f"- Status: {(item.get('status') or {}).get('name') or 'Unknown'}",
                f"- Type: {(item.get('issueType') or {}).get('name') or 'Unknown'}",
                f"- Project: {((item.get('project') or {}).get('key') or '')}",
                f"- Priority: {(item.get('priority') or {}).get('name') or 'Unspecified'}",
                f"- Assignee: {(item.get('assignee') or {}).get('displayName') or 'Unassigned'}",
                f"- Labels: {', '.join(item.get('labels') or []) or 'None'}",
                f"- Updated: {item.get('updated') or ''}",
                f"- URL: {item.get('issueUrl') or ''}",
                "",
            ]
        )
        lines[-1:-1] = render_visibility_metadata_lines(item)
    return "\n".join(lines)


def cmd_auth(args: argparse.Namespace) -> int:
    base_url = site_root(args.base_url)
    if args.full:
        oauth_state = run_oauth_bootstrap(base_url=base_url)
        cloud_id = oauth_state.get("cloud_id")
        expires_at = oauth_state.get("expires_at")
        selected_base_url = str(oauth_state.get("base_url") or base_url).strip()
    else:
        session = load_session(base_url)
        status = atlassian_status_payload(
            base_url=base_url,
            auth_command="jira",
        )
        cloud_id = session.cloud_id
        expires_at = status.get("expiresAt")
        selected_base_url = str(
            session.base_url or status.get("baseUrl") or base_url
        ).strip()
    if selected_base_url:
        persist_selected_base_urls(selected_base_url)
    print_json(
        {
            "authenticated": True,
            "base_url": selected_base_url,
            "cloud_id": cloud_id,
            "expires_at": expires_at,
            "token_file": str(TOKEN_FILE),
            "cloud_id_file": str(CLOUD_ID_FILE),
            "oauth_dir": str(OAUTH_DIR),
        }
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    payload = atlassian_status_payload(
        base_url=args.base_url,
        check_token=args.check,
        auth_command="jira",
    )
    payload["surface"] = "jira"
    if args.output == "json":
        print_json(payload)
        return 0
    lines = [
        "surface\tjira",
        f"base_url\t{payload.get('baseUrl') or ''}",
        f"credentials_configured\t{str(bool(payload.get('credentialsConfigured'))).lower()}",
        f"session_status\t{payload.get('sessionStatus') or 'missing'}",
        f"expires_at\t{payload.get('expiresAt') or ''}",
        f"has_refresh_token\t{str(bool(payload.get('hasRefreshToken'))).lower()}",
        f"token_file\t{payload.get('tokenFile') or ''}",
        f"next_step\t{payload.get('nextStep') or ''}",
    ]
    if payload.get("tokenPreflight"):
        lines.append(f"token_preflight\t{payload['tokenPreflight']}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    issue_ref = parse_issue_ref(args.issue, base_url_override=args.base_url)
    envelope = fetch_issue(issue_ref, fields=DEFAULT_GET_FIELDS)
    if args.output == "json":
        print_json(envelope)
        return 0
    if args.output == "meta":
        print_json(meta_issue(envelope))
        return 0
    if args.output == "adf":
        print_json(envelope.get("descriptionAdf"))
        return 0
    sys.stdout.write(markdown_issue(envelope))
    return 0


def capture(argv: list[str], _options: object) -> Capture:
    args = _parse_cli(argv)
    if args.command != "get":
        if args.command in {"search", "jql"}:
            payload = capture_json_command(
                args,
                cmd_search if args.command == "search" else cmd_jql,
                detail=f"jira {args.command} capture failed",
            )
            return Capture(
                data=payload,
                preferred_name=preferred_name(argv, object()),
                content_type="application/json",
                metadata={
                    "projector": "jira",
                    "jira_kind": args.command,
                },
            )
        raise NotImplementedError("jira capture does not support this command")
    issue_ref = parse_issue_ref(args.issue, base_url_override=args.base_url)
    envelope = fetch_issue(issue_ref, fields=DEFAULT_GET_FIELDS)
    return Capture(
        data=json_bytes(envelope),
        preferred_name=f"{_issue_key_for_locator(args.issue)}.json",
        content_type="application/json",
        metadata={
            "projector": "jira",
            "source_created_at": str(envelope.get("createdAt") or ""),
            "source_updated_at": str(envelope.get("updatedAt") or ""),
        },
    )


def project(argv: list[str], capture: Capture) -> Projection:
    kind = str(capture.metadata.get("jira_kind") or "get").strip()
    if kind in {"search", "jql"}:
        payload = json.loads(capture.data.decode("utf-8"))
        if not argv:
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
        return projection_bytes(
            render_search_markdown(payload).encode("utf-8"),
            content_type="text/markdown",
        )
    envelope = json.loads(capture.data.decode("utf-8"))
    if not argv:
        return projection_bytes(
            markdown_issue(envelope).encode("utf-8"),
            content_type="text/markdown",
        )
    args = _parse_cli(argv)
    if args.command != "get":
        return projection_bytes(capture.data, content_type=capture.content_type)
    if args.output == "json":
        return projection_bytes(
            pretty_json(capture.data),
            content_type="application/json",
        )
    if args.output == "meta":
        return projection_bytes(
            json_bytes(meta_issue(envelope)),
            content_type="application/json",
        )
    if args.output == "adf":
        return projection_bytes(
            json_bytes(envelope.get("descriptionAdf")),
            content_type="application/json",
        )
    return projection_bytes(
        markdown_issue(envelope).encode("utf-8"),
        content_type="text/markdown",
    )


def cmd_search(args: argparse.Namespace) -> int:
    if looks_like_raw_jql(args.query):
        raise ToolError(
            "`gotta jira search` accepts plain-text only; detected raw JQL field/operator "
            f"syntax. Likely next step: run `gotta jira jql {shlex.quote(args.query)}`"
        )
    issue_key = _search_issue_key(args.query)
    if issue_key:
        issue_ref = parse_issue_ref(issue_key, base_url_override=args.base_url)
        payload = _search_payload_for_issue(
            fetch_issue(issue_ref, fields=DEFAULT_GET_FIELDS),
            query=args.query,
        )
        if args.output == "json":
            print_json(payload)
            return 0
        sys.stdout.write(render_search_markdown(payload))
        return 0
    payload = search_jira(
        base_url=site_root(args.base_url),
        jql=build_plaintext_jql(args.query),
        limit=args.limit,
        cursor=args.next,
        fields=DEFAULT_SEARCH_FIELDS,
        subcommand="search",
    )
    if args.output == "json":
        print_json(payload)
        return 0
    sys.stdout.write(render_search_markdown(payload))
    return 0


def cmd_jql(args: argparse.Namespace) -> int:
    payload = search_jira(
        base_url=site_root(args.base_url),
        jql=args.query,
        limit=args.limit,
        cursor=args.next,
        fields=DEFAULT_SEARCH_FIELDS,
        subcommand="jql",
    )
    if args.output == "json":
        print_json(payload)
        return 0
    sys.stdout.write(render_search_markdown(payload))
    return 0


def render_write_preview_summary(payload: dict[str, Any]) -> str:
    lines = [
        f"action: {payload.get('action') or ''}",
        f"mode: {payload.get('mode') or 'preview'}",
    ]
    if payload.get("ready") is not None:
        lines.append(f"ready: {str(bool(payload.get('ready'))).lower()}")
    target = payload.get("target")
    if isinstance(target, dict):
        if target.get("issue"):
            lines.append(f"issue: {target['issue']}")
        if target.get("project"):
            lines.append(f"project: {target['project']}")
        if target.get("issueType"):
            lines.append(f"issue_type: {target['issueType']}")
        if target.get("board"):
            lines.append(f"board: {target['board']}")
        if target.get("boardName"):
            lines.append(f"board_name: {target['boardName']}")
        if target.get("sprint"):
            lines.append(f"sprint: {target['sprint']}")
        if target.get("sprintName"):
            lines.append(f"sprint_name: {target['sprintName']}")
        if target.get("sprintState"):
            lines.append(f"sprint_state: {target['sprintState']}")
    title = str(payload.get("title") or "")
    if title:
        lines.append(f"title: {title}")
    missing = payload.get("missingRequiredFields")
    if isinstance(missing, list) and missing:
        lines.append("missing_required:")
        lines.extend(f"- {item}" for item in missing if isinstance(item, str))
    field_values = payload.get("fieldValues")
    if isinstance(field_values, dict) and field_values:
        lines.append("fields:")
        for field_id, value in field_values.items():
            if isinstance(value, dict) and value.get("type") == "doc":
                rendered = "<adf doc>"
            elif isinstance(value, (dict, list)):
                rendered = json.dumps(value, sort_keys=True)
            else:
                rendered = str(value)
            lines.append(f"- {field_id}: {rendered}")
    body_markdown = str(payload.get("bodyMarkdown") or "").strip()
    if body_markdown:
        lines.extend(["body_preview:", preview_markdown_block(body_markdown)])
    issue_links = payload.get("issueLinks")
    if isinstance(issue_links, list) and issue_links:
        lines.append("issue_links:")
        for item in issue_links:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('source') or ''} -> {item.get('target') or ''} ({item.get('type') or ''})"
            )
    remote_links = payload.get("remoteLinks")
    if isinstance(remote_links, list) and remote_links:
        lines.append("remote_links:")
        for item in remote_links:
            if not isinstance(item, dict):
                continue
            lines.append(f"- {item.get('relationship') or ''}: {item.get('url') or ''}")
    return "\n".join(lines) + "\n"


def write_preview_or_json(payload: dict[str, Any], *, output: str) -> int:
    if output == "json":
        print_json(payload)
        return 0
    sys.stdout.write(render_write_preview_summary(payload))
    return 0


def create_body_adf(markdown: str) -> dict[str, Any] | None:
    if markdown == "":
        return None
    return markdown_to_adf(markdown)


def build_create_fields(
    args: argparse.Namespace,
    fields: dict[str, dict[str, Any]],
    *,
    project_key: str,
    issue_type_id: str,
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    payload_fields: dict[str, Any] = {
        "project": {"key": project_key},
        "issuetype": {"id": issue_type_id},
        "summary": args.title,
    }
    body_markdown = read_explicit_body(
        args.body,
        args.body_file,
        stdin_flag=args.stdin,
        required=False,
        label="issue body",
    )
    body_adf = create_body_adf(body_markdown)
    if body_adf is not None:
        payload_fields["description"] = body_adf
    if args.priority:
        apply_named_field(
            payload_fields, fields, field_id="priority", raw_values=[args.priority]
        )
    if args.assignee:
        apply_named_field(
            payload_fields, fields, field_id="assignee", raw_values=[args.assignee]
        )
    if args.parent:
        apply_named_field(
            payload_fields, fields, field_id="parent", raw_values=[args.parent]
        )
    if args.labels:
        apply_named_field(
            payload_fields, fields, field_id="labels", raw_values=list(args.labels)
        )
    if args.components:
        apply_named_field(
            payload_fields,
            fields,
            field_id="components",
            raw_values=list(args.components),
        )
    apply_generic_fields(payload_fields, fields, parse_field_assignments(args.field))
    return payload_fields, body_markdown, body_adf


def build_update_fields(
    args: argparse.Namespace,
    fields: dict[str, dict[str, Any]],
    *,
    current_markdown: str,
) -> tuple[dict[str, Any], str]:
    payload_fields: dict[str, Any] = {}
    if args.summary is not None:
        payload_fields["summary"] = args.summary
    if args.priority:
        apply_named_field(
            payload_fields, fields, field_id="priority", raw_values=[args.priority]
        )
    if args.assignee:
        apply_named_field(
            payload_fields, fields, field_id="assignee", raw_values=[args.assignee]
        )
    if args.parent:
        apply_named_field(
            payload_fields, fields, field_id="parent", raw_values=[args.parent]
        )
    if args.labels:
        apply_named_field(
            payload_fields, fields, field_id="labels", raw_values=list(args.labels)
        )
    if args.components:
        apply_named_field(
            payload_fields,
            fields,
            field_id="components",
            raw_values=list(args.components),
        )
    apply_generic_fields(payload_fields, fields, parse_field_assignments(args.field))

    body_markdown = ""
    if (
        args.body is not None
        or args.body_file is not None
        or args.stdin
        or args.replace_description
        or args.append_section
        or args.prepend_section
        or args.upsert_section
    ):
        body_input = read_explicit_body(
            args.body,
            args.body_file,
            stdin_flag=args.stdin,
            required=bool(
                args.replace_description
                or args.append_section
                or args.prepend_section
                or args.upsert_section
                or args.body is not None
                or args.body_file is not None
                or args.stdin
            ),
            label="update body",
        )
        if args.append_section:
            body_markdown = append_markdown_section(
                current_markdown, args.append_section, body_input
            )
        elif args.prepend_section:
            body_markdown = prepend_markdown_section(
                current_markdown, args.prepend_section, body_input
            )
        elif args.upsert_section:
            body_markdown = upsert_markdown_section(
                current_markdown, args.upsert_section, body_input
            )
        else:
            body_markdown = body_input
        payload_fields["description"] = markdown_to_adf(body_markdown)
    return payload_fields, body_markdown


def build_comment_payload(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    body_markdown = read_explicit_body(
        args.body,
        args.body_file,
        stdin_flag=args.stdin,
        required=True,
        label="comment body",
    )
    return body_markdown, markdown_to_adf(body_markdown)


def cmd_projects(args: argparse.Namespace) -> int:
    _, projects = fetch_projects(args.base_url)
    paged_projects, page = _paginate_items(
        projects,
        offset=args.offset,
        limit=args.limit,
        include_all=bool(args.all),
    )
    if args.output == "json":
        print_json({**page, "projects": paged_projects})
        return 0
    sys.stdout.write(
        render_projects_summary(
            paged_projects,
            total_count=int(page["totalCount"]),
            shown_count=int(page["shownCount"]),
            offset=int(page["offset"]),
            next_offset=page["nextOffset"],
        )
    )
    return 0


def cmd_issue_types(args: argparse.Namespace) -> int:
    session, project = resolve_project(args.base_url, args.project)
    issue_types = fetch_create_issue_types(
        session, str(project.get("key") or args.project)
    )
    paged_issue_types, page = _paginate_items(
        issue_types,
        offset=args.offset,
        limit=args.limit,
        include_all=bool(args.all),
    )
    payload = {
        "project": {
            "id": str(project.get("id") or ""),
            "key": str(project.get("key") or args.project),
            "name": str(project.get("name") or ""),
        },
        **page,
        "issueTypes": paged_issue_types,
    }
    if args.output == "json":
        print_json(payload)
        return 0
    sys.stdout.write(
        render_issue_types_summary(
            payload["project"]["key"],
            paged_issue_types,
            total_count=int(page["totalCount"]),
            shown_count=int(page["shownCount"]),
            offset=int(page["offset"]),
            next_offset=page["nextOffset"],
        )
    )
    return 0


def cmd_fields(args: argparse.Namespace) -> int:
    if args.issue:
        issue_ref = parse_issue_ref(args.issue, base_url_override=args.base_url)
        _, fields = fetch_edit_fields(issue_ref)
        context = f"edit {issue_ref.issue_key}"
    else:
        if not args.project or not args.type:
            raise ToolError("create-field discovery requires --project and --type")
        session, project = resolve_project(args.base_url, args.project)
        project_key = str(project.get("key") or args.project)
        issue_type = resolve_issue_type(session, project_key, args.type)
        fields = fetch_create_fields(
            session, project_key, str(issue_type.get("id") or "")
        )
        context = f"create {project_key}/{issue_type.get('name') or issue_type.get('id') or ''}"
    if args.output == "json":
        print_json({"context": context, "fields": fields})
        return 0
    sys.stdout.write(render_fields_summary(fields, context=context))
    return 0


def cmd_link_types(args: argparse.Namespace) -> int:
    _, link_types = fetch_issue_link_types(args.base_url)
    paged_link_types, page = _paginate_items(
        link_types,
        offset=args.offset,
        limit=args.limit,
        include_all=bool(args.all),
    )
    if args.output == "json":
        print_json({**page, "linkTypes": paged_link_types})
        return 0
    lines = [
        _listing_count_line(
            "link_types",
            total_count=int(page["totalCount"]),
            shown_count=int(page["shownCount"]),
            offset=int(page["offset"]),
            next_offset=page["nextOffset"],
        )
    ]
    for item in paged_link_types:
        lines.append(
            f"- {item.get('name') or ''} - id `{item.get('id') or ''}`, outward `{item.get('outward') or ''}`, inward `{item.get('inward') or ''}`"
        )
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def cmd_transitions(args: argparse.Namespace) -> int:
    issue_ref = parse_issue_ref(args.issue, base_url_override=args.base_url)
    _, transitions = fetch_transitions(issue_ref)
    paged_transitions, page = _paginate_items(
        transitions,
        offset=args.offset,
        limit=args.limit,
        include_all=bool(args.all),
    )
    if args.output == "json":
        print_json(
            {**page, "issue": issue_ref.issue_key, "transitions": paged_transitions}
        )
        return 0
    sys.stdout.write(
        render_transitions_summary(
            issue_ref.issue_key,
            paged_transitions,
            total_count=int(page["totalCount"]),
            shown_count=int(page["shownCount"]),
            offset=int(page["offset"]),
            next_offset=page["nextOffset"],
        )
    )
    return 0


def cmd_sprints(args: argparse.Namespace) -> int:
    if not args.project and args.board is None:
        raise ToolError("sprint discovery requires --project or --board")
    _, board_sprints = collect_board_sprints(
        args.base_url,
        project_key_or_id=args.project or "",
        board_id=args.board,
    )
    paging_unit = "sprints"
    if args.board is not None:
        board = dict(
            board_sprints[0]
            if board_sprints
            else {"id": str(args.board), "name": "", "type": "scrum"}
        )
        sprints = list(board.get("sprints") or [])
        paged_sprints, page = _paginate_items(
            sprints,
            offset=args.offset,
            limit=args.limit,
            include_all=bool(args.all),
        )
        if paged_sprints or not sprints:
            paged_boards = [{**board, "sprints": paged_sprints}]
        else:
            paged_boards = []
    else:
        flat_sprints: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for board in board_sprints:
            for sprint in list(board.get("sprints") or []):
                flat_sprints.append((board, sprint))
        if flat_sprints:
            paged_pairs, page = _paginate_items(
                flat_sprints,
                offset=args.offset,
                limit=args.limit,
                include_all=bool(args.all),
            )
            grouped: dict[str, dict[str, Any]] = {}
            order: list[str] = []
            for board, sprint in paged_pairs:
                board_id = str(board.get("id") or "")
                if board_id not in grouped:
                    grouped[board_id] = {**board, "sprints": []}
                    order.append(board_id)
                grouped[board_id]["sprints"].append(sprint)
            paged_boards = [grouped[board_id] for board_id in order]
        else:
            paging_unit = "boards"
            paged_boards, page = _paginate_items(
                board_sprints,
                offset=args.offset,
                limit=args.limit,
                include_all=bool(args.all),
            )
    payload = {
        "project": args.project or "",
        **page,
        "pagingUnit": paging_unit,
        "boards": paged_boards,
    }
    if args.output == "json":
        print_json(payload)
        return 0
    sys.stdout.write(
        render_sprints_summary(
            project_key_or_id=args.project or "",
            board_sprints=paged_boards,
            paging_unit=paging_unit,
            total_count=int(page["totalCount"]),
            shown_count=int(page["shownCount"]),
            offset=int(page["offset"]),
            next_offset=page["nextOffset"],
        )
    )
    return 0


def cmd_add_to_sprint(args: argparse.Namespace) -> int:
    issue_ref = parse_issue_ref(args.issue, base_url_override=args.base_url)
    if bool(args.current) == bool(args.sprint is not None):
        raise ToolError("choose exactly one of --current or --sprint")
    session, board, sprint = resolve_assignment_sprint(
        issue_ref.base_url,
        project_key_or_id=args.project or "",
        board_id=args.board,
        sprint_id=args.sprint,
        current=args.current,
    )
    preview = {
        "action": "add-to-sprint",
        "mode": "preview",
        "ready": True,
        "target": {
            "issue": issue_ref.issue_key,
            "project": args.project or "",
            "board": board.get("id") or "",
            "boardName": board.get("name") or "",
            "sprint": sprint.get("id") or "",
            "sprintName": sprint.get("name") or "",
            "sprintState": sprint.get("state") or "",
            "apiUrl": jira_add_issues_to_sprint_api_url(session, int(sprint["id"])),
        },
        "payload": {"issues": [issue_ref.issue_key]},
    }
    if not args.apply:
        return write_preview_or_json(preview, output=args.output)
    add_issues_to_sprint(session, int(sprint["id"]), issue_keys=[issue_ref.issue_key])
    result = {
        "action": "add-to-sprint",
        "mode": "applied",
        "issue": issue_ref.issue_key,
        "board": board,
        "sprint": sprint,
    }
    if args.output == "json":
        print_json(result)
        return 0
    sys.stdout.write(
        "\n".join(
            [
                "action: add-to-sprint",
                "mode: applied",
                f"issue: {issue_ref.issue_key}",
                f"board: {board.get('id') or ''}",
                f"sprint: {sprint.get('id') or ''}",
            ]
        )
        + "\n"
    )
    return 0


def sprint_request_enabled(
    *,
    current_sprint: bool = False,
    sprint_id: int | None = None,
) -> bool:
    return current_sprint or sprint_id is not None


def resolve_requested_sprint(
    base_url: str,
    *,
    current_sprint: bool,
    sprint_id: int | None,
    project_key_or_id: str = "",
    board_id: int | None = None,
) -> tuple[Session, dict[str, Any], dict[str, Any]] | None:
    if not sprint_request_enabled(current_sprint=current_sprint, sprint_id=sprint_id):
        return None
    return resolve_assignment_sprint(
        base_url,
        project_key_or_id=project_key_or_id,
        board_id=board_id,
        sprint_id=sprint_id,
        current=current_sprint,
    )


def cmd_create(args: argparse.Namespace) -> int:
    session, project = resolve_project(args.base_url, args.project)
    project_key = str(project.get("key") or args.project)
    issue_type = resolve_issue_type(session, project_key, args.type)
    fields = fetch_create_fields(session, project_key, str(issue_type.get("id") or ""))
    resolved_link_type_name = args.link_type
    resolved_link_type: dict[str, Any] | None = None
    if args.relates:
        _, resolved_link_type = resolve_link_type(session.base_url, args.link_type)
        resolved_link_type_name = str(resolved_link_type.get("name") or args.link_type)
    payload_fields, body_markdown, body_adf = build_create_fields(
        args,
        fields,
        project_key=project_key,
        issue_type_id=str(issue_type.get("id") or ""),
    )
    sprint_target = resolve_requested_sprint(
        session.base_url,
        current_sprint=args.current_sprint,
        sprint_id=args.sprint,
        project_key_or_id=project_key,
        board_id=args.board,
    )
    missing = required_missing_fields(fields, payload_fields)
    preview_target = {
        "project": project_key,
        "issueType": issue_type.get("name") or "",
        "apiUrl": jira_create_issue_api_url(session),
    }
    if sprint_target is not None:
        _, sprint_board, sprint = sprint_target
        preview_target.update(
            {
                "board": sprint_board.get("id") or "",
                "boardName": sprint_board.get("name") or "",
                "sprint": sprint.get("id") or "",
                "sprintName": sprint.get("name") or "",
                "sprintState": sprint.get("state") or "",
            }
        )
    preview = {
        "action": "create",
        "mode": "preview",
        "ready": not missing,
        "target": preview_target,
        "title": args.title,
        "fieldValues": payload_fields,
        "bodyMarkdown": body_markdown,
        "bodyAdf": body_adf,
        "missingRequiredFields": missing,
        "issueLinks": [
            {
                "source": "(new issue)",
                "target": normalize_issue_key(target),
                "type": resolved_link_type_name,
            }
            for target in args.relates
        ],
        "remoteLinks": [
            {"relationship": args.remote_link_relationship, "url": url}
            for url in args.remote_link
        ],
        "payload": {"fields": payload_fields},
    }
    if not args.apply:
        return write_preview_or_json(preview, output=args.output)
    if missing:
        raise ToolError(
            "missing required Jira create fields: "
            + ", ".join(missing)
            + f". Likely next step: run `gotta jira fields --project {project_key} --type {shlex.quote(str(issue_type.get('name') or args.type))}`"
        )
    try:
        created = create_issue(session, payload_fields=payload_fields)
    except ToolError as exc:
        raise ToolError(normalize_api_error_message(exc)) from exc
    if sprint_target is not None:
        sprint_session, _, sprint = sprint_target
        add_issues_to_sprint(
            sprint_session,
            int(sprint["id"]),
            issue_keys=[created["key"]],
        )
    if args.relates:
        link_type = resolved_link_type or {"name": resolved_link_type_name}
        for target in args.relates:
            create_issue_link(
                session,
                source_issue=created["key"],
                target_issue=target,
                link_type=link_type,
            )
    for url in args.remote_link:
        create_remote_link(
            session,
            created["key"],
            payload=create_remote_link_payload(
                url,
                relationship=args.remote_link_relationship,
            ),
        )
    result = {
        "action": "create",
        "mode": "applied",
        "issue": meta_issue(created),
        "linkedIssues": [normalize_issue_key(item) for item in args.relates],
        "remoteLinks": list(args.remote_link),
    }
    if sprint_target is not None:
        _, sprint_board, sprint = sprint_target
        result["board"] = sprint_board
        result["sprint"] = sprint
    if args.output == "json":
        print_json(result)
        return 0
    lines = [
        "action: create",
        "mode: applied",
        f"issue: {created.get('key') or ''}",
        f"url: {created.get('issueUrl') or ''}",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    issue_ref = parse_issue_ref(args.issue, base_url_override=args.base_url)
    session, fields = fetch_edit_fields(issue_ref)
    current = fetch_issue(issue_ref, fields=DEFAULT_GET_FIELDS)
    current_markdown = ""
    if isinstance(current.get("descriptionAdf"), dict):
        current_markdown = render_adf_block(current["descriptionAdf"]).strip()
    payload_fields, body_markdown = build_update_fields(
        args, fields, current_markdown=current_markdown
    )
    current_project_key = str(
        ((current.get("project") or {}).get("key") or args.project or "")
    )
    sprint_target = resolve_requested_sprint(
        issue_ref.base_url,
        current_sprint=args.current_sprint,
        sprint_id=args.sprint,
        project_key_or_id=current_project_key,
        board_id=args.board,
    )
    if not payload_fields and sprint_target is None:
        raise ToolError(
            "nothing to update; pass field edits or a description operation"
        )
    preview_target = {"issue": issue_ref.issue_key}
    if sprint_target is not None:
        _, sprint_board, sprint = sprint_target
        preview_target.update(
            {
                "project": current_project_key,
                "board": sprint_board.get("id") or "",
                "boardName": sprint_board.get("name") or "",
                "sprint": sprint.get("id") or "",
                "sprintName": sprint.get("name") or "",
                "sprintState": sprint.get("state") or "",
            }
        )
    preview = {
        "action": "update",
        "mode": "preview",
        "ready": True,
        "target": preview_target,
        "title": payload_fields.get("summary") or current.get("summary") or "",
        "fieldValues": payload_fields,
        "bodyMarkdown": body_markdown,
        "payload": {"fields": payload_fields},
    }
    if not args.apply:
        return write_preview_or_json(preview, output=args.output)
    updated = current
    if payload_fields:
        updated = update_issue_fields(
            session, issue_ref.issue_key, payload_fields=payload_fields
        )
    if sprint_target is not None:
        sprint_session, _, sprint = sprint_target
        add_issues_to_sprint(
            sprint_session,
            int(sprint["id"]),
            issue_keys=[issue_ref.issue_key],
        )
        if not payload_fields:
            updated = fetch_issue(issue_ref, fields=DEFAULT_GET_FIELDS)
    result = {"action": "update", "mode": "applied", "issue": meta_issue(updated)}
    if sprint_target is not None:
        _, sprint_board, sprint = sprint_target
        result["board"] = sprint_board
        result["sprint"] = sprint
    if args.output == "json":
        print_json(result)
        return 0
    sys.stdout.write(
        "\n".join(
            [
                "action: update",
                "mode: applied",
                f"issue: {updated.get('key') or ''}",
                f"url: {updated.get('issueUrl') or ''}",
            ]
        )
        + "\n"
    )
    return 0


def cmd_comment(args: argparse.Namespace) -> int:
    issue_ref = parse_issue_ref(args.issue, base_url_override=args.base_url)
    session = load_session(issue_ref.base_url)
    body_markdown, body_adf = build_comment_payload(args)
    preview = {
        "action": "comment",
        "mode": "preview",
        "ready": True,
        "target": {"issue": issue_ref.issue_key},
        "bodyMarkdown": body_markdown,
        "bodyAdf": body_adf,
        "payload": {"body": body_adf},
    }
    if not args.apply:
        return write_preview_or_json(preview, output=args.output)
    created = create_issue_comment(session, issue_ref.issue_key, body_adf=body_adf)
    result = {
        "action": "comment",
        "mode": "applied",
        "issue": issue_ref.issue_key,
        "commentId": str(created.get("id") or ""),
    }
    if args.output == "json":
        print_json(result)
        return 0
    sys.stdout.write(
        "\n".join(
            [
                "action: comment",
                "mode: applied",
                f"issue: {issue_ref.issue_key}",
                f"comment_id: {result['commentId']}",
            ]
        )
        + "\n"
    )
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    issue_ref = parse_issue_ref(args.issue, base_url_override=args.base_url)
    session = load_session(issue_ref.base_url)
    if re.match(r"^https?://", args.target):
        preview = {
            "action": "link",
            "mode": "preview",
            "ready": True,
            "target": {"issue": issue_ref.issue_key},
            "remoteLinks": [{"relationship": args.relationship, "url": args.target}],
            "payload": create_remote_link_payload(
                args.target,
                relationship=args.relationship,
                title=args.title,
                summary=args.summary,
            ),
        }
        if not args.apply:
            return write_preview_or_json(preview, output=args.output)
        create_remote_link(
            session,
            issue_ref.issue_key,
            payload=create_remote_link_payload(
                args.target,
                relationship=args.relationship,
                title=args.title,
                summary=args.summary,
            ),
        )
        result = {
            "action": "link",
            "mode": "applied",
            "issue": issue_ref.issue_key,
            "remoteLink": args.target,
        }
    else:
        _, link_type = resolve_link_type(session.base_url, args.type)
        target_key = normalize_issue_key(args.target)
        preview = {
            "action": "link",
            "mode": "preview",
            "ready": True,
            "target": {"issue": issue_ref.issue_key},
            "issueLinks": [
                {
                    "source": issue_ref.issue_key,
                    "target": target_key,
                    "type": link_type.get("name") or "",
                }
            ],
            "payload": {
                "type": {"name": str(link_type.get("name") or "")},
                "inwardIssue": {"key": issue_ref.issue_key},
                "outwardIssue": {"key": target_key},
            },
        }
        if not args.apply:
            return write_preview_or_json(preview, output=args.output)
        create_issue_link(
            session,
            source_issue=issue_ref.issue_key,
            target_issue=target_key,
            link_type=link_type,
        )
        result = {
            "action": "link",
            "mode": "applied",
            "issue": issue_ref.issue_key,
            "linkedIssue": target_key,
        }
    if args.output == "json":
        print_json(result)
        return 0
    sys.stdout.write(
        "\n".join(
            [
                f"action: {result['action']}",
                f"mode: {result['mode']}",
                f"issue: {result['issue']}",
            ]
        )
        + "\n"
    )
    return 0


def cmd_transition(args: argparse.Namespace) -> int:
    issue_ref = parse_issue_ref(args.issue, base_url_override=args.base_url)
    session, transition = resolve_transition(issue_ref, args.to)
    transition_fields = transition.get("fields")
    if not isinstance(transition_fields, dict):
        transition_fields = {}
    field_values: dict[str, Any] = {}
    apply_generic_fields(
        field_values, transition_fields, parse_field_assignments(args.field)
    )
    missing = required_missing_fields(transition_fields, field_values)
    preview = {
        "action": "transition",
        "mode": "preview",
        "ready": not missing,
        "target": {"issue": issue_ref.issue_key},
        "fieldValues": field_values,
        "missingRequiredFields": missing,
        "payload": {
            "transition": {"id": transition.get("id") or ""},
            "fields": field_values,
        },
    }
    if not args.apply:
        return write_preview_or_json(preview, output=args.output)
    if missing:
        raise ToolError(
            "missing required Jira transition fields: "
            + ", ".join(missing)
            + f". Likely next step: run `gotta jira transitions {issue_ref.issue_key}`"
        )
    updated = transition_issue(
        session,
        issue_ref.issue_key,
        transition_id=str(transition.get("id") or ""),
        fields=field_values,
    )
    result = {"action": "transition", "mode": "applied", "issue": meta_issue(updated)}
    if args.output == "json":
        print_json(result)
        return 0
    sys.stdout.write(
        "\n".join(
            [
                "action: transition",
                "mode: applied",
                f"issue: {updated.get('key') or ''}",
                f"status: {(updated.get('status') or {}).get('name') or ''}",
            ]
        )
        + "\n"
    )
    return 0


def resolve_mcp_runtime(base_url: str) -> tuple[str, str, str, str, str, str, str, str]:
    config_env = load_atlassian_config_env()
    client_id, client_secret, redirect_uri, scope = load_oauth_runtime_config()
    site_url = site_root(base_url)
    if not site_url:
        raise ToolError("missing Jira base URL")
    confluence_url = site_url + "/wiki"
    toolsets = (
        os.environ.get("GOTTA_ATLASSIAN_TOOLSETS", "").strip()
        or config_env.get("GOTTA_ATLASSIAN_TOOLSETS", "").strip()
        or "all"
    )
    session = load_session(site_url)
    return (
        client_id,
        client_secret,
        redirect_uri,
        scope,
        site_url,
        confluence_url,
        toolsets,
        session.cloud_id,
    )


def validate_mcp_passthrough_args(args: list[str]) -> None:
    for arg in args:
        flag = arg.split("=", 1)[0]
        if flag in DISALLOWED_MCP_PASSTHROUGH_FLAGS:
            raise ToolError(
                f"{flag} is managed by gotta jira mcp; configure durable OAuth app "
                f"settings via env or {atl.provider_env_reference('atlassian')} instead"
            )


def is_mcp_metadata_only_invocation(args: list[str]) -> bool:
    return any(arg in {"--help", "--version", "-h"} for arg in args)


def cmd_mcp(args: argparse.Namespace) -> int:
    mcp_atlassian = shutil.which("mcp-atlassian")
    if not mcp_atlassian:
        raise ToolError(
            "missing required command: mcp-atlassian. "
            "Likely next step: install the Atlassian MCP server CLI"
        )

    passthrough = list(args.mcp_args)
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]
    validate_mcp_passthrough_args(passthrough)

    env = os.environ.copy()
    env["FASTMCP_SHOW_CLI_BANNER"] = "false"
    env["FASTMCP_CHECK_FOR_UPDATES"] = "off"
    env.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")

    if is_mcp_metadata_only_invocation(passthrough):
        os.execvpe(mcp_atlassian, [mcp_atlassian, *passthrough], env)
        return 0

    try:
        (
            client_id,
            client_secret,
            redirect_uri,
            scope,
            site_url,
            confluence_url,
            toolsets,
            cloud_id,
        ) = resolve_mcp_runtime(args.base_url)
    except ToolError as exc:
        raise ToolError(f"{exc}. Likely next step: run 'gotta jira auth'") from exc

    argv = [
        mcp_atlassian,
        "--transport",
        "stdio",
        "--jira-url",
        site_url,
        "--confluence-url",
        confluence_url,
        "--toolsets",
        toolsets,
        "--oauth-client-id",
        client_id,
        "--oauth-client-secret",
        client_secret,
        "--oauth-redirect-uri",
        redirect_uri,
        "--oauth-scope",
        scope,
        "--oauth-cloud-id",
        cloud_id,
        *passthrough,
    ]
    os.execvpe(mcp_atlassian, argv, env)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta jira",
        description=(
            "Standalone Jira discovery, read, and authoring toolbelt. Interactive "
            "commands may open browser reauthorization if the cached Atlassian "
            "session is invalid."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "auth",
        help="ensure local Atlassian OAuth state is usable; refresh silently when possible",
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="force a full browser OAuth bootstrap instead of reusing or refreshing cached state",
    )
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("status", help="inspect local Jira/Atlassian auth readiness")
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument(
        "--check", action="store_true", help="run a token preflight against Atlassian"
    )
    p.add_argument("--output", choices=["json", "summary"], default="summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("get", help="fetch one Jira issue by key or URL")
    p.add_argument(
        "issue",
        help=f"Jira issue key like PROJ-123 or a Jira issue URL; bare keys require --base-url or {JIRA_BASE_URL_ENV}",
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override for bare issue keys (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument(
        "--output",
        choices=["markdown", "meta", "json", "adf"],
        default="markdown",
        help="render format for get; defaults to markdown",
    )
    p.set_defaults(func=cmd_get)

    p = sub.add_parser(
        "search",
        help="search Jira issues using a plain-text convenience query",
    )
    p.add_argument(
        "query",
        help=(
            "plain-text search query; exact issue keys resolve directly; "
            "for raw fielded JQL use `gotta jira jql`"
        ),
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument(
        "--limit", type=positive_int, default=10, help="maximum results to request"
    )
    p.add_argument(
        "--next", help="continuation token from a previous Jira search response"
    )
    p.add_argument("--output", choices=["markdown", "json"], default="markdown")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("jql", help="search Jira issues with raw JQL")
    p.add_argument("query", help="raw JQL search expression")
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument(
        "--limit", type=positive_int, default=10, help="maximum results to request"
    )
    p.add_argument(
        "--next", help="continuation token from a previous Jira search response"
    )
    p.add_argument("--output", choices=["markdown", "json"], default="markdown")
    p.set_defaults(func=cmd_jql)

    p = sub.add_parser(
        "projects", help="list Jira projects available to the current user"
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override (default: {JIRA_BASE_URL_ENV})",
    )
    add_list_paging_arguments(p, noun="projects")
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser(
        "sprints",
        help="list scrum boards and sprints available for a project or board",
    )
    p.add_argument("--project", help="project key or ID used to resolve scrum boards")
    p.add_argument("--board", type=positive_int, help="explicit scrum board ID")
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override (default: {JIRA_BASE_URL_ENV})",
    )
    add_list_paging_arguments(p, noun="boards or board-local sprints")
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_sprints)

    p = sub.add_parser(
        "issue-types",
        help="list Jira issue types available for issue creation in a project",
    )
    p.add_argument("--project", required=True, help="project key or ID")
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override (default: {JIRA_BASE_URL_ENV})",
    )
    add_list_paging_arguments(p, noun="issue types")
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_issue_types)

    p = sub.add_parser(
        "fields",
        help="discover Jira create or edit field requirements",
    )
    p.add_argument("issue", nargs="?", help="issue key or URL for edit-field discovery")
    p.add_argument("--project", help="project key or ID for create-field discovery")
    p.add_argument("--type", help="issue type name or ID for create-field discovery")
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_fields)

    p = sub.add_parser("link-types", help="list Jira issue link types")
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override (default: {JIRA_BASE_URL_ENV})",
    )
    add_list_paging_arguments(p, noun="link types")
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_link_types)

    p = sub.add_parser(
        "transitions", help="list available Jira transitions for an issue"
    )
    p.add_argument("issue", help="issue key or URL")
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override for bare issue keys (default: {JIRA_BASE_URL_ENV})",
    )
    add_list_paging_arguments(p, noun="transitions")
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_transitions)

    p = sub.add_parser(
        "add-to-sprint",
        help="preview or add an issue to a Jira sprint without custom field plumbing",
    )
    p.add_argument("issue", help="issue key or URL")
    target_mode = p.add_mutually_exclusive_group(required=True)
    target_mode.add_argument(
        "--current",
        action="store_true",
        help="resolve the unique active sprint for the requested project or board",
    )
    target_mode.add_argument("--sprint", type=positive_int, help="explicit sprint ID")
    p.add_argument("--project", help="project key or ID used to resolve sprint context")
    p.add_argument("--board", type=positive_int, help="explicit scrum board ID")
    p.add_argument(
        "--apply",
        action="store_true",
        help="add the issue to the sprint; default is preview only",
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override for bare issue keys (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_add_to_sprint)

    p = sub.add_parser(
        "create",
        help="preview or create a Jira issue with explicit fields and Markdown body input",
    )
    p.add_argument("--project", required=True, help="target project key or ID")
    p.add_argument("--type", required=True, help="issue type name or ID")
    p.add_argument("--title", required=True, help="issue summary/title")
    p.add_argument("--body", help="inline Markdown description")
    p.add_argument("--body-file", help="read Markdown description from a file")
    p.add_argument(
        "--stdin", action="store_true", help="read Markdown description from stdin"
    )
    sprint_mode = p.add_mutually_exclusive_group()
    sprint_mode.add_argument(
        "--current-sprint",
        action="store_true",
        help="resolve the unique active sprint for the target project or board",
    )
    sprint_mode.add_argument("--sprint", type=positive_int, help="explicit sprint ID")
    p.add_argument(
        "--board",
        type=positive_int,
        help="explicit scrum board ID for sprint resolution",
    )
    p.add_argument("--priority", help="priority name or ID")
    p.add_argument("--assignee", help="assignee account ID")
    p.add_argument("--parent", help="parent issue key")
    p.add_argument(
        "--label",
        dest="labels",
        action="append",
        default=[],
        help="label to add; repeat to add more than one",
    )
    p.add_argument(
        "--component",
        dest="components",
        action="append",
        default=[],
        help="component name or ID; repeat to add more than one",
    )
    p.add_argument(
        "--field",
        action="append",
        default=[],
        help="explicit FIELD=VALUE assignment; repeat as needed",
    )
    p.add_argument(
        "--relates",
        action="append",
        default=[],
        help="related issue key to link after create; repeat as needed",
    )
    p.add_argument(
        "--link-type",
        default="Relates",
        help="issue link type name or ID for --relates",
    )
    p.add_argument(
        "--remote-link",
        action="append",
        default=[],
        help="external URL to attach as a Jira remote link; repeat as needed",
    )
    p.add_argument(
        "--remote-link-relationship",
        default="relates to",
        help="relationship label to use for --remote-link entries",
    )
    p.add_argument(
        "--apply", action="store_true", help="create the issue; default is preview only"
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser(
        "update",
        help="preview or update Jira fields and description content for an existing issue",
    )
    p.add_argument("issue", help="issue key or URL")
    p.add_argument(
        "--project",
        help="project key or ID used to resolve sprint context; defaults to the issue project",
    )
    p.add_argument("--summary", help="replace the issue summary/title")
    p.add_argument("--body", help="inline Markdown body used for description updates")
    p.add_argument(
        "--body-file", help="read Markdown body from a file for description updates"
    )
    p.add_argument(
        "--stdin",
        action="store_true",
        help="read Markdown body from stdin for description updates",
    )
    sprint_mode = p.add_mutually_exclusive_group()
    sprint_mode.add_argument(
        "--current-sprint",
        action="store_true",
        help="resolve the unique active sprint for the issue project or requested board",
    )
    sprint_mode.add_argument("--sprint", type=positive_int, help="explicit sprint ID")
    p.add_argument(
        "--board",
        type=positive_int,
        help="explicit scrum board ID for sprint resolution",
    )
    description_mode = p.add_mutually_exclusive_group()
    description_mode.add_argument(
        "--replace-description",
        action="store_true",
        help="replace the issue description with the provided body",
    )
    description_mode.add_argument(
        "--append-section", help="append a Markdown section under the given heading"
    )
    description_mode.add_argument(
        "--prepend-section", help="prepend a Markdown section under the given heading"
    )
    description_mode.add_argument(
        "--upsert-section", help="replace or append a Markdown section by heading"
    )
    p.add_argument("--priority", help="priority name or ID")
    p.add_argument("--assignee", help="assignee account ID")
    p.add_argument("--parent", help="parent issue key")
    p.add_argument(
        "--label",
        dest="labels",
        action="append",
        default=[],
        help="label to add; repeat to add more than one",
    )
    p.add_argument(
        "--component",
        dest="components",
        action="append",
        default=[],
        help="component name or ID; repeat to add more than one",
    )
    p.add_argument(
        "--field",
        action="append",
        default=[],
        help="explicit FIELD=VALUE assignment; repeat as needed",
    )
    p.add_argument(
        "--apply", action="store_true", help="apply the update; default is preview only"
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override for bare issue keys (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser(
        "comment",
        help="preview or add a Jira issue comment from Markdown input",
    )
    p.add_argument("issue", help="issue key or URL")
    p.add_argument("--body", help="inline Markdown comment body")
    p.add_argument("--body-file", help="read Markdown comment body from a file")
    p.add_argument(
        "--stdin", action="store_true", help="read Markdown comment body from stdin"
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="create the comment; default is preview only",
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override for bare issue keys (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("link", help="preview or add a Jira issue link or remote link")
    p.add_argument("issue", help="source issue key or URL")
    p.add_argument("target", help="target issue key or external URL")
    p.add_argument(
        "--type",
        default="Relates",
        help="issue link type name or ID when linking another issue",
    )
    p.add_argument(
        "--relationship",
        default="relates to",
        help="relationship label for external remote links",
    )
    p.add_argument("--title", help="title override for remote links")
    p.add_argument("--summary", help="summary text for remote links")
    p.add_argument(
        "--apply", action="store_true", help="create the link; default is preview only"
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override for bare issue keys (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("transition", help="preview or transition a Jira issue")
    p.add_argument("issue", help="issue key or URL")
    p.add_argument("--to", required=True, help="transition name or ID")
    p.add_argument(
        "--field",
        action="append",
        default=[],
        help="transition FIELD=VALUE assignment; repeat as needed",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="run the transition; default is preview only",
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override for bare issue keys (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_transition)

    p = sub.add_parser(
        "mcp",
        help="start mcp-atlassian with the same managed local OAuth/session contract",
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override for MCP startup (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument(
        "mcp_args",
        nargs=argparse.REMAINDER,
        help="raw mcp-atlassian flags after --",
    )
    p.set_defaults(func=cmd_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if is_long_help_request(argv):
        return print_long_help(parser)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        raise SystemExit(main(sys.argv[1:]))
    except BrokenPipeError:
        raise SystemExit(0)
    except ToolError as exc:
        raise SystemExit(die(str(exc)))
