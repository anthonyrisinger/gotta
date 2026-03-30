#!/usr/bin/env python3
"""Read-only Google Docs helper for gotta."""

from __future__ import annotations

import argparse
import html
import json
import re
import shlex
import shutil
import signal
import subprocess
import sys
import urllib.parse

from gotta.capture import Capture, capture_json_command, json_bytes
from gotta.projection import Projection, projection_bytes
from gotta.helptext import is_long_help_request, print_long_help
from gotta.project import html_markdown, html_text, pretty_json
from gotta.resolve.route import query_route, strip_http_url_fragment
from gotta.resolve.search import plain_text_search_route
from gotta.source.render import render_source_metadata_lines
from gotta.source.stamp import derive_source_metadata_from_payload
from gotta.providers.google import (
    GOOGLE_DOCS_API_URL,
    GOOGLE_DOC_MIME,
    OAUTH_DIR,
    TOKEN_FILE,
    GoogleError,
    drive_file_meta,
    drive_export,
    drive_search_files,
    ensure_google_session,
    escape_drive_query,
    google_json,
    google_status_payload,
    parse_doc_ref,
    run_oauth_bootstrap,
)


DEFAULT_SEARCH_LIMIT = 10
GOOGLE_REDIRECT_URL_RE = re.compile(r"https://(?:www\.)?google\.com/url\?[^\"'<>]+")


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _slug(value: str, *, fallback: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    return value.strip("-") or fallback


def _output_extension(output: str) -> str:
    return {
        "markdown": "md",
        "summary": "summary",
        "text": "txt",
        "html": "html",
        "meta": "json",
        "json": "json",
    }.get(output, "md")


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def artifact_intent(argv: list[str]) -> str:
    if not argv:
        return "none"
    command = argv[0]
    if command == "search":
        return "discovery"
    if command == "get":
        return "evidence"
    return "none"


def _search_read_target(subject: str) -> str:
    subject = subject.strip()
    if not subject:
        return ""
    try:
        return canonical_locator(["get", subject])
    except Exception:
        return f"gdocs:{subject}"


def search_route(raw_tail: str) -> list[str]:
    return plain_text_search_route(
        "gdocs",
        raw_tail,
        read_redirects={"get": _search_read_target},
    )


def canonical_locator(argv: list[str]) -> str:
    args = _parse_cli(argv)
    if args.command == "get":
        doc_id, _ = parse_doc_ref(args.ref)
        return f"gdocs:{doc_id}"
    if args.command == "status":
        return "gdocs:status"
    return f"gdocs:{shlex.join(argv)}"


def preferred_name(argv: list[str], options: object) -> str:
    if getattr(options, "save_as", ""):
        return str(getattr(options, "save_as"))
    args = _parse_cli(argv)
    if args.command == "get":
        doc_id, _ = parse_doc_ref(args.ref)
        return f"{doc_id}.html"
    if args.command == "search":
        return f"gdocs-search-{_slug(args.query, fallback='gdocs')}.json"
    if args.command == "status":
        return f"gdocs.{_output_extension(args.output)}"
    return "gdocs.txt"


def route_target(target: str) -> list[str] | None:
    if target.startswith("gdocs:search "):
        return query_route(
            "search",
            target.removeprefix("gdocs:search "),
            valued_flags=("--limit", "--mode", "--output"),
        )
    if target.startswith("gdocs:"):
        return ["get", target.removeprefix("gdocs:")]
    if any(char.isspace() for char in target):
        return None
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != "docs.google.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] == "document" and "d" in parts:
        return ["get", strip_http_url_fragment(target)]
    return None


def document_meta(access_token: str, doc_id: str) -> dict[str, object]:
    docs_url = (
        f"{GOOGLE_DOCS_API_URL}/{urllib.parse.quote(doc_id)}"
        "?fields=documentId,title,revisionId"
    )
    meta = google_json(docs_url, access_token)
    drive_meta = drive_file_meta(
        access_token,
        doc_id,
        fields="id,name,createdTime,modifiedTime,webViewLink,owners(displayName,emailAddress)",
    )
    meta["url"] = str(
        drive_meta.get("webViewLink")
        or f"https://docs.google.com/document/d/{doc_id}/edit"
    )
    if drive_meta.get("createdTime"):
        meta["createdTime"] = drive_meta.get("createdTime")
    if drive_meta.get("modifiedTime"):
        meta["modifiedTime"] = drive_meta.get("modifiedTime")
    if drive_meta.get("owners"):
        meta["owners"] = drive_meta.get("owners")
    return meta


def document_json(access_token: str, doc_id: str) -> dict[str, object]:
    docs_url = f"{GOOGLE_DOCS_API_URL}/{urllib.parse.quote(doc_id)}"
    doc = google_json(docs_url, access_token)
    doc["url"] = f"https://docs.google.com/document/d/{doc_id}/edit"
    return doc


def html_to_markdown(data: bytes) -> bytes:
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise GoogleError(
            "pandoc is required for markdown conversion but is not installed"
        )
    proc = subprocess.run(
        [pandoc, "-f", "html", "-t", "gfm", "--wrap=none"],
        input=data,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GoogleError(detail or "pandoc failed to convert Google Docs HTML")
    return proc.stdout


def _owners_line(meta: dict[str, object]) -> str:
    owners = meta.get("owners")
    if not isinstance(owners, list):
        return ""
    owner_names = [
        str(owner.get("displayName") or "").strip()
        for owner in owners
        if isinstance(owner, dict) and str(owner.get("displayName") or "").strip()
    ]
    return ", ".join(owner_names)


def _markdown_prelude(doc_id: str, meta: dict[str, object]) -> bytes:
    header_lines = [
        f"- URL: {meta.get('url') or ''}",
        f"- Document ID: {doc_id}",
    ]
    if meta.get("createdTime"):
        header_lines.append(f"- Created: {meta.get('createdTime')}")
    if meta.get("modifiedTime"):
        header_lines.append(f"- Updated: {meta.get('modifiedTime')}")
    if meta.get("revisionId"):
        header_lines.append(f"- Revision: {meta.get('revisionId')}")
    owners = _owners_line(meta)
    if owners:
        header_lines.append(f"- Owners: {owners}")
    return ("\n".join(header_lines) + "\n\n---\n\n").encode("utf-8")


def _capture_meta(doc_id: str, meta: dict[str, object]) -> dict[str, object]:
    owners = meta.get("owners")
    return {
        "projector": "gdocs",
        "source_title": str(meta.get("title") or ""),
        "source_url": str(meta.get("url") or ""),
        "source_revision": str(meta.get("revisionId") or ""),
        "source_owners": owners if isinstance(owners, list) else [],
        "source_created_at": str(meta.get("createdTime") or ""),
        "source_updated_at": str(meta.get("modifiedTime") or ""),
        "doc_id": doc_id,
    }


def _canonicalize_google_redirect_url(target: str) -> str:
    unescaped = html.unescape(target)
    try:
        parsed = urllib.parse.urlsplit(unescaped)
    except ValueError:
        return target
    if (
        parsed.netloc.lower() not in {"www.google.com", "google.com"}
        or parsed.path != "/url"
    ):
        return target
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for key in ("q", "url"):
        redirect = str((params.get(key) or [""])[0] or "").strip()
        if redirect:
            return html.escape(redirect, quote=True)
    filtered = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"ust", "usg"}
    ]
    stable = urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(filtered, doseq=True),
            parsed.fragment,
        )
    )
    return html.escape(stable, quote=True)


def _canonicalize_export_html(data: bytes) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    canonical = GOOGLE_REDIRECT_URL_RE.sub(
        lambda match: _canonicalize_google_redirect_url(match.group(0)),
        text,
    )
    return canonical.encode("utf-8")


def capture(argv: list[str], _options: object) -> Capture:
    args = _parse_cli(argv)
    if args.command != "get":
        if args.command == "search":
            payload = capture_json_command(
                args,
                cmd_search,
                detail="gdocs search capture failed",
            )
            return Capture(
                data=payload,
                preferred_name=preferred_name(argv, object()),
                content_type="application/json",
                metadata={
                    "projector": "gdocs",
                    "gdocs_kind": "search",
                },
            )
        raise NotImplementedError("gdocs capture does not support this command")
    oauth_state = ensure_google_session(
        allow_bootstrap=True,
        interactive_ok=is_interactive(),
        auth_command="gdocs",
    )
    access_token = str(oauth_state.get("access_token") or "").strip()
    doc_id, _ = parse_doc_ref(args.ref)
    meta = document_meta(access_token, doc_id)
    document = document_json(access_token, doc_id)
    html = _canonicalize_export_html(drive_export(access_token, doc_id, "text/html"))
    return Capture(
        data=html,
        preferred_name=f"{doc_id}.html",
        content_type="text/html",
        metadata=_capture_meta(doc_id, meta),
        view_data={"meta": meta, "document": document},
    )


def project(argv: list[str], capture: Capture) -> Projection:
    kind = str(capture.metadata.get("gdocs_kind") or "get").strip()
    if kind == "search":
        payload = json.loads(capture.data.decode("utf-8"))
        if not argv:
            return projection_bytes(
                render_search_markdown(payload).encode("utf-8"),
                content_type="text/markdown",
            )
        args = _parse_cli(argv)
        if args.command != "search":
            return projection_bytes(capture.data, content_type=capture.content_type)
        if args.output == "json":
            return projection_bytes(
                pretty_json(capture.data), content_type="application/json"
            )
        return projection_bytes(
            render_search_markdown(payload).encode("utf-8"),
            content_type="text/markdown",
        )
    meta = capture.view_data.get("meta")
    if not isinstance(meta, dict):
        meta = {
            "title": capture.metadata.get("source_title") or "",
            "url": capture.metadata.get("source_url") or "",
            "revisionId": capture.metadata.get("source_revision") or "",
            "owners": capture.metadata.get("source_owners") or [],
            "createdTime": capture.metadata.get("source_created_at") or "",
            "modifiedTime": capture.metadata.get("source_updated_at") or "",
        }
    doc_id = str(capture.metadata.get("doc_id") or "").strip()
    if not argv:
        rendered = html_markdown(capture.data)
        body = rendered if rendered is not None else html_text(capture.data)
        return projection_bytes(
            _markdown_prelude(doc_id, meta) + body,
            content_type="text/markdown",
        )
    args = _parse_cli(argv)
    if args.command != "get":
        return projection_bytes(capture.data, content_type=capture.content_type)
    if args.output == "html":
        return projection_bytes(capture.data, content_type=capture.content_type)
    if args.output == "json":
        document = capture.view_data.get("document")
        if isinstance(document, dict):
            return projection_bytes(
                json_bytes(document),
                content_type="application/json",
            )
        return projection_bytes(json_bytes(meta), content_type="application/json")
    if args.output == "meta":
        return projection_bytes(json_bytes(meta), content_type="application/json")
    if args.output == "text":
        return projection_bytes(html_text(capture.data), content_type="text/plain")
    rendered = html_markdown(capture.data)
    body = rendered if rendered is not None else html_text(capture.data)
    return projection_bytes(
        _markdown_prelude(doc_id, meta) + body,
        content_type="text/markdown",
    )


def normalize_search_result(
    item: dict[str, object], *, matched_by: set[str]
) -> dict[str, object]:
    owners = item.get("owners")
    owner_names: list[str] = []
    if isinstance(owners, list):
        for owner in owners:
            if isinstance(owner, dict):
                display_name = str(owner.get("displayName") or "").strip()
                if display_name:
                    owner_names.append(display_name)
    return {
        "docId": str(item.get("id") or ""),
        "title": str(item.get("name") or "(untitled)"),
        "url": str(item.get("webViewLink") or ""),
        "createdTime": str(item.get("createdTime") or ""),
        "modifiedTime": str(item.get("modifiedTime") or ""),
        "owners": owner_names,
        "ownerDisplayName": owner_names[0] if owner_names else "",
        "matchedBy": sorted(matched_by),
    }


def merge_search_results(
    title_files: list[dict[str, object]],
    content_files: list[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for source, files in (("title", title_files), ("content", content_files)):
        for item in files:
            doc_id = str(item.get("id") or "").strip()
            if not doc_id:
                continue
            current = merged.get(doc_id)
            if current is None:
                merged[doc_id] = normalize_search_result(item, matched_by={source})
                continue
            matched_by = set(current.get("matchedBy") or [])
            matched_by.add(source)
            current["matchedBy"] = sorted(matched_by)
            if not current.get("url") and item.get("webViewLink"):
                current["url"] = str(item.get("webViewLink") or "")
            if not current.get("createdTime") and item.get("createdTime"):
                current["createdTime"] = str(item.get("createdTime") or "")
            if not current.get("modifiedTime") and item.get("modifiedTime"):
                current["modifiedTime"] = str(item.get("modifiedTime") or "")
            if not current.get("owners"):
                normalized = normalize_search_result(item, matched_by=matched_by)
                current["owners"] = normalized["owners"]
                current["ownerDisplayName"] = normalized["ownerDisplayName"]

    def sort_key(item: dict[str, object]) -> tuple[int, str, str]:
        matched_by = set(item.get("matchedBy") or [])
        if matched_by == {"title", "content"}:
            rank = 2
        elif "title" in matched_by:
            rank = 1
        else:
            rank = 0
        return (
            rank,
            str(item.get("modifiedTime") or ""),
            str(item.get("title") or "").lower(),
        )

    return sorted(merged.values(), key=sort_key, reverse=True)[:limit]


def search_documents(
    access_token: str,
    query: str,
    *,
    limit: int,
    mode: str,
) -> dict[str, object]:
    escaped = escape_drive_query(query)
    title_files: list[dict[str, object]] = []
    content_files: list[dict[str, object]] = []
    if mode in {"title", "auto"}:
        title_files = drive_search_files(
            access_token,
            (
                f"mimeType='{GOOGLE_DOC_MIME}' and trashed=false and "
                f"name contains '{escaped}'"
            ),
            limit=limit,
            fields="id,name,createdTime,modifiedTime,webViewLink,owners(displayName)",
        )
    if mode in {"content", "auto"}:
        content_files = drive_search_files(
            access_token,
            (
                f"mimeType='{GOOGLE_DOC_MIME}' and trashed=false and "
                f"fullText contains '{escaped}'"
            ),
            limit=limit,
            fields="id,name,createdTime,modifiedTime,webViewLink,owners(displayName)",
        )
    results = merge_search_results(title_files, content_files, limit=limit)
    return {
        "query": query,
        "mode": mode,
        "source": "gdocs",
        "resultCount": len(results),
        "results": results,
    }


def cmd_auth(args: argparse.Namespace) -> int:
    if args.full:
        oauth_state = run_oauth_bootstrap(interactive_ok=is_interactive())
    else:
        oauth_state = ensure_google_session(
            allow_bootstrap=True,
            interactive_ok=is_interactive(),
            auth_command="gdocs",
        )
    expires_at = oauth_state.get("expires_at")
    print(
        json.dumps(
            {
                "authenticated": True,
                "token_file": str(TOKEN_FILE),
                "oauth_dir": str(OAUTH_DIR),
                "expires_at": expires_at,
            },
            indent=2,
        )
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    payload = google_status_payload()
    payload["surface"] = "gdocs"
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    sys.stdout.write(
        "\n".join(
            [
                "surface\tgdocs",
                f"credentials_configured\t{str(bool(payload['credentialsConfigured'])).lower()}",
                f"session_status\t{payload['sessionStatus']}",
                f"expires_at\t{payload.get('expiresAt') or ''}",
                f"has_refresh_token\t{str(bool(payload.get('hasRefreshToken'))).lower()}",
                f"token_file\t{payload['tokenFile']}",
                f"next_step\t{payload.get('nextStep') or ''}",
            ]
        )
        + "\n"
    )
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    oauth_state = ensure_google_session(
        allow_bootstrap=True,
        interactive_ok=is_interactive(),
        auth_command="gdocs",
    )
    access_token = str(oauth_state.get("access_token") or "").strip()
    doc_id, _ = parse_doc_ref(args.ref)
    if args.output == "json":
        print(json.dumps(document_json(access_token, doc_id), indent=2, sort_keys=True))
        return 0
    if args.output == "meta":
        print(json.dumps(document_meta(access_token, doc_id), indent=2, sort_keys=True))
        return 0
    if args.output == "html":
        sys.stdout.buffer.write(drive_export(access_token, doc_id, "text/html"))
        return 0
    if args.output == "text":
        sys.stdout.buffer.write(drive_export(access_token, doc_id, "text/plain"))
        return 0
    try:
        markdown = drive_export(access_token, doc_id, "text/markdown")
    except GoogleError:
        try:
            markdown = html_to_markdown(drive_export(access_token, doc_id, "text/html"))
        except GoogleError:
            markdown = drive_export(access_token, doc_id, "text/plain")
    meta = document_meta(access_token, doc_id)
    header_lines = [
        f"- URL: {meta.get('url') or ''}",
        f"- Document ID: {doc_id}",
    ]
    if meta.get("createdTime"):
        header_lines.append(f"- Created: {meta.get('createdTime')}")
    if meta.get("modifiedTime"):
        header_lines.append(f"- Updated: {meta.get('modifiedTime')}")
    if meta.get("revisionId"):
        header_lines.append(f"- Revision: {meta.get('revisionId')}")
    owners = meta.get("owners")
    if isinstance(owners, list):
        owner_names = [
            str(owner.get("displayName") or "").strip()
            for owner in owners
            if isinstance(owner, dict) and str(owner.get("displayName") or "").strip()
        ]
        if owner_names:
            header_lines.append(f"- Owners: {', '.join(owner_names)}")
    prelude = "\n".join(header_lines) + "\n\n---\n\n"
    sys.stdout.buffer.write(prelude.encode("utf-8") + markdown)
    return 0


def render_search_markdown(payload: dict[str, object]) -> str:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return (
            f"### Google Docs Search: {payload.get('query') or ''}\n\n"
            f"- _Mode_: `{payload.get('mode') or 'auto'}`\n"
            "- _Matches_: 0\n\n"
            "No Google Docs matched.\n"
        )
    lines: list[str] = [
        f"### Google Docs Search: {payload.get('query') or ''}",
        "",
        "- _Surface_: `gdocs`",
        f"- _Mode_: `{payload.get('mode') or 'auto'}`",
        f"- _Matches_: {payload.get('resultCount') or len(results)}",
    ]
    lines.extend(
        render_source_metadata_lines(derive_source_metadata_from_payload(payload))
    )
    lines.append("")
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "(untitled)")
        url = str(item.get("url") or "")
        created = str(item.get("createdTime") or "")
        modified = str(item.get("modifiedTime") or "")
        owner = str(item.get("ownerDisplayName") or "")
        matched_by = [str(value) for value in item.get("matchedBy") or [] if str(value)]
        line = f"- [{title}]({url})"
        details: list[str] = []
        if matched_by:
            details.append(f"matched by `{'+'.join(matched_by)}`")
        if created:
            details.append(f"created `{created}`")
        if modified:
            details.append(f"modified `{modified}`")
        if owner:
            details.append(f"owner `{owner}`")
        if details:
            line += " - " + ", ".join(details)
        lines.append(line)
    return "\n".join(lines) + "\n"


def cmd_search(args: argparse.Namespace) -> int:
    oauth_state = ensure_google_session(
        allow_bootstrap=True,
        interactive_ok=is_interactive(),
        auth_command="gdocs",
    )
    access_token = str(oauth_state.get("access_token") or "").strip()
    payload = search_documents(
        access_token,
        args.query,
        limit=args.limit,
        mode=args.mode,
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    sys.stdout.write(render_search_markdown(payload))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta gdocs",
        description=(
            "Read-only Google Docs helper. Uses shared Google OAuth state and "
            "the Docs/Drive APIs for document-first retrieval, including "
            "accessible shared-drive documents."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "auth",
        help="ensure local Google OAuth state is usable; refresh silently when possible",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="force a full browser OAuth bootstrap instead of reusing or refreshing cached state",
    )
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser(
        "status", help="inspect local Google OAuth readiness for Docs access"
    )
    p.add_argument("--output", choices=["json", "summary"], default="summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("get", help="fetch one Google Doc by URL or document ID")
    p.add_argument("ref", help="Google Docs URL or document ID")
    p.add_argument(
        "--output",
        choices=["markdown", "text", "html", "meta", "json"],
        default="markdown",
        help="render format for get; defaults to markdown",
    )
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("search", help="search Google Docs by title or indexed content")
    p.add_argument("query", help="search query")
    p.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    p.add_argument(
        "--mode",
        choices=["auto", "title", "content"],
        default="auto",
        help="search titles, indexed body content, or both; defaults to auto",
    )
    p.add_argument("--output", choices=["markdown", "json"], default="markdown")
    p.set_defaults(func=cmd_search)

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
    except GoogleError as exc:
        return die(str(exc), code=1)


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main(sys.argv[1:]))
