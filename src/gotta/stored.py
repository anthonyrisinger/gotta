"""Canonical rendering helpers for stored artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from gotta.builtin import get_surface
from gotta.capture import Capture
from gotta.projection import Projection, projection_bytes, projection_for_capture
from gotta.project import html_markdown, looks_text, pretty_json


@dataclass(frozen=True, slots=True)
class StoredDisplay:
    data: bytes
    language: str
    degradations: tuple[str, ...] = ()


def guess_lang_from_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".html": "html",
        ".htm": "html",
        ".md": "markdown",
        ".markdown": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".xml": "xml",
        ".css": "css",
        ".js": "javascript",
        ".toml": "toml",
        ".sh": "bash",
        ".py": "python",
        ".go": "go",
        ".tf": "hcl",
        ".tfvars": "hcl",
    }.get(suffix, "txt")


def guess_lang_from_content_type(content_type: str) -> str:
    kind = content_type.split(";", 1)[0].strip().lower()
    return {
        "text/html": "html",
        "text/markdown": "markdown",
        "application/json": "json",
        "text/json": "json",
        "application/yaml": "yaml",
        "application/x-yaml": "yaml",
        "text/yaml": "yaml",
        "text/x-yaml": "yaml",
        "application/xml": "xml",
        "text/xml": "xml",
        "text/css": "css",
        "application/javascript": "javascript",
        "text/javascript": "javascript",
    }.get(kind, "txt" if kind.startswith("text/") else "")


def _project_html(data: bytes, *, fallback_content_type: str) -> Projection:
    try:
        markdown = html_markdown(data)
    except RuntimeError as exc:
        return projection_bytes(
            data,
            content_type=fallback_content_type,
            degradations=(f"html projection failed: {exc}",),
        )
    if markdown is None:
        return projection_bytes(data, content_type=fallback_content_type)
    return projection_bytes(markdown, content_type="text/markdown")


def _project_canonical(capture: Capture) -> Projection:
    content_type = capture.content_type.split(";", 1)[0].strip().lower()
    if content_type == "text/html":
        return _project_html(capture.data, fallback_content_type=capture.content_type)
    if content_type in {"application/json", "text/json"}:
        return projection_bytes(
            pretty_json(capture.data), content_type="application/json"
        )
    if content_type.startswith("text/") or looks_text(capture.data):
        return projection_for_capture(capture, capture.data)
    lines = [
        "# Binary Content",
        "",
        f"- Content Type: `{capture.content_type or 'application/octet-stream'}`",
        f"- Bytes: {len(capture.data)}",
        "",
        "Use the provider-native surface or a raw file tool if you need the uninterpreted bytes.",
        "",
    ]
    return projection_bytes(
        "\n".join(lines).encode("utf-8"),
        content_type="text/markdown",
    )


def _display_projection(capture: Capture) -> Projection:
    degradations: list[str] = []
    stored_projector = str(capture.metadata.get("projector") or "").strip()
    projector_argv = [
        str(part) for part in capture.metadata.get("argv") or [] if str(part).strip()
    ]
    if stored_projector:
        surface = get_surface(stored_projector)
        if surface is None or surface.project is None:
            degradations.append(
                f"stored projector `{stored_projector}` is unavailable; using canonical projection"
            )
        else:
            try:
                projection = surface.project(projector_argv, capture)
                if not degradations:
                    return projection
                return Projection(
                    data=projection.data,
                    content_type=projection.content_type,
                    degradations=tuple([*degradations, *projection.degradations]),
                )
            except RuntimeError as exc:
                degradations.append(
                    f"stored projector `{stored_projector}` failed: {exc}"
                )
    projection = _project_canonical(capture)
    if not degradations:
        return projection
    return Projection(
        data=projection.data,
        content_type=projection.content_type,
        degradations=tuple([*degradations, *projection.degradations]),
    )


def _stored_capture(path: Path) -> Capture:
    meta_path = path.parent / "meta.json"
    meta: dict[str, object] = {}
    if meta_path.exists():
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            meta = payload
    return Capture(
        data=path.read_bytes(),
        preferred_name=str(
            meta.get("original_name") or meta.get("preferred_name") or path.name or ""
        ),
        content_type=str(meta.get("content_type") or ""),
        metadata=dict(meta),
    )


def stored_display(path: Path) -> StoredDisplay:
    if not (path.parent / "meta.json").exists():
        data = path.read_bytes()
        return StoredDisplay(data=data, language=guess_lang_from_path(path.name))
    stored = _stored_capture(path)
    projection = _display_projection(stored)
    language = (
        "markdown"
        if stored.content_type.split(";", 1)[0].strip().lower() == "text/html"
        and projection.data != stored.data
        else guess_lang_from_content_type(stored.content_type)
        or guess_lang_from_path(path.name)
    )
    return StoredDisplay(
        data=projection.data,
        language=language,
        degradations=projection.degradations,
    )
