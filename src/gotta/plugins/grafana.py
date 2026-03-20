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
LEGACY_GRAFANA_TOKEN_ID_ENV = "GOTTA_GRAFANA_SERVICE_ACCOUNT_TOKEN_ID"
LEGACY_GRAFANA_TOKEN_SECRET_ENV = "GOTTA_GRAFANA_SERVICE_ACCOUNT_TOKEN_SECRET"

DEFAULT_LIMIT = 50
MAX_LIMIT = 5000
DASHBOARD_URL_RE = re.compile(r"^https?://[^/]+(?:/[^?#]*)?/d(?:-solo)?/(?P<uid>[^/?#]+)")


class ToolError(RuntimeError):
    """Raised when the Grafana API contract cannot be satisfied."""


@dataclass(frozen=True)
class Session:
    base_url: str
    token: str
    org_id: str
    legacy_keys: tuple[str, ...] = ()


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _load_grafana_config_env() -> dict[str, str]:
    return extract_provider_env(load_config(), "grafana")


def _env_or_config(config_env: dict[str, str], name: str) -> str:
    return os.environ.get(name, "").strip() or str(config_env.get(name) or "").strip()


def _legacy_keys(config_env: dict[str, str]) -> tuple[str, ...]:
    found: list[str] = []
    for key in (LEGACY_GRAFANA_TOKEN_ID_ENV, LEGACY_GRAFANA_TOKEN_SECRET_ENV):
        if os.environ.get(key, "").strip() or str(config_env.get(key) or "").strip():
            found.append(key)
    return tuple(found)


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
        legacy_keys=_legacy_keys(config_env),
    )


def _headers(session: Session) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {session.token}",
    }
    if session.org_id:
        headers["X-Grafana-Org-Id"] = session.org_id
    return headers


def _grafana_json(
    session: Session,
    path: str,
    *,
    params: list[tuple[str, str]] | None = None,
) -> Any:
    query = urllib.parse.urlencode(params or [], doseq=True)
    url = f"{session.base_url}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(url, headers=_headers(session), method="GET")
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
    candidate = strip_http_url_fragment(raw.strip())
    if not candidate:
        return ""
    match = DASHBOARD_URL_RE.match(candidate)
    if match:
        return match.group("uid")
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme:
        return ""
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "", candidate)
    return normalized


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
        "legacyKeysDetected": list(session.legacy_keys),
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
        payload["nextStep"] = "run `gotta grafana search <query>`"
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
    legacy = payload.get("legacyKeysDetected") or []
    if legacy:
        lines.append(f"legacy_keys_detected\t{','.join(str(item) for item in legacy)}")
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
    env_table.pop(LEGACY_GRAFANA_TOKEN_ID_ENV, None)
    env_table.pop(LEGACY_GRAFANA_TOKEN_SECRET_ENV, None)
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
    if not session.base_url:
        raise ToolError(f"missing Grafana base URL; set {GRAFANA_BASE_URL_ENV}")
    if not session.token:
        raise ToolError(f"missing Grafana service account token; set {GRAFANA_TOKEN_ENV}")
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
    lines = [
        f"# Grafana Search: {payload.get('query') or ''}",
        "",
        *render_source_metadata_lines(
            derive_source_metadata_from_payload(
                {
                    "plugin": "grafana",
                    "locator": canonical_locator(["search", payload.get("query") or ""]),
                    "url": payload.get("baseUrl") or "",
                    "source_updated_at": "",
                }
            )
        ),
        "",
        f"- Query: `{payload.get('query') or ''}`",
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


def _dashboard_payload(args: argparse.Namespace) -> dict[str, Any]:
    session = _load_session(
        base_url=args.base_url,
        token=args.service_account_token,
        org_id=args.org_id,
    )
    if not session.base_url:
        raise ToolError(f"missing Grafana base URL; set {GRAFANA_BASE_URL_ENV}")
    if not session.token:
        raise ToolError(f"missing Grafana service account token; set {GRAFANA_TOKEN_ENV}")
    uid = _dashboard_uid_from_ref(args.ref)
    if not uid:
        raise ToolError(
            f"invalid Grafana dashboard reference: {args.ref}. Expected a dashboard URL or uid"
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


def _slug(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower())
    return normalized.strip("-") or fallback


def _normalize_search_locator_tail(args: argparse.Namespace) -> list[str]:
    parts = ["search"]
    if args.type:
        parts.extend(["--type", args.type])
    for tag in args.tag:
        parts.extend(["--tag", tag])
    for folder_uid in args.folder_uid:
        parts.extend(["--folder-uid", folder_uid])
    for dashboard_uid in args.dashboard_uid:
        parts.extend(["--dashboard-uid", dashboard_uid])
    if args.starred:
        parts.append("--starred")
    if args.limit != DEFAULT_LIMIT:
        parts.extend(["--limit", str(args.limit)])
    if args.page != 1:
        parts.extend(["--page", str(args.page)])
    parts.append(args.query)
    return parts


def _normalize_args(argv: list[str]) -> list[str]:
    if not argv:
        return ["status"]
    first = argv[0]
    actions = {"auth", "status", "search", "get"}
    if first in actions or first.startswith("-"):
        return argv
    return ["get", *argv]


def route_target(target: str) -> list[str] | None:
    if target.startswith("grafana:"):
        rest = target.removeprefix("grafana:")
        if rest == "status":
            return ["status"]
        if rest.startswith("search "):
            return query_route(
                "search",
                rest.removeprefix("search ").strip(),
                valued_flags=("--type", "--tag", "--folder-uid", "--dashboard-uid", "--limit", "--page", "--output"),
                boolean_flags=("--starred",),
            )
        if rest.startswith("get "):
            parts = split_locator_tail(rest.removeprefix("get ").strip())
            if len(parts) != 1:
                return None
            return ["get", parts[0]]
        if _dashboard_uid_from_ref(rest):
            return ["get", rest]
        return None
    if DASHBOARD_URL_RE.match(strip_http_url_fragment(target)):
        return [target]
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta grafana",
        description=(
            "Read-only Grafana dashboard discovery through a service-account token. "
            "Bare invocation shows auth status; bare non-subcommand refs are treated as dashboard gets."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    auth = sub.add_parser("auth", help="persist Grafana base URL and service-account token")
    status = sub.add_parser("status", help="inspect Grafana auth and read readiness")
    search = sub.add_parser("search", help="search dashboards and folders")
    get = sub.add_parser("get", help="fetch one dashboard by URL or uid")

    for parser_ in (auth, status, search, get):
        parser_.add_argument("--base-url", help=f"Grafana base URL override (default: {GRAFANA_BASE_URL_ENV})")
        parser_.add_argument(
            "--service-account-token",
            help=f"Grafana service account token override (default: {GRAFANA_TOKEN_ENV})",
        )
        parser_.add_argument("--org-id", help=f"Grafana org id override (default: {GRAFANA_ORG_ID_ENV})")

    auth.add_argument("--output", choices=["json", "summary"], default="summary")
    status.add_argument("--output", choices=["json", "summary"], default="summary")

    search.add_argument("query", help="Grafana search query")
    search.add_argument("--type", choices=["dash-folder", "dash-db"], default="")
    search.add_argument("--tag", action="append", default=[])
    search.add_argument("--folder-uid", action="append", default=[])
    search.add_argument("--dashboard-uid", action="append", default=[])
    search.add_argument("--starred", action="store_true")
    search.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--output", choices=["markdown", "summary", "json"], default="markdown")

    get.add_argument("ref", help="Grafana dashboard URL or uid")
    get.add_argument("--output", choices=["markdown", "summary", "json"], default="markdown")
    return parser


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    return _build_parser().parse_args(_normalize_args(argv))


def canonical_locator(argv: list[str]) -> str:
    args = _parse_cli(argv)
    if args.command == "status":
        return "grafana:status"
    if args.command == "auth":
        return "grafana:auth"
    if args.command == "search":
        return "grafana:" + " ".join(_normalize_search_locator_tail(args))
    return f"grafana:{_dashboard_uid_from_ref(args.ref)}"


def preferred_name(argv: list[str], _options: Any) -> str:
    args = _parse_cli(argv)
    if args.command in {"status", "auth"}:
        extension = "json" if getattr(args, "output", "summary") == "json" else "summary"
        return f"grafana.{extension}"
    if args.command == "search":
        extension = {"markdown": "md", "summary": "summary", "json": "json"}[args.output]
        return f"grafana-search-{_slug(args.query, fallback='grafana')}.{extension}"
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
        if args.command == "search":
            return cmd_search(args)
        if args.command == "get":
            return cmd_get(args)
        return cmd_status(args)
    except ToolError as exc:
        return die(str(exc))
