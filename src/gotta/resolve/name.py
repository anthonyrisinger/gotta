"""Generic fallback naming and content-type derivation."""

from __future__ import annotations

from pathlib import Path

from gotta.builtin import get_plugin
from gotta.content.model import CommonOptions


def _output_extension(argv: list[str]) -> str:
    output_value = ""
    for index, token in enumerate(argv):
        if token.startswith("--output="):
            output_value = token.split("=", 1)[1]
        elif token == "--output" and index + 1 < len(argv):
            output_value = argv[index + 1]
    mapping = {
        "adf": "json",
        "body": "html",
        "csv": "csv",
        "html": "html",
        "json": "json",
        "links": "txt",
        "markdown": "md",
        "md": "md",
        "messages": "json",
        "meta": "json",
        "raw": "bin",
        "summary": "summary",
        "text": "txt",
        "titles": "txt",
    }
    return mapping.get(output_value, output_value) if output_value else "txt"


def preferred_name(plugin: str, argv: list[str], options: CommonOptions) -> str:
    spec = get_plugin(plugin)
    if spec and spec.preferred_name is not None:
        return spec.preferred_name(argv, options)
    if options.save_as:
        return options.save_as
    return f"{plugin}.{_output_extension(argv)}"


def infer_content_type(plugin: str, argv: list[str], name: str) -> str:
    spec = get_plugin(plugin)
    if spec and spec.content_type is not None:
        return spec.content_type(argv, name)
    extension = Path(name).suffix.lower()
    if extension == ".html":
        return "text/html"
    if extension == ".json":
        return "application/json"
    if extension == ".md":
        return "text/markdown"
    if extension == ".csv":
        return "text/csv"
    if extension in {".summary", ".txt"}:
        return "text/plain"
    if plugin == "read" and not argv:
        return "text/plain"
    return "application/octet-stream"
