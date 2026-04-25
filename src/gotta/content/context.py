from __future__ import annotations

import array
import hashlib
import io
import os
import select
import sys
from pathlib import Path

try:
    import fcntl
    import termios
except ImportError:  # pragma: no cover - platform-specific fallback
    fcntl = None
    termios = None

from gotta.actors import resolve_actor_context
from gotta.content.env import (
    ACTOR_ID_ENV,
    CONTEXT_ID_ENV,
    CONTEXT_SOURCE_ENV,
    SESSION_ACTOR_ENV,
)
from gotta.content.model import ContextBinding
from gotta import topology


_SANDBOX_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def _sandbox_boot_id() -> str:
    if os.environ.get("IS_SANDBOX", "").strip().lower() != "yes":
        return ""
    try:
        return _SANDBOX_BOOT_ID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def current_context_binding() -> ContextBinding:
    explicit = os.environ.get(CONTEXT_ID_ENV, "").strip()
    if explicit:
        source = os.environ.get(CONTEXT_SOURCE_ENV, "").strip() or "explicit"
        return ContextBinding(
            context_id=explicit,
            context_source=source,
            binding_id=session_token(explicit),
        )
    codex_thread = os.environ.get("CODEX_THREAD_ID", "").strip()
    if codex_thread:
        return ContextBinding(
            context_id=codex_thread,
            context_source="codex_thread",
            binding_id=session_token(codex_thread),
        )
    term_session = os.environ.get("TERM_SESSION_ID", "").strip()
    if term_session:
        return ContextBinding(
            context_id=term_session,
            context_source="terminal_session",
            binding_id=session_token(term_session),
        )
    sandbox_boot_id = _sandbox_boot_id()
    if sandbox_boot_id:
        return ContextBinding(
            context_id=sandbox_boot_id,
            context_source="sandbox_boot_id",
            binding_id=session_token(sandbox_boot_id),
        )
    values = [
        os.environ.get("TTY", "").strip(),
        os.environ.get("SHELL", "").strip(),
        os.environ.get("TERM_PROGRAM", "").strip(),
        os.environ.get("TERM", "").strip(),
        os.environ.get("COPILOT_LOADER_PID", "").strip(),
        os.environ.get("COPILOT_CLI_BINARY_VERSION", "").strip(),
    ]
    if not values[0]:
        values.append(os.getcwd())
    payload = "\n".join(values)
    context_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return ContextBinding(
        context_id=context_id,
        context_source="terminal_fingerprint",
        binding_id=session_token(context_id),
    )


def session_token(context_id: str) -> str:
    return hashlib.sha256(context_id.encode("utf-8")).hexdigest()[:12]


def default_session_id(context_id: str) -> str:
    return session_token(context_id)


def current_actor(*, default_actor: str = "") -> str:
    fallback = (
        os.environ.get(ACTOR_ID_ENV, "").strip()
        or default_actor.strip()
        or os.environ.get(SESSION_ACTOR_ENV, "").strip()
        or current_context_binding().binding_id
    )
    speaker = resolve_actor_context(default_speaker=fallback).speaker
    normalized = topology.normalize_identity(str(speaker or fallback).strip())
    if normalized and not topology.is_placeholder_identity(normalized):
        return normalized
    fallback_normalized = topology.normalize_identity(fallback)
    if fallback_normalized and not topology.is_placeholder_identity(
        fallback_normalized
    ):
        return fallback_normalized
    return current_context_binding().binding_id


def stdin_has_meaningful_text() -> bool:
    stream = sys.stdin
    try:
        if stream.isatty():
            return False
    except Exception:
        return False
    getvalue = getattr(stream, "getvalue", None)
    if callable(getvalue):
        try:
            value = getvalue()
            if not isinstance(value, str):
                return bool(value)
            tell = getattr(stream, "tell", None)
            cursor_value = tell() if callable(tell) else 0
            cursor = cursor_value if isinstance(cursor_value, int) else 0
            return len(value) > cursor
        except Exception:
            return False
    try:
        fileno = stream.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        return False
    try:
        readable, _, _ = select.select([fileno], [], [], 0)
    except (OSError, ValueError):
        return False
    if not readable:
        return False
    buffer = getattr(stream, "buffer", None)
    if buffer is not None and hasattr(buffer, "peek"):
        try:
            return bool(buffer.peek(1))
        except Exception:
            pass
    if fcntl is not None and termios is not None:
        try:
            available = array.array("i", [0])
            fcntl.ioctl(fileno, termios.FIONREAD, available, True)
            return bool(available[0])
        except Exception:
            pass
    return False


def stdin_has_readable_text() -> bool:
    return stdin_has_meaningful_text()
