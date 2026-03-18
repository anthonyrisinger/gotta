#!/usr/bin/env python3
"""Standalone Jira read toolbelt."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any

from gotta.config import set_provider_env_values
from gotta.helptext import is_long_help_request, print_long_help
from gotta.routing import query_route, strip_http_url_fragment
from gotta.source import derive_source_metadata_from_payload, render_source_metadata_lines
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


def positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer, got: {raw}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return value


load_atlassian_config_env = atl.load_atlassian_config_env
is_interactive = atl.is_interactive
load_oauth_runtime_config = atl.load_oauth_runtime_config
api_json = atl.api_json
site_root = atl.site_root
token_preflight_status = atl.token_preflight_status
load_cloud_id = atl.load_cloud_id
atlassian_status_payload = atl.atlassian_status_payload


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
        if allow_reauth and is_interactive() and token_preflight_status(token) == "invalid":
            run_oauth_bootstrap(base_url=base_url)
            return load_session(base_url, allow_reauth=False)
        raise


def default_base_url() -> str:
    config_env = load_atlassian_config_env()
    return site_root(
        atl.env_or_config(config_env, JIRA_BASE_URL_ENV, default=DEFAULT_JIRA_BASE_URL).strip()
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


def _search_payload_for_issue(envelope: dict[str, Any], *, query: str) -> dict[str, Any]:
    return {
        "query": query,
        "limit": 1,
        "requestedNext": "",
        "next": "",
        "size": 1,
        "results": [
            {
                "key": envelope.get("key"),
                "summary": envelope.get("summary"),
                "status": envelope.get("status"),
                "issueType": envelope.get("issueType"),
                "project": envelope.get("project"),
                "priority": envelope.get("priority"),
                "assignee": envelope.get("assignee"),
                "labels": envelope.get("labels") or [],
                "updated": envelope.get("updated") or "",
                "issueUrl": envelope.get("issueUrl") or "",
            }
        ],
        "source_created_at": envelope.get("created") or "",
        "source_updated_at": envelope.get("updated") or "",
    }


def canonical_locator(argv: list[str]) -> str:
    args = _parse_cli(argv)
    if args.command == "get":
        return f"jira:{_issue_key_for_locator(args.issue)}"
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
        return f"{issue_key}.{_output_extension(args.output)}"
    if args.command in {"search", "jql"}:
        return (
            f"jira-{args.command}-{_slug(args.query, fallback='jira')}"
            f".{_output_extension(args.output)}"
        )
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
        return ["get", target.removeprefix("jira:")]
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
    return IssueRef(issue_key=normalize_issue_key(url_match.group(1)), base_url=base_url)


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
    for child in node.get("content", []) if isinstance(node.get("content"), list) else []:
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
                inner.extend(f"> {line}" if line else ">" for line in block.splitlines())
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
                inner.extend(f"> {line}" if line else ">" for line in block.splitlines())
        return "\n".join(inner)
    return "".join(render_adf_inline(child) for child in content).strip()


def issue_url(base_url: str, issue_key: str) -> str:
    return f"{site_root(base_url)}/browse/{issue_key}"


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
    return {
        "siteUrl": envelope.get("siteUrl"),
        "issueUrl": envelope.get("issueUrl"),
        "id": envelope.get("id"),
        "key": envelope.get("key"),
        "summary": envelope.get("summary"),
        "status": envelope.get("status"),
        "issueType": envelope.get("issueType"),
        "project": envelope.get("project"),
        "priority": envelope.get("priority"),
        "assignee": envelope.get("assignee"),
        "reporter": envelope.get("reporter"),
        "labels": envelope.get("labels"),
        "created": envelope.get("created"),
        "updated": envelope.get("updated"),
    }


def jira_issue_api_url(session: Session, issue_key: str, fields: list[str]) -> str:
    query = urllib.parse.urlencode({"fields": ",".join(fields)})
    return (
        f"https://api.atlassian.com/ex/jira/{session.cloud_id}/rest/api/3/"
        f"issue/{urllib.parse.quote(issue_key)}?{query}"
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
    return normalize_issue(payload, base_url=session.base_url, include_description=True)


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
) -> dict[str, Any]:
    session = load_session(base_url)
    payload = {
        "jql": jql,
        "maxResults": limit,
        "fields": fields,
    }
    if cursor:
        payload["nextPageToken"] = cursor
    response = api_json("POST", jira_search_api_url(session), session.token, payload=payload)
    if not isinstance(response, dict):
        raise ToolError("unexpected Jira search response")
    issues = response.get("issues")
    if not isinstance(issues, list):
        issues = []
    results = [
        {
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
                "project",
                "priority",
                "assignee",
                "labels",
                "updated",
            }
        }
        for item in issues
        if isinstance(item, dict)
    ]
    return {
        "query": jql,
        "limit": limit,
        "requestedNext": cursor,
        "next": str(response.get("nextPageToken") or ""),
        "size": len(results),
        "results": results,
    }


def markdown_issue(envelope: dict[str, Any]) -> str:
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
    lines = [
        "# Jira Search",
        "",
        f"- Query: `{payload.get('query') or ''}`",
        f"- Results: {payload.get('size') or 0}",
    ]
    lines.extend(render_source_metadata_lines(derive_source_metadata_from_payload(payload)))
    next_token = str(payload.get("next") or "")
    if next_token:
        lines.append(f"- Next: `{next_token}`")
    lines.append("")
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
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
        selected_base_url = str(session.base_url or status.get("baseUrl") or base_url).strip()
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
    )
    if args.output == "json":
        print_json(payload)
        return 0
    sys.stdout.write(render_search_markdown(payload))
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
            "Standalone Jira fetch/search toolbelt. Interactive commands may open "
            "browser reauthorization if the cached Atlassian session is invalid."
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
    p.add_argument("--check", action="store_true", help="run a token preflight against Atlassian")
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
    p.add_argument("--limit", type=positive_int, default=10, help="maximum results to request")
    p.add_argument("--next", help="continuation token from a previous Jira search response")
    p.add_argument("--output", choices=["markdown", "json"], default="markdown")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("jql", help="search Jira issues with raw JQL")
    p.add_argument("query", help="raw JQL search expression")
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Jira base URL override (default: {JIRA_BASE_URL_ENV})",
    )
    p.add_argument("--limit", type=positive_int, default=10, help="maximum results to request")
    p.add_argument("--next", help="continuation token from a previous Jira search response")
    p.add_argument("--output", choices=["markdown", "json"], default="markdown")
    p.set_defaults(func=cmd_jql)

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
