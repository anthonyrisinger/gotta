"""Last-second projection helpers for canonical source bytes."""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess


def looks_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def pretty_json(data: bytes) -> bytes:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return data
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _pandoc(data: bytes, *, output: str) -> bytes | None:
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return None
    proc = subprocess.run(
        [pandoc, "-f", "html", "-t", output, "--wrap=none"],
        input=data,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "pandoc failed to project HTML")
    return proc.stdout


def html_markdown(data: bytes) -> bytes | None:
    return _pandoc(data, output="gfm")


def html_text(data: bytes) -> bytes:
    projected = _pandoc(data, output="plain")
    if projected is not None:
        return projected
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style|noscript)\b[^>]*>.*?</\1>", "", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip().encode("utf-8") + b"\n"
