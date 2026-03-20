"""Shared dispatcher and runtime helpers for packaged gotta commands."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import io
import json
import os
import re
import sys
from typing import Any

from gotta.content import (
    CONTENT_ENV,
    ContentError,
    CommonOptions,
    Materialization,
    ResolvedDirs,
    SESSION_ENV,
    artifact_locator,
    content_locator,
    materialize_bytes,
    resolve_dirs,
    session_identity,
    session_is_initialized,
)
from gotta.builtin import (
    PluginSpec,
    available_plugins as discovered_plugin_names,
    get_plugin,
)
from gotta.invocation import (
    ResolvedInvocation,
    SUPPRESS_MATERIALIZATION_ENV as INVOCATION_SUPPRESS_MATERIALIZATION_ENV,
    canonical_locator as resolve_canonical_locator,
    infer_content_type as resolve_content_type,
    invocation_locator as resolve_invocation_locator,
    preferred_name as resolve_preferred_name,
    resolve_invocation,
    should_materialize as resolve_should_materialize,
)
from gotta.source import (
    derive_source_metadata_from_payload,
    normalize_source_timestamp,
    slack_timestamp_to_iso,
)


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


SUPPRESS_MATERIALIZATION_ENV = INVOCATION_SUPPRESS_MATERIALIZATION_ENV


def available_plugins() -> list[str]:
    return discovered_plugin_names()


def plugin_spec(plugin: str) -> PluginSpec | None:
    return get_plugin(plugin)


def print_usage() -> int:
    print("usage: gotta <plugin> [args...]", file=sys.stderr)
    print("", file=sys.stderr)
    print("canonical session-binding path: `gotta ...`", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "session investigative surfaces live under `gotta session`: "
        "`manifest`, `timeline`, `graph`, `leads`, `analyze`",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("use `gotta --help-all` for recursive command help", file=sys.stderr)
    print("", file=sys.stderr)
    print("available plugins:", file=sys.stderr)
    for plugin in available_plugins():
        spec = plugin_spec(plugin)
        description = spec.description if spec else ""
        if description:
            print(f"  - {plugin:<10} {description}", file=sys.stderr)
            continue
        print(f"  - {plugin}", file=sys.stderr)
    return 0


def load_plugin_runner(plugin: str) -> Callable[[list[str]], int]:
    spec = plugin_spec(plugin)
    if spec is None:
        raise KeyError(plugin)
    return spec.runner


def split_common_options(argv: list[str]) -> tuple[CommonOptions, list[str]]:
    session_dir: str | None = None
    content_dir: str | None = None
    save_as: str | None = None

    cleaned: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--":
            cleaned.extend(argv[index:])
            break
        if item.startswith("--session="):
            session_dir = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--session":
            if index + 1 >= len(argv):
                raise ContentError("--session requires a value")
            session_dir = argv[index + 1]
            index += 2
            continue
        if item.startswith("--content-dir="):
            content_dir = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--content-dir":
            if index + 1 >= len(argv):
                raise ContentError("--content-dir requires a value")
            content_dir = argv[index + 1]
            index += 2
            continue
        if item.startswith("--save-as="):
            save_as = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--save-as":
            if index + 1 >= len(argv):
                raise ContentError("--save-as requires a value")
            save_as = argv[index + 1]
            index += 2
            continue
        cleaned.append(item)
        index += 1

    return CommonOptions(
        session_dir=session_dir,
        content_dir=content_dir,
        save_as=save_as,
    ), cleaned


def should_materialize(plugin: str, argv: list[str]) -> bool:
    return resolve_should_materialize(plugin, argv)


def invocation_locator(plugin: str, argv: list[str]) -> str:
    return resolve_invocation_locator(plugin, argv)


def canonical_locator(plugin: str, argv: list[str]) -> str:
    return resolve_canonical_locator(plugin, argv)


def derive_preferred_name(plugin: str, argv: list[str], options: CommonOptions) -> str:
    return resolve_preferred_name(plugin, argv, options)


def infer_content_type(plugin: str, argv: list[str], name: str) -> str:
    return resolve_content_type(plugin, argv, name)


class CapturedStdout(io.TextIOBase):
    """Capture text and binary writes through the standard stdout surface."""

    def __init__(self, encoding: str) -> None:
        self._encoding = encoding
        self._buffer = io.BytesIO()
        self.buffer = self._buffer

    @property
    def encoding(self) -> str:
        return self._encoding

    def write(self, data: str) -> int:
        payload = data.encode(self._encoding)
        self._buffer.write(payload)
        return len(data)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def getvalue(self) -> bytes:
        return self._buffer.getvalue()


@contextmanager
def capture_stdout() -> Any:
    original_stdout = sys.stdout
    capture = CapturedStdout(getattr(original_stdout, "encoding", None) or "utf-8")
    sys.stdout = capture
    try:
        yield capture
    finally:
        sys.stdout = original_stdout


@contextmanager
def scoped_runtime_env(dirs: ResolvedDirs) -> Any:
    previous = {
        SESSION_ENV: os.environ.get(SESSION_ENV),
        CONTENT_ENV: os.environ.get(CONTENT_ENV),
    }
    os.environ[SESSION_ENV] = str(dirs.session_dir)
    os.environ[CONTENT_ENV] = str(dirs.content_dir)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _emit_captured(data: bytes) -> None:
    if not data:
        return
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


def _json_value(data: bytes) -> Any | None:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload


_MARKDOWN_SOURCE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^\s*-\s*(?:\*\*)?Created:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE),
        "source_created_at",
    ),
    (
        re.compile(r"^\s*-\s*(?:\*\*)?Updated:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE),
        "source_updated_at",
    ),
    (
        re.compile(r"^\s*-\s*(?:\*\*)?Modified:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE),
        "source_updated_at",
    ),
    (
        re.compile(r"^\s*-\s*(?:\*\*)?Published:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE),
        "source_published_at",
    ),
    (
        re.compile(r"^\s*-\s*(?:\*\*)?Authored:(?:\*\*)?\s*(?P<value>\S.+?)\s*$", re.MULTILINE),
        "source_created_at",
    ),
)


def _json_nested(payload: dict[str, Any], *path: str) -> str:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "").strip()


def _extract_source_metadata_from_json(payload: Any) -> dict[str, str]:
    metadata = derive_source_metadata_from_payload(payload)
    if not isinstance(payload, dict):
        return metadata
    candidates = (
        ("source_published_at", _json_nested(payload, "published_at")),
        ("source_published_at", _json_nested(payload, "created_at")),
        ("source_updated_at", _json_nested(payload, "updatedAt")),
        ("source_created_at", _json_nested(payload, "createdAt")),
        ("source_updated_at", _json_nested(payload, "updated")),
        ("source_created_at", _json_nested(payload, "created")),
        ("source_updated_at", _json_nested(payload, "modifiedTime")),
        ("source_created_at", _json_nested(payload, "createdTime")),
        ("source_created_at", _json_nested(payload, "commit", "author", "date")),
        ("source_created_at", _json_nested(payload, "author", "date")),
    )
    for key, value in candidates:
        parsed = normalize_source_timestamp(value) or str(value or "").strip()
        if parsed and key not in metadata:
            metadata[key] = parsed
    return metadata


def _extract_source_metadata_from_markdown(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    metadata: dict[str, str] = {}
    for pattern, key in _MARKDOWN_SOURCE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = match.group("value").strip()
        parsed = normalize_source_timestamp(value) or value
        if parsed and key not in metadata:
            metadata[key] = parsed
    authored_match = re.search(r"\bauthored (?P<value>\d{4}-\d{2}-\d{2}T\S+Z?)\b", text)
    if authored_match and "source_created_at" not in metadata:
        metadata["source_created_at"] = authored_match.group("value")
    return metadata


def _derived_source_metadata(plugin: str, argv: list[str], data: bytes) -> dict[str, str]:
    metadata: dict[str, str] = {}
    canonical = canonical_locator(plugin, argv)
    defaults: dict[str, str] = {}
    if canonical.startswith("slack:thread:"):
        thread_ts = canonical.rsplit(":", 1)[-1]
        source_time = slack_timestamp_to_iso(thread_ts)
        if source_time:
            defaults["source_created_at"] = source_time
            defaults["source_updated_at"] = source_time
    if plugin != "slack" or not argv or argv[0] != "get":
        payload = _json_value(data)
        if payload is not None:
            metadata.update(_extract_source_metadata_from_json(payload))
        metadata.update(
            {
                key: value
                for key, value in _extract_source_metadata_from_markdown(data).items()
                if key not in metadata
            }
        )
        for key, value in defaults.items():
            metadata.setdefault(key, value)
        return metadata
    payload = _json_value(data)
    if isinstance(payload, dict):
        first_ts = slack_timestamp_to_iso(str(payload.get("firstTs") or ""))
        last_ts = slack_timestamp_to_iso(str(payload.get("lastTs") or ""))
        if first_ts:
            metadata["source_created_at"] = first_ts
        if last_ts:
            metadata["source_updated_at"] = last_ts
    metadata.update(
        {
            key: value
            for key, value in _extract_source_metadata_from_markdown(data).items()
            if key not in metadata
        }
    )
    for key, value in defaults.items():
        metadata.setdefault(key, value)
    return metadata


def _materialize_invocation(
    resolved_or_plugin: ResolvedInvocation | str,
    argv_or_data: list[str] | bytes,
    options: CommonOptions | None = None,
    data: bytes | None = None,
    *,
    dirs: ResolvedDirs,
) -> Materialization | None:
    if isinstance(resolved_or_plugin, ResolvedInvocation):
        resolved = resolved_or_plugin
        payload = argv_or_data if isinstance(argv_or_data, bytes) else b""
    else:
        plugin = resolved_or_plugin
        argv = list(argv_or_data) if isinstance(argv_or_data, list) else []
        payload = data or b""
        resolved = resolve_invocation(plugin, argv, options or CommonOptions())
    if not payload:
        return None
    materialize_plugin = resolved.resolved_plugin
    materialize_argv = resolved.resolved_argv
    actor = os.environ.get("GOTTA_ACTOR_LABEL", "").strip() or session_identity(dirs.session_dir)
    metadata = {
        "tool": "gotta",
        "plugin": materialize_plugin,
        "subcommand": materialize_argv[0] if materialize_argv else "",
        "argv": materialize_argv,
        "locator": invocation_locator(materialize_plugin, materialize_argv),
        "canonical_locator": resolved.canonical_locator,
        "source_kind": "stdin" if resolved.entry_plugin == "read" and resolved.entry_argv == ["-"] else "render",
        "content_type": resolved.content_type,
        "session_dir": str(dirs.session_dir),
        "content_dir": str(dirs.content_dir),
        "actor": actor,
    }
    if resolved.entry_plugin != materialize_plugin:
        metadata["entrypoint"] = resolved.entry_plugin
        metadata["entry_argv"] = resolved.entry_argv
        metadata["entry_locator"] = invocation_locator(resolved.entry_plugin, resolved.entry_argv)
        metadata["provider"] = resolved.provider
    actor_dir = os.environ.get("GOTTA_ACTOR_DIR", "").strip()
    if actor_dir:
        metadata["actor_dir"] = actor_dir
    invocation_id = os.environ.get("GOTTA_INVOCATION_ID", "").strip()
    if invocation_id:
        metadata["invocation_id"] = invocation_id
    metadata.update(_derived_source_metadata(materialize_plugin, materialize_argv, payload))
    return materialize_bytes(
        payload,
        dirs=dirs,
        preferred_name=resolved.preferred_name,
        metadata=metadata,
    )


def _emit_materialization_receipt(result: Materialization | None) -> None:
    if result is None:
        return
    print(
        f"stored content: {result.name_link} (data: {result.data_path})",
        file=sys.stderr,
    )
    print(
        "locators: "
        f"{artifact_locator(result.name_link.name, result.digest)}, "
        f"{content_locator(result.digest)}",
        file=sys.stderr,
    )


def _run_callable(func: Callable[[list[str]], int], argv: list[str]) -> int:
    return int(func(argv))


def require_operational_session(dirs: ResolvedDirs) -> None:
    if not session_is_initialized(dirs.session_dir):
        raise ContentError(
            "start or bind a session first with `gotta ...` or bootstrap one "
            "manually with `gotta session init --session <root>` before running "
            "operational commands"
        )


def run_plugin(plugin: str, argv: list[str]) -> int:
    if plugin == "session":
        options = CommonOptions()
        cleaned = argv
    else:
        try:
            options, cleaned = split_common_options(argv)
        except ContentError as exc:
            return die(str(exc))

    try:
        runner = load_plugin_runner(plugin)
    except KeyError:
        plugins = ", ".join(available_plugins())
        return die(f"unknown gotta plugin: {plugin}. available plugins: {plugins}")
    except RuntimeError as exc:
        return die(str(exc), code=1)

    resolved = resolve_invocation(plugin, cleaned, options)
    if not resolved.should_materialize:
        if options.session_dir or options.content_dir:
            try:
                dirs = resolve_dirs(options, create=False)
                require_operational_session(dirs)
            except ContentError as exc:
                return die(str(exc))
            with scoped_runtime_env(dirs):
                return _run_callable(runner, cleaned)
        return _run_callable(runner, cleaned)

    try:
        dirs = resolve_dirs(options, create=False)
        require_operational_session(dirs)
    except ContentError as exc:
        return die(str(exc))

    with scoped_runtime_env(dirs):
        with capture_stdout() as capture:
            code = _run_callable(runner, cleaned)
    data = capture.getvalue()
    if code == 0:
        try:
            result = _materialize_invocation(resolved, data, dirs=dirs)
        except ContentError as exc:
            return die(str(exc), code=1)
        _emit_materialization_receipt(result)
    _emit_captured(data)
    return code
