#!/usr/bin/env python3
"""Standalone Confluence page-body toolbelt."""

from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gotta.capture import Capture, capture_json_command, json_bytes
from gotta.config import set_provider_env_values
from gotta.drawio import DRAWIO_MIME, summarize_drawio
from gotta.helptext import is_long_help_request, print_long_help
from gotta.project import pretty_json
from gotta.resolve.route import query_route, strip_http_url_fragment
from gotta.source.render import render_source_metadata_lines
from gotta.source.stamp import derive_source_metadata_from_payload
from gotta.providers import atlassian as atl


DEFAULT_FIND_CONTEXT_CHARS = 120
DEFAULT_CONFLUENCE_BASE_URL = ""
CONFLUENCE_BASE_URL_ENV = "GOTTA_CONFLUENCE_BASE_URL"
PAGE_ARG_HELP = (
    "Confluence page URL, space landing/overview URL, or numeric page ID "
    f"against {CONFLUENCE_BASE_URL_ENV} when a Confluence base URL is persisted"
)
CONTENT_ARG_HELP = (
    "Confluence page, blog post, or comment URL, or numeric content ID "
    f"against {CONFLUENCE_BASE_URL_ENV} when a Confluence base URL is persisted"
)
DISALLOWED_MCP_PASSTHROUGH_FLAGS = atl.DISALLOWED_MCP_PASSTHROUGH_FLAGS
ToolError = atl.AtlassianError


@dataclass
class PageRef:
    page_id: str | None = None
    base_url: str = ""
    space_key: str = ""


@dataclass
class ContentRef:
    requested_id: str | None = None
    page_id: str | None = None
    comment_id: str | None = None
    base_url: str = ""
    space_key: str = ""
    allow_comment_fallback: bool = False


@dataclass
class Session:
    token: str
    cloud_id: str
    base_url: str


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


site_root = atl.site_root
read_text = atl.read_text
load_atlassian_config_env = atl.load_atlassian_config_env
load_oauth_runtime_config = atl.load_oauth_runtime_config
is_interactive = atl.is_interactive
api_json = atl.api_json
api_bytes = atl.api_bytes
token_preflight_status = atl.token_preflight_status
load_cloud_id = atl.load_cloud_id
atlassian_status_payload = atl.atlassian_status_payload


def default_base_url() -> str:
    config_env = load_atlassian_config_env()
    return atl.env_or_config(
        config_env,
        CONFLUENCE_BASE_URL_ENV,
        default=DEFAULT_CONFLUENCE_BASE_URL,
    ).strip()


def _slug(value: str, *, fallback: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    return value.strip("-") or fallback


def _output_extension(output: str) -> str:
    return {
        "markdown": "md",
        "json": "json",
        "body": "body.html",
        "meta": "json",
        "summary": "summary",
        "text": "txt",
    }.get(output, "md")


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def canonical_locator(argv: list[str]) -> str:
    args = _parse_cli(argv)
    if args.command == "get":
        content_ref = parse_content_ref(args.page)
        if content_ref.comment_id:
            return f"confluence:{content_ref.comment_id}"
        if content_ref.requested_id:
            return f"confluence:{content_ref.requested_id}"
        return f"confluence:{args.page}"
    if args.command == "search":
        return f"confluence:search {args.query}"
    if args.command == "cql":
        return f"confluence:cql {args.query}"
    if args.command == "status":
        return "confluence:status"
    return f"confluence:{shlex.join(argv)}"


def preferred_name(argv: list[str], options: object) -> str:
    if getattr(options, "save_as", ""):
        return str(getattr(options, "save_as"))
    args = _parse_cli(argv)
    if args.command == "get":
        content_ref = parse_content_ref(args.page)
        content_id = content_ref.comment_id or content_ref.requested_id or ""
        if content_id:
            return f"{content_id}.html"
        parsed = urllib.parse.urlparse(args.page)
        page_name = Path(parsed.path.rstrip("/")).name if parsed.scheme else args.page
        return f"{_slug(page_name, fallback='confluence')}.html"
    if args.command == "search":
        return f"confluence-search-{_slug(args.query, fallback='confluence')}.json"
    if args.command == "cql":
        return f"confluence-cql-{_slug(args.query, fallback='confluence')}.json"
    if args.command == "status":
        return f"confluence.{_output_extension(args.output)}"
    return "confluence.txt"


def route_target(target: str) -> list[str] | None:
    if target.startswith("https://") and ".atlassian.net/wiki/" in target:
        if any(char.isspace() for char in target):
            return None
        return ["get", strip_http_url_fragment(target)]
    if target.startswith("confluence:search "):
        return query_route(
            "search",
            target.removeprefix("confluence:search "),
            valued_flags=(
                "--space",
                "--type",
                "--limit",
                "--next",
                "--output",
                "--base-url",
            ),
            boolean_flags=("--title-only",),
        )
    if target.startswith("confluence:cql "):
        return query_route(
            "cql",
            target.removeprefix("confluence:cql "),
            valued_flags=("--limit", "--next", "--base-url"),
        )
    if target.startswith("confluence:"):
        return ["get", target.removeprefix("confluence:")]
    return None


def persist_selected_base_urls(base_url: str) -> None:
    confluence_url = base_url.strip().rstrip("/")
    site_url = site_root(confluence_url)
    if not site_url:
        return
    set_provider_env_values(
        "atlassian",
        {
            "GOTTA_JIRA_BASE_URL": site_url,
            CONFLUENCE_BASE_URL_ENV: confluence_url or f"{site_url}/wiki",
        },
    )


def run_oauth_bootstrap(*, base_url: str = "") -> dict[str, Any]:
    return atl.run_oauth_bootstrap(
        base_url=base_url,
        base_url_env=CONFLUENCE_BASE_URL_ENV,
    )


def _is_blogpost_ref(raw: str) -> bool:
    candidate = raw.strip().removeprefix("confluence:")
    parsed = urllib.parse.urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        return False
    return bool(
        re.search(r"/wiki/(?:spaces/[^/]+/)?blog(?:/[^/]+)*/\d+(?:/|$)", parsed.path)
    )


def parse_page_ref(raw: str, *, allow_blogpost: bool = False) -> PageRef:
    if raw.startswith("confluence:"):
        raw = raw.removeprefix("confluence:")
    if not allow_blogpost and _is_blogpost_ref(raw):
        raise ToolError(
            "blog post refs are not supported by this command; use gotta confluence get "
            "for blog content"
        )
    page_id: str | None = atl.extract_confluence_page_id(raw)
    space_key = ""
    base_url = default_base_url()
    base_match = re.match(r"^(https://[^/]+\.atlassian\.net)(?:/wiki/.*)?$", raw)
    if base_match:
        base_url = f"{base_match.group(1)}/wiki"
    if not page_id:
        overview_match = re.match(
            r"^(https://[^/]+\.atlassian\.net)/wiki/spaces/([^/]+)(?:/overview)?/?$",
            raw,
        )
        if overview_match:
            base_url = overview_match.group(1)
            space_key = urllib.parse.unquote(overview_match.group(2))
    if page_id is None and not space_key:
        raise ToolError(f"could not parse Confluence page ID from input: {raw}")
    return PageRef(page_id=page_id, base_url=base_url, space_key=space_key)


def _extract_focused_comment_id(raw: str) -> str:
    parsed = urllib.parse.urlparse(raw.strip())
    params = urllib.parse.parse_qs(parsed.query)
    return str(params.get("focusedCommentId", [""])[0] or "").strip()


def parse_content_ref(raw: str) -> ContentRef:
    candidate = raw.strip()
    if candidate.startswith("confluence:"):
        candidate = candidate.removeprefix("confluence:")
    base_url = default_base_url()
    base_match = re.match(r"^(https://[^/]+\.atlassian\.net)(?:/wiki/.*)?$", candidate)
    if base_match:
        base_url = f"{base_match.group(1)}/wiki"
    focused_comment_id = _extract_focused_comment_id(candidate)
    if focused_comment_id.isdigit():
        return ContentRef(
            requested_id=focused_comment_id,
            page_id=atl.extract_confluence_page_id(candidate),
            comment_id=focused_comment_id,
            base_url=base_url,
        )
    page_ref = parse_page_ref(candidate, allow_blogpost=True)
    requested_id = page_ref.page_id
    return ContentRef(
        requested_id=requested_id,
        page_id=page_ref.page_id,
        base_url=page_ref.base_url,
        space_key=page_ref.space_key,
        allow_comment_fallback=candidate.isdigit(),
    )


def load_token(base_url: str = "") -> str:
    return atl.load_token(
        base_url=base_url,
        auth_command="confluence",
        base_url_env=CONFLUENCE_BASE_URL_ENV,
    )


def should_reauth_after_api_error(token: str) -> bool:
    return token_preflight_status(token) == "invalid"


def discover_cloud_id(token: str, base_url: str) -> str:
    return atl.discover_cloud_id(
        token,
        base_url,
        base_url_env=CONFLUENCE_BASE_URL_ENV,
    )


def load_session(page_ref: PageRef, *, allow_reauth: bool = True) -> Session:
    token = load_token(page_ref.base_url)
    try:
        if page_ref.base_url:
            cloud_id = discover_cloud_id(token, page_ref.base_url)
            resolved_base_url = site_root(page_ref.base_url)
        else:
            cloud_id, resolved_base_url = atl.resolve_accessible_resource(
                token,
                "",
                base_url_env=CONFLUENCE_BASE_URL_ENV,
                cloud_id=load_cloud_id(),
            )
        return Session(token=token, cloud_id=cloud_id, base_url=resolved_base_url)
    except ToolError:
        if (
            allow_reauth
            and is_interactive()
            and token_preflight_status(token) == "invalid"
        ):
            run_oauth_bootstrap(base_url=page_ref.base_url)
            return load_session(page_ref, allow_reauth=False)
        raise


def page_api_url(
    session: Session, page_id: str, *, body_format: str = "storage"
) -> str:
    params = urllib.parse.urlencode({"body-format": body_format})
    return (
        f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/api/v2/"
        f"pages/{page_id}?{params}"
    )


def blogpost_api_url(
    session: Session, page_id: str, *, body_format: str = "storage"
) -> str:
    params = urllib.parse.urlencode({"body-format": body_format})
    return (
        f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/api/v2/"
        f"blogposts/{page_id}?{params}"
    )


def pages_api_url(
    session: Session,
    *,
    title: str = "",
    space_id: str = "",
    status: str = "",
    limit: int = 25,
    cursor: str | None = None,
) -> str:
    params: dict[str, Any] = {"limit": limit}
    if title:
        params["title"] = title
    if space_id:
        params["space-id"] = space_id
    if status:
        params["status"] = status
    if cursor:
        params["cursor"] = cursor
    query = urllib.parse.urlencode(params, doseq=True)
    return (
        f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/api/v2/"
        f"pages?{query}"
    )


def pages_collection_url(session: Session) -> str:
    return (
        f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/api/v2/pages"
    )


def comment_api_url(
    session: Session,
    comment_id: str,
    *,
    comment_kind: str,
    body_format: str = "storage",
) -> str:
    params = urllib.parse.urlencode({"body-format": body_format})
    return (
        f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/api/v2/"
        f"{comment_kind}/{comment_id}?{params}"
    )


def attachment_api_url(session: Session, attachment_id: str) -> str:
    return (
        f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/api/v2/"
        f"attachments/{attachment_id}"
    )


def attachment_download_api_url(
    session: Session,
    *,
    page_id: str,
    attachment_id: str,
) -> str:
    return (
        f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/rest/api/"
        f"content/{page_id}/child/attachment/{attachment_id}/download"
    )


def page_attachments_api_url(
    session: Session,
    page_id: str,
    *,
    filename: str = "",
    limit: int = 25,
) -> str:
    params: dict[str, Any] = {"limit": limit}
    if filename:
        params["filename"] = filename
    return (
        f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/api/v2/"
        f"pages/{page_id}/attachments?{urllib.parse.urlencode(params, doseq=True)}"
    )


def custom_content_attachments_api_url(
    session: Session,
    custom_content_id: str,
    *,
    filename: str = "",
    limit: int = 25,
) -> str:
    params: dict[str, Any] = {"limit": limit}
    if filename:
        params["filename"] = filename
    return (
        f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/api/v2/"
        f"custom-content/{custom_content_id}/attachments?{urllib.parse.urlencode(params, doseq=True)}"
    )


def search_api_url(
    session: Session, cql: str, *, limit: int, cursor: str | None
) -> str:
    params: dict[str, Any] = {"cql": cql, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    query = urllib.parse.urlencode(params, doseq=True)
    return (
        f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/rest/api/"
        f"search?{query}"
    )


def cql_string_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def absolutize_confluence_url(url: str, *, base_url: str) -> str:
    if not url:
        return ""
    if re.match(r"^https?://", url):
        return url
    if base_url:
        return urllib.parse.urljoin(base_url.rstrip("/") + "/", url.lstrip("/"))
    return url


def extract_cursor(link: str) -> str:
    parsed = urllib.parse.urlparse(link)
    params = urllib.parse.parse_qs(parsed.query)
    return str(params.get("cursor", [""])[0] or "")


def build_text_search_cql(args: argparse.Namespace) -> str:
    clauses: list[str] = []
    field = "title" if args.title_only else "text"
    clauses.append(f"{field} ~ {cql_string_literal(args.query)}")
    if args.space:
        clauses.append(f"space = {cql_string_literal(args.space)}")
    if args.type and args.type != "all":
        clauses.append(f"type = {args.type}")
    return " AND ".join(clauses)


def normalize_search_result(result: dict[str, Any], *, base_url: str) -> dict[str, Any]:
    content = result.get("content")
    if not isinstance(content, dict):
        content = {}
    space = content.get("space")
    if not isinstance(space, dict):
        space = {}
    container = result.get("container")
    if not isinstance(container, dict):
        container = {}
    normalized = {
        "title": result.get("title") or content.get("title"),
        "id": content.get("id") or result.get("id"),
        "type": content.get("type") or result.get("entityType"),
        "url": absolutize_confluence_url(
            str(result.get("url") or ""), base_url=base_url
        ),
    }
    excerpt = result.get("excerpt")
    if excerpt:
        normalized["excerptHtml"] = excerpt
    if space.get("key"):
        normalized["spaceKey"] = space.get("key")
    if space.get("name"):
        normalized["spaceName"] = space.get("name")
    if container.get("title"):
        normalized["containerTitle"] = container.get("title")
    if result.get("lastModified"):
        normalized["lastModified"] = result.get("lastModified")
    if result.get("friendlyLastModified"):
        normalized["friendlyLastModified"] = result.get("friendlyLastModified")
    return normalized


def render_excerpt_text(excerpt_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", excerpt_html)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def render_search_markdown(payload: dict[str, Any]) -> str:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return (
            f"### Confluence Search: {payload.get('cql') or ''}\n\n"
            f"- _Type_: `{payload.get('type') or 'page'}`\n"
            f"- _Matches_: 0\n\n"
            "No Confluence content matched.\n"
        )
    lines: list[str] = [
        f"### Confluence Search: {payload.get('cql') or ''}",
        "",
        "- _Surface_: `confluence`",
        f"- _Type_: `{payload.get('type') or 'page'}`",
        f"- _Matches_: {payload.get('size') or len(results)}",
    ]
    lines.extend(
        render_source_metadata_lines(derive_source_metadata_from_payload(payload))
    )
    if payload.get("next"):
        lines.append(f"- _Next_: `{payload['next']}`")
    if payload.get("previous"):
        lines.append(f"- _Previous_: `{payload['previous']}`")
    lines.append("")
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "(untitled)")
        url = str(item.get("url") or "")
        content_id = str(item.get("id") or "")
        content_type = str(item.get("type") or "")
        space_key = str(item.get("spaceKey") or "")
        space_name = str(item.get("spaceName") or "")
        modified = str(
            item.get("lastModified") or item.get("friendlyLastModified") or ""
        )
        line = f"- [{title}]({url})"
        details: list[str] = []
        if content_id:
            details.append(f"locator `confluence:{content_id}`")
        if content_type:
            details.append(f"type `{content_type}`")
        if space_key:
            details.append(f"space `{space_key}`")
        elif space_name:
            details.append(f"space `{space_name}`")
        if modified:
            details.append(f"modified `{modified}`")
        if details:
            line += " - " + ", ".join(details)
        lines.append(line)
        excerpt = str(item.get("excerptHtml") or "").strip()
        if excerpt:
            lines.append(f"  - {render_excerpt_text(excerpt)}")
    return "\n".join(lines) + "\n"


def search_confluence(
    session: Session,
    *,
    cql: str,
    limit: int,
    cursor: str | None,
    allow_reauth: bool = True,
) -> dict[str, Any]:
    try:
        payload = api_json(
            "GET",
            search_api_url(session, cql, limit=limit, cursor=cursor),
            session.token,
        )
        if not isinstance(payload, dict):
            raise ToolError("unexpected search response")
        links = payload.get("_links")
        if not isinstance(links, dict):
            links = {}
        results = payload.get("results")
        if not isinstance(results, list):
            results = []
        base_url = str(links.get("base") or session.base_url or "").strip()
        next_link = str(links.get("next") or "")
        prev_link = str(links.get("prev") or "")
        type_match = re.search(r"type = ([a-z]+)", cql or "")
        response = {
            "baseUrl": base_url,
            "cql": cql,
            "limit": limit,
            "size": len(results),
            "type": type_match.group(1) if type_match else "",
            "results": [
                normalize_search_result(item, base_url=base_url)
                for item in results
                if isinstance(item, dict)
            ],
        }
        if cursor:
            response["requestedNext"] = cursor
        if next_link:
            response["next"] = extract_cursor(next_link)
        if prev_link:
            response["previous"] = extract_cursor(prev_link)
        return response
    except ToolError:
        if (
            allow_reauth
            and is_interactive()
            and should_reauth_after_api_error(session.token)
        ):
            run_oauth_bootstrap(base_url=session.base_url)
            refreshed_session = load_session(
                PageRef(base_url=session.base_url),
                allow_reauth=False,
            )
            return search_confluence(
                refreshed_session,
                cql=cql,
                limit=limit,
                cursor=cursor,
                allow_reauth=False,
            )
        raise


def resolve_page_id(session: Session, page_ref: PageRef) -> str:
    if page_ref.page_id:
        return page_ref.page_id
    if not page_ref.space_key:
        raise ToolError("missing page id and space key")
    params = urllib.parse.urlencode({"keys": [page_ref.space_key]}, doseq=True)
    url = (
        f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/api/v2/"
        f"spaces?{params}"
    )
    payload = api_json("GET", url, session.token)
    if not isinstance(payload, dict):
        raise ToolError("unexpected space lookup response")
    results = payload.get("results")
    if (
        not isinstance(results, list)
        or len(results) != 1
        or not isinstance(results[0], dict)
    ):
        raise ToolError(
            f"could not resolve unique Confluence homepage for space {page_ref.space_key}"
        )
    homepage_id = str(results[0].get("homepageId") or "").strip()
    if not homepage_id:
        raise ToolError(
            f"space {page_ref.space_key} does not expose a homepageId via the Confluence API"
        )
    return homepage_id


def fetch_page(
    page_ref: PageRef, *, allow_reauth: bool = True
) -> tuple[Session, dict[str, Any]]:
    session: Session | None = None
    try:
        session = load_session(page_ref, allow_reauth=allow_reauth)
        page_id = resolve_page_id(session, page_ref)
        page = _fetch_page_payload(session, page_id)
        return session, page
    except ToolError:
        if (
            allow_reauth
            and is_interactive()
            and session is not None
            and should_reauth_after_api_error(session.token)
        ):
            run_oauth_bootstrap(base_url=page_ref.base_url)
            return fetch_page(page_ref, allow_reauth=False)
        raise


def _fetch_page_payload(session: Session, page_id: str) -> dict[str, Any]:
    page = api_json("GET", page_api_url(session, page_id), session.token)
    if not isinstance(page, dict):
        raise ToolError("unexpected page response")
    return page


def _fetch_blogpost_payload(session: Session, page_id: str) -> dict[str, Any]:
    page = api_json("GET", blogpost_api_url(session, page_id), session.token)
    if not isinstance(page, dict):
        raise ToolError("unexpected blogpost response")
    return page


def _fetch_page_like_payload(
    session: Session, page_id: str
) -> tuple[str, dict[str, Any]]:
    try:
        return "page", _fetch_page_payload(session, page_id)
    except ToolError as exc:
        if not _is_not_found_error(exc):
            raise
    return "blogpost", _fetch_blogpost_payload(session, page_id)


def page_web_url(session: Session, page_id: str) -> str:
    if not session.base_url or not page_id:
        return ""
    return f"{session.base_url.rstrip('/')}/wiki/pages/viewpage.action?pageId={page_id}"


def normalize_page_summary(page: dict[str, Any], session: Session) -> dict[str, Any]:
    normalized = {
        "id": str(page.get("id") or ""),
        "title": str(page.get("title") or ""),
        "status": str(page.get("status") or ""),
        "spaceId": str(page.get("spaceId") or ""),
        "parentId": str(page.get("parentId") or ""),
        "url": page_web_url(session, str(page.get("id") or "")),
    }
    version = page.get("version")
    if isinstance(version, dict):
        normalized["version"] = version
    created = str(page.get("createdAt") or "")
    if created:
        normalized["createdAt"] = created
    return normalized


def list_pages(
    session: Session,
    *,
    title: str = "",
    space_id: str = "",
    status: str = "current",
    limit: int = 25,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    payload = api_json(
        "GET",
        pages_api_url(
            session,
            title=title,
            space_id=space_id,
            status=status,
            limit=limit,
            cursor=cursor,
        ),
        session.token,
    )
    if not isinstance(payload, dict):
        raise ToolError("unexpected pages response")
    results = payload.get("results")
    if not isinstance(results, list):
        results = []
    links = payload.get("_links")
    if not isinstance(links, dict):
        links = {}
    next_link = str(links.get("next") or "")
    return [item for item in results if isinstance(item, dict)], extract_cursor(
        next_link
    )


def find_child_pages_by_title(
    session: Session,
    *,
    parent_page: dict[str, Any],
    title: str,
) -> list[dict[str, Any]]:
    parent_id = str(parent_page.get("id") or "")
    space_id = str(parent_page.get("spaceId") or "")
    if not parent_id or not space_id:
        raise ToolError("parent page is missing required id/spaceId metadata")
    matches: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        pages, cursor = list_pages(
            session,
            title=title,
            space_id=space_id,
            status="current",
            limit=100,
            cursor=cursor,
        )
        for page in pages:
            if str(page.get("title") or "") != title:
                continue
            if str(page.get("parentId") or "") != parent_id:
                continue
            matches.append(page)
        if not cursor:
            break
    return matches


def summarize_storage_html(
    storage_html: str, *, preview_chars: int = 240
) -> dict[str, Any]:
    normalized = re.sub(r"\s+", " ", storage_html).strip()
    preview = normalized[:preview_chars]
    if len(normalized) > preview_chars:
        preview += "..."
    return {
        "chars": len(storage_html),
        "preview": preview,
    }


def create_page(
    session: Session,
    *,
    parent_page: dict[str, Any],
    title: str,
    body_html: str,
    allow_reauth: bool = True,
) -> dict[str, Any]:
    parent_id = str(parent_page.get("id") or "")
    space_id = str(parent_page.get("spaceId") or "")
    if not parent_id or not space_id:
        raise ToolError("parent page is missing required id/spaceId metadata")
    payload = {
        "spaceId": space_id,
        "status": "current",
        "title": title,
        "parentId": parent_id,
        "body": {"representation": "storage", "value": body_html},
    }
    try:
        created = api_json(
            "POST",
            pages_collection_url(session),
            session.token,
            payload=payload,
        )
        if not isinstance(created, dict):
            raise ToolError("unexpected create response")
        page_id = str(created.get("id") or "")
        if not page_id:
            raise ToolError("create response did not include a page id")
        persisted = api_json("GET", page_api_url(session, page_id), session.token)
        if not isinstance(persisted, dict):
            raise ToolError("unexpected persisted page response after create")
        return persisted
    except ToolError:
        if (
            allow_reauth
            and is_interactive()
            and should_reauth_after_api_error(session.token)
        ):
            run_oauth_bootstrap(base_url=session.base_url)
            refreshed_session = load_session(
                PageRef(page_id=parent_id, base_url=session.base_url),
                allow_reauth=False,
            )
            return create_page(
                refreshed_session,
                parent_page=parent_page,
                title=title,
                body_html=body_html,
                allow_reauth=False,
            )
        raise


def _is_not_found_error(exc: ToolError) -> bool:
    return " failed with 404:" in str(exc)


def _fetch_comment_payload(
    session: Session,
    comment_id: str,
    *,
    page_id: str = "",
) -> dict[str, Any]:
    last_error: ToolError | None = None
    for comment_kind in ("footer-comments", "inline-comments"):
        try:
            comment = api_json(
                "GET",
                comment_api_url(session, comment_id, comment_kind=comment_kind),
                session.token,
            )
        except ToolError as exc:
            last_error = exc
            if _is_not_found_error(exc):
                continue
            raise
        if not isinstance(comment, dict):
            raise ToolError("unexpected comment response")
        comment["_comment_kind"] = comment_kind.removesuffix("s")
        if page_id and not str(comment.get("pageId") or "").strip():
            comment["pageId"] = page_id
        return comment
    if last_error is not None:
        raise last_error
    raise ToolError(f"could not resolve Confluence comment {comment_id}")


def fetch_read_target(
    content_ref: ContentRef, *, allow_reauth: bool = True
) -> tuple[Session, str, dict[str, Any]]:
    session: Session | None = None
    try:
        session = load_session(
            PageRef(base_url=content_ref.base_url), allow_reauth=allow_reauth
        )
        if content_ref.comment_id:
            comment = _fetch_comment_payload(
                session,
                content_ref.comment_id,
                page_id=str(content_ref.page_id or ""),
            )
            return session, "comment", comment
        page_ref = PageRef(
            page_id=content_ref.page_id,
            base_url=content_ref.base_url,
            space_key=content_ref.space_key,
        )
        try:
            page_id = resolve_page_id(session, page_ref)
            try:
                page = _fetch_page_payload(session, page_id)
                return session, "page", page
            except ToolError as exc:
                if not _is_not_found_error(exc):
                    raise
            if content_ref.allow_comment_fallback and content_ref.requested_id:
                try:
                    comment = _fetch_comment_payload(session, content_ref.requested_id)
                    return session, "comment", comment
                except ToolError as exc:
                    if not _is_not_found_error(exc):
                        raise
            blogpost = _fetch_blogpost_payload(session, page_id)
            return session, "blogpost", blogpost
        except ToolError:
            raise
    except ToolError:
        if (
            allow_reauth
            and is_interactive()
            and session is not None
            and should_reauth_after_api_error(session.token)
        ):
            run_oauth_bootstrap(base_url=content_ref.base_url)
            return fetch_read_target(content_ref, allow_reauth=False)
        raise


def storage_value(page: dict[str, Any]) -> str:
    return str(page["body"]["storage"]["value"])


def comment_storage_value(comment: dict[str, Any]) -> str:
    return str(comment["body"]["storage"]["value"])


def update_page(
    session: Session,
    page: dict[str, Any],
    new_body: str,
    *,
    message: str,
    allow_reauth: bool = True,
) -> dict[str, Any]:
    try:
        payload = {
            "id": str(page["id"]),
            "status": "current",
            "title": page["title"],
            "body": {"representation": "storage", "value": new_body},
            "version": {
                "number": int(page["version"]["number"]) + 1,
                "message": message,
                "minorEdit": True,
            },
        }
        url = (
            f"https://api.atlassian.com/ex/confluence/{session.cloud_id}/wiki/api/v2/"
            f"pages/{page['id']}"
        )
        updated = api_json("PUT", url, session.token, payload=payload)
        if not isinstance(updated, dict):
            raise ToolError("unexpected update response")
        persisted = api_json(
            "GET", page_api_url(session, str(page["id"])), session.token
        )
        if not isinstance(persisted, dict):
            raise ToolError("unexpected persisted page response after update")
        expected_version = int(page["version"]["number"]) + 1
        persisted_version = int(persisted["version"]["number"])
        if persisted_version != expected_version:
            raise ToolError(
                "persisted page version did not match update request: "
                f"expected {expected_version}, got {persisted_version}"
            )
        persisted["_update_echo_version"] = updated.get("version")
        return persisted
    except ToolError:
        if (
            allow_reauth
            and is_interactive()
            and should_reauth_after_api_error(session.token)
        ):
            run_oauth_bootstrap(base_url=session.base_url)
            refreshed_session = load_session(
                PageRef(page_id=str(page["id"]), base_url=session.base_url),
                allow_reauth=False,
            )
            return update_page(
                refreshed_session,
                page,
                new_body,
                message=message,
                allow_reauth=False,
            )
        raise


def ensure_base_version(page: dict[str, Any], expected_version: int | None) -> None:
    if expected_version is None:
        return
    current_version = int(page["version"]["number"])
    if current_version != expected_version:
        raise ToolError(
            "page version changed before update: "
            f"expected {expected_version}, got {current_version}"
        )


def format_update_result(updated: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": updated["id"],
        "title": updated["title"],
        "version": updated["version"],
    }
    echoed = updated.get("_update_echo_version")
    if isinstance(echoed, dict):
        result["updateEchoVersion"] = echoed
    return result


def replace_exact(body: str, old: str, new: str, *, expected_count: int) -> str:
    count = body.count(old)
    if count != expected_count:
        raise ToolError(
            f"expected {expected_count} match(es) for exact fragment, found {count}"
        )
    return body.replace(old, new)


def replace_between(
    body: str,
    start_marker: str,
    end_marker: str,
    replacement: str,
    *,
    include_markers: bool,
) -> str:
    start = body.find(start_marker)
    if start == -1:
        raise ToolError("start marker not found")
    search_from = start + len(start_marker)
    end = body.find(end_marker, search_from)
    if end == -1:
        raise ToolError("end marker not found")
    if end <= start:
        raise ToolError("end marker occurs before start marker")
    if include_markers:
        return body[:start] + replacement + body[end + len(end_marker) :]
    return body[:search_from] + replacement + body[end:]


def apply_operation(body: str, operation: dict[str, Any], *, index: int) -> str:
    op_type = str(operation.get("type", "")).strip()
    label = (
        str(operation.get("label", f"operation {index}")).strip()
        or f"operation {index}"
    )
    try:
        if op_type == "replace":
            old = read_explicit_input(
                operation.get("from"),
                operation.get("from_file"),
                input_name=f"{label} from input",
            )
            new = read_explicit_input(
                operation.get("to"),
                operation.get("to_file"),
                input_name=f"{label} to input",
            )
            expected_count = int(operation.get("expected_count", 1))
            return replace_exact(body, old, new, expected_count=expected_count)
        if op_type == "replace-section":
            start = str(operation.get("start", ""))
            end = str(operation.get("end", ""))
            if not start or not end:
                raise ToolError("replace-section requires start and end markers")
            replacement = read_explicit_input(
                operation.get("replacement"),
                operation.get("replacement_file"),
                input_name=f"{label} replacement input",
            )
            include_markers = bool(operation.get("include_markers", False))
            return replace_between(
                body,
                start,
                end,
                replacement,
                include_markers=include_markers,
            )
    except ToolError as exc:
        raise ToolError(f"{label}: {exc}") from exc
    raise ToolError(f"{label}: unsupported operation type: {op_type or '<missing>'}")


def load_operations(path: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(read_text(Path(path)))
    except json.JSONDecodeError as exc:
        raise ToolError(f"invalid batch JSON in {path}: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise ToolError("batch file must contain a non-empty JSON array of operations")
    operations: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ToolError(f"operation {index}: expected JSON object")
        operations.append(item)
    return operations


def format_noop_result(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": page["id"],
        "title": page["title"],
        "version": page["version"],
        "noop": True,
    }


def unified_diff(before: str, after: str, *, context: int = 2) -> str:
    lines = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile="before",
        tofile="after",
        n=context,
        lineterm="",
    )
    return "\n".join(lines)


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def count_with_context(body: str, needle: str, *, context_chars: int) -> dict[str, Any]:
    hits = []
    start = 0
    while True:
        idx = body.find(needle, start)
        if idx == -1:
            break
        hits.append(
            {
                "offset": idx,
                "snippet": body[
                    max(0, idx - context_chars) : idx + len(needle) + context_chars
                ],
            }
        )
        start = idx + max(1, len(needle))
    return {"count": len(hits), "matches": hits}


def read_explicit_input(
    inline: str | None, file_path: str | None, *, input_name: str
) -> str:
    if inline is not None and file_path is not None:
        raise ToolError(f"use either inline {input_name} or file input, not both")
    if file_path is not None:
        return read_text(Path(file_path))
    if inline is not None:
        return inline
    raise ToolError(f"missing {input_name}")


def read_single_input(
    inline: str | None, file_path: str | None, *, input_name: str
) -> str:
    if inline is not None and file_path is not None:
        raise ToolError(f"use either inline {input_name} or file input, not both")
    if file_path is not None:
        return read_text(Path(file_path))
    if inline is not None:
        return inline
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ToolError(f"missing {input_name}")


def render_markdown_to_storage(markdown: str) -> str:
    try:
        proc = subprocess.run(
            ["pandoc", "-f", "gfm", "-t", "html", "--wrap=none"],
            input=markdown,
            text=True,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise ToolError("pandoc is required for markdown conversion") from exc
    except subprocess.CalledProcessError as exc:
        raise ToolError(f"pandoc failed: {exc.stderr.strip()}") from exc
    return proc.stdout


def _normalize_heading_title(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def strip_matching_leading_h1(markdown: str, *, title: str) -> str:
    title_norm = _normalize_heading_title(title)
    if not title_norm:
        return markdown

    atx_match = re.match(
        r"^(?:[ \t]*\r?\n)*[ \t]{0,3}#(?!#)[ \t]+(?P<title>.*?)(?:[ \t]+#+)?[ \t]*(?:\r?\n|$)",
        markdown,
    )
    if (
        atx_match
        and _normalize_heading_title(str(atx_match.group("title") or "")) == title_norm
    ):
        return markdown[atx_match.end() :].lstrip("\r\n")

    setext_match = re.match(
        r"^(?:[ \t]*\r?\n)*(?P<title>[^\r\n]+)\r?\n[ \t]*=+[ \t]*(?:\r?\n|$)",
        markdown,
    )
    if (
        setext_match
        and _normalize_heading_title(str(setext_match.group("title") or ""))
        == title_norm
    ):
        return markdown[setext_match.end() :].lstrip("\r\n")

    return markdown


def _clean_markdown_projection(markdown: str) -> str:
    cleaned = re.sub(r"(?m)^wide760", "", markdown)
    cleaned = re.sub(
        r"(?m)^[0-9]+[ \t]+[0-9a-f]{8}-[0-9a-f-]{27}[ \t]+incomplete(?:\s+)*$",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?m)^[0-9]+(?:false|true)(?:default|list)(?:true|false)\s*$",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?m)^[0-9A-Za-z:/._-]*Untitled Diagram-[0-9]+\.drawio[0-9A-Za-z:/._-]*$",
        "",
        cleaned,
    )
    return cleaned


_DRAWIO_MACRO_RE = re.compile(
    r"<ac:structured-macro\b(?=[^>]*\bac:name=\"drawio\")[^>]*>(?P<body>.*?)</ac:structured-macro>",
    flags=re.DOTALL | re.IGNORECASE,
)
_DRAWIO_PARAM_RE = re.compile(
    r"<ac:parameter\b[^>]*ac:name=\"(?P<name>[^\"]+)\"[^>]*(?:>(?P<value>.*?)</ac:parameter>|/>)",
    flags=re.DOTALL | re.IGNORECASE,
)


def _strip_html_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def _parse_drawio_macro_params(macro_body: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for match in _DRAWIO_PARAM_RE.finditer(macro_body):
        name = str(match.group("name") or "").strip()
        value = str(match.group("value") or "")
        params[name] = html.unescape(_strip_html_tags(value)).strip()
    return params


def _html_list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _fetch_attachment_candidates(
    session: Session,
    *,
    page_id: str = "",
    custom_content_id: str = "",
    filename: str = "",
) -> list[dict[str, Any]]:
    urls: list[str] = []
    if custom_content_id:
        urls.append(
            custom_content_attachments_api_url(
                session,
                custom_content_id,
                filename=filename,
            )
        )
    if page_id:
        urls.append(page_attachments_api_url(session, page_id, filename=filename))
    candidates: list[dict[str, Any]] = []
    for url in urls:
        try:
            payload = api_json("GET", url, session.token)
        except ToolError as exc:
            if _is_not_found_error(exc):
                continue
            raise
        if not isinstance(payload, dict):
            continue
        results = payload.get("results")
        if not isinstance(results, list):
            continue
        for item in results:
            if isinstance(item, dict):
                candidates.append(item)
    return candidates


def _choose_attachment(
    candidates: list[dict[str, Any]],
    *,
    filename: str,
) -> dict[str, Any] | None:
    if not candidates:
        return None
    if filename:
        for candidate in candidates:
            if str(candidate.get("title") or "").strip() == filename:
                return candidate
    return candidates[0]


def _attachment_download_url(
    session: Session,
    attachment: dict[str, Any],
    *,
    page_id: str = "",
) -> str:
    attachment_id = str(attachment.get("id") or "").strip()
    attachment_page_id = str(attachment.get("pageId") or "").strip() or page_id
    if attachment_id and attachment_page_id:
        return attachment_download_api_url(
            session,
            page_id=attachment_page_id,
            attachment_id=attachment_id,
        )
    link = str(attachment.get("downloadLink") or "").strip()
    if not link:
        links = attachment.get("_links")
        if isinstance(links, dict):
            link = str(links.get("download") or "").strip()
    if not link:
        return ""
    return absolutize_confluence_url(
        link, base_url=f"{session.base_url.rstrip('/')}/wiki"
    )


def _resolve_drawio_attachment(
    session: Session,
    *,
    page_id: str,
    custom_content_id: str,
    filename: str,
) -> dict[str, Any] | None:
    candidates = _fetch_attachment_candidates(
        session,
        page_id=page_id,
        custom_content_id=custom_content_id,
        filename=filename,
    )
    attachment = _choose_attachment(candidates, filename=filename)
    if attachment is not None:
        return attachment
    if custom_content_id.isdigit():
        try:
            payload = api_json(
                "GET", attachment_api_url(session, custom_content_id), session.token
            )
        except ToolError as exc:
            if _is_not_found_error(exc):
                return None
            raise
        if isinstance(payload, dict):
            return payload
    return None


def _render_drawio_macro_html(
    session: Session | None,
    params: dict[str, str],
) -> str:
    diagram_name = (
        params.get("diagramDisplayName") or params.get("diagramName") or "(unnamed)"
    )
    page_id = params.get("pageId") or ""
    custom_content_id = params.get("custContentId") or params.get("contentId") or ""
    width = params.get("width") or ""
    height = params.get("height") or ""
    details = [
        f"<strong>Embedded draw.io diagram:</strong> <code>{html.escape(diagram_name)}</code>"
    ]
    meta_items: list[str] = []
    if custom_content_id:
        meta_items.append(
            f"Custom content ID: <code>{html.escape(custom_content_id)}</code>"
        )
    if page_id:
        meta_items.append(f"Page ID: <code>{html.escape(page_id)}</code>")
    if width or height:
        meta_items.append(
            "Configured size: <code>"
            + html.escape(f"{width or '?'}x{height or '?'}")
            + "</code>"
        )

    structure_items: list[str] = []
    if session is not None and diagram_name and (page_id or custom_content_id):
        attachment = _resolve_drawio_attachment(
            session,
            page_id=page_id,
            custom_content_id=custom_content_id,
            filename=diagram_name,
        )
        if attachment is not None:
            title = str(attachment.get("title") or "").strip()
            attachment_id = str(attachment.get("id") or "").strip()
            media_type = str(attachment.get("mediaType") or "").strip()
            download_url = _attachment_download_url(
                session, attachment, page_id=page_id
            )
            if attachment_id:
                meta_items.append(
                    f"Attachment ID: <code>{html.escape(attachment_id)}</code>"
                )
            if media_type:
                meta_items.append(
                    f"Attachment MIME type: <code>{html.escape(media_type)}</code>"
                )
            if title and title != diagram_name:
                meta_items.append(
                    f"Attachment title: <code>{html.escape(title)}</code>"
                )
            if download_url:
                try:
                    data = api_bytes("GET", download_url, session.token)
                except ToolError:
                    structure_items.append(
                        "Backing attachment resolved, but gotta could not download the diagram bytes."
                    )
                else:
                    if media_type == DRAWIO_MIME or title.endswith(".drawio"):
                        summary = summarize_drawio(data)
                        if summary.get("parsed"):
                            pages = summary.get("pages")
                            if isinstance(pages, list):
                                structure_items.append(
                                    f"Pages: <code>{len(pages)}</code>"
                                )
                                for page in pages[:5]:
                                    if not isinstance(page, dict):
                                        continue
                                    labels = page.get("labels")
                                    label_preview = ""
                                    if isinstance(labels, list):
                                        label_preview = ", ".join(
                                            html.escape(str(label))
                                            for label in labels[:4]
                                            if str(label).strip()
                                        )
                                    item = (
                                        f"<code>{html.escape(str(page.get('name') or '(unnamed)'))}</code>: "
                                        f"{int(page.get('vertexCount') or 0)} nodes, "
                                        f"{int(page.get('edgeCount') or 0)} edges"
                                    )
                                    if label_preview:
                                        item += f"; labels: {label_preview}"
                                    structure_items.append(item)
                                if len(pages) > 5:
                                    structure_items.append(
                                        f"... {len(pages) - 5} more page(s)"
                                    )
                        elif summary.get("decoded"):
                            structure_items.append(
                                "Diagram bytes resolved, but gotta could not parse the draw.io XML into graph structure."
                            )
                        else:
                            structure_items.append(
                                "Diagram attachment resolved, but the bytes were not a decodable draw.io mxfile."
                            )
                    else:
                        structure_items.append(
                            f"Resolved backing attachment, but it is not a draw.io mxfile: <code>{html.escape(media_type or 'unknown')}</code>."
                        )
            else:
                structure_items.append(
                    "Backing attachment resolved, but the Confluence attachment payload did not expose a download link."
                )
        else:
            structure_items.append(
                "The draw.io macro is present in canonical storage HTML, but gotta could not resolve the backing attachment yet."
            )
    else:
        structure_items.append(
            "The draw.io macro is present in canonical storage HTML. Resolve the backing attachment to summarize nodes and edges."
        )

    html_parts = [
        "<div>",
        f"<p>{details[0]}</p>",
    ]
    if meta_items:
        html_parts.append(_html_list(meta_items))
    if structure_items:
        html_parts.append("<p><strong>Structure</strong></p>")
        html_parts.append(_html_list(structure_items))
    html_parts.append("</div>")
    return "".join(html_parts)


def _replace_drawio_macros(
    storage_html: str,
    *,
    session: Session | None = None,
) -> str:
    def replace(match: re.Match[str]) -> str:
        params = _parse_drawio_macro_params(str(match.group("body") or ""))
        return _render_drawio_macro_html(session, params)

    return _DRAWIO_MACRO_RE.sub(replace, storage_html)


def _sanitize_storage_html_for_markdown(storage_html: str) -> str:
    cleaned = re.sub(r'\s(?:ac|ri):[A-Za-z0-9_-]+="[^"]*"', "", storage_html)
    cleaned = re.sub(r'\sdata-[A-Za-z0-9_-]+="[^"]*"', "", cleaned)
    cleaned = re.sub(
        r'<span class="placeholder-inline-tasks">\s*</span>',
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b[0-9]+\s+[0-9a-f]{8}-[0-9a-f-]{27}\s+incomplete\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def _projection_is_lossy(markdown: str) -> bool:
    return any(
        marker in markdown
        for marker in (
            "<table",
            "</table>",
            "<a href=",
            "Untitled Diagram-",
            "Embedded draw.io diagram",
            "placeholder-inline-tasks",
        )
    )


def render_storage_to_markdown(
    storage_html: str, *, session: Session | None = None
) -> str:
    storage_html = _replace_drawio_macros(storage_html, session=session)
    storage_html = _sanitize_storage_html_for_markdown(storage_html)
    try:
        proc = subprocess.run(
            ["pandoc", "-f", "html", "-t", "gfm", "--wrap=none"],
            input=storage_html,
            text=True,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise ToolError("pandoc is required for HTML-to-markdown conversion") from exc
    except subprocess.CalledProcessError as exc:
        raise ToolError(f"pandoc failed: {exc.stderr.strip()}") from exc
    return _clean_markdown_projection(proc.stdout)


def render_page_markdown(page: dict[str, Any], session: Session) -> str:
    page_id = str(page.get("id") or "")
    title = str(page.get("title") or "(untitled)")
    page_url = ""
    if session.base_url and page_id:
        page_url = f"{session.base_url.rstrip('/')}/wiki/pages/viewpage.action?pageId={page_id}"
    created = str(page.get("createdAt") or "")
    version = page.get("version")
    if not isinstance(version, dict):
        version = {}
    updated = str(version.get("createdAt") or "")
    body = render_storage_to_markdown(storage_value(page), session=session)
    lines = [f"# {title}", ""]
    if page_url:
        lines.append(f"- URL: {page_url}")
    if page_id:
        lines.append(f"- Page ID: {page_id}")
    if page.get("spaceId"):
        lines.append(f"- Space ID: {page.get('spaceId')}")
    if created:
        lines.append(f"- Created: {created}")
    if updated:
        lines.append(f"- Updated: {updated}")
    if version.get("number") is not None:
        lines.append(f"- Version: {version.get('number')}")
    if _projection_is_lossy(body):
        lines.append(
            f"- Projection: approximate markdown; use `gotta confluence get {page_id} --output body` "
            "for canonical Confluence storage HTML when page layout, embedded diagrams, tables, "
            "or macros matter"
        )
    lines.extend(["", "---", "", body.rstrip(), ""])
    return "\n".join(lines)


def render_comment_markdown(comment: dict[str, Any], session: Session) -> str:
    comment_id = str(comment.get("id") or "")
    page_id = str(comment.get("pageId") or "")
    title = str(comment.get("title") or "Confluence Comment")
    comment_url = ""
    if session.base_url and page_id and comment_id:
        comment_url = (
            f"{session.base_url.rstrip('/')}/wiki/pages/viewpage.action?"
            f"pageId={page_id}&focusedCommentId={comment_id}"
        )
    created = str(comment.get("createdAt") or "")
    version = comment.get("version")
    if not isinstance(version, dict):
        version = {}
    updated = str(version.get("createdAt") or "")
    body = render_storage_to_markdown(comment_storage_value(comment), session=session)
    lines = [f"# {title}", ""]
    if comment_url:
        lines.append(f"- URL: {comment_url}")
    if comment_id:
        lines.append(f"- Comment ID: {comment_id}")
    if page_id:
        lines.append(f"- Page ID: {page_id}")
    if created:
        lines.append(f"- Created: {created}")
    if updated:
        lines.append(f"- Updated: {updated}")
    if version.get("number") is not None:
        lines.append(f"- Version: {version.get('number')}")
    if _projection_is_lossy(body):
        lines.append(
            f"- Projection: approximate markdown; use `gotta confluence get {comment_id or page_id} --output body` "
            "for canonical Confluence storage HTML when page layout, embedded diagrams, tables, "
            "or macros matter"
        )
    lines.extend(["", "---", "", body.rstrip(), ""])
    return "\n".join(lines)


def _content_capture_meta(
    session: Session,
    kind: str,
    content: dict[str, Any],
) -> dict[str, object]:
    version = content.get("version")
    if not isinstance(version, dict):
        version = {}
    content_id = str(content.get("id") or "")
    page_id = str(content.get("pageId") or "")
    if kind == "comment":
        url = (
            f"{session.base_url.rstrip('/')}/wiki/pages/viewpage.action?"
            f"pageId={page_id}&focusedCommentId={content_id}"
            if session.base_url and page_id and content_id
            else ""
        )
    else:
        url = (
            f"{session.base_url.rstrip('/')}/wiki/pages/viewpage.action?pageId={content_id}"
            if session.base_url and content_id
            else ""
        )
    return {
        "projector": "confluence",
        "content_kind": kind,
        "content_id": content_id,
        "page_id": page_id,
        "source_title": str(content.get("title") or ""),
        "source_url": url,
        "source_base_url": session.base_url,
        "source_space_id": str(content.get("spaceId") or ""),
        "source_created_at": str(content.get("createdAt") or ""),
        "source_updated_at": str(version.get("createdAt") or ""),
        "source_version": str(version.get("number") or ""),
    }


def _content_capture_name(kind: str, content: dict[str, Any], fallback: str) -> str:
    content_id = str(content.get("id") or "").strip()
    if content_id:
        return f"{content_id}.html"
    if fallback:
        return f"{fallback}.html"
    return f"{kind}.html"


def _markdown_from_capture(capture: Capture) -> bytes:
    kind = str(capture.meta.get("content_kind") or "page")
    session: Session | None = None
    base_url = str(capture.meta.get("source_base_url") or "").strip()
    if base_url:
        try:
            session = load_session(PageRef(base_url=base_url))
        except ToolError:
            session = None
    body = render_storage_to_markdown(
        capture.data.decode("utf-8", errors="replace"),
        session=session,
    )
    title = str(
        capture.meta.get("source_title")
        or ("Confluence Comment" if kind == "comment" else "(untitled)")
    )
    lines = [f"# {title}", ""]
    url = str(capture.meta.get("source_url") or "")
    content_id = str(capture.meta.get("content_id") or "")
    page_id = str(capture.meta.get("page_id") or "")
    if url:
        lines.append(f"- URL: {url}")
    if kind == "comment":
        if content_id:
            lines.append(f"- Comment ID: {content_id}")
        if page_id:
            lines.append(f"- Page ID: {page_id}")
    else:
        if content_id:
            lines.append(f"- Page ID: {content_id}")
        if capture.meta.get("source_space_id"):
            lines.append(f"- Space ID: {capture.meta.get('source_space_id')}")
    if capture.meta.get("source_created_at"):
        lines.append(f"- Created: {capture.meta.get('source_created_at')}")
    if capture.meta.get("source_updated_at"):
        lines.append(f"- Updated: {capture.meta.get('source_updated_at')}")
    if capture.meta.get("source_version"):
        lines.append(f"- Version: {capture.meta.get('source_version')}")
    if _projection_is_lossy(body):
        lines.append(
            f"- Projection: approximate markdown; use `gotta confluence get {content_id or page_id} --output body` "
            "for canonical Confluence storage HTML when page layout, embedded diagrams, tables, "
            "or macros matter"
        )
    lines.extend(["", "---", "", body.rstrip(), ""])
    return "\n".join(lines).encode("utf-8")


def capture(argv: list[str], _options: object) -> Capture:
    args = _parse_cli(argv)
    if args.command != "get":
        if args.command in {"search", "cql"}:
            payload = capture_json_command(
                args,
                cmd_search if args.command == "search" else cmd_cql,
                detail=f"confluence {args.command} capture failed",
            )
            return Capture(
                data=payload,
                name=preferred_name(argv, object()),
                type="application/json",
                meta={
                    "projector": "confluence",
                    "confluence_kind": args.command,
                },
            )
        raise NotImplementedError("confluence capture does not support this command")
    session, content_kind, content = fetch_read_target(parse_content_ref(args.page))
    body = (
        comment_storage_value(content)
        if content_kind == "comment"
        else storage_value(content)
    )
    fallback = _slug(
        Path(urllib.parse.urlparse(args.page).path.rstrip("/")).name or args.page,
        fallback="confluence",
    )
    return Capture(
        data=body.encode("utf-8"),
        name=_content_capture_name(content_kind, content, fallback),
        type="text/html",
        meta=_content_capture_meta(session, content_kind, content),
        view={"content": content, "content_kind": content_kind},
    )


def project(argv: list[str], capture: Capture) -> bytes:
    kind = str(capture.meta.get("confluence_kind") or "get").strip()
    if kind in {"search", "cql"}:
        payload = json.loads(capture.data.decode("utf-8"))
        if not argv:
            return render_search_markdown(payload).encode("utf-8")
        args = _parse_cli(argv)
        if args.command != kind:
            return capture.data
        if args.output == "json":
            return pretty_json(capture.data)
        return render_search_markdown(payload).encode("utf-8")
    if not argv:
        return _markdown_from_capture(capture)
    args = _parse_cli(argv)
    if args.command != "get":
        return capture.data
    if args.output == "body":
        return capture.data
    if args.output == "markdown":
        return _markdown_from_capture(capture)
    content = capture.view.get("content")
    if args.output == "meta":
        if isinstance(content, dict):
            kind = str(capture.meta.get("content_kind") or "page")
            if kind == "comment":
                return json_bytes(
                    {
                        "id": content.get("id"),
                        "type": "comment",
                        "pageId": content.get("pageId"),
                        "version": content.get("version"),
                        "status": content.get("status"),
                    }
                )
            return json_bytes(
                {
                    "id": content.get("id"),
                    "type": kind,
                    "title": content.get("title"),
                    "version": content.get("version"),
                    "status": content.get("status"),
                    "spaceId": content.get("spaceId"),
                }
            )
        return json_bytes(capture.meta)
    if isinstance(content, dict):
        return json_bytes(content)
    return json_bytes(
        {
            "id": capture.meta.get("content_id") or capture.meta.get("page_id") or "",
            "type": capture.meta.get("content_kind") or "page",
            "title": capture.meta.get("source_title") or "",
            "body": {
                "storage": {"value": capture.data.decode("utf-8", errors="replace")}
            },
        }
    )


def format_resolve_page_payload(
    *,
    session: Session,
    parent_page: dict[str, Any],
    title: str,
    matches: list[dict[str, Any]],
) -> dict[str, Any]:
    parent_id = str(parent_page.get("id") or "")
    payload = {
        "title": title,
        "parent": normalize_page_summary(parent_page, session),
        "matchCount": len(matches),
        "found": len(matches) == 1,
        "matches": [normalize_page_summary(match, session) for match in matches],
    }
    payload["target"] = {
        "parentId": parent_id,
        "spaceId": str(parent_page.get("spaceId") or ""),
        "title": title,
    }
    return payload


def render_resolve_page_summary(payload: dict[str, Any]) -> str:
    parent = payload.get("parent")
    if not isinstance(parent, dict):
        parent = {}
    title = str(payload.get("title") or "")
    lines = [
        f"title: {title}",
        f"parent: {parent.get('title') or '(untitled)'} ({parent.get('id') or 'unknown'})",
        f"space_id: {payload.get('target', {}).get('spaceId') if isinstance(payload.get('target'), dict) else ''}",
        f"match_count: {payload.get('matchCount') or 0}",
    ]
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        lines.append("matches: none")
        return "\n".join(lines) + "\n"
    lines.append("matches:")
    for page in matches:
        if not isinstance(page, dict):
            continue
        page_id = str(page.get("id") or "")
        title = str(page.get("title") or "(untitled)")
        url = str(page.get("url") or "")
        line = f"- {title}"
        details = [f"locator `confluence:{page_id}`"] if page_id else []
        if url:
            details.append(url)
        if details:
            line += " - " + ", ".join(details)
        lines.append(line)
    return "\n".join(lines) + "\n"


def format_create_page_preview(
    *,
    session: Session,
    parent_page: dict[str, Any],
    title: str,
    body_html: str,
    body_source: str,
    sibling_matches: list[dict[str, Any]],
) -> dict[str, Any]:
    parent_id = str(parent_page.get("id") or "")
    space_id = str(parent_page.get("spaceId") or "")
    return {
        "mode": "dry-run",
        "bodySource": body_source,
        "title": title,
        "parent": normalize_page_summary(parent_page, session),
        "target": {
            "apiUrl": pages_collection_url(session),
            "parentId": parent_id,
            "spaceId": space_id,
            "title": title,
        },
        "siblingExists": bool(sibling_matches),
        "siblingCount": len(sibling_matches),
        "siblings": [
            normalize_page_summary(match, session) for match in sibling_matches
        ],
        "bodyPreview": summarize_storage_html(body_html),
    }


def render_create_page_preview(payload: dict[str, Any]) -> str:
    parent = payload.get("parent")
    if not isinstance(parent, dict):
        parent = {}
    body_preview = payload.get("bodyPreview")
    if not isinstance(body_preview, dict):
        body_preview = {}
    target = payload.get("target")
    if not isinstance(target, dict):
        target = {}
    lines = [
        f"mode: {payload.get('mode') or 'dry-run'}",
        f"title: {payload.get('title') or ''}",
        f"parent: {parent.get('title') or '(untitled)'} ({parent.get('id') or 'unknown'})",
        f"space_id: {target.get('spaceId') or ''}",
        f"body_source: {payload.get('bodySource') or ''}",
        f"sibling_exists: {'yes' if payload.get('siblingExists') else 'no'}",
        f"sibling_count: {payload.get('siblingCount') or 0}",
        f"create_target: {target.get('apiUrl') or ''}",
        f"body_chars: {body_preview.get('chars') or 0}",
    ]
    preview = str(body_preview.get("preview") or "").strip()
    if preview:
        lines.append(f"body_preview: {preview}")
    siblings = payload.get("siblings")
    if isinstance(siblings, list) and siblings:
        lines.append("siblings:")
        for page in siblings:
            if not isinstance(page, dict):
                continue
            page_id = str(page.get("id") or "")
            url = str(page.get("url") or "")
            line = f"- {page.get('title') or '(untitled)'}"
            details = [f"locator `confluence:{page_id}`"] if page_id else []
            if url:
                details.append(url)
            if details:
                line += " - " + ", ".join(details)
            lines.append(line)
    return "\n".join(lines) + "\n"


def format_created_page_result(
    page: dict[str, Any], session: Session
) -> dict[str, Any]:
    result = normalize_page_summary(page, session)
    result["mode"] = "created"
    return result


def load_create_page_body(args: argparse.Namespace) -> tuple[str, str]:
    if args.from_markdown:
        markdown = read_text(Path(args.from_markdown))
        markdown = strip_matching_leading_h1(markdown, title=args.title)
        return render_markdown_to_storage(markdown), "markdown"
    if args.from_html:
        return read_text(Path(args.from_html)), "html"
    raise ToolError("missing create-page body input")


def cmd_resolve_page(args: argparse.Namespace) -> int:
    session, parent_page = fetch_page(parse_page_ref(args.parent))
    matches = find_child_pages_by_title(
        session,
        parent_page=parent_page,
        title=args.title,
    )
    payload = format_resolve_page_payload(
        session=session,
        parent_page=parent_page,
        title=args.title,
        matches=matches,
    )
    if args.output == "json":
        print_json(payload)
        return 0
    sys.stdout.write(render_resolve_page_summary(payload))
    return 0


def cmd_create_page(args: argparse.Namespace) -> int:
    body_html, body_source = load_create_page_body(args)
    session, parent_page = fetch_page(parse_page_ref(args.parent))
    siblings = find_child_pages_by_title(
        session,
        parent_page=parent_page,
        title=args.title,
    )
    if not args.apply:
        payload = format_create_page_preview(
            session=session,
            parent_page=parent_page,
            title=args.title,
            body_html=body_html,
            body_source=body_source,
            sibling_matches=siblings,
        )
        if args.output == "json":
            print_json(payload)
            return 0
        sys.stdout.write(render_create_page_preview(payload))
        return 0
    if siblings:
        sibling = normalize_page_summary(siblings[0], session)
        raise ToolError(
            "a sibling page with the same title already exists under the requested parent: "
            f"confluence:{sibling['id']} {sibling.get('url') or ''}".strip()
        )
    created = create_page(
        session,
        parent_page=parent_page,
        title=args.title,
        body_html=body_html,
    )
    payload = format_created_page_result(created, session)
    if args.output == "json":
        print_json(payload)
        return 0
    lines = [
        "mode: created",
        f"title: {payload.get('title') or ''}",
        f"page: {payload.get('id') or ''}",
    ]
    if payload.get("spaceId"):
        lines.append(f"space_id: {payload['spaceId']}")
    if payload.get("parentId"):
        lines.append(f"parent_id: {payload['parentId']}")
    if payload.get("url"):
        lines.append(f"url: {payload['url']}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    session, content_kind, content = fetch_read_target(parse_content_ref(args.page))
    if args.output == "body":
        if content_kind == "comment":
            sys.stdout.write(comment_storage_value(content))
        else:
            sys.stdout.write(storage_value(content))
        return 0
    if args.output == "markdown":
        if content_kind == "comment":
            sys.stdout.write(render_comment_markdown(content, session))
        else:
            sys.stdout.write(render_page_markdown(content, session))
        return 0
    if args.output == "meta":
        if content_kind == "comment":
            content = {
                "id": content["id"],
                "type": "comment",
                "pageId": content.get("pageId"),
                "version": content.get("version"),
                "status": content.get("status"),
            }
        else:
            content = {
                "id": content["id"],
                "type": content_kind,
                "title": content["title"],
                "version": content["version"],
                "status": content.get("status"),
                "spaceId": content.get("spaceId"),
            }
    print_json(content)
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    needle = read_single_input(args.needle, args.needle_file, input_name="needle input")
    _, page = fetch_page(parse_page_ref(args.page))
    print_json(
        count_with_context(
            storage_value(page),
            needle,
            context_chars=DEFAULT_FIND_CONTEXT_CHARS,
        )
    )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    base_url = args.base_url.strip()
    session = load_session(PageRef(base_url=base_url))
    result = search_confluence(
        session,
        cql=build_text_search_cql(args),
        limit=args.limit,
        cursor=args.next,
    )
    if args.output == "json":
        print_json(result)
        return 0
    sys.stdout.write(render_search_markdown(result))
    return 0


def cmd_cql(args: argparse.Namespace) -> int:
    base_url = args.base_url.strip()
    session = load_session(PageRef(base_url=base_url))
    result = search_confluence(
        session,
        cql=args.query,
        limit=args.limit,
        cursor=args.next,
    )
    if args.output == "json":
        print_json(result)
        return 0
    sys.stdout.write(render_search_markdown(result))
    return 0


def cmd_replace(args: argparse.Namespace) -> int:
    old = read_explicit_input(
        args.old_inline, args.from_file, input_name="--from/--from-file"
    )
    new = read_explicit_input(
        args.new_inline, args.to_file, input_name="--to/--to-file"
    )
    session, page = fetch_page(parse_page_ref(args.page))
    ensure_base_version(page, args.base_version)
    before = storage_value(page)
    after = replace_exact(before, old, new, expected_count=args.expected_count)
    if not args.apply:
        diff = unified_diff(before, after)
        sys.stdout.write(diff + ("\n" if diff else ""))
        return 0
    if after == before:
        print_json(format_noop_result(page))
        return 0
    updated = update_page(session, page, after, message=args.message)
    print_json(format_update_result(updated))
    return 0


def cmd_replace_section(args: argparse.Namespace) -> int:
    replacement = read_explicit_input(
        args.replacement,
        args.replacement_file,
        input_name="--replacement/--replacement-file",
    )
    session, page = fetch_page(parse_page_ref(args.page))
    ensure_base_version(page, args.base_version)
    before = storage_value(page)
    after = replace_between(
        before,
        args.start,
        args.end,
        replacement,
        include_markers=args.include_markers,
    )
    if not args.apply:
        diff = unified_diff(before, after)
        sys.stdout.write(diff + ("\n" if diff else ""))
        return 0
    if after == before:
        print_json(format_noop_result(page))
        return 0
    updated = update_page(session, page, after, message=args.message)
    print_json(format_update_result(updated))
    return 0


def cmd_render_markdown(args: argparse.Namespace) -> int:
    markdown = read_single_input(args.markdown, args.file, input_name="markdown input")
    sys.stdout.write(render_markdown_to_storage(markdown))
    return 0


def cmd_update_body(args: argparse.Namespace) -> int:
    new_body = read_single_input(
        args.body_inline,
        args.from_file,
        input_name="--from-file, inline body, or stdin",
    )
    session, page = fetch_page(parse_page_ref(args.page))
    ensure_base_version(page, args.base_version)
    before = storage_value(page)
    after = new_body
    if not args.apply:
        diff = unified_diff(before, after)
        sys.stdout.write(diff + ("\n" if diff else ""))
        return 0
    if after == before:
        print_json(format_noop_result(page))
        return 0
    updated = update_page(
        session,
        page,
        after,
        message=args.message,
    )
    print_json(format_update_result(updated))
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    operations = load_operations(args.operations_file)
    session, page = fetch_page(parse_page_ref(args.page))
    ensure_base_version(page, args.base_version)
    before = storage_value(page)
    after = before
    for index, operation in enumerate(operations, start=1):
        after = apply_operation(after, operation, index=index)
    if not args.apply:
        diff = unified_diff(before, after)
        sys.stdout.write(diff + ("\n" if diff else ""))
        return 0
    if after == before:
        result = format_noop_result(page)
        result["operations"] = len(operations)
        print_json(result)
        return 0
    updated = update_page(session, page, after, message=args.message)
    result = format_update_result(updated)
    result["operations"] = len(operations)
    print_json(result)
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    base_url = args.base_url.strip()
    if args.full:
        oauth_state = run_oauth_bootstrap(base_url=base_url)
        cloud_id = oauth_state.get("cloud_id")
        expires_at = oauth_state.get("expires_at")
        selected_site_url = str(
            oauth_state.get("base_url") or site_root(base_url)
        ).strip()
    else:
        session = load_session(PageRef(base_url=site_root(base_url)))
        status = atlassian_status_payload(
            base_url=base_url,
            auth_command="confluence",
        )
        cloud_id = session.cloud_id
        expires_at = status.get("expiresAt")
        selected_site_url = str(
            session.base_url or status.get("baseUrl") or site_root(base_url)
        ).strip()
    selected_confluence_url = f"{selected_site_url}/wiki" if selected_site_url else ""
    if selected_confluence_url:
        persist_selected_base_urls(selected_confluence_url)
    print_json(
        {
            "authenticated": True,
            "cloud_id": cloud_id,
            "expires_at": expires_at,
            "base_url": selected_confluence_url,
            "token_file": str(atl.TOKEN_FILE),
            "cloud_id_file": str(atl.CLOUD_ID_FILE),
            "oauth_dir": str(atl.OAUTH_DIR),
        }
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    payload = atlassian_status_payload(
        base_url=args.base_url,
        check_token=args.check,
        auth_command="confluence",
    )
    payload["surface"] = "confluence"
    if payload.get("baseUrl"):
        payload["confluenceBaseUrl"] = str(payload["baseUrl"]).rstrip("/") + "/wiki"
    if args.output == "json":
        print_json(payload)
        return 0
    lines = [
        "surface\tconfluence",
        f"base_url\t{payload.get('confluenceBaseUrl') or ''}",
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


def resolve_mcp_runtime(
    base_url: str,
) -> tuple[str, str, str, str, str, str, str, str]:
    config_env = load_atlassian_config_env()
    client_id, client_secret, redirect_uri, scope = load_oauth_runtime_config()
    confluence_url = base_url.strip().rstrip("/")
    if not confluence_url:
        raise ToolError("missing Confluence base URL")
    site_url = site_root(confluence_url)
    toolsets = (
        os.environ.get("GOTTA_ATLASSIAN_TOOLSETS", "").strip()
        or config_env.get("GOTTA_ATLASSIAN_TOOLSETS", "").strip()
        or "all"
    )
    session = load_session(PageRef(base_url=site_url))
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
                f"{flag} is managed by gotta confluence mcp; configure durable OAuth app "
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
    # Force mcp-atlassian to use the file-backed gotta auth cache.
    # Its keyring path can drift stale independently and then break token refresh.
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
        raise ToolError(
            f"{exc}. Likely next step: run 'gotta confluence auth'"
        ) from exc

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
        prog="gotta confluence",
        description=(
            "Standalone Confluence page-body fetch/find/edit toolbelt. "
            "Interactive commands may open browser reauthorization if the "
            "cached Atlassian session is invalid."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "status", help="inspect local Confluence/Atlassian auth readiness"
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Confluence base URL override (default: {CONFLUENCE_BASE_URL_ENV})",
    )
    p.add_argument(
        "--check", action="store_true", help="run a token preflight against Atlassian"
    )
    p.add_argument("--output", choices=["json", "summary"], default="summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "get", help="fetch Confluence page, blog post, or comment content"
    )
    p.add_argument("page", help=CONTENT_ARG_HELP)
    p.add_argument(
        "--output",
        choices=["json", "body", "meta", "markdown"],
        default="markdown",
        help="output mode; defaults to markdown for first-pass reading. use body for canonical "
        "Confluence storage HTML when page layout, tasks, or diagrams need highest fidelity",
    )
    p.set_defaults(func=cmd_get)

    p = sub.add_parser(
        "find",
        help="count exact fragments in page body HTML and return surrounding context",
    )
    p.add_argument("page", help=PAGE_ARG_HELP)
    p.add_argument("needle", nargs="?", help="exact fragment; reads stdin if omitted")
    p.add_argument("--needle-file", help="file containing exact fragment")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser(
        "search",
        help="search Confluence content with plain text",
    )
    p.add_argument("query", help="plain-text search query")
    p.add_argument(
        "--title-only",
        action="store_true",
        help="search only page titles instead of full text",
    )
    p.add_argument("--space", help="restrict results to a Confluence space key")
    p.add_argument(
        "--type",
        choices=["page", "blogpost", "attachment", "comment", "all"],
        default="page",
        help="restrict results to one Confluence content type; defaults to page-first search",
    )
    p.add_argument("--limit", type=int, default=10, help="maximum results to request")
    p.add_argument("--next", help="continuation token from a previous search response")
    p.add_argument("--output", choices=["markdown", "json"], default="markdown")
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Confluence base URL override (default: {CONFLUENCE_BASE_URL_ENV})",
    )
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("cql", help="search Confluence content with raw CQL")
    p.add_argument("query", help="raw CQL search expression")
    p.add_argument("--limit", type=int, default=10, help="maximum results to request")
    p.add_argument("--next", help="continuation token from a previous search response")
    p.add_argument("--output", choices=["markdown", "json"], default="markdown")
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Confluence base URL override (default: {CONFLUENCE_BASE_URL_ENV})",
    )
    p.set_defaults(func=cmd_cql)

    p = sub.add_parser(
        "resolve-page",
        help="resolve an exact child page title under a specific parent page",
    )
    p.add_argument("--parent", required=True, help=PAGE_ARG_HELP)
    p.add_argument("--title", required=True, help="exact child page title to resolve")
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_resolve_page)

    p = sub.add_parser(
        "create-page",
        help="preview or create a child page under a specific parent page",
    )
    p.add_argument("--parent", required=True, help=PAGE_ARG_HELP)
    p.add_argument("--title", required=True, help="final child page title")
    body_group = p.add_mutually_exclusive_group(required=True)
    body_group.add_argument(
        "--from-markdown",
        help=(
            "markdown file to render to Confluence storage HTML before create; "
            "a leading H1 that matches --title is stripped before render"
        ),
    )
    body_group.add_argument(
        "--from-html",
        help="file containing Confluence storage HTML to create verbatim",
    )
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="create the page; default is dry-run preview",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="preview the create target explicitly; also the default when --apply is omitted",
    )
    p.add_argument("--output", choices=["summary", "json"], default="summary")
    p.set_defaults(func=cmd_create_page)

    p = sub.add_parser("replace", help="replace an exact fragment in page body HTML")
    p.add_argument("page", help=PAGE_ARG_HELP)
    p.add_argument("--from", dest="old_inline", help="old exact fragment")
    p.add_argument("--from-file", help="file containing old exact fragment")
    p.add_argument("--to", dest="new_inline", help="new replacement fragment")
    p.add_argument("--to-file", help="file containing new replacement fragment")
    p.add_argument(
        "--expected-count",
        type=int,
        default=1,
        help="require this exact number of matches before replacing",
    )
    p.add_argument(
        "--base-version",
        type=int,
        help="require the current page version to match before diffing or writing",
    )
    p.add_argument("--message", default="Replace Confluence body fragment")
    p.add_argument(
        "--apply",
        action="store_true",
        help="write one full-body update as a requested minor edit; default is dry-run diff",
    )
    p.set_defaults(func=cmd_replace)

    p = sub.add_parser(
        "replace-section", help="replace content between exact start/end markers"
    )
    p.add_argument("page", help=PAGE_ARG_HELP)
    p.add_argument("--start", required=True, help="exact start marker")
    p.add_argument("--end", required=True, help="exact end marker")
    p.add_argument("--replacement", help="replacement content")
    p.add_argument("--replacement-file", help="file containing replacement content")
    p.add_argument(
        "--include-markers",
        action="store_true",
        help="replace the markers themselves instead of only the content between them",
    )
    p.add_argument(
        "--base-version",
        type=int,
        help="require the current page version to match before diffing or writing",
    )
    p.add_argument("--message", default="Replace Confluence body section")
    p.add_argument(
        "--apply",
        action="store_true",
        help="write one full-body update as a requested minor edit; default is dry-run diff",
    )
    p.set_defaults(func=cmd_replace_section)

    p = sub.add_parser(
        "render-markdown",
        help="convert markdown to Confluence storage HTML for safe insertion",
    )
    p.add_argument(
        "markdown", nargs="?", help="inline markdown; reads stdin if omitted"
    )
    p.add_argument("--file", help="file containing markdown")
    p.set_defaults(func=cmd_render_markdown)

    p = sub.add_parser(
        "update-body",
        help="diff or write a full replacement page body from file, inline input, or stdin",
    )
    p.add_argument("page", help=PAGE_ARG_HELP)
    p.add_argument("body_inline", nargs="?", help="inline replacement body HTML")
    p.add_argument("--from-file", help="file containing replacement body HTML")
    p.add_argument(
        "--base-version",
        type=int,
        help="require the current page version to match before diffing or writing",
    )
    p.add_argument("--message", default="Update Confluence page body")
    p.add_argument(
        "--apply",
        action="store_true",
        help="write one full-body update as a requested minor edit; default is dry-run diff",
    )
    p.set_defaults(func=cmd_update_body)

    p = sub.add_parser(
        "batch",
        help="apply multiple exact/section replacements in memory, then write one full-body update",
    )
    p.add_argument("page", help=PAGE_ARG_HELP)
    p.add_argument(
        "--operations-file",
        required=True,
        help="JSON array describing replace/replace-section operations",
    )
    p.add_argument(
        "--base-version",
        type=int,
        help="require the current page version to match before diffing or writing",
    )
    p.add_argument("--message", default="Apply Confluence body batch")
    p.add_argument(
        "--apply",
        action="store_true",
        help="write one full-body update as a requested minor edit; default is dry-run diff",
    )
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser(
        "auth",
        help="ensure local Atlassian OAuth state is usable; refresh silently when possible",
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Confluence base URL override (default: {CONFLUENCE_BASE_URL_ENV})",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="force a full browser OAuth bootstrap instead of reusing or refreshing cached state",
    )
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser(
        "mcp",
        help="start mcp-atlassian with the same managed local OAuth/session contract",
    )
    p.add_argument(
        "--base-url",
        default=default_base_url(),
        help=f"Confluence base URL override for MCP startup (default: {CONFLUENCE_BASE_URL_ENV})",
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
