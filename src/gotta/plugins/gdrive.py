#!/usr/bin/env python3
"""Read-only Google Drive helper for gotta."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
import zlib

from gotta.capture import Capture, capture_json_command, json_bytes
from gotta.helptext import is_long_help_request, print_long_help
from gotta.project import html_markdown, html_text, pretty_json
from gotta.routing import query_route, strip_http_url_fragment
from gotta.source import derive_source_metadata_from_payload, render_source_metadata_lines
from gotta.providers.google import (
    DEFAULT_DRIVE_FIELDS,
    GOOGLE_DOC_MIME,
    OAUTH_DIR,
    TOKEN_FILE,
    GoogleError,
    drive_download_bytes,
    drive_export,
    drive_file_meta,
    drive_search_files,
    ensure_google_session,
    escape_drive_query,
    google_status_payload,
    parse_drive_ref,
    run_oauth_bootstrap,
)


DEFAULT_SEARCH_LIMIT = 10
TEXTLIKE_MIME_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/x-python-code",
    "application/x-sh",
    "application/x-shellscript",
)
DRAWIO_MIME = "application/vnd.jgraph.mxfile"


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
        "raw": "bin",
    }.get(output, "md")


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def canonical_locator(argv: list[str]) -> str:
    args = _parse_cli(argv)
    if args.command == "get":
        file_id, _ = parse_drive_ref(args.ref)
        return f"gdrive:{file_id}"
    if args.command == "status":
        return "gdrive:status"
    return f"gdrive:{shlex.join(argv)}"


def preferred_name(argv: list[str], options: object) -> str:
    if getattr(options, "save_as", ""):
        return str(getattr(options, "save_as"))
    args = _parse_cli(argv)
    if args.command == "get":
        file_id, _ = parse_drive_ref(args.ref)
        return f"{file_id}{_canonical_suffix('application/octet-stream')}"
    if args.command == "search":
        return f"gdrive-search-{_slug(args.query, fallback='gdrive')}.json"
    if args.command == "status":
        return f"gdrive.{_output_extension(args.output)}"
    return "gdrive.txt"


def _owner_names(meta: dict[str, object]) -> list[str]:
    owners = meta.get("owners")
    if not isinstance(owners, list):
        return []
    names: list[str] = []
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        display_name = str(owner.get("displayName") or "").strip()
        if display_name:
            names.append(display_name)
    return names


def _capture_meta(file_id: str, meta: dict[str, object]) -> dict[str, object]:
    return {
        "projector": "gdrive",
        "file_id": file_id,
        "source_title": str(meta.get("name") or ""),
        "source_url": str(meta.get("webViewLink") or ""),
        "source_mime": str(meta.get("mimeType") or ""),
        "source_size": str(meta.get("size") or ""),
        "source_owners": _owner_names(meta),
        "source_created_at": str(meta.get("createdTime") or ""),
        "source_updated_at": str(meta.get("modifiedTime") or ""),
    }


def _canonical_suffix(canonical_type: str) -> str:
    normalized = canonical_type.split(";", 1)[0].strip().lower()
    if normalized == "text/html":
        return ".html"
    if normalized == "application/json":
        return ".json"
    if normalized == "text/plain":
        return ".txt"
    if normalized == "application/octet-stream":
        return ".bin"
    guessed = mimetypes.guess_extension(normalized, strict=False)
    if guessed:
        return guessed
    if normalized.startswith("text/"):
        return ".txt"
    return ".bin"


def _capture_name(file_id: str, meta: dict[str, object], canonical_type: str) -> str:
    title = str(meta.get("name") or "").strip()
    suffix = _canonical_suffix(canonical_type)
    if title:
        if "." in Path(title).name:
            return title
        return f"{title}{suffix}"
    return f"{file_id}{suffix}"


def capture(argv: list[str], _options: object) -> Capture:
    args = _parse_cli(argv)
    if args.command != "get":
        if args.command == "search":
            payload = capture_json_command(
                args,
                cmd_search,
                detail="gdrive search capture failed",
            )
            return Capture(
                data=payload,
                name=preferred_name(argv, object()),
                type="application/json",
                meta={
                    "projector": "gdrive",
                    "gdrive_kind": "search",
                },
            )
        raise NotImplementedError("gdrive capture does not support this command")
    oauth_state = ensure_google_session(
        allow_bootstrap=True,
        interactive_ok=is_interactive(),
        auth_command="gdrive",
    )
    access_token = str(oauth_state.get("access_token") or "").strip()
    file_id, canonical_url = parse_drive_ref(args.ref)
    meta = drive_file_meta(access_token, file_id, fields=DEFAULT_DRIVE_FIELDS)
    meta.setdefault("webViewLink", canonical_url)
    mime_type = str(meta.get("mimeType") or "")
    if mime_type == GOOGLE_DOC_MIME:
        data = export_document(access_token, file_id, "html")
        canonical_type = "text/html"
    else:
        data = drive_download_bytes(access_token, file_id)
        canonical_type = mime_type or "application/octet-stream"
    return Capture(
        data=data,
        name=_capture_name(file_id, meta, canonical_type),
        type=canonical_type,
        meta=_capture_meta(file_id, meta),
        view={"meta": meta},
    )


def project(argv: list[str], capture: Capture) -> bytes:
    kind = str(capture.meta.get("gdrive_kind") or "get").strip()
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
    meta = capture.view.get("meta")
    if not isinstance(meta, dict):
        meta = {
            "id": capture.meta.get("file_id") or "",
            "name": capture.meta.get("source_title") or "",
            "webViewLink": capture.meta.get("source_url") or "",
            "mimeType": capture.meta.get("source_mime") or "",
            "size": capture.meta.get("source_size") or "",
            "createdTime": capture.meta.get("source_created_at") or "",
            "modifiedTime": capture.meta.get("source_updated_at") or "",
            "owners": [
                {"displayName": item}
                for item in capture.meta.get("source_owners") or []
                if isinstance(item, str)
            ],
        }
    source_mime = str(capture.meta.get("source_mime") or meta.get("mimeType") or "")
    canonical_type = capture.type.split(";", 1)[0].strip().lower()
    if not argv:
        if source_mime == GOOGLE_DOC_MIME or canonical_type == "text/html":
            rendered = html_markdown(capture.data)
            return rendered if rendered is not None else html_text(capture.data)
        if source_mime == DRAWIO_MIME:
            return drawio_summary(capture.data, meta).encode("utf-8")
        if is_textlike_mime_type(source_mime) or canonical_type.startswith("text/"):
            return capture.data
        return drive_file_summary(meta).encode("utf-8")
    args = _parse_cli(argv)
    if args.command != "get":
        return capture.data
    if args.output in {"json", "meta"}:
        return json_bytes(meta)
    if args.output == "raw":
        if source_mime == GOOGLE_DOC_MIME:
            raise RuntimeError(
                "raw download is not available for native Google Docs; use "
                "--output markdown, text, html, meta, or json"
            )
        return capture.data
    if source_mime == GOOGLE_DOC_MIME:
        if args.output == "html":
            return capture.data
        if args.output == "text":
            return html_text(capture.data)
        rendered = html_markdown(capture.data)
        return rendered if rendered is not None else html_text(capture.data)
    if source_mime == DRAWIO_MIME:
        if args.output == "raw":
            return capture.data
        if args.output == "html":
            raise RuntimeError(
                "draw.io mxfile content is not exportable as html; "
                "use --output markdown, text, raw, meta, or json"
            )
        return drawio_summary(capture.data, meta).encode("utf-8")
    if args.output == "html":
        if canonical_type == "text/html":
            return capture.data
        raise RuntimeError(
            f"{source_mime or 'this file'} is not exportable as html; "
            "use --output raw, meta, json, or markdown"
        )
    if args.output in {"markdown", "text"} and (
        is_textlike_mime_type(source_mime) or canonical_type.startswith("text/")
    ):
        if args.output == "markdown" and canonical_type == "text/html":
            rendered = html_markdown(capture.data)
            return rendered if rendered is not None else html_text(capture.data)
        return capture.data
    if args.output == "text":
        raise RuntimeError(
            f"{source_mime or 'this file'} is not directly readable as text; "
            "use --output raw, meta, json, or markdown"
        )
    return drive_file_summary(meta).encode("utf-8")


def route_target(target: str) -> list[str] | None:
    if target.startswith("gdrive:search "):
        return query_route(
            "search",
            target.removeprefix("gdrive:search "),
            valued_flags=("--limit", "--mode", "--mime-type", "--output"),
        )
    if target.startswith("gdrive:"):
        return ["get", target.removeprefix("gdrive:")]
    if any(char.isspace() for char in target):
        return None
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc == "drive.google.com":
        return ["get", strip_http_url_fragment(target)]
    if parsed.netloc == "docs.google.com":
        parts = [part for part in parsed.path.split("/") if part]
        if "d" in parts and not (parts and parts[0] in {"document", "spreadsheets"}):
            return ["get", strip_http_url_fragment(target)]
    return None


def html_to_markdown(data: bytes) -> bytes:
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise GoogleError("pandoc is required for markdown conversion but is not installed")
    proc = subprocess.run(
        [pandoc, "-f", "html", "-t", "gfm", "--wrap=none"],
        input=data,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GoogleError(detail or "pandoc failed to convert Google Drive HTML")
    return proc.stdout


def drive_file_summary(meta: dict[str, object]) -> str:
    file_id = str(meta.get("id") or "")
    title = str(meta.get("name") or "(untitled)")
    url = str(meta.get("webViewLink") or "")
    mime_type = str(meta.get("mimeType") or "")
    created = str(meta.get("createdTime") or "")
    modified = str(meta.get("modifiedTime") or "")
    size = str(meta.get("size") or "")
    owners = meta.get("owners")
    owner_names: list[str] = []
    if isinstance(owners, list):
        for owner in owners:
            if isinstance(owner, dict):
                name = str(owner.get("displayName") or "").strip()
                if name:
                    owner_names.append(name)
    lines = [f"# {title}", ""]
    if url:
        lines.append(f"- **URL:** {url}")
    if file_id:
        lines.append(f"- **Drive ID:** `{file_id}`")
    if mime_type:
        lines.append(f"- **MIME type:** `{mime_type}`")
    if created:
        lines.append(f"- **Created:** {created}")
    if modified:
        lines.append(f"- **Modified:** {modified}")
    if size:
        lines.append(f"- **Size:** {size} bytes")
    if owner_names:
        lines.append(f"- **Owners:** {', '.join(f'`{owner}`' for owner in owner_names)}")
    if mime_type == GOOGLE_DOC_MIME:
        lines.extend(
            [
                "",
                "This is a native Google Doc. `--output markdown`, `text`, and `html`",
                "will export the document body through Drive.",
            ]
        )
    elif mime_type == DRAWIO_MIME:
        lines.extend(
            [
                "",
                "This is a draw.io mxfile. `--output markdown` and `text` will render",
                "a structure summary from the canonical XML. Use `--output raw` to",
                "download the original mxfile bytes.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Readable output will render text-like files directly when possible.",
                "Use `--output raw` to download file bytes for non-text files.",
            ]
        )
    return "\n".join(lines) + "\n"


def is_textlike_mime_type(mime_type: str) -> bool:
    normalized = mime_type.strip().lower()
    return any(
        normalized == prefix or normalized.startswith(prefix)
        for prefix in TEXTLIKE_MIME_PREFIXES
    )


def _mxfile_xml(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    stripped = text.lstrip()
    if stripped.startswith("<mxfile"):
        return stripped
    try:
        decoded = base64.b64decode(data, validate=True)
    except Exception:
        return ""
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    stripped = text.lstrip()
    return stripped if stripped.startswith("<mxfile") else ""


def _mxgraph_root(diagram: ET.Element) -> ET.Element | None:
    direct_model = diagram.find("mxGraphModel")
    if direct_model is not None:
        root = direct_model.find("root")
        if root is not None:
            return root
    raw = str(diagram.text or "").strip()
    if not raw:
        return None
    try:
        compressed = base64.b64decode(raw)
    except Exception:
        return None
    for wbits in (-15, zlib.MAX_WBITS):
        try:
            expanded = zlib.decompress(compressed, wbits)
        except zlib.error:
            continue
        try:
            xml = urllib.parse.unquote(expanded.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        try:
            model = ET.fromstring(xml)
        except ET.ParseError:
            continue
        if model.tag == "mxGraphModel":
            root = model.find("root")
            if root is not None:
                return root
    return None


def _mx_value_label(value: str) -> str:
    # draw.io commonly stores HTML in values; normalize down to human-readable text.
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def drawio_summary(data: bytes, meta: dict[str, object]) -> str:
    title = str(meta.get("name") or "(untitled)")
    file_id = str(meta.get("id") or "")
    url = str(meta.get("webViewLink") or "")
    xml = _mxfile_xml(data)
    lines = [f"# {title}", ""]
    if url:
        lines.append(f"- **URL:** {url}")
    if file_id:
        lines.append(f"- **Drive ID:** `{file_id}`")
    lines.append(f"- **MIME type:** `{DRAWIO_MIME}`")
    if not xml:
        lines.extend(
            [
                "",
                "This draw.io mxfile is stored canonically, but the native summary could",
                "not decode a readable XML projection. Use `--output raw` if you need",
                "the original bytes outside gotta.",
            ]
        )
        return "\n".join(lines) + "\n"

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        lines.extend(
            [
                "",
                "This draw.io mxfile is stored canonically, but the XML could not be",
                "parsed into a readable structure summary. Use `--output raw` if you",
                "need the original bytes outside gotta.",
            ]
        )
        return "\n".join(lines) + "\n"

    diagrams = root.findall("diagram")
    lines.append(f"- **Pages:** {len(diagrams)}")
    page_summaries: list[str] = []
    for diagram in diagrams[:5]:
        page_name = str(diagram.get("name") or "(unnamed)").strip()
        graph = _mxgraph_root(diagram)
        cells = graph.findall("mxCell") if graph is not None else []
        vertex_count = 0
        edge_count = 0
        labels: list[str] = []
        for cell in cells:
            if cell.get("vertex") == "1":
                vertex_count += 1
                label = _mx_value_label(str(cell.get("value") or ""))
                if label and label not in labels:
                    labels.append(label)
            if cell.get("edge") == "1":
                edge_count += 1
        preview = ", ".join(labels[:4])
        page_summary = f"{page_name}: {vertex_count} nodes, {edge_count} edges"
        if preview:
            page_summary += f"; labels: {preview}"
        page_summaries.append(page_summary)
    if page_summaries:
        lines.extend(["", "## Structure", ""])
        for summary in page_summaries:
            lines.append(f"- {summary}")
    if len(diagrams) > 5:
        lines.append(f"- ... {len(diagrams) - 5} more page(s)")
    lines.extend(
        [
            "",
            "Use `--output raw` to download the original mxfile bytes.",
        ]
    )
    return "\n".join(lines) + "\n"


def normalize_search_result(item: dict[str, object], *, matched_by: set[str]) -> dict[str, object]:
    owners = item.get("owners")
    owner_names: list[str] = []
    if isinstance(owners, list):
        for owner in owners:
            if isinstance(owner, dict):
                display_name = str(owner.get("displayName") or "").strip()
                if display_name:
                    owner_names.append(display_name)
    return {
        "fileId": str(item.get("id") or ""),
        "title": str(item.get("name") or "(untitled)"),
        "url": str(item.get("webViewLink") or ""),
        "createdTime": str(item.get("createdTime") or ""),
        "modifiedTime": str(item.get("modifiedTime") or ""),
        "mimeType": str(item.get("mimeType") or ""),
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
            file_id = str(item.get("id") or "").strip()
            if not file_id:
                continue
            current = merged.get(file_id)
            if current is None:
                merged[file_id] = normalize_search_result(item, matched_by={source})
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
            if not current.get("mimeType") and item.get("mimeType"):
                current["mimeType"] = str(item.get("mimeType") or "")
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


def search_files(
    access_token: str,
    query: str,
    *,
    limit: int,
    mode: str,
    mime_type: str,
) -> dict[str, object]:
    escaped = escape_drive_query(query)
    mime_clause = f" and mimeType='{escape_drive_query(mime_type)}'" if mime_type else ""
    fields = "id,name,mimeType,createdTime,modifiedTime,webViewLink,owners(displayName)"
    title_files: list[dict[str, object]] = []
    content_files: list[dict[str, object]] = []
    if mode in {"title", "auto"}:
        title_files = drive_search_files(
            access_token,
            f"trashed=false{mime_clause} and name contains '{escaped}'",
            limit=limit,
            fields=fields,
        )
    if mode in {"content", "auto"}:
        content_files = drive_search_files(
            access_token,
            f"trashed=false{mime_clause} and fullText contains '{escaped}'",
            limit=limit,
            fields=fields,
        )
    results = merge_search_results(title_files, content_files, limit=limit)
    return {
        "query": query,
        "mode": mode,
        "mimeType": mime_type,
        "source": "google-drive",
        "resultCount": len(results),
        "results": results,
    }


def export_document(access_token: str, file_id: str, output: str) -> bytes:
    if output == "html":
        return drive_export(access_token, file_id, "text/html")
    if output == "text":
        return drive_export(access_token, file_id, "text/plain")
    try:
        return drive_export(access_token, file_id, "text/markdown")
    except GoogleError:
        try:
            return html_to_markdown(drive_export(access_token, file_id, "text/html"))
        except GoogleError:
            return drive_export(access_token, file_id, "text/plain")


def cmd_auth(args: argparse.Namespace) -> int:
    if args.full:
        oauth_state = run_oauth_bootstrap(interactive_ok=is_interactive())
    else:
        oauth_state = ensure_google_session(
            allow_bootstrap=True,
            interactive_ok=is_interactive(),
            auth_command="gdrive",
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
    payload["surface"] = "gdrive"
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    sys.stdout.write(
        "\n".join(
            [
                "surface\tgdrive",
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
        auth_command="gdrive",
    )
    access_token = str(oauth_state.get("access_token") or "").strip()
    file_id, canonical_url = parse_drive_ref(args.ref)
    meta = drive_file_meta(access_token, file_id, fields=DEFAULT_DRIVE_FIELDS)
    meta.setdefault("webViewLink", canonical_url)
    mime_type = str(meta.get("mimeType") or "")
    if args.output == "json":
        print(json.dumps(meta, indent=2, sort_keys=True))
        return 0
    if args.output == "meta":
        print(json.dumps(meta, indent=2, sort_keys=True))
        return 0
    if args.output == "raw":
        if mime_type == GOOGLE_DOC_MIME:
            raise GoogleError(
                "raw download is not available for native Google Docs; use "
                "--output markdown, text, html, meta, or json"
            )
        sys.stdout.buffer.write(drive_download_bytes(access_token, file_id))
        return 0
    if mime_type == GOOGLE_DOC_MIME:
        sys.stdout.buffer.write(export_document(access_token, file_id, args.output))
        return 0
    if args.output == "html":
        if mime_type == "text/html":
            sys.stdout.buffer.write(drive_download_bytes(access_token, file_id))
            return 0
        raise GoogleError(
            f"{mime_type or 'this file'} is not exportable as html; "
            "use --output raw, meta, json, or markdown"
        )
    if args.output in {"markdown", "text"} and is_textlike_mime_type(mime_type):
        data = drive_download_bytes(access_token, file_id)
        if args.output == "markdown" and mime_type == "text/html":
            try:
                data = html_to_markdown(data)
            except GoogleError:
                pass
        sys.stdout.buffer.write(data)
        return 0
    if args.output == "text":
        raise GoogleError(
            f"{mime_type or 'this file'} is not directly readable as text; "
            "use --output raw, meta, json, or markdown"
        )
    sys.stdout.write(drive_file_summary(meta))
    return 0


def render_search_markdown(payload: dict[str, object]) -> str:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return (
            f"### Google Drive Search: {payload.get('query') or ''}\n\n"
            f"- _Mode_: `{payload.get('mode') or 'auto'}`\n"
            "- _Matches_: 0\n\n"
            "No Google Drive files matched.\n"
        )
    lines: list[str] = [
        f"### Google Drive Search: {payload.get('query') or ''}",
        "",
        f"- _Mode_: `{payload.get('mode') or 'auto'}`",
        f"- _Matches_: {payload.get('resultCount') or len(results)}",
    ]
    lines.extend(render_source_metadata_lines(derive_source_metadata_from_payload(payload)))
    lines.append("")
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "(untitled)")
        url = str(item.get("url") or "")
        created = str(item.get("createdTime") or "")
        modified = str(item.get("modifiedTime") or "")
        owner = str(item.get("ownerDisplayName") or "")
        mime_type = str(item.get("mimeType") or "")
        matched_by = [str(value) for value in item.get("matchedBy") or [] if str(value)]
        line = f"- [{title}]({url})"
        details: list[str] = []
        if matched_by:
            details.append(f"matched by `{'+'.join(matched_by)}`")
        if mime_type:
            details.append(f"mime `{mime_type}`")
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
        auth_command="gdrive",
    )
    access_token = str(oauth_state.get("access_token") or "").strip()
    payload = search_files(
        access_token,
        args.query,
        limit=args.limit,
        mode=args.mode,
        mime_type=args.mime_type,
    )
    if args.output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    sys.stdout.write(render_search_markdown(payload))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gotta gdrive",
        description=(
            "Read-only Google Drive helper. Uses shared Google OAuth state and "
            "the Drive API for file metadata, document export, and binary download."
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

    p = sub.add_parser("status", help="inspect local Google OAuth readiness for Drive access")
    p.add_argument("--output", choices=["json", "summary"], default="summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("get", help="fetch one Google Drive file by URL or file ID")
    p.add_argument("ref", help="Google Drive URL or file ID")
    p.add_argument(
        "--output",
        choices=["markdown", "text", "html", "meta", "json", "raw"],
        default="markdown",
        help="render format for get; defaults to markdown",
    )
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("search", help="search Google Drive by title or indexed content")
    p.add_argument("query", help="search query")
    p.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    p.add_argument(
        "--mode",
        choices=["auto", "title", "content"],
        default="auto",
        help="search titles, indexed body content, or both; defaults to auto",
    )
    p.add_argument(
        "--mime-type",
        default="",
        help="optional Drive MIME filter, e.g. application/vnd.google-apps.document",
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
