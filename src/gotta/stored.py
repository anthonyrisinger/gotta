"""Canonical rendering helpers for stored artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from gotta.builtin import get_plugin
from gotta.capture import Capture
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


def _project_html(data: bytes) -> tuple[bytes, tuple[str, ...]]:
    try:
        markdown = html_markdown(data)
    except RuntimeError as exc:
        return data, (f"html projection failed: {exc}",)
    return (markdown if markdown is not None else data), ()


def _project_canonical(capture: Capture) -> tuple[bytes, tuple[str, ...]]:
    content_type = capture.type.split(";", 1)[0].strip().lower()
    if content_type == "text/html":
        return _project_html(capture.data)
    if content_type in {"application/json", "text/json"}:
        return pretty_json(capture.data), ()
    if content_type.startswith("text/") or looks_text(capture.data):
        return capture.data, ()
    lines = [
        "# Binary Content",
        "",
        f"- Content Type: `{capture.type or 'application/octet-stream'}`",
        f"- Bytes: {len(capture.data)}",
        "",
        "Use the provider-native surface or a raw file tool if you need the uninterpreted bytes.",
        "",
    ]
    return "\n".join(lines).encode("utf-8"), ()


def _display_projection(capture: Capture) -> tuple[bytes, tuple[str, ...]]:
    degradations: list[str] = []
    stored_projector = str(capture.meta.get("projector") or "").strip()
    projector_argv = [
        str(part) for part in capture.meta.get("argv") or [] if str(part).strip()
    ]
    if stored_projector:
        spec = get_plugin(stored_projector)
        if spec is None or spec.project is None:
            degradations.append(
                f"stored projector `{stored_projector}` is unavailable; using canonical projection"
            )
        else:
            try:
                return spec.project(projector_argv, capture), ()
            except RuntimeError as exc:
                degradations.append(
                    f"stored projector `{stored_projector}` failed: {exc}"
                )
    projected, projection_degradations = _project_canonical(capture)
    return projected, tuple([*degradations, *projection_degradations])


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
        name=str(
            meta.get("original_name") or meta.get("preferred_name") or path.name or ""
        ),
        type=str(meta.get("content_type") or ""),
        meta=dict(meta),
    )


def stored_display(path: Path) -> StoredDisplay:
    if not (path.parent / "meta.json").exists():
        data = path.read_bytes()
        return StoredDisplay(data=data, language=guess_lang_from_path(path.name))
    stored = _stored_capture(path)
    display, degradations = _display_projection(stored)
    language = (
        "markdown"
        if stored.type.split(";", 1)[0].strip().lower() == "text/html"
        and display != stored.data
        else guess_lang_from_content_type(stored.type)
        or guess_lang_from_path(path.name)
    )
    return StoredDisplay(data=display, language=language, degradations=degradations)
