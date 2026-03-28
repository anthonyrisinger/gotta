"""Captured stream helpers for dispatch."""

from __future__ import annotations

from contextlib import contextmanager
import io
import sys
from typing import Any


class _CapturedBuffer(io.RawIOBase):
    def __init__(self, backing: io.BytesIO, passthrough: Any | None = None) -> None:
        self._backing = backing
        self._passthrough = passthrough

    def write(self, data: bytes | bytearray) -> int:
        payload = bytes(data)
        self._backing.write(payload)
        if self._passthrough is not None:
            self._passthrough.write(payload)
            self._passthrough.flush()
        return len(payload)

    def flush(self) -> None:
        if self._passthrough is not None:
            self._passthrough.flush()


class CapturedStream(io.TextIOBase):
    """Capture text and binary writes while optionally mirroring to the real stream."""

    def __init__(
        self,
        stream: Any,
        *,
        preserve_tty: bool = False,
        passthrough: bool = False,
    ) -> None:
        self._stream = stream
        self._encoding = getattr(stream, "encoding", None) or "utf-8"
        self._isatty = (
            bool(stream.isatty())
            if preserve_tty and hasattr(stream, "isatty")
            else False
        )
        self._buffer = io.BytesIO()
        passthrough_buffer = getattr(stream, "buffer", None) if passthrough else None
        self.buffer = _CapturedBuffer(self._buffer, passthrough_buffer)
        self._passthrough = stream if passthrough else None

    @property
    def encoding(self) -> str:
        return self._encoding

    def write(self, data: str) -> int:
        payload = data.encode(self._encoding)
        self._buffer.write(payload)
        if self._passthrough is not None:
            self._passthrough.write(data)
            self._passthrough.flush()
        return len(data)

    def flush(self) -> None:
        if self._passthrough is not None:
            self._passthrough.flush()

    def isatty(self) -> bool:
        return self._isatty

    def getvalue(self) -> bytes:
        return self._buffer.getvalue()


@contextmanager
def capture_stdout(*, preserve_tty: bool = False, passthrough: bool = False) -> Any:
    original_stdout = sys.stdout
    capture = CapturedStream(
        original_stdout,
        preserve_tty=preserve_tty,
        passthrough=passthrough,
    )
    sys.stdout = capture
    try:
        yield capture
    finally:
        sys.stdout = original_stdout


@contextmanager
def capture_stderr(*, preserve_tty: bool = False, passthrough: bool = False) -> Any:
    original_stderr = sys.stderr
    capture = CapturedStream(
        original_stderr,
        preserve_tty=preserve_tty,
        passthrough=passthrough,
    )
    sys.stderr = capture
    try:
        yield capture
    finally:
        sys.stderr = original_stderr


def _emit_captured(data: bytes) -> None:
    if not data:
        return
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        sys.stdout.flush()
        return
    sys.stdout.write(data.decode("utf-8", errors="replace"))
    sys.stdout.flush()


def _emit_captured_stderr(data: bytes) -> None:
    if not data:
        return
    buffer = getattr(sys.stderr, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        sys.stderr.flush()
        return
    sys.stderr.write(data.decode("utf-8", errors="replace"))
    sys.stderr.flush()
