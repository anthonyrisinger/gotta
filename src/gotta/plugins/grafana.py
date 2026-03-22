#!/usr/bin/env python3
"""Read-only Grafana dashboard discovery through the HTTP API."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from gotta.config import (
    extract_provider_env,
    load_config,
    provider_env_reference,
    primary_config_file,
    write_config,
)
from gotta.helptext import is_long_help_request, print_long_help
from gotta.routing import query_route, split_locator_tail, strip_http_url_fragment
from gotta.source import derive_source_metadata_from_payload, render_source_metadata_lines


GRAFANA_BASE_URL_ENV = "GOTTA_GRAFANA_BASE_URL"
GRAFANA_TOKEN_ENV = "GOTTA_GRAFANA_SERVICE_ACCOUNT_TOKEN"
GRAFANA_ORG_ID_ENV = "GOTTA_GRAFANA_ORG_ID"

DEFAULT_LIMIT = 50
MAX_LIMIT = 5000
DEFAULT_DATASOURCE_LIST_LIMIT = 100
DASHBOARD_URL_RE = re.compile(r"^https?://[^/]+(?:/[^?#]*)?/d(?:-solo)?/(?P<uid>[^/?#]+)")
DEFAULT_USER_AGENT = "gotta/grafana-plugin"


class ToolError(RuntimeError):
    """Raised when the Grafana API contract cannot be satisfied."""


@dataclass(frozen=True)
class Session:
    base_url: str
    token: str
    org_id: str


@dataclass(frozen=True)
class DashboardRefContext:
    uid: str
    org_id: str
    from_time: str
    to_time: str


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


def nonnegative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer, got: {raw}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("expected a non-negative integer")
    return value


def _list_window_suffix(*, limit: int, offset: int, include_all: bool, default_limit: int) -> str:
    parts: list[str] = []
    if include_all:
        parts.append("all")
    elif limit != default_limit:
        parts.append(f"limit-{limit}")
    if offset:
        parts.append(f"offset-{offset}")
    return ("-" + "-".join(parts)) if parts else ""


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


def _load_grafana_config_env() -> dict[str, str]:
    return extract_provider_env(load_config(), "grafana")


def _env_or_config(config_env: dict[str, str], name: str) -> str:
    return os.environ.get(name, "").strip() or str(config_env.get(name) or "").strip()


def _normalize_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolError(
            f"invalid Grafana base URL: {raw}. Expected https://grafana.example.com"
        )
    return urllib.parse.urlunparse(parsed._replace(path=parsed.path.rstrip("/"), query="", fragment=""))


def _load_session(
    *,
    base_url: str | None = "",
    token: str | None = "",
    org_id: str | None = "",
) -> Session:
    config_env = _load_grafana_config_env()
    resolved_base_url = _normalize_base_url(
        str(base_url or "").strip() or _env_or_config(config_env, GRAFANA_BASE_URL_ENV)
    )
    resolved_token = str(token or "").strip() or _env_or_config(config_env, GRAFANA_TOKEN_ENV)
    resolved_org_id = str(org_id or "").strip() or _env_or_config(config_env, GRAFANA_ORG_ID_ENV)
    return Session(
        base_url=resolved_base_url,
        token=resolved_token,
        org_id=resolved_org_id,
    )


def _headers(session: Session) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {session.token}",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    if session.org_id:
        headers["X-Grafana-Org-Id"] = session.org_id
    return headers


def _grafana_json(
    session: Session,
    path: str,
    *,
    method: str = "GET",
    params: list[tuple[str, str]] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    query = urllib.parse.urlencode(params or [], doseq=True)
    url = f"{session.base_url}{path}"
    if query:
        url = f"{url}?{query}"
    headers = _headers(session)
    data: bytes | None = None
    if payload is not None:
        headers = {**headers, "Content-Type": "application/json"}
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read().decode(charset)
    except urllib.error.HTTPError as exc:
        charset = exc.headers.get_content_charset() or "utf-8"
        body = exc.read().decode(charset, errors="replace")
        message = body.strip() or exc.reason or f"HTTP {exc.code}"
        raise ToolError(f"Grafana API request failed ({exc.code}) for {path}: {message}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"Grafana API request failed for {path}: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Grafana API returned invalid JSON for {path}: {exc}") from exc


def _dashboard_uid_from_ref(raw: str) -> str:
    return _dashboard_ref_context(raw).uid


def _dashboard_ref_context(raw: str) -> DashboardRefContext:
    candidate = strip_http_url_fragment(raw.strip())
    if not candidate:
        return DashboardRefContext(uid="", org_id="", from_time="", to_time="")
    match = DASHBOARD_URL_RE.match(candidate)
    org_id = ""
    from_time = ""
    to_time = ""
    if match:
        parsed = urllib.parse.urlparse(candidate)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
        org_id = str((query.get("orgId") or [""])[0] or "").strip()
        from_time = str((query.get("from") or [""])[0] or "").strip()
        to_time = str((query.get("to") or [""])[0] or "").strip()
        return DashboardRefContext(
            uid=match.group("uid"),
            org_id=org_id,
            from_time=from_time,
            to_time=to_time,
        )
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme:
        return DashboardRefContext(uid="", org_id="", from_time="", to_time="")
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "", candidate)
    return DashboardRefContext(uid=normalized, org_id="", from_time="", to_time="")


def _require_configured_session(session: Session) -> None:
    if not session.base_url:
        raise ToolError(f"missing Grafana base URL; set {GRAFANA_BASE_URL_ENV}")
    if not session.token:
        raise ToolError(f"missing Grafana service account token; set {GRAFANA_TOKEN_ENV}")


def _relative_or_full_url(base_url: str, raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return urllib.parse.urljoin(base_url + "/", value.lstrip("/"))


def _status_payload(
    *,
    base_url: str = "",
    token: str = "",
    org_id: str = "",
) -> dict[str, Any]:
    session = _load_session(base_url=base_url, token=token, org_id=org_id)
    payload: dict[str, Any] = {
        "provider": "grafana",
        "baseUrl": session.base_url,
        "orgId": session.org_id,
        "tokenConfigured": bool(session.token),
        "configFile": str(primary_config_file()),
        "authStatus": "missing",
        "searchStatus": "unknown",
        "permissionsStatus": "unknown",
        "dashboardRead": "unknown",
        "nextStep": "",
    }
    if not session.base_url:
        payload["nextStep"] = (
            f"set {GRAFANA_BASE_URL_ENV} in {provider_env_reference('grafana')}"
        )
        return payload
    if not session.token:
        payload["nextStep"] = (
            f"set {GRAFANA_TOKEN_ENV} in {provider_env_reference('grafana')}"
        )
        return payload

    permissions_error = ""
    try:
        permissions = _grafana_json(session, "/api/access-control/user/permissions")
        payload["permissionsStatus"] = "usable"
        if isinstance(permissions, dict):
            payload["dashboardRead"] = "yes" if "dashboards:read" in permissions else "unknown"
            payload["permissionKeys"] = sorted(str(key) for key in permissions)[:50]
    except ToolError as exc:
        permissions_error = str(exc)
        payload["permissionsStatus"] = "error"
        payload["permissionsError"] = permissions_error

    try:
        results = _grafana_json(
            session,
            "/api/search",
            params=[("limit", "1"), ("page", "1"), ("query", "")],
        )
        payload["authStatus"] = "usable"
        payload["searchStatus"] = "usable"
        payload["searchResultCount"] = len(results) if isinstance(results, list) else 0
        payload["nextStep"] = "run `gotta grafana search --type dash-db`"
        return payload
    except ToolError as exc:
        payload["authStatus"] = "invalid"
        payload["searchStatus"] = "error"
        payload["searchError"] = str(exc)
        if permissions_error:
            payload["nextStep"] = (
                f"verify {GRAFANA_BASE_URL_ENV} and {GRAFANA_TOKEN_ENV}; "
                "the current service account token did not authenticate cleanly"
            )
        else:
            payload["nextStep"] = (
                f"verify {GRAFANA_BASE_URL_ENV} and {GRAFANA_TOKEN_ENV}; "
                "the current service account token did not authenticate cleanly"
            )
        return payload


def _render_status_summary(payload: dict[str, Any]) -> str:
    lines = [
        f"provider\t{payload.get('provider') or 'grafana'}",
        f"base_url\t{payload.get('baseUrl') or ''}",
        f"org_id\t{payload.get('orgId') or ''}",
        f"token_configured\t{str(bool(payload.get('tokenConfigured'))).lower()}",
        f"auth_status\t{payload.get('authStatus') or 'unknown'}",
        f"search_status\t{payload.get('searchStatus') or 'unknown'}",
        f"permissions_status\t{payload.get('permissionsStatus') or 'unknown'}",
        f"dashboard_read\t{payload.get('dashboardRead') or 'unknown'}",
        f"config_file\t{payload.get('configFile') or ''}",
    ]
    next_step = str(payload.get("nextStep") or "").strip()
    if next_step:
        lines.append(f"next_step\t{next_step}")
    return "\n".join(lines)


def _persist_auth_updates(updates: dict[str, str]) -> Path:
    config = load_config()
    providers = config.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        config["providers"] = providers
    provider_table = providers.setdefault("grafana", {})
    if not isinstance(provider_table, dict):
        provider_table = {}
        providers["grafana"] = provider_table
    env_table = provider_table.setdefault("env", {})
    if not isinstance(env_table, dict):
        env_table = {}
        provider_table["env"] = env_table
    for key, value in updates.items():
        if value:
            env_table[key] = value
    write_config(config)
    return primary_config_file()


def _search_params(args: argparse.Namespace) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("query", args.query), ("limit", str(args.limit)), ("page", str(args.page))]
    if args.type:
        params.append(("type", args.type))
    for tag in args.tag:
        params.append(("tag", tag))
    for folder_uid in args.folder_uid:
        params.append(("folderUIDs", folder_uid))
    for dashboard_uid in args.dashboard_uid:
        params.append(("dashboardUIDs", dashboard_uid))
    if args.starred:
        params.append(("starred", "true"))
    return params


def _search_payload(args: argparse.Namespace) -> dict[str, Any]:
    session = _load_session(
        base_url=args.base_url,
        token=args.service_account_token,
        org_id=args.org_id,
    )
    _require_configured_session(session)
    raw = _grafana_json(session, "/api/search", params=_search_params(args))
    if not isinstance(raw, list):
        raise ToolError("Grafana search returned a non-list payload")
    results: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = _relative_or_full_url(session.base_url, str(item.get("url") or ""))
        folder_url = _relative_or_full_url(session.base_url, str(item.get("folderUrl") or ""))
        results.append(
            {
                "id": item.get("id"),
                "uid": str(item.get("uid") or ""),
                "orgId": item.get("orgId"),
                "title": str(item.get("title") or ""),
                "type": str(item.get("type") or ""),
                "url": url,
                "folderId": item.get("folderId"),
                "folderUid": str(item.get("folderUid") or ""),
                "folderTitle": str(item.get("folderTitle") or ""),
                "folderUrl": folder_url,
                "tags": list(item.get("tags") or []),
                "isStarred": bool(item.get("isStarred")),
            }
        )
    return {
        "provider": "grafana",
        "baseUrl": session.base_url,
        "surface": "search",
        "query": args.query,
        "limit": args.limit,
        "page": args.page,
        "type": args.type or "",
        "tag": list(args.tag or []),
        "folderUid": list(args.folder_uid or []),
        "dashboardUid": list(args.dashboard_uid or []),
        "starred": bool(args.starred),
        "results": results,
        "size": len(results),
    }
def _search_markdown(payload: dict[str, Any]) -> str:
    query = str(payload.get("query") or "").strip()
    query_text = f"`{query}`" if query else "_all_"
    lines = [
        "# Grafana Search",
        "",
        *render_source_metadata_lines(
            derive_source_metadata_from_payload(
                {
                    "plugin": "grafana",
                    "locator": canonical_locator(_search_locator_argv_from_payload(payload)),
                    "url": payload.get("baseUrl") or "",
                    "source_updated_at": "",
                }
            )
        ),
        "",
        f"- Query: {query_text}",
        f"- Result count: {payload.get('size') or 0}",
        f"- Type filter: `{payload.get('type') or 'all'}`",
        "",
    ]
    results = list(payload.get("results") or [])
    if not results:
        lines.append("_No results._")
        return "\n".join(lines).rstrip() + "\n"
    for item in results:
        title = str(item.get("title") or "").strip() or "_untitled_"
        uid = str(item.get("uid") or "").strip()
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"- Type: `{item.get('type') or ''}`")
        if uid:
            lines.append(f"- UID: `{uid}`")
        if item.get("folderTitle"):
            lines.append(f"- Folder: {item.get('folderTitle')}")
        if item.get("url"):
            lines.append(f"- URL: {item.get('url')}")
        tags = list(item.get("tags") or [])
        lines.append(f"- Tags: {', '.join(str(tag) for tag in tags) if tags else '_none_'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _search_summary(payload: dict[str, Any]) -> str:
    lines = [
        f"surface\t{payload.get('surface') or 'search'}",
        f"query\t{payload.get('query') or ''}",
        f"results\t{payload.get('size') or 0}",
    ]
    for item in payload.get("results") or []:
        lines.append(
            "\t".join(
                [
                    str(item.get("type") or ""),
                    str(item.get("uid") or ""),
                    str(item.get("title") or ""),
                    str(item.get("folderTitle") or ""),
                    str(item.get("url") or ""),
                ]
            )
        )
    return "\n".join(lines)


def _dashboard_payload_for_ref(session: Session, ref: str) -> dict[str, Any]:
    _require_configured_session(session)
    uid = _dashboard_uid_from_ref(ref)
    if not uid:
        raise ToolError(
            f"invalid Grafana dashboard reference: {ref}. Expected a dashboard URL or uid"
        )
    raw = _grafana_json(session, f"/api/dashboards/uid/{urllib.parse.quote(uid)}")
    if not isinstance(raw, dict):
        raise ToolError("Grafana dashboard payload was not a JSON object")
    dashboard = raw.get("dashboard") if isinstance(raw.get("dashboard"), dict) else {}
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    url = _relative_or_full_url(session.base_url, str(meta.get("url") or ""))
    return {
        "provider": "grafana",
        "baseUrl": session.base_url,
        "uid": str(dashboard.get("uid") or uid),
        "title": str(dashboard.get("title") or meta.get("slug") or uid),
        "dashboard": dashboard,
        "meta": {**meta, "url": url},
    }


def _dashboard_payload(args: argparse.Namespace) -> dict[str, Any]:
    session = _load_session(
        base_url=args.base_url,
        token=args.service_account_token,
        org_id=args.org_id,
    )
    return _dashboard_payload_for_ref(session, args.ref)


def _dashboard_markdown(payload: dict[str, Any]) -> str:
    dashboard = dict(payload.get("dashboard") or {})
    meta = dict(payload.get("meta") or {})
    title = str(payload.get("title") or payload.get("uid") or "dashboard")
    lines = [
        f"# {title}",
        "",
        *render_source_metadata_lines(
            derive_source_metadata_from_payload(
                {
                    "plugin": "grafana",
                    "locator": canonical_locator(["get", payload.get("uid") or ""]),
                    "url": meta.get("url") or payload.get("baseUrl") or "",
                    "source_updated_at": meta.get("updated") or "",
                }
            )
        ),
        "",
        f"- UID: `{payload.get('uid') or ''}`",
        f"- URL: {meta.get('url') or ''}",
        f"- Folder UID: `{meta.get('folderUid') or ''}`",
        f"- Folder Title: {meta.get('folderTitle') or ''}",
        f"- Version: {dashboard.get('version') or ''}",
        f"- Editable: {str(bool(meta.get('canEdit') or dashboard.get('editable'))).lower()}",
        f"- Tags: {', '.join(str(tag) for tag in dashboard.get('tags') or []) or '_none_'}",
        "",
        "```json",
        json.dumps(payload, indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)


def _dashboard_summary(payload: dict[str, Any]) -> str:
    dashboard = dict(payload.get("dashboard") or {})
    meta = dict(payload.get("meta") or {})
    return "\n".join(
        [
            f"title\t{payload.get('title') or ''}",
            f"uid\t{payload.get('uid') or ''}",
            f"url\t{meta.get('url') or ''}",
            f"folder_uid\t{meta.get('folderUid') or ''}",
            f"folder_title\t{meta.get('folderTitle') or ''}",
            f"version\t{dashboard.get('version') or ''}",
            f"editable\t{str(bool(meta.get('canEdit') or dashboard.get('editable'))).lower()}",
        ]
    )


def _datasources_payload(args: argparse.Namespace) -> dict[str, Any]:
    session = _load_session(
        base_url=args.base_url,
        token=args.service_account_token,
        org_id=args.org_id,
    )
    _require_configured_session(session)
    raw = _grafana_json(session, "/api/datasources")
    if not isinstance(raw, list):
        raise ToolError("Grafana datasources payload was not a JSON list")
    datasources: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        datasources.append(
            {
                "id": item.get("id"),
                "uid": str(item.get("uid") or ""),
                "name": str(item.get("name") or ""),
                "type": str(item.get("type") or ""),
                "url": str(item.get("url") or ""),
                "access": str(item.get("access") or ""),
                "isDefault": bool(item.get("isDefault")),
                "readOnly": bool(item.get("readOnly")),
            }
        )
    paged, page = _paginate_items(
        datasources,
        offset=args.offset,
        limit=args.limit,
        include_all=bool(args.all),
    )
    return {
        "provider": "grafana",
        "baseUrl": session.base_url,
        **page,
        "datasources": paged,
    }


def _datasources_summary(payload: dict[str, Any]) -> str:
    lines = [
        f"total\t{payload.get('totalCount') or 0}",
        f"shown\t{payload.get('shownCount') or 0}",
        f"offset\t{payload.get('offset') or 0}",
    ]
    if payload.get("nextOffset") is not None:
        lines.append(f"next_offset\t{payload.get('nextOffset')}")
    for item in payload.get("datasources") or []:
        lines.append(
            "\t".join(
                [
                    str(item.get("uid") or ""),
                    str(item.get("type") or ""),
                    str(item.get("name") or ""),
                    str(bool(item.get("isDefault"))).lower(),
                    str(item.get("access") or ""),
                    str(item.get("url") or ""),
                ]
            )
        )
    return "\n".join(lines)


def _datasource_matches(item: dict[str, Any], ref: str) -> bool:
    normalized = ref.strip()
    if not normalized:
        return False
    return (
        str(item.get("uid") or "") == normalized
        or str(item.get("name") or "").casefold() == normalized.casefold()
    )


def _datasource_by_uid(session: Session, uid: str) -> dict[str, Any]:
    raw = _grafana_json(session, f"/api/datasources/uid/{urllib.parse.quote(uid)}")
    if not isinstance(raw, dict):
        raise ToolError(f"Grafana datasource {uid} did not resolve to a JSON object")
    return {
        "id": raw.get("id"),
        "uid": str(raw.get("uid") or uid),
        "name": str(raw.get("name") or ""),
        "type": str(raw.get("type") or ""),
        "url": str(raw.get("url") or ""),
        "access": str(raw.get("access") or ""),
        "isDefault": bool(raw.get("isDefault")),
        "readOnly": bool(raw.get("readOnly")),
    }


def _dashboard_datasource_uids(payload: dict[str, Any]) -> list[str]:
    dashboard = dict(payload.get("dashboard") or {})
    seen: list[str] = []
    for panel in dashboard.get("panels") or []:
        if not isinstance(panel, dict):
            continue
        datasource = panel.get("datasource")
        if isinstance(datasource, dict):
            uid = str(datasource.get("uid") or "").strip()
            if uid and uid not in seen:
                seen.append(uid)
        for target in panel.get("targets") or []:
            if not isinstance(target, dict):
                continue
            target_ds = target.get("datasource")
            if not isinstance(target_ds, dict):
                continue
            uid = str(target_ds.get("uid") or "").strip()
            if uid and uid not in seen:
                seen.append(uid)
    return seen


def _resolve_query_datasource(
    session: Session,
    *,
    datasource_ref: str,
    dashboard_ref: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if datasource_ref:
        datasources_payload = _datasources_payload(
            argparse.Namespace(
                base_url=session.base_url,
                service_account_token=session.token,
                org_id=session.org_id,
            )
        )
        matches = [
            item
            for item in datasources_payload.get("datasources") or []
            if _datasource_matches(item, datasource_ref)
        ]
        if not matches:
            raise ToolError(
                f"unknown Grafana datasource: {datasource_ref}. Run `gotta grafana datasources`."
            )
        if len(matches) > 1:
            raise ToolError(
                f"ambiguous Grafana datasource: {datasource_ref}. Match by uid instead of name."
            )
        return matches[0], None
    if dashboard_ref:
        dashboard = _dashboard_payload_for_ref(session, dashboard_ref)
        uids = _dashboard_datasource_uids(dashboard)
        if not uids:
            raise ToolError(
                f"dashboard {dashboard.get('uid') or dashboard_ref} does not expose a queryable datasource"
            )
        if len(uids) > 1:
            raise ToolError(
                f"dashboard {dashboard.get('uid') or dashboard_ref} uses multiple datasources; pass --datasource"
            )
        return _datasource_by_uid(session, uids[0]), dashboard
    raise ToolError(
        "Grafana query requires --datasource <uid-or-name> or --dashboard <uid-or-url>"
    )


def _effective_query_context(
    args: argparse.Namespace,
) -> tuple[str, str, str, str]:
    dashboard_context = _dashboard_ref_context(str(args.dashboard or ""))
    dashboard_ref = str(args.dashboard or "").strip()
    from_time = str(args.from_time or "").strip() or dashboard_context.from_time or "now-1h"
    to_time = str(args.to_time or "").strip() or dashboard_context.to_time or "now"
    org_id = str(args.org_id or "").strip() or dashboard_context.org_id
    return dashboard_ref, org_id, from_time, to_time


def _frame_series(result: dict[str, Any]) -> list[dict[str, Any]]:
    frames = result.get("frames") or []
    series: list[dict[str, Any]] = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        schema = frame.get("schema") or {}
        fields = list(schema.get("fields") or [])
        values = list((frame.get("data") or {}).get("values") or [])
        if not fields or not values:
            continue
        timestamps: list[int] = []
        for field, column in zip(fields, values):
            if str(field.get("name") or "") == "Time":
                timestamps = [int(value) for value in column]
                break
        for field, column in zip(fields, values):
            if str(field.get("name") or "") == "Time":
                continue
            labels = dict(field.get("labels") or {})
            points: list[dict[str, Any]] = []
            if timestamps:
                for ts, value in zip(timestamps, column):
                    points.append({"time": int(ts), "value": value})
            else:
                for value in column:
                    points.append({"value": value})
            series.append(
                {
                    "name": str(field.get("name") or "Value"),
                    "labels": labels,
                    "points": points,
                }
            )
    return series


def _query_payload(args: argparse.Namespace) -> dict[str, Any]:
    dashboard_ref, effective_org_id, effective_from_time, effective_to_time = _effective_query_context(
        args
    )
    session = _load_session(
        base_url=args.base_url,
        token=args.service_account_token,
        org_id=effective_org_id,
    )
    _require_configured_session(session)
    datasource, dashboard = _resolve_query_datasource(
        session,
        datasource_ref=args.datasource or "",
        dashboard_ref=dashboard_ref,
    )
    raw = _grafana_json(
        session,
        "/api/ds/query",
        method="POST",
        payload={
            "from": effective_from_time,
            "to": effective_to_time,
            "queries": [
                {
                    "refId": "A",
                    "datasource": {"uid": datasource["uid"]},
                    "expr": args.expr,
                    "instant": not args.range,
                    "queryType": "range" if args.range else "instant",
                }
            ],
        },
    )
    if not isinstance(raw, dict):
        raise ToolError("Grafana query payload was not a JSON object")
    result = dict((raw.get("results") or {}).get("A") or {})
    series = _frame_series(result)
    payload: dict[str, Any] = {
        "provider": "grafana",
        "baseUrl": session.base_url,
        "query": args.expr,
        "mode": "range" if args.range else "instant",
        "from": effective_from_time,
        "to": effective_to_time,
        "datasource": datasource,
        "series": series,
        "seriesCount": len(series),
    }
    if session.org_id:
        payload["orgId"] = session.org_id
    if dashboard is not None:
        payload["dashboard"] = {
            "uid": dashboard.get("uid") or "",
            "title": dashboard.get("title") or "",
            "url": dict(dashboard.get("meta") or {}).get("url") or "",
        }
    return payload


def _series_label_text(series: dict[str, Any]) -> str:
    labels = dict(series.get("labels") or {})
    if not labels:
        return ""
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def _query_summary(payload: dict[str, Any]) -> str:
    lines = [
        f"mode\t{payload.get('mode') or 'instant'}",
        f"datasource_uid\t{dict(payload.get('datasource') or {}).get('uid') or ''}",
        f"datasource_name\t{dict(payload.get('datasource') or {}).get('name') or ''}",
        f"series\t{payload.get('seriesCount') or 0}",
    ]
    dashboard = dict(payload.get("dashboard") or {})
    if dashboard:
        lines.append(f"dashboard_uid\t{dashboard.get('uid') or ''}")
        lines.append(f"dashboard_title\t{dashboard.get('title') or ''}")
    for series in payload.get("series") or []:
        points = list(series.get("points") or [])
        last = points[-1] if points else {}
        lines.append(
            "\t".join(
                [
                    str(series.get("name") or ""),
                    _series_label_text(series),
                    str(last.get("value") if isinstance(last, dict) else ""),
                    str(len(points)),
                ]
            )
        )
    return "\n".join(lines)


def _query_markdown(payload: dict[str, Any]) -> str:
    datasource = dict(payload.get("datasource") or {})
    dashboard = dict(payload.get("dashboard") or {})
    lines = [
        "# Grafana Query",
        "",
        f"- Mode: `{payload.get('mode') or 'instant'}`",
        f"- Datasource: `{datasource.get('name') or ''}` (`{datasource.get('uid') or ''}`)",
        f"- Query: `{payload.get('query') or ''}`",
    ]
    if dashboard:
        lines.append(f"- Dashboard: {dashboard.get('title') or ''} (`{dashboard.get('uid') or ''}`)")
        if dashboard.get("url"):
            lines.append(f"- Dashboard URL: {dashboard.get('url')}")
    lines.append("")
    series = list(payload.get("series") or [])
    if not series:
        lines.append("_No series returned._")
        return "\n".join(lines).rstrip() + "\n"
    for item in series:
        label = _series_label_text(item)
        heading = str(item.get("name") or "Value")
        if label:
            heading = f"{heading} [{label}]"
        lines.append(f"## {heading}")
        lines.append("")
        for point in item.get("points") or []:
            value = point.get("value") if isinstance(point, dict) else point
            timestamp = point.get("time") if isinstance(point, dict) else ""
            if timestamp:
                lines.append(f"- `{timestamp}` -> `{value}`")
            else:
                lines.append(f"- `{value}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _slug(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower())
    return normalized.strip("-") or fallback


def _search_locator_argv_from_payload(payload: dict[str, Any]) -> list[str]:
    parts = ["search"]
    type_filter = str(payload.get("type") or "").strip()
    if type_filter:
        parts.extend(["--type", type_filter])
    for tag in payload.get("tag") or []:
        parts.extend(["--tag", str(tag)])
    for folder_uid in payload.get("folderUid") or []:
        parts.extend(["--folder-uid", str(folder_uid)])
    for dashboard_uid in payload.get("dashboardUid") or []:
        parts.extend(["--dashboard-uid", str(dashboard_uid)])
    if payload.get("starred"):
        parts.append("--starred")
    limit = int(payload.get("limit") or DEFAULT_LIMIT)
    if limit != DEFAULT_LIMIT:
        parts.extend(["--limit", str(limit)])
    page = int(payload.get("page") or 1)
    if page != 1:
        parts.extend(["--page", str(page)])
    query = str(payload.get("query") or "").strip()
    if query:
        parts.append(query)
    return parts


def _normalize_search_locator_tail(args: argparse.Namespace) -> list[str]:
    payload = {
        "query": args.query,
        "type": args.type,
        "tag": list(args.tag or []),
        "folderUid": list(args.folder_uid or []),
        "dashboardUid": list(args.dashboard_uid or []),
        "starred": bool(args.starred),
        "limit": args.limit,
        "page": args.page,
    }
    parts = _search_locator_argv_from_payload(payload)
    if getattr(args, "output", "markdown") != "markdown":
        parts.extend(["--output", args.output])
    return parts


def _normalize_datasources_locator_tail(args: argparse.Namespace) -> list[str]:
    parts = ["datasources"]
    if getattr(args, "all", False):
        parts.append("--all")
    elif int(getattr(args, "limit", DEFAULT_DATASOURCE_LIST_LIMIT)) != DEFAULT_DATASOURCE_LIST_LIMIT:
        parts.extend(["--limit", str(args.limit)])
    if int(getattr(args, "offset", 0)):
        parts.extend(["--offset", str(args.offset)])
    if getattr(args, "output", "summary") != "summary":
        parts.extend(["--output", args.output])
    return parts


def _normalize_query_locator_tail(args: argparse.Namespace) -> list[str]:
    dashboard_ref, effective_org_id, effective_from_time, effective_to_time = _effective_query_context(
        args
    )
    parts = ["query"]
    if args.datasource:
        parts.extend(["--datasource", args.datasource])
    if dashboard_ref:
        parts.extend(["--dashboard", _dashboard_uid_from_ref(dashboard_ref) or dashboard_ref])
    if effective_org_id:
        parts.extend(["--org-id", effective_org_id])
    if args.range:
        parts.append("--range")
    if effective_from_time != "now-1h":
        parts.extend(["--from", effective_from_time])
    if effective_to_time != "now":
        parts.extend(["--to", effective_to_time])
    parts.append(args.expr)
    return parts


def _normalize_args(argv: list[str]) -> list[str]:
    if not argv:
        return ["status"]
    return argv


def route_target(target: str) -> list[str] | None:
    if target.startswith("grafana:"):
        rest = target.removeprefix("grafana:")
        if rest == "status":
            return ["status"]
        if rest == "datasources":
            return ["datasources"]
        if rest.startswith("datasources "):
            parts = split_locator_tail(rest.removeprefix("datasources ").strip())
            if not parts:
                return None
            return ["datasources", *parts]
        if rest == "search":
            return ["search"]
        if rest.startswith("search "):
            parts = split_locator_tail(rest.removeprefix("search ").strip())
            return ["search", *parts]
        if rest.startswith("query "):
            return query_route(
                "query",
                rest.removeprefix("query ").strip(),
                valued_flags=("--datasource", "--dashboard", "--org-id", "--from", "--to", "--output"),
                boolean_flags=("--range",),
            )
        if rest.startswith("get "):
            parts = split_locator_tail(rest.removeprefix("get ").strip())
            if len(parts) != 1:
                return None
            return ["get", parts[0]]
        return None
    if DASHBOARD_URL_RE.match(strip_http_url_fragment(target)):
        return ["get", target]
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta grafana",
        description=(
            "Read-only Grafana dashboard discovery and datasource queries through "
            "a service-account token."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    auth = sub.add_parser("auth", help="persist Grafana base URL and service-account token")
    status = sub.add_parser("status", help="inspect Grafana auth and read readiness")
    datasources = sub.add_parser("datasources", help="list readable Grafana datasources")
    search = sub.add_parser("search", help="search saved Grafana objects")
    query = sub.add_parser("query", help="run a datasource query through Grafana")
    get = sub.add_parser("get", help="fetch one dashboard by URL or uid")

    for parser_ in (auth, status, datasources, search, query, get):
        parser_.add_argument("--base-url", help=f"Grafana base URL override (default: {GRAFANA_BASE_URL_ENV})")
        parser_.add_argument(
            "--service-account-token",
            help=f"Grafana service account token override (default: {GRAFANA_TOKEN_ENV})",
        )
        parser_.add_argument("--org-id", help=f"Grafana org id override (default: {GRAFANA_ORG_ID_ENV})")

    auth.add_argument("--output", choices=["json", "summary"], default="summary")
    status.add_argument("--output", choices=["json", "summary"], default="summary")
    datasources.add_argument("--output", choices=["json", "summary"], default="summary")
    datasources.add_argument("--limit", type=positive_int, default=DEFAULT_DATASOURCE_LIST_LIMIT)
    datasources.add_argument(
        "--offset",
        type=nonnegative_int,
        default=0,
        help="skip the first N datasources before rendering the current page",
    )
    datasources.add_argument(
        "--all",
        action="store_true",
        help="show all datasources explicitly instead of the default bounded page",
    )

    search.add_argument("query", nargs="?", default="", help="optional Grafana search query")
    search.add_argument("--type", choices=["dash-folder", "dash-db"], default="")
    search.add_argument("--tag", action="append", default=[])
    search.add_argument("--folder-uid", action="append", default=[])
    search.add_argument("--dashboard-uid", action="append", default=[])
    search.add_argument("--starred", action="store_true")
    search.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--output", choices=["markdown", "summary", "json"], default="markdown")

    query.add_argument("expr", help="datasource query expression (for AMP/Prometheus datasources, this is PromQL)")
    source_group = query.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--datasource", help="Grafana datasource uid or exact name")
    source_group.add_argument("--dashboard", help="Infer datasource from one dashboard uid or URL")
    query.add_argument(
        "--range",
        action="store_true",
        help="run a range query instead of an instant query",
    )
    query.add_argument(
        "--from",
        dest="from_time",
        default=None,
        help="Grafana relative or absolute start time (default: inherit from dashboard URL or now-1h)",
    )
    query.add_argument(
        "--to",
        dest="to_time",
        default=None,
        help="Grafana relative or absolute end time (default: inherit from dashboard URL or now)",
    )
    query.add_argument("--output", choices=["markdown", "summary", "json"], default="summary")

    get.add_argument("ref", help="Grafana dashboard URL or uid")
    get.add_argument("--output", choices=["markdown", "summary", "json"], default="markdown")
    return parser


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    return _build_parser().parse_args(_normalize_args(argv))


def canonical_locator(argv: list[str]) -> str:
    args = _parse_cli(argv)
    if args.command == "status":
        return "grafana:status"
    if args.command == "datasources":
        return "grafana:" + " ".join(_normalize_datasources_locator_tail(args))
    if args.command == "auth":
        return "grafana:auth"
    if args.command == "search":
        return "grafana:" + " ".join(_normalize_search_locator_tail(args))
    if args.command == "query":
        return "grafana:" + " ".join(_normalize_query_locator_tail(args))
    return f"grafana:get {_dashboard_uid_from_ref(args.ref) or args.ref}"


def preferred_name(argv: list[str], _options: Any) -> str:
    args = _parse_cli(argv)
    if args.command in {"status", "auth", "datasources"}:
        extension = "json" if getattr(args, "output", "summary") == "json" else "summary"
        if args.command == "datasources":
            suffix = _list_window_suffix(
                limit=args.limit,
                offset=args.offset,
                include_all=bool(args.all),
                default_limit=DEFAULT_DATASOURCE_LIST_LIMIT,
            )
            return f"grafana-datasources{suffix}.{extension}"
        return f"grafana.{extension}"
    if args.command == "search":
        extension = {"markdown": "md", "summary": "summary", "json": "json"}[args.output]
        suffix_parts: list[str] = []
        if args.type:
            suffix_parts.append(args.type)
        if args.query:
            suffix_parts.append(_slug(args.query, fallback="all"))
        suffix = "-".join(part for part in suffix_parts if part) or "all"
        return f"grafana-search-{suffix}.{extension}"
    if args.command == "query":
        extension = {"markdown": "md", "summary": "summary", "json": "json"}[args.output]
        return f"grafana-query-{_slug(args.expr, fallback='grafana')}.{extension}"
    extension = {"markdown": "md", "summary": "summary", "json": "json"}[args.output]
    return f"{_dashboard_uid_from_ref(args.ref) or 'grafana-dashboard'}.{extension}"


def cmd_auth(args: argparse.Namespace) -> int:
    updates = {
        GRAFANA_BASE_URL_ENV: _normalize_base_url(args.base_url) if args.base_url else "",
        GRAFANA_TOKEN_ENV: args.service_account_token.strip() if args.service_account_token else "",
        GRAFANA_ORG_ID_ENV: args.org_id.strip() if args.org_id else "",
    }
    if any(updates.values()):
        path = _persist_auth_updates(updates)
    else:
        path = primary_config_file()
    payload = _status_payload(
        base_url=updates[GRAFANA_BASE_URL_ENV],
        token=updates[GRAFANA_TOKEN_ENV],
        org_id=updates[GRAFANA_ORG_ID_ENV],
    )
    payload["configFile"] = str(path)
    if args.output == "json":
        print_json(payload)
        return 0
    print(_render_status_summary(payload))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    payload = _status_payload(
        base_url=args.base_url or "",
        token=args.service_account_token or "",
        org_id=args.org_id or "",
    )
    if args.output == "json":
        print_json(payload)
        return 0
    print(_render_status_summary(payload))
    return 0


def cmd_datasources(args: argparse.Namespace) -> int:
    payload = _datasources_payload(args)
    if args.output == "json":
        print_json(payload)
        return 0
    print(_datasources_summary(payload))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    args.limit = max(1, min(int(args.limit), MAX_LIMIT))
    args.page = max(1, int(args.page))
    payload = _search_payload(args)
    if args.output == "json":
        print_json(payload)
        return 0
    if args.output == "summary":
        print(_search_summary(payload))
        return 0
    print(_search_markdown(payload), end="")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    payload = _query_payload(args)
    if args.output == "json":
        print_json(payload)
        return 0
    if args.output == "summary":
        print(_query_summary(payload))
        return 0
    print(_query_markdown(payload), end="")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    payload = _dashboard_payload(args)
    if args.output == "json":
        print_json(payload)
        return 0
    if args.output == "summary":
        print(_dashboard_summary(payload))
        return 0
    print(_dashboard_markdown(payload), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    if is_long_help_request(argv):
        print_long_help(_build_parser())
        return 0
    parser = _build_parser()
    try:
        args = parser.parse_args(_normalize_args(argv))
    except SystemExit as exc:
        if int(exc.code or 0) == 0:
            return 0
        raise
    try:
        if args.command == "auth":
            return cmd_auth(args)
        if args.command == "datasources":
            return cmd_datasources(args)
        if args.command == "search":
            return cmd_search(args)
        if args.command == "query":
            return cmd_query(args)
        if args.command == "get":
            return cmd_get(args)
        return cmd_status(args)
    except ToolError as exc:
        return die(str(exc))
