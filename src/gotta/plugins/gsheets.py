#!/usr/bin/env python3
"""Read-only Google Sheets helper for gotta."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shlex
import signal
import sys
from typing import Any
import urllib.parse

from gotta.capture import Capture, capture_json_command, json_bytes
from gotta.helptext import is_long_help_request, print_long_help
from gotta.project import pretty_json
from gotta.routing import query_route, strip_http_url_fragment
from gotta.source import (
    derive_source_metadata_from_payload,
    render_source_metadata_lines,
)
from gotta.providers.google import (
    GOOGLE_SHEET_MIME,
    OAUTH_DIR,
    TOKEN_FILE,
    GoogleError,
    drive_file_meta,
    drive_search_files,
    ensure_google_session,
    escape_drive_query,
    google_status_payload,
    parse_sheet_ref,
    run_oauth_bootstrap,
    sheets_spreadsheet_meta,
    sheets_values,
)


DEFAULT_SEARCH_LIMIT = 10
DEFAULT_PREVIEW_ROWS = 20
DEFAULT_PREVIEW_COLS = 12


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
        "meta": "json",
        "json": "json",
        "csv": "csv",
        "summary": "summary",
        "text": "txt",
    }.get(output, "md")


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def canonical_locator(argv: list[str]) -> str:
    args = _parse_cli(argv)
    if args.command == "get":
        spreadsheet_id, _ = parse_sheet_ref(args.ref)
        return f"gsheets:{spreadsheet_id}"
    if args.command == "status":
        return "gsheets:status"
    return f"gsheets:{shlex.join(argv)}"


def preferred_name(argv: list[str], options: object) -> str:
    if getattr(options, "save_as", ""):
        return str(getattr(options, "save_as"))
    args = _parse_cli(argv)
    if args.command == "get":
        spreadsheet_id, _ = parse_sheet_ref(args.ref)
        return f"{spreadsheet_id}.json"
    if args.command == "search":
        return f"gsheets-search-{_slug(args.query, fallback='gsheets')}.json"
    if args.command == "status":
        return f"gsheets.{_output_extension(args.output)}"
    return "gsheets.txt"


def route_target(target: str) -> list[str] | None:
    if target.startswith("gsheets:search "):
        return query_route(
            "search",
            target.removeprefix("gsheets:search "),
            valued_flags=("--limit", "--mode", "--output"),
        )
    if target.startswith("gsheets:"):
        return ["get", target.removeprefix("gsheets:")]
    if any(char.isspace() for char in target):
        return None
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != "docs.google.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] == "spreadsheets" and "d" in parts:
        return ["get", strip_http_url_fragment(target)]
    return None


def _quote_sheet_title(title: str) -> str:
    escaped = title.replace("'", "''")
    return f"'{escaped}'"


def _column_letters(count: int) -> str:
    if count <= 0:
        return "A"
    value = count
    letters = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _sheet_titles(meta: dict[str, Any]) -> list[str]:
    titles: list[str] = []
    sheets = meta.get("sheets")
    if not isinstance(sheets, list):
        return titles
    for item in sheets:
        if not isinstance(item, dict):
            continue
        properties = item.get("properties")
        if not isinstance(properties, dict):
            continue
        title = str(properties.get("title") or "").strip()
        if title:
            titles.append(title)
    return titles


def _find_sheet(meta: dict[str, Any], title: str) -> dict[str, Any] | None:
    sheets = meta.get("sheets")
    if not isinstance(sheets, list):
        return None
    for item in sheets:
        if not isinstance(item, dict):
            continue
        properties = item.get("properties")
        if not isinstance(properties, dict):
            continue
        if str(properties.get("title") or "").strip() == title:
            return properties
    return None


def _sheet_range(title: str, rows: int, cols: int) -> str:
    return f"{_quote_sheet_title(title)}!A1:{_column_letters(cols)}{rows}"


def _normalize_values(values: list[list[str]], *, width: int) -> list[list[str]]:
    normalized: list[list[str]] = []
    for row in values:
        trimmed = [str(value) for value in row[:width]]
        if len(trimmed) < width:
            trimmed.extend("" for _ in range(width - len(trimmed)))
        normalized.append(trimmed)
    return normalized


def _markdown_table(values: list[list[str]]) -> str:
    if not values:
        return "_No populated cells in this range._"
    width = max(len(row) for row in values) if values else 0
    width = max(width, 1)
    normalized = _normalize_values(values, width=width)
    header = normalized[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _csv_text(values: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in values:
        writer.writerow(row)
    return buffer.getvalue()


def spreadsheet_summary(meta: dict[str, Any]) -> str:
    spreadsheet_id = str(meta.get("spreadsheetId") or "")
    properties = meta.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    title = str(properties.get("title") or "(untitled spreadsheet)")
    url = str(meta.get("url") or "")
    lines = [f"# {title}", ""]
    if url:
        lines.append(f"- **URL:** {url}")
    if spreadsheet_id:
        lines.append(f"- **Spreadsheet ID:** `{spreadsheet_id}`")
    if meta.get("createdTime"):
        lines.append(f"- **Created:** {meta.get('createdTime')}")
    if meta.get("modifiedTime"):
        lines.append(f"- **Modified:** {meta.get('modifiedTime')}")
    titles = _sheet_titles(meta)
    lines.append(f"- **Sheets:** {len(titles)}")
    if titles:
        lines.append(f"- **Sheet names:** {', '.join(f'`{item}`' for item in titles)}")
    lines.extend(
        [
            "",
            "Readable output previews sheet contents. Use `--sheet` and/or `--range`",
            "to narrow the view, or `--output json|csv|meta` for structured access.",
        ]
    )
    return "\n".join(lines) + "\n"


def _capture_meta(spreadsheet_id: str, bundle: dict[str, object]) -> dict[str, object]:
    meta = bundle.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    return {
        "projector": "gsheets",
        "spreadsheet_id": spreadsheet_id,
        "source_title": str(meta.get("properties", {}).get("title") or ""),
        "source_url": str(meta.get("url") or ""),
        "source_created_at": str(meta.get("createdTime") or ""),
        "source_updated_at": str(meta.get("modifiedTime") or ""),
    }


def capture(argv: list[str], _options: object) -> Capture:
    args = _parse_cli(argv)
    if args.command != "get":
        if args.command == "search":
            payload = capture_json_command(
                args,
                cmd_search,
                detail="gsheets search capture failed",
            )
            return Capture(
                data=payload,
                name=preferred_name(argv, object()),
                type="application/json",
                meta={
                    "projector": "gsheets",
                    "gsheets_kind": "search",
                },
            )
        raise NotImplementedError("gsheets capture does not support this command")
    oauth_state = ensure_google_session(
        allow_bootstrap=True,
        interactive_ok=is_interactive(),
        auth_command="gsheets",
    )
    access_token = str(oauth_state.get("access_token") or "").strip()
    spreadsheet_id, _ = parse_sheet_ref(args.ref)
    bundle = build_preview_bundle(
        access_token,
        spreadsheet_id,
        sheet_name=args.sheet,
        a1_range=args.range,
        rows=args.rows,
        cols=args.cols,
    )
    return Capture(
        data=json_bytes(bundle),
        name=f"{spreadsheet_id}.json",
        type="application/json",
        meta=_capture_meta(spreadsheet_id, bundle),
    )


def project(argv: list[str], capture: Capture) -> bytes:
    kind = str(capture.meta.get("gsheets_kind") or "get").strip()
    if kind == "search":
        payload = json.loads(capture.data.decode("utf-8"))
        if not argv:
            return render_search_markdown(payload).encode("utf-8")
        args = _parse_cli(argv)
        if args.command != "search":
            return capture.data
        if args.output == "json":
            return pretty_json(capture.data)
        return render_search_markdown(payload).encode("utf-8")
    bundle = json.loads(capture.data.decode("utf-8"))
    meta = bundle.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    previews = bundle.get("previews")
    if not isinstance(previews, list):
        previews = []
    if not argv:
        return render_previews_markdown(meta, previews).encode("utf-8")
    args = _parse_cli(argv)
    if args.command != "get":
        return capture.data
    if args.output == "meta":
        return json_bytes(meta)
    if args.output == "json":
        return pretty_json(capture.data)
    if args.output == "csv":
        if len(previews) != 1:
            raise RuntimeError(
                "`--output csv` requires `--sheet` or `--range` to select exactly one sheet view"
            )
        values = previews[0].get("values")
        if not isinstance(values, list):
            values = []
        return _csv_text(values).encode("utf-8")
    return render_previews_markdown(meta, previews).encode("utf-8")


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
        "spreadsheetId": str(item.get("id") or ""),
        "title": str(item.get("name") or "(untitled)"),
        "url": str(item.get("webViewLink") or ""),
        "createdTime": str(item.get("createdTime") or ""),
        "modifiedTime": str(item.get("modifiedTime") or ""),
        "owners": owner_names,
        "ownerDisplayName": owner_names[0] if owner_names else "",
        "matchedBy": sorted(matched_by),
        "mimeType": str(item.get("mimeType") or ""),
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
            spreadsheet_id = str(item.get("id") or "").strip()
            if not spreadsheet_id:
                continue
            current = merged.get(spreadsheet_id)
            if current is None:
                merged[spreadsheet_id] = normalize_search_result(
                    item, matched_by={source}
                )
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


def search_spreadsheets(
    access_token: str,
    query: str,
    *,
    limit: int,
    mode: str,
) -> dict[str, object]:
    escaped = escape_drive_query(query)
    fields = "id,name,mimeType,createdTime,modifiedTime,webViewLink,owners(displayName)"
    mime_clause = f"mimeType='{GOOGLE_SHEET_MIME}' and trashed=false"
    title_files: list[dict[str, object]] = []
    content_files: list[dict[str, object]] = []
    if mode in {"title", "auto"}:
        title_files = drive_search_files(
            access_token,
            f"{mime_clause} and name contains '{escaped}'",
            limit=limit,
            fields=fields,
        )
    if mode in {"content", "auto"}:
        content_files = drive_search_files(
            access_token,
            f"{mime_clause} and fullText contains '{escaped}'",
            limit=limit,
            fields=fields,
        )
    results = merge_search_results(title_files, content_files, limit=limit)
    return {
        "query": query,
        "mode": mode,
        "source": "gsheets",
        "resultCount": len(results),
        "results": results,
    }


def render_search_markdown(payload: dict[str, object]) -> str:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return (
            f"### Google Sheets Search: {payload.get('query') or ''}\n\n"
            "- _Surface_: `gsheets`\n"
            f"- _Mode_: `{payload.get('mode') or 'auto'}`\n"
            "- _Matches_: 0\n\n"
            "No Google Sheets matched.\n"
        )
    lines: list[str] = [
        f"### Google Sheets Search: {payload.get('query') or ''}",
        "",
        "- _Surface_: `gsheets`",
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


def _preview_payload(
    access_token: str,
    spreadsheet_id: str,
    meta: dict[str, Any],
    *,
    sheet_name: str,
    rows: int,
    cols: int,
) -> dict[str, Any]:
    a1_range = _sheet_range(sheet_name, rows, cols)
    payload = sheets_values(access_token, spreadsheet_id, a1_range=a1_range)
    values = payload.get("values")
    if not isinstance(values, list):
        values = []
    string_values = [
        [str(cell) for cell in row] for row in values if isinstance(row, list)
    ]
    properties = _find_sheet(meta, sheet_name) or {}
    grid = properties.get("gridProperties")
    if not isinstance(grid, dict):
        grid = {}
    return {
        "sheetTitle": sheet_name,
        "range": str(payload.get("range") or a1_range),
        "rowCount": grid.get("rowCount"),
        "columnCount": grid.get("columnCount"),
        "values": string_values,
    }


def render_previews_markdown(
    meta: dict[str, Any], previews: list[dict[str, Any]]
) -> str:
    lines = [spreadsheet_summary(meta).rstrip(), ""]
    for preview in previews:
        title = str(preview.get("sheetTitle") or "(sheet)")
        a1_range = str(preview.get("range") or "")
        row_count = preview.get("rowCount")
        column_count = preview.get("columnCount")
        values = preview.get("values")
        if not isinstance(values, list):
            values = []
        lines.append(f"## {title}")
        lines.append("")
        details: list[str] = []
        if a1_range:
            details.append(f"preview `{a1_range}`")
        if isinstance(row_count, int) and isinstance(column_count, int):
            details.append(f"grid `{row_count} x {column_count}`")
        if details:
            lines.append("- " + ", ".join(details))
            lines.append("")
        lines.append(_markdown_table(values))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_preview_bundle(
    access_token: str,
    spreadsheet_id: str,
    *,
    sheet_name: str,
    a1_range: str | None,
    rows: int,
    cols: int,
) -> dict[str, Any]:
    meta = sheets_spreadsheet_meta(access_token, spreadsheet_id)
    drive_meta = drive_file_meta(
        access_token,
        spreadsheet_id,
        fields="id,name,createdTime,modifiedTime,webViewLink,mimeType,owners(displayName,emailAddress)",
    )
    if drive_meta.get("createdTime"):
        meta["createdTime"] = drive_meta.get("createdTime")
    if drive_meta.get("modifiedTime"):
        meta["modifiedTime"] = drive_meta.get("modifiedTime")
    if drive_meta.get("owners"):
        meta["owners"] = drive_meta.get("owners")
    if drive_meta.get("webViewLink"):
        meta["url"] = drive_meta.get("webViewLink")
    titles = _sheet_titles(meta)
    if not titles:
        raise GoogleError("spreadsheet metadata did not include any sheets")
    if sheet_name and sheet_name not in titles:
        raise GoogleError(
            f"sheet `{sheet_name}` does not exist. available sheets: {', '.join(titles)}"
        )
    selected_titles = [sheet_name] if sheet_name else titles
    previews: list[dict[str, Any]] = []
    if a1_range:
        if "!" not in a1_range:
            chosen = sheet_name or titles[0]
            a1_range = f"{_quote_sheet_title(chosen)}!{a1_range}"
        payload = sheets_values(access_token, spreadsheet_id, a1_range=a1_range)
        values = payload.get("values")
        if not isinstance(values, list):
            values = []
        previews.append(
            {
                "sheetTitle": sheet_name or titles[0],
                "range": str(payload.get("range") or a1_range),
                "values": [
                    [str(cell) for cell in row]
                    for row in values
                    if isinstance(row, list)
                ],
            }
        )
    else:
        for title in selected_titles:
            previews.append(
                _preview_payload(
                    access_token,
                    spreadsheet_id,
                    meta,
                    sheet_name=title,
                    rows=rows,
                    cols=cols,
                )
            )
    return {
        "spreadsheetId": str(meta.get("spreadsheetId") or spreadsheet_id),
        "title": str((meta.get("properties") or {}).get("title") or ""),
        "url": str(meta.get("url") or ""),
        "meta": meta,
        "previews": previews,
    }


def cmd_auth(args: argparse.Namespace) -> int:
    if args.full:
        oauth_state = run_oauth_bootstrap(interactive_ok=is_interactive())
    else:
        oauth_state = ensure_google_session(
            allow_bootstrap=True,
            interactive_ok=is_interactive(),
            auth_command="gsheets",
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
    payload["surface"] = "gsheets"
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    sys.stdout.write(
        "\n".join(
            [
                "surface\tgsheets",
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
        auth_command="gsheets",
    )
    access_token = str(oauth_state.get("access_token") or "").strip()
    spreadsheet_id, _ = parse_sheet_ref(args.ref)
    bundle = build_preview_bundle(
        access_token,
        spreadsheet_id,
        sheet_name=args.sheet,
        a1_range=args.range,
        rows=args.rows,
        cols=args.cols,
    )
    if args.output == "meta":
        print(json.dumps(bundle["meta"], indent=2, sort_keys=True))
        return 0
    if args.output == "json":
        print(json.dumps(bundle, indent=2, sort_keys=True))
        return 0
    if args.output == "csv":
        previews = bundle.get("previews")
        if not isinstance(previews, list) or len(previews) != 1:
            raise GoogleError(
                "`--output csv` requires `--sheet` or `--range` to select exactly one sheet view"
            )
        preview = previews[0]
        values = preview.get("values")
        if not isinstance(values, list):
            values = []
        sys.stdout.write(_csv_text(values))
        return 0
    previews = bundle.get("previews")
    if not isinstance(previews, list):
        previews = []
    sys.stdout.write(render_previews_markdown(bundle["meta"], previews))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    oauth_state = ensure_google_session(
        allow_bootstrap=True,
        interactive_ok=is_interactive(),
        auth_command="gsheets",
    )
    access_token = str(oauth_state.get("access_token") or "").strip()
    payload = search_spreadsheets(
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
        prog="gotta gsheets",
        description=(
            "Read-only Google Sheets helper. Uses shared Google OAuth state and "
            "the Sheets/Drive APIs for spreadsheet-first retrieval, including "
            "accessible shared-drive spreadsheets."
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
        "status", help="inspect local Google OAuth readiness for Sheets access"
    )
    p.add_argument("--output", choices=["json", "summary"], default="summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("get", help="fetch one Google Sheet by URL or spreadsheet ID")
    p.add_argument("ref", help="Google Sheets URL or spreadsheet ID")
    p.add_argument("--sheet", help="optional sheet/tab title to narrow the preview")
    p.add_argument(
        "--range", help="optional A1 range; defaults to a bounded preview per sheet"
    )
    p.add_argument(
        "--rows",
        type=int,
        default=DEFAULT_PREVIEW_ROWS,
        help="preview row count per sheet",
    )
    p.add_argument(
        "--cols",
        type=int,
        default=DEFAULT_PREVIEW_COLS,
        help="preview column count per sheet",
    )
    p.add_argument(
        "--output",
        choices=["markdown", "meta", "json", "csv"],
        default="markdown",
        help="render format for get; defaults to markdown",
    )
    p.set_defaults(func=cmd_get)

    p = sub.add_parser(
        "search", help="search Google Sheets by title or indexed content"
    )
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
