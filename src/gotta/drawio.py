"""Shared draw.io helpers."""

from __future__ import annotations

import base64
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from typing import Any


DRAWIO_MIME = "application/vnd.jgraph.mxfile"


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
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def summarize_drawio(data: bytes) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "mimeType": DRAWIO_MIME,
        "decoded": False,
        "parsed": False,
        "pages": [],
    }
    xml = _mxfile_xml(data)
    if not xml:
        summary["error"] = "decode"
        return summary
    summary["decoded"] = True
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        summary["error"] = "parse"
        return summary
    summary["parsed"] = True
    diagrams = root.findall("diagram")
    pages: list[dict[str, Any]] = []
    for diagram in diagrams:
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
        pages.append(
            {
                "name": page_name,
                "vertexCount": vertex_count,
                "edgeCount": edge_count,
                "labels": labels,
            }
        )
    summary["pages"] = pages
    return summary


def render_drawio_summary_markdown(
    data: bytes,
    *,
    title: str,
    url: str = "",
    file_id: str = "",
    raw_hint: str = "Use `--output raw` to download the original mxfile bytes.",
) -> str:
    summary = summarize_drawio(data)
    lines = [f"# {title}", ""]
    if url:
        lines.append(f"- **URL:** {url}")
    if file_id:
        lines.append(f"- **Drive ID:** `{file_id}`")
    lines.append(f"- **MIME type:** `{DRAWIO_MIME}`")
    if not summary.get("decoded"):
        lines.extend(
            [
                "",
                "This draw.io mxfile is stored canonically, but the native summary could",
                "not decode a readable XML projection.",
                raw_hint,
            ]
        )
        return "\n".join(lines) + "\n"
    if not summary.get("parsed"):
        lines.extend(
            [
                "",
                "This draw.io mxfile is stored canonically, but the XML could not be",
                "parsed into a readable structure summary.",
                raw_hint,
            ]
        )
        return "\n".join(lines) + "\n"

    pages = summary.get("pages")
    if not isinstance(pages, list):
        pages = []
    lines.append(f"- **Pages:** {len(pages)}")
    if pages:
        lines.extend(["", "## Structure", ""])
        for page in pages[:5]:
            if not isinstance(page, dict):
                continue
            page_name = str(page.get("name") or "(unnamed)")
            vertex_count = int(page.get("vertexCount") or 0)
            edge_count = int(page.get("edgeCount") or 0)
            labels = page.get("labels")
            preview = ""
            if isinstance(labels, list):
                preview = ", ".join(
                    str(label) for label in labels[:4] if str(label).strip()
                )
            line = f"- {page_name}: {vertex_count} nodes, {edge_count} edges"
            if preview:
                line += f"; labels: {preview}"
            lines.append(line)
        if len(pages) > 5:
            lines.append(f"- ... {len(pages) - 5} more page(s)")
    lines.extend(["", raw_hint])
    return "\n".join(lines) + "\n"
